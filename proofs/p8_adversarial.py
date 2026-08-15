#!/usr/bin/env python
"""p8 — four attacks on the navigation budget policy, and what each one costs.

Every routing policy is a claim about money that only means something if someone
has tried to break it. This proof attacks the controller four ways, and for each
one reports the spend BEFORE the control and the outcome AFTER it:

    A  RUNAWAY LOOP          a loop that earns another node from every outcome,
                             forever. Uncontrolled spend is MEASURED on a short
                             sample and extrapolated; controlled spend is run in
                             full and stops on its own.
    B  UNAFFORDABLE TIER     a principal whose ceiling cannot pay for the rung
                             its role demands. At one ceiling the policy should
                             DOWNGRADE, at a lower one it should REFUSE, and the
                             boundary between the two is arithmetic, not taste.
    C  WHOLE-LADDER CLIMB    a cascade driven to escalate on every verdict until
                             it runs out of ladder, then out of attempts.
    D  PROVIDER FAILS AFTER  a provider that generates the answer, consumes the
       CONSUMING TOKENS      tokens, and THEN fails. Nothing is returned, so
                             nothing is charged — and the tokens are gone.

D is the interesting one, and it is included because it is the attack this
controller does NOT fully stop. The ledger charges from the token counts in a
RESPONSE, so a call that dies after generation is unbilled real spend, and the
run-level call ceiling does not bound it either, because that ceiling counts
CHARGES and a failed call never becomes one. This proof measures the size of
that hole rather than describing it, and re-runs the same attack against the
attempt ceiling that closes it.

    uv run python proofs/p8_adversarial.py --config-dir config/navigation \\
        --base-url http://127.0.0.1:8111
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OUT, Args, Proof  # noqa: E402

from s15code.economics import (  # noqa: E402
    BudgetedGateway,
    BudgetPolicy,
    BudgetRefused,
    EconomicsConfig,
    MeteredTransport,
    PolicyThresholds,
    call_site,
)
from s15code.gateway import GatewayClient  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")

#: The role the attacks call as. It maps to the top rung in tiers.yaml, which is
#: what makes "demand a tier you cannot pay for" the DEFAULT behaviour of this
#: role rather than something the attack has to contrive.
ATTACK_ROLE = "answer_with_evidence"

ATTACK_PROMPT = (
    "Recompute the ETA for a 42 km leg at 90 km/h, then refine it once more for accuracy."
)
ATTACK_SYSTEM = "You are a navigation assistant. Answer briefly and state the final result."

#: How many uncontrolled calls to actually make before extrapolating. Small on
#: purpose: the point of the uncontrolled arm is to price ONE call honestly, and
#: paying for two hundred of them to prove a multiplication is not evidence, it
#: is just a bill.
UNCONTROLLED_SAMPLE = 6
#: The loop length both arms are compared over.
LOOP_ROUNDS = 60


class FailsAfterTokens:
    """A provider that does the work, burns the tokens, and then fails.

    This is not a strawman. A gateway timeout, a dropped connection, or a 5xx
    from a load balancer after generation all land here: the provider metered the
    call on its side, and the client has nothing to charge from. The wrapper
    keeps what the real response WOULD have been so the unbilled spend can be
    priced exactly rather than guessed at.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.burned: list[dict[str, Any]] = []

    async def chat(self, *, prompt: str, system: str, request: dict[str, Any] | None = None):
        response = await self.inner.chat(prompt=prompt, system=system, request=request)
        self.burned.append(response)
        raise RuntimeError("provider generated the response, then the connection dropped")

    def unbilled(self, pricing: Any) -> float:
        return sum(
            pricing.cost(
                response.get("model"),
                input_tokens=int(response.get("input_tokens") or 0),
                output_tokens=int(response.get("output_tokens") or 0),
            )
            for response in self.burned
        )


async def attack_a_runaway(config: EconomicsConfig, base_url: str) -> dict[str, Any]:
    """A loop with no brakes, priced twice: without the controller and with it."""
    tier = config.ladder.most_capable

    # --- before the control: no budget, no policy, just a loop --------------
    raw = MeteredTransport(GatewayClient(base_url))
    uncontrolled_cost, uncontrolled_calls = 0.0, 0
    for _ in range(UNCONTROLLED_SAMPLE):
        try:
            response = await raw.chat(
                prompt=ATTACK_PROMPT, system=ATTACK_SYSTEM,
                request=config.ladder.request_for(tier),
            )
        except Exception:
            continue
        uncontrolled_calls += 1
        uncontrolled_cost += config.pricing.cost(
            response.get("model"),
            input_tokens=int(response.get("input_tokens") or 0),
            output_tokens=int(response.get("output_tokens") or 0),
        )
    per_call = (uncontrolled_cost / uncontrolled_calls) if uncontrolled_calls else 0.0

    # --- after the control: the same loop through the controller ------------
    budget = config.budget(principal="nav/s15/adversary", amount=config.default_budget,
                           run_id="p8-runaway")
    metered = MeteredTransport(GatewayClient(base_url))
    controller = BudgetedGateway(metered, budget=budget, policy=config.policy(),
                                 pricing=config.pricing, ladder=config.ladder)
    admitted, refused = 0, 0
    for index in range(LOOP_ROUNDS):
        with call_site(f"runaway#{index}", ATTACK_ROLE):
            try:
                await controller.complete(ATTACK_PROMPT, ATTACK_SYSTEM)
                admitted += 1
            except BudgetRefused:
                refused += 1
            except Exception:
                pass
    return {
        "uncontrolled_sample_calls": uncontrolled_calls,
        "uncontrolled_sample_cost": uncontrolled_cost,
        "uncontrolled_cost_per_call": per_call,
        "uncontrolled_projected_over_loop": per_call * LOOP_ROUNDS,
        "loop_rounds": LOOP_ROUNDS,
        "controlled_spend": budget.spent,
        "controlled_ceiling": budget.total,
        "admitted": admitted,
        "refused": refused,
        "ledger": budget.snapshot(),
    }


async def attack_b_unaffordable(config: EconomicsConfig, base_url: str) -> dict[str, Any]:
    """Demand the top rung on a ceiling that cannot pay for it.

    Two ceilings, derived from the ladder rather than written down: one that
    cannot afford the requested rung but CAN afford a cheaper one (the policy
    must downgrade), and one that cannot afford any rung at all (it must refuse).
    """
    policy = config.policy()
    top = policy.project(config.ladder.most_capable)
    bottom = policy.project(config.ladder.cheapest)
    ceilings = {
        # Comfortably above the cheapest rung, comfortably below the dearest.
        "downgrade_expected": round((bottom * 2 + top) / 3, 8),
        # Below even the cheapest rung's worst case: nothing can be admitted.
        "refuse_expected": round(bottom / 2, 8),
    }
    outcomes: dict[str, Any] = {"projected": {"top": top, "bottom": bottom}, "ceilings": ceilings}
    for name, ceiling in ceilings.items():
        budget = config.budget(principal="nav/s15/adversary", amount=ceiling, run_id=f"p8-{name}")
        metered = MeteredTransport(GatewayClient(base_url))
        controller = BudgetedGateway(metered, budget=budget, policy=policy,
                                     pricing=config.pricing, ladder=config.ladder)
        record: dict[str, Any] = {"ceiling": ceiling}
        with call_site(f"demand#{name}", ATTACK_ROLE, config.ladder.most_capable.name):
            try:
                reply = await controller.complete(ATTACK_PROMPT, ATTACK_SYSTEM)
                record.update({
                    "outcome": reply.get("budget_decision"),
                    "served_tier": reply.get("tier"),
                    "served_model": reply.get("model"),
                    "cost": reply.get("cost"),
                })
            except BudgetRefused as refused:
                record.update({"outcome": "refuse", "reason": str(refused), "cost": 0.0})
        record["spent"] = budget.spent
        record["refusals"] = list(budget.refusals)
        outcomes[name] = record
    return outcomes


async def attack_c_full_climb(config: EconomicsConfig, base_url: str) -> dict[str, Any]:
    """Escalate on every verdict until the ladder, then the attempt ceiling, runs out."""
    budget = config.budget(principal="nav/s15/p1", amount=config.default_budget, run_id="p8-climb")
    metered = MeteredTransport(GatewayClient(base_url))
    controller = BudgetedGateway(metered, budget=budget, policy=config.policy(),
                                 pricing=config.pricing, ladder=config.ladder)
    names = list(config.ladder.names)
    climbed: list[dict[str, Any]] = []
    # One node id throughout: the per-NODE call ceiling is what has to stop this,
    # not the per-run one, because a retrying node is the shape a cascade takes.
    node_id = "climb#node"
    for index in range(len(names) + 3):  # deliberately more attempts than rungs
        rung = names[min(index, len(names) - 1)]
        with call_site(node_id, ATTACK_ROLE, rung):
            try:
                reply = await controller.complete(ATTACK_PROMPT, ATTACK_SYSTEM)
                climbed.append({"attempt": index + 1, "requested": rung,
                                "served": reply.get("tier"), "model": reply.get("model"),
                                "decision": reply.get("budget_decision"), "cost": reply.get("cost")})
            except BudgetRefused as refused:
                climbed.append({"attempt": index + 1, "requested": rung, "served": None,
                                "refused": str(refused)})
            except Exception as failure:
                climbed.append({"attempt": index + 1, "requested": rung,
                                "error": f"{type(failure).__name__}: {failure}"})
    return {
        "attempts": climbed,
        "rungs_served": [row.get("served") for row in climbed if row.get("served")],
        "distinct_rungs_served": sorted({row["served"] for row in climbed if row.get("served")}),
        "refused": sum(1 for row in climbed if "refused" in row),
        "spent": budget.spent,
        "ceiling": budget.total,
        "max_calls_per_node": config.thresholds.max_calls_per_node,
        "ledger": budget.snapshot(),
    }


async def _burn_arm(
    config: EconomicsConfig, base_url: str, *, rounds: int, thresholds: PolicyThresholds, label: str
) -> dict[str, Any]:
    """Drive the failing provider `rounds` times under one set of thresholds."""
    budget = config.budget(principal="nav/s15/adversary", amount=config.default_budget,
                           run_id=f"p8-burn-{label}")
    faulty = FailsAfterTokens(GatewayClient(base_url))
    metered = MeteredTransport(faulty)
    controller = BudgetedGateway(
        metered, budget=budget, policy=BudgetPolicy(config.ladder, config.pricing, thresholds),
        pricing=config.pricing, ladder=config.ladder,
    )
    refused, errored = 0, 0
    for _ in range(rounds):
        # One node id throughout, so the per-node ceilings are the ones on trial.
        with call_site("burner#node", ATTACK_ROLE, config.ladder.cheapest.name):
            try:
                await controller.complete(ATTACK_PROMPT, ATTACK_SYSTEM)
            except BudgetRefused:
                refused += 1
            except Exception:
                errored += 1
    return {
        "rounds": rounds,
        "calls_that_reached_the_provider": len(faulty.burned),
        "transport_failures": metered.failures,
        "ledger_charges": len(budget.charges),
        "ledger_spent": budget.spent,
        "attempts_counted": budget.attempts,
        "real_tokens_consumed": {
            "input": sum(int(r.get("input_tokens") or 0) for r in faulty.burned),
            "output": sum(int(r.get("output_tokens") or 0) for r in faulty.burned),
        },
        "unbilled_spend": faulty.unbilled(config.pricing),
        "refused": refused,
        "errored": errored,
        "refusal_reasons": [entry.get("reason") for entry in budget.refusals[:3]],
    }


async def attack_d_fail_after_tokens(config: EconomicsConfig, base_url: str) -> dict[str, Any]:
    """The spend before the control, and the refusal after it — same attack, twice.

    ``before`` runs with the attempt ceilings switched off, which is exactly the
    policy as it stood when this attack was written and as every config that
    predates it still stands. ``after`` runs with the ceilings this repository
    now configures. Nothing else differs: same provider, same rounds, same
    prompt, same ladder.
    """
    rounds = 8
    disabled = replace(config.thresholds, max_attempts_per_run=0, max_attempts_per_node=0)
    before = await _burn_arm(config, base_url, rounds=rounds, thresholds=disabled, label="before")
    after = await _burn_arm(config, base_url, rounds=rounds, thresholds=config.thresholds,
                            label="after")
    return {
        "before": before,
        "after": after,
        "thresholds": {
            "before": {"max_attempts_per_run": 0, "max_attempts_per_node": 0},
            "after": {"max_attempts_per_run": config.thresholds.max_attempts_per_run,
                      "max_attempts_per_node": config.thresholds.max_attempts_per_node},
        },
        "unbilled_avoided": before["unbilled_spend"] - after["unbilled_spend"],
        # What the ceiling buys in the worst case: the exposure is now bounded by
        # the number of attempts times the dearest call the ladder can make.
        "bounded_exposure": (
            config.thresholds.max_attempts_per_run
            * config.policy().project(config.ladder.most_capable)
        ),
    }


async def run_attacks(config: EconomicsConfig, base_url: str) -> dict[str, Any]:
    print("  A runaway loop ...", flush=True)
    a = await attack_a_runaway(config, base_url)
    print("  B unaffordable tier ...", flush=True)
    b = await attack_b_unaffordable(config, base_url)
    print("  C whole-ladder climb ...", flush=True)
    c = await attack_c_full_climb(config, base_url)
    print("  D provider fails after consuming tokens ...", flush=True)
    d = await attack_d_fail_after_tokens(config, base_url)
    return {"a": a, "b": b, "c": c, "d": d}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--config-dir", default=os.getenv("S15_CONFIG_DIR"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--otel-endpoint", default=os.getenv("S15_OTEL_EXPORTER_ENDPOINT"))
    parser.add_argument("--label", default="")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config = EconomicsConfig.load(parsed.config_dir)
    base_url = parsed.base_url.rstrip("/")
    args = Args(
        task="four attacks on the navigation budget policy", budget=config.default_budget,
        principal="nav/s15/adversary", offline=False, base_url=base_url,
        otel_endpoint=parsed.otel_endpoint or None, respond_as="text",
        config_dir=parsed.config_dir, live_embeddings=False, label=parsed.label,
    )
    proof = Proof(name="p8_adversarial", args=args, mode="live", mode_detail={"base_url": base_url})

    print(f"\np8: attacking the {' < '.join(config.ladder.names)} ladder at {base_url}\n")
    started = time.time()
    results = asyncio.run(run_attacks(config, base_url))
    elapsed = time.time() - started
    a, b, c, d = results["a"], results["b"], results["c"], results["d"]

    # --- A: the runaway loop ------------------------------------------------
    proof.fact("A before the control", (
        f"{a['uncontrolled_sample_calls']} uncontrolled calls cost ${a['uncontrolled_sample_cost']:.8f} "
        f"(${a['uncontrolled_cost_per_call']:.8f}/call) -> ${a['uncontrolled_projected_over_loop']:.6f} "
        f"projected over {a['loop_rounds']} rounds, and nothing would have stopped it"
    ))
    proof.fact("A after the control", (
        f"{a['loop_rounds']} rounds, {a['admitted']} admitted, {a['refused']} refused, "
        f"spent ${a['controlled_spend']:.8f} of ${a['controlled_ceiling']:.8f}"
    ))
    proof.check("A the runaway loop is stopped by the controller, not by luck",
                a["refused"] > 0 and a["controlled_spend"] <= a["controlled_ceiling"],
                f"{a['refused']} refusals, spent {a['controlled_spend']:.8f} "
                f"<= ceiling {a['controlled_ceiling']:.8f}")
    proof.check("A the controlled loop costs less than the uncontrolled one would have",
                a["controlled_spend"] < a["uncontrolled_projected_over_loop"],
                f"{a['controlled_spend']:.8f} < {a['uncontrolled_projected_over_loop']:.8f}")

    # --- B: the unaffordable tier -------------------------------------------
    for name in ("downgrade_expected", "refuse_expected"):
        row = b[name]
        proof.fact(f"B {name}", (
            f"ceiling ${row['ceiling']:.8f} -> {row['outcome']}"
            + (f", served {row.get('served_tier')} ({row.get('served_model')})"
               if row.get("served_tier") else "")
            + f", spent ${row['spent']:.8f}"
        ))
    proof.check("B a ceiling that cannot pay for the requested rung downgrades rather than refusing",
                b["downgrade_expected"]["outcome"] in ("downgrade", "branch"),
                b["downgrade_expected"]["outcome"])
    proof.check("B a ceiling that cannot pay for ANY rung refuses rather than overspending",
                b["refuse_expected"]["outcome"] == "refuse" and b["refuse_expected"]["spent"] == 0.0,
                f"{b['refuse_expected']['outcome']}, spent {b['refuse_expected']['spent']}")

    # --- C: the whole-ladder climb ------------------------------------------
    proof.fact("C ladder climb", " -> ".join(
        f"{row['requested']}:{row.get('served') or 'REFUSED'}" for row in c["attempts"]
    ))
    proof.fact("C stopped by", (
        f"{c['refused']} refusals after {len(c['rungs_served'])} admitted calls on one node "
        f"(max_calls_per_node={c['max_calls_per_node']}), spent ${c['spent']:.8f}"
    ))
    proof.check("C the cascade climbs its whole ladder",
                len(c["distinct_rungs_served"]) == len(config.ladder.names),
                f"{c['distinct_rungs_served']} of {list(config.ladder.names)}")
    proof.check("C a node that keeps escalating is stopped by the per-node call ceiling",
                c["refused"] > 0 and len(c["rungs_served"]) <= c["max_calls_per_node"],
                f"{len(c['rungs_served'])} admitted <= {c['max_calls_per_node']}, {c['refused']} refused")

    # --- D: the provider that fails after consuming tokens ------------------
    before, after = d["before"], d["after"]
    proof.fact("D BEFORE the control (attempt ceilings off)", (
        f"{before['rounds']} rounds -> {before['calls_that_reached_the_provider']} calls reached the "
        f"provider and burned {before['real_tokens_consumed']['input']}in/"
        f"{before['real_tokens_consumed']['output']}out = ${before['unbilled_spend']:.8f} of real "
        f"tokens, while the ledger recorded {before['ledger_charges']} charges and "
        f"${before['ledger_spent']:.8f}. Nothing refused it: {before['refused']} refusals."
    ))
    proof.fact("D AFTER the control (attempt ceilings on)", (
        f"{after['rounds']} rounds -> {after['calls_that_reached_the_provider']} calls reached the "
        f"provider (${after['unbilled_spend']:.8f} burned), then {after['refused']} refusals. "
        f"Reason: {(after['refusal_reasons'] or ['-'])[0]}"
    ))
    proof.fact("D what the ceiling bought", (
        f"${d['unbilled_avoided']:.8f} of unbilled spend avoided over {before['rounds']} rounds; "
        f"worst-case exposure now bounded at {config.thresholds.max_attempts_per_run} attempts x "
        f"${config.policy().project(config.ladder.most_capable):.8f} = ${d['bounded_exposure']:.6f} "
        f"instead of unbounded"
    ))
    proof.check("D before the control, every round reached the provider unbilled",
                before["calls_that_reached_the_provider"] == before["rounds"]
                and before["unbilled_spend"] > 0 and before["ledger_spent"] == 0.0,
                f"{before['calls_that_reached_the_provider']}/{before['rounds']} calls, "
                f"${before['unbilled_spend']:.8f} unbilled, ledger ${before['ledger_spent']:.8f}")
    proof.check("D after the control, the attempt ceiling cuts the provider off",
                after["calls_that_reached_the_provider"] < before["calls_that_reached_the_provider"]
                and after["refused"] > 0,
                f"{after['calls_that_reached_the_provider']} calls then {after['refused']} refusals, "
                f"against {before['calls_that_reached_the_provider']} unbounded")
    proof.check("D the ledger still never charges for a call it saw no response for",
                before["ledger_charges"] == 0 and after["ledger_charges"] == 0,
                f"before {before['ledger_charges']}, after {after['ledger_charges']} charges")
    proof.check("D the fix bounds the exposure it cannot bill",
                d["unbilled_avoided"] > 0, f"${d['unbilled_avoided']:.8f} avoided")

    # --- the refusals have to be visible, not just counted ------------------
    journal = {
        "run_id": "p8-runaway",
        "finished": True,
        "nodes": {},
        "edges": (),
        "events": [{"sequence": 1, "kind": "run_started", "node_id": None, "payload": {}}],
    }
    export = export_run(journal, budget=a["ledger"], endpoint=args.otel_endpoint,
                        principal=args.principal)
    root = export.as_dict()["spans"][0]
    refusal_events = [event for event in (root.get("events") or []) if event[0] == "budget.refused"]
    proof.fact("telemetry", (
        f"root span carries s15.budget.refusals={root['attributes'].get('s15.budget.refusals')} "
        f"and {len(refusal_events)} budget.refused span events"
    ))
    proof.check("every refusal is visible in the trace, not only in the ledger",
                len(refusal_events) == a["refused"] and a["refused"] > 0,
                f"{len(refusal_events)} span events for {a['refused']} refusals")

    proof.fact("wall clock", f"{elapsed:.1f}s")
    proof.record("attacks", results)
    proof.record("economics", config.describe())
    proof.record("refusal_span_events", refusal_events[:20])
    return proof


def main() -> None:
    parsed = parse_args()
    proof = run(parsed)
    OUT.mkdir(parents=True, exist_ok=True)
    sys.exit(proof.finish())


if __name__ == "__main__":
    main()
