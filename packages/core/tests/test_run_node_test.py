"""Modified 2026-10-14: native-free execution, failure logs and standalone compatibility."""

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from ag_ui_workflow import WorkflowStepNode
from pydaograph import CStatus

from flowx_core.runtime import RunNodeTest
from flowx_core.runtime.run_node_test import _execute_node_test


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

    output = node.process_input("", {}, {})

    assert output.derived["ok"] is True
    assert observed["command"] == ["/custom/python", "-m", "pytest", str(test_path), "-q"]
    assert observed["cwd"] == str(tmp_path)
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == 30
    assert node.result is not None
    assert node.result["ok"] is True
    assert observed["command"] is node.command is node.result["command"] is output.derived["command"]
    assert node.result["status_line"] == "✓ Test completed successfully."
    assert log_path.read_text(encoding="utf-8").endswith("✓ Test completed successfully.\n")


def test_run_node_test_implements_process_input(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_ExampleNode.py"
    log_path = tmp_path / "logs" / "ExampleNode.txt"
    node = RunNodeTest(
        node_name="ExampleNode",
        test_path=test_path,
        log_path=log_path,
        command=["python", "-m", "pytest", str(test_path), "-q"],
        cwd=str(tmp_path),
        timeout=30,
        run_subprocess=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="1 passed\n",
            stderr="",
        ),
        timeout_expired=TimeoutError,
    )

    output = node.process_input("", {}, {})

    assert issubclass(RunNodeTest, WorkflowStepNode)
    assert RunNodeTest.process_input is not WorkflowStepNode.process_input
    assert output.card == {"kind": "node-test", "status": "✓ Test completed successfully."}
    assert output.derived["ok"] is True


@pytest.mark.parametrize("returncode", [0, 3])
def test_plain_execution_preserves_results_log_and_command_identity(tmp_path, returncode):
    test_path = tmp_path / "tests" / "test_Plain.py"
    log_path = tmp_path / "logs" / "Plain.log"
    command = ["/python/plain", "-m", "pytest", str(test_path), "-q"]

    def run(actual_command, **kwargs):
        assert actual_command is command
        assert kwargs == {"cwd": str(tmp_path), "capture_output": True, "text": True, "timeout": 8}
        return SimpleNamespace(returncode=returncode, stdout="pytest output", stderr="pytest stderr")

    result = _execute_node_test(
        node_name="Plain", test_path=test_path, log_path=log_path, command=command,
        cwd=str(tmp_path), timeout=8, run_subprocess=run, timeout_expired=TimeoutError,
    )

    status = "✓ Test completed successfully." if returncode == 0 else "✗ Test failed with return code 3."
    assert result == {
        "node_name": "Plain", "test_path": str(test_path), "log_path": str(log_path),
        "command": command, "cwd": str(tmp_path), "returncode": returncode,
        "stdout": "pytest output", "stderr": "pytest stderr", "ok": returncode == 0,
        "timed_out": False, "status_line": status,
    }
    assert result["command"] is command
    log = log_path.read_text(encoding="utf-8")
    assert log.startswith("=== Node Test Log ===\nNode: Plain\nTest started: ")
    assert "Test ended: " in log
    assert f"Test file: {test_path}\nExecuting: {' '.join(command)}\nWorking directory: {tmp_path}\n" in log
    assert f"Return code: {returncode}\n\n" in log
    assert "--- STDOUT ---\npytest output\n\n" in log
    assert "--- STDERR ---\npytest stderr\n\n" in log
    assert log.endswith(f"{status}\n")


@pytest.mark.parametrize("failure", ["timeout", "exception"])
def test_plain_execution_logs_timeout_and_exception_without_raising(tmp_path, failure):
    command = ["/python/failure", "-m", "pytest", "test_failure.py", "-q"]
    log_path = tmp_path / "logs" / f"{failure}.log"

    def run(actual_command, **kwargs):
        assert actual_command is command
        assert kwargs == {"cwd": str(tmp_path), "capture_output": True, "text": True, "timeout": 4}
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 4, output="partial output", stderr="partial error")
        raise OSError("subprocess unavailable")

    result = _execute_node_test(
        node_name="Failure", test_path=tmp_path / "test_failure.py", log_path=log_path,
        command=command, cwd=str(tmp_path), timeout=4,
        run_subprocess=run, timeout_expired=subprocess.TimeoutExpired,
    )

    stderr = "" if failure == "timeout" else "OSError: subprocess unavailable"
    status = (
        "✗ Test timed out after 4 seconds." if failure == "timeout"
        else "✗ Test raised an exception: OSError: subprocess unavailable"
    )
    assert result["command"] is command
    assert result["ok"] is False
    assert result["returncode"] is None
    assert result["stdout"] == ""
    assert result["stderr"] == stderr
    assert result["timed_out"] is (failure == "timeout")
    assert result["status_line"] == status
    log = log_path.read_text(encoding="utf-8")
    assert "Return code: N/A\n\n" in log
    assert "--- STDOUT ---" not in log
    assert "partial output" not in log and "partial error" not in log
    if stderr:
        assert f"--- STDERR ---\n{stderr}\n\n" in log
    else:
        assert "--- STDERR ---" not in log
    assert log.endswith(f"{status}\n")


@pytest.mark.parametrize("failure", ["returncode", "timeout", "exception"])
def test_standalone_execute_preserves_status_result_and_process_input(tmp_path, failure):
    command = ["python", "-m", "pytest", "test_standalone.py", "-q"]

    def run(actual_command, **_kwargs):
        assert actual_command is command
        if failure == "timeout":
            raise TimeoutError("expired")
        if failure == "exception":
            raise ValueError("broken subprocess")
        return SimpleNamespace(returncode=5, stdout=None, stderr=None)

    node = RunNodeTest(
        node_name="Standalone", test_path=tmp_path / "test_standalone.py",
        log_path=tmp_path / "logs" / "standalone.log", command=command,
        cwd=str(tmp_path), timeout=6, run_subprocess=run, timeout_expired=TimeoutError,
    )
    status = node._execute()
    assert isinstance(status, CStatus)
    assert not status.isErr()
    first_result = node.result
    assert first_result is not None
    assert first_result["command"] is command
    assert first_result["ok"] is False
    assert first_result["returncode"] == (5 if failure == "returncode" else None)
    assert first_result["timed_out"] is (failure == "timeout")
    assert first_result["stdout"] == ""
    assert first_result["stderr"] == ("ValueError: broken subprocess" if failure == "exception" else "")

    output = node.process_input("ignored", {}, {"unused": object()})
    assert node.result is not first_result
    assert output.derived == node.result == first_result
    assert output.derived is not node.result
    assert output.derived["command"] is node.command is command
    assert output.card == {"kind": "node-test", "status": node.result["status_line"]}
    assert node.log_path.read_text(encoding="utf-8").endswith(f"{node.result['status_line']}\n")


@pytest.mark.parametrize("standalone", [False, True])
def test_log_io_errors_still_propagate_without_publishing_a_result(tmp_path, standalone):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    kwargs = {
        "node_name": "Blocked", "test_path": tmp_path / "test_blocked.py",
        "log_path": blocked / "log.txt", "command": ["python"], "cwd": str(tmp_path),
        "timeout": 1, "run_subprocess": lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
        "timeout_expired": TimeoutError,
    }
    if standalone:
        node = RunNodeTest(**kwargs)
        with pytest.raises(FileExistsError):
            node._execute()
        assert node.result is None
    else:
        with pytest.raises(FileExistsError):
            _execute_node_test(**kwargs)