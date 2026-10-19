# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""Turn a user request, output logs and workflow metadata into a structured build intent.

Output logs are matched to the stage that produced them with plain rules; only free
text needs the LLM judge, which is given the known workflow ids, node step ids and
main entrypoint ids so it can say which part to create or modify.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Iterable, Literal, Mapping

from flowx_core.tools.text_tools import truncate_context

Mode = Literal["skip", "create", "modify"]
_MODES = ("skip", "create", "modify")
FIX_HEADER = "Fix the following output log:"
WORKFLOW_TARGET = "workflow"

INTENT_SYSTEM_PROMPT = """\
You route requests for a workflow generator. Answer with one JSON object and no commentary.

A workflow has three build stages:
- graph: workflow.json plus its requirement analysis
- nodes: one generated Python node per node step id
- main: the FastAPI service entrypoint (main_entrypoint)

For each stage choose a mode: "create" (build it from scratch), "modify" (change what exists)
or "skip" (leave it alone). Changes to an earlier stage reach the later stages automatically,
so give a later stage a mode only when it needs its own instruction.

JSON shape:
{
  "workflow_id": "<id of an existing workflow, or null to create a new one>",
  "requirement": "<new workflow only: the complete requirement text>",
  "graph": {"mode": "skip|create|modify", "instruction": "<what to change>"},
  "nodes": {"mode": "skip|create|modify", "instruction": "<change that applies to every node>"},
  "node_amendments": {"<node step id>": "<change for that node only>"},
  "main": {"mode": "skip|create|modify", "instruction": "<what to change>"}
}

Rules:
- Use only workflow ids and node step ids that appear in the metadata.
- A change to one node's behaviour goes in node_amendments under its step id.
- Adding or removing nodes, changing their dependencies or descriptions is a graph change.
- A new workflow has "workflow_id": null, a requirement, and graph mode "create".
- Output logs belong to the stage that produced them: node test or audit logs to that node,
  graph audit logs to graph, main entrypoint logs to main.
"""


def join_text(*parts: str) -> str:
	return "\n\n".join(part for part in parts if part)


def fix_text(log: str) -> str:
	return f"{FIX_HEADER}\n{log}"


@dataclass(frozen=True)
class StagePlan:
	"""What to do in one stage: ``instruction`` is a change request, ``fix`` an output log to repair."""

	mode: Mode = "skip"
	instruction: str = ""
	fix: str = ""

	@property
	def amendment(self) -> str:
		return join_text(self.instruction, fix_text(self.fix) if self.fix else "")


@dataclass(frozen=True)
class AgentIntent:
	"""A request reformatted into stage plans; ``workflow_id=None`` means create a new workflow."""

	workflow_id: str | None = None
	requirement: str = ""
	graph: StagePlan = StagePlan()
	nodes: StagePlan = StagePlan()
	node_amendments: Mapping[str, str] = field(default_factory=dict)
	main: StagePlan = StagePlan()

	@property
	def is_empty(self) -> bool:
		return not self.node_amendments and all(
			plan.mode == "skip" for plan in (self.graph, self.nodes, self.main)
		)

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)

	@classmethod
	def from_dict(cls, data: Mapping[str, Any]) -> AgentIntent:
		"""Validate a mapping from the judge or from ``to_dict``."""
		workflow_id = data.get("workflow_id") or None
		if workflow_id is not None and not isinstance(workflow_id, str):
			raise ValueError("workflow_id must be a string or null")
		amendments = data.get("node_amendments") or {}
		if not isinstance(amendments, Mapping) or any(
			not isinstance(name, str) or not isinstance(text, str) for name, text in amendments.items()
		):
			raise ValueError("node_amendments must map node step ids to text")
		return cls(
			workflow_id=workflow_id,
			requirement=_text(data.get("requirement"), "requirement"),
			graph=_stage(data.get("graph"), "graph"),
			nodes=_stage(data.get("nodes"), "nodes"),
			node_amendments={name: text.strip() for name, text in amendments.items() if text.strip()},
			main=_stage(data.get("main"), "main"),
		)


def _text(value: Any, name: str) -> str:
	if value is None:
		return ""
	if not isinstance(value, str):
		raise ValueError(f"{name} must be a string")
	return value.strip()


def _stage(value: Any, name: str) -> StagePlan:
	if value is None:
		return StagePlan()
	if not isinstance(value, Mapping):
		raise ValueError(f"{name} must be an object")
	mode = value.get("mode", "skip")
	if mode not in _MODES:
		raise ValueError(f"{name}.mode must be one of {_MODES}")
	return StagePlan(
		mode, _text(value.get("instruction"), f"{name}.instruction"), _text(value.get("fix"), f"{name}.fix"),
	)


def match_log_to_stage(log: str, node_ids: Iterable[str]) -> tuple[str, str | None] | None:
	"""Name the stage ("graph", "nodes" with its step id, or "main") that wrote ``log``, if recognisable."""
	node_ids = tuple(node_ids)
	# The innermost traceback frame is the last one, so it decides which node failed.
	for stem in reversed(re.findall(r'File "[^"]*?([A-Za-z_]\w*)\.py"', log)):
		if stem in node_ids:
			return "nodes", stem
	lowered = log.lower()
	if re.search(r"\bgraph\b.*\baudit\b|workflow\.json|graph[_ ]plan", lowered):
		return "graph", None
	if re.search(r"main[_ ]entrypoint|main\.py|uvicorn|fastapi", lowered):
		return "main", None
	mentions = [(match.start(), name) for name in node_ids for match in re.finditer(rf"\b{re.escape(name)}\b", log)]
	return ("nodes", max(mentions)[1]) if mentions else None


def error_key(stage: str, node: str | None = None) -> str:
	"""Key under which a stage's unresolved error is retained."""
	return f"nodes/{node}" if stage == "nodes" and node else stage


def _assemble(workflow_id: str, entries: Iterable[tuple[str, str | None, str]]) -> AgentIntent:
	graph, main, nodes = [], [], {}
	for stage, node, text in entries:
		if stage == "graph":
			graph.append(text)
		elif stage == "main":
			main.append(text)
		else:
			nodes.setdefault(node, []).append(text)
	return AgentIntent(
		workflow_id=workflow_id,
		graph=StagePlan("modify", fix="\n\n".join(graph)) if graph else StagePlan(),
		nodes=StagePlan("modify") if nodes else StagePlan(),
		node_amendments={name: fix_text("\n\n".join(texts)) for name, texts in nodes.items()},
		main=StagePlan("modify", fix="\n\n".join(main)) if main else StagePlan(),
	)


def intent_from_logs(
	logs: Iterable[str],
	*,
	workflow_id: str,
	node_ids: Iterable[str],
	errors: Mapping[str, str] | None = None,
) -> tuple[AgentIntent, list[str]]:
	"""Rule-based intent: modify every stage a log or a retained error belongs to.

	``errors`` are errors retained from earlier runs, keyed ``graph``, ``main`` or
	``nodes/<step id>``. Returns the intent and the logs no rule could place.
	"""
	node_ids, entries, unmatched = tuple(node_ids), [], []
	for key, text in (errors or {}).items():
		stage, _, node = key.partition("/")
		if stage == "nodes" and not node:
			logs = [*logs, text]  # not attributed to a node: place it like any other log
		else:
			entries.append((stage, node or None, text))
	for log in logs:
		match = match_log_to_stage(log, node_ids)
		if match is None:
			unmatched.append(log)
		else:
			entries.append((*match, log))
	return _assemble(workflow_id, [entry for entry in entries if entry[0] in ("graph", "nodes", "main")]), unmatched


def merge_intents(first: AgentIntent, second: AgentIntent) -> AgentIntent:
	"""Union of two intents for one workflow; instructions are concatenated."""
	def stage(a: StagePlan, b: StagePlan) -> StagePlan:
		modes = {a.mode, b.mode}
		mode = "create" if "create" in modes else "modify" if "modify" in modes else "skip"
		return StagePlan(mode, join_text(a.instruction, b.instruction), join_text(a.fix, b.fix))

	amendments = dict(first.node_amendments)
	for name, text in second.node_amendments.items():
		amendments[name] = join_text(amendments.get(name, ""), text)
	return AgentIntent(
		workflow_id=first.workflow_id or second.workflow_id,
		requirement=first.requirement or second.requirement,
		graph=stage(first.graph, second.graph), nodes=stage(first.nodes, second.nodes),
		node_amendments=amendments, main=stage(first.main, second.main),
	)


def intent_for_target(
	target: str, instruction: str, *, workflow_id: str, catalog: Mapping[str, Mapping[str, Any]],
) -> AgentIntent:
	"""Evolve the whole workflow (``WORKFLOW_TARGET``) or one node, named by step id or node id."""
	if target == WORKFLOW_TARGET:
		return AgentIntent(workflow_id=workflow_id, graph=StagePlan("modify", instruction))
	nodes = catalog[workflow_id]["nodes"]
	name = target if target in nodes else next((step for step, node_id in nodes.items() if node_id == target), None)
	if name is None:
		raise ValueError(
			f"unknown target {target!r}: use {WORKFLOW_TARGET!r} or a node step id or node id of {sorted(nodes)}"
		)
	return AgentIntent(
		workflow_id=workflow_id, nodes=StagePlan("modify"), node_amendments={name: instruction},
	)


def resolve_intent(intent: AgentIntent, catalog: Mapping[str, Mapping[str, Any]]) -> AgentIntent:
	"""Check ids against the catalog; a new workflow always starts with a graph create."""
	if intent.workflow_id is None:
		requirement = intent.requirement or intent.graph.instruction
		if not requirement:
			raise ValueError("a new workflow needs a requirement")
		if intent.node_amendments:
			raise ValueError("a new workflow has no nodes to amend")
		return replace(intent, requirement=requirement, graph=replace(intent.graph, mode="create"))
	meta = catalog.get(intent.workflow_id)
	if meta is None:
		raise ValueError(f"unknown workflow_id: {intent.workflow_id}")
	# A graph change in the same intent may add the nodes being amended.
	unknown = set(intent.node_amendments).difference(meta["nodes"])
	if unknown and intent.graph.mode == "skip":
		raise ValueError(f"unknown node step ids: {sorted(unknown)}")
	if intent.graph.mode == "create":
		return replace(intent, requirement=intent.requirement or intent.graph.instruction)
	return intent


def _json_object(text: str) -> dict[str, Any]:
	start, end = text.find("{"), text.rfind("}")
	if start < 0 or end < start:
		raise ValueError("the judge did not return a JSON object")
	try:
		value = json.loads(text[start:end + 1])
	except json.JSONDecodeError as exc:
		raise ValueError(f"the judge returned invalid JSON: {exc}") from exc
	if not isinstance(value, dict):
		raise ValueError("the judge did not return a JSON object")
	return value


def build_judge_prompt(
	*,
	prompt: str,
	logs: Iterable[str],
	catalog: Mapping[str, Mapping[str, Any]],
	workflow_id: str | None,
	feedback: str = "",
) -> str:
	"""Metadata first (ids to match against), then the request, unmatched logs and any rejection."""
	known = {
		wid: {**meta, "errors": {
			key: truncate_context(text, label="error", max_chars=1500) for key, text in meta["errors"].items()
		}}
		for wid, meta in catalog.items()
	}
	sections = [
		"Known workflows (ids, node step ids, main entrypoint ids, unresolved errors):",
		json.dumps(known, indent=2, ensure_ascii=False, sort_keys=True),
	]
	if workflow_id:
		sections.append(f"This request targets workflow_id {workflow_id!r}; do not choose another.")
	sections += ["User request:", prompt or "(none)"]
	logs = [truncate_context(log, label="output log", max_chars=4000) for log in logs]
	if logs:
		sections += ["Output logs not matched to a stage:", *logs]
	if feedback:
		sections.append(f"Your previous answer was rejected: {feedback}\nReturn a corrected JSON object.")
	return "\n\n".join(sections)


def build_intent(
	*,
	prompt: str = "",
	logs: Iterable[str] = (),
	fix_errors: bool = False,
	workflow_id: str | None = None,
	target: str | None = None,
	catalog: Mapping[str, Mapping[str, Any]],
	judge: Callable[[str], str] | None = None,
	feedback: str = "",
) -> AgentIntent:
	"""Reformat a request: rules place logs and retained errors, the judge reads free text.

	With ``target`` (``WORKFLOW_TARGET`` or a node) the prompt is the evolve command for that
	part and the judge is not consulted.
	"""
	text, logs = prompt.strip(), list(logs)
	workflow = workflow_id
	if workflow is None and (logs or fix_errors or target) and len(catalog) == 1:
		workflow = next(iter(catalog))
	if target is not None and workflow is None:
		raise ValueError("target needs an existing workflow: pass workflow_id")
	if workflow is not None and workflow not in catalog:
		raise ValueError(f"unknown workflow_id: {workflow}")
	rule, unmatched = AgentIntent(), logs
	if workflow is not None:
		meta = catalog[workflow]
		rule, unmatched = intent_from_logs(
			logs, workflow_id=workflow, node_ids=meta["nodes"], errors=meta["errors"] if fix_errors else None,
		)
	if target is not None:
		if not text and not unmatched:
			raise ValueError("target needs an evolve command: give a prompt")
		command = join_text(text, *(fix_text(log) for log in unmatched))
		evolved = intent_for_target(target, command, workflow_id=workflow, catalog=catalog)
		return resolve_intent(merge_intents(evolved, rule), catalog)
	if not text and not unmatched:
		if rule.is_empty:
			raise ValueError("nothing to do: give a prompt, output logs or fix_errors with retained errors")
		return resolve_intent(rule, catalog)
	if workflow is None and not catalog:
		if not text:
			raise ValueError("a new workflow needs a prompt")
		return resolve_intent(AgentIntent(requirement=text, graph=StagePlan("create")), catalog)
	if judge is None:
		raise ValueError("a judge is required to interpret free text; pass an explicit intent instead")
	response = judge(build_judge_prompt(
		prompt=text, logs=unmatched, catalog=catalog, workflow_id=workflow, feedback=feedback,
	))
	judged = AgentIntent.from_dict(_json_object(response))
	if workflow is not None:
		judged = replace(judged, workflow_id=workflow)
	return resolve_intent(merge_intents(judged, rule), catalog)


__all__ = [
	"AgentIntent", "FIX_HEADER", "INTENT_SYSTEM_PROMPT", "StagePlan", "WORKFLOW_TARGET", "build_intent",
	"build_judge_prompt", "error_key", "fix_text", "intent_for_target", "intent_from_logs", "join_text",
	"match_log_to_stage", "merge_intents", "resolve_intent",
]
