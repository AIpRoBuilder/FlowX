<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
Moved 2026-11-22 from worker/prompts/pydaograph_node_prompt.md and adapted from an LLM system prompt into a contract for a coding agent.
Modified: explicit subclass wait gates for the pooled AG-UI runtime.
Modified: bytes-only file processing, explicit persistence, and undecorated fresh node construction.
Modified 2026-11-02: clarify runtime variable types and metadata dictionary access.
Modified 2026-11-23: add node-editing baseline guidance and prohibit direct edits to FlowX-owned workflow files.
Modified 2026-11-25: update WorkflowFileNode contract from single file_bytes to files: dict[str, bytes]. -->
# Node contract

You write one workflow node: a Python module that defines one `WorkflowStepNode` or `WorkflowFileNode` subclass. Submit it as the `code` artifact, together with its pytest module as the `test` artifact (see the test contract). Keep the code runnable with real logic inside the preferred subclass hook declared by the selected base-node reference in `meta.node_reference`.

Core objective:
- Generate the smallest runnable node implementation that fully satisfies the requirement analysis.
- Prefer direct, readable code over abstraction.
- Avoid over-engineering: no extra classes, no architecture layers, no speculative extensibility.
- Do not mock data or simulate node execution/processes; implement real runnable logic that uses actual inputs/dependencies.

Runtime variable types and access:
- `step_meta()` returns `StepMeta`: `StepMeta` is a `TypedDict` and a `dict` at runtime, not an object with metadata attributes. Read its required fields as `meta["id"]` (`str`) and `meta["dependencies"]` (`list[str]`); never use `meta.id` or `meta.dependencies`. Other metadata keys are optional; use `.get(...)` when their presence is not guaranteed.
- Node constants `STEP_ID`, `TITLE`, and `PROMPT` are `str`; `DEPENDENCIES` is `list[str]`; `INPUT_REQUIRED` is `bool`.
- `inputs_format` is a workflow.json declaration, not a runtime dict. If it declares one `string`
  field, `process_input` receives that value as plain text. If it declares several fields, or an
  `object`/`array` field, `process_input` receives the submitted value as JSON text: parse it with
  `json.loads(user_input)` and validate the declared keys and types. Do not assume `user_input` is a
  `dict`, and do not rely on the runtime to pass each declared field separately.
- The `WorkflowStepNode.process_input` hook receives `user_input: str`; the `WorkflowFileNode.process_input` hook receives `files: dict[str, bytes]`. The file runtime accepts a non-empty `dict[str, bytes | bytearray]` with non-empty string file names and passes a fresh `dict[str, bytes]` to the hook.
- Both hooks receive `dependency_results: dict[str, StepRunOutput]` and `session_state: dict[str, Any]`. A `StepRunOutput` is an object whose `.card` and `.derived` are `dict[str, Any]`; access an upstream value with `dependency_results[step_id].derived[key]`. Values inside those dictionaries and `session_state` depend on the workflow, so do not assume their types without the node contract.
- After pipeline attachment, `get_workflow_param()` returns a `WorkflowParam` object: `param.inputs` is `dict[str, StepRunInput]`, `param.outputs` is `dict[str, StepRunOutput]`, and `param.state` is `dict[str, Any]`. Raw input values are not necessarily `str` (they may also be bytes, bytearray, messages, lists, dictionaries, or `None`); distinguish them from the text passed to the step hook. Access shared inputs and state under `param.condition`.

Guidelines:
- Write code with the simplest possible approach that satisfies requirements.
- Choose the subclass base from node metadata `meta_node_kind` first. Only if `meta_node_kind` is absent may you fall back to `ext_data.type` using the `contract.node_types` catalog.
- Treat the ag_ui_workflow base-node catalog in `contract.node_types` and the selected base-node reference in `meta.node_reference` as the only source of truth for:
    - which base class to import
    - whether the node is interactive
    - which processing hook to implement or inherit
    - whether `inputs_format` is allowed
- Treat `StepRunOutput card/derived contract methods` from the selected base-node reference as the authoritative source for output keys, value shapes, and response format.
- Treat `decorator-marked subclass implementation hooks` from the selected base-node reference as the preferred methods a subclass should implement or customize.
- Do not override other inherited methods unless the selected base-node contract or the explicit requirement requires it.
- `INPUT_REQUIRED` is metadata only. Required direct input needs `__init__` calling `super().__init__()` and `self.setWaitFunc(self.wait_function)`. Define the non-blocking callback in the subclass, obtain `self.get_workflow_param()` after attachment, and access inputs/state under `param.condition`. Return `True` if `param.can_run(self.STEP_ID, self.DEPENDENCIES)` is false so failed dependencies cannot strand gates; otherwise report `awaiting_input`/`running` and return readiness. Automatic nodes install no gate.
- Pooled nodes are reused; keep request state in `session_state`/`WorkflowParam`, not persistent instance fields.
- Import only the selected workflow base class from `ag_ui_workflow.nodes` and `StepRunOutput` from `ag_ui_workflow.workflow_types`, plus any actually used helpers.
- Generate ordinary undecorated subclasses. The runtime uses per-`STEP_ID` class maps to construct fresh nodes; neither `@register_class` nor `clone(self) -> self` is required. Existing optional legacy decorators or clone methods may remain.
- `WorkflowFileNode` is abstract: implement `process_input(self, files: dict[str, bytes], dependency_results, session_state) -> StepRunOutput` to process uploaded files mapped by file name. Implement persistence explicitly only when required; the base class provides no upload decoding or file saving. The HTTP backend converts file paths/base64/uploads to `dict[str, bytes]` before engine input; do not normalize those formats in the subclass. A file gate requires a non-empty `dict` at `param.inputs[self.STEP_ID]` whose keys are non-empty stripped `str` file names and whose values are `(bytes, bytearray)`; an empty `b""` entry may be valid while `{}` is not.
- Define class constants:
    - `STEP_ID` (machine-readable id)
    - `TITLE` (human-readable step title)
    - `PROMPT` (input prompt for user)
    - `DEPENDENCIES` (list of upstream step ids)
- Implement only the processing hook allowed by the selected base-node contract from the catalog in `contract.node_types`.
- If the selected base node is the spatial-temporal contract variant, keep the inherited `run` / runtime flow, and use the selected base-node reference plus node contract instructions as the source of truth for the parsed subclass hook and any prompt/guidance helper methods reachable from it.
- When the node's own description or `PROMPT` should steer contract generation, add the smallest override among those parsed prompt/guidance helper methods so that guidance enters the inherited generation flow.
- Use the parsed subclass implementation hook itself only when the node truly needs custom model invocation or response parsing beyond prompt-guidance changes.
- Use `dependency_results[<step_id>].derived[...]` to read prerequisite outputs.
- Extract upstream variables only from dependency nodes listed in `DEPENDENCIES`.
- The upstream nodes in `context.ancestors` (source, `derived_keys`, `session_state_keys`) are authoritative for upstream `STEP_ID` and `derived` keys.
- Do not invent upstream variable names/keys; match keys from dependent nodes' returned `StepRunOutput.derived` structure.
- Do not assign hardcoded empty placeholders (for example `""`, `[]`, `{}`) to required upstream values; read from dependency outputs and fail fast with a clear validation error when required values are missing.
- If a required upstream key cannot be confirmed from provided dependency context, use safe fallback handling with minimal branching and add one concise TODO only when necessary.
- Persist mutable cross-step values in `session_state`.
- Return `StepRunOutput(card=..., derived=...)`.
- Keep `card` JSON-serializable and practical for downstream consumers.
- Keep `derived` as structured values for downstream step computation.
- For prompt, image, video, FFmpeg, or Blender workflows, import `media_space` from `flowx_core.service.media` and obtain `space = media_space(self.STEP_ID, session_state)` (do not read private `session_state[...]` keys directly). Use `space.generate_image(...)`, `space.generate_video(...)`, `space.resize_image(...)`, `space.crop_image(...)`, `space.save_image(...)`, `space.save_bytes(...)`, `space.ffmpeg_trim_resize(...)`, `space.ffmpeg(...)`, or `space.blender_render(...)` to produce `asset` (`MediaAsset`), and return explicit dict literals for canvas rendering and static AST schema inspection:
    - Prompt nodes: `card={"kind": "prompt", "prompt": prompt_text, "summary": prompt_text}` and `derived={"prompt": prompt_text}`.
    - Image nodes: `card={"kind": "image", "media": asset.relpath, "mime": asset.mime, "width": asset.width, "height": asset.height}` and `derived={"image_path": str(asset.abspath), "width": asset.width, "height": asset.height}`.
    - Video nodes: `card={"kind": "video", "media": asset.relpath, "poster": asset.poster_relpath, "mime": asset.mime, "duration": asset.duration}` and `derived={"video_path": str(asset.abspath), "duration": asset.duration}`.
- Do not override `run` unless explicitly required; base class `run` orchestrates flow.
- If the required behavior cannot be fully implemented from available context, keep valid runnable placeholder logic and add a concise TODO comment for the missing detail.
- Never fabricate outputs (for example fake API responses, synthetic records, or simulated service success) when real execution paths are required.
- If external data access is required by node metadata, add a dedicated helper function (for example `query_data`) and call it from the selected subclass hook.

Minimality checklist (must follow):
- Keep imports minimal; only import symbols actually used.
- Keep one preferred subclass hook implementation when the selected base-node reference marks one; otherwise rely on the inherited base processing by default.
- Do not add helper methods unless they remove duplicated logic used at least twice.
- Do not include mocked/sample/simulated runtime data in business logic.
- Add detailed debug logging that writes to a local file path so execution can be inspected after runs.
- Keep logging implementation simple: prefer the standard `logging` module, a module-local logger, a `FileHandler`, and `pathlib.Path(...).mkdir(parents=True, exist_ok=True)` for the log directory.
- Log meaningful checkpoints, resolved inputs/dependency values, validation failures, external command execution outcomes, and raised exceptions without leaking secrets.
- Do not add debug prints, test stubs, or markdown/comments beyond concise TODOs.
- Use straightforward guard clauses and `.get(...)` defaults instead of complex validation frameworks.
- Keep summaries/cards concise and requirement-focused.

Editing an existing node:
- Never edit files under `<workspace>/workflows/` directly (published node versions, snapshots, `plan/`, or `service/`); submit changes only through `submit_task`.
- When `context.baseline` is present, start from that published version and apply only the change requested by `context.intent` and `context.changes` (spec diff or upstream parent `source_diff`). Keep outputs backward-compatible with downstream nodes unless the change explicitly requires updating them.

Example:

The example below reflects the current installed ag_ui_workflow hook names. If `meta.node_reference` differs, follow the reference instead of the example.

```python
from __future__ import annotations

from typing import Any

from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput


def _parse_float(text: str, field_name: str) -> float:
    value = text.strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number, got: {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return parsed


class ExpenseNode(WorkflowStepNode):
    STEP_ID = "ExpenseNode"
    TITLE = "Step 2 · Expense"
    PROMPT = "Enter monthly expense"
    DEPENDENCIES = ["income"]

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            ready = self.STEP_ID in param.inputs
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        expenses = _parse_float(user_input, "Monthly expense")
        income = dependency_results["income"].derived["monthlyIncome"]
        savings = income - expenses
        savings_rate = (savings / income * 100.0) if income > 0 else 0.0

        session_state["expenses"] = expenses
        session_state["savings"] = savings
        session_state["savingsRate"] = savings_rate
        card = {
            "label": "Expense + savings result",
            "rows": [
                {"name": "monthlyExpense", "value": f"{expenses:.2f}"},
                {"name": "monthlySavings", "value": f"{savings:.2f}"},
                {"name": "savingsRate", "value": f"{savings_rate:.2f}%"},
            ],
        }
        derived = {
            "monthlyExpense": expenses,
            "monthlySavings": savings,
            "savingsRate": savings_rate,
        }
        return StepRunOutput(card=card, derived=derived)


class BudgetAdvisorNode(WorkflowStepNode):
    STEP_ID = "BudgetAdvisorNode"
    TITLE = "Step 2.5 · Budget Advisor"
    PROMPT = "Ask a budgeting question"
    DEPENDENCIES = ["income", "ExpenseNode"]

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            ready = self.STEP_ID in param.inputs
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        monthly_income = dependency_results["income"].derived.get("monthlyIncome", 0.0)
        monthly_expense = dependency_results["ExpenseNode"].derived.get("monthlyExpense", 0.0)
        monthly_savings = monthly_income - monthly_expense

        session_state["monthlySavings"] = monthly_savings

        answer = (
            "Budget advice based on the current context.\n"
            f"Question: {user_input}\n"
            f"monthlyIncome={monthly_income:.2f}, monthlyExpense={monthly_expense:.2f}, monthlySavings={monthly_savings:.2f}"
        )
        card = {
            "label": "Budget advisor result",
            "rows": [
                {"name": "question", "value": user_input},
                {"name": "advice", "value": answer},
            ],
        }
        derived = {
            "question": user_input,
            "advice": answer,
            "monthlySavings": monthly_savings,
        }
        return StepRunOutput(card=card, derived=derived)


class SavingsPlanNode(WorkflowStepNode):
    STEP_ID = "SavingsPlanNode"
    TITLE = "Step 3 · Savings Plan"
    PROMPT = ""
    DEPENDENCIES = ["income", "expense"]
    INPUT_REQUIRED = False

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input
        monthly_income = dependency_results["income"].derived["monthlyIncome"]
        monthly_expense = dependency_results["expense"].derived["monthlyExpense"]
        monthly_savings = monthly_income - monthly_expense
        annual_savings = monthly_savings * 12.0

        session_state["monthlySavings"] = monthly_savings
        session_state["annualSavings"] = annual_savings

        card = {
            "label": "Savings plan projection",
            "rows": [
                {"name": "monthlyIncome", "value": f"{monthly_income:.2f}"},
                {"name": "monthlyExpense", "value": f"{monthly_expense:.2f}"},
                {"name": "monthlySavings", "value": f"{monthly_savings:.2f}"},
                {"name": "annualSavings", "value": f"{annual_savings:.2f}"},
            ],
        }
        derived = {
            "monthlySavings": monthly_savings,
            "annualSavings": annual_savings,
        }
        return StepRunOutput(card=card, derived=derived)
```

For uploaded text files, process the `{file_name: file_bytes}` mapping without assuming inherited persistence:

```python
from typing import Any

from ag_ui_workflow.nodes import WorkflowFileNode
from ag_ui_workflow.workflow_types import StepRunOutput


class UploadNode(WorkflowFileNode):
    STEP_ID = "UploadNode"
    TITLE = "Read uploaded text"
    PROMPT = "Upload one or more UTF-8 text files"
    DEPENDENCIES = []

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            raw_files = param.inputs.get(self.STEP_ID)
            ready = (
                isinstance(raw_files, dict)
                and bool(raw_files)
                and all(
                    isinstance(name, str) and bool(name.strip()) and isinstance(data, (bytes, bytearray))
                    for name, data in raw_files.items()
                )
            )
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(
        self,
        files: dict[str, bytes],
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        texts = {name: data.decode("utf-8") for name, data in files.items()}
        byte_count = sum(len(data) for data in files.values())
        return StepRunOutput(
            card={"label": "Uploaded text", "fileNames": list(files), "byteCount": byte_count},
            derived={"files": texts, "fileNames": list(files), "byteCount": byte_count},
        )
```

When asked to create new nodes, follow this shape: choose the subclass base from `meta_node_kind`, import only that base class from `ag_ui_workflow.nodes`, define declarative class constants, and add a runnable processing method only when the selected base-node contract requires one.
