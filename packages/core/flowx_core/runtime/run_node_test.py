from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

from pydaograph import CStatus, GNode  # pyright: ignore[reportMissingImports]


class RunNodeTest(GNode):
    """Execute one generated node pytest file and persist its dedicated log."""

    def __init__(
        self,
        *,
        node_name: str,
        test_path: Path,
        log_path: Path,
        command: list[str],
        cwd: str,
        timeout: int,
        run_subprocess: Callable[..., Any],
        timeout_expired: type[BaseException],
    ) -> None:
        super().__init__()
        self.node_name = node_name
        self.test_path = test_path
        self.log_path = log_path
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.run_subprocess = run_subprocess
        self.timeout_expired = timeout_expired
        self.result: Dict[str, Any] | None = None

    def run(self) -> CStatus:
        node_started_at = datetime.now()
        returncode: int | None = None
        stdout_text = ""
        stderr_text = ""
        timed_out = False
        status_line = ""

        try:
            process_result = self.run_subprocess(
                self.command,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            returncode = process_result.returncode
            stdout_text = process_result.stdout or ""
            stderr_text = process_result.stderr or ""
            ok = returncode == 0
            status_line = (
                "✓ Test completed successfully."
                if ok
                else f"✗ Test failed with return code {returncode}."
            )
        except self.timeout_expired:
            ok = False
            timed_out = True
            status_line = f"✗ Test timed out after {self.timeout} seconds."
        except Exception as exc:
            ok = False
            stderr_text = f"{type(exc).__name__}: {exc}"
            status_line = f"✗ Test raised an exception: {stderr_text}"

        node_ended_at = datetime.now()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8") as node_log_file:
            node_log_file.write("=== Node Test Log ===\n")
            node_log_file.write(f"Node: {self.node_name}\n")
            node_log_file.write(f"Test started: {node_started_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
            node_log_file.write(f"Test ended: {node_ended_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
            node_log_file.write(f"Test file: {self.test_path}\n")
            node_log_file.write(f"Executing: {' '.join(self.command)}\n")
            node_log_file.write(f"Working directory: {self.cwd}\n")
            node_log_file.write(
                f"Return code: {returncode if returncode is not None else 'N/A'}\n\n"
            )
            if stdout_text:
                node_log_file.write(f"--- STDOUT ---\n{stdout_text}\n\n")
            if stderr_text:
                node_log_file.write(f"--- STDERR ---\n{stderr_text}\n\n")
            node_log_file.write(f"{status_line}\n")

        self.result = {
            "node_name": self.node_name,
            "test_path": str(self.test_path),
            "log_path": str(self.log_path),
            "command": self.command,
            "cwd": self.cwd,
            "returncode": returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "ok": ok,
            "timed_out": timed_out,
            "status_line": status_line,
        }
        return CStatus()
