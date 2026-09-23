# Prompt Asset Library

This directory stores reusable prompt/reference assets shared by code generators.

## Files

- Workflow node base-class metadata is now injected directly from `ag_ui_workflow` base classes at runtime.
- Default consumers: `core/architect/graph_planner.py`, `core/architect/node_planner.py`, `core/demand_analyzer/requirement_disector.py`, and `core/worker/node_writer.py` through `flowx_core.tools.workflow_node_reference`.

## Why this exists

- Keeps shared prompt/reference assets that are still repository-local and not derived from installed runtime packages.

## Update guidance

- If a prompt needs workflow base-node contract details, prefer updating `flowx_core.tools.workflow_node_reference` so all planners/writers/auditors keep using the same injected ag_ui_workflow metadata.
