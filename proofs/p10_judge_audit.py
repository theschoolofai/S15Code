#!/usr/bin/env python
"""p10 — audit the judge, because every cost per resolved task depends on it.

`p1` divides money by the number of tasks a panel of language models called
resolved. That makes the panel the load-bearing component of the entire
measurement: if it is 20% too generous, every cost per resolved task in the
README is 20% too low, and no amount of careful metering downstream repairs it.
"The judge is generic and the rubric is principled" is an argument. This is a
measurement.

Real answers are collected at each rung of the ladder, then labelled twice:

    mechanically  by proofs/keys/navigation_key.jsonl, which knows the actual
                  right answer to each task and nothing else about it
    by the panel  exactly as p1 asks it, same rubric, same threshold

and the two labellings are compared. The two directions of disagreement are
reported separately because they break the measurement in opposite ways:

    FALSE RESOLVE   the panel passed a wrong answer. Resolution rates come out
                    too high and cost per resolved task too LOW — the flattering
                    error, and the one worth being suspicious of.
    FALSE UNRESOLVE the panel failed a right answer. Costs come out too HIGH,
                    and a cascade looks more necessary than it is.

The per-member agreement matters as much as the panel's: this panel pairs a 14B
local model with a hosted one, and knowing which of them the disagreements come
from is the difference between a disclosed weakness and an unexamined one.

    uv run python proofs/p10_judge_audit.py --config-dir config/navigation \\
        --tasks proofs/tasks/navigation.jsonl --key proofs/keys/navigation_key.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OUT, Args, Proof  # noqa: E402
from p1_cost_per_task import ANSWER_ROLE, ANSWER_SYSTEM  # noqa: E402

from s15code.economics import (  # noqa: E402
    BudgetedGateway,
    BudgetRefused,
    EconomicsConfig,
    MeteredTransport,
    call_site,
)
from s15code.evals import EvalsConfig, RubricJudge  # noqa: E402
from s15code.evals.tasks import load_tasks  # noqa: E402
from s15code.gateway import GatewayClient  # noqa: E402

DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")
DEFAULT_TASKS = Path(__file__).resolve().parent / "tasks" / "navigation.jsonl"
DEFAULT_KEY = Path(__file__).resolve().parent / "keys" / "navigation_key.jsonl"


def load_key(path: str | Path) -> dict[str, dict[str, Any]]:
    """The answer key, as data. Nothing but this proof ever reads it."""
    entries: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        record = json.loads(stripped)
        entries[str(record["id"])] = record
    return entries


def key_verdict(entry: dict[str, Any], answer: str) -> tuple[bool, list[str]]:
    """Mechanically: is this answer right? Returns the label and what decided it."""
    if not answer.strip():
        return False, ["empty answer"]
    reasons: list[str] = []
    ok = True
    for pattern in entry.get("must") or []:
        if not re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
            ok = False
            reasons.append(f"missing: {pattern}")
    for pattern in entry.get("must_not") or []:
        if re.search(pattern, answer, re.IGNORECASE | re.DOTALL):
            ok = False
            reasons.append(f"forbidden: {pattern}")
    return ok, reasons


async def collect(
    *, tasks, rungs, config: EconomicsConfig, evals: EvalsConfig, base_url: str, key
) -> list[dict[str, Any]]:
    client = GatewayClient(base_url)
    transport = MeteredTransport(client)
    judge = RubricJudge(client, evals.rubric, pricing=config.pricing, panel=evals.rubric.panel)
    rows: list[dict[str, Any]] = []
    for task in tasks:
        entry = key.get(task.id)
        if entry is None:
            print(f"  {task.id}: no key entry, skipped", flush=True)
            continue
        for rung in rungs:
            budget = config.budget(principal="nav/s15/p10", amount=config.default_budget,
                                   run_id=f"p10-{task.id}-{rung}")
            controller = BudgetedGateway(transport, budget=budget, policy=config.policy(),
                                         pricing=config.pricing, ladder=config.ladder)
            answer, error = "", None
            with call_site(f"{task.id}#{rung}", ANSWER_ROLE, rung):
                try:
                    reply = await controller.complete(task.task, ANSWER_SYSTEM)
                    answer = str(reply.get("text") or "")
                except BudgetRefused as refused:
                    error = f"budget refused: {refused}"
                except Exception as failure:
                    error = f"{type(failure).__name__}: {failure}"
            objective, why = key_verdict(entry, answer)
            verdict = await judge.judge(
                task=task.task, answer=answer, expectation=task.expectation, task_id=task.id,
                answer_provider=None, answer_model=None, error=error,
            )
            per_judge = {
                sample.judge: sample.resolved for sample in verdict.samples
            }
            rows.append({
                "task_id": task.id, "difficulty": task.difficulty, "rung": rung,
                "model": config.ladder.tier(rung).model,
                "answer_chars": len(answer), "error": error,
                "cost": budget.spent,
                "key_resolved": objective, "key_reasons": why,
                "judge_resolved": bool(verdict.resolved),
                "judge_overall": verdict.overall, "judge_status": verdict.status,
                "agreement": verdict.agreement, "disputed": verdict.disputed,
                "per_judge": per_judge,
                "per_judge_overall": {s.judge: s.overall for s in verdict.samples},
                "answer": answer,
                "judge_notes": {s.judge: s.notes[:200] for s in verdict.samples},
            })
            mark = "==" if objective == bool(verdict.resolved) else "!!"
            print(f"  {mark} {task.id:8} {rung:9} key={'PASS' if objective else 'FAIL'} "
                  f"judge={'PASS' if verdict.resolved else 'FAIL'} "
                  f"overall={verdict.overall if verdict.overall is None else round(verdict.overall, 3)} "
                  f"per_judge={per_judge}", flush=True)
    await client.close()
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS))
    parser.add_argument("--key", default=str(DEFAULT_KEY))
    parser.add_argument("--config-dir", default=os.getenv("S15_CONFIG_DIR"))
    parser.add_argument("--rungs", default="", help="comma-separated tiers; default is every rung")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--label", default="")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config = EconomicsConfig.load(parsed.config_dir)
    evals = EvalsConfig.load(parsed.config_dir)
    tasks = load_tasks(parsed.tasks)
    key = load_key(parsed.key)
    rungs = ([r.strip() for r in parsed.rungs.split(",") if r.strip()]
             if parsed.rungs else list(config.ladder.names))

    args = Args(
        task=f"judge audit over {len(tasks)} tasks x {len(rungs)} rungs", budget=config.default_budget,
        principal="nav/s15/p10", offline=False, base_url=parsed.base_url.rstrip("/"),
        otel_endpoint=None, respond_as="text", config_dir=parsed.config_dir,
        live_embeddings=False, label=parsed.label,
    )
    proof = Proof(name="p10_judge_audit", args=args, mode="live",
                  mode_detail={"base_url": args.base_url})

    print(f"\np10: {len(tasks)} tasks x {len(rungs)} rungs, panel "
          f"{[m.name for m in evals.rubric.panel]}\n")
    started = time.time()
    rows = asyncio.run(collect(tasks=tasks, rungs=rungs, config=config, evals=evals,
                               base_url=args.base_url, key=key))
    elapsed = time.time() - started

    graded = [row for row in rows if row["judge_status"] != "judge_failed"]
    agree = [row for row in graded if row["key_resolved"] == row["judge_resolved"]]
    false_resolve = [row for row in graded if row["judge_resolved"] and not row["key_resolved"]]
    false_unresolve = [row for row in graded if not row["judge_resolved"] and row["key_resolved"]]

    proof.fact("answers graded", f"{len(graded)} of {len(rows)} ({len(rows) - len(graded)} judge failures)")
    proof.fact("panel agrees with the key", (
        f"{len(agree)}/{len(graded)} = {len(agree) / len(graded):.1%}" if graded else "n/a"
    ))
    proof.fact("FALSE RESOLVE (panel passed a wrong answer)", (
        f"{len(false_resolve)} — inflates resolution rate, UNDERSTATES cost per resolved task: "
        + ", ".join(f"{row['task_id']}/{row['rung']}" for row in false_resolve[:8])
    ))
    proof.fact("FALSE UNRESOLVE (panel failed a right answer)", (
        f"{len(false_unresolve)} — deflates resolution rate, OVERSTATES cost per resolved task: "
        + ", ".join(f"{row['task_id']}/{row['rung']}" for row in false_unresolve[:8])
    ))

    # Per-member agreement with the key: which half of the panel is the weak one.
    members = sorted({name for row in graded for name in row["per_judge"]})
    for member in members:
        scored = [row for row in graded if row["per_judge"].get(member) is not None]
        hits = [row for row in scored if row["per_judge"][member] == row["key_resolved"]]
        proof.fact(f"judge {member} vs key", (
            f"{len(hits)}/{len(scored)} = {len(hits) / len(scored):.1%}" if scored else "no samples"
        ))
    disputed = [row for row in graded if row["disputed"]]
    proof.fact("panel members disagreed with each other", f"{len(disputed)}/{len(graded)}")

    by_rung = {}
    for rung in rungs:
        subset = [row for row in graded if row["rung"] == rung]
        if subset:
            by_rung[rung] = {
                "key_resolution_rate": sum(1 for r in subset if r["key_resolved"]) / len(subset),
                "judge_resolution_rate": sum(1 for r in subset if r["judge_resolved"]) / len(subset),
            }
    proof.fact("resolution rate by rung, key vs panel", json.dumps(
        {rung: {k: round(v, 3) for k, v in stats.items()} for rung, stats in by_rung.items()}
    ))
    proof.fact("wall clock", f"{elapsed:.1f}s")

    proof.check("every task in the set has a key entry",
                all(task.id in key for task in tasks),
                sorted({task.id for task in tasks} - set(key)) or "all keyed")
    proof.check("the panel produced a verdict for every answer",
                len(graded) == len(rows), f"{len(rows) - len(graded)} judge failures")
    proof.check("the panel is not systematically generous: false resolves are the minority",
                len(false_resolve) <= len(graded) * 0.25 if graded else False,
                f"{len(false_resolve)} false resolves of {len(graded)} graded")

    proof.record("rows", rows)
    proof.record("false_resolve", false_resolve)
    proof.record("false_unresolve", false_unresolve)
    proof.record("by_rung", by_rung)
    proof.record("agreement", {
        "graded": len(graded), "agree": len(agree),
        "false_resolve": len(false_resolve), "false_unresolve": len(false_unresolve),
    })
    return proof


def main() -> None:
    parsed = parse_args()
    proof = run(parsed)
    OUT.mkdir(parents=True, exist_ok=True)
    sys.exit(proof.finish())


if __name__ == "__main__":
    main()
