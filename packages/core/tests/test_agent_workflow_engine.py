from __future__ import annotations

import json
from graphlib import TopologicalSorter
from pathlib import Path
from types import SimpleNamespace

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


def test_request_rejects_a_non_callable_repair_input_callback():
	with pytest.raises(ValueError, match="repair_input_callback"):
		orchestrator_module.DynamicWorkflowRequest(
			workflow_json_path="workflow.json", requirement_md_path="requirement.md",
			repair_input_callback=object(),
		)


def test_repair_callback_guides_each_nodes_amendment(world):
	agent = world.agent()
	world.modifier.failing["Collect"] = 1  # first test fails; the retest passes
	seen = []

	def repair_input(context):
		seen.append(context)
		return f"guide {context.node_name}"

	result = agent.build_workflow(
		"collect and publish", node_options={"repair_input_callback": repair_input},
	)

	assert result.ok, result.errors
	assert {context.node_name for context in seen} == {"Collect", "Publish"}
	assert all(isinstance(context, orchestrator_module.NodeRepairInput) for context in seen)
	assert "guide Collect" in world.modifier.user_inputs
	assert "guide Publish" in world.modifier.user_inputs


def test_repair_input_callback_receives_the_node_test_context():
	class Step(orchestrator_module._NodeGenerationStep):
		STEP_ID = "Collect"

	contexts = []

	def callback(context):
		contexts.append(context)
		return "fix the comparison"

	run = SimpleNamespace(request=SimpleNamespace(repair_input_callback=callback))
	request = SimpleNamespace(test_log="pytest FAILED for Collect", test_result={"ok": False})

	assert Step()._ask_repair_input(run, request, 2) == "fix the comparison"
	assert contexts == [orchestrator_module.NodeRepairInput(
		node_name="Collect", attempt=2,
		test_log="pytest FAILED for Collect", test_result={"ok": False},
	)]


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
