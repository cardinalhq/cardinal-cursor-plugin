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

CORE_VERSION = "0.5.0"

# A Cardinal host whose certificate the machine does not trust (self-hosted,
# self-signed) is pinned by cardinal-connect under ~/.cardinal/tls; every
# module here that talks HTTP imports cardinal_core.tls, which installs a
# urllib opener verifying a pinned host against its pin (nothing pinned: the
# opener is urllib's own). Not imported here: a hook's fast path stays light.
