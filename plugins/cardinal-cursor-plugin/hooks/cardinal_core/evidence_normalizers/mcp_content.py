"""mcp-content: an MCP tool result ({content, structuredContent}, a list of
content blocks, the result text, Claude Code's spill notice).

Matches a call whose source is an MCP server, or a result that has exactly the
MCP result shape: a dict whose keys are all MCP result keys and whose
`content` is a string or a list of blocks. A built-in tool's result that
merely has a `content` key (Claude Code's Write: {type, filePath, content,
structuredPatch, ...}) is NOT read as MCP content.
"""

from __future__ import annotations

from pathlib import Path

from . import Normalized, Normalizer, ToolCall, input_summary, register

MCP_RESULT_KEYS = frozenset(("content", "structuredContent", "isError", "_meta"))


def _is_block(b) -> bool:
    return isinstance(b, dict) and isinstance(b.get("type"), str)


def mcp_shaped(r) -> bool:
    if isinstance(r, dict):
        if not r or not set(r).issubset(MCP_RESULT_KEYS):
            return False
        c = r.get("content")
        if "content" in r and not (isinstance(c, str) or (isinstance(c, list) and all(_is_block(b) or isinstance(b, str)
                                                                                     for b in c))):
            return False
        return "content" in r or "structuredContent" in r
    if isinstance(r, list):
        return bool(r) and all(_is_block(b) for b in r)
    return False


def match(call: ToolCall) -> bool:
    kind = call.source.get("kind") if isinstance(call.source, dict) else None
    return kind == "mcp" or mcp_shaped(call.response)


def normalize(call: ToolCall) -> Normalized:
    from .. import evidence

    spill = Path(call.spill_root) if call.spill_root else None
    failed = call.error is not None
    response = call.error if failed and call.response is None else call.response
    body = evidence.normalize(response, spill, structure_json=not failed)
    out = Normalized(summary=input_summary(call.tool_input), is_error=failed)
    if isinstance(call.response, dict) and call.response.get("isError") is True:
        out.is_error = True
    if "structured" in body:
        out.structured = body["structured"]
        out.has_structured = True
    out.text = list(body.get("text") or [])
    out.other_blocks = int(body.get("other_blocks") or 0)
    if body.get("spilled"):
        out.spilled_bytes = body.get("spilled_bytes")
    return out


register(Normalizer("mcp-content", match, normalize))
