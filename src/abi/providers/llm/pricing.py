"""Cost estimation for OpenAI-compatible models.

Prices are best-effort and conservative; users running on other providers can
add entries via config in v0.2. For v0.1 we ship a small built-in table and
return 0.0 for unknown models (still recorded in tokens).
"""

from __future__ import annotations

# USD per 1,000 tokens
PRICES: dict[str, tuple[float, float]] = {
    # input, output
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
    "gpt-5": (0.0050, 0.0150),
    "deepseek-chat": (0.00027, 0.0011),
    "deepseek-reasoner": (0.00055, 0.0022),
}


def estimate_cost_usd(model: str, *, tokens_in: int, tokens_out: int) -> float:
    """Return cost in USD. Unknown models return 0.0 (still tracks token totals)."""
    key = model.lower()
    if key not in PRICES:
        # try prefix match (handles dated suffixes like "gpt-4o-mini-2024-07-18")
        for k in PRICES:
            if key.startswith(k):
                key = k
                break
        else:
            return 0.0
    p_in, p_out = PRICES[key]
    return (tokens_in / 1000.0) * p_in + (tokens_out / 1000.0) * p_out
