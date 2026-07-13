"""Plugin-side cost computation for providers that do not emit cost natively.

Claude Code emits cost_usd itself — no table here. Codex (OpenAI) and
Gemini need plugin-computed cost, otherwise their sessions land at $0 and
disappear from the Outcomes Dashboard's spend-headed views.

USD per 1M tokens, per each provider's public pricing page. Lookup is
exact-match first, then longest-prefix so dated SKUs (e.g.
`gpt-5-codex-2026-03-01`, `gemini-2.0-pro-2026-03-01`) still price
correctly. Keep tables in sync with the pricing pages.

Billing semantics unified across providers:
- `input_tokens` is the total input count; `cached_input_tokens` is a
  subset billed at the cached rate.
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

PROVIDER_TABLES: dict[str, dict[str, dict[str, float]]] = {
    "openai": OPENAI_PRICING_USD_PER_M,
    "gemini": GEMINI_PRICING_USD_PER_M,
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
    non_cached_input = max(0, input_total - cached)
    cost = (
        non_cached_input * price["input"]
        + cached          * price["cached_input"]
        + (output + thought) * price["output"]
    ) / 1_000_000.0
    return round(cost, 6)
