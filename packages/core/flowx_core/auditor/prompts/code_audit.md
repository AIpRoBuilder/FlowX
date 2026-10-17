# Code Audit Checklist

Modified 2026-10-13: audit explicit class-map construction and bytes-processing contracts.

Use this checklist to audit AG-UI workflow subclasses against the installed base-node reference.

## Rules
- Define `STEP_ID`, `TITLE`, `PROMPT`, and `DEPENDENCIES`, matching the graph plan.
- Implement the selected base's required hook and return `StepRunOutput(card=..., derived=...)`: `process_input` for step/file nodes.
- File nodes implement `process_input(file_bytes: bytes, dependency_results, session_state)`; decoding HTTP uploads happens before engine input, and persistence is explicit business logic.
- Keep inherited `run` orchestration. If a custom `run` is required, it accepts only `self` and returns `CStatus`.
- Fresh instances are constructed from an explicit `node_classes` map; neither `@register_class` nor `clone` is required.
- Interactive nodes explicitly install a non-blocking wait callback and report `awaiting_input`/`running` through `WorkflowParam`; `INPUT_REQUIRED` alone does not create a gate.
- Keep request-local mutable state in `session_state`/`WorkflowParam`, not pooled instance fields.
- If the file has syntax errors or no classes, the audit fails immediately.

## How to run the automated audit
```python
from flowx_core.auditor.node_auditor import NodeAuditor

auditor = NodeAuditor()
passed, violations = auditor.audit_node_file("worker/nodes/sample_node.py")

if passed:
    print("Audit passed")
else:
    for v in violations:
        print(f"{v.class_name}:{v.lineno} [{v.rule}] {v.detail}")
```

## Interpreting results
- `passed` is `True` only when no violations remain.
- Each violation includes the class name, rule name, line number, and a short description to fix.
