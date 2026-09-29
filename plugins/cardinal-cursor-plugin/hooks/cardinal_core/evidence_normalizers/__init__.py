"""Pluggable result normalizers for generic evidence capture.

Every tool call an agent makes (a built-in tool, any MCP server's tool, a tool
nobody has heard of yet) goes through ONE capture pipeline
(cardinal_core.evidence_capture.capture_call). What is captured is decided
there, without naming any tool. A normalizer only decides how a result is
*shaped* for citing: it turns a raw tool response into the
{structured, text, other_blocks} body a receipt stores, so a storyboard can
bind a field (`/exit_code`, a `stdout` line) instead of a blob.

Contract (tests: core/tests/test_evidence_capture_call.py):
  - A normalizer never gates. It cannot skip a capture, change the
    sensitivity verdict, or bypass the scrub and cap that run after it.
  - match() is total and cheap. It matches on the result's SHAPE (a dict with
    stdout/stderr, a dict with structuredPatch), or on the call's source kind,
    never on a list of tool names. A tool name may only be a fallback hint
    inside a normalizer.
  - An exception in match() or normalize(), or a return that is not a
    Normalized, falls back to "generic". "generic" always exists and always
    runs last, so a runtime with no normalizer at all still captures
    everything.
  - Output is UNSCRUBBED; the pipeline scrubs and caps it afterwards.

Adding one: drop a module in this package that calls register(Normalizer(...))
and import it at the bottom of this file. build/vendor.py copies the package
as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class ToolCall:
    """One finished tool call, as an adapter hook saw it. Raw: nothing here
    has been scrubbed."""

    runtime: str                  # "claude-code" | "cursor" | "gemini" | "codex" | "pi" | "opencode" | ...
    tool_name: str                # the runtime's name for the tool, raw
    source: dict                  # {"kind": "mcp"|"builtin"|"tool"|"cardinal", "server"?: str, "runtime": str}
    tool: str                     # short name (MCP: the tool part; else tool_name)
    tool_input: Any = None        # any JSON value, not only a dict
    response: Any = None          # the runtime's result value, or None
    error: Optional[str] = None   # failure text, for a failed call
    session_id: Optional[str] = None
    tool_use_id: Optional[str] = None
    cwd: Optional[str] = None
    spill_root: Optional[str] = None   # a dir whose files a result may point at (Claude: ~/.claude/projects)
    client: str = ""              # "claude-code/0.36.0"
    called_at: Optional[str] = None


@dataclass
class Normalized:
    """A normalizer's output, UNSCRUBBED."""

    structured: Any = None        # bindable JSON
    text: list = field(default_factory=list)
    other_blocks: int = 0
    is_error: bool = False
    exit_code: Optional[int] = None
    summary: Optional[str] = None
    spilled_bytes: Optional[int] = None
    has_structured: bool = False  # structured was set (it may legitimately be null)


@dataclass(frozen=True)
class Normalizer:
    id: str
    match: Callable[[ToolCall], bool]
    normalize: Callable[[ToolCall], Normalized]


REGISTRY: list = []

GENERIC = "generic"

MAX_SUMMARY_CHARS = 120


def register(n: Normalizer) -> None:
    """Add a normalizer (replacing one with the same id). Order is
    registration order; "generic" is implicit and always last."""
    if not isinstance(n, Normalizer) or not n.id or n.id == GENERIC:
        raise ValueError("bad normalizer")
    for i, old in enumerate(REGISTRY):
        if old.id == n.id:
            REGISTRY[i] = n
            return
    REGISTRY.append(n)


def one_line(s: Any, n: int = MAX_SUMMARY_CHARS) -> Optional[str]:
    """A string squeezed to one line of at most n characters, or None."""
    if not isinstance(s, str):
        return None
    t = " ".join(s.split())
    if not t:
        return None
    return t if len(t) <= n else t[: n - 1] + "…"


def input_summary(tool_input: Any) -> Optional[str]:
    """A short description of a call from its input, whatever the tool: the
    first short string among the input's top-level values (a command, a
    path, a pattern, a URL, a query), else None. Shape-based: it looks at
    values, never at the tool's name."""
    if isinstance(tool_input, str):
        return one_line(tool_input)
    if not isinstance(tool_input, dict):
        return None
    preferred = ("command", "cmd", "file_path", "filePath", "path", "pattern", "url", "query", "description")
    for k in preferred:
        s = one_line(tool_input.get(k))
        if s:
            return s
    for v in list(tool_input.values())[:16]:
        if isinstance(v, str) and len(v) <= 4096 and "\n" not in v.strip():
            s = one_line(v)
            if s:
                return s
    return None


def _generic(call: ToolCall) -> Normalized:
    """Any result: a dict or list is kept as structured JSON; a string is
    kept as text (a JSON object/array text becomes structured; Claude Code's
    spill notice is followed under spill_root); None is an empty text; a
    failure is its error text."""
    from .. import evidence

    spill = Path(call.spill_root) if call.spill_root else None
    out = Normalized(summary=input_summary(call.tool_input))
    if call.error is not None and call.response is None:
        out.is_error = True
        out.text = [call.error]
        return out
    r = call.response
    if isinstance(r, (dict, list)):
        out.structured = r
        out.has_structured = True
    elif isinstance(r, str):
        body = evidence.normalize(r, spill, structure_json=call.error is None)
        if "structured" in body:
            out.structured = body["structured"]
            out.has_structured = True
        out.text = list(body.get("text") or [])
        if body.get("spilled"):
            out.spilled_bytes = body.get("spilled_bytes")
    elif r is None:
        out.text = [""]
    else:
        out.structured = r
        out.has_structured = True
    if call.error is not None:
        out.is_error = True
        out.text.append(call.error)
    return out


def normalize_call(call: ToolCall) -> tuple:
    """-> (Normalized, normalizer id). The first registered normalizer whose
    match() is true shapes the result; any failure falls back to generic.
    Never raises."""
    for n in list(REGISTRY):
        try:
            if not n.match(call):
                continue
        except Exception:
            continue
        try:
            out = n.normalize(call)
        except Exception:
            break
        if isinstance(out, Normalized):
            return out, n.id
        break
    try:
        return _generic(call), GENERIC
    except Exception:
        text = call.error if isinstance(call.error, str) else ""
        return Normalized(text=[text], is_error=call.error is not None), GENERIC


# The shipped normalizers register themselves on import.
from . import mcp_content, shell, file_edit  # noqa: E402,F401
