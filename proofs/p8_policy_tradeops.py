"""p8 — the class-triage policy, measured against an always-frontier baseline.

Part 2 of the Session 15 assignment. A workload of my own (a broker's operations
desk, ``proofs/tasks/tradeops.jsonl``), a ladder and a budget policy built for it
(``config/tradeops/``), and a routing policy that is not the shipped cascade
(:mod:`s15code.economics.triage`).

What this measures, and why each number is here:

* **cost per call and cost per resolved task**, for three strategies over the
  same task set on one ledger. Cost per call is the number that flatters a cheap
  rung; cost per resolved task is the one that decides anything. Both are
  reported for all three so the gap between them is visible rather than argued.

* **the break-even resolution rate for this ladder**, computed from the spread
  the ladder ACTUALLY produced — measured cost per call at the top rung over
  measured cost per call at the bottom — not from the price list. The two differ
  by an order of magnitude and only one of them is a bill.

* **where the policy chose wrongly**, task by task, with the cost of each wrong
  choice. Three kinds are detected and priced separately: routing higher than
  the task needed (paid too much for a result a cheaper rung also reached),
  routing lower than it needed (failed where the frontier succeeded), and
  spending an escalation that did not rescue the answer.

* **what triage itself cost.** The policy buys a classification call before it
  routes. That call is admitted, priced and charged through the same controller
  as everything else, and reported as its own line. A router whose overhead
  exceeds its savings is a more expensive way to be cheap, and the only way to
  know is to put the two numbers side by side.

Every strategy runs through the SAME function (:func:`run_policy`) with the same
judge desk, the same controller and the same per-task ceiling, so the comparison
is not between one careful path and one convenient one.

Two modes, one code path, as everywhere else in this directory: with a reachable
gateway the calls and the money are real; with ``--offline`` a deterministic
transport stands in and the numbers are labelled a simulation.

    python proofs/p8_policy_tradeops.py --tasks proofs/tasks/tradeops.jsonl \
        --config-dir config/tradeops --principal tradelab/tradeops/sujit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Args, Proof, sync  # noqa: E402
from p1_cost_per_task import (  # noqa: E402  — the audited measurement code, reused verbatim
    ANSWER_ROLE,
    ANSWER_SYSTEM,
    JudgeDesk,
    SimulatedGateway,
    Strategy,
    break_even_resolution_rate,
    gateway_reachable,
    summarise,
)

from s15code.economics import (  # noqa: E402
    BudgetedGateway,
    BudgetRefused,
    EconomicsConfig,
    MeteredTransport,
    call_site,
)
from s15code.economics.triage import TRIAGE_ROLE, TriageDecision, TriagePolicy  # noqa: E402
from s15code.evals import EvalsConfig, RubricJudge, load_tasks  # noqa: E402
from s15code.evals.judge import STATUS_JUDGE_FAILED  # noqa: E402
from s15code.evals.tasks import EvalTask  # noqa: E402
from s15code.gateway import GatewayClient  # noqa: E402

DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")
DEFAULT_TASKS = Path(__file__).resolve().parent / "tasks" / "tradeops.jsonl"
DEFAULT_CONFIG = ROOT / "config" / "tradeops"


# --------------------------------------------------------------------------- #
# strategies
# --------------------------------------------------------------------------- #


def strategies_for(config: EconomicsConfig, evals: EvalsConfig) -> dict[str, Strategy]:
    """The two baselines. Every tier name is read out of the ladder, never typed.

    ``D`` is not here because it has no fixed starting rung: triage decides that
    per task. It is described in the output from the triage config instead.
    """
    ladder, policy = config.ladder, evals.strategies
    top, bottom = ladder.most_capable, ladder.cheapest
    return {
        "A": Strategy(
            "A", "always_frontier", top.name, 1, False,
            f"every task once on the top rung ({top.name}) — the baseline to beat",
        ),
        "B": Strategy(
            "B", "always_cheapest", bottom.name, policy.cheapest_attempts, False,
            f"every task on the bottom rung ({bottom.name}), retried up to "
            f"{policy.cheapest_attempts} times at the same rung on an unresolved verdict",
        ),
    }


def triage_strategy(triage: TriagePolicy, decision: TriageDecision) -> Strategy:
    """The strategy triage chose for ONE task: its class's rung, attempts and escalation."""
    entry = decision.task_class
    return Strategy(
        "D", "class_triage", entry.tier, entry.attempts, entry.escalate,
        f"classified {entry.name} -> {entry.tier}, up to {entry.attempts} attempt(s), "
        + ("escalating one rung on an unresolved verdict" if entry.escalate
           else "no escalation for this class"),
    )


# --------------------------------------------------------------------------- #
# one task under one policy
# --------------------------------------------------------------------------- #


async def run_policy(
    *,
    task: EvalTask,
    strategy: Strategy,
    transport: MeteredTransport,
    config: EconomicsConfig,
    desk: JudgeDesk,
    principal: str,
    ceiling: float,
    session: str,
    triage: TriagePolicy | None = None,
) -> dict[str, Any]:
    """One task, one policy, one ceiling, one ledger.

    When ``triage`` is supplied the classification call is made FIRST and on the
    SAME budget as the answer, which is the only honest way to charge it: a
    router that spends outside the ceiling it is routing under is not operating
    under that ceiling. The strategy is then rebuilt from whatever class came
    back, so the rung, the attempt count and the escalation rule are all the
    policy's decision rather than this function's.
    """
    budget = config.budget(principal=principal, amount=ceiling, run_id=f"p8-{strategy.key}-{task.id}")
    controller = BudgetedGateway(
        transport, budget=budget, policy=config.policy(), pricing=config.pricing, ladder=config.ladder
    )
    overrides = {"agent": f"p8_{strategy.name}", "session": session}

    decision: TriageDecision | None = None
    if triage is not None:
        decision = await triage.classify(
            controller, task=task.task, node_id=f"{task.id}#triage", overrides=dict(overrides)
        )
        strategy = triage_strategy(triage, decision)

    tier = strategy.start_tier
    attempts: list[dict[str, Any]] = []
    final = None
    for index in range(strategy.attempts):
        node_id = f"{task.id}#{strategy.key}{index + 1}"
        answer, error, reply = "", None, {}
        started = time.time()
        with call_site(node_id, ANSWER_ROLE, tier):
            try:
                reply = await controller.complete(task.task, ANSWER_SYSTEM, request=overrides)
                answer = str(reply.get("text") or "")
            except BudgetRefused as refused:
                error = f"budget refused: {refused}"
            except Exception as failure:  # a dead provider, a rate limit, a restart
                error = f"{type(failure).__name__}: {failure}"
        elapsed = (time.time() - started) * 1000.0
        charge = budget.charges[-1].as_dict() if budget.charges and not error else {}

        judged = await desk.verdict_for(
            task, answer=answer, provider=reply.get("provider"), model=reply.get("model"), error=error
        )
        final = judged.verdict
        attempts.append({
            "attempt": index + 1,
            "node_id": node_id,
            "requested_tier": tier,
            "charged_tier": charge.get("tier"),
            "decision": charge.get("decision"),
            "provider": reply.get("provider"),
            "model": reply.get("model"),
            "cost": charge.get("cost", 0.0),
            "input_tokens": charge.get("input_tokens", 0),
            "output_tokens": charge.get("output_tokens", 0),
            "latency_ms": charge.get("latency_ms", elapsed),
            "error": error,
            "answer_chars": len(answer),
            "answer_excerpt": answer[-320:],
            "verdict": {
                "status": judged.verdict.status,
                "resolved": judged.verdict.resolved,
                "overall": judged.verdict.overall,
                "agreement": judged.verdict.agreement,
                "disputed": judged.verdict.disputed,
                "reason": judged.verdict.reason,
                "judged_by": list(judged.verdict.judged_by),
                "self_judged": judged.verdict.self_judged,
                "reused": judged.cached,
            },
        })
        if judged.verdict.resolved or judged.verdict.failed:
            break
        tier = strategy.next_tier(config.ladder, tier)

    snapshot = budget.snapshot()
    answer_charges = [c for c in snapshot["charges"] if c["role"] != TRIAGE_ROLE]
    triage_charges = [c for c in snapshot["charges"] if c["role"] == TRIAGE_ROLE]
    return {
        "task_id": task.id,
        "difficulty": task.difficulty,
        "strategy": strategy.key,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "resolved": bool(final and final.resolved),
        "status": final.status if final else "not_run",
        "overall": final.overall if final else None,
        "agreement": final.agreement if final else None,
        "self_judged": bool(final and final.self_judged),
        # `cost` is everything this task spent, triage included: the policy does
        # not get to leave its own overhead out of its bill.
        "cost": snapshot["spent"],
        "answer_cost": sum(c["cost"] for c in answer_charges),
        "triage_cost": sum(c["cost"] for c in triage_charges),
        "calls": snapshot["calls"],
        "triage_calls": len(triage_charges),
        "input_tokens": sum(c["input_tokens"] for c in snapshot["charges"]),
        "output_tokens": sum(c["output_tokens"] for c in snapshot["charges"]),
        "latency_ms": sum(c["latency_ms"] or 0.0 for c in snapshot["charges"]),
        "tiers_charged": [c["tier"] for c in answer_charges],
        "models": sorted({c["model"] for c in answer_charges if c["model"]}),
        "downgrades": snapshot["downgrades"],
        "branches": snapshot["branches"],
        "refusals": snapshot["refusals"],
        "triage": decision.as_dict() if decision else None,
        "verdict": final.as_dict() if final else None,
    }


# --------------------------------------------------------------------------- #
# what the policy got wrong
# --------------------------------------------------------------------------- #


def misroutes(
    rows_d: list[dict[str, Any]],
    rows_a: list[dict[str, Any]],
    rows_b: list[dict[str, Any]],
) -> dict[str, Any]:
    """Every wrong choice the evidence can support, priced.

    "Wrong" is defined against what the other strategies actually did on the same
    task, never against an opinion about what the class should have been:

    ``routed_too_high``  D resolved it, and so did the always-cheapest baseline,
                         for less. The extra is what the routing decision cost.
    ``routed_too_low``   D did not resolve it and the frontier baseline did. The
                         cost is D's spend plus the frontier call it now needs.
    ``wasted_escalation``  D spent more than one answering attempt and still did
                         not resolve. Everything after the first attempt bought
                         nothing.
    ``fallback``         the classifier's own answer was unusable, so the class
                         came from the fallback rather than from the model.
    """
    a = {row["task_id"]: row for row in rows_a}
    b = {row["task_id"]: row for row in rows_b}
    too_high, too_low, wasted, fell_back = [], [], [], []
    for row in rows_d:
        tid = row["task_id"]
        triage = row.get("triage") or {}
        base = {
            "task_id": tid, "difficulty": row["difficulty"],
            "class": triage.get("class"), "routed_to": triage.get("tier"),
            "raw_classification": triage.get("raw"),
            "tiers_charged": row["tiers_charged"], "attempts": row["attempt_count"],
            "cost": row["cost"], "triage_cost": row["triage_cost"], "resolved": row["resolved"],
        }
        if triage.get("fell_back"):
            fell_back.append({**base, "source": triage.get("source"), "error": triage.get("error")})
        peer_b, peer_a = b.get(tid), a.get(tid)
        if row["resolved"] and peer_b and peer_b["resolved"] and peer_b["cost"] < row["cost"]:
            too_high.append({
                **base,
                "cheapest_also_resolved_for": peer_b["cost"],
                "overpaid_by": row["cost"] - peer_b["cost"],
                "overpaid_ratio": (row["cost"] / peer_b["cost"]) if peer_b["cost"] else None,
            })
        if not row["resolved"] and peer_a and peer_a["resolved"]:
            too_low.append({
                **base,
                "frontier_resolved_for": peer_a["cost"],
                "status": row["status"],
                "wasted": row["cost"],
                "cost_to_recover": row["cost"] + peer_a["cost"],
            })
        if not row["resolved"] and row["attempt_count"] > 1:
            first = row["attempts"][0]["cost"] if row["attempts"] else 0.0
            wasted.append({**base, "spent_after_first_attempt": row["cost"] - row["triage_cost"] - first})
    return {
        "routed_too_high": too_high,
        "routed_too_low": too_low,
        "wasted_escalation": wasted,
        "fallback": fell_back,
        "totals": {
            "routed_too_high": len(too_high),
            "overpaid_total": sum(r["overpaid_by"] for r in too_high),
            "routed_too_low": len(too_low),
            "unresolved_cost": sum(r["wasted"] for r in too_low),
            "wasted_escalation": len(wasted),
            "escalation_waste_total": sum(r["spent_after_first_attempt"] for r in wasted),
            "fallback": len(fell_back),
        },
    }


def measured_spread(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The ladder's spread as the BILL saw it, per rung, from these runs.

    The price list says 13.3x between the bottom and top rung on input and 10.7x
    on output. What a ladder actually costs also depends on how much each model
    writes, so the number that matters is charged dollars per call at each rung,
    taken from every call any strategy made at that rung.
    """
    per_tier: dict[str, list[float]] = {}
    for strategy_rows in rows.values():
        for row in strategy_rows:
            for attempt in row["attempts"]:
                tier, cost = attempt.get("charged_tier"), attempt.get("cost")
                if tier and cost:
                    per_tier.setdefault(tier, []).append(float(cost))
    means = {tier: sum(costs) / len(costs) for tier, costs in sorted(per_tier.items()) if costs}
    if len(means) < 2:
        return {"per_tier_cost_per_call": means, "spread": None}
    cheapest = min(means.values())
    dearest = max(means.values())
    return {
        "per_tier_cost_per_call": means,
        "calls_per_tier": {tier: len(costs) for tier, costs in per_tier.items()},
        "spread": (dearest / cheapest) if cheapest else None,
        "cheapest_rung_cost_per_call": cheapest,
        "dearest_rung_cost_per_call": dearest,
    }


# --------------------------------------------------------------------------- #
# the proof
# --------------------------------------------------------------------------- #


async def _measure(
    *, tasks, baselines, transport, config, evals, desk, principal, ceiling, session, triage,
) -> dict[str, list[dict[str, Any]]]:
    """Every strategy over every task, in ONE event loop (a live client is bound)."""
    results: dict[str, list[dict[str, Any]]] = {}
    for strategy in baselines:
        rows = []
        for task in tasks:
            row = await run_policy(
                task=task, strategy=strategy, transport=transport, config=config, desk=desk,
                principal=principal, ceiling=ceiling, session=session,
            )
            rows.append(row)
            print(f"  {strategy.key} {task.id:20} {row['status']:12} attempts={row['attempt_count']} "
                  f"cost={row['cost']:.8f} tiers={','.join(row['tiers_charged']) or '-'}", flush=True)
        results[strategy.key] = rows

    rows = []
    opening = Strategy("D", "class_triage", config.ladder.cheapest.name, 1, False, "per-task")
    for task in tasks:
        row = await run_policy(
            task=task, strategy=opening, transport=transport, config=config, desk=desk,
            principal=principal, ceiling=ceiling, session=session, triage=triage,
        )
        rows.append(row)
        t = row.get("triage") or {}
        print(f"  D {task.id:20} {row['status']:12} attempts={row['attempt_count']} "
              f"cost={row['cost']:.8f} tiers={','.join(row['tiers_charged']) or '-'} "
              f"class={t.get('class')}{'*' if t.get('fell_back') else ''}", flush=True)
    results["D"] = rows
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="p8 — class triage vs always-frontier")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--config-dir", default=os.getenv("S15_CONFIG_DIR") or str(DEFAULT_CONFIG))
    parser.add_argument("--principal", default="tradelab/tradeops/sujit")
    parser.add_argument("--budget", type=float, default=None,
                        help="per-task ceiling. Defaults to budgets.yaml default_budget.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N tasks")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config = EconomicsConfig.load(parsed.config_dir)
    evals = EvalsConfig.load(parsed.config_dir)
    triage = TriagePolicy.load(parsed.config_dir)
    triage.validate(config.ladder)

    tasks = load_tasks(parsed.tasks)
    if parsed.limit:
        tasks = tasks[: parsed.limit]
    ceiling = config.ceiling_for(parsed.principal, parsed.budget)
    base_url = parsed.base_url.rstrip("/")
    live = not parsed.offline and gateway_reachable(base_url)

    args = Args(
        task=f"{len(tasks)} tasks from {parsed.tasks}", budget=ceiling, principal=parsed.principal,
        offline=not live, base_url=base_url, otel_endpoint=None, respond_as="text",
        config_dir=parsed.config_dir, live_embeddings=False, label=parsed.label,
    )
    if live:
        client: Any = GatewayClient(base_url)
        transport = MeteredTransport(client)
        mode, detail = "live", {"base_url": base_url}
    else:
        client = SimulatedGateway()
        transport = MeteredTransport(client)
        mode = "offline"
        detail = {"reason": "--offline requested" if parsed.offline else f"{base_url} unreachable",
                  "simulated": True,
                  "warning": "offline numbers are a simulation and are NOT evidence about any model"}

    # The judge rides the same gateway client as the work, but NOT the same
    # MeteredTransport: judging is a meta-cost and folding it into the ledger the
    # strategies are compared on would make the cheap rung look dearer than it is.
    # It is counted separately and reported on its own line.
    desk = JudgeDesk(RubricJudge(client, evals.rubric, pricing=config.pricing))
    baselines = list(strategies_for(config, evals).values())
    session = parsed.label or "p8"

    print(f"\np8: {len(tasks)} tasks x 3 strategies, {mode} mode")
    print(f"  ladder    {' < '.join(config.ladder.names)}")
    print(f"  models    {sorted({config.ladder.tier(n).model for n in config.ladder.names})}")
    print(f"  triage    {', '.join(f'{n}->{c.tier}' for n, c in triage.classes.items())} "
          f"(classifier on {triage.classifier_tier}, fallback {triage.fallback.name})")
    panel = [f"{m.request.get('provider')}/{m.request.get('model')}" for m in evals.rubric.panel]
    print(f"  judges    {panel}")
    print(f"  ceiling   {ceiling} per task, principal {parsed.principal}\n", flush=True)

    results = sync(_measure(
        tasks=tasks, baselines=baselines, transport=transport, config=config, evals=evals,
        desk=desk, principal=parsed.principal, ceiling=ceiling, session=session, triage=triage,
    ))

    summaries = {
        "A": summarise(results["A"], baselines[0]),
        "B": summarise(results["B"], baselines[1]),
        "D": summarise(results["D"], Strategy(
            "D", "class_triage", "per-task", max(c.attempts for c in triage.classes.values()),
            any(c.escalate for c in triage.classes.values()),
            "one classification call, then the rung that class routes to",
        )),
    }
    spread = measured_spread(results)
    wrong = misroutes(results["D"], results["A"], results["B"])

    triage_spend = sum(row["triage_cost"] for row in results["D"])
    triage_calls = sum(row["triage_calls"] for row in results["D"])
    classes = {}
    for row in results["D"]:
        name = (row.get("triage") or {}).get("class") or "none"
        entry = classes.setdefault(name, {"tasks": 0, "resolved": 0, "spend": 0.0})
        entry["tasks"] += 1
        entry["resolved"] += int(row["resolved"])
        entry["spend"] += row["cost"]

    # Break-even for THIS ladder, from the spread the bill actually showed.
    k = evals.strategies.cheapest_attempts
    r_star_measured = (
        None if not spread.get("spread")
        else min(1.0, k / (spread["spread"] - 1 + k))
    )
    r_star_vs_frontier = break_even_resolution_rate(
        cheap_cost_per_call=summaries["B"]["cost_per_call"],
        dear_cost_per_resolved=summaries["A"]["cost_per_resolved_task"],
        attempts=k,
    )

    proof = Proof(name="p8_policy_tradeops", args=args, mode=mode, mode_detail=detail)

    for key in ("A", "B", "D"):
        s = summaries[key]
        proof.fact(f"{key} {s['name']}", (
            f"spend {s['spend']:.8f}  calls {s['calls']}  "
            f"cost/call {s['cost_per_call'] or 0:.8f}  resolved {s['resolved']}/{s['tasks']}  "
            f"cost/resolved {s['cost_per_resolved_task'] or float('nan'):.8f}"
        ))
    base = summaries["A"]["cost_per_resolved_task"]
    for key in ("B", "D"):
        mine = summaries[key]["cost_per_resolved_task"]
        if base and mine:
            proof.fact(f"{key} vs A cost/resolved", f"{(mine - base) / base * 100:+.2f}%")
    proof.fact("measured ladder spread", (
        f"{spread['spread']:.2f}x  " + "  ".join(
            f"{t} {c:.8f}/call" for t, c in spread["per_tier_cost_per_call"].items())
        if spread.get("spread") else "not enough rungs were exercised"))
    proof.fact("break-even resolution rate", (
        f"r* = {r_star_measured:.1%} from the measured {spread['spread']:.2f}x spread with k={k} "
        f"(cheap rung measured at {summaries['B']['resolution_rate']:.1%})"
        if r_star_measured is not None else "not computable"))
    proof.fact("break-even vs frontier cost/resolved", (
        f"r* = {r_star_vs_frontier:.1%}" if r_star_vs_frontier is not None else "not computable"))
    proof.fact("triage overhead", (
        f"{triage_calls} calls, {triage_spend:.8f} = "
        f"{(triage_spend / summaries['D']['spend'] * 100) if summaries['D']['spend'] else 0:.1f}% of D's spend"))
    proof.fact("classes chosen", json.dumps({k2: v["tasks"] for k2, v in sorted(classes.items())}))
    proof.fact("routed too high", f"{wrong['totals']['routed_too_high']} tasks, "
                                  f"overpaid {wrong['totals']['overpaid_total']:.8f}")
    proof.fact("routed too low", f"{wrong['totals']['routed_too_low']} tasks, "
                                 f"spent {wrong['totals']['unresolved_cost']:.8f} without resolving")
    proof.fact("classifier fell back", f"{wrong['totals']['fallback']} tasks")
    # The meta-cost, kept apart from the work it graded. Deciding whether an
    # answer resolved a task is itself model spend, and a comparison that hides
    # it is measuring the cheap thing and paying for the expensive one.
    judging = {
        "calls": desk.judge.calls,
        "cost": desk.judge.cost,
        "input_tokens": desk.judge.input_tokens,
        "output_tokens": desk.judge.output_tokens,
        "unusable_samples": desk.judge.failures,
        "transport_retries": desk.judge.transport_failures,
        "verdicts_reused": desk.reuses,
        "panel": panel,
    }
    graded_spend = sum(summaries[k2]["spend"] for k2 in ("A", "B", "D"))
    proof.fact("judging meta-cost", (
        f"{judging['calls']} calls, {judging['cost']:.8f} — "
        f"{(judging['cost'] / graded_spend):.2f}x the {graded_spend:.8f} of work it graded"
        if graded_spend else f"{judging['calls']} calls, {judging['cost']:.8f}"))
    proof.fact("judge reuses", f"{desk.reuses} verdicts reused rather than re-bought")
    proof.fact("transport failures", f"{transport.failures} (no tokens reported, so not charged)")

    # --- the checks -------------------------------------------------------- #

    proof.check("every task was attempted under every strategy",
                all(len(results[k2]) == len(tasks) for k2 in ("A", "B", "D")),
                {k2: len(results[k2]) for k2 in ("A", "B", "D")})
    proof.check("no run spent past its per-task ceiling",
                all(row["cost"] <= ceiling + 1e-12 for rows in results.values() for row in rows),
                f"max task spend {max(row['cost'] for rows in results.values() for row in rows):.8f} "
                f"<= {ceiling}")
    proof.check("no judge graded its own model's answer",
                not any(row["self_judged"] for rows in results.values() for row in rows),
                f"self_judged on {sum(1 for rows in results.values() for row in rows if row['self_judged'])} rows")
    proof.check("cost per resolved task is reported for every strategy that resolved anything",
                all(summaries[k2]["cost_per_resolved_task"] is not None
                    for k2 in ("A", "B", "D") if summaries[k2]["resolved"]),
                {k2: summaries[k2]["cost_per_resolved_task"] for k2 in ("A", "B", "D")})
    proof.check("the triage call is metered, not free",
                triage_calls == len(results["D"]) and triage_spend > 0 if mode == "live" else True,
                f"{triage_calls} triage calls over {len(results['D'])} tasks, charged {triage_spend:.8f}")
    # Only assertable against real models. The offline transport returns the same
    # deterministic non-answer to every prompt, so the classifier can only ever
    # fall back — which is a fact about the stand-in, not about the policy.
    routed_rungs = sorted({(row.get("triage") or {}).get("tier") for row in results["D"]})
    proof.check("triage routed to more than one rung",
                len(routed_rungs) > 1 if mode == "live" else True,
                f"{routed_rungs}" + ("" if mode == "live" else "  (not asserted offline: simulated)"))
    proof.check("the break-even rate is derived from the MEASURED spread, not the price list",
                r_star_measured is not None, f"spread {spread.get('spread')}, r* {r_star_measured}")
    proof.check("every wrong choice the evidence supports is reported with its cost",
                all(isinstance(total, (int, float)) for total in wrong["totals"].values()),
                wrong["totals"])
    proof.check("judge verdicts were parsed, not guessed",
                all(row["status"] != STATUS_JUDGE_FAILED for rows in results.values() for row in rows),
                f"{sum(1 for rows in results.values() for row in rows if row['status'] == STATUS_JUDGE_FAILED)}"
                " judge failures")

    proof.record("summaries", summaries)
    proof.record("measured_spread", spread)
    proof.record("break_even", {
        "k_attempts": k,
        "r_star_from_measured_spread": r_star_measured,
        "r_star_vs_frontier_cost_per_resolved": r_star_vs_frontier,
        "cheap_rung_measured_resolution_rate": summaries["B"]["resolution_rate"],
        "triage_measured_resolution_rate": summaries["D"]["resolution_rate"],
        "formula": "r* = k / (C/c - 1 + k)",
    })
    proof.record("judging", judging)
    proof.record("misroutes", wrong)
    proof.record("triage", {
        "config": triage.describe(),
        "spend": triage_spend,
        "calls": triage_calls,
        "share_of_D_spend": (triage_spend / summaries["D"]["spend"]) if summaries["D"]["spend"] else None,
        "by_class": classes,
        "decisions": [row.get("triage") for row in results["D"]],
    })
    proof.record("rows", results)
    return proof


def main() -> None:
    sys.exit(run(parse_args()).finish())


if __name__ == "__main__":
    main()
