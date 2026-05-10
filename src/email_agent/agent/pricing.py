"""Per-model token pricing for `usage_ledger.cost_usd`.

The `BudgetGovernor` reads this ledger to gate runs, so getting numbers
roughly right matters more than getting them perfect. Update the table
when prices change; assistants whose `model_name` isn't listed get the
`_DEFAULT` fallback (deliberately mid-range so we don't accidentally
under-bill an unknown model).

Numbers are USD per **million tokens** (Fireworks' billing unit). Source:
Fireworks pricing page; refresh periodically.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class TokenPrice:
    """USD per million tokens for one model.

    `cache_read_usd_per_mtok` is the discounted rate for prompt tokens
    served from the provider's cache. Providers report `cache_read_tokens`
    as a subset of `input_tokens`, so we charge:
        (input_tokens - cache_read_tokens) * input
        + cache_read_tokens                 * cache_read
        + output_tokens                     * output
    """

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal


# Fireworks-hosted models. Keys are the full Fireworks model id stored on
# `assistants.model`.
_PRICING: dict[str, TokenPrice] = {
    # MiniMax M2 — Fireworks: $0.30 input / $1.20 output / $0.06 cache-read
    # per million tokens.
    "accounts/fireworks/models/minimax-m2p7": TokenPrice(
        input_usd_per_mtok=Decimal("0.30"),
        output_usd_per_mtok=Decimal("1.20"),
        cache_read_usd_per_mtok=Decimal("0.06"),
    ),
}

_DEFAULT = TokenPrice(
    input_usd_per_mtok=Decimal("0.50"),
    output_usd_per_mtok=Decimal("1.50"),
    cache_read_usd_per_mtok=Decimal("0.10"),
)

_MTOK = Decimal("1000000")
_QUANTUM = Decimal("0.0001")


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> Decimal:
    """USD cost for one run, quantised to 4 decimal places.

    `cache_read_tokens` is a subset of `input_tokens` (OpenAI-compatible
    billing convention), so the uncached portion is
    `input_tokens - cache_read_tokens`.
    """
    price = _PRICING.get(model, _DEFAULT)
    uncached_input = max(0, input_tokens - cache_read_tokens)
    raw = (
        Decimal(uncached_input) * price.input_usd_per_mtok
        + Decimal(cache_read_tokens) * price.cache_read_usd_per_mtok
        + Decimal(output_tokens) * price.output_usd_per_mtok
    ) / _MTOK
    return raw.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = ["TokenPrice", "estimate_cost_usd"]
