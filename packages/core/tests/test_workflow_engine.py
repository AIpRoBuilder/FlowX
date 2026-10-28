"""Modified: direct native construction, minimal JSON and request lifecycle.

Modified: native state identity, request isolation and reusable generation/repair APIs.
Modified 2026-10-15: direct generation requests and fresh mutable repair requests.
Modified 2026-10-15: metadata-selected batches and repair outputs without stage bookkeeping.
Modified 2026-10-15: complete per-node generation requests without batch fields.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, FrozenInstanceError, dataclass, field, fields, replace
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from ag_ui_workflow import RunResult, StepRunOutput, WorkflowFileNode, WorkflowPipelinePool, WorkflowStepNode
from pydaograph import GElement

import flowx_core.tools.workflow_engine as workflow_engine_module
import flowx_core.workflows.node_runtime_modifier as modifier_module
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.architect.graph import NodeMeta
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.llm_client.coder import Coder
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase, RequestNodeFileCoder
from flowx_core.workflows.node_generation import NodeGenerationRequest, NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    ExecuteNodeTest,
    GenerateNodeTestFile,
    NodeRuntimeModifierPipeline,
    NodeRuntimeModifierRequest,
)
from flowx_core.tools.workflow_engine import (
    close_workflow_engine,
    init_workflow_engine,
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


def test_build_init_and_run_steps() -> None:
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
        result = engine.run_steps(request_id="first-request", state={"received": received})
        assert engine.steps_meta[1]["dependencies"] == ["first"]
        assert isinstance(engine.pool, WorkflowPipelinePool)
        assert isinstance(result, RunResult)
        assert result.status == "completed"
        assert result.new_steps == ["first", "second"]
        assert result.outputs["first"].derived == {"value": 2}
        assert received[0]["first"].derived == {"value": 2}
        assert result.outputs["second"].derived == {"value": 5}
        assert engine.awaiting_input(result.request_id) is None
        assert engine.run_steps(request_id="second-request").outputs == result.outputs
    finally:
        close_workflow_engine(engine)
    assert engine.pool.closed


def test_run_steps_preserves_native_input_normalization() -> None:
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
            result = engine.run_steps(inputs={"echo": value}, state={"marker": 1})
            assert result.outputs["echo"].derived == {"input": expected, "marker": 1}
    finally:
        close_workflow_engine(engine)


def test_workflow_does_not_reserve_a_hidden_control_step_name() -> None:
    class ControlStep(_FirstStep):
        STEP_ID = "_flowx_run_request"

    engine = init_workflow_engine({"_flowx_run_request": ControlStep}, {"nodes": [{"name": "_flowx_run_request"}]})
    try:
        result = engine.run_steps(state={"marker": 1})
        assert result.outputs["_flowx_run_request"].derived == {"value": 2}
    finally:
        close_workflow_engine(engine)


def test_run_steps_rejects_failure_and_skips_dependents() -> None:
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
            engine.run_steps()
        assert events == ["broken"]
        assert engine.run_steps(state={"recovered": True}).status == "completed"
        assert events == ["broken", "downstream"]
        assert workflow_engine_module._RUN_REQUEST.get() is None
    finally:
        close_workflow_engine(engine)


def test_run_steps_seeds_fresh_state_and_reuses_native_pool() -> None:
    states = []
    native_states = []
    native_nodes = []
    dependencies_seen = []

    class StatefulStep(WorkflowStepNode):
        STEP_ID = "stateful"

        def process_input(self, user_input, dependency_results, session_state):
            assert session_state is self.get_workflow_param().state
            native_states.append(session_state)
            native_nodes.append(self)
            states.append(dict(session_state))
            assert "previous" not in session_state
            session_state["previous"] = user_input
            return StepRunOutput(derived={"value": user_input})

    class FollowingStep(WorkflowStepNode):
        STEP_ID = "following"
        DEPENDENCIES = ["stateful"]

        def process_input(self, user_input, dependency_results, session_state):
            assert session_state is self.get_workflow_param().state
            if session_state.get("marker"):
                assert session_state is native_states[-1]
            dependencies_seen.append(dependency_results)
            return StepRunOutput(derived={"previous": session_state.get("previous")})

    # Native JSON construction resolves topology, even in reverse order.
    engine = init_workflow_engine(
        {"following": FollowingStep, "stateful": StatefulStep},
        {"nodes": [{"name": "following", "depends": ["stateful"]}, {"name": "stateful"}]},
    )
    pool = engine.pool
    first_seed, second_seed = {"marker": 1}, {"marker": 2}
    try:
        first = engine.run_steps(inputs={"stateful": "one"}, state=first_seed)
        second = engine.run_steps(inputs={"stateful": "two"}, state=second_seed)
        skipped = engine.run_steps(skipped_steps=["stateful"])
        assert states == [{"marker": 1}, {"marker": 2}]
        assert first.outputs["following"].derived == {"previous": "one"}
        assert second.outputs["following"].derived == {"previous": "two"}
        assert skipped.outputs["following"].derived == {"previous": None}
        assert skipped.outputs["stateful"].card == {"status": "skipped"}
        assert dependencies_seen[-1]["stateful"].derived == {}
        assert first.outputs["stateful"].derived == {"value": "one"}
        assert first_seed == {"marker": 1}
        assert second_seed == {"marker": 2}
        assert native_states[0] is not first_seed
        assert native_states[0] is not native_states[1]  # native clear() replaces the dictionary
        assert native_states == [{"marker": 1, "previous": "one"}, {"marker": 2, "previous": "two"}]
        assert native_nodes[0] is native_nodes[1]
        assert native_nodes[0].get_workflow_param().state == {}
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


def test_run_steps_rejects_non_step_output() -> None:
    class InvalidStep(WorkflowStepNode):
        STEP_ID = "invalid"

        def process_input(self, user_input, dependency_results, session_state):
            return {"value": 1}

    engine = init_workflow_engine({"invalid": InvalidStep}, {"nodes": [{"name": "invalid"}]})
    try:
        with pytest.raises(RuntimeError, match="process_input must return StepRunOutput"):
            engine.run_steps()
        with pytest.raises(ValueError, match="unknown workflow step"):
            engine.run_steps(inputs={"missing": "value"})
    finally:
        close_workflow_engine(engine)


def test_initializer_forwards_class_map_and_normalizes_topology(monkeypatch) -> None:
    captured = {}

    class Pool:
        def __init__(self, *, pipeline_json, node_classes, size):
            captured.update(pipeline_json=pipeline_json, node_classes=node_classes, size=size)

    real_build_pool = workflow_engine_module.build_workflow_pool

    def capture_build_pool(node_classes_arg, workflow_config_arg):
        captured["workflow_config"] = workflow_config_arg
        pool, steps_meta = real_build_pool(node_classes_arg, workflow_config_arg)
        captured["steps_meta"] = steps_meta
        return pool, steps_meta

    monkeypatch.setattr(workflow_engine_module, "_WorkflowPipelinePool", Pool)
    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", capture_build_pool)
    node_classes = {"first": _FirstStep, "second": _SecondStep}
    nodes = [
        {"name": "first", "type": "_FirstStep", "meta_type": "GNode", "loop": 1,
         "description": "保留配置", "config": {"labels": ["original"]}},
        {"name": "second", "depends": ["first"]},
    ]
    workflow_config = {"pool_size": 4, "nodes": nodes}
    snapshot = json.dumps(workflow_config)
    engine = init_workflow_engine(node_classes, workflow_config)
    assert captured["node_classes"] is node_classes
    assert captured["workflow_config"] is workflow_config
    assert json.loads(captured["pipeline_json"]) == {"nodes": [
        {"name": "first", "depends": []},
        {"name": "second", "depends": ["first"]},
    ]}
    assert [step["id"] for step in captured["steps_meta"]] == ["first", "second"]
    assert json.dumps(workflow_config) == snapshot
    assert captured["size"] == 4
    assert engine.graph_config_id == "flowx"


def test_initializer_retains_the_graph_spec_for_graph_backed_callers() -> None:
    engine = init_workflow_engine(
        {"first": _FirstStep, "second": _SecondStep},
        {"nodes": [{"name": "first"}, {"name": "second", "depends": ["first"]}]},
    )
    try:
        assert type(engine.graph).__name__ == "GraphSpec"
        assert json.loads(engine.graph.pipeline_json) == {"nodes": [
            {"name": "first", "depends": []},
            {"name": "second", "depends": ["first"]},
        ]}
        assert engine.graph.node_classes["second"] is _SecondStep
    finally:
        close_workflow_engine(engine)


def test_native_pool_accepts_supplied_node_fields() -> None:
    nodes = [{"name": "first", "type": "_FirstStep", "meta_type": "GNode", "loop": 1}]
    engine = init_workflow_engine({"first": _FirstStep}, {"nodes": nodes})
    try:
        assert engine.run_steps().outputs["first"].derived == {"value": 2}
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
        first = engine.run_steps(state={"marker": 1})
        second = engine.run_steps(state={"marker": 2})
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
            assert session_state is self.get_workflow_param().state
            return StepRunOutput(derived={"size": len(file_bytes)})

    engine = init_workflow_engine({"file": FileNode}, {"nodes": [{"name": "file"}]})
    try:
        assert engine.run_steps(inputs={"file": b"Ada"}).outputs["file"].derived == {"size": 3}
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

    real_build_pool = workflow_engine_module.build_workflow_pool

    def capture_build_pool(node_classes, workflow_config):
        pool, steps_meta = real_build_pool(node_classes, workflow_config)
        captured["steps_meta"] = steps_meta
        return pool, steps_meta

    monkeypatch.setattr(workflow_engine_module, "_WorkflowPipelinePool", Pool)
    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", capture_build_pool)
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


def _native_concurrent_request_state_reference() -> None:
    barrier = Barrier(2)
    native_states = []
    seen_steps = []

    class MutateState(WorkflowStepNode):
        STEP_ID = "mutate"
        INPUT_REQUIRED = False

        def process_input(self, user_input, dependency_results, session_state):
            assert session_state is self.get_workflow_param().state
            assert session_state["marker"] == user_input
            assert "previous" not in session_state
            native_states.append(session_state)
            session_state["previous"] = user_input
            barrier.wait(timeout=10)
            assert session_state["previous"] == user_input
            session_state["sink"].append(user_input)
            return StepRunOutput(derived={"marker": user_input})

    class ReadState(WorkflowStepNode):
        STEP_ID = "read"
        DEPENDENCIES = ["mutate"]
        INPUT_REQUIRED = False

        def process_input(self, user_input, dependency_results, session_state):
            assert session_state is self.get_workflow_param().state
            seen_steps.append(session_state["marker"])
            return StepRunOutput(derived={
                "marker": session_state["marker"],
                "previous": session_state.get("previous"),
                "skipped": dependency_results["mutate"].card.get("status") == "skipped",
            })

    engine = init_workflow_engine(
        {"mutate": MutateState, "read": ReadState},
        {"pool_size": 2, "nodes": [{"name": "mutate"}, {"name": "read", "depends": ["mutate"]}]},
    )
    seeds = [{"marker": marker, "sink": []} for marker in ("one", "two")]

    def run(index):
        result = engine.run_steps(inputs={"mutate": seeds[index]["marker"]}, state=seeds[index])
        assert workflow_engine_module._RUN_REQUEST.get() is None
        return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(run, range(2)))
        assert first.outputs["read"].derived == {"marker": "one", "previous": "one", "skipped": False}
        assert second.outputs["read"].derived == {"marker": "two", "previous": "two", "skipped": False}
        assert native_states[0] is not native_states[1]
        assert sorted(state["previous"] for state in native_states) == ["one", "two"]
        with engine.pool.lease() as (_, param):
            assert param.state == {}
        assert seeds == [{"marker": "one", "sink": ["one"]}, {"marker": "two", "sink": ["two"]}]
        skipped = engine.run_steps(state={"marker": "three"}, skipped_steps=["mutate"])
        assert skipped.outputs["read"].derived == {"marker": "three", "previous": None, "skipped": True}
        assert sorted(seen_steps) == ["one", "three", "two"]
    finally:
        close_workflow_engine(engine)


def test_concurrent_requests_isolate_native_state_and_skips() -> None:
    script = f"import runpy; runpy.run_path({__file__!r})['_native_concurrent_request_state_reference']()"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generation_request_requires_complete_keyword_only_inputs_and_is_frozen(tmp_path) -> None:
    meta = NodeMeta(name="direct", type="GNode", desc="direct", meta_node_kind="WorkflowStepNode")
    required = {
        "graph_plan_path": str(tmp_path / "workflow.json"),
        "requirement_md_path": str(tmp_path / "requirement.md"),
        "requirement_text": "Requirement for direct",
        "node_meta": meta,
        "raw_node": meta.to_dict(),
        "target_path": tmp_path / "direct.py",
        "markdown_path": tmp_path / "node_docs" / "direct.md",
    }
    request = NodeGenerationRequest(**required)
    request_fields = {setting.name: setting for setting in fields(request)}
    optional = {
        "language", "temperature", "generate_markdown", "skip_node_planner", "markdown_amendment",
        "node_amendment", "node_format", "dependency_context", "state",
    }
    assert request_fields.keys() == required.keys() | optional
    assert all(setting.init and setting.kw_only for setting in request_fields.values())
    assert {
        name for name, setting in request_fields.items()
        if setting.default is MISSING and setting.default_factory is MISSING
    } == required.keys()
    for missing in required:
        with pytest.raises(TypeError, match=missing):
            NodeGenerationRequest(**{name: value for name, value in required.items() if name != missing})
    assert request.node_meta is meta
    assert request.raw_node is required["raw_node"]
    assert request.language == "python"
    assert request.temperature == 0.35
    assert request.generate_markdown is True
    assert request.skip_node_planner is False
    assert request.markdown_amendment == request.node_amendment == request.dependency_context == ""
    assert request.node_format == request.state == {}
    fresh = NodeGenerationRequest(**required)
    assert fresh.node_format is not request.node_format
    assert fresh.state is not request.state
    removed_fields = {
        "node_name", "workflow_name", "node_locations", "skipped_node_names",
        "node_names", "selected_node_names", "node_docs_dir", "generate_markdowns",
        "markdown_amendments", "node_amendments", "workflow_registry", "raw_nodes",
        "artifacts", "node_formats", "context",
    }
    assert removed_fields.isdisjoint(request_fields)
    assert all(not hasattr(request, name) for name in removed_fields)
    with pytest.raises(FrozenInstanceError):
        request.temperature = 0.5


@dataclass
class _RecordingRequestCoder(RequestNodeFileCoder):
    calls: list = field(default_factory=list)
    instances: list = field(default_factory=list)

    __hash__ = object.__hash__

    def __post_init__(self):
        super().__post_init__()
        self.instances.append(self)

    def process_input(self, user_input, dependency_results, session_state):
        assert session_state is self.get_workflow_param().state
        assert session_state["node_writer"]["node_meta"] is session_state["expected_node_meta"]
        raw_node = session_state["expected_raw_node"]
        planned_node = session_state["node_plan_request"]["node"]
        assert planned_node == raw_node
        assert planned_node is not raw_node
        assert planned_node["ext_data"] is raw_node["ext_data"]
        return super().process_input(user_input, dependency_results, session_state)

    def code_to_file(self, user_prompt, file_path, **kwargs):
        session_state = self.get_workflow_param().state
        request = session_state["node_writer"]
        assert request["node_name"] == request["node_meta"].name
        assert session_state["marker"] == request["node_name"]
        self.calls.append({
            "node_name": request["node_name"], "prompt": user_prompt, "path": file_path,
            "state_id": id(session_state), "temperature": kwargs["temperature"],
        })
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# offline generated node\n", encoding="utf-8")
        return path


class _PassingNodeAuditor(NodeAuditor):
    def process_input(self, user_input, dependency_results, session_state):
        assert session_state is self.get_workflow_param().state
        assert session_state["auditor"]["node_meta"] is session_state["node_writer"]["node_meta"]
        assert session_state["auditor"]["node_meta"] is session_state["expected_node_meta"]
        assert Path(dependency_results["node_generate"].derived["generated_path"]).is_file()
        session_state["audit_calls"].append({
            "node_name": session_state["auditor"]["node_meta"].name, "state_id": id(session_state),
        })
        return StepRunOutput(derived={"ok": True, "violations": []})


def test_generation_request_reuses_one_eager_workflow_for_all_node_kinds(tmp_path) -> None:
    calls, instances, audit_calls = [], [], []
    coder = _RecordingRequestCoder(client=object(), calls=calls, instances=instances)
    workflow = NodeGenerationWorkflow(
        node_planner=NodePlanElement(client=object()), node_coder=coder,
        node_auditor=_PassingNodeAuditor(),
    )
    engine = workflow
    assert engine is not None
    pool = engine.pool
    assert workflow._node_classes["node_generate"] is _RecordingRequestCoder
    assert len(instances) == 2  # injected settings source plus exactly one native slot
    pooled_coder = instances[-1]
    assert pooled_coder.client is coder.client
    assert pooled_coder.calls is calls
    before = dict(pooled_coder.__dict__)
    requests = []
    try:
        for index, kind in enumerate(("WorkflowStepNode", "WorkflowFileNode")):
            node_name = f"node_{index}"
            root = tmp_path / node_name
            root.mkdir()
            requirement = root / "requirement.md"
            requirement.write_text(f"Requirement for {node_name}", encoding="utf-8")
            ext_data = (
                {"type": "user_file_input", "remote_desc": "persist to request bucket"}
                if kind == "WorkflowFileNode" else {"type": "none"}
            )
            meta = NodeMeta(name=node_name, type="GNode", desc=node_name, meta_node_kind=kind, ext_data=ext_data)
            raw_node = meta.to_dict()
            node_format = {"inputs": {f"input_{index}": "str"}, "outputs": {f"output_{index}": "str"}}
            dependency_context = f"Request dependency context for {node_name}"
            state = {
                "marker": node_name, "expected_node_meta": meta, "expected_raw_node": raw_node,
                "audit_calls": audit_calls,
            }
            state_snapshot = dict(state)
            request = NodeGenerationRequest(
                graph_plan_path=str(root / "workflow.json"), requirement_md_path=str(requirement),
                requirement_text=requirement.read_text(), node_meta=meta, raw_node=raw_node,
                target_path=root / f"{node_name}.py", markdown_path=root / "node_docs" / f"{node_name}.md",
                generate_markdown=False, node_format=node_format, dependency_context=dependency_context,
                state=state,
            )
            assert request.node_meta is meta
            assert request.raw_node is raw_node
            assert request.node_format is node_format
            assert request.state is state
            assert all(request.state is not previous.state for previous in requests)
            requests.append(request)
            assert not hasattr(request, "context")
            with pytest.raises(FrozenInstanceError):
                request.node_meta = meta
            assert workflow.run(request) == str(request.target_path)
            assert calls[-1]["node_name"] == meta.name
            assert calls[-1]["path"] == str(request.target_path)
            assert calls[-1]["temperature"] == request.temperature == 0.35
            assert calls[-1]["state_id"] != id(state)
            assert audit_calls[-1] == {"node_name": node_name, "state_id": calls[-1]["state_id"]}
            assert len(audit_calls) == len(calls)
            assert state == state_snapshot
            assert raw_node == meta.to_dict()
            assert request.requirement_text in calls[-1]["prompt"]
            assert json.dumps(node_format, ensure_ascii=False, indent=2) in calls[-1]["prompt"]
            assert dependency_context in calls[-1]["prompt"]
            assert not request.markdown_path.exists()
            assert f"Generate a {kind} subclass" in calls[-1]["prompt"]
            assert workflow is engine
            assert workflow.init_node_engine() is engine
            assert engine.pool is pool
            with pool.lease() as (_, param):
                assert param.state == {}
            assert pooled_coder.get_workflow_param().state == {}
            assert len(instances) == 2
            assert not hasattr(workflow, "_last_result")
            if kind == "WorkflowFileNode":
                assert "remote_desc: persist to request bucket" in calls[-1]["prompt"]
                original = request
                request = replace(request, node_amendment="fix request persistence")
                assert request is not original
                assert original.node_amendment == ""
                assert request.node_amendment == "fix request persistence"
                assert request.node_meta is original.node_meta is meta
                assert request.raw_node is original.raw_node is raw_node
                assert request.node_format is original.node_format is node_format
                assert request.state is original.state is state
                requests.append(request)
                assert workflow.run(request) == str(request.target_path)
                assert request.node_amendment in calls[-1]["prompt"]
                assert audit_calls[-1] == {"node_name": node_name, "state_id": calls[-1]["state_id"]}
                assert len(audit_calls) == len(calls)
                assert state == state_snapshot
                assert workflow is engine
                assert engine.pool is pool
                assert "Preserve the WorkflowFileNode" in calls[-1]["prompt"]
                assert "remote_desc: persist to request bucket" in calls[-1]["prompt"]
        assert {key: value for key, value in pooled_coder.__dict__.items()} == before
        assert len(calls) == len(audit_calls) == 3
        assert not any(
            value is request
            for owner in (workflow, workflow.node_planner, workflow.node_coder, workflow.node_auditor,
                          pooled_coder, engine, pool)
            for value in vars(owner).values()
            for request in requests
        )
        with pool.lease() as (_, param):
            assert param.state == {}
        assert workflow_engine_module._RUN_REQUEST.get() is None
    finally:
        workflow.close()
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed
    with pytest.raises(RuntimeError, match="closed"):
        workflow.run(request)
    with pytest.raises(RuntimeError, match="closed"):
        workflow.init_node_engine()


@pytest.mark.parametrize("kind", ["WorkflowStepNode", "WorkflowFileNode"])
def test_request_coder_direct_contract_helpers_accept_node_metadata(kind) -> None:
    coder = RequestNodeFileCoder(client=object())
    meta = NodeMeta(name="direct", type="GNode", desc="direct", meta_node_kind=kind)
    assert f"Generate a {kind} subclass" in coder.get_node_contract_text(meta)
    assert f"Preserve the {kind}" in coder.get_feedback_contract_text(meta)
    assert isinstance(hash(coder), int)
    assert coder.STEP_ID == "node_generate"


@dataclass
class _RecordingTestGenerator(GenerateNodeTestFile):
    calls: list = field(default_factory=list)
    instances: list = field(default_factory=list)

    __hash__ = object.__hash__

    def __post_init__(self):
        super().__post_init__()
        self.instances.append(self)

    def code_to_file(self, user_prompt, file_path, **kwargs):
        session_state = self.get_workflow_param().state
        request = session_state["node_runtime_modifier"]
        assert request.test_file_path == request.log_file_path == request.test_log == request.amended_node_file_path == ""
        assert request.test_result is None
        assert f"working_directory: {request.working_directory}" in user_prompt
        assert self.root_dir_path not in user_prompt
        self.calls.append({
            "prompt": user_prompt, "path": file_path, "temperature": kwargs["temperature"],
            "request": request, "marker": session_state["marker"],
        })
        path = Path(file_path)
        path.write_text("def test_offline():\n    assert True\n", encoding="utf-8")
        return path


@dataclass
class _RecordingAmender(AmendNodeFromTestLog):
    calls: list = field(default_factory=list)

    __hash__ = object.__hash__

    def code_to_file(self, user_prompt, file_path, **kwargs):
        request = self.get_workflow_param().state["node_runtime_modifier"]
        self.calls.append({
            "prompt": user_prompt, "path": file_path, "temperature": kwargs["temperature"], "request": request,
        })
        return Path(file_path)


def test_modifier_requests_reuse_pool_without_retaining_request_or_execution_data(tmp_path, monkeypatch) -> None:
    generation_calls, amendment_calls, execution_calls, instances = [], [], [], []
    executors = []
    original_apply_settings = modifier_module.apply_node_settings
    native_results = []
    original_run_steps = NodeRuntimeModifierPipeline.run_steps

    def record_native_result(self, *args, **kwargs):
        result = original_run_steps(self, *args, **kwargs)
        native_results.append(result)
        return result

    def record_executor(node):
        original_apply_settings(node)
        if isinstance(node, ExecuteNodeTest):
            executors.append(node)

    monkeypatch.setattr(modifier_module, "apply_node_settings", record_executor)
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "run_steps", record_native_result)

    def run_subprocess(command, **kwargs):
        execution_calls.append({"command": list(command), **kwargs})
        return subprocess.CompletedProcess(command, 1 if "alpha" in command[-2] else 0, "offline output", "")

    pipeline = NodeRuntimeModifierPipeline(
        node_test_writer=_RecordingTestGenerator(
            client=object(), root_dir_path="/stale/construction/root", calls=generation_calls, instances=instances,
        ),
        node_writer=_RecordingAmender(client=object(), calls=amendment_calls), run_subprocess=run_subprocess,
    )
    engine = pipeline
    assert engine is not None
    assert len(instances) == 2
    results = []
    try:
        for index, node_name in enumerate(("alpha", "beta")):
            workdir = tmp_path / node_name
            workdir.mkdir()
            (workdir / f"{node_name}.py").write_text(
                "from ag_ui_workflow import WorkflowStepNode, StepRunOutput\n"
                f"class {node_name.title()}(WorkflowStepNode):\n"
                f"    STEP_ID = {node_name!r}\n"
                "    INPUT_REQUIRED = False\n"
                "    def process_input(self, user_input, dependency_results, session_state):\n"
                "        return StepRunOutput(derived={'ok': True})\n",
                encoding="utf-8",
            )
            graph = {"nodes": [NodeMeta(name=node_name, type="GNode", desc=node_name,
                                       meta_node_kind="WorkflowStepNode").to_dict()]}
            state = {"marker": node_name}
            request = NodeRuntimeModifierRequest(
                node_file_name=f"{node_name}.py", workflow_graph=graph, working_directory=f"{workdir}/.",
                node_name=node_name, timeout=40 + index,
                python_command=f"request-python-{index}", test_temperature=0.1 + index / 10,
                amendment_temperature=0.4, test_file_name=f"custom_tests/test_{node_name}.py",
                state=state,
            )
            removed_fields = {"workflow_name", "skipped_stages", "completed_stages"}
            assert removed_fields.isdisjoint(setting.name for setting in fields(request))
            assert all(not hasattr(request, name) for name in removed_fields)
            stale_outputs = {
                "test_file_path": "stale-test.py", "log_file_path": "stale.log", "test_log": "stale log",
                "test_result": {"node_name": "stale", "ok": True}, "amended_node_file_path": "stale.py",
            }
            assert {setting.name for setting in fields(request) if not setting.init} == stale_outputs.keys()
            for name, value in stale_outputs.items():
                setattr(request, name, value)
            inputs = {setting.name: getattr(request, setting.name) for setting in fields(request) if setting.init}
            result = pipeline.run(request)
            results.append(result)
            assert type(result) is NodeRuntimeModifierRequest
            assert result is not request
            assert not hasattr(result, "context")
            assert generation_calls[-1]["request"] is result
            assert generation_calls[-1]["marker"] == node_name
            assert result.state is request.state is state
            assert state == {"marker": node_name}
            assert result.working_directory == str(workdir.resolve())
            assert request.workflow_graph is graph
            assert {setting.name: getattr(request, setting.name) for setting in fields(request) if setting.init} == inputs
            assert {name: getattr(request, name) for name in stale_outputs} == stale_outputs
            assert result.test_result is not request.test_result
            assert result.node_name == request.node_name == node_name
            assert all(not hasattr(result, name) for name in removed_fields)
            assert pipeline is engine
            native_result = native_results[-1]
            assert isinstance(native_result, RunResult)
            assert native_result.status == "completed"
            assert set(native_result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
            assert all(isinstance(output, StepRunOutput) for output in native_result.outputs.values())
            assert native_result.outputs["generate_node_test"].derived == {"test_file_path": result.test_file_path}
            assert native_result.outputs["run_node_test"].derived == {
                **result.test_result, "log_file_path": result.log_file_path,
            }
            assert native_result.outputs["run_node_test"].derived is not result.test_result
            assert native_result.outputs["amend_node"].derived == {"amended_node_file_path": result.amended_node_file_path}
            assert json.loads(Path(result.workflow_graph).read_text()) == graph
            assert execution_calls[-1]["cwd"] == str(workdir)
            assert execution_calls[-1]["timeout"] == 40 + index
            assert execution_calls[-1]["command"][0] == f"request-python-{index}"
            assert result.test_result["node_name"] == node_name
            assert result.amended_node_file_path == str(workdir / f"{node_name}.py")
            assert result.test_result["ok"] is (index == 1)
            assert generation_calls[-1]["temperature"] == request.test_temperature
            assert f"Path({str(workdir / f'{node_name}.py')!r})" in generation_calls[-1]["prompt"]
        assert len(amendment_calls) == 1
        assert amendment_calls[0]["temperature"] == 0.4
        assert amendment_calls[0]["request"] is results[0]
        assert results[0].test_result["node_name"] == "alpha"
        assert results[0].test_result["command"][0] == "request-python-0"
        assert results[0].test_result is not results[1].test_result
        assert results[0].workflow_graph != results[1].workflow_graph
        skipped = pipeline.run(NodeRuntimeModifierRequest(
            node_file_name="gamma.py", workflow_graph={"nodes": []}, working_directory=results[1].working_directory,
            skip_generate_node_test=True, skip_run_node_test=True, skip_amend_node=True,
        ))
        assert all(not hasattr(skipped, name) for name in removed_fields)
        assert skipped.test_result is None
        assert skipped.test_log == skipped.log_file_path == ""
        assert skipped.test_file_path == str(Path(skipped.working_directory) / "tests" / "test_gamma.py")
        assert skipped.amended_node_file_path == str(Path(skipped.working_directory) / "gamma.py")
        assert native_results[-1].outputs["generate_node_test"].derived == {"test_file_path": ""}
        assert native_results[-1].outputs["run_node_test"].derived == {"log_file_path": ""}
        assert native_results[-1].outputs["amend_node"].derived == {
            "amended_node_file_path": skipped.amended_node_file_path,
        }
        assert skipped.workflow_graph != results[1].workflow_graph
        assert len(generation_calls) == len(execution_calls) == 2
        assert len(instances) == 2
        assert instances[-1].root_dir_path == "/stale/construction/root"
        assert not hasattr(pipeline, "_last_result")
        assert not any(value is request for value in vars(pipeline).values() for request in results)
        assert len(executors) == 1
        executor = executors[0]
        with engine.pool.lease() as (_, param):
            assert param.state == {}
            assert executor.run_subprocess is run_subprocess
            assert executor.node_name == executor.cwd == ""
            assert executor.command == []
            assert executor.result is None
    finally:
        pipeline.close()
        pipeline.close()
    assert pipeline.closed
    assert engine.pool.closed
    with pytest.raises(RuntimeError, match="closed"):
        pipeline.run(request)