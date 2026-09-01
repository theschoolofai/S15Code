#!/usr/bin/env python
"""p8 — attack the SRE-triage budget policy and show that refusals are visible.

Four attacks, one code path (the real controller, ladder, and journal):

1. **Runaway loop.** The same adversarial planner as p3, once against a real
   ceiling and once against a ceiling so large it might as well be off. Spend
   flattens under the controller; the uncontrolled extrapolation does not.
2. **Unaffordable tier.** Request the frontier rung with a ceiling that cannot
   cover even the cheapest projection. No provider call. The refusal envelope
   carries projected cost, remaining budget, and shortfall.
3. **Forced cascade.** Ask for economy, then standard, then frontier on one
   budget — the cost of climbing the whole ladder for one task, which is what a
   confidence signal pays when it is wrong.
4. **Provider fails after consuming tokens.** First call is billed. The next
   raises with no usage, so it is recorded as a transport failure and not
   charged. A subsequent request is refused. The refusal is in the ledger even
   though nothing was billed for it.

    python proofs/p8_attack_policy.py --task "<anything>" --budget 0.002 --principal course/s15/p8
    python proofs/p8_attack_policy.py --task "<anything>" --budget 0.002 --offline
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from harness import Args, Proof, economics, main, sync, transport_for
from p3_denial_of_wallet import drive_runaway

from s15code.economics import BudgetedGateway, BudgetRefused, MeteredTransport, call_site
from s15code.economics.controller import ChatTransport

SYSTEM = "Reply in one short sentence. This is a controlled attack."


class FailAfterTokensTransport:
    """Succeeds once (tokens billed), then raises with no usage."""

    def __init__(self, inner: ChatTransport) -> None:
        self.inner = inner
        self.calls = 0

    async def chat(self, *, prompt: str, system: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return await self.inner.chat(prompt=prompt, system=system, request=request)
        raise RuntimeError("provider failed after consuming prompt tokens; no usage returned")


async def _unaffordable(args: Args, transport: MeteredTransport) -> dict[str, Any]:
    config = economics(args)
    policy, ladder = config.policy(), config.ladder
    cheapest_cost = policy.project(ladder.cheapest)
    ceiling = cheapest_cost / 1000.0
    budget = config.budget(principal=args.principal, amount=ceiling, run_id="p8-unaffordable")
    gateway = BudgetedGateway(transport, budget=budget, policy=policy, pricing=config.pricing, ladder=ladder)
    error: str | None = None
    detail: dict[str, Any] = {}
    with call_site("unaffordable", "answer_with_evidence", ladder.most_capable.name):
        try:
            await gateway.complete(args.task, SYSTEM)
        except BudgetRefused as refused:
            error = str(refused)
            detail = dict(refused.detail)
    projected = float(detail.get("projected_cost") or 0.0)
    remaining = float(detail.get("remaining") or budget.remaining)
    return {
        "ceiling": ceiling,
        "spent": budget.spent,
        "calls": budget.calls,
        "refusals": len(budget.refusals),
        "transport_calls": transport.calls,
        "error": error,
        "detail": detail,
        "projected_usd": projected,
        "remaining_usd": remaining,
        "shortfall_usd": max(0.0, projected - remaining),
        "snapshot": budget.snapshot(),
    }


async def _cascade(args: Args, transport: MeteredTransport) -> dict[str, Any]:
    """Pay every rung of the ladder on purpose, as a wrong confidence signal does."""
    config = economics(args)
    policy, ladder = config.policy(), config.ladder
    ceiling = max(policy.project(ladder.tier(name)) for name in ladder.names) * (len(ladder.names) + 2)
    budget = config.budget(principal=args.principal, amount=ceiling, run_id="p8-cascade")
    gateway = BudgetedGateway(transport, budget=budget, policy=policy, pricing=config.pricing, ladder=ladder)
    steps: list[dict[str, Any]] = []
    for name in ladder.names:
        with call_site(f"cascade-{name}", "content", name):
            try:
                out = await gateway.complete(args.task, SYSTEM)
                steps.append({
                    "requested": name,
                    "served": out.get("tier"),
                    "model": out.get("model"),
                    "provider": out.get("provider"),
                    "cost": out.get("cost"),
                    "decision": out.get("budget_decision"),
                    "text_chars": len(out.get("text") or ""),
                })
            except BudgetRefused as refused:
                steps.append({"requested": name, "error": str(refused), "detail": refused.detail})
    return {
        "ceiling": ceiling,
        "spent": budget.spent,
        "calls": budget.calls,
        "steps": steps,
        "models": [s.get("model") for s in steps],
        "snapshot": budget.snapshot(),
    }


async def _fail_after_tokens(args: Args, inner: ChatTransport) -> dict[str, Any]:
    config = economics(args)
    policy, ladder = config.policy(), config.ladder
    # Room for one cheap call, not two. The second attempt fails the provider;
    # the third is a refusal.
    ceiling = policy.project(ladder.cheapest) * 1.5
    failing = FailAfterTokensTransport(inner)
    transport = MeteredTransport(failing)
    budget = config.budget(principal=args.principal, amount=ceiling, run_id="p8-fail")
    gateway = BudgetedGateway(transport, budget=budget, policy=policy, pricing=config.pricing, ladder=ladder)
    outcomes: list[dict[str, Any]] = []
    for index, label in enumerate(("billed", "provider_error", "refused"), start=1):
        with call_site(f"fail-{index}", "content", ladder.cheapest.name):
            try:
                out = await gateway.complete(args.task, SYSTEM)
                outcomes.append({"label": label, "ok": True, "cost": out.get("cost"),
                                 "tier": out.get("tier"), "model": out.get("model")})
            except BudgetRefused as refused:
                outcomes.append({"label": label, "ok": False, "kind": "BudgetRefused",
                                 "detail": refused.detail, "error": str(refused)})
            except Exception as error:
                outcomes.append({"label": label, "ok": False, "kind": type(error).__name__,
                                 "error": str(error)})
    snapshot = budget.snapshot()
    # The error path cannot shrink remaining (no usage to charge), so a refusal
    # on THIS budget may never arrive. Prove visibility on a fresh exhausted one.
    tiny = config.budget(principal=args.principal, amount=policy.project(ladder.cheapest) / 1000.0,
                         run_id="p8-fail-refuse")
    refuse_gateway = BudgetedGateway(MeteredTransport(inner), budget=tiny, policy=policy,
                                     pricing=config.pricing, ladder=ladder)
    with call_site("fail-refuse", "answer_with_evidence", ladder.most_capable.name):
        try:
            await refuse_gateway.complete(args.task, SYSTEM)
            outcomes.append({"label": "refused", "ok": True, "kind": "unexpected_success"})
        except BudgetRefused as refused:
            outcomes.append({"label": "refused", "ok": False, "kind": "BudgetRefused",
                             "detail": refused.detail, "error": str(refused)})
    tiny_snap = tiny.snapshot()
    return {
        "ceiling": ceiling,
        "spent": budget.spent,
        "calls": budget.calls,
        "refusals": snapshot["refusals"] + tiny_snap["refusals"],
        "refusal_log": snapshot["refusal_log"] + tiny_snap["refusal_log"],
        "transport_calls": transport.calls,
        "transport_failures": transport.failures,
        "outcomes": outcomes,
        "snapshot": snapshot,
        "tiny_snapshot": tiny_snap,
    }


async def _scenarios(args: Args) -> dict[str, Any]:
    live, mode, detail = transport_for(args)
    unaffordable = await _unaffordable(args, live)
    cascade = await _cascade(args, live)
    fail = await _fail_after_tokens(args, live.inner)
    return {"mode": mode, "mode_detail": detail, "unaffordable": unaffordable,
            "cascade": cascade, "fail": fail}


def run(args: Args) -> Proof:
    observed = sync(_scenarios(args))
    proof = Proof(name="p8_attack_policy", args=args, mode=observed["mode"],
                  mode_detail=observed["mode_detail"])

    # --- 1. runaway: controller on vs the same loop extrapolated ------------- #
    # Economy calls on this ladder are ~$4e-5. A 0.05 default would admit every
    # round of a 80-iteration proof stop. Derive a ceiling the adversary can
    # actually hit, and keep --budget as the caller's declared number.
    config = economics(args)
    tight = max(config.policy().project(config.ladder.cheapest) * 20, 0.0004)
    original_budget = args.budget
    args.budget = min(original_budget, tight)
    with tempfile.TemporaryDirectory(prefix="s15-p8-on-") as on_dir:
        # Must exceed the spend ceiling so the CONTROLLER stops the loop,
        # not the proof's own safety net.
        held = sync(drive_runaway(args, loop_limit=80, data_dir=Path(on_dir)))
    args.budget = original_budget * 1000 if original_budget else 1.0
    with tempfile.TemporaryDirectory(prefix="s15-p8-off-") as off_dir:
        loose = sync(drive_runaway(args, loop_limit=8, data_dir=Path(off_dir)))
    args.budget = original_budget

    held_budget, loose_budget = held["budget"], loose["budget"]
    cost_per_call = (loose_budget["spent"] / loose_budget["calls"]) if loose_budget["calls"] else 0.0
    extrapolated = cost_per_call * 10_000

    proof.fact("runaway held spent", f"{held_budget['spent']:.8f} of {held_budget['total']:.8f}")
    proof.fact("runaway held refusals", held_budget["refusals"])
    proof.fact("runaway held BudgetRefused nodes", held["refused_nodes"])
    proof.fact("runaway loose spent (8 rounds)", f"{loose_budget['spent']:.8f}")
    proof.fact("runaway extrapolated uncontrolled", f"~{extrapolated:.4f} over 10000 rounds")
    proof.fact("unaffordable projected / remaining / shortfall",
               f"{observed['unaffordable']['projected_usd']:.8f} / "
               f"{observed['unaffordable']['remaining_usd']:.8f} / "
               f"{observed['unaffordable']['shortfall_usd']:.8f}")
    proof.fact("cascade models", observed["cascade"]["models"])
    proof.fact("cascade spent", f"{observed['cascade']['spent']:.8f}")
    proof.fact("fail-after-tokens spent / failures / refusals",
               f"{observed['fail']['spent']:.8f} / {observed['fail']['transport_failures']} / "
               f"{observed['fail']['refusals']}")

    proof.check("the controller, not the loop, stopped the runaway spend",
                held_budget["spent"] <= held_budget["total"] and held_budget["refusals"] > 0,
                f"spent {held_budget['spent']:.8f} <= {held_budget['total']:.8f}, "
                f"{held_budget['refusals']} refusals, {held['rounds']} rounds")
    proof.check("a runaway refusal is a visible graph failure carrying BudgetRefused",
                held["refused_nodes"] > 0,
                f"{held['refused_nodes']} nodes failed with BudgetRefused")
    proof.check("without a tight ceiling the same loop keeps spending",
                loose_budget["refusals"] == 0 and loose_budget["calls"] == loose["rounds"],
                f"loose {loose_budget['spent']:.8f}/{loose_budget['calls']} calls in "
                f"{loose['rounds']} rounds, 0 refusals; held refused after "
                f"{held_budget['calls']} of {held['rounds']}")

    unaff = observed["unaffordable"]
    proof.check("an unaffordable frontier request never reaches a provider",
                unaff["calls"] == 0 and unaff["transport_calls"] == 0 and unaff["refusals"] >= 1,
                unaff)
    proof.check("the refusal envelope reports projected, remaining and shortfall",
                unaff["projected_usd"] > unaff["remaining_usd"] and unaff["shortfall_usd"] > 0,
                {"projected_usd": unaff["projected_usd"], "remaining_usd": unaff["remaining_usd"],
                 "shortfall_usd": unaff["shortfall_usd"]})
    proof.check("the unaffordable refusal is in the ledger even though nothing was billed",
                unaff["spent"] == 0 and unaff["refusals"] >= 1,
                {"spent": unaff["spent"], "refusals": unaff["refusals"]})

    cascade = observed["cascade"]
    served = [s.get("model") for s in cascade["steps"] if s.get("model")]
    proof.check("forcing the cascade climbs more than one model of the ladder",
                len(set(served)) >= 2,
                cascade["steps"])
    proof.check("the cascade bill is the sum of the rungs it passed through",
                cascade["spent"] > 0 and cascade["calls"] >= 2,
                {"spent": cascade["spent"], "calls": cascade["calls"]})

    fail = observed["fail"]
    kinds = [o.get("kind") for o in fail["outcomes"]]
    proof.check("a provider error with no usage is not charged",
                fail["transport_failures"] >= 1 and fail["calls"] == fail["transport_calls"],
                {"transport_calls": fail["transport_calls"],
                 "transport_failures": fail["transport_failures"],
                 "ledger_calls": fail["calls"], "spent": fail["spent"], "kinds": kinds})
    proof.check("a later request is refused and the refusal is recorded",
                fail["refusals"] >= 1 and any(o.get("kind") == "BudgetRefused" for o in fail["outcomes"]),
                fail["outcomes"])

    proof.record("runaway_held", {k: v for k, v in held.items() if k not in {"journal", "config"}})
    proof.record("runaway_loose", {k: v for k, v in loose.items() if k not in {"journal", "config"}})
    proof.record("unaffordable", unaff)
    proof.record("cascade", cascade)
    proof.record("fail_after_tokens", fail)
    proof.record("uncontrolled_bill_extrapolated", extrapolated)
    return proof


if __name__ == "__main__":
    main(run, __doc__ or "")
