# SPDX-License-Identifier: Apache-2.0
"""Offline smoke example for ``DynamicWorkflowOrchestrator``.

This example uses small deterministic stand-ins for the AgentBuilder-published
node-generation and runtime-modifier workflows, so it needs no LLM key and no
network access. It exercises the expanded single-engine path:

* one engine containing both business nodes,
* per-node request namespaces,
* repair-loop and finalizer steps,
* an in-place engine update when a business node is added.

Run from the repository root:

    PYTHONPATH=packages/core python examples/dynamic_workflow_orchestrator.py

The example writes a persistent bundle under
``examples/output/dynamic_workflow_orchestrator`` by default. It prints every
generated node source and test path, for example ``Collect.py``, ``Publish.py``
and ``Archive.py``. Pass ``--output DIR`` to choose another root.

For the real LLM-backed flow, use ``examples/sdk_quickstart.py`` or
``examples/sdk_guided_repair.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ag_ui_workflow import StepRunOutput, WorkflowStepNode

import flowx_core.dynamic_workflow_orchestrator as orchestrator_module
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.dynamic_workflow_orchestrator import (
    DynamicWorkflowOrchestrator,
    DynamicWorkflowRequest,
)
from flowx_core.workflows.dynamic_node_graph import ExpandedWorkflowEngine


class FakePlanner(WorkflowStepNode):
    """The planner is skipped when ``skip_node_planner=True``; the class is still required."""

    STEP_ID = "node_plan"
    DEPENDENCIES = []
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results, session_state
        return StepRunOutput()


class FakeCoder(WorkflowStepNode):
    """Write a minimal valid node file instead of calling an LLM."""

    STEP_ID = "node_generate"
    DEPENDENCIES = ["node_plan"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results
        writer = session_state["node_writer"]
        raw_node = session_state["node_plan_request"]["node"]
        name = writer["node_name"]
        depends = list(raw_node.get("depends", []))
        Path(writer["output_path"]).write_text(
            "from ag_ui_workflow import StepRunOutput, WorkflowStepNode\n\n"
            f"class {name}(WorkflowStepNode):\n"
            f"    STEP_ID = {name!r}\n"
            f"    DEPENDENCIES = {depends!r}\n\n"
            "    def process_input(self, user_input, dependency_results, session_state):\n"
            "        _ = dependency_results\n"
            "        return StepRunOutput()\n",
            encoding="utf-8",
        )
        return StepRunOutput(derived={"generated_path": writer["output_path"]})


class FakeTestWriter(WorkflowStepNode):
    """Write a passing pytest module instead of calling an LLM."""

    STEP_ID = "generate_node_test"
    DEPENDENCIES = []
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results
        request = session_state["node_runtime_modifier"]
        test_path = Path(request.test_file_name)
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            "def test_smoke():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        request.test_file_path = str(test_path)
        return StepRunOutput(derived={"test_file_path": str(test_path)})


class FakeAmendWriter(WorkflowStepNode):
    """Accept the test result; the passing test means no source amendment is needed."""

    STEP_ID = "amend_node"
    DEPENDENCIES = ["run_node_test"]
    INPUT_REQUIRED = True

    def wait_function(self):
        return True

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results
        request = session_state["node_runtime_modifier"]
        request.amended_node_file_path = request.node_file_name
        return StepRunOutput(derived={"amended_node_file_path": request.node_file_name})


class _SuccessfulProcess:
    returncode = 0
    stdout = ""
    stderr = ""


def _run_subprocess(*args, **kwargs):
    del args, kwargs
    return _SuccessfulProcess()


def _fake_workflows():
    """Build graph-backed stand-ins for the workflows AgentBuilder publishes."""
    generation = SimpleNamespace(
        node_planner=FakePlanner(),
        node_coder=FakeCoder(),
        node_auditor=NodeAuditor(),
        graph=object(),
        json_config=lambda: {"pool_size": 1},
    )
    modifier = SimpleNamespace(
        node_test_writer=FakeTestWriter(),
        node_writer=FakeAmendWriter(),
        run_subprocess=_run_subprocess,
        timeout_expired=subprocess.TimeoutExpired,
        graph=object(),
        json_config=lambda: {"pool_size": 1},
    )
    return generation, modifier


def _write_plan(root: Path, nodes: list[dict]) -> tuple[Path, Path]:
    graph_path = root / "workflow.json"
    requirement_path = root / "requirement_analysis.md"
    graph_path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    requirement_path.write_text(
        "Offline smoke requirement: build and publish a message.\n",
        encoding="utf-8",
    )
    return graph_path, requirement_path


def _run_directory(base: Path) -> Path:
    """Return a fresh visible run directory without deleting earlier output."""
    if not base.exists() or not any(base.iterdir()):
        base.mkdir(parents=True, exist_ok=True)
        return base
    run_dir = base / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _print_generated_nodes(version) -> None:
    for name, node in version.node_versions.items():
        print(f"node {name} source: {node.source_path}")
        test_path = node.artifacts.get("test")
        if test_path is not None:
            print(f"node {name} test:   {test_path}")
        assert node.source_path.is_file(), f"missing generated source for {name}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "output" / "dynamic_workflow_orchestrator",
        help="Output root; a timestamped run directory is used when it is already non-empty.",
    )
    args = parser.parse_args()
    output_dir = args.output.expanduser().resolve()
    run_dir = _run_directory(output_dir)
    print(f"example output: {run_dir}")

    generation, modifier = _fake_workflows()
    previous_generation = orchestrator_module.NODE_GENERATION_WORKFLOW
    previous_modifier = orchestrator_module.NODE_RUNTIME_MODIFIER_PIPELINE
    orchestrator_module.NODE_GENERATION_WORKFLOW = generation
    orchestrator_module.NODE_RUNTIME_MODIFIER_PIPELINE = modifier
    try:
        graph_path, requirement_path = _write_plan(run_dir, [
            {"name": "Collect", "type": "", "desc": "Collect a message."},
            {"name": "Publish", "type": "", "desc": "Publish it.", "depends": ["Collect"]},
        ])
        orchestrator = DynamicWorkflowOrchestrator(
            storage_dir=run_dir / "storage",
            workflow_id="offline-example",
        )
        try:
            first = orchestrator.run(DynamicWorkflowRequest(
                workflow_json_path=graph_path,
                requirement_md_path=requirement_path,
                skip_node_planner=True,
                generate_markdown=False,
                max_repair_retries=0,
            ))
            engine = orchestrator.generation_engine
            print(f"first workflow version: {first.version}")
            print(f"first bundle: {first.workflow_path}")
            print(f"changed nodes: {first.changed_nodes}")
            print(f"node ids: {first.generated_node_id_map}")
            print(f"engine: {type(engine).__name__}")
            print(f"finalizers: {engine.finalize_ids}")
            _print_generated_nodes(first)
            assert first.version == 1
            assert set(first.changed_nodes) == {"Collect", "Publish"}
            assert isinstance(engine, ExpandedWorkflowEngine)
            assert set(engine.finalize_ids) == {"Collect", "Publish"}

            # Adding a node changes the enabled business topology. The orchestrator
            # calls ExpandedWorkflowEngine.update(...), which rebuilds only the
            # changed/added chains and reloads the existing pool.
            graph_path, requirement_path = _write_plan(run_dir, [
                {"name": "Collect", "type": "", "desc": "Collect a message."},
                {"name": "Publish", "type": "", "desc": "Publish it.", "depends": ["Collect"]},
                {"name": "Archive", "type": "", "desc": "Archive it.", "depends": ["Publish"]},
            ])
            second = orchestrator.run(DynamicWorkflowRequest(
                workflow_json_path=graph_path,
                requirement_md_path=requirement_path,
                skip_node_planner=True,
                generate_markdown=False,
                max_repair_retries=0,
            ))
            print(f"second workflow version: {second.version}")
            print(f"second bundle: {second.workflow_path}")
            print(f"changed nodes: {second.changed_nodes}")
            print(f"finalizers after update: {engine.finalize_ids}")
            _print_generated_nodes(second)
            assert second.version == 2
            assert set(second.changed_nodes) == {"Archive"}
            assert set(engine.finalize_ids) == {"Collect", "Publish", "Archive"}
        finally:
            orchestrator.close()
    finally:
        orchestrator_module.NODE_GENERATION_WORKFLOW = previous_generation
        orchestrator_module.NODE_RUNTIME_MODIFIER_PIPELINE = previous_modifier


if __name__ == "__main__":
    main()
