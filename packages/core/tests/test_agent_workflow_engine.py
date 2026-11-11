from __future__ import annotations

# Modified 2026-11-02: cover iterative node retries and paused-run cleanup.
# Modified 2026-11-03: cover allocated workflow creation and recovery-state reporting.
# Modified 2026-11-03: cover allocated graph creation, modification, and overwrite safeguards.
# Modified 2026-11-11: cover named workflow allocation and storage safety.

import json
import threading
from dataclasses import replace
from graphlib import TopologicalSorter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import flowx_core.agent_workflow_engine as engine_module
import flowx_core.dynamic_workflow_orchestrator as orchestrator_module
from flowx_core.agent_intent import AgentIntent, StagePlan
from flowx_core.agent_workflow_engine import AgentRequest, AgentWorkflowEngine

NODE_SOURCE = '''from ag_ui_workflow import StepRunOutput, WorkflowStepNode


class {name}(WorkflowStepNode):
    STEP_ID = "{name}"
    DEPENDENCIES = {depends}

    def process_input(self, user_input, dependency_results, session_state):
        return StepRunOutput(card={{"ok": True}}, derived={{"x": 1}})
'''
GRAPH_NODES = [{"name": "Collect"}, {"name": "Publish", "depends": ["Collect"]}]
AUDIT_FAILURE = "graph plan audit did not pass. Feedback:\nLine 1: bad"


class FakeGraph:
	"""GraphGenerationWorkflow stand-in: writes the requirement and graph like the real one."""

	def __init__(self) -> None:
		self.requests, self.fail = [], 0

	def run(self, request):
		self.requests.append(request)
		graph = Path(request.graph_plan_path)
		graph.parent.mkdir(parents=True, exist_ok=True)
		if not request.skip_analyze_requirement:
			Path(request.requirement_md_path).write_text(
				f"# requirement\n{request.requirement_text}{request.requirement_amendment}", encoding="utf-8",
			)
		if not request.skip_plan_graph:
			graph.write_text(json.dumps({"nodes": GRAPH_NODES}), encoding="utf-8")
		if self.fail:
			self.fail -= 1
			raise RuntimeError(AUDIT_FAILURE)
		return SimpleNamespace()


class FakeMain:
	def __init__(self) -> None:
		self.requests, self.contents = [], []

	def run(self, request):
		self.requests.append(request)
		self.contents.append(sorted(path.name for path in Path(request.project_root_path).iterdir()))
		Path(request.output_path).write_text("# main\n", encoding="utf-8")
		return request.output_path


class FakeNodeGeneration:
	def __init__(self) -> None:
		self.requests = []
		self.node_auditor = SimpleNamespace(audit_node_file=lambda *args, **kwargs: (True, []))

	def run(self, request):
		self.requests.append(request)
		request.target_path.write_text(NODE_SOURCE.format(
			name=request.node_meta.name, depends=list(request.raw_node.get("depends", [])),
		), encoding="utf-8")
		return str(request.target_path)


class FakeModifier:
	def __init__(self) -> None:
		self.failing: dict[str, int] = {}
		self.user_inputs: list[str] = []
		# (node, skip_generate, skip_run, skip_amend, log content when the call started)
		self.calls: list[tuple[str, bool, bool, bool, str]] = []

	def run(self, request, user_input: str = ""):
		self.user_inputs.append(user_input)
		log = Path(request.log_file_name)
		prior_log = log.read_text(encoding="utf-8") if log.is_file() else ""
		self.calls.append((
			request.node_name, request.skip_generate_node_test, request.skip_run_node_test,
			request.skip_amend_node, prior_log,
		))
		if request.skip_run_node_test:  # amend from the existing log only
			return SimpleNamespace(amended_node_file_path=request.node_file_name, test_log=prior_log, test_result=None)
		remaining = self.failing.get(request.node_name, 0)
		ok = remaining <= 0
		if not ok:
			self.failing[request.node_name] = remaining - 1
		text = f"pytest {'passed' if ok else 'FAILED'} for {request.node_name}"
		for path, content in ((request.log_file_name, text), (request.test_file_name, "def test_x():\n    assert True\n")):
			Path(path).parent.mkdir(parents=True, exist_ok=True)
			Path(path).write_text(content, encoding="utf-8")
		return SimpleNamespace(
			amended_node_file_path=request.node_file_name, test_log=text, test_result={"ok": ok},
		)

	def run_interactive(self, request, input_callback):
		value = input_callback(request)
		return self.run(request, user_input=value)


class FakeRuntime:
	def __init__(self) -> None:
		self.started, self.stopped = [], []
		self.run_log = "=== Main Entrypoint Test Log ===\n✓ Test completed successfully.\n"

	def rerun_server(self, *, main_entrypoint_path, previous_process=None):
		process = SimpleNamespace(pid=100 + len(self.started))
		self.started.append((main_entrypoint_path, previous_process))
		return process, {"pid": process.pid, "command": ["python", main_entrypoint_path]}

	def stop_managed_server_process(self, process):
		self.stopped.append(process)

	def test_main_entrypoint(self, *, main_entrypoint_path, log_path):
		Path(log_path).parent.mkdir(parents=True, exist_ok=True)
		Path(log_path).write_text(self.run_log, encoding="utf-8")


def _build_world(tmp_path, monkeypatch):
	world = SimpleNamespace(
		graph=FakeGraph(), main=FakeMain(), generation=FakeNodeGeneration(), modifier=FakeModifier(),
		runtime=FakeRuntime(), prompts=[], answer=None, root=tmp_path / "agent", agents=[],
	)

	def judge(prompt):
		world.prompts.append(prompt)
		if world.answer is None:
			raise AssertionError("the judge must not be called")
		return json.dumps(world.answer)

	def agent(**options):
		engine = AgentWorkflowEngine(storage_dir=world.root, judge=judge, runtime=lambda: world.runtime, **options)
		world.agents.append(engine)
		return engine

	world.agent = agent
	monkeypatch.setattr(engine_module, "GRAPH_GENERATION_WORKFLOW", world.graph)
	monkeypatch.setattr(engine_module, "MAIN_ENTRYPOINT_GENERATION_WORKFLOW", world.main)
	monkeypatch.setattr(orchestrator_module, "NODE_GENERATION_WORKFLOW", world.generation)
	monkeypatch.setattr(orchestrator_module, "NODE_RUNTIME_MODIFIER_PIPELINE", world.modifier)
	return world


def _close_world(world):
	for engine in world.agents:
		engine.close()
	assert not engine_module.ORCHESTRATORS


class _InlineEngine:
	"""Runs step classes in dependency order on the calling thread; native pools are costly to churn."""

	def __init__(self, classes, config):
		self.reload(classes, config)

	def reload(self, classes, config):
		self.classes = classes
		self.order = list(TopologicalSorter(
			{node["name"]: node.get("depends", []) for node in config["nodes"]},
		).static_order())

	def run_steps(self, *, state=None, **_options):
		session_state, outputs = dict(state or {}), {}
		for name in self.order:
			node_class = self.classes[name]
			dependencies = {dependency: outputs[dependency] for dependency in node_class.DEPENDENCIES}
			outputs[name] = node_class().process_input("", dependencies, session_state)
		return SimpleNamespace(outputs=outputs, status="completed")


@pytest.fixture
def world(tmp_path, monkeypatch):
	for module in (engine_module, orchestrator_module):
		monkeypatch.setattr(module, "init_workflow_engine", lambda classes, config, **_: _InlineEngine(classes, config))
		monkeypatch.setattr(module, "close_workflow_engine", lambda engine: None)
	monkeypatch.setattr(orchestrator_module, "reload_workflow_engine", lambda engine, classes, config: engine.reload(classes, config))
	world = _build_world(tmp_path, monkeypatch)
	yield world
	_close_world(world)


@pytest.fixture
def native_world(tmp_path, monkeypatch):
	world = _build_world(tmp_path, monkeypatch)
	yield world
	_close_world(world)


def _statuses(result):
	return [result.stages[name].status for name in ("reformat", "graph", "nodes", "main")]


def test_build_workflow_creates_graph_nodes_and_a_main_entrypoint_package(world):
	agent = world.agent()

	result = agent.build_workflow("collect and publish")

	assert result.ok, result.errors
	assert _statuses(result) == ["completed"] * 4
	assert result.intent.graph.mode == "create"
	assert world.graph.requests[0].requirement_text == "collect and publish"
	workflow_id = result.workflow_id
	assert agent.workflow_ids == (workflow_id,) and workflow_id in engine_module.ORCHESTRATORS
	artifacts = result.artifacts
	assert artifacts["workflow_version"] == 1
	assert set(artifacts["nodes"]) == {"Collect", "Publish"}
	main = artifacts["main_entrypoint"]
	main_path = Path(main["path"])
	# The generated main imports its nodes from its directory, which must be a valid package name.
	assert main_path.parent.name == f"service_{main['id']}" and main_path.parent.name.isidentifier()
	assert world.main.contents == [["Collect.py", "Publish.py", "workflow.json"]]
	assert dict(result.logs["nodes_audit"]) == {"Collect": "passed", "Publish": "passed"}
	assert set(result.logs["nodes_test"]) == {"Collect", "Publish"}
	assert result.errors == {}


def test_judge_picks_a_node_from_metadata_and_later_stages_follow(world):
	agent = world.agent()
	first = agent.build_workflow("collect and publish")
	collect_id = first.artifacts["nodes"]["Collect"]["node_id"]
	world.answer = {"workflow_id": None, "node_amendments": {"Collect": "validate its input"}}

	result = agent.build_workflow("make Collect validate its input", workflow_id=first.workflow_id)

	assert result.ok, result.errors
	assert [result.stages[name].status for name in ("graph", "nodes", "main")] == [
		"skipped", "completed", "completed",
	]
	# The judge saw the ids it has to match against.
	for expected in (first.workflow_id, collect_id, first.artifacts["main_entrypoint"]["id"], "Publish"):
		assert expected in world.prompts[0]
	amended = [request for request in world.generation.requests[2:] if request.node_amendment]
	assert amended[0].node_meta.name == "Collect" and "validate its input" in amended[0].node_amendment
	assert result.artifacts["workflow_version"] == 2
	assert result.artifacts["main_entrypoint"]["version"] == 2


def test_output_logs_are_matched_to_a_stage_without_the_judge(world):
	agent = world.agent()
	first = agent.build_workflow("collect and publish")
	log = 'Traceback\n  File "/service/Collect.py", line 4, in process_input\nValueError: boom'

	result = agent.build_workflow(workflow_id=first.workflow_id, logs=[log])

	assert result.ok, result.errors
	assert not world.prompts
	assert set(result.intent.node_amendments) == {"Collect"}
	assert result.stages["graph"].status == "skipped"
	assert "ValueError: boom" in world.generation.requests[2].node_amendment


def test_a_stage_is_retried_with_its_failure_until_it_passes(world):
	agent = world.agent()
	world.graph.fail = 1

	result = agent.build_workflow("collect and publish", max_retries=1)

	assert result.ok, result.errors
	assert result.stages["graph"].attempts == 2
	retry = world.graph.requests[1]
	assert retry.graph_action == "amend" and retry.skip_analyze_requirement
	assert "graph plan audit did not pass" in retry.amendment
	assert result.errors == {}


def test_exhausted_retries_block_later_stages_and_the_error_is_repaired_by_a_later_request(world):
	agent = world.agent()
	world.graph.fail = 1

	failed = agent.build_workflow("collect and publish", max_retries={"graph": 0})

	assert not failed.ok
	assert _statuses(failed) == ["completed", "failed", "blocked", "blocked"]
	assert "graph plan audit did not pass" in failed.errors["graph"]
	with pytest.raises(RuntimeError, match="graph plan audit"):
		failed.raise_for_failure()

	fixed = agent.build_workflow(workflow_id=failed.workflow_id, fix_errors=True)

	assert fixed.ok, fixed.errors
	assert not world.prompts
	assert world.graph.requests[1].amendment.startswith("Fix the following output log:")
	assert _statuses(fixed) == ["completed"] * 4
	assert fixed.errors == {}
	assert fixed.raise_for_failure() is fixed


def test_a_failing_node_is_amended_from_its_own_test_log_on_retry(world):
	agent = world.agent()
	world.modifier.failing["Collect"] = 2  # first generation: test, then retest after repair, both fail

	result = agent.build_workflow(
		"collect and publish", max_retries={"nodes": 1}, node_options={"max_repair_retries": 0},
	)

	assert result.ok, result.errors
	assert result.stages["nodes"].attempts == 2
	retries = [request for request in world.generation.requests if request.node_amendment]
	assert retries[0].node_meta.name == "Collect"
	assert "pytest FAILED for Collect" in retries[0].node_amendment
	assert dict(result.logs["nodes_test"])["Collect"] == "pytest passed for Collect"


def test_unrecovered_node_failures_are_retained_per_node(world):
	agent = world.agent()
	world.modifier.failing["Collect"] = 99

	result = agent.build_workflow("collect and publish", max_retries={"nodes": 0})

	assert _statuses(result) == ["completed", "completed", "failed", "blocked"]
	assert set(result.errors) == {"nodes/Collect"}
	assert "pytest FAILED for Collect" in result.logs["nodes_test"]["Collect"]
	assert agent.catalog()[result.workflow_id]["errors"].keys() == {"nodes/Collect"}


def test_a_failing_node_is_repaired_again_until_its_test_passes(world):
	agent = world.agent()
	world.modifier.failing["Collect"] = 3  # pass 1: test and retest fail; pass 2: test fails, retest passes

	result = agent.build_workflow("collect and publish", max_retries={"nodes": 0})

	assert result.ok, result.errors
	assert result.stages["nodes"].attempts == 1
	collect = [call[1:4] for call in world.modifier.calls if call[0] == "Collect"]
	# (skip_generate, skip_run, skip_amend): the first pass writes the test, the second reuses it.
	assert collect == [(False, False, False), (True, False, True), (True, False, False), (True, False, True)]


def test_repair_retries_are_bounded_by_max_repair_retries(world):
	agent = world.agent()
	world.modifier.failing["Collect"] = 99

	result = agent.build_workflow(
		"collect and publish", max_retries={"nodes": 0}, node_options={"max_repair_retries": 1},
	)

	assert set(result.errors) == {"nodes/Collect"}
	assert len([call for call in world.modifier.calls if call[0] == "Collect"]) == 4  # two passes, test + retest each


def test_an_audit_violation_is_amended_from_and_the_node_rechecked(world):
	agent = world.agent()
	audits = []

	def audit(path, node_meta=None, graph_plan_path=None):
		audits.append(node_meta.name)
		if node_meta.name == "Collect" and audits.count("Collect") == 1:
			return False, [SimpleNamespace(lineno=3, rule="bad_rule", detail="fix me")]
		return True, []

	world.generation.node_auditor = SimpleNamespace(audit_node_file=audit)

	result = agent.build_workflow("collect and publish", max_retries={"nodes": 0})

	assert result.ok, result.errors
	collect = [call for call in world.modifier.calls if call[0] == "Collect"]
	# Test and audit; amend from the violation report (no test run); retest and audit again.
	assert [call[1:4] for call in collect] == [(False, False, False), (True, True, False), (True, False, False)]
	assert collect[1][4] == "Line 3: bad_rule - fix me"
	assert dict(result.logs["nodes_audit"])["Collect"] == "passed"


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_request_validates_the_repair_retry_budget(value):
	with pytest.raises(ValueError, match="max_repair_retries"):
		orchestrator_module.DynamicWorkflowRequest(
			workflow_json_path="workflow.json", requirement_md_path="requirement.md", max_repair_retries=value,
		)


def test_request_rejects_a_non_boolean_await_repair_input():
	with pytest.raises(ValueError, match="await_repair_input"):
		orchestrator_module.DynamicWorkflowRequest(
			workflow_json_path="workflow.json", requirement_md_path="requirement.md",
			await_repair_input="yes",
		)


def test_a_main_run_log_error_is_routed_to_the_node_it_names(world):
	agent = world.agent()
	world.runtime.run_log = (
		"=== Main Entrypoint Test Log ===\n--- STDERR ---\nTraceback (most recent call last):\n"
		'  File "/service/Collect.py", line 3, in process_input\nValueError: boom\n'
	)

	result = agent.build_workflow("collect and publish", smoke_test_main=True, max_retries={"main": 0})

	assert _statuses(result) == ["completed", "completed", "completed", "failed"]
	assert set(result.errors) == {"nodes/Collect"}
	assert "run log reported errors" in result.errors["nodes/Collect"]


def test_the_engine_exposes_each_build_function_separately(world):
	agent = world.agent()

	created = agent.build_workflow_json("collect and publish")
	assert _statuses(created) == ["completed", "completed", "skipped", "skipped"]
	workflow_id = created.workflow_id
	assert created.artifacts["graph"] and not created.artifacts["nodes"]

	modified = agent.build_workflow_json("add auditing", workflow_id=workflow_id)
	assert world.graph.requests[1].requirement_amendment == "add auditing"
	assert world.graph.requests[1].graph_action == "amend" and not modified.artifacts["nodes"]

	nodes = agent.build_nodes(workflow_id)
	assert _statuses(nodes) == ["completed", "skipped", "completed", "skipped"]
	assert set(nodes.artifacts["nodes"]) == {"Collect", "Publish"}

	amended = agent.build_nodes(workflow_id, {"Publish": "also log"})
	assert amended.stages["nodes"].artifacts["changed_nodes"] == ["Publish"]

	main = agent.build_main_entrypoint(workflow_id)
	assert _statuses(main) == ["completed", "skipped", "skipped", "completed"]
	assert Path(main.artifacts["main_entrypoint"]["path"]).is_file()
	again = agent.build_main_entrypoint(workflow_id)
	assert again.artifacts["main_entrypoint"]["version"] == 2


def test_list_nodes_reports_step_ids_and_node_ids_once_built(world):
	agent = world.agent()
	created = agent.build_workflow_json("collect and publish")

	assert agent.list_nodes(created.workflow_id) == [
		{"step_id": "Collect", "node_id": None, "version": 0},
		{"step_id": "Publish", "node_id": None, "version": 0},
	]

	built = agent.build_nodes(created.workflow_id)
	listed = agent.list_nodes(created.workflow_id)

	assert [node["step_id"] for node in listed] == ["Collect", "Publish"]
	assert {node["step_id"]: node["node_id"] for node in listed} == {
		name: node["node_id"] for name, node in built.artifacts["nodes"].items()
	}
	with pytest.raises(KeyError):
		agent.list_nodes("missing")


def test_evolve_a_node_by_step_id_or_node_id_skips_the_judge_and_follows_to_main(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")
	publish_id = agent.list_nodes(built.workflow_id)[1]["node_id"]

	by_name = agent.evolve(built.workflow_id, "validate the input", target="Collect")
	by_id = agent.evolve(built.workflow_id, "also log", target=publish_id)

	assert by_name.ok and by_id.ok, (by_name.errors, by_id.errors)
	assert not world.prompts
	assert _statuses(by_name) == ["completed", "skipped", "completed", "completed"]
	assert by_name.stages["nodes"].artifacts["changed_nodes"] == ["Collect"]
	assert by_id.stages["nodes"].artifacts["changed_nodes"] == ["Publish"]
	assert by_id.artifacts["main_entrypoint"]["version"] == 3
	amended = [request for request in world.generation.requests[2:] if request.node_amendment]
	assert [(r.node_meta.name, r.node_amendment) for r in amended] == [
		("Collect", "validate the input"), ("Publish", "also log"),
	]


def test_evolve_the_workflow_amends_the_graph_without_the_judge(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")

	result = agent.evolve(built.workflow_id, "add auditing", target="workflow")

	assert result.ok, result.errors
	assert not world.prompts
	assert result.stages["graph"].status == "completed"
	request = world.graph.requests[-1]
	assert request.graph_action == "amend" and request.amendment == "add auditing"
	assert request.requirement_amendment == "add auditing"


def test_evolve_without_a_target_lets_the_judge_decide(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")
	world.answer = {"node_amendments": {"Publish": "also log"}}

	result = agent.evolve(built.workflow_id, "make Publish log more")

	assert result.ok, result.errors
	assert len(world.prompts) == 1
	assert set(result.intent.node_amendments) == {"Publish"}


def test_evolve_rejects_bad_commands_and_targets(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")

	with pytest.raises(ValueError, match="command"):
		agent.evolve(built.workflow_id, "  ")
	result = agent.evolve(built.workflow_id, "change it", target="Ghost", max_retries=0)
	assert not result.ok and "unknown target" in result.errors["reformat"]
	with pytest.raises(ValueError, match="target"):
		AgentRequest(prompt="x", target=" ")
	with pytest.raises(ValueError, match="either target or intent"):
		AgentRequest(target="workflow", intent=AgentIntent(workflow_id=built.workflow_id))


def test_the_main_entrypoint_follows_the_latest_workflow_version_only_when_stale(world):
	agent = world.agent()
	result = agent.build_workflow("collect and publish")

	idle = agent.build_workflow(workflow_id=result.workflow_id, intent=AgentIntent(workflow_id=result.workflow_id))
	assert _statuses(idle) == ["completed", "skipped", "skipped", "skipped"]

	agent.build_nodes(result.workflow_id, {"Collect": "tweak"})
	follow = agent.build_workflow(workflow_id=result.workflow_id, intent=AgentIntent(workflow_id=result.workflow_id))
	assert follow.stages["main"].status == "completed"
	assert follow.artifacts["main_entrypoint"]["version"] == 2


def test_run_rerun_and_stop_a_service_through_the_runtime_service(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")
	main_path = built.artifacts["main_entrypoint"]["path"]

	first = agent.run_service(built.workflow_id)
	second = agent.run_service(built.workflow_id)

	assert first["pid"] == 100 and second["pid"] == 101
	assert first["main_entrypoint_id"] == built.artifacts["main_entrypoint"]["id"]
	assert first["base_url"] == "http://127.0.0.1:8000"
	assert [call[0] for call in world.runtime.started] == [main_path, main_path]
	assert world.runtime.started[1][1].pid == 100  # the previous process is handed over for replacement
	agent.stop_service(built.workflow_id)
	assert [process.pid for process in world.runtime.stopped] == [101]

	served = agent.build_workflow(workflow_id=built.workflow_id, intent=AgentIntent(workflow_id=built.workflow_id), serve=True)
	assert served.service["pid"] == 102
	with pytest.raises(FileNotFoundError):
		agent.run_service(agent.build_workflow_json("only a graph").workflow_id)


def test_base_url_maps_bind_addresses_to_loopback(tmp_path):
	record = engine_module.WorkflowRecord("wf", tmp_path, main_entrypoint_id="main")
	assert record.base_url == "http://127.0.0.1:8000"

	record.fastapi_host, record.fastapi_port = "::", 9001
	assert record.base_url == "http://[::1]:9001"

	record.fastapi_host, record.fastapi_port = "example.test", 9002
	assert record.base_url == "http://example.test:9002"

	record.fastapi_host, record.fastapi_port = "127.0.0.1", 9123
	record.save()
	assert engine_module.WorkflowRecord.load(tmp_path).base_url == "http://127.0.0.1:9123"


def test_closing_the_engine_stops_started_services_and_unregisters_orchestrators(world):
	agent = world.agent()
	built = agent.build_workflow("collect and publish")
	agent.run_service(built.workflow_id)

	agent.close()
	agent.close()

	assert [process.pid for process in world.runtime.stopped] == [100]
	assert not engine_module.ORCHESTRATORS
	with pytest.raises(RuntimeError, match="closed"):
		agent.build_workflow("anything")


def test_workflows_are_found_again_after_a_restart(world):
	built = world.agent().build_workflow("collect and publish")
	world.agents[0].close()

	restarted = world.agent()

	assert restarted.workflow_ids == (built.workflow_id,)
	record = restarted.record(built.workflow_id)
	assert str(record.main_entrypoint_path) == built.artifacts["main_entrypoint"]["path"]
	assert record.main_entrypoint_id == built.artifacts["main_entrypoint"]["id"]
	catalog = restarted.catalog()[built.workflow_id]
	assert set(catalog["nodes"]) == {"Collect", "Publish"} and catalog["has_main"]
	world.answer = {"node_amendments": {"Publish": "also log"}}
	result = restarted.build_workflow("log in Publish", workflow_id=built.workflow_id)
	assert result.ok and result.artifacts["main_entrypoint"]["version"] == 2


def test_request_problems_are_reported_as_a_failed_reformat_stage(world):
	agent = world.agent()

	result = agent.build_workflow("anything", workflow_id="missing", max_retries=0)

	assert not result.ok and result.workflow_id == ""
	assert _statuses(result) == ["failed", "blocked", "blocked", "blocked"]
	assert "unknown workflow_id" in result.errors["reformat"]
	assert agent.workflow_ids == ()

	world.answer = {"workflow_id": None}
	agent.build_workflow("first")
	world.answer = {"graph": {"mode": "maybe"}}
	bad = agent.build_workflow("second", workflow_id=agent.workflow_ids[0], max_retries=1)
	assert "mode" in bad.errors["reformat"]
	assert len(world.prompts) == 2 and "previous answer was rejected" in world.prompts[1]


def test_building_requires_the_published_workflows(world, monkeypatch):
	agent = world.agent()
	monkeypatch.setattr(engine_module, "MAIN_ENTRYPOINT_GENERATION_WORKFLOW", None)

	with pytest.raises(RuntimeError, match="has not published"):
		agent.build_workflow("collect")


@pytest.mark.parametrize("options", [
	{"max_retries": -1}, {"max_retries": True}, {"max_retries": {"graph": -1}}, {"max_retries": {"typo": 1}},
])
def test_request_validates_the_retry_budget(options):
	with pytest.raises(ValueError, match="max_retries"):
		AgentRequest(**options)


def test_request_normalizes_logs_and_resolves_per_stage_retries():
	assert AgentRequest(logs="one log").logs == ("one log",)
	assert AgentRequest(logs=["a", "b"]).logs == ("a", "b")
	request = AgentRequest(max_retries={"nodes": 4})
	assert (request.retries("nodes"), request.retries("graph")) == (4, 1)
	assert AgentRequest(max_retries=0).retries("main") == 0
	assert StagePlan("modify", "x").amendment == "x"


def test_real_native_engines_run_the_whole_request_pipeline(native_world):
	agent = native_world.agent()

	created = agent.build_workflow("collect and publish")
	native_world.answer = {"node_amendments": {"Collect": "validate its input"}}
	modified = agent.build_workflow("validate in Collect", workflow_id=created.workflow_id)

	assert created.ok and _statuses(created) == ["completed"] * 4, created.errors
	assert modified.ok and _statuses(modified) == ["completed", "skipped", "completed", "completed"], modified.errors
	assert any("validate its input" in request.node_amendment for request in native_world.generation.requests)
	assert modified.artifacts["main_entrypoint"]["version"] == 2


def test_allocated_workflow_builds_with_create_intent(native_world):
	agent = native_world.agent()
	workflow_id = agent.resolve_record(None).workflow_id
	result = agent.build_new_workflow(workflow_id, "collect and publish", cascade=False)
	assert result.ok, result.errors
	assert result.workflow_id == workflow_id and result.intent.graph.mode == "create"
	assert native_world.graph.requests[0].requirement_text == "collect and publish"
	with pytest.raises(ValueError, match="already has"):
		agent.build_new_workflow(workflow_id, "do not overwrite")


def test_named_allocation_persists_and_legacy_ids_reload(tmp_path, monkeypatch):
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda *args, **kwargs: None)
	root = tmp_path / "workflows"
	agent = AgentWorkflowEngine(storage_dir=root)
	try:
		record = agent.create_record("Ticket Triage")
		legacy = agent.resolve_record(None)
		assert record.workflow_id == "Ticket Triage"
		assert record.storage_dir == root / "Ticket Triage"
		assert json.loads((record.storage_dir / "agent.json").read_text())["workflow_id"] == "Ticket Triage"
		assert legacy.workflow_id != record.workflow_id
		assert agent.catalog()["Ticket Triage"]["has_graph"] is False
	finally:
		agent.close()
	reloaded = AgentWorkflowEngine(storage_dir=root)
	try:
		assert set(reloaded.workflow_ids) == {record.workflow_id, legacy.workflow_id}
		assert reloaded.record("Ticket Triage").storage_dir == root / "Ticket Triage"
	finally:
		reloaded.close()


def test_worker_can_allocate_while_run_lock_is_held(tmp_path, monkeypatch):
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda *args, **kwargs: None)
	agent = AgentWorkflowEngine(storage_dir=tmp_path / "workflows")
	created = []
	try:
		with agent._run_lock:
			worker = threading.Thread(target=lambda: created.append(agent.resolve_record(None)))
			worker.start()
			worker.join(timeout=3)
			assert not worker.is_alive(), "allocation must not wait on the caller's run lock"
		assert len(created) == 1
	finally:
		agent.close()


@pytest.mark.parametrize("name", ["", " ", ".", "..", "../escape", "/absolute", "a/b", "a\\b",
	" trailing ", "trailing.", "bad\nname", "bad:name", "CON", "lpt1.txt"])
def test_invalid_workflow_names_are_rejected_without_writing(tmp_path, monkeypatch, name):
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda *args, **kwargs: None)
	agent = AgentWorkflowEngine(storage_dir=tmp_path / "workflows")
	try:
		with pytest.raises(ValueError, match="safe single folder"):
			agent.create_record(name)
		assert list(agent.storage_dir.iterdir()) == []
	finally:
		agent.close()


@pytest.mark.parametrize("kind", ["record", "directory", "file", "symlink"])
def test_named_allocation_never_overwrites_existing_paths(tmp_path, monkeypatch, kind):
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda *args, **kwargs: None)
	agent = AgentWorkflowEngine(storage_dir=tmp_path / "workflows")
	path = agent.storage_dir / "Ticket Triage"
	try:
		if kind == "record":
			agent.create_record("Ticket Triage")
		elif kind == "directory":
			path.mkdir()
			(path / "keep").write_text("original")
		elif kind == "file":
			path.write_text("original")
		else:
			path.symlink_to(tmp_path / "missing")
		with pytest.raises(FileExistsError):
			agent.create_record("Ticket Triage")
		if kind == "directory":
			assert (path / "keep").read_text() == "original"
		elif kind == "file":
			assert path.read_text() == "original"
		elif kind == "symlink":
			assert path.is_symlink()
	finally:
		agent.close()


def test_allocated_graph_creates_and_modifies_without_building_nodes(native_world):
	agent = native_world.agent()
	workflow_id = agent.resolve_record(None).workflow_id
	created = agent.build_allocated_workflow_json(workflow_id, "collect and publish", temperature=0.17)
	assert created.ok, created.errors
	assert created.workflow_id == workflow_id and created.intent.graph.mode == "create"
	assert _statuses(created) == ["completed", "completed", "skipped", "skipped"]
	assert native_world.graph.requests[0].requirement_text == "collect and publish"
	modified = agent.build_allocated_workflow_json(workflow_id, "add auditing", temperature=0.23)
	assert modified.ok, modified.errors
	assert modified.intent.graph.mode == "modify"
	assert _statuses(modified) == ["completed", "completed", "skipped", "skipped"]
	assert native_world.graph.requests[1].requirement_amendment == "add auditing"
	assert not modified.artifacts["nodes"] and not native_world.main.requests


def test_named_workflow_graph_build_uses_named_folder(native_world):
	agent = native_world.agent()
	workflow_id = agent.create_record("Ticket Triage").workflow_id
	result = agent.build_allocated_workflow_json(workflow_id, "Classify support tickets")
	assert result.ok, result.errors
	assert result.workflow_id == "Ticket Triage"
	assert Path(result.artifacts["graph"]) == native_world.root / "Ticket Triage" / "plan" / "workflow.json"
	assert native_world.graph.requests[0].requirement_text == "Classify support tickets"


def test_legacy_implicit_allocation_builds_graph_in_native_worker(native_world):
	agent = native_world.agent()
	result = agent.build_workflow_json("Classify support tickets")
	assert result.ok, result.errors
	assert len(result.workflow_id) == 32
	assert Path(result.artifacts["graph"]) == native_world.root / result.workflow_id / "plan" / "workflow.json"


@pytest.fixture
def allocated_graph_unit(tmp_path, monkeypatch):
	"""Exercise the request wrapper without the stale world fixtures or native pools."""
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda *args, **kwargs: None)
	agent = AgentWorkflowEngine(storage_dir=tmp_path / "agent")
	record = engine_module.WorkflowRecord("allocated-unit", agent.storage_dir / "allocated-unit", "main")
	agent._records[record.workflow_id] = record
	result = object()
	run = Mock(return_value=result)
	monkeypatch.setattr(agent, "run", run)
	try:
		yield SimpleNamespace(agent=agent, record=record, run=run, result=result)
	finally:
		agent.close()


def test_allocated_graph_creates_from_prompt_without_cascade(allocated_graph_unit):
	unit = allocated_graph_unit
	prompt = "Collect and publish tickets"

	result = unit.agent.build_allocated_workflow_json(unit.record.workflow_id, prompt, temperature=0.17)

	assert result is unit.result
	unit.run.assert_called_once()
	request = unit.run.call_args.args[0]
	assert isinstance(request, AgentRequest)
	assert request.intent == AgentIntent(
		workflow_id=unit.record.workflow_id, requirement=prompt, graph=StagePlan("create"),
	)
	assert request.cascade is False
	assert request.temperature == 0.17


def test_allocated_graph_modifies_existing_graph_without_cascade(allocated_graph_unit):
	unit = allocated_graph_unit
	unit.record.graph_path.parent.mkdir(parents=True)
	unit.record.graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
	unit.record.requirement_path.write_text("# Existing requirements", encoding="utf-8")
	prompt = "Add priority to collected tickets"

	result = unit.agent.build_allocated_workflow_json(unit.record.workflow_id, prompt, temperature=0.23)

	assert result is unit.result
	unit.run.assert_called_once()
	request = unit.run.call_args.args[0]
	assert isinstance(request, AgentRequest)
	assert request.intent == AgentIntent(
		workflow_id=unit.record.workflow_id, graph=StagePlan("modify", prompt),
	)
	assert request.cascade is False
	assert request.temperature == 0.23


@pytest.mark.parametrize("existing_artifact", ["graph", "requirement"])
def test_allocated_graph_rejects_partial_artifacts_without_overwriting(allocated_graph_unit, existing_artifact):
	unit = allocated_graph_unit
	path = unit.record.graph_path if existing_artifact == "graph" else unit.record.requirement_path
	path.parent.mkdir(parents=True)
	content = '{"nodes": [{"name": "Collect"}]}' if existing_artifact == "graph" else "# Existing requirements"
	path.write_text(content, encoding="utf-8")

	with pytest.raises(ValueError):
		unit.agent.build_allocated_workflow_json(unit.record.workflow_id, "Do not overwrite")

	unit.run.assert_not_called()
	assert path.read_text(encoding="utf-8") == content
	missing = unit.record.requirement_path if existing_artifact == "graph" else unit.record.graph_path
	assert not missing.exists()


@pytest.mark.parametrize("prompt", ["", " \t\n"])
@pytest.mark.parametrize("has_graph", [False, True])
def test_allocated_graph_rejects_empty_prompt(allocated_graph_unit, prompt, has_graph):
	unit = allocated_graph_unit
	if has_graph:
		unit.record.graph_path.parent.mkdir(parents=True)
		unit.record.graph_path.write_text('{"nodes": [{"name": "Collect"}]}', encoding="utf-8")
		unit.record.requirement_path.write_text("# Existing requirements", encoding="utf-8")

	with pytest.raises(ValueError, match="prompt"):
		unit.agent.build_allocated_workflow_json(unit.record.workflow_id, prompt)

	unit.run.assert_not_called()


class _PauseOrchestrator:
	"""Minimal orchestrator: pause once at Reply's gate, then publish the stage."""

	def __init__(self, workflow_id: str) -> None:
		self.workflow_id = workflow_id
		self.workflow_version = 1
		self.generated_node_id_map = {}
		self.last_logs = SimpleNamespace(test={}, audit={})
		self.last_stage_output = None
		self.started, self.resumed, self.closed = [], [], []

	def get_workflow_version(self):
		return SimpleNamespace(version=1, node_versions={})

	def start(self, request, agent_state=None):
		self.started.append(request)
		self.last_logs.test["Reply"] = "pytest FAILED for Reply"
		return orchestrator_module.OrchestratorRun(
			request_id="r1", status="awaiting_input",
			awaiting={"id": "Reply__amend_node", "title": "Amend Reply", "prompt": "Fix Reply"},
		)

	def resume(self, request_id, step_id, value):
		self.resumed.append((request_id, step_id, value))
		stage = {"attempts": 1, "error": "", "changed": True, "artifacts": {}}
		self.last_stage_output = {
			"workflow_id": self.workflow_id, "intent": None, "error": "",
			"stages": {
				"reformat": {"stage": "reformat", "status": "completed", **stage},
				"graph": {"stage": "graph", "status": "completed", **stage},
				"nodes": {"stage": "nodes", "status": "completed", **stage},
				"main": {"stage": "main", "status": "completed", **stage},
			},
		}
		return orchestrator_module.OrchestratorRun(
			request_id=request_id, status="completed", version=SimpleNamespace(version=1),
		)

	def close(self, request_id=None):
		self.closed.append(request_id)


def _pause_agent(tmp_path, monkeypatch):
	monkeypatch.setattr(engine_module, "init_workflow_engine", lambda classes, config, **_: _InlineEngine(classes, config))
	monkeypatch.setattr(engine_module, "close_workflow_engine", lambda engine: None)
	monkeypatch.setattr(engine_module, "GRAPH_GENERATION_WORKFLOW", SimpleNamespace())
	monkeypatch.setattr(engine_module, "MAIN_ENTRYPOINT_GENERATION_WORKFLOW", SimpleNamespace())
	agent = AgentWorkflowEngine(storage_dir=tmp_path / "agent")
	record = agent.resolve_record(None)
	record.graph_path.parent.mkdir(parents=True, exist_ok=True)
	record.graph_path.write_text(json.dumps({"nodes": [{"name": "Reply"}]}), encoding="utf-8")
	real = engine_module.ORCHESTRATORS.pop(record.workflow_id)
	real.close()
	fake = _PauseOrchestrator(record.workflow_id)
	engine_module.ORCHESTRATORS[record.workflow_id] = fake
	request = AgentRequest(
		workflow_id=record.workflow_id,
		intent=AgentIntent(workflow_id=record.workflow_id, nodes=StagePlan("create")),
		await_repair_input=True,
	)
	return agent, fake, request


def test_a_failed_node_test_pauses_the_build_and_answer_resumes_it(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	try:
		paused = agent.run(request)

		assert paused.status == "input_required" and paused.ok
		assert paused.request_id == "r1"
		assert paused.awaiting["stepId"] == "Reply__amend_node"
		assert paused.awaiting["node"] == "Reply"
		assert paused.awaiting["test_log"] == "pytest FAILED for Reply"
		assert paused.stages["nodes"].status == "awaiting"
		assert fake.started[0].await_repair_input is True

		with pytest.raises(RuntimeError, match="waiting for input"):
			agent.run(request)
		assert agent.pending_requests()[0]["request_id"] == "r1"

		done = agent.answer("r1", "use a fallback", "Reply__amend_node")

		assert done.status == "completed" and done.ok
		assert _statuses(done) == ["completed"] * 4
		assert fake.resumed == [("r1", "Reply__amend_node", "use a fallback")]
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_abort_releases_a_paused_build(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	try:
		assert agent.run(request).status == "input_required"

		agent.abort("r1")

		assert fake.closed == ["r1"]
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_recovery_state_distinguishes_paused_and_native_cleanup(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	try:
		assert agent.build_recovery_state(request.workflow_id)["status"] == "clear"
		paused = agent.run(request)
		state = agent.build_recovery_state(request.workflow_id)
		assert state["status"] == "input_required" and state["request_id"] == paused.request_id
		agent.abort(paused.request_id)
		fake._pending = {"native-id": object()}
		assert agent.build_recovery_state(request.workflow_id)["status"] == "cleanup_required"
	finally:
		agent.close()


def test_abort_can_retry_if_native_cancellation_fails(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	close = fake.close

	def fail_close(request_id=None):
		raise RuntimeError("cancellation failed")

	try:
		agent.run(request)
		monkeypatch.setattr(fake, "close", fail_close)
		with pytest.raises(RuntimeError, match="cancellation failed"):
			agent.abort("r1")
		assert [item["request_id"] for item in agent.pending_requests()] == ["r1"]
		monkeypatch.setattr(fake, "close", close)
		agent.abort("r1")
		assert fake.closed == ["r1"]
		assert agent.pending_requests() == []
	finally:
		monkeypatch.setattr(fake, "close", close)
		agent.close()


def test_answer_defaults_to_the_current_waiting_step(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	try:
		paused = agent.run(request)
		# Caller-visible payloads are copies, not the retained waiting metadata.
		paused.awaiting["stepId"] = "wrong"
		agent.pending_requests()[0]["awaiting"]["stepId"] = "wrong"
		result = agent.answer("r1", "repair it")
		assert result.status == "completed" and result.ok
		assert fake.resumed == [("r1", "Reply__amend_node", "repair it")]
	finally:
		agent.close()


def test_node_start_exception_retries_then_pauses(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts, original_start = [], fake.start

	def fail_once(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		if len(starts) == 1:
			raise RuntimeError("temporary node start failure")
		return original_start(build, agent_state)

	monkeypatch.setattr(fake, "start", fail_once)
	try:
		paused = agent.run(replace(request, max_retries={"nodes": 1}))
		assert paused.status == "input_required"
		assert paused.stages["nodes"].attempts == 2
		assert starts == [1, 2]
		assert agent.answer(paused.request_id).ok
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_exhausted_node_start_errors_report_blocked_unobserved_head(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts = []

	def always_fail(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		raise RuntimeError("cannot initialize nodes")

	monkeypatch.setattr(fake, "start", always_fail)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 2}))
		assert starts == [1, 2, 3]
		assert not result.ok and result.status == "completed"
		assert _statuses(result) == ["blocked", "blocked", "failed", "blocked"]
		assert result.stages["nodes"].attempts == 3
		assert result.errors == {"nodes": "RuntimeError: cannot initialize nodes"}
		assert agent.record(request.workflow_id).errors == result.errors
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_node_failure_uses_observed_head_context(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	observed = {
		"workflow_id": request.workflow_id,
		"intent": request.intent.to_dict(),
		"stages": {
			"reformat": engine_module.StageOutcome("reformat", "completed", 1).to_dict(),
			"graph": engine_module.StageOutcome("graph", "skipped").to_dict(),
		},
		"error": "",
	}

	def fail_after_head(build, agent_state=None):
		fake.last_stage_output = observed
		raise RuntimeError("failure after the head")

	monkeypatch.setattr(fake, "start", fail_after_head)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 0}))
		assert _statuses(result) == ["completed", "skipped", "failed", "blocked"]
		assert result.intent == request.intent
		assert result.errors == {"nodes": "RuntimeError: failure after the head"}
	finally:
		agent.close()


def test_head_failure_blocks_nodes_without_spending_node_retries(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts = []

	def fail_head(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		record = agent.record(request.workflow_id)
		record.errors["graph"] = "graph planning failed"
		fake.last_stage_output = {
			"workflow_id": request.workflow_id,
			"intent": request.intent.to_dict(),
			"stages": {
				"reformat": engine_module.StageOutcome("reformat", "completed", 1).to_dict(),
				"graph": engine_module.StageOutcome("graph", "failed", 1, "graph planning failed").to_dict(),
			},
			"error": "graph planning failed",
		}
		raise RuntimeError("graph planning failed")

	monkeypatch.setattr(fake, "start", fail_head)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 2}))
		assert starts == [1]
		assert not result.ok
		assert _statuses(result) == ["completed", "failed", "blocked", "blocked"]
		assert result.errors == {"graph": "graph planning failed"}
	finally:
		agent.close()


def test_disabled_graph_keeps_head_context_when_body_publishes_without_steps(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	record = agent.record(request.workflow_id)
	record.graph_path.write_text(json.dumps({"nodes": [{"name": "Reply", "enable": False}]}), encoding="utf-8")
	monkeypatch.setattr(fake, "start", lambda *args, **kwargs: orchestrator_module.OrchestratorRun(
		request_id="", status="completed", version=SimpleNamespace(version=1),
	))
	try:
		result = agent.run(request)
		assert result.ok
		assert _statuses(result) == ["completed", "skipped", "completed", "skipped"]
		assert result.intent == request.intent
	finally:
		agent.close()


def test_node_start_exception_does_not_reuse_an_old_head_context(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	fake.last_stage_output = {
		"workflow_id": request.workflow_id,
		"intent": request.intent.to_dict(),
		"stages": {
			"reformat": engine_module.StageOutcome("reformat", "completed", 1).to_dict(),
			"graph": engine_module.StageOutcome("graph", "completed", 1).to_dict(),
		},
		"error": "",
	}

	def fail(*args, **kwargs):
		raise RuntimeError("failed before a new head")

	monkeypatch.setattr(fake, "start", fail)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 0}))
		assert _statuses(result) == ["blocked", "blocked", "failed", "blocked"]
	finally:
		agent.close()


def test_node_failure_with_only_node_context_blocks_unknown_head(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)

	def fail_with_node_context(build, agent_state=None):
		fake.last_stage_output = {
			"stages": {"nodes": engine_module.StageOutcome("nodes", "failed", 1, "failed").to_dict()},
			"error": "failed",
		}
		return orchestrator_module.OrchestratorRun(request_id="", status="failed", error="failed")

	monkeypatch.setattr(fake, "start", fail_with_node_context)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 0}))
		assert _statuses(result) == ["blocked", "blocked", "failed", "blocked"]
		assert result.workflow_id == request.workflow_id
		assert result.errors == {"nodes": "failed"}
	finally:
		agent.close()


@pytest.mark.parametrize("observed_stage", [False, True])
@pytest.mark.parametrize("entry", ["start", "resume"])
def test_exhausted_node_failure_preserves_structured_logs(tmp_path, monkeypatch, observed_stage, entry):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)

	def fail_with_logs(request_id):
		fake.last_logs.test["Reply"] = "pytest FAILED for Reply"
		fake.last_logs.audit["Reply"] = "audit violation"
		if observed_stage:
			fake.last_stage_output = {
				"stages": {"nodes": {"artifacts": {"other": "preserved"}}},
				"error": "node failed",
			}
		return orchestrator_module.OrchestratorRun(
			request_id=request_id, status="failed", error="node failed", failures={"Reply": "node failed"},
		)

	if entry == "start":
		monkeypatch.setattr(fake, "start", lambda *args, **kwargs: fail_with_logs(""))
	else:
		monkeypatch.setattr(fake, "resume", lambda request_id, step_id, value: fail_with_logs(request_id))
	try:
		result = agent.run(replace(request, max_retries={"nodes": 0}))
		if entry == "resume":
			assert result.status == "input_required"
			result = agent.answer(result.request_id)
		assert result.logs == {
			"nodes_test": {"Reply": "pytest FAILED for Reply"},
			"nodes_audit": {"Reply": "audit violation"},
		}
		if observed_stage:
			assert result.stages["nodes"].artifacts["other"] == "preserved"
		fake.last_logs.test["Reply"] = "later test"
		assert result.logs["nodes_test"]["Reply"] == "pytest FAILED for Reply"
	finally:
		agent.close()


def test_node_start_exception_does_not_expose_previous_run_logs(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	fake.last_logs.test["Reply"] = "previous build's test"
	fake.last_logs.audit["Reply"] = "previous build's audit"

	def fail_before_new_run(*args, **kwargs):
		raise RuntimeError("could not start")

	monkeypatch.setattr(fake, "start", fail_before_new_run)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 0}))
		assert not result.ok
		assert result.logs == {}
	finally:
		agent.close()


def test_node_start_failure_preserves_a_completed_head_context(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	context = {
		"workflow_id": request.workflow_id,
		"intent": request.intent.to_dict(),
		"stages": {
			"reformat": engine_module.StageOutcome("reformat", "completed", 1).to_dict(),
			"graph": engine_module.StageOutcome("graph", "skipped").to_dict(),
		},
		"error": "",
	}

	def fail(*args, **kwargs):
		raise RuntimeError("failed")

	monkeypatch.setattr(fake, "start", fail)
	try:
		paused, final = agent._nodes_start(
			replace(request, max_retries={"nodes": 0}), agent.record(request.workflow_id), context,
			head_done=True, graph_stable=False,
		)
		assert paused is None
		result = agent._result(request, final)
		assert _statuses(result) == ["completed", "skipped", "failed", "blocked"]
		assert result.intent == request.intent
		assert result.errors == {"nodes": "RuntimeError: failed"}
	finally:
		agent.close()


def test_node_start_does_not_swallow_base_exception(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)

	def interrupted(*args, **kwargs):
		raise KeyboardInterrupt()

	monkeypatch.setattr(fake, "start", interrupted)
	try:
		with pytest.raises(KeyboardInterrupt):
			agent.run(request)
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_node_start_does_not_retry_when_native_cleanup_failed(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts = []

	def fail_cleanup(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		fake._pending = {"native-request": object()}
		raise RuntimeError("native cancellation failed")

	monkeypatch.setattr(fake, "start", fail_cleanup)
	try:
		with pytest.raises(RuntimeError, match="native cancellation failed"):
			agent.run(replace(request, max_retries={"nodes": 2}))
		assert starts == [1]
	finally:
		agent.close()


def test_large_node_retry_budget_does_not_recurse(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts = []

	def always_fail(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		return orchestrator_module.OrchestratorRun(request_id="", status="failed", error="still failing")

	monkeypatch.setattr(fake, "start", always_fail)
	try:
		result = agent.run(replace(request, max_retries={"nodes": 1500}))
		assert len(starts) == 1501
		assert starts[0] == 1 and starts[-1] == 1501
		assert result.stages["nodes"].attempts == 1501
		assert result.errors == {"nodes": "still failing"}
	finally:
		agent.close()


def test_pause_failure_retries_with_node_feedback_and_new_request_id(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts, original_start, original_resume = [], fake.start, fake.resume

	def start(build, agent_state=None):
		starts.append((build, agent_state["agent_nodes_attempt"]))
		if len(starts) == 1:
			return original_start(build, agent_state)
		return orchestrator_module.OrchestratorRun(
			request_id="r2", status="awaiting_input", awaiting={"id": "Reply__amend_node"},
		)

	def resume(request_id, step_id, value):
		if request_id == "r1":
			fake.resumed.append((request_id, step_id, value))
			fake.last_logs.test["Reply"] = "assertion failed after repair"
			return orchestrator_module.OrchestratorRun(
				request_id=request_id, status="failed", error="node tests failed",
				failures={"Reply": "node tests failed"},
			)
		return original_resume(request_id, step_id, value)

	monkeypatch.setattr(fake, "start", start)
	monkeypatch.setattr(fake, "resume", resume)
	try:
		first = agent.run(replace(request, max_retries={"nodes": 1}))
		second = agent.answer(first.request_id, "first answer")
		assert second.status == "input_required" and second.request_id == "r2"
		assert second.stages["nodes"].attempts == 2
		assert [attempt for _, attempt in starts] == [1, 2]
		feedback = starts[1][0].node_amendments["Reply"]
		assert "node tests failed" in feedback and "assertion failed after repair" in feedback
		assert [item["request_id"] for item in agent.pending_requests()] == ["r2"]
		done = agent.answer("r2", "second answer")
		assert done.ok and done.status == "completed"
		assert fake.resumed == [
			("r1", "Reply__amend_node", "first answer"),
			("r2", "Reply__amend_node", "second answer"),
		]
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_paused_build_keeps_its_original_retry_budget(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	retries = {"nodes": 0}
	request = replace(request, max_retries=retries)

	def fail_resume(request_id, step_id, value):
		return orchestrator_module.OrchestratorRun(
			request_id=request_id, status="failed", error="test still fails",
			failures={"Reply": "test still fails"},
		)

	monkeypatch.setattr(fake, "resume", fail_resume)
	try:
		paused = agent.run(request)
		retries["nodes"] = 9
		result = agent.answer(paused.request_id)
		assert not result.ok and result.stages["nodes"].attempts == 1
		assert len(fake.started) == 1
		assert "test still fails" in result.errors["nodes/Reply"]
	finally:
		agent.close()


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_resume_exception_retries_unless_cleanup_failed(
	tmp_path, monkeypatch, cleanup_pending,
):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	request = replace(request, max_retries={"nodes": 0})

	def fail_resume(request_id, step_id, value):
		if cleanup_pending:
			fake._pending = {request_id: object()}
		raise RuntimeError("resume failed")

	monkeypatch.setattr(fake, "resume", fail_resume)
	try:
		agent.run(request)
		if cleanup_pending:
			with pytest.raises(RuntimeError, match="resume failed"):
				agent.answer("r1", "answer")
			assert len(agent.pending_requests()) == 1
			agent.abort("r1")
			assert fake.closed == ["r1"]
		else:
			result = agent.answer("r1", "answer")
			assert not result.ok
			assert result.errors == {"nodes": "RuntimeError: resume failed"}
		assert agent.pending_requests() == []
	finally:
		agent.close()


def test_resume_exception_starts_a_fresh_attempt(tmp_path, monkeypatch):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)
	starts, original_start = [], fake.start

	def start(build, agent_state=None):
		starts.append(agent_state["agent_nodes_attempt"])
		if len(starts) == 1:
			return original_start(build, agent_state)
		return orchestrator_module.OrchestratorRun(
			request_id="r2", status="awaiting_input", awaiting={"id": "Reply__amend_node"},
		)

	def fail_resume(*args):
		raise RuntimeError("native resume failed")

	monkeypatch.setattr(fake, "start", start)
	monkeypatch.setattr(fake, "resume", fail_resume)
	try:
		first = agent.run(replace(request, max_retries={"nodes": 1}))
		second = agent.answer(first.request_id)
		assert second.status == "input_required" and second.request_id == "r2"
		assert second.stages["nodes"].attempts == 2
		assert starts == [1, 2]
		assert [item["request_id"] for item in agent.pending_requests()] == ["r2"]
		agent.abort("r2")
	finally:
		agent.close()


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_resume_base_exception_propagates_and_keeps_only_live_pending(tmp_path, monkeypatch, cleanup_pending):
	agent, fake, request = _pause_agent(tmp_path, monkeypatch)

	def interrupted(request_id, step_id, value):
		if cleanup_pending:
			fake._pending = {request_id: object()}
		raise KeyboardInterrupt()

	monkeypatch.setattr(fake, "resume", interrupted)
	try:
		agent.run(request)
		with pytest.raises(KeyboardInterrupt):
			agent.answer("r1")
		assert bool(agent.pending_requests()) is cleanup_pending
		if cleanup_pending:
			agent.abort("r1")
	finally:
		agent.close()
