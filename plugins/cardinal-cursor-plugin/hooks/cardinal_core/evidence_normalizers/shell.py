"""shell: a command's result, whatever the tool is called.

Matches on shape: a dict carrying `stdout` or `stderr` strings (Claude Code's
Bash, Cursor's Shell, a third-party MCP `run_command` returning that shape),
or a failed call whose error text starts with "Exit code N" (Claude Code's
PostToolUseFailure for a non-zero exit).

Output: structured {stdout, stderr, exit_code?, interrupted?,
background_task_id?, timed_out_ms?, return_code_interpretation?}. An exit
code is recorded only when the runtime reported one: a successful call
without one does not claim 0.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import Normalized, Normalizer, ToolCall, input_summary, register

EXIT_CODE_RE = re.compile(r"^Exit code (-?\d+)[ \t]*(?:\r?\n|$)")
_EXIT_KEYS = ("exit_code", "exitCode", "returncode", "returnCode", "status_code")


def _shell_dict(r) -> bool:
    return isinstance(r, dict) and (isinstance(r.get("stdout"), str) or isinstance(r.get("stderr"), str))


def match(call: ToolCall) -> bool:
    if _shell_dict(call.response):
        return True
    return call.response is None and isinstance(call.error, str) and EXIT_CODE_RE.match(call.error) is not None


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,6}", v.strip()):
        return int(v.strip())
    return None


def _persisted(r: dict, spill_root) -> tuple:
    """Claude Code keeps a large command output in a file and hands the hook
    a preview plus `persistedOutputPath`: read it (bounded) when it resolves
    under spill_root. -> (text or None, size or None)."""
    from .. import evidence

    p = r.get("persistedOutputPath")
    if not isinstance(p, str) or not spill_root:
        return None, None
    try:
        root = Path(spill_root).resolve()
        path = Path(p).expanduser()
        if not path.is_absolute():
            return None, None
        path = path.resolve()
        path.relative_to(root)
        if not path.is_file():
            return None, None
        size = path.stat().st_size
        if size > evidence.MAX_SPILL_BYTES:
            return None, None
        with open(path, "rb") as f:
            return f.read(evidence.MAX_SPILL_READ_BYTES).decode("utf-8", errors="ignore"), size
    except (OSError, ValueError, RuntimeError):
        return None, None


def normalize(call: ToolCall) -> Normalized:
    out = Normalized(summary=input_summary(call.tool_input))
    r = call.response
    if not _shell_dict(r):
        m = EXIT_CODE_RE.match(call.error or "")
        out.is_error = True
        out.exit_code = int(m.group(1))
        out.structured = {"exit_code": out.exit_code, "output": (call.error or "")[m.end():]}
        out.has_structured = True
        return out
    s = {"stdout": r.get("stdout") if isinstance(r.get("stdout"), str) else "",
         "stderr": r.get("stderr") if isinstance(r.get("stderr"), str) else ""}
    spilled, size = _persisted(r, call.spill_root)
    if spilled is not None:
        s["stdout"] = spilled
        out.spilled_bytes = size
    for k in _EXIT_KEYS:
        code = _int(r.get(k))
        if code is not None:
            out.exit_code = code
            s["exit_code"] = code
            break
    if r.get("interrupted") is True:
        s["interrupted"] = True
    bg = r.get("backgroundTaskId")
    if isinstance(bg, str) and bg:
        s["background_task_id"] = bg
    t = _int(r.get("timedOutAfterMs"))
    if t is not None:
        s["timed_out_ms"] = t
    interp = r.get("returnCodeInterpretation")
    if isinstance(interp, str) and interp:
        s["return_code_interpretation"] = interp
    if call.error is not None:
        out.is_error = True
        m = EXIT_CODE_RE.match(call.error)
        if m and out.exit_code is None:
            out.exit_code = int(m.group(1))
            s["exit_code"] = out.exit_code
    if out.exit_code is not None and out.exit_code != 0:
        out.is_error = True
    out.structured = s
    out.has_structured = True
    return out


register(Normalizer("shell", match, normalize))
