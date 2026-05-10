"""Per-model token pricing for `usage_ledger.cost_cents`.

The `BudgetGovernor` reads this ledger to gate runs, so getting numbers
roughly right matters more than getting them perfect. Update the table
when prices change; assistants whose `model_name` isn't listed get the
`_DEFAULT` fallback (deliberately mid-range so we don't accidentally
under-bill an unknown model).

Numbers are USD cents per **million tokens** (Fireworks' billing unit).
Source: Fireworks pricing page; refresh periodically.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenPrice:
    """USD cents per million tokens for one model.

    `cache_read_cents_per_mtok` is the discounted rate for prompt tokens
    served from the provider's cache. Providers report `cache_read_tokens`
    as a subset of `input_tokens`, so we charge:
        (input_tokens - cache_read_tokens) * input
        + cache_read_tokens                 * cache_read
        + output_tokens                     * output
    """

    input_cents_per_mtok: float
    output_cents_per_mtok: float
    cache_read_cents_per_mtok: float


# Fireworks-hosted models. Keys can be either the short alias used in
# `assistants.model` or the full `accounts/fireworks/models/...` id.
_PRICING: dict[str, TokenPrice] = {
    # MiniMax M2 — Fireworks: $0.30 input / $1.20 output / $0.06 cache-read
    # per million tokens.
    "accounts/fireworks/models/minimax-m2p7": TokenPrice(
        input_cents_per_mtok=30.0,
        output_cents_per_mtok=120.0,
        cache_read_cents_per_mtok=6.0,
    ),
}

_DEFAULT = TokenPrice(
    input_cents_per_mtok=50.0,
    output_cents_per_mtok=150.0,
    cache_read_cents_per_mtok=10.0,
)


def estimate_cost_cents(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> int:
    """Round-up cost in cents for a model + token counts.

    `cache_read_tokens` is treated as a subset of `input_tokens` (the
    OpenAI-compatible billing convention), so the uncached portion of the
    prompt is `input_tokens - cache_read_tokens`.

    Returns 0 only when all counts are 0 (TestModel runs).
    """
    price = _PRICING.get(model, _DEFAULT)
    uncached_input = max(0, input_tokens - cache_read_tokens)
    raw = (
        uncached_input * price.input_cents_per_mtok
        + cache_read_tokens * price.cache_read_cents_per_mtok
        + output_tokens * price.output_cents_per_mtok
    ) / 1_000_000
    if raw == 0:
        return 0
    return max(1, int(raw + 0.999999))


__all__ = ["TokenPrice", "estimate_cost_cents"]
