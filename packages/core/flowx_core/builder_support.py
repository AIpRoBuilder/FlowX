"""Builder-owned artifact bookkeeping.

Modified 2026-10-14: retain published paths in the builder, never service run contexts.
Modified 2026-10-16: drop runtime/process state, test-path caches and node coder lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WorkflowArtifactState:
    """Published locations for a named workflow, not a retained generation request."""

    graph_plan_path: str
    node_location_map: dict[str, str] = field(default_factory=dict)
    node_doc_paths: dict[str, str] = field(default_factory=dict)


@dataclass
class ArtifactSessionState:
    requirement_md_path: Optional[str] = None
    graph_plan_path: Optional[str] = None
    planned_graph: Any = None
    workflow_json_path: Optional[str] = None
    main_output_path: Optional[str] = None
    node_location_map: dict[str, str] = field(default_factory=dict)
    workflow_artifacts: dict[str, WorkflowArtifactState] = field(default_factory=dict)


@dataclass
class BuildSession:
    artifacts: ArtifactSessionState = field(default_factory=ArtifactSessionState)

    @property
    def requirement_md_path(self) -> Optional[str]:
        return self.artifacts.requirement_md_path

    @requirement_md_path.setter
    def requirement_md_path(self, value: Optional[str]) -> None:
        self.artifacts.requirement_md_path = value

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
    def node_location_map(self) -> dict[str, str]:
        return self.artifacts.node_location_map

    @node_location_map.setter
    def node_location_map(self, value: dict[str, str]) -> None:
        self.artifacts.node_location_map = value


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
    session_marking_prompt: str
    analyzer_cls: Any
    planner_cls: Any
    node_planner_cls: Any
    main_writer_cls: Any

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
            session_marking_prompt=self.session_marking_prompt,
        )
        node_planner = self.node_planner_cls(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
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