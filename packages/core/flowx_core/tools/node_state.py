# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""Per-business-node state lookup for steps expanded into one dynamic graph.

The dynamic workflow engine schedules every business node's generation and
repair substeps in one native pipeline.  ``WorkflowParam.state`` is therefore
shared by all of those substeps.  Concrete expanded steps declare ``NODE_KEY``
and ``STATE_KIND`` so the request builders can resolve only their own namespace.
"""

from __future__ import annotations

from typing import Any, Mapping

NAMESPACE_KEY = "dynamic_workflow_namespaces"


def get_namespace(state: Mapping[str, Any], node_name: str) -> Any:
	"""Return the namespace object stored for ``node_name``."""
	namespaces = state.get(NAMESPACE_KEY)
	if not isinstance(namespaces, Mapping):
		raise KeyError("dynamic workflow node namespaces are missing")
	try:
		return namespaces[node_name]
	except KeyError as exc:
		raise KeyError(f"dynamic workflow namespace is missing for node {node_name!r}") from exc


def get_node_state(node: Any) -> Mapping[str, Any]:
	"""Return the request-state mapping a pooled step must read.

	Steps created by the expanded dynamic graph set ``NODE_KEY`` and
	``STATE_KIND``.  Legacy standalone workflows leave both attributes unset and
	continue to use the whole ``WorkflowParam.state``.
	"""
	state = node.get_workflow_param().state
	node_name = getattr(node, "NODE_KEY", "")
	if not node_name:
		return state
	namespace = get_namespace(state, node_name)
	kind = getattr(node, "STATE_KIND", "generation")
	if kind == "repair":
		return namespace.repair_state
	return namespace.generation_state


__all__ = ["NAMESPACE_KEY", "get_namespace", "get_node_state"]
