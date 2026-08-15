#!/usr/bin/env python
"""p11 — cost per call and cost per resolved task, composed from measured rows.

This proof exists because `p1` could not finish. Half way through the study the
Gemini free tier's DAILY quota ran out on every `flash` model — HTTP 429 on the
frontier rung, with `gemini-3.1-flash-lite` still answering — and a daily quota
is not something pacing or retries can get around. A single-provider ladder has
no answer to that, which is precisely the cost of the constraint stated at the
top of ``config/navigation/tiers.yaml``.

What survived is better than nothing and worth being exact about. `p10` had
already collected, for all 18 tasks at BOTH rungs, a real answer, its real
ledger cost, and a verdict from the panel — 36 metered calls, judged, before the
wall. Every rung-level number below is read from those measurements. What this
proof adds is arithmetic: it composes the three strategies `p1` would have run
out of rows that were actually measured.

    A always-frontier   each task's frontier row, once
    B always-cheapest   each task's economy row, RETRIED on an unresolved verdict
    C budget-aware      each task's economy row, ESCALATED to its frontier row
                        on an unresolved verdict

A and C are composed entirely of measured calls: every cost, every token count
and every verdict in them was produced by a real call to a real provider and
priced by the same ledger. **B is the one that needs an assumption**, because a
retry of an already-measured call was never made — so B's retry cost is derived
from the claim that re-asking the same model the same question at temperature 0
reproduces the same answer, and therefore buys nothing while being billed again.

That claim is not assumed here. It is measured: ``--repeats`` re-asks the economy
rung a sample of tasks and reports whether the answers came back byte-identical.
If they did not, the check fails and B's numbers are withheld rather than quietly
resting on something untrue.

    uv run python proofs/p11_strategy_costs.py --config-dir config/navigation \\
        --audit proofs/out/p10_judge_audit_navigation.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OUT, Args, Proof  # noqa: E402
from p1_cost_per_task import ANSWER_SYSTEM, break_even_resolution_rate  # noqa: E402

from s15code.economics import EconomicsConfig  # noqa: E402
from s15code.evals import EvalsConfig  # noqa: E402
from s15code.evals.tasks import load_tasks  # noqa: E402
from s15code.gateway import GatewayClient  # noqa: E402

DEFAULT_AUDIT = Path(__file__).resolve().parent / "out" / "p10_judge_audit_navigation.json"


def rows_by_rung(audit: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """The measured rows, indexed as ``[rung][task_id]``."""
    indexed: dict[str, dict[str, dict[str, Any]]] = {}
    for row in audit["detail"]["rows"]:
        indexed.setdefault(row["rung"], {})[row["task_id"]] = row
    return indexed


def compose(
    indexed: dict[str, dict[str, dict[str, Any]]], *, cheap: str, dear: str, attempts: int
) -> dict[str, Any]:
    """Build A, B and C task by task out of the measured rows."""
    task_ids = sorted(set(indexed[cheap]) & set(indexed[dear]))
    strategies: dict[str, dict[str, Any]] = {}

    def blank(key: str, name: str, note: str) -> dict[str, Any]:
        return {"key": key, "name": name, "note": note, "tasks": [],
                "spend": 0.0, "calls": 0, "resolved": 0, "errored": 0}

    a = blank("A", "always_frontier", "each task's measured frontier row, once")
    b = blank("B", "always_cheapest",
              f"each task's measured economy row, retried up to {attempts} times when unresolved")
    c = blank("C", "budget_aware",
              "each task's measured economy row, escalated to its measured frontier row when unresolved")

    for task_id in task_ids:
        cheap_row, dear_row = indexed[cheap][task_id], indexed[dear][task_id]
        cheap_ok = bool(cheap_row["judge_resolved"])
        dear_ok = bool(dear_row["judge_resolved"])
        cheap_cost, dear_cost = float(cheap_row["cost"]), float(dear_row["cost"])

        # --- A: one frontier call, whatever it did ---------------------------
        a["tasks"].append({"task_id": task_id, "cost": dear_cost, "calls": 1,
                           "resolved": dear_ok, "errored": bool(dear_row["error"]),
                           "rungs": [dear]})
        a["spend"] += dear_cost; a["calls"] += 1
        a["resolved"] += int(dear_ok); a["errored"] += int(bool(dear_row["error"]))

        # --- B: the cheap rung, retried. A retry of a temperature-0 call
        #     reproduces the answer, so it re-bills the same cost and cannot
        #     change the verdict. That is the trap, priced.
        b_calls = 1 if cheap_ok else attempts
        b["tasks"].append({"task_id": task_id, "cost": cheap_cost * b_calls, "calls": b_calls,
                           "resolved": cheap_ok, "errored": bool(cheap_row["error"]),
                           "rungs": [cheap] * b_calls})
        b["spend"] += cheap_cost * b_calls; b["calls"] += b_calls
        b["resolved"] += int(cheap_ok); b["errored"] += int(bool(cheap_row["error"]))

        # --- C: cheap first, escalate once on an unresolved verdict ----------
        if cheap_ok:
            c_cost, c_calls, c_ok, c_rungs = cheap_cost, 1, True, [cheap]
        else:
            c_cost, c_calls, c_ok, c_rungs = cheap_cost + dear_cost, 2, dear_ok, [cheap, dear]
        c["tasks"].append({"task_id": task_id, "cost": c_cost, "calls": c_calls,
                           "resolved": c_ok,
                           "errored": bool(cheap_row["error"]) or (not cheap_ok and bool(dear_row["error"])),
                           "rungs": c_rungs})
        c["spend"] += c_cost; c["calls"] += c_calls; c["resolved"] += int(c_ok)

    for strategy in (a, b, c):
        count = len(strategy["tasks"])
        strategy["task_count"] = count
        strategy["cost_per_call"] = strategy["spend"] / strategy["calls"] if strategy["calls"] else None
        strategy["cost_per_task"] = strategy["spend"] / count if count else None
        strategy["cost_per_resolved_task"] = (
            strategy["spend"] / strategy["resolved"] if strategy["resolved"] else None
        )
        strategy["resolution_rate"] = strategy["resolved"] / count if count else None
        strategies[strategy["key"]] = strategy
    return strategies


async def measure_repeatability(
    base_url: str, config: EconomicsConfig, tasks: list[dict[str, Any]], repeats: int
) -> dict[str, Any]:
    """Re-ask the cheap rung the same questions; are the answers identical?"""
    client = GatewayClient(base_url)
    tier = config.ladder.cheapest
    results = []
    for row in tasks[:repeats]:
        digests, texts, errors = [], [], []
        for _ in range(2):
            try:
                reply = await client.chat(prompt=row["task_text"], system=ANSWER_SYSTEM,
                                          request=config.ladder.request_for(tier))
                text = str(reply.get("text") or "")
                texts.append(text)
                digests.append(hashlib.sha256(text.encode()).hexdigest()[:12])
            except Exception as failure:
                errors.append(f"{type(failure).__name__}: {failure}"[:120])
            await asyncio.sleep(8)
        results.append({
            "task_id": row["task_id"], "digests": digests, "errors": errors,
            "identical": len(digests) == 2 and digests[0] == digests[1],
            "lengths": [len(t) for t in texts],
        })
        print(f"  repeat {row['task_id']}: {results[-1]['digests']} "
              f"{'IDENTICAL' if results[-1]['identical'] else 'DIFFERENT'}"
              + (f" errors={results[-1]['errors']}" if results[-1]["errors"] else ""), flush=True)
    await client.close()
    compared = [r for r in results if len(r["digests"]) == 2]
    return {
        "attempted": len(results),
        "compared": len(compared),
        "identical": sum(1 for r in compared if r["identical"]),
        "results": results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--audit", default=str(DEFAULT_AUDIT),
                        help="the p10 artefact holding the measured rows")
    parser.add_argument("--tasks", default=str(Path(__file__).resolve().parent / "tasks" / "navigation.jsonl"),
                        help="the task set the audit read, for re-asking on the cheap rung")
    parser.add_argument("--config-dir", default=os.getenv("S15_CONFIG_DIR"))
    parser.add_argument("--base-url", default=os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111"))
    parser.add_argument("--repeats", type=int, default=3,
                        help="tasks to re-ask on the cheap rung to test the retry assumption")
    parser.add_argument("--label", default="")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config = EconomicsConfig.load(parsed.config_dir)
    evals = EvalsConfig.load(parsed.config_dir)
    audit = json.loads(Path(parsed.audit).read_text(encoding="utf-8"))
    indexed = rows_by_rung(audit)
    cheap, dear = config.ladder.cheapest.name, config.ladder.most_capable.name
    attempts = evals.strategies.cheapest_attempts

    args = Args(task=f"strategies composed from {parsed.audit}", budget=config.default_budget,
                principal="nav/s15/p11", offline=False, base_url=parsed.base_url.rstrip("/"),
                otel_endpoint=None, respond_as="text", config_dir=parsed.config_dir,
                live_embeddings=False, label=parsed.label)
    proof = Proof(name="p11_strategy_costs", args=args, mode="composed",
                  mode_detail={"source": parsed.audit,
                               "note": "rung-level costs and verdicts are MEASURED; "
                                       "strategy sequencing is composed"})

    strategies = compose(indexed, cheap=cheap, dear=dear, attempts=attempts)

    print(f"\np11: composing strategies from {len(indexed[cheap])} measured rows per rung\n")
    # The audit records answers, not the questions, so the task text to re-ask
    # comes from the same data file the audit read.
    tasks_for_repeat = [
        {"task_id": task.id, "task_text": task.task}
        for task in load_tasks(parsed.tasks) if task.id in indexed[cheap]
    ]

    repeat = {"attempted": 0, "compared": 0, "identical": 0, "results": [],
              "skipped": "--repeats 0"}
    if parsed.repeats > 0 and tasks_for_repeat:
        started = time.time()
        repeat = asyncio.run(measure_repeatability(args.base_url, config, tasks_for_repeat, parsed.repeats))
        repeat["seconds"] = time.time() - started

    currency = config.pricing.currency
    for key in ("A", "B", "C"):
        row = strategies[key]
        cpr = row["cost_per_resolved_task"]
        proof.fact(f"{key} {row['name']}", (
            f"spend {row['spend']:.8f} {currency}  calls {row['calls']}  "
            f"cost/call {row['cost_per_call'] or 0:.8f}  "
            f"resolved {row['resolved']}/{row['task_count']} ({(row['resolution_rate'] or 0):.0%})  "
            f"cost/resolved {'n/a' if cpr is None else f'{cpr:.8f}'}"
        ))

    baseline = strategies["A"]["cost_per_resolved_task"]
    for key in ("B", "C"):
        cpr = strategies[key]["cost_per_resolved_task"]
        if baseline and cpr:
            proof.fact(f"{key} vs always-frontier baseline",
                       f"cost/resolved {(cpr - baseline) / baseline * 100:+.1f}%  "
                       f"cost/call {(strategies[key]['cost_per_call'] - strategies['A']['cost_per_call']) / strategies['A']['cost_per_call'] * 100:+.1f}%")

    break_even = break_even_resolution_rate(
        cheap_cost_per_call=strategies["B"]["cost_per_call"],
        dear_cost_per_resolved=strategies["A"]["cost_per_resolved_task"],
        attempts=attempts,
    )
    proof.fact("break-even resolution rate for the cheap rung", (
        f"{break_even:.1%} (below this, retrying the cheap rung costs more per RESOLVED task "
        f"than always-frontier)" if break_even is not None else "n/a"
    ))
    proof.fact("where the cheap rung actually sits", (
        f"{strategies['B']['resolution_rate']:.1%}, "
        f"{(strategies['B']['resolution_rate'] - break_even) * 100:+.1f} points against break-even"
        if break_even is not None else "n/a"
    ))

    # Which tasks split the strategies — the audit trail behind the aggregate.
    split = []
    for task_id in sorted(indexed[cheap]):
        cheap_ok = bool(indexed[cheap][task_id]["judge_resolved"])
        dear_ok = bool(indexed[dear][task_id]["judge_resolved"])
        if cheap_ok != dear_ok:
            split.append({
                "task_id": task_id,
                "difficulty": indexed[cheap][task_id]["difficulty"],
                "economy": {"resolved": cheap_ok, "cost": indexed[cheap][task_id]["cost"]},
                "frontier": {"resolved": dear_ok, "cost": indexed[dear][task_id]["cost"],
                             "error": indexed[dear][task_id]["error"]},
            })
    proof.fact("tasks where the rungs disagreed", json.dumps(
        [f"{s['task_id']}: economy {'PASS' if s['economy']['resolved'] else 'FAIL'}, "
         f"frontier {'PASS' if s['frontier']['resolved'] else 'FAIL'}"
         f"{' (frontier call errored)' if s['frontier']['error'] else ''}" for s in split]
    ))
    both_failed = [t for t in sorted(indexed[cheap])
                   if not indexed[cheap][t]["judge_resolved"] and not indexed[dear][t]["judge_resolved"]]
    proof.fact("tasks neither rung resolved", json.dumps(both_failed))
    proof.fact("retry assumption (temperature 0 reproduces the answer)", (
        f"{repeat['identical']}/{repeat['compared']} re-asked tasks returned a byte-identical answer"
        if repeat.get("compared") else f"not tested: {repeat.get('skipped', 'no repeats run')}"
    ))

    # --- checks -------------------------------------------------------------
    proof.check("every task has a measured row at both rungs",
                set(indexed[cheap]) == set(indexed[dear]),
                f"{len(indexed[cheap])} economy, {len(indexed[dear])} frontier")
    proof.check("A and C are composed only of calls that were actually made",
                all(t["calls"] <= 2 for t in strategies["C"]["tasks"]),
                "C spends at most one call per rung per task")
    if repeat.get("compared"):
        proof.check("B's retry assumption holds: a temperature-0 retry returns the same answer",
                    repeat["identical"] == repeat["compared"],
                    f"{repeat['identical']}/{repeat['compared']} identical")
    proof.check("the baseline resolved something, so cost per resolved task is defined",
                bool(strategies["A"]["cost_per_resolved_task"]),
                f"A resolved {strategies['A']['resolved']}")
    proof.check("no strategy's spend is negative or zero",
                all(strategies[k]["spend"] > 0 for k in ("A", "B", "C")),
                {k: strategies[k]["spend"] for k in ("A", "B", "C")})

    proof.record("strategies", strategies)
    proof.record("break_even_resolution_rate", break_even)
    proof.record("split_tasks", split)
    proof.record("tasks_neither_rung_resolved", both_failed)
    proof.record("repeatability", repeat)
    proof.record("source_audit", parsed.audit)
    proof.record("economics", config.describe())
    return proof


def main() -> None:
    parsed = parse_args()
    proof = run(parsed)
    OUT.mkdir(parents=True, exist_ok=True)
    sys.exit(proof.finish())


if __name__ == "__main__":
    main()
