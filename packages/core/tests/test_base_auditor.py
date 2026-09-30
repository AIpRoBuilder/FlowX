from pathlib import Path

import pytest

from flowx_core.auditor import (
    BaseAuditor,
    ContextAuditor,
    MainEntryPointAuditor,
    NodeAuditor,
    OutputAuditor,
    RuleViolation,
)


@pytest.mark.parametrize(
    ("auditor_type", "legacy_method", "source"),
    [
        (BaseAuditor, "audit_base_file", "class Example:\n    def run(self):\n        self.missing()\n"),
        (ContextAuditor, "audit_context_file", "class Example:\n    pass\n"),
        (NodeAuditor, "audit_node_file", "class Example:\n    pass\n"),
        (MainEntryPointAuditor, "audit_main_entrypoint_file", "class Example:\n    pass\n"),
        (OutputAuditor, "audit_log_file", '--- STDERR ---\n  File "example.py", line 3\nValueError: bad\n'),
    ],
)
def test_audit_file_preserves_legacy_audit(auditor_type, legacy_method, source, tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text(source, encoding="utf-8")
    auditor = auditor_type()

    assert auditor.audit_file(str(path)) == getattr(auditor, legacy_method)(str(path))


def test_process_input_serializes_violations_and_accepts_user_input(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text("class Example:\n    def run(self):\n        self.missing()\n", encoding="utf-8")

    result = BaseAuditor().process_input(str(path), {}, {})

    assert result.card == {"kind": "audit", "status": "failed"}
    assert result.derived == {
        "file_path": str(path),
        "ok": False,
        "violations": [{
            "class_name": "Example",
            "rule": "undefined_self_call",
            "detail": "self.missing() is called but missing is not defined in the class.",
            "lineno": 3,
        }],
    }


def test_process_input_uses_subclass_audit_file_and_request_options(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "node.py"
    path.write_text("class Example:\n    pass\n", encoding="utf-8")
    auditor = NodeAuditor()
    violation = RuleViolation("Example", "test", "failed", 1)
    received = []

    def audit(file_path, node_meta=None, graph_plan_path=None):
        received.append((file_path, node_meta, graph_plan_path))
        return False, [violation]

    monkeypatch.setattr(auditor, "audit_file", audit)
    result = auditor.process_input("ignored", {}, {"auditor": {
        "file_path": str(path), "node_meta": None, "graph_plan_path": "graph.json",
    }})

    assert received == [(str(path), None, "graph.json")]
    assert result.derived["violations"] == [
        {"class_name": "Example", "rule": "test", "detail": "failed", "lineno": 1}
    ]


def test_process_input_reports_success(tmp_path: Path) -> None:
    path = tmp_path / "good.py"
    path.write_text("class Example:\n    def run(self):\n        return 1\n", encoding="utf-8")

    result = BaseAuditor().process_input("", {}, {"auditor": {"file_path": str(path)}})

    assert result.card["status"] == "passed"
    assert result.derived == {"file_path": str(path), "ok": True, "violations": []}


@pytest.mark.parametrize("invalid_value", [None, "file.py", []])
def test_process_input_rejects_invalid_request(invalid_value) -> None:
    with pytest.raises(ValueError, match="must be a dictionary"):
        BaseAuditor().process_input("file.py", {}, {"auditor": invalid_value})


def test_process_input_requires_file_path() -> None:
    with pytest.raises(ValueError, match="non-empty file_path"):
        BaseAuditor().process_input("", {}, {})