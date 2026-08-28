"""PatientTransport: wait out a rate limit, never wait out a 404.

Hermetic, no network, no sleeping of any consequence (backoff is set to ~0).
The classifier is the part worth testing hardest: treating a permanent failure
as transient burns wall-clock for nothing, and treating a transient failure as
permanent is the original bug — a provider's per-minute limit recorded as a
model's failure to answer.
"""

from __future__ import annotations

import pytest

from s15code.economics.patient_transport import (
    PatientTransport,
    classify,
    suggested_wait,
)

OK = {"text": "ok", "provider": "p", "model": "m", "input_tokens": 1, "output_tokens": 1}


class Scripted:
    """Raises the given errors in order, then returns a normal response."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def chat(self, *, prompt, system, request=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return dict(OK)


# --- the classifier -------------------------------------------------------- #


@pytest.mark.parametrize("message", [
    'gateway /v1/chat returned 503: all providers unavailable. reason: TPM limit',
    "gateway returned 503: {'provider': 'gemini_1', 'reason': 'backoff: RPM quota burned (42s left)'}",
    'gemini HTTP 429: You exceeded your current quota',
    "reason: 'cooldown (1.4s)'",
    "HTTP 502 bad gateway",
    "read timed out",
])
def test_transient_failures_are_recognised(message):
    assert classify(RuntimeError(message)) == "transient"


@pytest.mark.parametrize("message", [
    'gemini HTTP 404: models/gemini-3.1-pro is not found for API version v1beta',
    "unknown provider 'groq'. Try one of: ['gemini_1']",
    "HTTP 401 unauthorized",
    "Invalid API key",
])
def test_permanent_failures_are_recognised(message):
    assert classify(RuntimeError(message)) == "permanent"


def test_permanent_wins_over_transient_when_a_message_contains_both():
    # "404 ... temporarily unavailable" must not be waited out.
    assert classify(RuntimeError("HTTP 404 not found; temporarily unavailable")) == "permanent"


def test_the_providers_own_wait_hint_is_read():
    assert suggested_wait(RuntimeError("backoff: RPM quota burned (42s left)")) == 42.0
    assert suggested_wait(RuntimeError("quota burned (3.5 seconds remaining)")) == 3.5
    assert suggested_wait(RuntimeError("no number here")) is None


# --- behaviour ------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_and_then_succeeds():
    inner = Scripted(RuntimeError("503: all providers unavailable. reason: TPM limit"))
    transport = PatientTransport(inner, retries=3, backoff_seconds=0.001)

    result = await transport.chat(prompt="p", system="s")

    assert result["text"] == "ok"
    assert inner.calls == 2
    assert transport.waits == 1


@pytest.mark.asyncio
async def test_a_permanent_failure_is_raised_immediately_without_waiting():
    inner = Scripted(RuntimeError("gemini HTTP 404: model not found"))
    transport = PatientTransport(inner, retries=5, backoff_seconds=999)

    with pytest.raises(RuntimeError, match="404"):
        await transport.chat(prompt="p", system="s")

    assert inner.calls == 1          # never retried
    assert transport.waits == 0      # and never slept
    assert transport.permanent_failures == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_and_the_original_error_survives():
    inner = Scripted(*[RuntimeError("429 rate limit") for _ in range(10)])
    transport = PatientTransport(inner, retries=2, backoff_seconds=0.001)

    with pytest.raises(RuntimeError, match="429"):
        await transport.chat(prompt="p", system="s")

    assert inner.calls == 3          # the first attempt plus 2 retries
    assert transport.exhausted == 1


@pytest.mark.asyncio
async def test_a_hinted_wait_is_capped_so_one_call_cannot_hang_the_run():
    inner = Scripted(RuntimeError("backoff: RPM quota burned (9999s left)"))
    transport = PatientTransport(inner, retries=1, backoff_seconds=0.001, max_wait_seconds=0.01)

    await transport.chat(prompt="p", system="s")

    assert transport.total_waited_seconds <= 0.01


@pytest.mark.asyncio
async def test_it_never_invents_a_response():
    """Exhausting retries must fail, not fabricate a success."""
    inner = Scripted(*[RuntimeError("503 all providers unavailable") for _ in range(5)])
    transport = PatientTransport(inner, retries=1, backoff_seconds=0.001)

    with pytest.raises(RuntimeError):
        await transport.chat(prompt="p", system="s")


@pytest.mark.asyncio
async def test_snapshot_reports_what_patience_cost():
    inner = Scripted(RuntimeError("429 rate limit"))
    transport = PatientTransport(inner, retries=2, backoff_seconds=0.001)
    await transport.chat(prompt="p", system="s")

    snap = transport.snapshot()
    assert snap["waits"] == 1
    assert snap["retries_configured"] == 2
    assert snap["exhausted_after_retries"] == 0
    assert snap["reasons"]
