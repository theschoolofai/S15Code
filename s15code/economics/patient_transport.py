"""PatientTransport — a rate limit is a transport failure, not a wrong answer.

`s15code/evals/judge.py` states the principle exactly right, and implements it
for the judge path only::

    A rate limit or a restarting gateway is a TRANSPORT failure, not a judge
    failure: the judge never saw the answer. Retrying with backoff is the
    honest response; giving up and calling the task unresolved would not be.

The identical argument applies to the ANSWER path, where it is not implemented.
In `proofs/p1_cost_per_task.py` an attempt that raises is caught, recorded with
an ``error``, and handed to the judge, which scores an empty answer as
unresolved without calling anyone. So on a free-tier account — where HTTP 429
and provider cooldowns are routine rather than exceptional — a model that was
never actually asked the question is recorded as having failed to answer it,
and the resolution rate, which is the denominator of cost-per-resolved-task,
silently absorbs the provider's rate limit as if it were a quality signal.

That is not a hypothetical. On the first live run of the support-desk task set
the ledger recorded 23 successful calls against **60 transport failures**
(Groq ``TPM limit`` and Gemini ``RPM quota burned``). The always-frontier
baseline made zero successful calls and was scored 0/18 resolved — a number
that says nothing whatsoever about the frontier model's quality, but which
would have gone straight into a cost-per-resolved-task table.

This wrapper closes that gap on the answer path:

* **Retry only what deserves retrying.** A 429, a 503 "all providers
  unavailable", a cooldown or a TPM/RPM refusal is transient and is retried
  with exponential backoff. A 404, a malformed request or an unknown model is
  permanent and is raised immediately — waiting on it only wastes wall-clock.
* **Pace by default.** Free-tier limits are per-minute, so the cheapest way to
  not hit them is to not burst. A small gap between calls costs wall-clock and
  buys measurement validity.
* **Stay honest about what happened.** Retries and waits are counted and
  reported, so a run that only completed because it waited says so rather than
  quietly looking healthy.

Note what this deliberately does NOT do: it never invents a response, never
substitutes a cheaper model, and never converts a genuine provider error into a
success. If every retry is exhausted the original exception is re-raised and
the attempt fails exactly as it would have before.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .controller import ChatTransport

#: Substrings that mark a failure as transient and therefore worth waiting out.
#: Matched case-insensitively against ``str(exception)``.
RETRYABLE = (
    "429",
    "rate limit",
    "ratelimit",
    "tpm limit",
    "rpm limit",
    "quota burned",
    "exceeded your current quota",
    "resource_exhausted",
    "cooldown",
    "backoff",
    "all providers unavailable",
    "503",
    "502",
    "timeout",
    "timed out",
    "connection reset",
    "temporarily unavailable",
)

#: Substrings that mark a failure as permanent, checked FIRST so that a message
#: like "404 ... model unavailable" is never mistaken for a transient outage.
PERMANENT = (
    "404",
    "not found",
    "unknown provider",
    "invalid api key",
    "unauthorized",
    "401",
    "403",
)

_WAIT_HINT = re.compile(r"(\d+(?:\.\d+)?)\s*s(?:ec|econds)?\s*(?:left|remaining)", re.I)


def classify(error: BaseException) -> str:
    """``"permanent"``, ``"transient"`` or ``"unknown"`` for one failure."""
    text = f"{type(error).__name__}: {error}".lower()
    for marker in PERMANENT:
        if marker in text:
            return "permanent"
    for marker in RETRYABLE:
        if marker in text:
            return "transient"
    return "unknown"


def suggested_wait(error: BaseException) -> float | None:
    """Any wait the provider itself named, e.g. ``"RPM quota burned (42s left)"``.

    Honouring the provider's own number beats guessing: it is the one party
    that knows when the window actually reopens.
    """
    match = _WAIT_HINT.search(str(error))
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:
        return None


@dataclass
class PatientTransport:
    """Wrap a transport so transient provider refusals are waited out.

    Sits OUTSIDE the meter, so a retried call is metered exactly once — only the
    attempt that actually returned tokens is ever charged::

        MeteredTransport(PatientTransport(GatewayClient(url)))
    """

    inner: ChatTransport
    retries: int = 4
    backoff_seconds: float = 8.0
    #: Minimum gap between calls. Free-tier limits are per-minute; not bursting
    #: is cheaper than recovering from a burst.
    pace_seconds: float = 0.0
    #: Never wait longer than this for a single attempt, whatever the provider says.
    max_wait_seconds: float = 90.0

    waits: int = 0
    total_waited_seconds: float = 0.0
    permanent_failures: int = 0
    exhausted: int = 0
    reasons: list[str] = field(default_factory=list)
    _last_call_at: float = 0.0

    async def _pace(self) -> None:
        if self.pace_seconds <= 0:
            return
        gap = self.pace_seconds - (time.monotonic() - self._last_call_at)
        if gap > 0 and self._last_call_at:
            await asyncio.sleep(gap)
        self._last_call_at = time.monotonic()

    async def chat(
        self, *, prompt: str, system: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            await self._pace()
            try:
                return await self.inner.chat(prompt=prompt, system=system, request=request)
            except Exception as error:
                last = error
                kind = classify(error)
                if kind == "permanent":
                    self.permanent_failures += 1
                    self.reasons.append(f"permanent, not retried: {str(error)[:160]}")
                    raise
                if attempt >= self.retries:
                    self.exhausted += 1
                    self.reasons.append(f"exhausted {self.retries} retries: {str(error)[:160]}")
                    raise
                # Prefer the provider's own number; otherwise exponential backoff.
                hinted = suggested_wait(error)
                wait = hinted + 1.0 if hinted is not None else self.backoff_seconds * (2**attempt)
                wait = min(wait, self.max_wait_seconds)
                self.waits += 1
                self.total_waited_seconds += wait
                self.reasons.append(f"{kind}, waited {wait:.0f}s: {str(error)[:120]}")
                await asyncio.sleep(wait)
        assert last is not None
        raise last

    def snapshot(self) -> dict[str, Any]:
        """What patience actually cost, so a slow-but-valid run says so."""
        return {
            "retries_configured": self.retries,
            "pace_seconds": self.pace_seconds,
            "waits": self.waits,
            "total_waited_seconds": round(self.total_waited_seconds, 1),
            "permanent_failures": self.permanent_failures,
            "exhausted_after_retries": self.exhausted,
            "reasons": self.reasons[-12:],
        }
