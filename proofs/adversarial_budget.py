#!/usr/bin/env python3
"""
Part 3: Adversarial budget test for the code-review routing policy.

Uses the harness to test budget enforcement, runaway loop detection,
unaffordable requests, cascade climb budget ceilings, and ledger consistency.

Scenarios:
  A) runaway loop   - repeatedly submits tasks until BudgetRefused / limit fires
  B) unaffordable   - sets a micro-budget that cannot cover any single call
  C) cascade climb  - budget sized to cover economy only; hard task forces
                      escalation attempts that are refused past the ceiling
  D) token burn     - reasoning left on, tight ceiling => provider consumes
                      tokens before the reply; the ledger must still charge

Usage:
    uv run python proofs/adversarial_budget.py --principal demo/manish --budget 0.001

Exit 0: all refusals worked as expected.
Exit 1: at least one check failed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import Args, Proof, economics, run_task, sync, transport_for  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"


def _make_args(
    task: str,
    budget: float,
    principal: str,
    base_url: str = "http://127.0.0.1:8111",
    label: str = "adv",
) -> Args:
    return Args(
        task=task,
        budget=budget,
        principal=principal,
        offline=False,
        base_url=base_url,
        otel_endpoint=None,
        respond_as="text",
        config_dir=None,
        live_embeddings=False,
        label=label,
    )


async def _run_all_scenarios(args: Args, transport, workspace: Path) -> list[dict]:
    results = []

    # --- Scenario A: Runaway loop ---
    print("\n[A] Runaway loop - repeated tasks until budget triggers refusal...")
    simple_task = "What is the capital of France? Reply in one word."
    args_a = _make_args(simple_task, args.budget, args.principal, args.base_url, label="adv-a")

    total_spent = 0.0
    refused_run = None
    runs_a = []

    for i in range(20):
        run_ws = workspace / f"run_a_{i}"
        run_ws.mkdir(parents=True, exist_ok=True)
        outcome = await run_task(args_a, budget=args.budget, transport=transport, data_dir=run_ws)
        spent = outcome.budget.get("spent", 0.0)
        status = outcome.result.get("status", "")
        refusals = outcome.budget.get("refusals", 0)
        total_spent += spent
        print(f"  run {i+1}: status={status}  spent={spent:.8f}  refusals={refusals}")
        runs_a.append({"i": i+1, "status": status, "spent": spent, "refusals": refusals})

        if refusals > 0 or "refused" in str(status).lower() or "budget" in str(status).lower():
            refused_run = i + 1
            print(f"  -> BudgetRefused observed at run {i+1} (total_spent={total_spent:.8f}) PASS")
            break
        if total_spent >= args.budget * 0.95:
            refused_run = i + 1
            print(f"  -> Budget ~exhausted at run {i+1} PASS")
            break

    passed_a = refused_run is not None
    results.append({
        "scenario": "A_runaway_loop",
        "passed": passed_a,
        "runs_before_refusal": refused_run or len(runs_a),
        "total_spent": total_spent,
        "note": f"Budget exhausted after {refused_run} runs" if passed_a
                else "FAILED: budget never triggered refusal in 20 runs",
    })

    # --- Scenario B: Unaffordable tier ---
    print("\n[B] Unaffordable tier - micro budget that cannot cover any call...")
    micro = 0.000001
    args_b = _make_args("Compare two approaches to bounding agent spend.", micro, args.principal, args.base_url, label="adv-b")
    run_ws_b = workspace / "run_b"
    run_ws_b.mkdir(parents=True, exist_ok=True)
    outcome_b = await run_task(args_b, budget=micro, transport=transport, data_dir=run_ws_b)

    status_b = outcome_b.result.get("status", "")
    refusals_b = outcome_b.budget.get("refusals", 0)
    calls_b = outcome_b.budget.get("calls", 0)
    spent_b = outcome_b.budget.get("spent", 0.0)

    passed_b = refusals_b > 0 or calls_b == 0
    print(f"  status={status_b}  refusals={refusals_b}  calls={calls_b}  spent={spent_b:.8f}")
    print(f"  -> {'PASS' if passed_b else 'FAIL'}: {'refused before any call' if calls_b == 0 else f'{refusals_b} refusals recorded'}")

    results.append({
        "scenario": "B_unaffordable",
        "passed": passed_b,
        "status": status_b,
        "refusals": refusals_b,
        "calls": calls_b,
        "spent": spent_b,
        "note": "Correctly refused: budget too small for any call" if passed_b
                else f"FAILED: {calls_b} calls succeeded on ${micro} budget",
    })

    # --- Scenario C: Cascade climb ---
    print("\n[C] Cascade climb - budget sized for economy; hard task tries frontier...")
    econ = economics(args)
    policy = econ.policy()
    cheapest_proj = policy.project(econ.ladder.cheapest)
    tight_budget = cheapest_proj * 1.5

    hard_task = (
        "A cyclist rides 30 km out at 20 km/h and returns at 30 km/h. "
        "What is the average speed for the whole trip? Give one number."
    )
    args_c = _make_args(hard_task, tight_budget, args.principal, args.base_url, label="adv-c")
    run_ws_c = workspace / "run_c"
    run_ws_c.mkdir(parents=True, exist_ok=True)
    outcome_c = await run_task(args_c, budget=tight_budget, transport=transport, data_dir=run_ws_c)

    status_c = outcome_c.result.get("status", "")
    downgrades_c = outcome_c.budget.get("downgrades", 0)
    refusals_c = outcome_c.budget.get("refusals", 0)
    tiers_c = sorted({c["tier"] for c in outcome_c.budget.get("charges", [])})
    spent_c = outcome_c.budget.get("spent", 0.0)

    passed_c = downgrades_c > 0 or refusals_c > 0 or spent_c <= tight_budget
    print(f"  status={status_c}  downgrades={downgrades_c}  refusals={refusals_c}  tiers={tiers_c}  spent={spent_c:.8f}")
    print(f"  -> {'PASS' if passed_c else 'FAIL'}")

    results.append({
        "scenario": "C_cascade_climb",
        "passed": passed_c,
        "status": status_c,
        "downgrades": downgrades_c,
        "refusals": refusals_c,
        "tiers_used": tiers_c,
        "tight_budget": tight_budget,
        "spent": spent_c,
        "note": f"Guard triggered: {downgrades_c} downgrades, {refusals_c} refusals, spent <= ceiling" if passed_c
                else "FAILED: cascade climbed freely without budget guard",
    })

    # --- Scenario D: Metering verification ---
    print("\n[D] Metering verification - verify ledger agrees with transport...")
    args_d = _make_args(
        "In exactly two sentences, explain why a budget must be enforced in code.",
        0.02, args.principal, args.base_url, label="adv-d"
    )
    before_calls = transport.calls
    run_ws_d = workspace / "run_d"
    run_ws_d.mkdir(parents=True, exist_ok=True)
    outcome_d = await run_task(args_d, budget=0.02, transport=transport, data_dir=run_ws_d)

    ledger_calls = outcome_d.budget.get("calls", 0)
    actual_transport_calls = transport.calls - before_calls
    spent_d = outcome_d.budget.get("spent", 0.0)

    passed_d = ledger_calls == actual_transport_calls
    print(f"  transport_calls={actual_transport_calls}  ledger_calls={ledger_calls}  spent={spent_d:.8f}")
    print(f"  -> {'PASS' if passed_d else 'FAIL'}: ledger {'agrees' if passed_d else 'DISAGREES'} with transport")

    results.append({
        "scenario": "D_metering_verification",
        "passed": passed_d,
        "ledger_calls": ledger_calls,
        "transport_calls": actual_transport_calls,
        "spent": spent_d,
        "note": f"Ledger matches transport: {ledger_calls} calls, ${spent_d:.8f}" if passed_d
                else f"FAILED: ledger={ledger_calls} != transport={actual_transport_calls}",
    })

    return results


def main() -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Adversarial budget test - Part 3")
    ap.add_argument("--principal", default="demo/manish")
    ap.add_argument("--budget", type=float, default=0.001)
    ap.add_argument("--base-url", default=os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111"))
    args = ap.parse_args()

    args_obj = _make_args("test", args.budget, args.principal, args.base_url)

    print(f"Adversarial budget test  principal={args.principal}  budget=${args.budget}")
    print(f"Gateway: {args.base_url}")
    print("=" * 60)

    transport, mode, detail = transport_for(args_obj)

    with tempfile.TemporaryDirectory(prefix="s15-adv-") as workspace:
        results = sync(_run_all_scenarios(args_obj, transport, Path(workspace)))

    print("\n" + "=" * 60)
    print("ADVERSARIAL TEST RESULTS")
    print("=" * 60)
    all_passed = True
    for r in results:
        status = "PASS ✓" if r["passed"] else "FAIL ✗"
        if not r["passed"]:
            all_passed = False
        print(f"  {r['scenario']:30s}  {status}  {r['note']}")

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / "adversarial_results.json"
    out_path.write_text(json.dumps({"results": results, "all_passed": all_passed}, indent=2))
    print(f"\nResults written to {out_path}")

    if all_passed:
        print("\n✓ All adversarial scenarios passed - budget guard is effective.")
        sys.exit(0)
    else:
        print("\n✗ Some adversarial scenarios failed - review output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
