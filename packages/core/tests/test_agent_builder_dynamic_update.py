import json
from pathlib import Path
from types import SimpleNamespace

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_builder(monkeypatch, tmp_path: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
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
    builder.dynamic_graph_cache["node_plans"] = {"NodeA": "NodeA.md"}
    builder.dynamic_graph_cache["server_runtime"] = {"pid": 99}

    assert builder.artifact_state.graph_plan_path == str(tmp_path / "workflow.json")
    assert builder.artifact_state.node_docs_dir == str(tmp_path / "node_docs")
    assert builder.runtime_state.log_path == str(tmp_path / "runtime.log")
    assert builder.runtime_state.backend_server_process is process
    assert builder.artifact_state.dynamic_graph_cache["node_plans"] == {"NodeA": "NodeA.md"}
    assert builder.runtime_state.dynamic_graph_cache["server_runtime"] == {"pid": 99}


def _write_graph(graph_path: Path, nodes: list[dict]) -> None:
    graph_path.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_update_nodes_plan_preserves_existing_files_and_generates_only_missing(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowOperationNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "AddedNode",
                "type": "WorkflowOperationNode",
                "desc": "added",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "HiddenNode",
                "type": "WorkflowOperationNode",
                "desc": "hidden",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )

    existing_plan_path = tmp_path / "node_docs" / "ExistingNode.md"
    existing_plan_path.parent.mkdir(parents=True, exist_ok=True)
    existing_plan_path.write_text("existing plan\n", encoding="utf-8")

    class _FakeNodePlanner:
        def __init__(self):
            self.plan_payloads = []

        def plan_each(self, *, requirement_text, graph_plan_text, output_dir, **kwargs):
            payload = json.loads(graph_plan_text)
            self.plan_payloads.append(payload)
            written = []
            for node in payload["nodes"]:
                path = Path(output_dir) / f"{node['name']}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {node['name']}\n", encoding="utf-8")
                written.append(path)
            return written

    builder.node_planner = _FakeNodePlanner()
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    result = builder.update_nodes_plan()

    assert existing_plan_path.read_text(encoding="utf-8") == "existing plan\n"
    assert (tmp_path / "node_docs" / "AddedNode.md").is_file()
    assert (tmp_path / "node_docs" / "HiddenNode.md").is_file()
    assert [node["name"] for node in builder.node_planner.plan_payloads[0]["nodes"]] == ["AddedNode", "HiddenNode"]
    assert result["node_plan"]["existing"] == {"ExistingNode": str(existing_plan_path)}
    assert set(result["node_plan"]["generated"]) == {"AddedNode", "HiddenNode"}
    assert "node_ui" not in result
    assert set(builder.dynamic_graph_cache["node_plans"]) == {"ExistingNode", "AddedNode", "HiddenNode"}


def test_update_nodes_generates_only_missing_backend_nodes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowOperationNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "AddedNode",
                "type": "WorkflowOperationNode",
                "desc": "added",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )

    (tmp_path / "node_docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)

    (tmp_path / "node_docs" / "ExistingNode.md").write_text("existing plan\n", encoding="utf-8")
    (tmp_path / "ExistingNode.py").write_text("class ExistingNode: ...\n", encoding="utf-8")
    (tmp_path / "tests" / "test_ExistingNode.py").write_text("def test_existing_node():\n    assert True\n", encoding="utf-8")

    class _FakeNodePlanner:
        def plan_each(self, *, requirement_text, graph_plan_text, output_dir, **kwargs):
            payload = json.loads(graph_plan_text)
            written = []
            for node in payload["nodes"]:
                path = Path(output_dir) / f"{node['name']}.md"
                path.write_text(f"# {node['name']}\n", encoding="utf-8")
                written.append(path)
            return written

    backend_calls = []
    node_test_calls = []
    main_calls = []

    def fake_generate_selected_nodes(node_names, *, language="python", temperature=0.3, reset_mappings=False):
        backend_calls.append(list(node_names))
        written = []
        for node_name in node_names:
            path = tmp_path / f"{node_name}.py"
            path.write_text(f"class {node_name}: ...\n", encoding="utf-8")
            written.append(str(path))
        builder.node_location_map = {Path(path).stem: path for path in written}
        return written

    def fake_generate_main_entrypoint(graph_plan_path, output_filename="main.py", fastapi_host="0.0.0.0", temperature=0.0, fastapi_port=8000):
        main_calls.append(
            {
                "graph_plan_path": graph_plan_path,
                "output_filename": output_filename,
                "fastapi_port": fastapi_port,
            }
        )
        output_path = Path(output_filename)
        if not output_path.is_absolute():
            output_path = tmp_path / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("print('main')\n", encoding="utf-8")
        builder.main_output_path = str(output_path)
        return str(output_path)

    def fake_generate_selected_node_tests(node_names, *, language="python", temperature=0.2):
        node_test_calls.append(list(node_names))
        written = []
        for node_name in node_names:
            path = tmp_path / "tests" / f"test_{node_name}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"def test_{node_name.lower()}():\n    assert True\n", encoding="utf-8")
            written.append(str(path))
        return written

    builder.node_planner = _FakeNodePlanner()
    builder._generate_selected_nodes = fake_generate_selected_nodes
    builder._generate_selected_node_tests = fake_generate_selected_node_tests
    builder.generate_main_entrypoint = fake_generate_main_entrypoint
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    result = builder.update_nodes(backend_port=8123)

    assert backend_calls == [["AddedNode"]]
    assert node_test_calls == [["AddedNode"]]
    assert (tmp_path / "ExistingNode.py").read_text(encoding="utf-8") == "class ExistingNode: ...\n"
    assert set(result["backend_nodes"]["generated"]) == {"AddedNode"}
    assert result["node_tests"]["existing"] == {"ExistingNode": str(tmp_path / "tests" / "test_ExistingNode.py")}
    assert set(result["node_tests"]["generated"]) == {"AddedNode"}
    assert main_calls == [
        {
            "graph_plan_path": str(graph_path),
            "output_filename": str(tmp_path / "main.py"),
            "fastapi_port": 8123,
        }
    ]
    assert json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))["nodes"][1]["name"] == "AddedNode"
    assert set(builder.dynamic_graph_cache["backend_nodes"]) == {"ExistingNode", "AddedNode"}
    assert set(builder.dynamic_graph_cache["node_tests"]) == {"ExistingNode", "AddedNode"}
    assert "node_ui" not in result


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
                "type": "WorkflowOperationNode",
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

    def fake_generate_selected_node_tests(node_names, *, language="python", temperature=0.2):
        generated_test_calls.append(
            {
                "node_names": list(node_names),
                "language": language,
                "temperature": temperature,
            }
        )
        written = []
        for node_name in node_names:
            path = graph_dir / "tests" / f"test_{node_name}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"def test_{node_name.lower()}():\n    assert True\n", encoding="utf-8")
            written.append(str(path.resolve()))
        return written

    builder._make_node_coder = lambda node_meta: fake_node_coder
    builder._generate_selected_node_tests = fake_generate_selected_node_tests
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
            "node_names": ["GeneratedNode"],
            "language": "python",
            "temperature": 0.35,
        }
    ]
    assert expected_path.is_file()
    assert (graph_dir / "tests" / "test_GeneratedNode.py").is_file()
    assert builder.node_location_map == {"GeneratedNode": str(expected_path)}
    assert builder.dynamic_graph_cache["node_tests"] == {
        "GeneratedNode": str((graph_dir / "tests" / "test_GeneratedNode.py").resolve())
    }


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
                "type": "WorkflowOperationNode",
                "desc": "first",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "SecondNode",
                "type": "WorkflowOperationNode",
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

    generated_paths = builder._node_build_service.run_node_generation_pipeline(
        ["SecondNode", "FirstNode"],
        language="python",
        temperature=0.35,
        reset_mappings=True,
    )

    first_worker = builder._node_build_service.nodes["FirstNode"]
    second_worker = builder._node_build_service.nodes["SecondNode"]

    rerun_paths = builder._node_build_service.run_node_generation_pipeline(
        ["FirstNode"],
        language="python",
        temperature=0.1,
        reset_mappings=False,
    )

    assert fake_node_coder.write_calls == ["FirstNode", "SecondNode", "FirstNode"]
    assert list(builder._node_build_service.nodes) == ["FirstNode", "SecondNode"]
    assert builder._node_build_service.nodes["FirstNode"] is first_worker
    assert builder._node_build_service.nodes["SecondNode"] is second_worker
    assert first_worker.temperature == 0.1
    assert first_worker.node_index == 1
    assert second_worker.node_index == 2
    assert [Path(path).stem for path in generated_paths] == ["FirstNode", "SecondNode"]
    assert [Path(path).stem for path in rerun_paths] == ["FirstNode"]


def test_cached_node_worker_can_amend_from_log_prompt(monkeypatch, tmp_path):
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
                "type": "WorkflowOperationNode",
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

    generated_paths = builder._node_build_service.generate_selected_nodes(
        ["GeneratedNode"],
        reset_mappings=True,
    )
    worker = builder._node_build_service.nodes["GeneratedNode"]

    worker._current_coder = None
    worker._current_resolved_file_path = None
    builder.node_coder_map = {}

    worker._amend("Traceback from node test log", 0)

    assert generated_paths == [str((graph_dir / "GeneratedNode.py").resolve())]
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


def test_sync_workflow_graph_json_does_not_duplicate_relative_root_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
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
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
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
        def write_main_entrypoint(self, **kwargs):
            captured.update(kwargs)
            output_path = Path(kwargs["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("print('main')\n", encoding="utf-8")
            return output_path

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("Main entrypoint amendment should not run in this test.")

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


def test_get_node_input_output_formats_collects_inputs_and_backend_card_schema(monkeypatch, tmp_path):
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
                "type": "WorkflowOperationNode",
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
        "class Summarize(WorkflowOperationNode):\n"
        "    STEP_ID = 'Summarize'\n"
        "    TITLE = 'Summarize'\n"
        "    def process_operation(self, dependency_results, session_state):\n"
        "        return StepRunOutput(card={'kind': 'report', 'status': 'done'})\n",
        encoding="utf-8",
    )

    builder.graph_plan_path = str(graph_path)

    formats = builder.get_node_input_output_formats()

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
    assert builder.dynamic_graph_cache["node_input_output_formats"] == formats


def test_rerun_server_validates_artifacts_and_restarts_processes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowOperationNode",
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


def test_run_node_tests_uses_selected_python_command_and_writes_separate_node_logs(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowOperationNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "AddedNode",
                "type": "WorkflowOperationNode",
                "desc": "added",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )
    builder.graph_plan_path = str(graph_path)

    existing_test_path = tmp_path / "tests" / "test_ExistingNode.py"
    added_test_path = tmp_path / "tests" / "test_AddedNode.py"
    existing_test_path.parent.mkdir(parents=True, exist_ok=True)
    existing_test_path.write_text("def test_existing_node():\n    assert False\n", encoding="utf-8")
    added_test_path.write_text("def test_added_node():\n    assert True\n", encoding="utf-8")
    builder.dynamic_graph_cache["node_tests"] = {
        "ExistingNode": str(existing_test_path),
        "AddedNode": str(added_test_path),
    }

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")

    observed_calls = []
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

    result = builder.run_node_tests(
        node_names=["AddedNode", "ExistingNode"],
        log_filename="node_tests_log.txt",
    )

    assert observed_calls == [
        {
            "command": ["/custom/python", "-m", "pytest", str(existing_test_path.resolve()), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
        {
            "command": ["/custom/python", "-m", "pytest", str(added_test_path.resolve()), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
    ]
    assert result["ok"] is False
    assert [item["node_name"] for item in result["results"]] == ["ExistingNode", "AddedNode"]
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["stderr"] == "AssertionError: boom\n"
    assert result["results"][1]["ok"] is True
    assert result["results"][1]["stdout"] == ".\n1 passed in 0.01s\n"

    summary_log_path = Path(result["log_path"])
    log_dir_path = Path(result["log_dir"])
    assert builder.log_path == str(summary_log_path)
    assert summary_log_path.is_file()
    assert log_dir_path.is_dir()
    assert result["log_paths"] == {
        "ExistingNode": result["results"][0]["log_path"],
        "AddedNode": result["results"][1]["log_path"],
    }

    summary_log_text = summary_log_path.read_text(encoding="utf-8")
    assert "Node test: ExistingNode" in summary_log_text
    assert "Node test: AddedNode" in summary_log_text
    assert str(log_dir_path) in summary_log_text

    existing_node_log_path = Path(result["results"][0]["log_path"])
    added_node_log_path = Path(result["results"][1]["log_path"])
    assert existing_node_log_path.parent == log_dir_path
    assert added_node_log_path.parent == log_dir_path
    assert existing_node_log_path.is_file()
    assert added_node_log_path.is_file()

    existing_log_text = existing_node_log_path.read_text(encoding="utf-8")
    added_log_text = added_node_log_path.read_text(encoding="utf-8")
    assert "Node: ExistingNode" in existing_log_text
    assert "AssertionError: boom" in existing_log_text
    assert "Node: AddedNode" in added_log_text
    assert "1 passed in 0.01s" in added_log_text