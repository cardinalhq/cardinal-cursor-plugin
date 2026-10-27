"""Hook-side visibility for investigation writes that could not reach Cardinal."""

from __future__ import annotations
import contextlib
import json
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from . import evidence_capture as capture
from . import evidence
from . import investigation_events as ie

MARKER = "NOT RECORDED —"
THROTTLE = 600
WRITE_SUBCOMMANDS = {"checkpoint", "question", "post", "ack"}
CODE = re.compile(r"\A(?:Exit code:?|Process exited with code)\s+(-?\d+)(?:\s|$)")
SHELL_TOOLS = {
    "claude": {"Bash"},
    "codex": {"Bash", "exec", "exec_command", "functions.exec_command"},
    "cursor": {"Shell", "run_terminal_cmd"},
    "gemini": {"run_shell_command"},
    "copilot": {"bash", "powershell"},
}


def write_command(command):
    """Recognize executable command words, never quoted prose or file contents.

    Split only unquoted shell separators, then shlex each simple command.
    Shell interpreters with -c and command substitutions are deliberately not
    inspected: their string arguments are not this tool's command words.
    """
    if not isinstance(command, str):
        return False
    # Discard heredoc bodies; the invocation on the first line still counts.
    lines, delimiter = [], None
    for line in command.splitlines(keepends=True):
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        lines.append(line)
        match = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line)
        if match:
            delimiter = match.group(2)
    command = "".join(lines)
    segments, start, quote, escaped, comment, depth = [], 0, None, False, False, 0
    for i, char in enumerate(command):
        if comment:
            if char == "\n":
                segments.append(command[start:i])
                start, comment = i + 1, False
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (i == 0 or command[i - 1].isspace()):
            comment = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char in ";&|\n" and depth == 0:
            segments.append(command[start:i])
            start = i + 1
    segments.append(command[start:])
    for segment in segments:
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            continue
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
        if words and words[0] in ("env", "command", "exec"):
            words.pop(0)
            while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
                words.pop(0)
        if words and re.fullmatch(r"python(?:[23](?:\.\d+)?)?", Path(words[0]).name):
            words.pop(0)
            while words and words[0] in ("-I", "-u", "-B"):
                words.pop(0)
        if (
            len(words) >= 3
            and Path(words[0]).name == "cardinal-storyboard"
            and words[1] == "investigation"
            and words[2] in WRITE_SUBCOMMANDS
            and not any(word in ("--help", "-h") for word in words[3:])
        ):
            return True
    return False


def strings(value, depth=0):
    if depth > 12:
        return []
    if isinstance(value, str):
        return [value[:100000]]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v, depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for v in value[:100] for s in strings(v, depth + 1)]
    return []


def exit_code(value, *, prefix=True):
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode", "returncode", "return_code"):
            if isinstance(value.get(key), int) and not isinstance(value[key], bool):
                return value[key]
        # stdout/stderr are data, not runtime status prefixes.
        for key, item in value.items():
            if key not in ("stdout", "stderr", "text"):
                code = exit_code(item, prefix=key in ("output", "tool_response", "toolResponse"))
                if code is not None:
                    return code
    elif isinstance(value, str) and prefix:
        match = CODE.match(value)
        if match:
            return int(match.group(1))
    return None


def path(home: Path, sid: str) -> Path:
    return ie.sessions_dir(home) / f"{sid}.failures.json"


def observe(home, sid, tool_input, response=None, error=None, *, runtime=None, tool_name=None, now=None, succeeded=None):
    """Detect before capture's opt-out/control-plane early returns. No network.

    Only reachability failures (3 or the fixed marker) count; usage/refusal
    errors do not. A successful write clears the state. `succeeded`: the
    runtime's own success signal when its result carries no exit code (Claude
    reports a non-zero Bash exit as PostToolUseFailure, so a non-interrupted
    PostToolUse means exit 0). Returns a throttled
    additionalContext message, independent of evidence context settings.
    """
    runtime = "claude" if runtime == "claude-code" else runtime
    if (
        tool_name not in SHELL_TOOLS.get(runtime, set())
        or not ie.valid_session(sid)
        or not isinstance(tool_input, dict)
        or not capture.control_plane(tool_input)
    ):
        return None
    command = (
        tool_input.get("command") if runtime == "claude" else tool_input.get("cmd", tool_input.get("command"))
    )
    if not write_command(command):
        return None
    text = "\n".join(strings(response) + strings(error))
    code = exit_code(response)
    if code is None:
        code = exit_code(error)
    if code is None and succeeded is True and not error:
        code = 0
    marker = next((line for line in text.splitlines() if line.startswith(MARKER)), None)
    failed = code == 3 or marker is not None
    success = not failed and not error and code == 0
    if not failed and not success:
        return None
    now = time.time() if now is None else now
    with ie.locked(home, sid) as locked:
        if not locked:
            return None
        if success:
            with contextlib.suppress(FileNotFoundError):
                path(home, sid).unlink()
            return None
        try:
            state = json.loads(path(home, sid).read_text())
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["count"] = int(state.get("count") or 0) + 1
        state.setdefault("first_at", now)
        state["last_at"] = now
        reason = marker[len(MARKER) :] if marker is not None else ie._one_line(text) or "command exited 3"
        state["last_reason"] = ie._one_line(evidence.scrub_string(reason), 300)
        message = None
        if "notified_at" not in state or now - state["notified_at"] >= THROTTLE:
            since = datetime.fromtimestamp(state["first_at"]).strftime("%H:%M")
            message = (
                f"Cardinal could not record {state['count']} investigation updates since {since} "
                f"({state['last_reason']}). Tell the user now; the investigation record is not current."
            )
            state["notified_at"] = now
        ie.write_json(path(home, sid), state)
        return message
