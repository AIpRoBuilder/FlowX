# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-27: unit tests for DAG-aware per-pass RunJournal and session run rotation.
"""Unit tests for flowx_core.service.run_journal.RunJournal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from flowx_core.service.run_journal import RunJournal


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _step_card(step_id: str, *, state: str = "completed", is_final: bool = False, summary: str = "") -> str:
    return _sse({
        "type": "CUSTOM",
        "name": "step_card",
        "value": {
            "stepId": step_id,
            "title": step_id,
            "state": state,
            "card": {"summary": summary or f"{step_id} ok"},
            "isFinal": is_final,
        },
    })


def _input_required(step_id: str, prompt: str = "Need input") -> str:
    return _sse({
        "type": "CUSTOM",
        "name": "input_required",
        "value": {"stepId": step_id, "title": step_id, "prompt": prompt},
    })


def _finished(*, ok: bool = True, step_id: str = "", awaiting: str | None = None, is_final: bool = False) -> str:
    result: dict[str, Any] = {"ok": ok, "stepId": step_id, "isFinal": is_final}
    if awaiting is not None:
        result["awaitingInput"] = awaiting
    return _sse({"type": "RUN_FINISHED", "result": result})


def _read_run(workflow_root: Path, run_id: str) -> dict[str, Any]:
    return json.loads((workflow_root / "runs" / run_id / "run.json").read_text(encoding="utf-8"))


def test_step_map_head_tail_from_step_meta_and_plan_fallback(tmp_path: Path) -> None:
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "workflow.json").write_text(
        json.dumps({
            "nodes": [
                {"name": "HeadA", "depends": []},
                {"name": "HeadB", "depends": []},
                {"name": "Disabled", "depends": [], "enable": False},
                {"name": "Mid", "depends": ["HeadA", "HeadB"]},
                {"name": "Tail", "depends": ["Mid"]},
            ]
        }),
        encoding="utf-8",
    )

    from_plan = RunJournal(tmp_path)
    assert from_plan.head_steps == frozenset({"HeadA", "HeadB"})
    assert from_plan.tail_steps == frozenset({"Tail"})
    assert "Disabled" not in from_plan.steps

    from_meta = RunJournal(
        tmp_path,
        steps=[
            {"id": "NovelInput", "dependencies": []},
            {"id": "PanelPlan", "dependencies": ["NovelInput"]},
            {"id": "DialogueComposite", "dependencies": ["PanelPlan"]},
        ],
    )
    assert from_meta.head_steps == frozenset({"NovelInput"})
    assert from_meta.tail_steps == frozenset({"DialogueComposite"})


def test_sequential_passes_on_same_session_create_distinct_completed_runs(tmp_path: Path) -> None:
    steps = [
        {"id": "NovelInput", "dependencies": []},
        {"id": "CharacterSeedInput", "dependencies": ["NovelInput"]},
        {"id": "DialogueComposite", "dependencies": ["CharacterSeedInput"]},
    ]
    journal = RunJournal(tmp_path, steps=steps)

    # Pass 1: pause at CharacterSeedInput, then resume to completion.
    run1, new1 = journal.resolve_run_id("comic-run", "NovelInput")
    assert new1 is True
    list(journal.tee(run1, "comic-run", iter([
        _step_card("NovelInput", summary="novel 1"),
        _input_required("CharacterSeedInput", "Upload seed"),
        _finished(ok=True, step_id="NovelInput", awaiting="CharacterSeedInput"),
    ])))
    snap1_mid = _read_run(tmp_path, run1)
    assert snap1_mid["status"] == "input_required"
    assert snap1_mid["awaiting"]["stepId"] == "CharacterSeedInput"

    run1_resume, new1_resume = journal.resolve_run_id("comic-run", "CharacterSeedInput")
    assert run1_resume == run1 and new1_resume is False
    list(journal.tee(run1_resume, "comic-run", iter([
        _step_card("CharacterSeedInput", summary="seed 1"),
        _step_card("DialogueComposite", is_final=True, summary="strip 1"),
        _finished(ok=True, step_id="DialogueComposite", is_final=True),
    ])))
    snap1_done = _read_run(tmp_path, run1)
    assert snap1_done["status"] == "completed"
    assert snap1_done["awaiting"] is None

    # Pass 2 on the same session starts a fresh run_id and leaves Pass 1 untouched.
    run2, new2 = journal.resolve_run_id("comic-run", "NovelInput")
    assert new2 is True and run2 != run1
    list(journal.tee(run2, "comic-run", iter([
        _step_card("NovelInput", summary="novel 2"),
        _step_card("CharacterSeedInput", summary="seed 2"),
        _step_card("DialogueComposite", is_final=True, summary="strip 2"),
        _finished(ok=True, step_id="DialogueComposite", is_final=True),
    ])))

    # Pass 3 with a trailing non-head call after tail completion also starts a new run instead of corrupting Pass 2.
    run3, new3 = journal.resolve_run_id("comic-run", "CharacterSeedInput")
    assert new3 is True and run3 not in {run1, run2}
    list(journal.tee(run3, "comic-run", iter([
        _input_required("NovelInput", "Paste novel"),
        _finished(ok=True, awaiting="NovelInput"),
    ])))

    assert _read_run(tmp_path, run1)["status"] == "completed"
    assert _read_run(tmp_path, run2)["status"] == "completed"
    assert _read_run(tmp_path, run2)["awaiting"] is None
    assert _read_run(tmp_path, run3)["status"] == "input_required"
    assert _read_run(tmp_path, run3)["awaiting"]["stepId"] == "NovelInput"


def test_head_retry_reuses_run_while_head_restart_mid_pass_abandons_old_run(tmp_path: Path) -> None:
    steps = [
        {"id": "NovelInput", "dependencies": []},
        {"id": "CharacterSeedInput", "dependencies": ["NovelInput"]},
        {"id": "DialogueComposite", "dependencies": ["CharacterSeedInput"]},
    ]
    journal = RunJournal(tmp_path, steps=steps)
    abandoned: list[str] = []

    # First submit rejected at head step -> pauses on NovelInput.
    run1, new1 = journal.resolve_run_id("s1", "NovelInput", on_abandon=abandoned.append)
    assert new1 is True
    list(journal.tee(run1, "s1", iter([
        _input_required("NovelInput", "Too short"),
        _finished(ok=True, awaiting="NovelInput"),
    ])))

    # Retrying NovelInput while awaiting NovelInput must reuse run1.
    run1_retry, new_retry = journal.resolve_run_id("s1", "NovelInput", on_abandon=abandoned.append)
    assert run1_retry == run1 and new_retry is False
    assert abandoned == []
    list(journal.tee(run1_retry, "s1", iter([
        _step_card("NovelInput", summary="accepted"),
        _input_required("CharacterSeedInput", "Upload seed"),
        _finished(ok=True, step_id="NovelInput", awaiting="CharacterSeedInput"),
    ])))

    # Submitting NovelInput again while paused downstream abandons and cancels run1, starting run2.
    run2, new2 = journal.resolve_run_id("s1", "NovelInput", on_abandon=abandoned.append)
    assert new2 is True and run2 != run1
    assert abandoned == [run1]
    snap1 = _read_run(tmp_path, run1)
    assert snap1["status"] == "cancelled"
    assert snap1["awaiting"] is None


def test_multi_root_dag_resumes_sibling_head_then_rotates_after_completion(tmp_path: Path) -> None:
    steps = [
        {"id": "HeadA", "dependencies": []},
        {"id": "HeadB", "dependencies": []},
        {"id": "Join", "dependencies": ["HeadA", "HeadB"]},
    ]
    journal = RunJournal(tmp_path, steps=steps)

    run1, new1 = journal.resolve_run_id("multi", "HeadA")
    assert new1 is True
    list(journal.tee(run1, "multi", iter([
        _step_card("HeadA"),
        _input_required("HeadB"),
        _finished(ok=True, step_id="HeadA", awaiting="HeadB"),
    ])))

    # HeadB is also in head_steps, but has not completed in run1 -> must resume run1.
    run1_b, new_b = journal.resolve_run_id("multi", "HeadB")
    assert run1_b == run1 and new_b is False
    list(journal.tee(run1_b, "multi", iter([
        _step_card("HeadB"),
        _step_card("Join", is_final=True),
        _finished(ok=True, step_id="Join", is_final=True),
    ])))
    assert _read_run(tmp_path, run1)["status"] == "completed"

    # Next HeadA starts a fresh pass.
    run2, new2 = journal.resolve_run_id("multi", "HeadA")
    assert new2 is True and run2 != run1


def test_tail_completed_legacy_input_required_snapshot_rotates_without_cancelling(tmp_path: Path) -> None:
    steps = [
        {"id": "Head", "dependencies": []},
        {"id": "Tail", "dependencies": ["Head"]},
    ]
    legacy_dir = tmp_path / "runs" / "legacy-run"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "run.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run_id": "legacy-run",
            "session_id": "sess",
            "workflow_version": 1,
            "status": "input_required",
            "awaiting": {"stepId": "Head", "title": "", "prompt": ""},
            "nodes": {
                "Head": {"status": "awaiting_input", "title": "Head", "card": {}},
                "Tail": {"status": "completed", "title": "Tail", "card": {}, "is_final": True},
            },
        }),
        encoding="utf-8",
    )

    abandoned: list[str] = []
    journal = RunJournal(tmp_path, steps=steps)
    new_run, started_new = journal.resolve_run_id(
        "sess", "Head", current_run_id="legacy-run", on_abandon=abandoned.append,
    )
    assert started_new is True and new_run != "legacy-run"
    assert abandoned == ["legacy-run"]
    # Already tail-completed snapshot is not overwritten with "cancelled".
    assert _read_run(tmp_path, "legacy-run")["status"] == "input_required"


def test_bind_session_run_uses_reset_run_id_and_clears_awaiting_on_error(tmp_path: Path) -> None:
    steps = [{"id": "Only", "dependencies": []}]
    journal = RunJournal(tmp_path, steps=steps)

    journal.bind_session_run("sess", "fresh-reset-id")
    resolved, started_new = journal.resolve_run_id("sess", "Only")
    assert resolved == "fresh-reset-id" and started_new is False

    list(journal.tee(resolved, "sess", iter([
        _input_required("Only"),
        _sse({"type": "RUN_ERROR", "message": "boom"}),
        _finished(ok=False, step_id="Only"),
    ])))
    snap = _read_run(tmp_path, resolved)
    assert snap["status"] == "failed"
    assert snap["awaiting"] is None
    assert snap["error"] == "boom"

    next_run, started_next = journal.resolve_run_id("sess", "Only")
    assert started_next is True and next_run != resolved


def test_lazy_service_package_imports() -> None:
    core_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {core_root!r}); "
                "from flowx_core.service.run_journal import RunJournal; "
                "from flowx_core.service.media import media_space; "
                "assert 'flowx_core.service.manager' not in sys.modules; "
                "assert 'flowx_core.builder' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
