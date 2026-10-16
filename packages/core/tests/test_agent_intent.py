from __future__ import annotations

import json

import pytest

from flowx_core.agent_intent import (
	AgentIntent,
	StagePlan,
	build_intent,
	build_judge_prompt,
	intent_from_logs,
	match_log_to_stage,
	merge_intents,
	resolve_intent,
)

NODES = ("Collect", "Publish")
CATALOG = {
	"wf1": {
		"nodes": {"Collect": "node-1", "Publish": "node-2"}, "workflow_version": 3,
		"main_entrypoint_id": "main-1", "has_graph": True, "has_main": True, "errors": {},
	},
	"wf2": {
		"nodes": {"Other": None}, "workflow_version": 0, "main_entrypoint_id": "main-2",
		"has_graph": True, "has_main": False, "errors": {},
	},
}


def _refuse(_prompt: str) -> str:
	raise AssertionError("the judge must not be called")


@pytest.mark.parametrize("log, expected", [
	('Traceback\n  File "/w/service/Collect.py", line 7, in process_input\nValueError: bad', ("nodes", "Collect")),
	('File "/w/main.py", line 3\nFile "/w/Publish.py", line 9\nKeyError', ("nodes", "Publish")),
	("RuntimeError: graph plan audit did not pass. Feedback:\nLine 1: bad", ("graph", None)),
	("RuntimeError: main entrypoint audit did not pass. Feedback:\nLine 4: bad", ("main", None)),
	("RuntimeError: node audit failed for Publish: [violation]", ("nodes", "Publish")),
	("something unrelated", None),
])
def test_match_log_to_stage(log, expected):
	assert match_log_to_stage(log, NODES) == expected


def test_intent_from_logs_routes_each_log_and_reports_unmatched():
	intent, unmatched = intent_from_logs(
		[
			'File "/w/Collect.py", line 1\nValueError',
			"graph plan audit did not pass",
			"main entrypoint audit did not pass",
			"no idea",
		],
		workflow_id="wf1", node_ids=NODES,
	)

	assert unmatched == ["no idea"]
	assert intent.workflow_id == "wf1"
	assert (intent.graph.mode, intent.nodes.mode, intent.main.mode) == ("modify", "modify", "modify")
	assert "graph plan audit" in intent.graph.fix and intent.graph.instruction == ""
	assert set(intent.node_amendments) == {"Collect"}
	assert intent.node_amendments["Collect"].startswith("Fix the following output log:")


def test_intent_from_logs_uses_retained_errors_by_key_and_places_unattributed_ones():
	intent, unmatched = intent_from_logs(
		[], workflow_id="wf1", node_ids=NODES,
		errors={"nodes/Publish": "boom", "graph": "bad graph", "nodes": "node audit failed for Collect: x"},
	)

	assert unmatched == []
	assert set(intent.node_amendments) == {"Publish", "Collect"}
	assert intent.graph.fix == "bad graph"
	assert intent.main.mode == "skip"


def test_from_dict_validates_and_round_trips():
	intent = AgentIntent.from_dict({
		"workflow_id": "wf1", "graph": {"mode": "modify", "instruction": " add a node "},
		"node_amendments": {"Collect": "validate", "Publish": "  "},
	})

	assert intent.graph == StagePlan("modify", "add a node")
	assert dict(intent.node_amendments) == {"Collect": "validate"}
	assert AgentIntent.from_dict(intent.to_dict()) == intent
	for bad in ({"graph": {"mode": "rebuild"}}, {"graph": "x"}, {"node_amendments": {"A": 1}}, {"workflow_id": 4}):
		with pytest.raises(ValueError):
			AgentIntent.from_dict(bad)


def test_merge_prefers_create_over_modify_and_concatenates_instructions():
	merged = merge_intents(
		AgentIntent("wf1", graph=StagePlan("modify", "a"), node_amendments={"Collect": "x"}),
		AgentIntent("wf1", graph=StagePlan("create", "b", "log"), node_amendments={"Collect": "y"}),
	)

	assert merged.graph == StagePlan("create", "a\n\nb", "log")
	assert merged.node_amendments["Collect"] == "x\n\ny"


def test_resolve_intent_rules():
	new = resolve_intent(AgentIntent(graph=StagePlan("skip", "make a collector")), {})
	assert (new.requirement, new.graph.mode) == ("make a collector", "create")
	with pytest.raises(ValueError, match="needs a requirement"):
		resolve_intent(AgentIntent(), {})
	with pytest.raises(ValueError, match="unknown workflow_id"):
		resolve_intent(AgentIntent(workflow_id="nope"), CATALOG)
	with pytest.raises(ValueError, match="unknown node step ids"):
		resolve_intent(AgentIntent("wf1", node_amendments={"Ghost": "x"}), CATALOG)
	# A graph change may introduce the node being amended.
	resolve_intent(AgentIntent("wf1", graph=StagePlan("modify", "add Ghost"), node_amendments={"Ghost": "x"}), CATALOG)


def test_new_workflow_without_known_workflows_needs_no_judge():
	intent = build_intent(prompt="  build a collector  ", catalog={}, judge=_refuse)

	assert intent.workflow_id is None and intent.requirement == "build a collector"
	assert intent.graph.mode == "create"


def test_logs_and_retained_errors_alone_never_call_the_judge():
	intent = build_intent(
		logs=['File "/w/Collect.py", line 1\nValueError: bad'], workflow_id="wf1", catalog=CATALOG, judge=_refuse,
	)
	assert set(intent.node_amendments) == {"Collect"} and intent.graph.mode == "skip"

	catalog = {"wf1": {**CATALOG["wf1"], "errors": {"main": "main entrypoint audit did not pass"}}}
	fixed = build_intent(fix_errors=True, catalog=catalog, judge=_refuse)
	assert fixed.workflow_id == "wf1" and fixed.main.mode == "modify"

	with pytest.raises(ValueError, match="nothing to do"):
		build_intent(fix_errors=True, workflow_id="wf1", catalog=CATALOG, judge=_refuse)


def test_judge_chooses_the_workflow_and_logs_are_merged_into_its_answer():
	prompts = []

	def judge(prompt):
		prompts.append(prompt)
		return "```json\n" + json.dumps({
			"workflow_id": "wf2", "graph": {"mode": "modify", "instruction": "add retries"},
		}) + "\n```"

	chosen = build_intent(prompt="add retries to the other workflow", catalog=CATALOG, judge=judge)
	assert chosen.workflow_id == "wf2" and chosen.graph == StagePlan("modify", "add retries")
	# The judge sees ids to match against.
	for expected in ("wf1", "wf2", "node-1", "main-2", "Publish"):
		assert expected in prompts[0]

	targeted = build_intent(
		prompt="add retries", workflow_id="wf1", catalog=CATALOG, judge=judge,
		logs=['File "/w/Publish.py", line 2\nKeyError'],
	)
	assert targeted.workflow_id == "wf1"
	assert targeted.graph.instruction == "add retries"
	assert set(targeted.node_amendments) == {"Publish"}


@pytest.mark.parametrize("response, message", [
	("no json here", "JSON object"),
	("{broken", "JSON object"),
	("[1, 2]", "JSON object"),
	('{"graph": {"mode": "maybe"}}', "mode"),
	('{"workflow_id": "ghost"}', "unknown workflow_id"),
])
def test_invalid_judge_answers_are_rejected(response, message):
	with pytest.raises(ValueError, match=message):
		build_intent(prompt="change something", catalog=CATALOG, judge=lambda _prompt: response)


def test_free_text_needs_a_judge_when_workflows_exist():
	with pytest.raises(ValueError, match="judge is required"):
		build_intent(prompt="change something", workflow_id="wf1", catalog=CATALOG)


def test_judge_prompt_truncates_long_errors_and_reports_rejection():
	catalog = {"wf1": {**CATALOG["wf1"], "errors": {"graph": "x" * 20000}}}
	prompt = build_judge_prompt(
		prompt="fix it", logs=["y" * 20000], catalog=catalog, workflow_id="wf1", feedback="mode was invalid",
	)

	assert len(prompt) < 9000
	assert "truncated" in prompt
	assert "do not choose another" in prompt
	assert "mode was invalid" in prompt
