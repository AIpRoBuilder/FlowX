# AG-UI Lifecycle Main Backend Prompt
Use this system prompt whenever you need a runnable FastAPI backend for the AG-UI lifecycle workflow.

Target architecture:
- The generated file must follow the lifecycle backend style used in the reference example.
- Use `WorkflowEngine` from `ag_ui_workflow` (not `GPipeline`-style CLI architecture).
- Keep the implementation session-centric with one engine per `sessionId`.

Required imports:
- `from __future__ import annotations`
- `from pathlib import Path`
- `from dotenv import load_dotenv`
- `from fastapi import FastAPI, HTTPException`
- `from fastapi.responses import StreamingResponse`
- `from pydantic import BaseModel`
- `from ag_ui_workflow import WorkflowEngine`
- `import uvicorn`
- Import node classes from the generated root package (for example `from example_agent_output import StepA, StepB`)

Required globals and setup:
- Automatically load environment variables from `.env` at module startup, before app/engine creation.
- Resolve `.env` path by checking the generated file directory first, then fallback to project root-level `.env`.
- Call `load_dotenv(...)` for discovered path(s) with non-destructive behavior (do not override existing process env by default).
- `app = FastAPI(title=...)`
- `PIPELINE_JSON_PATH = Path(__file__).with_name("workflow_pipeline.json")`
- `ENGINES: dict[str, WorkflowEngine] = {}`
- `STEP_CHAIN = [NodeA.step_meta(), NodeB.step_meta(), ...]` in deterministic order
- End with a script launcher under `if __name__ == "__main__":` that calls `uvicorn.run(app, ...)`
- Do not import node classes from `.step_nodes`
- Do not use relative node imports such as `from . import ...`; import from root package name.
- Node imports must work in script execution style: `python main.py`.
- Avoid `try/except` import fallback blocks.

Required models:
- `RunStepInput` with fields: `sessionId`, `stepId`, `input`
- `RunStepInput` should additionally accept optional `file_path` for `user_file_input` steps (WorkflowFileNode).
- `RunStepInput.input` must support flexible payloads (`str | dict[str, Any] | None`), not `str`-only.
- `ResetSessionInput` with field: `sessionId`
- `ResetSessionOutput` with fields: `ok`, `sessionId`, `threadId`, `runId`
- Keep these camelCase field names exactly as shown

Required functions and endpoints:
- `_get_engine(session_id: str) -> WorkflowEngine`
    - Return cached engine from `ENGINES` when present
    - Otherwise create a new `WorkflowEngine` with:
        - `pipeline_json_path=str(PIPELINE_JSON_PATH)`
        - `steps_meta=STEP_CHAIN`
        - `thread_id` derived from session id
    - Store and return the created engine
- `GET /`
    - Return a simple JSON-serializable health payload confirming the backend is running
- For normal workflows, `POST /api/run-step`
    - Resolve engine via `_get_engine(payload.sessionId)`
    - Resolve step metadata by `payload.stepId` (from `STEP_CHAIN`) and branch by `extData.type`.
    - For `extData.type == "user_file_input"` (WorkflowFileNode):
        - If `payload.file_path` is provided, use `{"file_path": payload.file_path}` as step input.
        - Else pass `payload.input` through.
    - For all other step types, pass `payload.input` through to `engine._run_step_events(...)`.
    - Return `StreamingResponse(engine._run_step_events(step_id, normalized_input), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})`
- For cron workflows, replace `POST /api/run-step` with `POST /cron/start`
    - Do not generate `/api/run-step` in cron mode.
    - Start or reuse an in-memory periodic runner for the active session.
    - Use `asyncio.create_task(...)` for the background loop.
    - Periodically call `engine._run_all_steps_events(...)` based on the configured cron expression.
- `POST /api/reset-session` with `response_model=ResetSessionOutput`
    - Resolve engine and call `engine.reset_session()`
    - Return `ResetSessionOutput(ok=True, sessionId=..., threadId=engine.thread_id, runId=engine.session.run_id)`

Output constraints:
- Return only runnable Python code.
- No Markdown fences.
- No explanatory prose.
- Do not add unrelated routes, CLI parsing, or extra framework scaffolding.
