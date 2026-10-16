from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from flowx_core.tools.workflow_logs import WorkflowNodeLogs


def test_node_logs_store_test_and_audit_text_by_step_id() -> None:
	logs = WorkflowNodeLogs()
	logs.record_test("Collect", "pytest passed")
	logs.record_audit("Collect", "passed")
	logs.record_test("Publish", "pytest failed")

	assert logs.test == {"Collect": "pytest passed", "Publish": "pytest failed"}
	assert logs.audit == {"Collect": "passed"}


def test_node_logs_expose_read_only_snapshots() -> None:
	logs = WorkflowNodeLogs()
	logs.record_test("Collect", "first")
	snapshot = logs.test
	logs.record_test("Collect", "second")

	assert snapshot == {"Collect": "first"}
	assert logs.test == {"Collect": "second"}
	with pytest.raises(TypeError):
		snapshot["Collect"] = "mutated"  # type: ignore[index]


def test_node_logs_accept_concurrent_records() -> None:
	logs = WorkflowNodeLogs()
	with ThreadPoolExecutor(max_workers=8) as executor:
		list(executor.map(lambda index: logs.record_audit(f"Node{index}", str(index)), range(50)))

	assert logs.audit == {f"Node{index}": str(index) for index in range(50)}
