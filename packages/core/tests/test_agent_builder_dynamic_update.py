import json
from pathlib import Path
from types import SimpleNamespace

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.tools.node_formats import collect_node_formats
import flowx_core.workflows.node_runtime_modifier as node_runtime_modifier_module


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_builder(monkeypatch, tmp_path: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    return AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )


def test_builder_progress_logs_to_runtime_file(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    builder._start_progress(3)

    log_path = Path(builder.runtime_log_path)
    assert log_path.is_file()

    log_text = log_path.read_text(encoding="utf-8")
    assert "Pipeline started. Total steps: 3" in log_text
    assert "Initializing" in log_text


def test_builder_session_splits_artifact_and_runtime_state(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    process = object()
    builder.graph_plan_path = str(tmp_path / "workflow.json")
    builder.node_docs_dir = str(tmp_path / "node_docs")
    builder.log_path = str(tmp_path / "runtime.log")
    builder.backend_server_process = process
    builder.dynamic_graph_cache["node_tests"] = {"NodeA": "test_NodeA.py"}
    builder.dynamic_graph_cache["server_runtime"] = {"pid": 99}

    assert builder.artifact_state.graph_plan_path == str(tmp_path / "workflow.json")
    assert builder.artifact_state.node_docs_dir == str(tmp_path / "node_docs")
    assert builder.runtime_state.log_path == str(tmp_path / "runtime.log")
    assert builder.runtime_state.backend_server_process is process
    assert builder.artifact_state.dynamic_graph_cache["node_tests"] == {"NodeA": "test_NodeA.py"}
    assert builder.runtime_state.dynamic_graph_cache["server_runtime"] == {"pid": 99}


def _write_graph(graph_path: Path, nodes: list[dict]) -> None:
    graph_path.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_generate_nodes_writes_backend_files_next_to_graph_plan(monkeypatch, tmp_path):
    project_root = tmp_path / "project_root"
    graph_dir = tmp_path / "graph_dir"
    builder = _make_builder(monkeypatch, project_root)

    requirement_path = graph_dir / "requirement.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = graph_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "GeneratedNode",
                "type": "WorkflowStepNode",
                "desc": "generated",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )

    class _FakeNodeCoder:
        def __init__(self):
            self.root_dir_path = ""
            self.write_calls = []

        def write_node_from_requirement(self, node_name, node_meta, requirement_md_path, output_path, **kwargs):
            self.write_calls.append(
                {
                    "node_name": node_name,
                    "requirement_md_path": requirement_md_path,
                    "output_path": output_path,
                    "root_dir_path": self.root_dir_path,
                }
            )
            target_path = Path(output_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.suffix != ".py":
                target_path = target_path.with_suffix(".py")
            target_path.write_text(f"class {node_name}: ...\n", encoding="utf-8")
            return str(target_path)

        def amend_code_with_feedback(self, *args, **kwargs):
            raise AssertionError("audit amendment should not be called")

    fake_node_coder = _FakeNodeCoder()
    generated_test_calls = []

    def fake_run_modifier(**kwargs):
        generated_test_calls.append(kwargs)
        path = graph_dir / "tests" / f"test_{kwargs['node_name']}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def test_{kwargs['node_name'].lower()}():\n    assert True\n", encoding="utf-8")
        return SimpleNamespace(test_file_path=str(path.resolve()))

    builder._make_node_coder = lambda node_meta: fake_node_coder
    builder._node_artifact_service.run_modifier = fake_run_modifier
    builder.node_auditor = type(
        "_FakeNodeAuditor",
        (),
        {"audit_node_file": staticmethod(lambda *args, **kwargs: (True, []))},
    )()
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    generated_paths = builder.generate_nodes()

    expected_path = (graph_dir / "GeneratedNode.py").resolve()
    assert generated_paths == [str(expected_path)]
    assert fake_node_coder.write_calls == [
        {
            "node_name": "GeneratedNode",
            "requirement_md_path": str(requirement_path),
            "output_path": str(expected_path),
            "root_dir_path": str(graph_dir.resolve()),
        }
    ]
    assert generated_test_calls == [
        {
            "node_name": "GeneratedNode",
            "workflow_name": "default",
            "graph_plan_path": str(graph_path.resolve()),
            "generate_test": True,
            "run_test": False,
            "amend_node": False,
            "temperature": 0.35,
        }
    ]
    assert expected_path.is_file()
    assert (graph_dir / "tests" / "test_GeneratedNode.py").is_file()
    assert builder.node_location_map == {"GeneratedNode": str(expected_path)}


def test_node_build_service_reuses_cached_workers_in_generation_pipeline(monkeypatch, tmp_path):
    project_root = tmp_path / "project_root"
    graph_dir = tmp_path / "graph_dir"
    builder = _make_builder(monkeypatch, project_root)

    requirement_path = graph_dir / "requirement.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = graph_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "FirstNode",
                "type": "WorkflowStepNode",
                "desc": "first",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "SecondNode",
                "type": "WorkflowStepNode",
                "desc": "second",
                "enable": True,
                "depends": ["FirstNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )

    class _FakeNodeCoder:
        def __init__(self):
            self.root_dir_path = ""
            self.write_calls = []

        def write_node_from_requirement(self, node_name, node_meta, requirement_md_path, output_path, **kwargs):
            self.write_calls.append(node_name)
            target_path = Path(output_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.suffix != ".py":
                target_path = target_path.with_suffix(".py")
            target_path.write_text(f"class {node_name}: ...\n", encoding="utf-8")
            return str(target_path)

        def amend_code_with_feedback(self, *args, **kwargs):
            raise AssertionError("audit amendment should not be called")

    fake_node_coder = _FakeNodeCoder()
    builder._make_node_coder = lambda node_meta: fake_node_coder
    builder.node_auditor = type(
        "_FakeNodeAuditor",
        (),
        {"audit_node_file": staticmethod(lambda *args, **kwargs: (True, []))},
    )()
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    workflow = builder._node_build_service.get_or_create_workflow("default")
    first_context = workflow.run(
        node_names=["SecondNode", "FirstNode"],
        language="python",
        temperature=0.35,
        reset_mappings=True,
        generate_markdowns=False,
    )

    first_generator = workflow.nodes["FirstNode"]
    second_generator = workflow.nodes["SecondNode"]

    second_context = workflow.run(
        node_names=["FirstNode"],
        language="python",
        temperature=0.1,
        reset_mappings=False,
        generate_markdowns=False,
    )

    assert fake_node_coder.write_calls == ["FirstNode", "SecondNode", "FirstNode"]
    assert builder._node_build_service.get_or_create_workflow("default") is workflow
    assert builder._node_build_service.get_or_create_workflow("separate") is not workflow
    assert list(workflow.nodes) == ["FirstNode", "SecondNode"]
    assert workflow.nodes["FirstNode"] is first_generator
    assert workflow.nodes["SecondNode"] is second_generator
    assert first_generator.total == 1
    assert first_generator.node_index == 1
    assert second_generator.node_index == 2
    assert [Path(builder.node_location_map[name]).stem for name in first_context.selected_node_names] == [
        "FirstNode", "SecondNode"
    ]
    assert second_context.selected_node_names == ["FirstNode"]


def test_cached_node_generator_can_amend_from_log_prompt(monkeypatch, tmp_path):
    project_root = tmp_path / "project_root"
    graph_dir = tmp_path / "graph_dir"
    builder = _make_builder(monkeypatch, project_root)

    requirement_path = graph_dir / "requirement.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = graph_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "GeneratedNode",
                "type": "WorkflowStepNode",
                "desc": "generated",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )

    class _FakeNodeCoder:
        def __init__(self):
            self.root_dir_path = ""
            self.amend_calls = []

        def write_node_from_requirement(self, node_name, node_meta, requirement_md_path, output_path, **kwargs):
            target_path = Path(output_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.suffix != ".py":
                target_path = target_path.with_suffix(".py")
            target_path.write_text(f"class {node_name}: ...\n", encoding="utf-8")
            return str(target_path)

        def amend_code_with_feedback(self, file_path, amendment, **kwargs):
            self.amend_calls.append(
                {
                    "file_path": file_path,
                    "amendment": amendment,
                    "kwargs": kwargs,
                }
            )

    fake_node_coder = _FakeNodeCoder()
    builder._make_node_coder = lambda node_meta: fake_node_coder
    builder.node_auditor = type(
        "_FakeNodeAuditor",
        (),
        {"audit_node_file": staticmethod(lambda *args, **kwargs: (True, []))},
    )()
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    workflow = builder._node_build_service.get_or_create_workflow()
    context = workflow.run(
        node_names=["GeneratedNode"],
        reset_mappings=True,
        generate_markdowns=False,
    )
    generator = workflow.nodes["GeneratedNode"]

    generator._amend("Traceback from node test log", 0)

    assert context.selected_node_names == ["GeneratedNode"]
    assert generator.last_generated_path == str((graph_dir / "GeneratedNode.py").resolve())
    assert fake_node_coder.amend_calls == [
        {
            "file_path": str((graph_dir / "GeneratedNode.py").resolve()),
            "amendment": "Traceback from node test log",
            "kwargs": {
                "graph_plan_path": str(graph_path),
                "requirement_md_path": str(requirement_path),
                "current_node_name": "GeneratedNode",
                "language": "python",
                "temperature": 0.35,
            },
        }
    ]


def test_node_artifact_service_routes_test_update_by_workflow_and_node_names(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExampleNode",
                "type": "WorkflowStepNode",
                "desc": "example",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )
    node_path = tmp_path / "ExampleNode.py"
    node_path.write_text("# source\n", encoding="utf-8")
    builder.graph_plan_path = str(graph_path)
    builder.node_location_map["ExampleNode"] = str(node_path)

    modifiers = {}

    class _FakeModifier:
        def __init__(self):
            self.node_test_writer = SimpleNamespace(root_dir_path="")
            self.node_writer = SimpleNamespace(root_dir_path="")
            self.calls = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                test_file_path=str(tmp_path / "tests" / "billing" / "test_ExampleNode.py"),
                log_file_path=str(tmp_path / "logs" / "billing" / "ExampleNode_test.log"),
                test_result={"node_name": "ExampleNode", "ok": False},
                amended_node_file_path=str(node_path),
                skipped_stages=[],
            )

    def get_modifier(workflow_name, node_name):
        key = (workflow_name, node_name)
        modifiers.setdefault(key, _FakeModifier())
        workflow_modifiers = builder._node_artifact_service.modifiers.setdefault(workflow_name, {})
        return workflow_modifiers.setdefault(node_name, modifiers[key])

    builder._node_artifact_service.get_or_create_modifier = get_modifier

    context = builder._node_artifact_service.run_modifier(
        workflow_name="billing",
        node_name="ExampleNode",
        graph_plan_path=str(graph_path),
        timeout=17,
    )

    modifier = modifiers[("billing", "ExampleNode")]
    assert context.test_file_path == str(tmp_path / "tests" / "billing" / "test_ExampleNode.py")
    assert context.log_file_path == str(tmp_path / "logs" / "billing" / "ExampleNode_test.log")
    assert context.test_result == {"node_name": "ExampleNode", "ok": False}
    assert context.amended_node_file_path == str(node_path)
    assert modifier.calls[0]["workflow_name"] == "billing"
    assert modifier.calls[0]["node_name"] == "ExampleNode"
    assert modifier.calls[0]["test_file_name"] == "tests/billing/test_ExampleNode.py"
    assert modifier.calls[0]["log_file_name"] == "logs/billing/ExampleNode_test.log"
    assert modifier.calls[0]["timeout"] == 17
    assert modifier.calls[0]["skip_generate_node_test"] is False
    assert modifier.calls[0]["skip_run_node_test"] is False
    assert modifier.calls[0]["skip_amend_node"] is False
    assert builder._node_artifact_service.modifiers["billing"]["ExampleNode"] is modifier
    assert builder.dynamic_graph_cache["workflow_node_tests"] == {
        "billing": {
            "ExampleNode": str(tmp_path / "tests" / "billing" / "test_ExampleNode.py")
        }
    }


def test_named_node_test_update_uses_its_workflow_graph_and_node_file(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_node = {
        "name": "SharedNode",
        "type": "WorkflowStepNode",
        "desc": "shared",
        "enable": True,
        "depends": [],
        "ext_data": {"type": "none", "desc": "none"},
    }
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    alpha_graph = alpha_dir / "workflow.json"
    beta_graph = beta_dir / "workflow.json"
    _write_graph(alpha_graph, [graph_node])
    _write_graph(beta_graph, [graph_node])
    alpha_node = alpha_dir / "SharedNode.py"
    beta_node = beta_dir / "SharedNode.py"
    alpha_node.write_text("# alpha\n", encoding="utf-8")
    beta_node.write_text("# beta\n", encoding="utf-8")

    workflow = builder._node_build_service.get_or_create_workflow("alpha")
    workflow._context = SimpleNamespace(
        graph_plan_path=str(alpha_graph),
        artifacts={"SharedNode": {"node_file_path": str(alpha_node)}},
    )
    builder.graph_plan_path = str(beta_graph)
    builder.node_location_map["SharedNode"] = str(beta_node)

    observed = []

    class _FakeModifier:
        node_test_writer = SimpleNamespace(root_dir_path="")
        node_writer = SimpleNamespace(root_dir_path="")

        def run(self, **kwargs):
            observed.append(kwargs)
            return SimpleNamespace(
                test_file_path=str(alpha_dir / "tests" / "alpha" / "test_SharedNode.py"),
                log_file_path=str(alpha_dir / "logs" / "alpha" / "SharedNode_test.log"),
                test_result={"ok": True},
                amended_node_file_path=str(alpha_node),
                skipped_stages=[],
            )

    builder._node_artifact_service.get_or_create_modifier = lambda *_args: _FakeModifier()

    builder._node_artifact_service.run_modifier(workflow_name="alpha", node_name="SharedNode")

    assert observed[0]["node_file_name"] == str(alpha_node)
    assert observed[0]["workflow_graph"] == str(alpha_graph)
    assert builder.dynamic_graph_cache["workflow_node_tests"]["alpha"]["SharedNode"].startswith(str(alpha_dir))

    builder._node_artifact_service.run_modifier(
        workflow_name="alpha",
        node_name="SharedNode",
        generate_test=False,
    )

    assert observed[1]["skip_generate_node_test"] is True
    assert observed[1]["skip_run_node_test"] is False
    assert observed[1]["workflow_graph"] == str(alpha_graph)


def test_node_artifact_service_caches_modifiers_by_workflow_and_node(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    monkeypatch.setattr(node_runtime_modifier_module, "GenerateNodeTestFile", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(node_runtime_modifier_module, "AmendNodeFromTestLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(node_runtime_modifier_module, "NodeRuntimeModifierPipeline", lambda **kwargs: SimpleNamespace(**kwargs))

    service = builder._node_artifact_service
    first = service.get_or_create_modifier("orders", "Collect")

    assert service.get_or_create_modifier("orders", "Collect") is first
    assert service.get_or_create_modifier("orders", "Publish") is not first
    assert service.get_or_create_modifier("billing", "Collect") is not first
    assert set(service.modifiers) == {"orders", "billing"}
    assert set(service.modifiers["orders"]) == {"Collect", "Publish"}


def test_sync_workflow_graph_json_does_not_duplicate_relative_root_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)

    project_dir = tmp_path / "demo_resume_workflow"
    project_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=project_dir.name,
    )

    graph_path = project_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ResumeUploadNode",
                "type": "WorkflowFileNode",
                "desc": "upload",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_file_input", "desc": "upload"},
            }
        ],
    )

    builder.graph_plan_path = str(graph_path)

    synced_path = builder._main_entrypoint_service.sync_workflow_graph_json()

    assert Path(synced_path).resolve() == graph_path.resolve()
    assert not (project_dir / project_dir.name / "workflow.json").exists()


def test_write_main_entrypoint_does_not_duplicate_relative_root_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)

    project_dir = tmp_path / "demo_resume_workflow"
    project_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=project_dir.name,
    )

    graph_path = project_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ResumeUploadNode",
                "type": "WorkflowFileNode",
                "desc": "upload",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_file_input", "desc": "upload"},
            }
        ],
    )

    captured = {}

    class _FakeMainWriter:
        def process_input(self, _user_input, _dependency_results, session_state):
            request = session_state["main_entrypoint"]
            assert request["action"] == "write"
            captured.update(request)
            output_path = Path(request["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("print('main')\n", encoding="utf-8")
            return SimpleNamespace(derived={"main_entrypoint_path": str(output_path)})

    builder.main_writer = _FakeMainWriter()
    builder.main_entry_auditor = SimpleNamespace(
        audit_main_entrypoint_file=lambda *_args, **_kwargs: (True, []),
    )

    main_path = builder._write_main_entrypoint(
        graph_plan_path=str(graph_path),
        output_filename=str(Path(project_dir.name) / "main.py"),
    )

    assert Path(main_path).resolve() == (project_dir / "main.py").resolve()
    assert Path(captured["output_path"]).resolve() == (project_dir / "main.py").resolve()


def test_collect_node_formats_collects_inputs_and_backend_card_schema(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "CollectInput",
                "type": "WorkflowStepNode",
                "desc": "collect user input",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_input", "desc": "collect input"},
                "inputs_format": {"query": "String", "limit": "NUMBER"},
            },
            {
                "name": "Summarize",
                "type": "WorkflowStepNode",
                "desc": "summarize results",
                "enable": True,
                "depends": ["CollectInput"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )

    (tmp_path / "CollectInput.py").write_text(
        "class CollectInput(WorkflowStepNode):\n"
        "    STEP_ID = 'CollectInput'\n"
        "    TITLE = 'Collect Input'\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        return StepRunOutput(card={'kind': 'summary', 'fields': [{'name': 'query', 'type': 'string'}]})\n",
        encoding="utf-8",
    )
    (tmp_path / "Summarize.py").write_text(
        "class Summarize(WorkflowStepNode):\n"
        "    STEP_ID = 'Summarize'\n"
        "    TITLE = 'Summarize'\n"
        "    INPUT_REQUIRED = False\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        return StepRunOutput(card={'kind': 'report', 'status': 'done'})\n",
        encoding="utf-8",
    )

    builder.graph_plan_path = str(graph_path)

    formats = collect_node_formats(builder, builder._load_planned_graph())

    assert formats == {
        "CollectInput": {
            "user_input_format": {"query": "string", "limit": "number"},
            "backend_output_card_format": {
                "kind": "summary",
                "fields": [{"name": "query", "type": "string"}],
            },
            "backend_node_path": str(tmp_path / "CollectInput.py"),
        },
        "Summarize": {
            "user_input_format": {},
            "backend_output_card_format": {"kind": "report", "status": "done"},
            "backend_node_path": str(tmp_path / "Summarize.py"),
        },
    }


def test_rerun_server_validates_artifacts_and_restarts_processes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowStepNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )

    (tmp_path / "node_docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_docs" / "ExistingNode.md").write_text("# ExistingNode\n", encoding="utf-8")
    (tmp_path / "ExistingNode.py").write_text("class ExistingNode: ...\n", encoding="utf-8")
    main_path = tmp_path / "main.py"
    main_path.write_text("print('main')\n", encoding="utf-8")

    class _RunningProcess:
        def __init__(self, pid=1):
            self.pid = pid
            self.terminated = False
            self.killed = False
            self._running = True

        def poll(self):
            return None if self._running else 0

        def terminate(self):
            self.terminated = True
            self._running = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True
            self._running = False

    spawned = []

    class _SpawnedProcess(_RunningProcess):
        next_pid = 100

        def __init__(self, command, cwd=None, env=None):
            super().__init__(pid=_SpawnedProcess.next_pid)
            _SpawnedProcess.next_pid += 1
            self.command = command
            self.cwd = cwd
            self.env = env
            spawned.append(self)

    def fake_popen(command, cwd=None, env=None):
        return _SpawnedProcess(command, cwd=cwd, env=env)

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/usr/bin/python3.10")

    old_backend = _RunningProcess(pid=11)
    builder.backend_server_process = old_backend
    builder.graph_plan_path = str(graph_path)
    builder.main_output_path = str(main_path)

    monkeypatch.setattr(agent_builder_module.subprocess, "Popen", fake_popen)
    runtime = builder.rerun_server(backend_port=9001)

    assert old_backend.terminated is True
    assert len(spawned) == 1
    assert runtime["backend"]["pid"] == spawned[0].pid
    assert spawned[0].command == ["/usr/bin/python3.10", str(main_path)]
    assert spawned[0].cwd == str(tmp_path)
    assert runtime["artifacts"]["backend_nodes"] == {"ExistingNode": str(tmp_path / "ExistingNode.py")}


def test_test_main_entrypoint_uses_selected_python_command(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    main_path = tmp_path / "main.py"
    main_path.write_text("print('main')\n", encoding="utf-8")

    observed: dict[str, object] = {}

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")

    def fake_run(command, cwd=None, capture_output=None, text=None, timeout=None):
        observed["command"] = command
        observed["cwd"] = cwd
        observed["capture_output"] = capture_output
        observed["text"] = text
        observed["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(agent_builder_module.subprocess, "run", fake_run)

    assert builder.test_main_entrypoint(str(main_path), log_filename="runtime_test_log.txt") is True
    assert observed["command"] == ["/custom/python", str(main_path)]
    assert observed["cwd"] == str(tmp_path)
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == 60


def test_node_modifier_reuses_tests_and_repairs_only_failed_nodes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowStepNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "AddedNode",
                "type": "WorkflowStepNode",
                "desc": "added",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )
    builder.graph_plan_path = str(graph_path)
    for node_name in ("ExistingNode", "AddedNode"):
        (tmp_path / f"{node_name}.py").write_text("# original\n", encoding="utf-8")
        test_path = tmp_path / "tests" / f"test_{node_name}.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("def test_node(): pass\n", encoding="utf-8")
    cached_test = tmp_path / "tests" / "existing_custom.py"
    (tmp_path / "tests" / "test_ExistingNode.py").rename(cached_test)
    builder.dynamic_graph_cache["node_tests"] = {"ExistingNode": str(cached_test)}

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")
    observed_calls = []
    amended = []
    subprocess_results = iter(
        [
            SimpleNamespace(returncode=1, stdout="F\n", stderr="AssertionError: boom\n"),
            SimpleNamespace(returncode=0, stdout=".\n1 passed in 0.01s\n", stderr=""),
        ]
    )

    def fake_run(command, cwd=None, capture_output=None, text=None, timeout=None):
        observed_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return next(subprocess_results)

    monkeypatch.setattr(agent_builder_module.subprocess, "run", fake_run)

    from ag_ui_workflow import WorkflowStepNode

    class TestWriter(node_runtime_modifier_module.GenerateNodeTestFile):
        def __init__(self):
            WorkflowStepNode.__init__(self)
            self.root_dir_path = str(tmp_path)

        def write_test_from_node_file(self, *_args, **_kwargs):
            raise AssertionError("existing tests must be reused")

    class NodeWriter(node_runtime_modifier_module.AmendNodeFromTestLog):
        def __init__(self):
            WorkflowStepNode.__init__(self)
            self.root_dir_path = str(tmp_path)

        def amend_code_with_feedback(self, code_path, amendment, **_kwargs):
            amended.append(Path(code_path).name)
            assert "AssertionError: boom" in amendment
            Path(code_path).write_text("# repaired\n", encoding="utf-8")
            return Path(code_path)

    def modifier_for(_workflow_name, _node_name):
        return node_runtime_modifier_module.NodeRuntimeModifierPipeline(
            node_test_writer=TestWriter(),
            node_writer=NodeWriter(),
            run_subprocess=fake_run,
            timeout_expired=TimeoutError,
        )

    builder._node_artifact_service.get_or_create_modifier = modifier_for
    service = builder._node_artifact_service
    result = {
        name: service.run_modifier(workflow_name="default", node_name=name, generate_test=False)
        for name in ("ExistingNode", "AddedNode")
    }

    assert observed_calls == [
        {
            "command": ["/custom/python", "-m", "pytest", str(cached_test), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
        {
            "command": ["/custom/python", "-m", "pytest", str(tmp_path / "tests" / "test_AddedNode.py"), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
    ]
    assert list(result) == ["ExistingNode", "AddedNode"]
    assert result["ExistingNode"].test_result["ok"] is False
    assert result["AddedNode"].test_result["ok"] is True
    assert result["ExistingNode"].test_file_path == str(cached_test)
    assert result["AddedNode"].test_file_path == str(tmp_path / "tests" / "test_AddedNode.py")
    assert result["ExistingNode"].skipped_stages == ["generate_node_test"]
    assert result["AddedNode"].skipped_stages == ["generate_node_test", "amend_node"]
    assert amended == ["ExistingNode.py"]
    assert (tmp_path / "ExistingNode.py").read_text(encoding="utf-8") == "# repaired\n"
    assert (tmp_path / "AddedNode.py").read_text(encoding="utf-8") == "# original\n"
    assert "AssertionError: boom" in Path(result["ExistingNode"].log_file_path).read_text(encoding="utf-8")
    assert "1 passed in 0.01s" in Path(result["AddedNode"].log_file_path).read_text(encoding="utf-8")