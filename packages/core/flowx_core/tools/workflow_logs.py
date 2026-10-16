# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""Per-run node log text keyed by workflow step_id."""

from __future__ import annotations

from threading import Lock
from types import MappingProxyType
from typing import Mapping


class WorkflowNodeLogs:
	"""Thread-safe runtime-modifier test logs and audit logs, as text keyed by node step_id."""

	def __init__(self) -> None:
		self._lock = Lock()
		self._test: dict[str, str] = {}
		self._audit: dict[str, str] = {}

	def record_test(self, step_id: str, text: str) -> None:
		with self._lock:
			self._test[step_id] = text

	def record_audit(self, step_id: str, text: str) -> None:
		with self._lock:
			self._audit[step_id] = text

	@property
	def test(self) -> Mapping[str, str]:
		with self._lock:
			return MappingProxyType(dict(self._test))

	@property
	def audit(self) -> Mapping[str, str]:
		with self._lock:
			return MappingProxyType(dict(self._audit))


__all__ = ["WorkflowNodeLogs"]
