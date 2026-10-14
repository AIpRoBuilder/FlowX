from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ag_ui_workflow import WorkflowStepNode

from flowx_core import builder_services
from flowx_core.agent_builder import AgentBuilder
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.workflows import node_runtime_modifier
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_generation import NodeGenerationWorkflow


_THREAD_TIMEOUT = 5


class _CacheLockProbe:
    """Signal actual lock contention without sleeps or scheduling assumptions."""

    def __init__(self, lock) -> None:
        self._lock = lock
        self.contended = Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            assert self._lock.acquire(timeout=_THREAD_TIMEOUT), "cache lock was not released"
        return self

    def __exit__(self, *exc_info) -> None:
        self._lock.release()


@pytest.fixture(params=("generation", "modifier"))
def cached_service(request, monkeypatch, tmp_path):
    """Mock eager pool constructors, keeping the real service ownership logic."""
    builder = SimpleNamespace(
        node_planner=object(),
        node_auditor=object(),
        _make_node_coder=Mock(return_value=object()),
        root_dir=str(tmp_path),
        api_key="offline-placeholder",
        model="offline-model",
        provider="openai",
        session_marking_prompt="session marker",
        _run_subprocess=Mock(),
        _subprocess_timeout_expired=Mock(return_value=TimeoutError),
    )
    created = []

    def construct(**kwargs):
        resource = SimpleNamespace(**kwargs, close=Mock())
        created.append(resource)
        return resource

    constructor = Mock(side_effect=construct)
    if request.param == "generation":
        service = builder_services.NodeBuildService(builder)
        monkeypatch.setattr(builder_services, "NodeGenerationWorkflow", constructor)
        monkeypatch.setattr(
            builder_services,
            "resolve_workflow_node_reference",
            lambda **kwargs: SimpleNamespace(meta_node_kind=kwargs["meta_node_kind"]),
        )

        def lookup(workflow_name="default", node_name="WorkflowStepNode"):
            return service._workflow_for(SimpleNamespace(meta_node_kind=node_name, ext_data={}))

        cache = service.workflows
    else:
        service = builder_services.NodeArtifactService(builder)
        monkeypatch.setattr(node_runtime_modifier, "NodeRuntimeModifierPipeline", constructor)
        for name in ("GenerateNodeTestFile", "AmendNodeFromTestLog"):
            monkeypatch.setattr(node_runtime_modifier, name, Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)))
        lookup = service.get_or_create_modifier
        cache = service.modifiers

    return SimpleNamespace(
        kind=request.param,
        builder=builder,
        service=service,
        cache=cache,
        lookup=lookup,
        constructor=constructor,
        construct=construct,
        created=created,
    )


def _observe_cache_lock(service):
    probe = _CacheLockProbe(service._cache_lock)
    service._cache_lock = probe
    return probe


def test_node_generation_reuses_existing_workflow_steps() -> None:
    for step in (NodePlanElement, PromptNodeFileCoderBase, NodeAuditor):
        assert issubclass(step, WorkflowStepNode)
    assert NodeGenerationWorkflow.__annotations__["node_coder"] is not None


def test_cache_ownership_locks_are_per_service() -> None:
    builder = SimpleNamespace()
    services = [
        builder_services.NodeBuildService(builder),
        builder_services.NodeBuildService(builder),
        builder_services.NodeArtifactService(builder),
        builder_services.NodeArtifactService(builder),
    ]
    assert len({id(service._cache_lock) for service in services}) == len(services)


def test_concurrent_first_use_publishes_one_cached_pool(cached_service) -> None:
    lock = _observe_cache_lock(cached_service.service)
    construction_started, release_constructor = Event(), Event()

    def construct(**kwargs):
        resource = cached_service.construct(**kwargs)
        construction_started.set()
        assert release_constructor.wait(_THREAD_TIMEOUT), "constructor was not released"
        return resource

    cached_service.constructor.side_effect = construct
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cached_service.lookup, "default", "WorkflowStepNode")
        try:
            assert construction_started.wait(_THREAD_TIMEOUT)
            second = executor.submit(cached_service.lookup, "default", "WorkflowStepNode")
            assert lock.contended.wait(_THREAD_TIMEOUT)
            assert cached_service.constructor.call_count == 1
        finally:
            release_constructor.set()
        resource = first.result(timeout=_THREAD_TIMEOUT)
        assert second.result(timeout=_THREAD_TIMEOUT) is resource

    assert cached_service.lookup("default", "WorkflowStepNode") is resource
    assert cached_service.created == [resource]
    assert cached_service.constructor.call_count == 1
    cached_service.service.close()
    resource.close.assert_called_once_with()
    assert cached_service.cache == {}


def test_service_close_closes_and_clears_all_owned_pools(cached_service) -> None:
    keys = (
        ("default", "WorkflowStepNode"),
        ("default", "WorkflowFileNode"),
        ("another-workflow", "WorkflowSkillNode"),
    )
    resources = [cached_service.lookup(*key) for key in keys]
    if cached_service.kind == "generation":
        context = cached_service.service.contexts["default"] = object()

    cached_service.service.close()
    cached_service.service.close()
    assert cached_service.cache == {}
    for resource in resources:
        resource.close.assert_called_once_with()
    if cached_service.kind == "generation":
        assert cached_service.service.contexts["default"] is context

    # close() also serves reset_llm_config(); the service can be reused.
    replacement = cached_service.lookup(*keys[0])
    assert replacement is not resources[0]
    assert cached_service.constructor.call_count == len(keys) + 1
    cached_service.service.close()
    replacement.close.assert_called_once_with()
    assert cached_service.cache == {}


def test_close_waits_for_in_progress_cache_creation(cached_service) -> None:
    lock = _observe_cache_lock(cached_service.service)
    construction_started, release_constructor = Event(), Event()

    def construct(**kwargs):
        resource = cached_service.construct(**kwargs)
        construction_started.set()
        assert release_constructor.wait(_THREAD_TIMEOUT), "constructor was not released"
        return resource

    cached_service.constructor.side_effect = construct
    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(cached_service.lookup, "default", "WorkflowStepNode")
        try:
            assert construction_started.wait(_THREAD_TIMEOUT)
            closing = executor.submit(cached_service.service.close)
            assert lock.contended.wait(_THREAD_TIMEOUT)
            cached_service.created[0].close.assert_not_called()
        finally:
            release_constructor.set()
        resource = creation.result(timeout=_THREAD_TIMEOUT)
        closing.result(timeout=_THREAD_TIMEOUT)

    assert cached_service.created == [resource]
    resource.close.assert_called_once_with()
    assert cached_service.cache == {}


def test_lookup_waits_for_in_progress_cache_close(cached_service) -> None:
    original = cached_service.lookup("default", "WorkflowStepNode")
    lock = _observe_cache_lock(cached_service.service)
    close_started, release_close = Event(), Event()

    def close_pool():
        close_started.set()
        assert release_close.wait(_THREAD_TIMEOUT), "pool close was not released"

    original.close.side_effect = close_pool
    with ThreadPoolExecutor(max_workers=2) as executor:
        closing = executor.submit(cached_service.service.close)
        try:
            assert close_started.wait(_THREAD_TIMEOUT)
            creation = executor.submit(cached_service.lookup, "default", "WorkflowStepNode")
            assert lock.contended.wait(_THREAD_TIMEOUT)
            assert cached_service.constructor.call_count == 1
        finally:
            release_close.set()
        closing.result(timeout=_THREAD_TIMEOUT)
        replacement = creation.result(timeout=_THREAD_TIMEOUT)

    assert replacement is not original
    assert cached_service.lookup("default", "WorkflowStepNode") is replacement
    original.close.assert_called_once_with()
    replacement.close.assert_not_called()
    cached_service.service.close()
    replacement.close.assert_called_once_with()
    assert cached_service.cache == {}


def test_constructor_failure_releases_cache_lock_for_retry(cached_service) -> None:
    cached_service.constructor.side_effect = RuntimeError("construction failed")
    with pytest.raises(RuntimeError, match="construction failed"):
        cached_service.lookup("default", "WorkflowStepNode")
    assert cached_service.created == []
    cached_service.constructor.side_effect = cached_service.construct
    resource = cached_service.lookup("default", "WorkflowStepNode")
    assert cached_service.lookup("default", "WorkflowStepNode") is resource
    assert cached_service.constructor.call_count == 2
    cached_service.service.close()
    resource.close.assert_called_once_with()
    assert cached_service.cache == {}


def test_cache_hits_preserve_component_configuration(cached_service) -> None:
    resource = cached_service.lookup("default", "WorkflowStepNode")
    builder = cached_service.builder
    if cached_service.kind == "generation":
        assert resource.node_planner is builder.node_planner
        assert resource.node_coder is builder._make_node_coder.return_value
        assert resource.node_auditor is builder.node_auditor
        builder.node_planner, builder.node_auditor = object(), object()
        assert cached_service.lookup("default", "WorkflowStepNode") is resource
        assert resource.node_planner is builder.node_planner
        assert resource.node_auditor is builder.node_auditor
        builder._make_node_coder.assert_called_once()
    else:
        assert resource.run_subprocess is builder._run_subprocess
        assert resource.timeout_expired is TimeoutError
        assert resource.node_test_writer.root_dir_path == builder.root_dir
        assert resource.node_test_writer.model == resource.node_writer.model == builder.model
        assert resource.node_test_writer.session_marking_prompt == builder.session_marking_prompt
        builder.model = "replacement-model"
        assert cached_service.lookup(" default ", " WorkflowStepNode ") is resource
        assert resource.node_test_writer.model == resource.node_writer.model == "offline-model"
    cached_service.constructor.assert_called_once()
    cached_service.service.close()


def test_agent_builder_close_delegates_to_cache_owners() -> None:
    builder = SimpleNamespace(
        _node_build_service=Mock(),
        _node_artifact_service=Mock(),
    )
    AgentBuilder.close(builder)
    builder._node_build_service.close.assert_called_once_with()
    builder._node_artifact_service.close.assert_called_once_with()