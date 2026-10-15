# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-10-13: reconcile input metadata with generated node constants without importing them.
# Modified 2026-10-13: serialize cached pool ownership during creation and cleanup.
# Modified 2026-10-14: run main generation and auditing as a single-pass workflow without repair retries.
# Modified 2026-10-14: delegate main-entrypoint requests to MainEntrypointGenerationWorkflow.
# Modified 2026-10-14: inject service state, components, and callbacks independently of AgentBuilder.
# Modified 2026-10-14: delegate graph building to a skippable single-pass workflow.
# Modified 2026-10-14: own one initialized workflow per service; accept all run state in requests.
# Modified 2026-10-15: repair results use the unified request model.
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ag_ui_workflow import RunResult

from flowx_core.architect.graph import Graph
from flowx_core.tools.workflow_node_reference import resolve_workflow_node_reference
from flowx_core.workflows.graph_generation import GraphGenerationRequest, GraphGenerationWorkflow
from flowx_core.workflows.main_entrypoint_generation import (
    MainEntrypointGenerationRequest,
    MainEntrypointGenerationWorkflow,
)
from flowx_core.workflows.node_generation import (
    NodeGenerationRequest,
    NodeGenerationWorkflow,
)
from flowx_core.workflows.node_runtime_modifier import (
    NodeRuntimeModifierPipeline,
    NodeRuntimeModifierRequest,
)


@dataclass(kw_only=True)
class GraphBuildService:
    """Own one graph workflow, never a builder session or a run context."""

    analyzer: Any
    planner: Any
    graph_auditor: Any
    workflow: GraphGenerationWorkflow = field(init=False)

    def __post_init__(self) -> None:
        self.workflow = GraphGenerationWorkflow(
            analyzer=self.analyzer, planner=self.planner, graph_auditor=self.graph_auditor,
        )

    def run(self, request: GraphGenerationRequest) -> RunResult:
        return self.workflow.run(request)

    def close(self) -> None:
        self.workflow.close()


@dataclass(kw_only=True)
class NodeBuildService:
    """Use the same node-generation workflow for every requested node kind."""

    node_planner: Any
    node_coder: Any
    node_auditor: Any
    workflow: NodeGenerationWorkflow = field(init=False)

    def __post_init__(self) -> None:
        self.workflow = NodeGenerationWorkflow(
            node_planner=self.node_planner, node_coder=self.node_coder, node_auditor=self.node_auditor,
        )

    def run(self, request: NodeGenerationRequest) -> str:
        return self.workflow.run(request)

    def close(self) -> None:
        self.workflow.close()


@dataclass(kw_only=True)
class NodeArtifactService:
    """Reuse one repair workflow; names, paths and skip flags come from requests."""

    node_test_writer: Any
    node_writer: Any
    run_subprocess: Callable[..., Any]
    timeout_expired: type[BaseException]
    workflow: NodeRuntimeModifierPipeline = field(init=False)

    def __post_init__(self) -> None:
        self.workflow = NodeRuntimeModifierPipeline(
            node_test_writer=self.node_test_writer, node_writer=self.node_writer,
            run_subprocess=self.run_subprocess, timeout_expired=self.timeout_expired,
        )

    def run(self, request: NodeRuntimeModifierRequest) -> NodeRuntimeModifierRequest:
        return self.workflow.run(request)

    def close(self) -> None:
        self.workflow.close()


@dataclass(kw_only=True)
class MainEntrypointService:
    """Own one main-entrypoint workflow without retaining any build session."""

    main_writer: Any
    main_entry_auditor: Any
    workflow: MainEntrypointGenerationWorkflow = field(init=False)

    def __post_init__(self) -> None:
        self.workflow = MainEntrypointGenerationWorkflow(
            main_writer=self.main_writer, main_entry_auditor=self.main_entry_auditor,
        )

    def run(self, request: MainEntrypointGenerationRequest) -> str:
        return self.workflow.run(request)

    def close(self) -> None:
        self.workflow.close()


@dataclass(kw_only=True)
class RuntimeService:
    """Execute explicit process/log requests; the builder owns process handles."""

    logger: Any
    select_python_command: Callable[[], str]
    popen_subprocess: Callable[..., Any]
    run_subprocess: Callable[..., Any]
    timeout_expired: type[BaseException]

    def stop_managed_server_process(self, process: Optional[Any]) -> None:
        if process is None:
            return
        try:
            if callable(getattr(process, "poll", None)) and process.poll() is not None:
                return
        except Exception:
            return

        try:
            process.terminate()
            if callable(getattr(process, "wait", None)):
                process.wait(timeout=5)
            return
        except Exception:
            pass

        try:
            process.kill()
            if callable(getattr(process, "wait", None)):
                process.wait(timeout=5)
        except Exception:
            return

    def rerun_server(
        self,
        *,
        main_entrypoint_path: str,
        previous_process: Optional[Any] = None,
    ) -> tuple[Any, dict[str, Any]]:
        main_path = Path(main_entrypoint_path).expanduser().resolve()
        self.stop_managed_server_process(previous_process)
        python_cmd = self.select_python_command()
        backend_command = [python_cmd, str(main_path)]
        process = self.popen_subprocess(
            backend_command,
            cwd=str(main_path.parent),
            env=os.environ.copy(),
        )
        return process, {
            "pid": getattr(process, "pid", None),
            "command": backend_command,
            "cwd": str(main_path.parent),
        }

    def test_main_entrypoint(
        self,
        *,
        main_entrypoint_path: str,
        log_path: str,
    ) -> None:
        abs_path = os.path.abspath(main_entrypoint_path)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write("=== Main Entrypoint Test Log ===\n")
            log_file.write(f"Test started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write(f"Testing file: {abs_path}\n")
            log_file.write(f"{'=' * 50}\n\n")

            try:
                python_cmd = self.select_python_command()
                log_file.write(f"Executing: {python_cmd} {abs_path}\n\n")
                result = self.run_subprocess(
                    [python_cmd, abs_path],
                    cwd=os.path.dirname(abs_path),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                log_file.write(f"Return code: {result.returncode}\n\n")

                if result.stdout:
                    log_file.write(f"--- STDOUT ---\n{result.stdout}\n\n")
                if result.stderr:
                    log_file.write(f"--- STDERR ---\n{result.stderr}\n\n")

                if result.returncode == 0:
                    log_file.write("✓ Test completed successfully.\n")
                else:
                    log_file.write(f"✗ Test failed with return code {result.returncode}.\n")

            except self.timeout_expired:
                log_file.write("✗ Test timed out after 60 seconds.\n")
            except Exception as exc:
                log_file.write(f"✗ Test raised an exception: {type(exc).__name__}: {exc}\n")

            log_file.write(f"\n{'=' * 50}\n")
            log_file.write(f"Test ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        self.logger.info("Test log written to: %s", log_path)
        self.logger.debug("Main entrypoint test log generated for %s", abs_path)


def _declared_step_metadata(node_path: Path, node_name: str) -> dict[str, Any]:
    """Read literal node constants without triggering skill installs or module side effects."""
    try:
        tree = ast.parse(node_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return {}
    node_class = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == node_name), None)
    if node_class is None:
        return {}
    fields = {"STEP_ID": "id", "TITLE": "title", "PROMPT": "prompt", "DEPENDENCIES": "dependencies", "INPUT_REQUIRED": "inputRequired"}
    metadata: dict[str, Any] = {}
    for statement in node_class.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in fields or value is None:
                continue
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError):
                continue
            field = fields[target.id]
            if field == "inputRequired" and isinstance(parsed, bool):
                metadata[field] = parsed
            elif field == "dependencies" and isinstance(parsed, (list, tuple)) and all(isinstance(dep, str) for dep in parsed):
                metadata[field] = list(parsed)
            elif field in {"id", "title", "prompt"} and isinstance(parsed, str):
                metadata[field] = parsed
    return metadata


def build_steps_meta(
    planned_graph: Graph,
    *,
    node_location_map: Mapping[str, str],
    resolve_root_path: Callable[[str | Path], Path],
    expected_backend_node_path: Callable[..., Path],
    include_hidden_nodes: bool = False,
) -> list[dict[str, Any]]:
    steps_meta: list[dict[str, Any]] = []

    for node_name in planned_graph.get_topological_sorted_nodes():
        node_meta = planned_graph.get_node_meta(node_name)
        if node_meta is None:
            continue
        if not include_hidden_nodes and not bool(getattr(node_meta, "enable", True)):
            continue

        reference = resolve_workflow_node_reference(
            meta_node_kind=getattr(node_meta, "meta_node_kind", None),
            ext_data=getattr(node_meta, "ext_data", None),
        )
        prompt = str(getattr(node_meta, "desc", "") or "").strip() or reference.summary
        ext_data = getattr(node_meta, "ext_data", None)
        ext_type = str(ext_data.get("type", "none") if isinstance(ext_data, Mapping) else ext_data or "none").strip().lower()
        input_required = reference.input_required
        if reference.meta_node_kind == "WorkflowStepNode":
            input_required = ext_type == "user_input"
        backend_path = node_location_map.get(node_name)
        node_path = resolve_root_path(backend_path) if backend_path else expected_backend_node_path(node_name)
        declared = _declared_step_metadata(node_path, node_name)

        steps_meta.append(
            {
                "id": declared.get("id", node_name),
                "title": declared.get("title", node_name),
                "prompt": declared.get("prompt", prompt),
                "dependencies": declared.get("dependencies", list(getattr(node_meta, "depends", []) or [])),
                "inputRequired": declared.get("inputRequired", bool(input_required)),
                "nodeKind": str(reference.capability_category or "operation"),
                "extData": ext_data if isinstance(ext_data, Mapping) else (ext_data or {}),
                "metaNodeKind": str(reference.meta_node_kind or ""),
                "enabled": bool(getattr(node_meta, "enable", True)),
            }
        )

    return steps_meta