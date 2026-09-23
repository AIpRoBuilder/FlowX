"""Generate PyDaoGraph context parameter classes via an LLM."""

from __future__ import annotations

import sys
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS


def _format_field_lines(fields: Mapping[str, object]) -> str:
    return "\n".join(f"- {name} = {repr(default)}" for name, default in fields.items())


def _to_snake(value: str) -> str:
    """Convert arbitrary names into safe snake_case identifiers."""

    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_")
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", cleaned).lower()
    return snake or "field"


def _summarize_graph_plan(graph_plan: Mapping[str, Any]) -> str:
    """Return a short description of pipeline nodes for extra context."""

    nodes = graph_plan.get("nodes", []) if isinstance(graph_plan, Mapping) else []
    names = [node.get("name") for node in nodes if isinstance(node, Mapping) and node.get("name")]
    if not names:
        return ""

    return "pipeline nodes: " + " -> ".join(names)


@dataclass
class PromptContextParamCoder(Coder):

    """Coder that generates `GParam` subclasses representing shared context."""

    prompt_path: str = "worker/prompts/pydaograph_context_prompt.md"

    def __post_init__(self) -> None:
        prompt_file = ROOT_DIR / self.prompt_path
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        self.system_prompt = prompt_file.read_text(encoding="utf-8")
        super().__post_init__()

    def _build_user_prompt(
        self,
        *,
        param_name: str,
        description: str,
        fields: Mapping[str, object],
        reset_behavior: str,
    ) -> str:
        field_lines = _format_field_lines(fields)
        return (
            "Create a single PyDaoGraph context parameter class.\n"
            f"Class name: {param_name}\n"
            f"Purpose: {description}\n"
            "Fields with default values (as class attributes):\n"
            f"{field_lines}\n"
            f"Reset behavior: {reset_behavior}\n"
            "Add a concise class-level `desc` string describing the parameter.\n"
            "Implement reset(self, curStatus: CStatus) to restore defaults.\n"
            "Return only runnable Python code for this class."
        )

    def _extract_context_fields(self, data_flow: Mapping[str, Any]) -> Mapping[str, object]:
        """Pick context nodes from a data-flow diagram and turn them into fields."""

        nodes = data_flow.get("nodeDataArray", []) if isinstance(data_flow, Mapping) else []
        contexts = [node for node in nodes if isinstance(node, Mapping) and node.get("type") == "context"]

        if not contexts:
            return {}

        fields: dict[str, object] = {}
        for index, node in enumerate(contexts, start=1):
            name = node.get("name") or f"context_{index}"
            field_name = _to_snake(name)
            if not field_name:
                field_name = f"context_{index}"

            # Default to empty string to keep fields simple and LLM-friendly.
            if field_name in fields:
                field_name = f"{field_name}_{index}"

            fields[field_name] = ""

        return fields

    def write_context_param(
        self,
        param_name: str,
        description: str,
        output_path: str,
        *,
        fields: Optional[Mapping[str, object]] = None,
        reset_behavior: Optional[str] = None,
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        """Generate a GParam subclass file with sensible defaults."""

        default_fields = {"value": 0, "count": 0}
        normalized_fields = dict(fields) if fields else default_fields
        reset_text = reset_behavior or "Restore every field to its default value."

        target_path = Path(output_path)
        if target_path.suffix.lower() != ".py":
            target_path = target_path.with_suffix(".py")

        target_path.parent.mkdir(parents=True, exist_ok=True)

        user_prompt = self._build_user_prompt(
            param_name=param_name,
            description=description,
            fields=normalized_fields,
            reset_behavior=reset_text,
        )

        return self.code_to_file(
            user_prompt,
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def write_context_param_from_data_flow(
        self,
        data_flow_path: str,
        param_name: str,
        output_path: str,
        *,
        graph_plan_path: Optional[str] = None,
        reset_behavior: Optional[str] = None,
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        """Generate a GParam subclass using context nodes from data-flow JSON and optional graph plan."""

        data_flow_file = Path(data_flow_path)
        if not data_flow_file.exists():
            raise FileNotFoundError(f"data_flow file not found: {data_flow_file}")

        data_flow = json.loads(data_flow_file.read_text(encoding="utf-8"))
        fields = self._extract_context_fields(data_flow)
        if not fields:
            fields = {"value": 0, "count": 0}

        graph_summary = ""
        if graph_plan_path:
            graph_plan_file = Path(graph_plan_path)
            if not graph_plan_file.exists():
                raise FileNotFoundError(f"graph_plan file not found: {graph_plan_file}")

            graph_plan = json.loads(graph_plan_file.read_text(encoding="utf-8"))
            graph_summary = _summarize_graph_plan(graph_plan)

        df_class = data_flow.get("class") if isinstance(data_flow, Mapping) else None
        df_desc = data_flow.get("description") if isinstance(data_flow, Mapping) else None
        description = df_desc or f"Context parameters derived from data-flow {df_class or ''}".strip()
        if graph_summary:
            description = f"{description}. {graph_summary}".strip()
        reset_text = reset_behavior or "Restore every field to its default value."

        target_path = Path(output_path)
        if target_path.suffix.lower() != ".py":
            target_path = target_path.with_suffix(".py")

        target_path.parent.mkdir(parents=True, exist_ok=True)

        user_prompt = self._build_user_prompt(
            param_name=param_name,
            description=description,
            fields=fields,
            reset_behavior=reset_text,
        )

        return self.code_to_file(
            user_prompt,
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )
