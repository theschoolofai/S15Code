"""Attempt ceilings: bounding a provider that spends tokens and then fails.

The call ceilings in ``policy.py`` count CHARGES, and a charge only exists once a
response came back with token counts to price. That leaves a gap wide enough to
drive a bill through: a provider that generates an answer, consumes the tokens
and then drops the connection returns nothing to charge from, so it never
increments ``calls``, never moves ``max_calls_per_run`` or ``max_calls_per_node``,
and can be handed work again immediately — forever.

``proofs/p8_adversarial.py`` found that by attacking the policy. These tests pin
the fix, and the first one pins the BUG, so that a future change which silently
starts charging for failed calls has to come here and argue with it.

Hermetic, like ``test_economics.py``: the ladder, the prices and the thresholds
are invented here, so nothing depends on what ``config/`` happens to ship.
"""

from __future__ import annotations

import pytest

from s15code.economics import (
    BudgetedGateway,
    BudgetPolicy,
    BudgetRefused,
    MeteredTransport,
    PolicyThresholds,
    Pricing,
    RunBudget,
    TierLadder,
    call_site,
)

LADDER_DATA = {
    "order": ["thrifty", "lavish"],
    "default_tier": "thrifty",
    "tiers": {
        "thrifty": {"request": {"provider": "p", "model": "cheap-1", "max_tokens": 100},
                    "projected_input_tokens": 1000, "projected_output_tokens": 100},
        "lavish": {"request": {"provider": "p", "model": "dear-1", "max_tokens": 2000},
                   "projected_input_tokens": 1000, "projected_output_tokens": 2000},
    },
    "role_tiers": {"default": "thrifty"},
}

PRICING_DATA = {
    "currency": "USD",
    "unit_tokens": 1_000_000,
    "default": {"input": 100.0, "output": 100.0},
    "models": {"cheap-1": {"input": 1.0, "output": 1.0}, "dear-1": {"input": 1.0, "output": 1.0}},
}


@pytest.fixture
def ladder() -> TierLadder:
    return TierLadder.from_mapping(LADDER_DATA)


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.from_mapping(PRICING_DATA)


class BurnsThenFails:
    """Consumes tokens on the provider side, then fails before returning them."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *, prompt: str, system: str, request=None):
        self.calls += 1
        raise RuntimeError("provider generated the response, then the connection dropped")


def gateway(ladder, pricing, transport, *, total=1.0, **thresholds) -> tuple[BudgetedGateway, RunBudget]:
    defaults = dict(downgrade_at=1.0, refuse_at=1.0, headroom_fraction=0.0,
                    max_calls_per_run=0, max_calls_per_node=0,
                    max_attempts_per_run=0, max_attempts_per_node=0)
    resolved = PolicyThresholds(**{**defaults, **thresholds})
    budget = RunBudget(total=total)
    controller = BudgetedGateway(
        transport, budget=budget, policy=BudgetPolicy(ladder, pricing, resolved),
        pricing=pricing, ladder=ladder,
    )
    return controller, budget


async def drive(controller, *, rounds: int, node_id: str = "n") -> dict[str, int]:
    outcomes = {"refused": 0, "failed": 0}
    for _ in range(rounds):
        with call_site(node_id, "default"):
            try:
                await controller.complete("prompt", "system")
            except BudgetRefused:
                outcomes["refused"] += 1
            except Exception:
                outcomes["failed"] += 1
    return outcomes


async def test_call_ceilings_are_blind_to_a_provider_that_fails_after_consuming_tokens(
    ladder, pricing
):
    """The bug, pinned. Call ceilings cannot see an unbillable call."""
    provider = BurnsThenFails()
    transport = MeteredTransport(provider)
    controller, budget = gateway(ladder, pricing, transport, max_calls_per_run=2, max_calls_per_node=2)

    outcomes = await drive(controller, rounds=10)

    # Every one of the ten reached the provider: the ceiling of 2 never bound,
    # because `calls` counts charges and no charge was ever created.
    assert provider.calls == 10
    assert outcomes["failed"] == 10 and outcomes["refused"] == 0
    assert budget.calls == 0 and budget.spent == 0.0
    # ...while `attempts` did see all ten. That is what makes the fix possible.
    assert budget.attempts == 10


async def test_attempt_ceiling_stops_a_provider_that_fails_after_consuming_tokens(ladder, pricing):
    provider = BurnsThenFails()
    transport = MeteredTransport(provider)
    controller, budget = gateway(ladder, pricing, transport, max_attempts_per_run=3)

    outcomes = await drive(controller, rounds=10)

    assert provider.calls == 3, "the provider stops being handed work after the third attempt"
    assert outcomes["failed"] == 3 and outcomes["refused"] == 7
    assert budget.attempts == 3
    assert [entry["reason"] for entry in budget.refusals][0].startswith("run attempt ceiling reached")


async def test_the_per_node_attempt_ceiling_bounds_one_looping_node(ladder, pricing):
    provider = BurnsThenFails()
    controller, budget = gateway(ladder, pricing, MeteredTransport(provider),
                                 max_attempts_per_node=2)

    first = await drive(controller, rounds=5, node_id="a")
    second = await drive(controller, rounds=5, node_id="b")

    # Two attempts each: the ceiling is per node, so a second node starts fresh.
    assert provider.calls == 4
    assert first["failed"] == 2 and first["refused"] == 3
    assert second["failed"] == 2 and second["refused"] == 3
    assert budget.node_attempts("a") == 2 and budget.node_attempts("b") == 2


async def test_attempts_and_charges_stay_different_numbers_on_a_healthy_provider(ladder, pricing):
    """A working provider must not be double-counted: one call, one attempt, one charge."""

    class Works:
        async def chat(self, *, prompt: str, system: str, request=None):
            return {"text": "ok", "provider": "p", "model": "cheap-1",
                    "input_tokens": 10, "output_tokens": 10}

    controller, budget = gateway(ladder, pricing, MeteredTransport(Works()), max_attempts_per_run=5)
    await drive(controller, rounds=3)

    assert budget.attempts == 3 and budget.calls == 3
    assert budget.spent == pytest.approx(60 / 1e6)


async def test_attempt_ceilings_default_to_disabled(ladder, pricing):
    """Zero means off, so no configuration that predates this feature changes."""
    provider = BurnsThenFails()
    controller, budget = gateway(ladder, pricing, MeteredTransport(provider))

    outcomes = await drive(controller, rounds=6)

    assert provider.calls == 6 and outcomes["refused"] == 0
    assert budget.attempts == 6
    assert PolicyThresholds.from_mapping({}).max_attempts_per_run == 0
    assert PolicyThresholds.from_mapping({}).max_attempts_per_node == 0


def test_attempt_ceilings_are_read_from_config_like_every_other_threshold():
    thresholds = PolicyThresholds.from_mapping(
        {"max_attempts_per_run": 30, "max_attempts_per_node": 5}
    )
    assert thresholds.max_attempts_per_run == 30
    assert thresholds.max_attempts_per_node == 5
