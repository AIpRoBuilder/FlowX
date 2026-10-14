"""Modified: direct native construction, minimal JSON and request lifecycle."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from threading import Barrier
from typing import Any

import pytest
from ag_ui_workflow import RunResult, StepRunOutput, WorkflowFileNode, WorkflowPipelinePool, WorkflowStepNode
from pydaograph import GElement

import flowx_core.tools.workflow_engine as workflow_engine_module
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.llm_client.coder import Coder
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.tools.workflow_engine import (
    close_workflow_engine,
    init_workflow_engine,
    run_workflow,
)


class _FirstStep(WorkflowStepNode):
    STEP_ID = "first"
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results, session_state
        return StepRunOutput(derived={"value": 2})


class _SecondStep(WorkflowStepNode):
    STEP_ID = "second"
    DEPENDENCIES = ["first"]
    INPUT_REQUIRED = False

    def __init__(self, *, received=None) -> None:
        super().__init__()
        self.received = received if received is not None else []

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input
        session_state.get("received", self.received).append(dependency_results)
        return StepRunOutput(derived={"value": dependency_results["first"].derived["value"] + 3})


def test_build_init_and_run_workflow() -> None:
    received = []
    NODES = (_FirstStep, _SecondStep)
    NODE_CLASSES = {node.STEP_ID: node for node in NODES}
    workflow_config = {"nodes": [{"name": "first"}, {"name": "second", "depends": ["first"]}]}
    assert _FirstStep.__name__ not in GElement.registeredClasses()
    engine = init_workflow_engine(
        NODE_CLASSES, workflow_config,
        graph_config_id="test-workflow-engine",
    )
    try:
        result = run_workflow(engine, request_id="first-request", session_state={"received": received})
        assert engine.steps_meta[1]["dependencies"] == ["first"]
        assert isinstance(engine.pool, WorkflowPipelinePool)
        assert isinstance(result, RunResult)
        assert result.status == "completed"
        assert result.new_steps == ["first", "second"]
        assert result.outputs["first"].derived == {"value": 2}
        assert received[0]["first"].derived == {"value": 2}
        assert result.outputs["second"].derived == {"value": 5}
        assert engine.awaiting_input(result.request_id) is None
        assert run_workflow(engine, request_id="second-request").outputs == result.outputs
    finally:
        close_workflow_engine(engine)
    assert engine.pool.closed


def test_run_workflow_preserves_native_input_normalization() -> None:
    class EchoStep(WorkflowStepNode):
        STEP_ID = "echo"

        def process_input(self, user_input, dependency_results, session_state):
            return StepRunOutput(derived={"input": user_input, "marker": session_state.get("marker")})

    engine = init_workflow_engine({"echo": EchoStep}, {"nodes": [{"name": "echo"}]})
    try:
        cases = [
            ({"text": "hello", "mode": "brief"}, "hello"),
            ({"input": "hello", "other": "value"}, "hello"),
            ({"value": 42}, "42"),
            ('{"text": "hello"}', "hello"),
            (["one", "two"], '["one", "two"]'),
            (None, ""),
        ]
        for value, expected in cases:
            result = run_workflow(engine, inputs={"echo": value}, session_state={"marker": 1})
            assert result.outputs["echo"].derived == {"input": expected, "marker": 1}
    finally:
        close_workflow_engine(engine)


def test_workflow_does_not_reserve_a_hidden_control_step_name() -> None:
    class ControlStep(_FirstStep):
        STEP_ID = "_flowx_run_request"

    engine = init_workflow_engine({"_flowx_run_request": ControlStep}, {"nodes": [{"name": "_flowx_run_request"}]})
    try:
        result = run_workflow(engine, session_state={"marker": 1})
        assert result.outputs["_flowx_run_request"].derived == {"value": 2}
    finally:
        close_workflow_engine(engine)


def test_run_workflow_rejects_failure_and_skips_dependents() -> None:
    events = []

    class FailingStep(WorkflowStepNode):
        STEP_ID = "broken"
        INPUT_REQUIRED = False

        def process_input(self, user_input, dependency_results, session_state):
            if not session_state.get("recovered"):
                events.append("broken")
                raise ValueError("invalid output")
            return StepRunOutput(derived={"value": 2})

    class DownstreamStep(WorkflowStepNode):
        STEP_ID = "downstream"
        DEPENDENCIES = ["broken"]

        def process_input(self, user_input, dependency_results, session_state):
            events.append("downstream")
            return StepRunOutput()

    steps = {"broken": FailingStep, "downstream": DownstreamStep}
    engine = init_workflow_engine(steps, {"nodes": [{"name": "broken"}, {"name": "downstream", "depends": ["broken"]}]})
    try:
        with pytest.raises(RuntimeError, match="workflow step 'broken' failed:.*invalid output"):
            run_workflow(engine)
        assert events == ["broken"]
        assert run_workflow(engine, session_state={"recovered": True}).status == "completed"
        assert events == ["broken", "downstream"]
    finally:
        close_workflow_engine(engine)


def test_run_workflow_seeds_fresh_state_and_reuses_native_pool() -> None:
    states = []
    dependencies_seen = []

    class StatefulStep(WorkflowStepNode):
        STEP_ID = "stateful"

        def process_input(self, user_input, dependency_results, session_state):
            states.append(dict(session_state))
            assert "previous" not in session_state
            session_state["previous"] = user_input
            return StepRunOutput(derived={"value": user_input})

    class FollowingStep(WorkflowStepNode):
        STEP_ID = "following"
        DEPENDENCIES = ["stateful"]

        def process_input(self, user_input, dependency_results, session_state):
            dependencies_seen.append(dependency_results)
            return StepRunOutput(derived={"previous": session_state.get("previous")})

    # Native JSON construction resolves topology, even in reverse order.
    engine = init_workflow_engine(
        {"following": FollowingStep, "stateful": StatefulStep},
        {"nodes": [{"name": "following", "depends": ["stateful"]}, {"name": "stateful"}]},
    )
    pool = engine.pool
    try:
        first = run_workflow(engine, inputs={"stateful": "one"}, session_state={"marker": 1})
        second = run_workflow(engine, inputs={"stateful": "two"}, session_state={"marker": 2})
        skipped = run_workflow(engine, skipped_steps=["stateful"])
        assert states == [{"marker": 1}, {"marker": 2}]
        assert first.outputs["following"].derived == {"previous": "one"}
        assert second.outputs["following"].derived == {"previous": "two"}
        assert skipped.outputs["following"].derived == {"previous": None}
        assert skipped.outputs["stateful"].card == {"status": "skipped"}
        assert dependencies_seen[-1]["stateful"].derived == {}
        assert first.outputs["stateful"].derived == {"value": "one"}
        assert engine.pool is pool
    finally:
        close_workflow_engine(engine)


@pytest.mark.parametrize("nodes", [
    [{"name": "first", "depends": ["missing"]}, {"name": "second"}],
    [{"name": "first", "depends": ["second"]}, {"name": "second", "depends": ["first"]}],
])
def test_native_pool_rejects_invalid_dependencies(nodes) -> None:
    class First(_FirstStep):
        DEPENDENCIES = nodes[0].get("depends", [])

    class Second(_SecondStep):
        DEPENDENCIES = nodes[1].get("depends", [])

    NODES = (First, Second)
    NODE_CLASSES = {node.STEP_ID: node for node in NODES}
    with pytest.raises(RuntimeError):
        init_workflow_engine(NODE_CLASSES, {"nodes": nodes})


def test_run_workflow_rejects_non_step_output() -> None:
    class InvalidStep(WorkflowStepNode):
        STEP_ID = "invalid"

        def process_input(self, user_input, dependency_results, session_state):
            return {"value": 1}

    engine = init_workflow_engine({"invalid": InvalidStep}, {"nodes": [{"name": "invalid"}]})
    try:
        with pytest.raises(RuntimeError, match="process_input must return StepRunOutput"):
            run_workflow(engine)
        with pytest.raises(ValueError, match="unknown workflow step"):
            run_workflow(engine, inputs={"missing": "value"})
    finally:
        close_workflow_engine(engine)


def test_initializer_forwards_supplied_nodes_and_class_map_unchanged(monkeypatch) -> None:
    captured = {}

    class Pool:
        def __init__(self, *, pipeline_json, node_classes, size):
            captured.update(pipeline_json=pipeline_json, node_classes=node_classes, size=size)

    class Engine:
        def __init__(self, **kwargs):
            self.pool = kwargs["pool"]
            captured.update(kwargs)

    monkeypatch.setattr(workflow_engine_module, "_WorkflowPipelinePool", Pool)
    monkeypatch.setattr(workflow_engine_module, "WorkflowEngine", Engine)
    node_classes = {"first": _FirstStep, "second": _SecondStep}
    nodes = [
        {"name": "first", "type": "_FirstStep", "meta_type": "GNode", "loop": 1,
         "description": "保留配置", "config": {"labels": ["original"]}},
        {"name": "second", "depends": ["first"]},
    ]
    workflow_config = {"pool_size": 4, "nodes": nodes}
    snapshot = json.dumps(workflow_config)
    init_workflow_engine(node_classes, workflow_config)
    assert captured["node_classes"] is node_classes
    assert captured["pipeline_json"] == json.dumps({"nodes": nodes})
    assert json.loads(captured["pipeline_json"])["nodes"] == nodes
    assert json.dumps(workflow_config) == snapshot
    assert captured["size"] == 4
    assert captured["graph_config_id"] == "flowx"


def test_native_pool_accepts_supplied_node_fields() -> None:
    nodes = [{"name": "first", "type": "_FirstStep", "meta_type": "GNode", "loop": 1}]
    engine = init_workflow_engine({"first": _FirstStep}, {"nodes": nodes})
    try:
        assert run_workflow(engine).outputs["first"].derived == {"value": 2}
        assert "depends" not in nodes[0]
    finally:
        close_workflow_engine(engine)


def _native_explicit_classes_reference() -> None:
    created = []

    class ConfiguredNode(WorkflowStepNode):
        def __init__(self):
            super().__init__()
            self.offset = 0
            apply_node_settings(self)
            created.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is NODE_CLASSES[self.STEP_ID]
            assert "process_input" not in self.__dict__
            total = sum(output.derived["value"] for output in dependency_results.values())
            return StepRunOutput(derived={"value": total + self.offset, "marker": session_state["marker"]})

    class Parent(ConfiguredNode):
        STEP_ID = "parent"

    class Child(ConfiguredNode):
        STEP_ID = "child"
        DEPENDENCIES = ["parent"]

    NODES = (Parent, Child)
    NODE_CLASSES = {node.STEP_ID: node for node in NODES}
    with node_settings({"parent": {"offset": 2}, "child": {"offset": 3}}):
        engine = init_workflow_engine(
            NODE_CLASSES,
            {"pool_size": 2, "nodes": [{"name": "child", "depends": ["parent"]}, {"name": "parent"}]},
        )
    try:
        assert engine.steps_meta[0]["id"] == "child"
        assert engine.steps_meta[0]["dependencies"] == ["parent"]
        assert len(created) == 4
        assert len({id(node) for node in created}) == 4
        assert all(type(node) is NODE_CLASSES[node.STEP_ID] for node in created)
        first = run_workflow(engine, session_state={"marker": 1})
        second = run_workflow(engine, session_state={"marker": 2})
        assert first.outputs["child"].derived == {"value": 5, "marker": 1}
        assert second.outputs["child"].derived == {"value": 5, "marker": 2}
        assert len(created) == 4
        assert Parent.STEP_ID == "parent"
        assert Parent.DEPENDENCIES == []
        assert Child.STEP_ID == "child"
        assert Child.DEPENDENCIES == ["parent"]
        assert sorted(node.STEP_ID for node in created) == ["child", "child", "parent", "parent"]
    finally:
        close_workflow_engine(engine)


def test_explicit_classes_construct_fresh_original_node_types_per_slot() -> None:
    # Keep the two-slot native lifetime isolated from repeated pool destruction
    # in other tests, just like the generated backend and interactive reference.
    script = f"import runpy; runpy.run_path({__file__!r})['_native_explicit_classes_reference']()"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", [_FirstStep(), object, "_FirstStep", lambda: _FirstStep()])
def test_workflow_config_rejects_non_node_classes(value) -> None:
    with pytest.raises(TypeError, match="concrete GElement class"):
        init_workflow_engine({"first": value}, {"nodes": [{"name": "first"}]})


@pytest.mark.parametrize("nodes", [
    [{"name": "alias"}],
    [{"name": "first", "depends": ["missing"]}],
])
def test_workflow_config_rejects_mismatched_class_identity(nodes) -> None:
    with pytest.raises(ValueError, match="class STEP_ID/DEPENDENCIES"):
        init_workflow_engine({nodes[0]["name"]: _FirstStep}, {"nodes": nodes})


def test_minimal_json_preserves_file_node_support() -> None:
    class FileNode(WorkflowFileNode):
        STEP_ID = "file"

        def process_input(self, file_bytes, dependency_results, session_state):
            assert type(self) is FileNode
            return StepRunOutput(derived={"size": len(file_bytes)})

    engine = init_workflow_engine({"file": FileNode}, {"nodes": [{"name": "file"}]})
    try:
        assert run_workflow(engine, inputs={"file": b"Ada"}).outputs["file"].derived == {"size": 3}
    finally:
        close_workflow_engine(engine)


def test_initializer_uses_class_metadata_without_constructing_nodes(monkeypatch) -> None:
    captured = {}

    class ConfiguredFirst(_FirstStep):
        TITLE = "Configured title"

        def __init__(self):
            pytest.fail("only the native pool may construct a node")

    class Pool:
        def __init__(self, *, pipeline_json, node_classes, size):
            captured["node_classes"] = node_classes

    class Engine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(workflow_engine_module, "_WorkflowPipelinePool", Pool)
    monkeypatch.setattr(workflow_engine_module, "WorkflowEngine", Engine)
    NODES = (ConfiguredFirst,)
    NODE_CLASSES = {node.STEP_ID: node for node in NODES}
    init_workflow_engine(NODE_CLASSES, {"nodes": [{"name": "first"}]})
    assert captured["node_classes"] is NODE_CLASSES
    assert captured["steps_meta"] == [ConfiguredFirst.step_meta()]


@pytest.mark.parametrize("config, message", [
    ({"nodes": []}, "non-empty nodes"),
    ({"nodes": [{"name": ""}]}, "non-empty strings"),
    ({"nodes": [{"name": "first"}, {"name": "first"}]}, "exactly once"),
    ({"nodes": [{"name": "missing"}]}, "exactly once"),
    ({"nodes": [{"name": "first", "depends": ["first"]}]}, "itself"),
    ({"nodes": [{"name": "first", "depends": "missing"}]}, "list of node names"),
    ({"nodes": [{"name": "first", "loop": 2}]}, "single-pass"),
    ({"nodes": [{"name": "first"}], "pool_size": True}, "positive integer"),
    ({"nodes": [{"name": "first"}], "pool_size": 0}, "positive integer"),
])
def test_workflow_configuration_checks_only_helper_contract(config, message) -> None:
    with pytest.raises(ValueError, match=message):
        init_workflow_engine({"first": _FirstStep}, config)


def _native_pause_and_continue_reference() -> None:
    class AskNameNode(WorkflowStepNode):
        STEP_ID = "ask_name"
        INPUT_REQUIRED = True

        def __init__(self):
            super().__init__()
            self.setWaitFunc(self.wait_function)

        def wait_function(self):
            param = self.get_workflow_param()
            with param.condition:
                if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                    return True
                ready = bool(param.inputs.get(self.STEP_ID))
                param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
                return ready

        def process_input(self, user_input, dependency_results, session_state):
            return StepRunOutput(derived={"name": user_input})

    class GreetNode(WorkflowStepNode):
        STEP_ID = "greet"
        DEPENDENCIES = ["ask_name"]

        def process_input(self, user_input, dependency_results, session_state):
            return StepRunOutput(derived={"greeting": f"Hello, {dependency_results['ask_name'].derived['name']}!"})

    NODES = (AskNameNode, GreetNode)
    NODE_CLASSES = {node.STEP_ID: node for node in NODES}
    engine = init_workflow_engine(
        NODE_CLASSES,
        {"nodes": [{"name": "ask_name"}, {"name": "greet", "depends": ["ask_name"]}]},
        graph_config_id="demo",
    )
    try:
        waiting = engine.run("req-1")
        assert waiting.status == "awaiting_input"
        assert waiting.awaiting["id"] == "ask_name"
        result = engine.answer("req-1", "Ada")
        assert result.status == "completed"
        assert result.request_id == "req-1"
        assert result.outputs["greet"].derived == {"greeting": "Hello, Ada!"}
        assert engine.run("req-2").status == "awaiting_input"
    finally:
        close_workflow_engine(engine)
    assert engine.pool.closed


def test_native_engine_can_pause_and_continue_like_reference() -> None:
    # Match a server's one-pool process lifetime; native wait callbacks are not
    # reliable after unrelated pools have been repeatedly built and destroyed.
    script = f"import runpy; runpy.run_path({__file__!r})['_native_pause_and_continue_reference']()"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_scoped_settings_retain_inherited_configuration() -> None:
    class ForwardingCoder(Coder):
        def __init__(self, *, label="default", **kwargs):
            self.label = label
            super().__init__(**kwargs)

    class NestedForwardingCoder(ForwardingCoder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    client = object()
    node = NestedForwardingCoder(client=client, model="configured-model", provider="deepseek", label="configured")
    settings = {setting.name: getattr(node, setting.name) for setting in fields(node) if setting.init}
    settings["label"] = node.label
    with node_settings({node.STEP_ID: settings}):
        rebuilt = type(node)()
    assert rebuilt.client is client
    assert rebuilt.model == "configured-model"
    assert rebuilt.provider == "deepseek"
    assert rebuilt.label == "configured"


@pytest.mark.parametrize("node_class", [Coder, NodePlanElement, PromptNodeFileCoderBase, PromptNodeTestFileCoder])
@pytest.mark.parametrize("custom_prompt", [False, True])
def test_scoped_coders_preserve_prompt_and_single_marking_block(node_class, custom_prompt) -> None:
    node = node_class(client=object(), session_marking_prompt="CUSTOM-MARKER")
    if custom_prompt:
        node.system_prompt = "Configured prompt\n\nCUSTOM-MARKER"
    settings = {setting.name: getattr(node, setting.name) for setting in fields(node) if setting.init}
    with node_settings({node.STEP_ID: settings}):
        rebuilt = node_class()
    assert rebuilt.system_prompt == node.system_prompt
    assert rebuilt.system_prompt.count("CUSTOM-MARKER") == 1


@pytest.mark.parametrize("node_class", [NodePlanElement, PromptNodeFileCoderBase, PromptNodeTestFileCoder])
def test_explicit_constructor_prompt_is_not_rebuilt_from_template(node_class) -> None:
    node = node_class(client=object(), system_prompt="Configured prompt", session_marking_prompt="CUSTOM-MARKER")
    assert node.system_prompt == "Configured prompt\n\nCUSTOM-MARKER"


@pytest.mark.parametrize("node_class", [NodePlanElement, PromptNodeFileCoderBase, PromptNodeTestFileCoder])
def test_scoped_settings_apply_before_prompt_file_access(node_class, tmp_path) -> None:
    prompt_path = tmp_path / "custom-prompt.md"
    prompt_path.write_text("Custom template", encoding="utf-8")
    client = object()
    with node_settings({node_class.STEP_ID: {"client": client, "prompt_path": str(prompt_path)}}):
        node = node_class()
    assert node.client is client
    assert node.prompt_path == str(prompt_path)
    assert node.system_prompt.startswith("Custom template")
    assert node_class.prompt_path != str(prompt_path)


def test_node_settings_restore_scope_after_constructor_failure(tmp_path) -> None:
    client = object()
    with node_settings({NodePlanElement.STEP_ID: {"client": client, "system_prompt": "Outer prompt"}}):
        with pytest.raises(FileNotFoundError):
            with node_settings({NodePlanElement.STEP_ID: {"prompt_path": str(tmp_path / "missing.md")}}):
                NodePlanElement()
        assert NodePlanElement().system_prompt == "Outer prompt"
    assert NodePlanElement(client=client).system_prompt != "Outer prompt"


def test_concurrent_construction_settings_do_not_mutate_classes() -> None:
    barrier = Barrier(2)
    clients = [object(), object()]

    def create(index):
        with node_settings({Coder.STEP_ID: {"client": clients[index], "model": f"model-{index}"}}):
            barrier.wait(timeout=10)
            return Coder()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(create, range(2)))
    assert first.client is clients[0]
    assert second.client is clients[1]
    assert (first.model, second.model) == ("model-0", "model-1")
    assert Coder.model == "gpt-4.1-mini"
    assert Coder.client is None