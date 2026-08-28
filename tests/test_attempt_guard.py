"""AttemptGuardedTransport: caps raw attempts, independent of success.

Hermetic, no network. Proves the gap `proofs/p8_flaky_provider_dow.py`
demonstrates end to end: a transport that ALWAYS raises after doing its work is
still capped, because the guard counts the attempt, not the charge.
"""

from __future__ import annotations

import pytest

from s15code.economics import BudgetRefused, call_site
from s15code.economics.attempt_guard import AttemptGuardedTransport


class AlwaysFailsAfterWorkTransport:
    """Every call "does the work" then raises — never returns a response."""

    def __init__(self) -> None:
        self.attempts = 0

    async def chat(self, *, prompt, system, request=None):
        self.attempts += 1
        raise ConnectionError("dropped after generating output")


class SometimesSucceedsTransport:
    def __init__(self, fail_first: int) -> None:
        self.fail_first, self.calls = fail_first, 0

    async def chat(self, *, prompt, system, request=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("dropped")
        return {"text": "ok", "provider": "p", "model": "m", "input_tokens": 1, "output_tokens": 1}


@pytest.mark.asyncio
async def test_run_ceiling_trips_on_failures_alone():
    inner = AlwaysFailsAfterWorkTransport()
    guard = AttemptGuardedTransport(inner, max_attempts_per_run=3)

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await guard.chat(prompt="p", system="s")

    with pytest.raises(BudgetRefused):
        await guard.chat(prompt="p", system="s")

    # The guard refused BEFORE ever calling the inner transport a 4th time.
    assert inner.attempts == 3
    assert guard.run_attempts == 3
    assert len(guard.refusals) == 1


@pytest.mark.asyncio
async def test_node_ceiling_is_independent_of_run_ceiling():
    inner = AlwaysFailsAfterWorkTransport()
    guard = AttemptGuardedTransport(inner, max_attempts_per_run=100, max_attempts_per_node=2)

    with call_site("node-a", "content"):
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await guard.chat(prompt="p", system="s")
        with pytest.raises(BudgetRefused) as excinfo:
            await guard.chat(prompt="p", system="s")
    assert excinfo.value.node_id == "node-a"

    # A different node still has its own, separate allowance.
    with call_site("node-b", "content"):
        with pytest.raises(ConnectionError):
            await guard.chat(prompt="p", system="s")

    assert guard.node_attempts == {"node-a": 2, "node-b": 1}


@pytest.mark.asyncio
async def test_a_successful_call_still_counts_as_an_attempt():
    inner = SometimesSucceedsTransport(fail_first=1)
    guard = AttemptGuardedTransport(inner, max_attempts_per_run=2)

    with pytest.raises(ConnectionError):
        await guard.chat(prompt="p", system="s")
    result = await guard.chat(prompt="p", system="s")
    assert result["text"] == "ok"

    with pytest.raises(BudgetRefused):
        await guard.chat(prompt="p", system="s")
    assert guard.run_attempts == 2
