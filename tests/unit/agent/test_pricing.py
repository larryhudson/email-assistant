from email_agent.agent.pricing import estimate_cost_cents


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_cents(model="anything", input_tokens=0, output_tokens=0) == 0


def test_known_model_uses_table() -> None:
    # 1M input + 1M output at minimax-m2p7 → 30 + 120 = 150 cents.
    cost = estimate_cost_cents(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == 150


def test_small_run_rounds_up_to_at_least_one_cent() -> None:
    cost = estimate_cost_cents(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=10,
        output_tokens=5,
    )
    assert cost == 1


def test_unknown_model_falls_back_to_default() -> None:
    cost = estimate_cost_cents(model="bogus", input_tokens=1_000_000, output_tokens=0)
    assert cost == 50


def test_cache_read_tokens_billed_at_cache_rate() -> None:
    # 1M total prompt tokens, all served from cache → 6 cents (not 30).
    cost = estimate_cost_cents(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == 6


def test_partial_cache_split_billing() -> None:
    # 1M input total, 800k from cache → 200k uncached at 30c + 800k at 6c
    # = 6 + 4.8 = 10.8 cents → rounded up to 11.
    cost = estimate_cost_cents(
        model="accounts/fireworks/models/minimax-m2p7",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=800_000,
    )
    assert cost == 11
