"""file-edit: a change to a file, as a patch.

Matches on shape: a dict carrying `structuredPatch` (Claude Code's Edit,
MultiEdit and Write results). The patch is the evidence of what changed:
structured {file_path, patch, type?, user_modified?, replace_all?,
content?}. The file's previous contents (`originalFile`) are never kept, and
a Write's new `content` only when it is small (the patch alone otherwise).
"""

from __future__ import annotations

from . import Normalized, Normalizer, ToolCall, input_summary, register

MAX_WRITE_CONTENT_BYTES = 32 << 10


def match(call: ToolCall) -> bool:
    r = call.response
    return isinstance(r, dict) and "structuredPatch" in r


def normalize(call: ToolCall) -> Normalized:
    r = call.response
    s = {"patch": r.get("structuredPatch")}
    fp = r.get("filePath", r.get("file_path"))
    if isinstance(fp, str):
        s["file_path"] = fp
    for src, dst in (("type", "type"), ("userModified", "user_modified"), ("replaceAll", "replace_all")):
        if src in r and isinstance(r[src], (str, bool)):
            s[dst] = r[src]
    content = r.get("content")
    if isinstance(content, str) and len(content.encode("utf-8", errors="ignore")) <= MAX_WRITE_CONTENT_BYTES:
        s["content"] = content
    out = Normalized(structured=s, has_structured=True, summary=input_summary(call.tool_input))
    if call.error is not None:
        out.is_error = True
        out.text = [call.error]
    return out


register(Normalizer("file-edit", match, normalize))
