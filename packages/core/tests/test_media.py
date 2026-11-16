# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Offline tests for media toolkit, AST card/derived auditing, and run journaling."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from flowx_core.architect.graph import NodeMeta
from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.media import (
    REQUEST_ID_STATE_KEY,
    WORKFLOW_ROOT_STATE_KEY,
    media_space,
    prompt_card,
)
from flowx_core.tools.file_tools import (
    compile_node_file_and_get_derived_keys,
    compile_node_file_and_get_step_output_card_schema,
)
from flowx_core.worker.main_writer import PromptMainFileCoder


def test_media_space_dry_run_image_resize_crop_video_and_blender(tmp_path: Path) -> None:
    state = {
        WORKFLOW_ROOT_STATE_KEY: str(tmp_path),
        REQUEST_ID_STATE_KEY: "req_media_1",
    }
    gen_space = media_space("GenerateImage", state, dry_run=True)
    generated = gen_space.generate_image("A neon skyline at dusk", size="512x512")
    assert generated.kind == "image"
    assert generated.mime == "image/png"
    assert generated.abspath.is_file()
    assert generated.relpath == "runs/req_media_1/media/GenerateImage/generated.png"
    assert generated.width == 512 and generated.height == 512

    resize_space = media_space("ResizeAndCrop", state, dry_run=True)
    resized = resize_space.resize_image(generated.abspath, width=128, height=96, fit="stretch")
    assert resized.width == 128 and resized.height == 96
    cropped = resize_space.crop_image(resized.abspath, left=10, top=8, right=90, bottom=68)
    assert cropped.width == 80 and cropped.height == 60

    vid_space = media_space("GenerateVideo", state, dry_run=True)
    video = vid_space.generate_video("Camera pan across skyline", image=cropped.abspath, duration=2.5, size="320x240")
    assert video.kind == "video"
    assert video.mime == "video/mp4"
    assert video.abspath.is_file()
    assert video.poster_relpath is not None
    assert (tmp_path / video.poster_relpath).is_file()

    cut_space = media_space("TrimVideo", state, dry_run=True)
    trimmed = cut_space.ffmpeg_trim_resize(video.abspath, start=0.1, duration=1.0, width=160, height=120)
    assert trimmed.kind == "video"
    assert trimmed.abspath.is_file()

    blend_space = media_space("BlenderRender", state, dry_run=True)
    rendered = blend_space.blender_render(tmp_path / "scene.py", ext=".png")
    assert rendered.kind == "image"
    assert rendered.abspath.is_file()

    p_card = prompt_card("A neon skyline at dusk")
    assert p_card["kind"] == "prompt" and p_card["prompt"] == "A neon skyline at dusk"


def test_blender_requires_explicit_opt_in_when_not_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLOWX_MEDIA_DRY_RUN", raising=False)
    monkeypatch.delenv("FLOWX_ENABLE_BLENDER", raising=False)
    space = media_space("BlenderNode", {WORKFLOW_ROOT_STATE_KEY: str(tmp_path)}, dry_run=False)
    with pytest.raises(RuntimeError, match="FLOWX_ENABLE_BLENDER=1"):
        space.blender_render(tmp_path / "scene.py")


def test_generated_media_nodes_pass_auditor_and_expose_static_ast_schemas(tmp_path: Path) -> None:
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "PromptInput",
                        "type": "PromptInput",
                        "meta_node_kind": "WorkflowStepNode",
                        "desc": "Collect prompt",
                        "ext_data": {"type": "user_input", "desc": "prompt text"},
                        "enable": True,
                    },
                    {
                        "name": "GeneratePoster",
                        "type": "GeneratePoster",
                        "meta_node_kind": "WorkflowStepNode",
                        "desc": "Generate image",
                        "ext_data": {"type": "url", "desc": "image api"},
                        "depends": ["PromptInput"],
                        "enable": True,
                    },
                    {
                        "name": "AnimateClip",
                        "type": "AnimateClip",
                        "meta_node_kind": "WorkflowStepNode",
                        "desc": "Animate poster into video",
                        "ext_data": {"type": "url", "desc": "video api"},
                        "depends": ["PromptInput", "GeneratePoster"],
                        "enable": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    prompt_src = '''from typing import Any
from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput


class PromptInput(WorkflowStepNode):
    STEP_ID = "PromptInput"
    TITLE = "Prompt"
    PROMPT = "Enter generation prompt"
    DEPENDENCIES = []

    def __init__(self) -> None:
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            val = param.inputs.get(self.STEP_ID)
            ready = isinstance(val, str) and bool(val.strip())
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        text = user_input.strip()
        return StepRunOutput(
            card={"kind": "prompt", "prompt": text, "summary": text},
            derived={"prompt": text},
        )
'''
    poster_src = '''from typing import Any
from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput
from flowx_core.media import media_space


class GeneratePoster(WorkflowStepNode):
    STEP_ID = "GeneratePoster"
    TITLE = "Generate Poster"
    PROMPT = ""
    DEPENDENCIES = ["PromptInput"]
    INPUT_REQUIRED = False

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input
        prompt_text = dependency_results["PromptInput"].derived["prompt"]
        space = media_space(self.STEP_ID, session_state)
        raw_img = space.generate_image(prompt_text, size="512x512")
        resized = space.resize_image(raw_img.abspath, width=256, height=256, fit="stretch")
        return StepRunOutput(
            card={
                "kind": "image",
                "media": resized.relpath,
                "mime": resized.mime,
                "width": resized.width,
                "height": resized.height,
            },
            derived={
                "image_path": str(resized.abspath),
                "width": resized.width,
                "height": resized.height,
            },
        )
'''
    video_src = '''from typing import Any
from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput
from flowx_core.media import media_space


class AnimateClip(WorkflowStepNode):
    STEP_ID = "AnimateClip"
    TITLE = "Animate Video"
    PROMPT = ""
    DEPENDENCIES = ["PromptInput", "GeneratePoster"]
    INPUT_REQUIRED = False

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input
        prompt_text = dependency_results["PromptInput"].derived["prompt"]
        poster_path = dependency_results["GeneratePoster"].derived["image_path"]
        space = media_space(self.STEP_ID, session_state)
        clip = space.generate_video(prompt_text, image=poster_path, duration=1.5, size="320x240")
        cut = space.ffmpeg_trim_resize(clip.abspath, start=0.0, duration=1.0, width=160, height=120)
        return StepRunOutput(
            card={
                "kind": "video",
                "media": cut.relpath,
                "poster": cut.poster_relpath,
                "mime": cut.mime,
                "duration": cut.duration,
            },
            derived={
                "video_path": str(cut.abspath),
                "duration": cut.duration,
            },
        )
'''
    sources = {
        "PromptInput": prompt_src,
        "GeneratePoster": poster_src,
        "AnimateClip": video_src,
    }
    raw_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    metas = {item["name"]: NodeMeta.from_dict(item) for item in raw_graph["nodes"]}
    auditor = NodeAuditor()

    for name, code in sources.items():
        (tmp_path / f"{name}.py").write_text(code, encoding="utf-8")

    for name in ("PromptInput", "GeneratePoster", "AnimateClip"):
        ok, violations = auditor.audit_file(
            str(tmp_path / f"{name}.py"),
            node_meta=metas[name],
            graph_plan_path=str(graph_path),
        )
        assert ok, violations

    assert set(compile_node_file_and_get_derived_keys(str(tmp_path / "GeneratePoster.py"))) == {
        "image_path",
        "width",
        "height",
    }
    poster_schema = compile_node_file_and_get_step_output_card_schema(str(tmp_path / "GeneratePoster.py"))
    assert poster_schema is not None
    assert poster_schema["card"]["kind"] == "image"
    assert "media" in poster_schema["card"]


def test_end_to_end_generated_service_journals_run_and_media_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWX_MEDIA_DRY_RUN", "1")
    monkeypatch.delenv("FLOWX_WORKFLOW_ROOT", raising=False)

    workflow_root = tmp_path / "workflows" / "cinema_flow"
    service_pkg = workflow_root / "service" / f"service_{uuid4().hex[:8]}"
    service_pkg.mkdir(parents=True)
    (workflow_root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "workflow_id": "cinema_flow", "workflow_version": 4, "workflows": [], "nodes": {}}),
        encoding="utf-8",
    )

    graph = {
        "pool_size": 1,
        "nodes": [
            {"name": "PromptInput", "depends": []},
            {"name": "RenderPoster", "depends": ["PromptInput"]},
            {"name": "MakeTrailer", "depends": ["PromptInput", "RenderPoster"]},
        ],
    }
    graph_path = service_pkg / "workflow.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")

    (service_pkg / "PromptInput.py").write_text(
        '''from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput

class PromptInput(WorkflowStepNode):
    STEP_ID = "PromptInput"
    TITLE = "Prompt Input"
    PROMPT = "Describe scene"
    DEPENDENCIES = []

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            val = param.inputs.get(self.STEP_ID)
            ready = isinstance(val, str) and bool(val.strip())
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, user_input, dependency_results, session_state):
        prompt = user_input.strip()
        return StepRunOutput(
            card={"kind": "prompt", "prompt": prompt, "summary": prompt},
            derived={"prompt": prompt},
        )
''',
        encoding="utf-8",
    )
    (service_pkg / "RenderPoster.py").write_text(
        '''from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput
from flowx_core.media import media_space

class RenderPoster(WorkflowStepNode):
    STEP_ID = "RenderPoster"
    TITLE = "Render Poster"
    PROMPT = ""
    DEPENDENCIES = ["PromptInput"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        prompt = dependency_results["PromptInput"].derived["prompt"]
        space = media_space(self.STEP_ID, session_state)
        img = space.generate_image(prompt, size="256x256")
        cut = space.resize_image(img.abspath, width=128, height=128, fit="stretch")
        return StepRunOutput(
            card={"kind": "image", "media": cut.relpath, "mime": cut.mime, "width": cut.width, "height": cut.height},
            derived={"image_path": str(cut.abspath)},
        )
''',
        encoding="utf-8",
    )
    (service_pkg / "MakeTrailer.py").write_text(
        '''from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput
from flowx_core.media import media_space

class MakeTrailer(WorkflowStepNode):
    STEP_ID = "MakeTrailer"
    TITLE = "Make Trailer"
    PROMPT = ""
    DEPENDENCIES = ["PromptInput", "RenderPoster"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        prompt = dependency_results["PromptInput"].derived["prompt"]
        poster = dependency_results["RenderPoster"].derived["image_path"]
        space = media_space(self.STEP_ID, session_state)
        vid = space.generate_video(prompt, image=poster, duration=1.2, size="160x120")
        return StepRunOutput(
            card={"kind": "video", "media": vid.relpath, "poster": vid.poster_relpath, "mime": vid.mime, "duration": vid.duration},
            derived={"video_path": str(vid.abspath)},
        )
''',
        encoding="utf-8",
    )

    main_path = PromptMainFileCoder().write_main_entrypoint(
        project_root_path=str(service_pkg),
        graph_plan_json_path=str(graph_path),
        output_path=str(service_pkg / "main.py"),
    )
    assert MainEntryPointAuditor().audit_file(str(main_path), nodes_root=str(service_pkg)) == (True, [])

    monkeypatch.syspath_prepend(str(service_pkg.parent))
    mod_name = f"media_main_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, mod_name, module)
    spec.loader.exec_module(module)

    try:
        with TestClient(module.app) as client:
            resp = client.post(
                "/api/run-step",
                json={"sessionId": "sess-1", "stepId": "PromptInput", "input": "Cyberpunk harbor in rain"},
            )
            assert resp.status_code == 200
            assert "RUN_FINISHED" in resp.text

            latest = json.loads((workflow_root / "runs" / "latest.json").read_text(encoding="utf-8"))
            assert latest["status"] == "completed"
            assert latest["workflow_version"] == 4
            run_id = latest["run_id"]

            run_record = json.loads((workflow_root / "runs" / run_id / "run.json").read_text(encoding="utf-8"))
            assert run_record["status"] == "completed"
            assert set(run_record["nodes"]) == {"PromptInput", "RenderPoster", "MakeTrailer"}
            assert run_record["nodes"]["PromptInput"]["card"]["kind"] == "prompt"
            img_rel = run_record["nodes"]["RenderPoster"]["card"]["media"]
            vid_rel = run_record["nodes"]["MakeTrailer"]["card"]["media"]
            assert (workflow_root / img_rel).is_file()
            assert (workflow_root / vid_rel).is_file()
    finally:
        for loaded in list(sys.modules):
            if loaded == service_pkg.name or loaded.startswith(service_pkg.name + "."):
                sys.modules.pop(loaded, None)
