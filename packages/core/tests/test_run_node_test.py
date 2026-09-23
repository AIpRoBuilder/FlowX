from pathlib import Path
from types import SimpleNamespace

from flowx_core.runtime import RunNodeTest


def test_run_node_test_executes_pytest_and_writes_its_log(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_ExampleNode.py"
    log_path = tmp_path / "logs" / "ExampleNode.txt"
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=".\n1 passed\n", stderr="")

    node = RunNodeTest(
        node_name="ExampleNode",
        test_path=test_path,
        log_path=log_path,
        command=["/custom/python", "-m", "pytest", str(test_path), "-q"],
        cwd=str(tmp_path),
        timeout=30,
        run_subprocess=fake_run,
        timeout_expired=TimeoutError,
    )

    status = node.run()

    assert status.isOK()
    assert observed["command"] == ["/custom/python", "-m", "pytest", str(test_path), "-q"]
    assert observed["cwd"] == str(tmp_path)
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == 30
    assert node.result is not None
    assert node.result["ok"] is True
    assert node.result["status_line"] == "✓ Test completed successfully."
    assert log_path.read_text(encoding="utf-8").endswith("✓ Test completed successfully.\n")