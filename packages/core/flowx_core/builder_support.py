from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from flowx_core.tools.workflow_node_reference import resolve_workflow_node_reference


def _default_artifact_dynamic_graph_cache() -> dict[str, Any]:
    return {
        "graph_nodes": [],
        "graph_plan_path": "",
        "node_plans": {},
        "backend_nodes": {},
        "node_tests": {},
        "node_input_output_formats": {},
    }


@dataclass
class ArtifactSessionState:
    requirement_md_path: Optional[str] = None
    requirement_analysis_result: Optional[dict[str, Any]] = None
    graph_plan_path: Optional[str] = None
    planned_graph: Any = None
    workflow_json_path: Optional[str] = None
    main_output_path: Optional[str] = None
    node_docs_dir: Optional[str] = None
    node_doc_paths: list[str] = field(default_factory=list)
    last_amended_node_doc_path: Optional[str] = None
    node_location_map: dict[str, str] = field(default_factory=dict)
    node_coder_map: dict[str, Any] = field(default_factory=dict)

    dynamic_graph_cache: dict[str, Any] = field(default_factory=_default_artifact_dynamic_graph_cache)


@dataclass
class RuntimeSessionState:
    log_path: Optional[str] = None
    backend_server_process: Any = None
    dynamic_graph_cache: dict[str, Any] = field(default_factory=lambda: {"server_runtime": {}})


class DynamicGraphCacheView(MutableMapping[str, Any]):
    def __init__(
        self,
        artifact_cache: dict[str, Any],
        runtime_cache: dict[str, Any],
    ) -> None:
        self._artifact_cache = artifact_cache
        self._runtime_cache = runtime_cache

    @staticmethod
    def _is_runtime_key(key: str) -> bool:
        return key == "server_runtime"

    def _cache_for_key(self, key: str) -> dict[str, Any]:
        return self._runtime_cache if self._is_runtime_key(key) else self._artifact_cache

    def __getitem__(self, key: str) -> Any:
        return self._cache_for_key(key)[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._cache_for_key(key)[key] = value

    def __delitem__(self, key: str) -> None:
        del self._cache_for_key(key)[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._artifact_cache
        for key in self._runtime_cache:
            if key not in self._artifact_cache:
                yield key

    def __len__(self) -> int:
        return len(set(self._artifact_cache) | set(self._runtime_cache))

    def replace(self, payload: Mapping[str, Any]) -> None:
        self._artifact_cache.clear()
        self._artifact_cache.update(_default_artifact_dynamic_graph_cache())
        self._runtime_cache.clear()
        self._runtime_cache.update({"server_runtime": {}})
        for key, value in payload.items():
            self[key] = value


@dataclass
class BuildSession:
    artifacts: ArtifactSessionState = field(default_factory=ArtifactSessionState)
    runtime: RuntimeSessionState = field(default_factory=RuntimeSessionState)
    _dynamic_graph_cache: DynamicGraphCacheView = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._dynamic_graph_cache = DynamicGraphCacheView(
            self.artifacts.dynamic_graph_cache,
            self.runtime.dynamic_graph_cache,
        )

    @property
    def requirement_md_path(self) -> Optional[str]:
        return self.artifacts.requirement_md_path

    @requirement_md_path.setter
    def requirement_md_path(self, value: Optional[str]) -> None:
        self.artifacts.requirement_md_path = value

    @property
    def requirement_analysis_result(self) -> Optional[dict[str, Any]]:
        return self.artifacts.requirement_analysis_result

    @requirement_analysis_result.setter
    def requirement_analysis_result(self, value: Optional[dict[str, Any]]) -> None:
        self.artifacts.requirement_analysis_result = value

    @property
    def graph_plan_path(self) -> Optional[str]:
        return self.artifacts.graph_plan_path

    @graph_plan_path.setter
    def graph_plan_path(self, value: Optional[str]) -> None:
        self.artifacts.graph_plan_path = value

    @property
    def planned_graph(self) -> Any:
        return self.artifacts.planned_graph

    @planned_graph.setter
    def planned_graph(self, value: Any) -> None:
        self.artifacts.planned_graph = value

    @property
    def workflow_json_path(self) -> Optional[str]:
        return self.artifacts.workflow_json_path

    @workflow_json_path.setter
    def workflow_json_path(self, value: Optional[str]) -> None:
        self.artifacts.workflow_json_path = value

    @property
    def main_output_path(self) -> Optional[str]:
        return self.artifacts.main_output_path

    @main_output_path.setter
    def main_output_path(self, value: Optional[str]) -> None:
        self.artifacts.main_output_path = value

    @property
    def node_docs_dir(self) -> Optional[str]:
        return self.artifacts.node_docs_dir

    @node_docs_dir.setter
    def node_docs_dir(self, value: Optional[str]) -> None:
        self.artifacts.node_docs_dir = value

    @property
    def node_doc_paths(self) -> list[str]:
        return self.artifacts.node_doc_paths

    @node_doc_paths.setter
    def node_doc_paths(self, value: list[str]) -> None:
        self.artifacts.node_doc_paths = value

    @property
    def last_amended_node_doc_path(self) -> Optional[str]:
        return self.artifacts.last_amended_node_doc_path

    @last_amended_node_doc_path.setter
    def last_amended_node_doc_path(self, value: Optional[str]) -> None:
        self.artifacts.last_amended_node_doc_path = value

    @property
    def log_path(self) -> Optional[str]:
        return self.runtime.log_path

    @log_path.setter
    def log_path(self, value: Optional[str]) -> None:
        self.runtime.log_path = value

    @property
    def node_location_map(self) -> dict[str, str]:
        return self.artifacts.node_location_map

    @node_location_map.setter
    def node_location_map(self, value: dict[str, str]) -> None:
        self.artifacts.node_location_map = value

    @property
    def node_coder_map(self) -> dict[str, Any]:
        return self.artifacts.node_coder_map

    @node_coder_map.setter
    def node_coder_map(self, value: dict[str, Any]) -> None:
        self.artifacts.node_coder_map = value

    @property
    def dynamic_graph_cache(self) -> DynamicGraphCacheView:
        return self._dynamic_graph_cache

    @dynamic_graph_cache.setter
    def dynamic_graph_cache(self, value: Mapping[str, Any]) -> None:
        self._dynamic_graph_cache.replace(value)

    @property
    def backend_server_process(self) -> Any:
        return self.runtime.backend_server_process

    @backend_server_process.setter
    def backend_server_process(self, value: Any) -> None:
        self.runtime.backend_server_process = value


@dataclass
class LLMComponentBundle:
    analyzer: Any
    planner: Any
    node_planner: Any
    main_writer: Any


@dataclass
class BuilderComponentFactory:
    api_key: str
    model: str
    provider: str
    root_dir: str
    skills_root_path: str
    session_marking_prompt: str
    analyzer_cls: Any
    planner_cls: Any
    node_planner_cls: Any
    main_writer_cls: Any
    workflow_step_node_coder_cls: Any
    workflow_operation_node_coder_cls: Any
    workflow_file_node_coder_cls: Any
    workflow_skill_node_coder_cls: Any
    spatial_temporal_contract_node_coder_cls: Any

    def create_bundle(self) -> LLMComponentBundle:
        analyzer = self.analyzer_cls(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            session_marking_prompt=self.session_marking_prompt,
        )
        planner = self.planner_cls(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            skills_root_path=self.skills_root_path,
            session_marking_prompt=self.session_marking_prompt,
        )
        node_planner = self.node_planner_cls(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            skills_root_path=self.skills_root_path,
            session_marking_prompt=self.session_marking_prompt,
        )
        main_writer = self.main_writer_cls(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            session_marking_prompt=self.session_marking_prompt,
        )
        return LLMComponentBundle(
            analyzer=analyzer,
            planner=planner,
            node_planner=node_planner,
            main_writer=main_writer,
        )

    def create_node_coder(self, node_meta: Any, *, root_dir_path: str) -> Any:
        ext_data = node_meta.ext_data if node_meta and hasattr(node_meta, "ext_data") else None
        meta_node_kind = getattr(node_meta, "meta_node_kind", None) if node_meta is not None else None
        reference = resolve_workflow_node_reference(
            meta_node_kind=meta_node_kind,
            ext_data=ext_data,
        )

        coder_kwargs = {
            "api_key": self.api_key,
            "model": self.model,
            "provider": self.provider,
            "root_dir_path": root_dir_path,
            "session_marking_prompt": self.session_marking_prompt,
        }

        if reference.meta_node_kind == "WorkflowSkillNode":
            return self.workflow_skill_node_coder_cls(
                skills_root_path=self.skills_root_path,
                **coder_kwargs,
            )
        if reference.meta_node_kind == "SpatialTemporalContractNode":
            return self.spatial_temporal_contract_node_coder_cls(**coder_kwargs)
        if reference.meta_node_kind == "WorkflowOperationNode":
            return self.workflow_operation_node_coder_cls(**coder_kwargs)
        if reference.meta_node_kind == "WorkflowFileNode":
            return self.workflow_file_node_coder_cls(**coder_kwargs)
        return self.workflow_step_node_coder_cls(**coder_kwargs)


def format_rule_violations(violations: Sequence[Any]) -> str:
    return "\n".join(
        f"Line {getattr(violation, 'lineno', 0)}: {getattr(violation, 'rule', '')} - {getattr(violation, 'detail', '')}"
        for violation in violations
    )


@dataclass
class AuditRepairLoop:
    logger: Any
    max_attempts: int
    render_feedback: Callable[[Sequence[Any]], str] = format_rule_violations

    def run(
        self,
        *,
        audit: Callable[[], tuple[bool, Sequence[Any]]],
        amend: Callable[[str, int], None],
        failure_message_prefix: str,
        on_success: Optional[Callable[[int], None]] = None,
        on_retry: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        for audit_round in range(1, self.max_attempts + 1):
            ok, violations = audit()
            if ok:
                if on_success is not None:
                    on_success(audit_round)
                return

            amendment = self.render_feedback(violations)
            if audit_round >= self.max_attempts:
                raise RuntimeError(f"{failure_message_prefix}{amendment}")

            if on_retry is not None:
                on_retry(amendment, audit_round)
            amend(amendment, audit_round)