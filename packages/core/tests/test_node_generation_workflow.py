"""Modified: verify exact class maps, scoped settings and isolated native pools."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier

import pytest
from ag_ui_workflow import StepRunOutput, WorkflowEngine, WorkflowStepNode

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.auditor.data import RuleViolation
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.worker.node_writer import WorkflowSkillNodeCoder, WorkflowStepNodeCoder
from flowx_core.workflows import node_generation
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.architect.graph import NodeMeta


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@dataclass
class _FakePlanner(NodePlanElement):
    events: list[str] = field(default_factory=list)

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def code_to_file(self, prompt, path, **kwargs):
        Path(path).write_text("# Node plan", encoding="utf-8")
        return Path(path)


@dataclass
class _FakeWriter(WorkflowStepNodeCoder):
    events: list[str] = field(default_factory=list)
    prompt_contexts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)


@dataclass
class _FakeAuditor(NodeAuditor):
    audit: Callable[..., tuple[bool, list[RuleViolation]]] | None = None

    def __post_init__(self):
        super().__init__()

    def audit_file(self, file_path, node_meta=None, graph_plan_path=None):
        assert self.audit is not None, "the audit callable must be retained by scoped construction"
        return self.audit(file_path, node_meta, graph_plan_path=graph_plan_path)


def _make_builder(monkeypatch, root: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakePlanner)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    return AgentBuilder(api_key="key", model="model", provider="provider", root_dir=str(root))


def test_node_build_service_runs_single_node_workflow_per_kind(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Standalone", "type": "WorkflowStepNode", "desc": "standalone",
         "depends": [], "ext_data": {"type": "none"}},
    ]}), encoding="utf-8")
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Build standalone node", encoding="utf-8")

    class Planner(NodePlanElement):
        def __post_init__(self):
            apply_node_settings(self)
            WorkflowStepNode.__init__(self)

        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("# Standalone plan", encoding="utf-8")
            return Path(path)

    class Coder(_FakeWriter):
        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("class Standalone: pass\n", encoding="utf-8")
            return Path(path)

    builder.graph_plan_path = str(graph_path)
    builder.requirement_md_path = str(requirement_path)
    builder.node_planner = Planner()
    builder._make_node_coder = lambda _meta: Coder()
    builder.node_auditor = _FakeAuditor(lambda *_args, **_kwargs: (True, []))
    service = builder._node_build_service
    context = service.run()

    assert context.selected_node_names == ["Standalone"]
    assert context.artifacts["Standalone"]["node_markdown"] == "# Standalone plan"
    assert context.artifacts["Standalone"]["node_file"] == "class Standalone: pass\n"
    assert service.workflows["WorkflowStepNode"]._last_result.outputs["node_audit"].derived["ok"] is True
    builder.close()


def test_node_generation_workflow_carries_prior_artifacts_and_routes_requested_workflows(
    monkeypatch, tmp_path: Path
) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Build a small data pipeline.\n", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "Collect",
                        "type": "WorkflowStepNode",
                        "desc": "Collect input",
                        "enable": True,
                        "depends": [],
                        "ext_data": {"type": "user_input", "desc": "collect input"},
                        "inputs_format": {"query": "String"},
                    },
                    {
                        "name": "Transform",
                        "type": "WorkflowStepNode",
                        "desc": "Transform data",
                        "enable": True,
                        "depends": ["Collect"],
                        "ext_data": {"type": "none", "desc": "none"},
                    },
                    {
                        "name": "Publish",
                        "type": "WorkflowStepNode",
                        "desc": "Publish results",
                        "enable": True,
                        "depends": ["Transform"],
                        "ext_data": {"type": "none", "desc": "none"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    class _FakeNodePlanElement(_FakePlanner):
        def process_input(self, user_input, dependency_results, session_state):
            request = session_state["node_plan_request"]
            path = Path(request["output_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# {request['node']['name']} plan\n{request['requirement_text']}", encoding="utf-8"
            )
            return StepRunOutput(derived={"markdown_path": str(path.resolve())})

    generated_prompt_contexts: dict[str, str] = {}
    builder.node_planner = _FakeNodePlanElement()

    class _FakeNodeCoder(_FakeWriter):
        def write_node_from_requirement(
            self, node_name, node_meta, requirement_md_path, output_path, **kwargs
        ):
            self.prompt_contexts[node_name] = kwargs["additional_generation_context"]
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# generated {node_name}\n", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("unexpected audit repair")

    builder._make_node_coder = lambda _meta: _FakeNodeCoder(prompt_contexts=generated_prompt_contexts)
    builder.node_auditor = _FakeAuditor(lambda *_args, **_kwargs: (True, []))
    service = builder._node_build_service
    invoked_workflows: list[str] = []

    def run_review(*, node_name, artifact, context):
        invoked_workflows.append(node_name)
        assert artifact["node_file_path"].endswith("Publish.py")
        return {"reviewed": True}

    context = service.run(
        graph_plan_path=str(graph_path),
        requirement_md_path=str(requirement_path),
        run_nodes={"Collect": True, "Transform": False, "Publish": True},
        generate_markdowns=True,
        workflow_requests={"Publish": ["review"]},
        workflow_registry={"review": run_review},
    )

    assert context.selected_node_names == ["Collect", "Publish"]
    assert context.skipped_node_names == ["Transform"]
    assert set(context.artifacts) == {"Collect", "Publish"}
    assert Path(context.artifacts["Collect"]["node_markdown_path"]).is_file()
    assert Path(context.artifacts["Collect"]["node_file_path"]).is_file()
    assert "# generated Collect" in generated_prompt_contexts["Publish"]
    assert "# Collect plan" in generated_prompt_contexts["Publish"]
    assert "Build a small data pipeline." in generated_prompt_contexts["Publish"]
    assert context.node_formats["Collect"]["user_input_format"] == {"query": "string"}
    assert '"query": "string"' in generated_prompt_contexts["Collect"]
    assert invoked_workflows == ["Publish"]
    assert context.artifacts["Publish"]["workflow_outputs"] == {"review": {"reviewed": True}}
    builder.close()


def test_node_generation_has_three_ordered_stages_and_publishes_explicit_amendment(
    monkeypatch, tmp_path: Path
) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate one step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "First", "type": "WorkflowStepNode", "desc": "first", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
        {"name": "Second", "type": "WorkflowStepNode", "desc": "second", "enable": True,
         "depends": ["First"], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)
    events: list[str] = []
    configs = []
    real_init_engine = node_generation.init_workflow_engine

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_init_engine(node_classes, workflow_config)

    monkeypatch.setattr(node_generation, "init_workflow_engine", capture_config)
    (tmp_path / "Second.py").write_text("# existing Second", encoding="utf-8")

    class FakePlanner(_FakePlanner):
        def process_input(self, user_input, dependency_results, session_state):
            name = session_state["node_plan_request"]["node"]["name"]
            self.events.append(f"{name}:plan")
            path = Path(session_state["node_plan_request"]["output_path"])
            path.write_text(f"# {name} plan", encoding="utf-8")
            return StepRunOutput(derived={"markdown_path": str(path.resolve())})

    builder.node_planner = FakePlanner(events=events)

    class FakeCoder(_FakeWriter):
        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            self.events.append(f"{name}:write")
            path = Path(output_path)
            path.write_text(f"# initial {name}", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, file_path, feedback, **kwargs):
            self.events.append("Second:repair")
            assert feedback == "Apply requested fix"
            Path(file_path).write_text("# repaired Second", encoding="utf-8")
            return Path(file_path)

    builder._make_node_coder = lambda _meta: FakeCoder(events=events)

    def audit(file_path, _meta, **kwargs):
        name = Path(file_path).stem
        events.append(f"{name}:audit")
        if name == "Second" and "repaired" not in Path(file_path).read_text(encoding="utf-8"):
            return False, [RuleViolation(class_name=name, rule="bad_code", detail="repair needed", lineno=1)]
        return True, []

    builder.node_auditor = _FakeAuditor(audit)
    service = builder._node_build_service
    context = service.run(workflow_name="stages", node_amendments={"Second": "Apply requested fix"}, workflow_requests={"Second": "review"}, workflow_registry={
        "review": lambda *, artifact, **kwargs: events.append("Second:review") or artifact["node_file"]
    })

    assert len(configs) == 1  # All nodes of this kind use the same initialized pool.
    component_classes = {"node_plan": FakePlanner, "node_generate": FakeCoder, "node_audit": _FakeAuditor}
    workflow = service.workflows["WorkflowStepNode"]
    for node_classes, config in configs:
        assert node_classes == component_classes
        assert list(node_classes) == list(component_classes)
        assert all(node_classes[name] is cls for name, cls in component_classes.items())
        assert workflow._node_classes is node_classes
        assert workflow._engine.steps_meta == [cls.step_meta() for cls in component_classes.values()]
        assert config["pool_size"] == 1
        assert {node["name"]: node.get("depends", []) for node in config["nodes"]} == {
            "node_plan": [], "node_generate": ["node_plan"], "node_audit": ["node_generate"],
        }
    assert events == [
        "First:plan", "First:write", "First:audit",
        "Second:plan", "Second:repair", "Second:audit", "Second:review",
    ]
    assert context.artifacts["Second"]["node_file"] == "# repaired Second"
    assert context.artifacts["Second"]["workflow_outputs"] == {"review": "# repaired Second"}
    assert service.workflows["WorkflowStepNode"].node_coder is not None
    assert service.workflows["WorkflowStepNode"]._node_classes == component_classes
    assert builder.node_location_map["Second"] == context.artifacts["Second"]["node_file_path"]
    builder.close()


def test_failed_audit_does_not_publish_generated_node(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate a step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Broken", "type": "WorkflowStepNode", "desc": "broken", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    class FakeCoder(_FakeWriter):
        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            path = Path(output_path)
            path.write_text("# invalid", encoding="utf-8")
            return path

    builder._make_node_coder = lambda _meta: FakeCoder()
    audit_calls = []

    def audit(*_args, **_kwargs):
        audit_calls.append(1)
        return False, [RuleViolation(class_name="Broken", rule="bad_code", detail="invalid", lineno=1)]

    builder.node_auditor = _FakeAuditor(audit)
    service = builder._node_build_service

    with pytest.raises(RuntimeError, match="bad_code"):
        service.run(workflow_name="failed", generate_markdowns=False, reset_mappings=True)

    assert service.contexts["failed"] is not None
    assert "Broken" not in service.contexts["failed"].artifacts
    assert "Broken" not in builder.node_location_map
    assert "Broken" not in builder.node_coder_map
    assert audit_calls == [1]
    builder.close()


def test_generation_reuses_pool_and_constructor_snapshots_when_components_change(monkeypatch, tmp_path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    graph = tmp_path / "workflow.json"
    graph.write_text(json.dumps({"nodes": [{"name": "Example", "type": "WorkflowStepNode"}]}), encoding="utf-8")
    requirement = tmp_path / "requirement.md"
    requirement.write_text("One node", encoding="utf-8")
    builder.graph_plan_path = str(graph)
    builder.requirement_md_path = str(requirement)
    events = []

    class Planner(_FakePlanner):
        def process_input(self, user_input, dependency_results, session_state):
            self.events.append("plan")
            output_path = session_state["node_plan_request"]["output_path"]
            Path(output_path).write_text("Node plan", encoding="utf-8")
            return StepRunOutput(derived={"markdown_path": output_path})

    class Coder(_FakeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            assert "old_run" not in session_state
            session_state["old_run"] = True
            self.events.append("write")
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            Path(output_path).write_text("# generated", encoding="utf-8")
            return Path(output_path)

    builder.node_planner = Planner(events=events)
    builder._make_node_coder = lambda _meta: Coder(events=events)
    audits = []
    builder.node_auditor = _FakeAuditor(lambda *_a, **_kw: audits.append("original") or (True, []))
    service = builder._node_build_service
    try:
        service.run(generate_markdowns=False)
        workflow = service.workflows["WorkflowStepNode"]
        engine = workflow._engine
        assert workflow._last_result.outputs["node_plan"].card == {"status": "skipped"}
        first_result = workflow._last_result
        service.run(generate_markdowns=True)
        assert workflow._engine is engine
        assert workflow._last_result.request_id != first_result.request_id
        assert events == ["write", "plan", "write"]
        replacement_auditor = _FakeAuditor(lambda *_a, **_kw: pytest.fail("replacement was not constructed into the pool"))
        builder.node_auditor = replacement_auditor
        workflow.node_coder.write_node_from_requirement = lambda *_a, **_kw: pytest.fail("instance monkeypatch")

        class ReplacementCoder(_FakeWriter):
            def process_input(self, *_args, **_kwargs):
                raise AssertionError("replacement coder was not constructed into the pool")

        workflow.node_coder = ReplacementCoder()
        service.run(generate_markdowns=True)
        assert workflow.node_auditor is replacement_auditor
        assert events == ["write", "plan", "write", "write"]
        assert audits == ["original", "original", "original"]
        assert workflow._engine is engine
    finally:
        builder.close()
    assert engine.pool.closed


@pytest.mark.parametrize("pool_size", [1, 2])
def test_generation_constructs_real_nodes_and_isolates_reused_concurrent_slots(monkeypatch, tmp_path, pool_size):
    planners, coders, auditors, requests, configs = [], [], [], [], []
    barrier = Barrier(2) if pool_size == 2 else None

    @dataclass
    class Planner(_FakePlanner):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Planner
            assert self.STEP_ID == "node_plan"
            assert self.DEPENDENCIES == []
            assert self.get_workflow_param().state is session_state
            return super().process_input(user_input, dependency_results, session_state)

    @dataclass
    class Coder(_FakeWriter):
        instances: list = field(default_factory=list)
        requests: list = field(default_factory=list)
        barrier: object = None

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Coder
            assert self.STEP_ID == "node_generate"
            assert self.DEPENDENCIES == ["node_plan"]
            assert set(dependency_results) == {"node_plan"}
            assert self.get_workflow_param().state is session_state
            assert "previous_request" not in session_state
            session_state["previous_request"] = True
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            if self.barrier is not None and Path(requirement_md_path).parent.name != "third":
                self.barrier.wait(timeout=10)
            self.requests.append((self, output_path, kwargs["temperature"], kwargs["language"]))
            Path(output_path).write_text(f"# generated {Path(requirement_md_path).parent.name}", encoding="utf-8")
            return Path(output_path)

    @dataclass
    class Auditor(_FakeAuditor):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Auditor
            assert self.STEP_ID == "node_audit"
            assert self.DEPENDENCIES == ["node_generate"]
            assert set(dependency_results) == {"node_generate"}
            assert self.get_workflow_param().state is session_state
            return super().process_input(user_input, dependency_results, session_state)

    planner = Planner(instances=planners)
    coder = Coder(instances=coders, requests=requests, barrier=barrier)
    auditor = Auditor(lambda *_args, **_kwargs: (True, []), auditors)
    planners.clear()
    coders.clear()
    auditors.clear()
    original_metadata = [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Planner, Coder, Auditor)]
    config = node_generation.NodeGenerationWorkflow.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(node_generation.NodeGenerationWorkflow, "json_config", staticmethod(lambda: config))
    real_init_engine = node_generation.init_workflow_engine

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_init_engine(node_classes, workflow_config)

    monkeypatch.setattr(node_generation, "init_workflow_engine", capture_config)

    def request(label, temperature, language="python", generate_markdowns=True):
        root = tmp_path / label
        root.mkdir()
        docs = root / "node_docs"
        docs.mkdir()
        requirement = root / "requirement.md"
        requirement.write_text(label, encoding="utf-8")
        context = node_generation.NodeGenerationContext(
            graph_plan_path=str(root / "workflow.json"), requirement_md_path=str(requirement),
            requirement_text=label, node_names=["Example"], selected_node_names=["Example"],
            language=language, temperature=temperature, node_docs_dir=docs,
            generate_markdowns=generate_markdowns, raw_nodes={"Example": {"name": "Example"}},
        )
        return context, NodeMeta(name="Example", type="WorkflowStepNode", desc=label), "Example", root / "Example.py"

    workflow = node_generation.NodeGenerationWorkflow("WorkflowStepNode", planner, coder, auditor)
    engine = workflow._engine
    try:
        assert type(engine) is WorkflowEngine
        component_classes = {node.STEP_ID: node for node in (Planner, Coder, Auditor)}
        assert len(configs) == 1
        assert configs[0][0] == component_classes
        assert all(configs[0][0][name] is cls for name, cls in component_classes.items())
        assert workflow._node_classes is configs[0][0]
        assert configs[0][1] is config
        assert engine.steps_meta == [cls.step_meta() for cls in component_classes.values()]
        for instances, component, cls in ((planners, planner, Planner), (coders, coder, Coder), (auditors, auditor, Auditor)):
            assert len(instances) == pool_size
            assert all(type(node) is cls and node is not component for node in instances)
            assert len({id(node) for node in instances}) == pool_size
            assert all("process_input" not in node.__dict__ for node in instances)
            assert all(node.instances is instances for node in instances)
        assert all(node.requests is requests and node.barrier is barrier for node in coders)
        assert all(node.audit is auditor.audit for node in auditors)
        # A prototype instance cannot be used as a dispatcher by native nodes.
        planner.process_input = coder.process_input = auditor.process_input = lambda *_a, **_kw: pytest.fail("prototype dispatch")
        first, second = request("first", 0.1), request("second", 0.7, "javascript")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(workflow.run, *args) for args in (first, second)]
            paths = [future.result(timeout=15) for future in futures]
        assert paths == [str(first[-1]), str(second[-1])]
        assert sorted((Path(path).parent.name, temperature, language) for _, path, temperature, language in requests) == [
            ("first", 0.1, "python"), ("second", 0.7, "javascript"),
        ]
        if pool_size == 2:
            assert requests[0][0] is not requests[1][0]
        assert workflow.run(*request("third", 0.4, generate_markdowns=False)).endswith("third/Example.py")
        assert workflow._last_result.outputs["node_plan"].card == {"status": "skipped"}
        assert workflow._engine is engine
        assert (len(planners), len(coders), len(auditors)) == (pool_size,) * 3
        assert [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Planner, Coder, Auditor)] == original_metadata
    finally:
        workflow.close()
    assert engine.pool.closed


@pytest.mark.parametrize("pool_size", [1, 2])
def test_generation_retains_dataclass_settings_and_restores_construction_scope(monkeypatch, pool_size):
    @dataclass
    class Planner(NodePlanElement):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    @dataclass
    class Coder(WorkflowSkillNodeCoder):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    @dataclass
    class Auditor(_FakeAuditor):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    config = node_generation.NodeGenerationWorkflow.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(node_generation.NodeGenerationWorkflow, "json_config", staticmethod(lambda: config))
    workflows = []
    try:
        for label in ("first", "second"):
            planners, coders, auditors = [], [], []
            planner = Planner(
                client=object(), instances=planners, system_prompt=f"{label} planner prompt",
                session_marking_prompt=f"{label} planner marking", skills_root_path=f"/{label}/skills",
                output_path=f"/{label}/plan.md", overwrite=False, temperature=0.17, max_tokens=123,
            )
            coder = Coder(
                client=object(), instances=coders, system_prompt=f"{label} coder prompt",
                session_marking_prompt=f"{label} coder marking", skills_root_path=f"/{label}/skills",
                timeout=4.5, use_streaming=False,
            )
            auditor = Auditor(audit=lambda *_a, **_kw: (True, []), instances=auditors)
            auditor.white_list.add(label)
            planners.clear()
            coders.clear()
            auditors.clear()
            snapshots = ((planner, planners), (coder, coders), (auditor, auditors))
            with node_settings({Planner.STEP_ID: {"system_prompt": "outer prompt"}}):
                workflow = node_generation.NodeGenerationWorkflow("WorkflowStepNode", planner, coder, auditor)
                workflows.append(workflow)
                assert _FakePlanner().system_prompt == "outer prompt"
            assert _FakePlanner().system_prompt == NodePlanElement.system_prompt
            assert _FakeAuditor().audit is None
            for prototype, instances in snapshots:
                assert len(instances) == pool_size
                assert len({id(node) for node in instances}) == pool_size
                assert all(type(node) is type(prototype) and node is not prototype for node in instances)
                assert all(node.instances is instances for node in instances)
            for prototype, instances in ((planner, planners), (coder, coders)):
                assert all(node.client is prototype.client for node in instances)
                assert all(node.system_prompt == prototype.system_prompt for node in instances)
                assert all(node.system_prompt.count(prototype.session_marking_prompt) == 1 for node in instances)
                assert all(node.skills_root_path == prototype.skills_root_path for node in instances)
                prompt_snapshot = prototype.system_prompt
                prototype.system_prompt = "late replacement"
                prototype.client = object()
                assert all(node.system_prompt == prompt_snapshot for node in instances)
                assert all(node.client is not prototype.client for node in instances)
            assert all(
                (node.output_path, node.overwrite, node.temperature, node.max_tokens)
                == (planner.output_path, False, 0.17, 123) for node in planners
            )
            assert all(node.timeout == 4.5 and node.use_streaming is False for node in coders)
            assert all(node.audit is auditor.audit for node in auditors)
            whitelist_snapshot = set(auditor.white_list)
            assert all(node.white_list == whitelist_snapshot and node.white_list is not auditor.white_list for node in auditors)
            assert len({id(node.white_list) for node in auditors}) == pool_size
            auditor.white_list.add("late replacement")
            auditor.audit = lambda *_a, **_kw: pytest.fail("late audit replacement")
            assert all(node.white_list == whitelist_snapshot and node.audit is not auditor.audit for node in auditors)
        assert workflows[0]._engine is not workflows[1]._engine
        assert workflows[0]._node_classes == workflows[1]._node_classes == {
            node.STEP_ID: node for node in (Planner, Coder, Auditor)
        }
    finally:
        for workflow in workflows:
            workflow.close()
    assert all(workflow._engine.pool.closed for workflow in workflows)
