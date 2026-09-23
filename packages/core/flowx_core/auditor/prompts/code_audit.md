# Code Audit Checklist

Use this checklist to audit PyDaoGraph node files and enforce the two required methods the runtime depends on.

## Rules
- Every node class must implement `def clone(self):` with a docstring containing `Create a copy of this node` and must return `self`.
- Every node class must implement `def run(self) -> CStatus:` and accept only `self`.
- If the file has syntax errors or no classes, the audit fails immediately.

## How to run the automated audit
```python
from auditor.node_auditor import CodeAuditor

auditor = CodeAuditor()
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
