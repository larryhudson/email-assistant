from decimal import Decimal

from email_agent.agent.pricing import estimate_cost_usd


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_usd(model="anything", input_tokens=0, output_tokens=0) == Decimal("0.0000")


def test_known_model_uses_table() -> None:
    # 1M input + 1M output at minimax-m2p7 → $0.30 + $1.20 = $1.5000.
    cost = estimate_cost_usd(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("1.5000")


def test_small_run_carries_4dp_precision() -> None:
    # 10 input + 5 output → 10*0.30/1M + 5*1.20/1M = 0.000003 + 0.000006 = 0.000009
    # → quantised to 4dp = 0.0000.
    cost = estimate_cost_usd(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=10,
        output_tokens=5,
    )
    assert cost == Decimal("0.0000")


def test_typical_run_carries_4dp_precision() -> None:
    # 1700 input + 200 output at minimax: 1700*0.30/1M + 200*1.20/1M
    # = 0.00051 + 0.00024 = 0.00075 → 0.0008 (HALF_UP at 4dp).
    cost = estimate_cost_usd(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_700,
        output_tokens=200,
    )
    assert cost == Decimal("0.0008")


def test_unknown_model_falls_back_to_default() -> None:
    cost = estimate_cost_usd(model="bogus", input_tokens=1_000_000, output_tokens=0)
    assert cost == Decimal("0.5000")


def test_cache_read_tokens_billed_at_cache_rate() -> None:
    # 1M total prompt tokens, all served from cache → $0.06.
    cost = estimate_cost_usd(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == Decimal("0.0600")


def test_partial_cache_split_billing() -> None:
    # 1M input, 800k from cache → 200k * 0.30 + 800k * 0.06 / 1M
    # = 0.06 + 0.048 = 0.108 → 0.1080.
    cost = estimate_cost_usd(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=800_000,
    )
    assert cost == Decimal("0.1080")
