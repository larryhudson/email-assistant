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
    input_cents_per_mtok: float
    output_cents_per_mtok: float


# Fireworks-hosted models. Keys can be either the short alias used in
# `assistants.model` or the full `accounts/fireworks/models/...` id.
_PRICING: dict[str, TokenPrice] = {
    # MiniMax M2 — Fireworks lists $0.30 / $1.20 per million tokens.
    "accounts/fireworks/models/minimax-m2p7": TokenPrice(
        input_cents_per_mtok=30.0, output_cents_per_mtok=120.0
    ),
}

_DEFAULT = TokenPrice(input_cents_per_mtok=50.0, output_cents_per_mtok=150.0)


def estimate_cost_cents(*, model: str, input_tokens: int, output_tokens: int) -> int:
    """Round-up cost in cents for a given model + token counts.

    Returns 0 only when both token counts are 0 (TestModel runs).
    """
    price = _PRICING.get(model, _DEFAULT)
    raw = (
        input_tokens * price.input_cents_per_mtok + output_tokens * price.output_cents_per_mtok
    ) / 1_000_000
    if raw == 0:
        return 0
    # Round up to the nearest cent so a 0.001-cent run still records 1.
    return max(1, int(raw + 0.999999))


__all__ = ["TokenPrice", "estimate_cost_cents"]
