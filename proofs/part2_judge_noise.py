#!/usr/bin/env python
"""Is the judge the noise source?

Part 2 saw three tasks flip outcome on an IDENTICAL model+prompt (h05, m04, h04
— a 20% flip rate). Two explanations, with very different fixes:

  * the MODEL answered differently  -> sample more, or accept the variance
  * the JUDGE scored differently    -> the verdict is not a measurement, and no
                                       amount of model sampling repairs it

This isolates the second. One answer is generated per task and then submitted to
the *same* judge panel N times. The answer text is byte-identical across trials,
so every disagreement is the judge's.

Both judges are on free tiers, so this costs nothing but wall clock.

    python proofs/part2_judge_noise.py --trials 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import DEFAULT_BASE_URL, MeteredTransport, gateway_reachable  # noqa: E402

from s15code.economics import EconomicsConfig  # noqa: E402
from s15code.evals import EvalsConfig, RubricJudge, load_tasks  # noqa: E402
from s15code.gateway import GatewayClient  # noqa: E402

# The four tasks that produced every disagreement in Part 2.
DECISIVE = ["m04_swallowed_error", "m05_float_equality",
            "h04_retry_budget", "h05_partial_write"]

SYSTEM = ("You are reviewing a short Python function that contains exactly one "
          "defect. Name the defect and give the corrected code. Be brief.")


async def main_async(args) -> int:
    if not gateway_reachable(args.base_url):
        print(f"ERROR: {args.base_url} unreachable", file=sys.stderr)
        return 2

    econ = EconomicsConfig.load(args.config_dir)
    evals = EvalsConfig.load(args.config_dir)
    tasks = {t.id: t for t in load_tasks(args.tasks)}
    chosen = [tasks[t] for t in DECISIVE if t in tasks]

    transport = MeteredTransport(GatewayClient(args.base_url))
    judge = RubricJudge(transport, evals.rubric, pricing=econ.pricing)

    # The rung whose answers we grade. economy is where the flips happened.
    tier = econ.ladder._tiers[args.tier]
    request = dict(tier.request)

    rows = []
    for task in chosen:
        # ---- one answer, generated once -----------------------------------
        resp = await transport.chat(prompt=task.task, system=SYSTEM, request=request)
        answer = resp.get("text", "")
        print(f"\n=== {task.id} ===", flush=True)
        print(f"  answer: {len(answer)} chars on {resp.get('model')}", flush=True)

        # ---- judged N times, identical input each time --------------------
        trials = []
        for i in range(args.trials):
            v = await judge.judge(task=task.task, answer=answer,
                                  expectation=task.expectation, task_id=task.id)
            d = v.as_dict()
            d["resolved"] = bool(v.resolved)
            d["overall"] = float(v.overall or 0.0)
            d["agreement"] = v.agreement
            trials.append(d)
            print(f"    trial {i+1:>2}: resolved={str(d['resolved']):5s} "
                  f"overall={d['overall']:.4f} agreement={d['agreement']}", flush=True)

        res = [t["resolved"] for t in trials]
        ov = [t["overall"] for t in trials]
        flipped = len(set(res)) > 1
        rows.append({
            "task_id": task.id,
            "model": resp.get("model"),
            "answer_chars": len(answer),
            "answer": answer,
            "trials": trials,
            "resolved_true": sum(res),
            "resolved_false": len(res) - sum(res),
            "verdict_flipped": flipped,
            "overall_min": min(ov), "overall_max": max(ov),
            "overall_mean": statistics.fmean(ov),
            "overall_stdev": statistics.stdev(ov) if len(ov) > 1 else 0.0,
            "distinct_overalls": len(set(round(x, 6) for x in ov)),
        })
        print(f"  -> resolved {sum(res)}/{len(res)}  "
              f"overall {min(ov):.4f}..{max(ov):.4f}  "
              f"{'VERDICT FLIPPED' if flipped else 'verdict stable'}", flush=True)

    out = Path(__file__).resolve().parents[1] / "proofs" / "out" / "part2_judge_noise.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "trials_per_task": args.trials,
        "tier_graded": args.tier,
        "judges": [m.request.get("model") for m in evals.rubric.panel],
        "bar": evals.rubric.threshold,
        "min_criterion": evals.rubric.min_criterion,
        "tie_break": evals.rubric.tie_break,
        "rows": rows,
    }, indent=1, default=str))

    print("\n" + "=" * 66)
    print("JUDGE NOISE — identical answer, judged repeatedly")
    print("=" * 66)
    print(f"  trials per task   {args.trials}")
    print(f"  judges            {[m.request.get('model') for m in evals.rubric.panel]}")
    print(f"  bar/floor/tie     {evals.rubric.threshold} / "
          f"{evals.rubric.min_criterion} / {evals.rubric.tie_break}")
    print()
    print(f"  {'task':24s} {'resolved':>10s} {'overall range':>22s} {'stdev':>8s}  flip?")
    print("  " + "-" * 76)
    flips = 0
    for r in rows:
        flips += bool(r["verdict_flipped"])
        rng = f"{r['overall_min']:.4f}..{r['overall_max']:.4f}"
        print(f"  {r['task_id']:24s} {r['resolved_true']:>4d}/{args.trials:<5d} "
              f"{rng:>22s} {r['overall_stdev']:>8.4f}  "
              f"{'YES' if r['verdict_flipped'] else 'no'}")
    print()
    print(f"  tasks whose VERDICT flipped on identical input: {flips}/{len(rows)}")
    print(f"  judge calls made: {transport.calls} (all free-tier providers)")
    print(f"\n  -> {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="measure judge variance on identical input")
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--tier", default="economy")
    p.add_argument("--tasks", default="proofs/tasks/codereview.jsonl")
    p.add_argument("--config-dir", default="config/part2")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
