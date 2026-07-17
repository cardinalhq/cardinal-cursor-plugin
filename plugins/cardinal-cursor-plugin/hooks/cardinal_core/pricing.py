"""Plugin-side cost computation for providers that do not emit cost natively.

Claude Code emits cost_usd itself. Codex (OpenAI), Gemini — and omnigent
sessions on harnesses whose server did not maintain a cost total — need
plugin-computed cost, otherwise their sessions land at $0 and disappear
from the Outcomes Dashboard's spend-headed views.

USD per 1M tokens, per each provider's public pricing page. Lookup is
exact-match first, then longest-prefix so dated SKUs (e.g.
`gpt-5-codex-2026-03-01`, `claude-opus-4-5-20251101`) still price
correctly. Keep tables in sync with the pricing pages.

Billing semantics:
- OpenAI/Gemini tables (no `cache_write` key): `input_tokens` is the
  total input count; `cached_input_tokens` is a SUBSET billed at the
  cached rate.
- Anthropic table (`cache_write` key present): `input_tokens`,
  `cached_input_tokens` (cache reads), and `cache_creation_tokens`
  (cache writes, 1.25x input for the default 5-minute TTL) are DISJOINT
  buckets, matching Anthropic usage payloads.
- `thought_tokens` (reasoning) bill as output. OpenAI usage payloads have
  no thought bucket (reasoning is already inside output_tokens) — the key
  is simply absent and contributes 0.
"""

from __future__ import annotations

from typing import Any

OPENAI_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gpt-5":         {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-codex":   {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini":    {"input": 0.25, "cached_input": 0.025, "output":  2.00},
    "gpt-5-nano":    {"input": 0.05, "cached_input": 0.005, "output":  0.40},
    "o3":            {"input": 2.00, "cached_input": 0.500, "output":  8.00},
    "o3-mini":       {"input": 1.10, "cached_input": 0.550, "output":  4.40},
    "o4-mini":       {"input": 1.10, "cached_input": 0.275, "output":  4.40},
}

GEMINI_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gemini-2.0-pro":        {"input": 1.25,  "cached_input": 0.3125,  "output": 10.00},
    "gemini-2.0-flash":      {"input": 0.10,  "cached_input": 0.025,   "output":  0.40},
    "gemini-2.0-flash-lite": {"input": 0.075, "cached_input": 0.01875, "output":  0.30},
    "gemini-1.5-pro":        {"input": 1.25,  "cached_input": 0.3125,  "output":  5.00},
    "gemini-1.5-flash":      {"input": 0.075, "cached_input": 0.01875, "output":  0.30},
    "gemini-1.5-flash-8b":   {"input": 0.0375,"cached_input": 0.009375,"output":  0.15},
}

# Anthropic list pricing (platform.claude.com/docs/en/pricing). Cache
# reads bill at 0.1x input; cache writes at 1.25x input (5-minute TTL —
# the default; 1h-TTL writes bill 2x but usage payloads don't
# distinguish TTLs, so the common case is priced). Sonnet 5 uses the
# $3/$15 sticker, not the 2026-08-31 intro price. Dated full IDs
# (claude-opus-4-5-20251101) resolve by longest-prefix.
ANTHROPIC_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "claude-fable-5":    {"input": 10.00, "cached_input": 1.00, "cache_write": 12.50, "output": 50.00},
    "claude-mythos-5":   {"input": 10.00, "cached_input": 1.00, "cache_write": 12.50, "output": 50.00},
    "claude-opus-4-8":   {"input":  5.00, "cached_input": 0.50, "cache_write":  6.25, "output": 25.00},
    "claude-opus-4-7":   {"input":  5.00, "cached_input": 0.50, "cache_write":  6.25, "output": 25.00},
    "claude-opus-4-6":   {"input":  5.00, "cached_input": 0.50, "cache_write":  6.25, "output": 25.00},
    "claude-opus-4-5":   {"input":  5.00, "cached_input": 0.50, "cache_write":  6.25, "output": 25.00},
    "claude-opus-4-1":   {"input": 15.00, "cached_input": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-opus-4-0":   {"input": 15.00, "cached_input": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-opus-4-20250514":   {"input": 15.00, "cached_input": 1.50, "cache_write": 18.75, "output": 75.00},
    "claude-sonnet-5":   {"input":  3.00, "cached_input": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4-6": {"input":  3.00, "cached_input": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "cached_input": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4-0": {"input":  3.00, "cached_input": 0.30, "cache_write":  3.75, "output": 15.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "cached_input": 0.30, "cache_write": 3.75, "output": 15.00},
    "claude-haiku-4-5":  {"input":  1.00, "cached_input": 0.10, "cache_write":  1.25, "output":  5.00},
}

PROVIDER_TABLES: dict[str, dict[str, dict[str, float]]] = {
    "openai": OPENAI_PRICING_USD_PER_M,
    "gemini": GEMINI_PRICING_USD_PER_M,
    "anthropic": ANTHROPIC_PRICING_USD_PER_M,
}


def price_for_model(
    model: str | None,
    table: dict[str, dict[str, float]],
) -> dict[str, float] | None:
    if not model:
        return None
    if model in table:
        return table[model]
    # Longest-prefix fallback for dated / suffixed SKUs.
    match = ""
    for key in table:
        if model.startswith(key) and len(key) > len(match):
            match = key
    return table.get(match) if match else None


def compute_cost_usd(
    model: str | None,
    usage: dict[str, Any],
    table: dict[str, dict[str, float]],
) -> float | None:
    """USD cost for one api_request, or None if the model isn't priced.
    Returning None (vs 0.0) skips the attribute so unpriced models don't
    accumulate misleading zero rows in lakerunner."""
    price = price_for_model(model, table)
    if price is None:
        return None
    input_total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    thought = int(usage.get("thought_tokens") or 0)
    if "cache_write" in price:
        # Anthropic semantics: input / cache-read / cache-write are
        # disjoint buckets; cache creation bills at a premium.
        cache_creation = int(usage.get("cache_creation_tokens") or 0)
        cost = (
            input_total * price["input"]
            + cached          * price["cached_input"]
            + cache_creation  * price["cache_write"]
            + (output + thought) * price["output"]
        ) / 1_000_000.0
    else:
        # OpenAI/Gemini semantics: cached is a subset of input.
        non_cached_input = max(0, input_total - cached)
        cost = (
            non_cached_input * price["input"]
            + cached          * price["cached_input"]
            + (output + thought) * price["output"]
        ) / 1_000_000.0
    return round(cost, 6)
