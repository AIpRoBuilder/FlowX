from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from flowx_core.auditor.base_auditor import BaseAuditor
from flowx_core.auditor.data import RuleViolation


class OutputAuditor(BaseAuditor):
    """Inspect a test log for errors emitted to stderr.

    The ProjectCoder writes logs with a special
    ``--- STDERR ---`` section when the generated main entrypoint fails.
    ``OutputAuditor`` looks for that marker, then scans the following
    text for:
    - Python traceback file paths (``File "..."`` entries), and
    - argparse-style CLI failures (``<prog>: error: ...``).
    - pybind/libc++abi crash signatures with ``At: <file>(<line>): ...``.

    Each unique error source is reported as a ``RuleViolation``.
    """

    def audit_log_file(self, log_path: str) -> Tuple[bool, List[RuleViolation]]:
        """Return ``(ok, violations)`` for a log file.

        Args:
            log_path: Path to the log file produced by
                :py:meth:`ProjectCoder.test_main_entrypoint`.

        If the log contains no ``--- STDERR ---`` section the audit passes
        automatically.  Otherwise traceback entries and argparse-style
        failures are converted into ``RuleViolation`` objects.
        """

        path = Path(log_path)
        if not path.is_file():
            raise FileNotFoundError(f"Log file not found: {path}")

        text = path.read_text(encoding="utf-8")

        try:
            _, rest = text.split("--- STDERR ---", 1)
        except ValueError:
            # no stderr section -> nothing to report
            return True, []

        # only keep content until the next section marker (e.g. --- STDOUT ---)
        rest = rest.split("---", 1)[0]
        violations: List[RuleViolation] = []

        # pattern matches the typical Python traceback "File "/path.py", line 123".
        pattern = re.compile(r'File ["\'](?P<file>[^"\']+)["\'], line (?P<lineno>\d+)')

        seen: set[str] = set()
        lines = rest.splitlines()
        for idx, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            fname = match.group("file")
            if fname in seen:
                continue
            seen.add(fname)
            lineno = int(match.group("lineno"))

            # try to capture a more specific error message appearing below the
            # traceback entry.  Typical Python tracebacks include several
            # additional lines after the "File ..." line: the source code
            # snippet, a caret pointing at the location, and finally the
            # actual exception text (e.g. ``SyntaxError: ...``).  We look for
            # the first subsequent non-blank line that contains a colon and is
            # not another "File ..." line; if we find one we use it as the
            # detail.  Otherwise fall back to the generic ``stderr_file`` label
            # for compatibility with earlier behaviour.
            detail = "stderr_file"
            for extra in lines[idx + 1 :]:
                stripped = extra.strip()
                if not stripped:
                    continue
                if stripped.startswith("File "):
                    # reached next traceback entry; stop looking
                    break
                # choose the first line that looks like an error message
                if ":" in stripped:
                    detail = stripped
                    break

            violations.append(
                RuleViolation(
                    class_name="(log)",
                    rule=fname,
                    detail=detail,
                    lineno=lineno,
                )
            )

        # pybind/libc++abi crashes may surface as:
        # libc++abi: terminating due to uncaught exception of type pybind11::error_already_set: ...
        # At:
        #   /path/to/file.py(57): run
        pybind_header_pattern = re.compile(
            r'^libc\+\+abi: terminating due to uncaught exception of type (?P<etype>[^:]+): (?P<msg>.+)$',
            re.MULTILINE,
        )
        at_frame_pattern = re.compile(
            r'^\s*At:\s*(?P<file>.+?)\((?P<lineno>\d+)\):\s*(?P<context>.+)$',
            re.MULTILINE,
        )

        pybind_details: List[str] = []
        for match in pybind_header_pattern.finditer(rest):
            pybind_details.append(
                f"{match.group('etype').strip()}: {match.group('msg').strip()}"
            )

        if pybind_details:
            pybind_detail = pybind_details[-1]
            for match in at_frame_pattern.finditer(rest):
                fname = match.group("file").strip()
                if not fname or fname in seen:
                    continue
                seen.add(fname)

                lineno = int(match.group("lineno"))
                context = match.group("context").strip()
                detail = pybind_detail
                if context:
                    detail = f"{pybind_detail} | frame: {context}"

                violations.append(
                    RuleViolation(
                        class_name="(log)",
                        rule=fname,
                        detail=detail,
                        lineno=lineno,
                    )
                )

        # argparse errors may not include traceback entries, e.g.:
        # usage: main_entrypoint.py ...
        # main_entrypoint.py: error: the following arguments are required: --graph, --nodes-root
        argparse_pattern = re.compile(r'^(?P<prog>[^:\n]+): error: (?P<msg>.+)$', re.MULTILINE)
        for match in argparse_pattern.finditer(rest):
            prog = match.group("prog").strip()
            msg = match.group("msg").strip()

            if not prog:
                continue
            if prog in seen:
                continue
            seen.add(prog)

            violations.append(
                RuleViolation(
                    class_name="(log)",
                    rule=prog,
                    detail=msg,
                    lineno=1,
                )
            )

        return len(violations) == 0, violations
