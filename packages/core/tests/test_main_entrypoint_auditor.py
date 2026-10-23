# Modified 2026-10-13: regressions for native JSON/configured pools and lifecycle auditing.
from pathlib import Path

import pytest

from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor


def _main_source() -> str:
    template = Path(__file__).resolve().parents[1] / "flowx_core/worker/templates/pydaograph_main.py.tmpl"
    source = template.read_text(encoding="utf-8")
    replacements = {
        "__NODES_PACKAGE_NAME__": "example_nodes",
        "__NODE_IMPORTS__": "    Ask,",
        "__NODE_CLASS_ITEMS__": "    Ask,",
        "__STEP_CHAIN_ITEMS__": "    Ask.step_meta(),",
        "__APP_NAME__": "example",
        "__PROJECT_ROOT_EXPR__": "_HERE.parent",
        "__PIPELINE_JSON_EXPR__": '_HERE / "workflow.json"',
        "__UVICORN_HOST__": '"0.0.0.0"',
        "__UVICORN_PORT__": "8000",
        "__UVICORN_RELOAD__": "False",
    }
    for placeholder, value in replacements.items():
        source = source.replace(placeholder, value)
    compile(source, "main.py", "exec")
    return source


def _write_main_file(tmp_path, *, include_uvicorn_launcher: bool = True, source: str | None = None) -> str:
    main_code = source if source is not None else _main_source()
    if not include_uvicorn_launcher:
        main_code = main_code.split('if __name__ == "__main__":')[0]
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


@pytest.mark.parametrize("old,new,rule", [
    ("graph_config_id=GRAPH_CONFIG_ID,", "pipeline_json_path=str(PIPELINE_JSON_PATH),", "workflow_engine_constructor_invalid"),
    ("steps_meta=STEP_CHAIN,", "steps_meta=STEP_CHAIN, thread_id='old',", "workflow_engine_constructor_invalid"),
    ("pool=PIPELINE_POOL,", "pool=PIPELINE_POOL, session=object(),", "workflow_engine_constructor_invalid"),
    ("engine.events(request_id, {step_id: normalized_input})", "engine._run_step_events(step_id, normalized_input)", "legacy_workflow_api_forbidden"),
    ("engine.close(request_id)", "engine.reset_session()", "legacy_workflow_api_forbidden"),
    ("threadId=GRAPH_CONFIG_ID", "threadId=engine.thread_id", "legacy_workflow_api_forbidden"),
    ("runId=new_request_id", "runId=engine.session.run_id", "legacy_workflow_api_forbidden"),
    ("engine.events(request_id, {step_id: normalized_input})", "engine.events(step_id, normalized_input)", "run_step_events_arguments_invalid"),
    ("engine.close(request_id)", "engine.close()", "reset_session_close_arguments_invalid"),
    ("engine.close(request_id)", "engine.close(None)", "reset_session_close_arguments_invalid"),
    ("SESSION_REQUESTS[payload.sessionId] = new_request_id", "pass", "reset_session_request_rotation_missing"),
    ("lifespan=lifespan", "", "workflow_lifespan_missing"),
    ("pipeline_json=_build_pipeline_json(),", "pipeline_json_path=str(PIPELINE_JSON_PATH),", "workflow_pool_constructor_invalid"),
    ("pipeline_json=_build_pipeline_json(),", "nodes=_ordered_node_classes(),", "workflow_pool_constructor_invalid"),
    ("pipeline_json=_build_pipeline_json(),", "pipelines=[],", "workflow_pool_constructor_invalid"),
    ("node_classes=NODE_CLASSES,", "", "workflow_pool_constructor_invalid"),
    ("node_classes=NODE_CLASSES,", "node_classes=None,", "workflow_pool_class_map_required"),
    ("pipeline_json=_build_pipeline_json(),", "pipeline_json=str(PIPELINE_JSON_PATH),", "workflow_pool_json_content_required"),
    ("pipeline_json=_build_pipeline_json(),", "pipeline_json='workflow.json',", "workflow_pool_json_content_required"),
    ("pipeline_json=_build_pipeline_json(),", "pipeline_json=PIPELINE_JSON_PATH.read_text(encoding='utf-8'),", "workflow_pool_planning_config_forbidden"),
    ("pipeline_json=_build_pipeline_json(),", "pipeline_json=json.dumps(workflow_config),", "workflow_pool_planning_config_forbidden"),
    ('size=workflow_config["pool_size"],', "size=4,", "workflow_pool_size_config_required"),
    ('size=workflow_config["pool_size"],', 'size=int(os.getenv("FLOWX_WORKFLOW_POOL_SIZE", "4")),', "workflow_pool_env_size_forbidden"),
])
def test_main_entrypoint_auditor_rejects_old_or_incorrect_api(tmp_path, old, new, rule):
    source = _main_source()
    assert old in source, "the invalid-API mutation must still match the generated template"
    source = source.replace(old, new)
    path = _write_main_file(tmp_path, source=source)

    ok, violations = MainEntryPointAuditor().audit_file(path)

    assert not ok
    assert rule in {violation.rule for violation in violations}


def test_main_entrypoint_auditor_accepts_json_content_pool(tmp_path):
    native_graph = {"nodes": [{"name": "ask", "type": "Ask", "depends": [], "meta_type": "GNode", "loop": 1}]}
    source = _main_source().replace("pipeline_json=_build_pipeline_json(),", f"pipeline_json=json.dumps({native_graph!r}),")
    ok, violations = MainEntryPointAuditor().audit_file(_write_main_file(tmp_path, source=source))
    assert ok, violations


def test_main_entrypoint_auditor_rejects_aliased_old_constructor(tmp_path):
    source = _main_source().replace(" WorkflowEngine,", " WorkflowEngine as Engine,")
    source = source.replace("ENGINE = WorkflowEngine(", "ENGINE = Engine(thread_id='old',")
    ok, violations = MainEntryPointAuditor().audit_file(_write_main_file(tmp_path, source=source))
    assert not ok
    assert any(item.rule == "workflow_engine_constructor_invalid" for item in violations)


@pytest.mark.parametrize("source,rule", [
    (_main_source().replace("ENGINE.close", "PIPELINE_POOL.close", 1).replace("run_in_threadpool(PIPELINE_POOL.close)\n            ENGINE", "run_in_threadpool(ENGINE.close)\n            ENGINE"), "workflow_shutdown_order_invalid"),
    (_main_source().replace("await run_in_threadpool(ENGINE.close)", "pass"), "workflow_shutdown_missing"),
    (_main_source() + "\ndef bad_reset():\n    PIPELINE_POOL.close()\n", "workflow_pool_close_lifetime_invalid"),
    (_main_source() + "\ndef bad_helper(workflow_config):\n    return WorkflowPipelinePool(pipeline_json=_build_pipeline_json(), size=workflow_config['pool_size'])\n", "workflow_constructor_count_invalid"),
    (_main_source() + "\ndef bad_helper():\n    return WorkflowEngine(graph_config_id=GRAPH_CONFIG_ID, steps_meta=STEP_CHAIN, pool=PIPELINE_POOL)\n", "workflow_constructor_lifetime_invalid"),
    (_main_source() + "\ndef bad_helper(node):\n    node.setWaitFunc(lambda: True)\n", "entrypoint_wait_gate_forbidden"),
])
def test_main_entrypoint_auditor_enforces_runtime_ownership(tmp_path, source, rule):
    ok, violations = MainEntryPointAuditor().audit_file(_write_main_file(tmp_path, source=source))
    assert not ok
    assert rule in {item.rule for item in violations}