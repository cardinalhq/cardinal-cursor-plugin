"""cardinal-agent-core — the Cardinal agent-telemetry contract, written once.

Extracted from the four per-agent plugins (claude/codex/cursor/gemini) per
docs/specs/agent-core.md. Adapters supply agent-specific facts (paths,
event names, payload spellings); this package owns the algorithms and the
OTLP contract.

Design constraints honored throughout (spec §omnigent constraints):
- No module-level path constants — state locations come in via AgentPaths.
- No module-level connection state — emit targets are arguments.
- Identity (user_email/actor) is an argument, never a file read.
"""

CORE_VERSION = "0.2.0"
