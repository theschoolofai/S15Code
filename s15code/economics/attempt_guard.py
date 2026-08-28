"""AttemptGuardedTransport — closes a real gap in the shipped controller.

``MeteredTransport`` (``controller.py``) is explicit about a design choice: a
transport call that raises is counted as a *failure*, not a *charge*, because
"[a failed call] consumed no tokens the gateway could report." That is true for
the ordinary failure modes glc_v4 raises today (a rate limit, a dead provider,
a timeout before any output). It is NOT true for a provider that generates
output and only THEN drops the connection — a slow stream cut off after most
of the tokens were already produced and billed on the provider's own account,
which real gateways do observe. `proofs/p8_flaky_provider_dow.py` demonstrates
the consequence: because ``RunBudget.calls`` (what ``max_calls_per_run`` and
``max_calls_per_node`` in ``policy.py`` both gate on) only increments inside
``RunBudget.charge()``, and ``charge()`` is only reached after a transport call
*returns*, a provider that always fails after generating output can be retried
by an adversarial loop forever. The run ceiling in dollars still holds — no
charge is ever recorded, so ``spent`` never moves — but the CALL ceiling, which
is supposed to be the backstop for exactly the case where every price estimate
is wrong, never trips either, because it is counting the wrong thing: successful
charges, not attempts.

``AttemptGuardedTransport`` is the fix, applied without touching a single line
of the shipped ``controller.py`` or ``budget.py``: it wraps the transport one
layer OUTSIDE the existing meter and counts every attempt — success or
failure — against its own run- and node-scoped ceilings, using the same
:class:`~s15code.economics.budget.BudgetRefused` exception the real controller
raises, so a tripped guard is indistinguishable in the journal from an ordinary
budget refusal. It needs no cooperation from the provider: it counts attempts
made, not tokens reported, so a provider that never once tells the truth about
what it billed is still capped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .budget import BudgetRefused
from .controller import ChatTransport, current_call_site

#: Attribution used when a call is made outside any node's context.
UNATTRIBUTED = "unattributed"


@dataclass
class AttemptGuardedTransport:
    """Caps raw attempts per run and per node, independent of whether any of
    them ever return successfully.

    Sits between :class:`~s15code.economics.controller.BudgetedGateway` and the
    real (metered) transport::

        gateway = BudgetedGateway(
            AttemptGuardedTransport(MeteredTransport(flaky_transport),
                                     max_attempts_per_run=60, max_attempts_per_node=6),
            budget=budget, policy=policy, pricing=pricing, ladder=ladder,
        )
    """

    inner: ChatTransport
    max_attempts_per_run: int = 0
    max_attempts_per_node: int = 0
    run_attempts: int = 0
    node_attempts: dict[str, int] = field(default_factory=dict)
    #: Every attempt this guard refused, for the journal / a proof to inspect.
    refusals: list[dict[str, Any]] = field(default_factory=list)

    async def chat(
        self, *, prompt: str, system: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        site = current_call_site()
        node_id = site.node_id if site is not None else UNATTRIBUTED

        would_be_run = self.run_attempts + 1
        would_be_node = self.node_attempts.get(node_id, 0) + 1

        if self.max_attempts_per_run and would_be_run > self.max_attempts_per_run:
            reason = (
                f"attempt ceiling reached ({self.run_attempts}/{self.max_attempts_per_run} "
                f"raw attempts this run, regardless of whether any returned tokens)"
            )
            self.refusals.append({"node_id": node_id, "scope": "run", "reason": reason})
            raise BudgetRefused(reason, node_id=node_id, detail={"scope": "run", "reason": reason})
        if self.max_attempts_per_node and would_be_node > self.max_attempts_per_node:
            reason = (
                f"attempt ceiling reached ({self.node_attempts.get(node_id, 0)}/"
                f"{self.max_attempts_per_node} raw attempts on node {node_id!r})"
            )
            self.refusals.append({"node_id": node_id, "scope": "node", "reason": reason})
            raise BudgetRefused(reason, node_id=node_id, detail={"scope": "node", "reason": reason})

        # The attempt counts the moment it is MADE, not the moment it succeeds —
        # that is the entire fix. A provider that always fails after generating
        # output still trips this ceiling, because tripping it never depended on
        # the provider honestly reporting what it did.
        self.run_attempts = would_be_run
        self.node_attempts[node_id] = would_be_node
        return await self.inner.chat(prompt=prompt, system=system, request=request)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_attempts": self.run_attempts,
            "node_attempts": dict(self.node_attempts),
            "refusals": list(self.refusals),
            "max_attempts_per_run": self.max_attempts_per_run,
            "max_attempts_per_node": self.max_attempts_per_node,
        }
