from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor


def _write_main_file(tmp_path, *, include_uvicorn_launcher: bool) -> str:
    launcher = ""
    if include_uvicorn_launcher:
        launcher = """

if __name__ == \"__main__\":
    uvicorn.run(app, host=\"0.0.0.0\", port=8000, reload=False)
"""

    main_code = f'''from __future__ import annotations

from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ag_ui_workflow import WorkflowEngine


app = FastAPI(title="example")
PIPELINE_JSON_PATH = Path(__file__).with_name("workflow_pipeline.json")
ENGINES: dict[str, WorkflowEngine] = {{}}
STEP_CHAIN = [{{"id": "step-a", "extData": {{"type": "none"}}}}]


class RunStepInput(BaseModel):
    sessionId: str
    stepId: str
    input: str | dict[str, Any] | None = None
    file_path: str | None = None


class ResetSessionInput(BaseModel):
    sessionId: str


class ResetSessionOutput(BaseModel):
    ok: bool
    sessionId: str
    threadId: str
    runId: str


def _get_engine(session_id: str) -> WorkflowEngine:
    engine = ENGINES.get(session_id)
    if engine is None:
        engine = WorkflowEngine(pipeline_json_path=str(PIPELINE_JSON_PATH), steps_meta=STEP_CHAIN, thread_id=session_id)
        ENGINES[session_id] = engine
    return engine


@app.get("/")
async def index() -> dict[str, str | bool]:
    return {{"ok": True, "service": "ag-ui-lifecycle-backend"}}


@app.post("/api/run-step")
async def run_step(payload: RunStepInput):
    engine = _get_engine(payload.sessionId)
    step_id = payload.stepId
    normalized_input = payload.input
    stream = engine._run_step_events(step_id, normalized_input)
    return StreamingResponse(stream, media_type="text/event-stream")


@app.post("/api/reset-session", response_model=ResetSessionOutput)
async def reset_session(payload: ResetSessionInput) -> ResetSessionOutput:
    engine = _get_engine(payload.sessionId)
    engine.reset_session()
    return ResetSessionOutput(ok=True, sessionId=payload.sessionId, threadId=engine.thread_id, runId=engine.session.run_id)
{launcher}
'''

    target = tmp_path / "main.py"
    target.write_text(main_code, encoding="utf-8")
    return str(target)


def test_main_entrypoint_auditor_requires_uvicorn_launcher(tmp_path):
    path = _write_main_file(tmp_path, include_uvicorn_launcher=False)

    ok, violations = MainEntryPointAuditor().audit_main_entrypoint_file(path)

    assert not ok
    assert any(violation.rule == "uvicorn_launcher_missing" for violation in violations)


def test_main_entrypoint_auditor_accepts_uvicorn_launcher(tmp_path):
    path = _write_main_file(tmp_path, include_uvicorn_launcher=True)

    ok, violations = MainEntryPointAuditor().audit_main_entrypoint_file(path)

    assert ok
    assert violations == []