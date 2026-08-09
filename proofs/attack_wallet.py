#!/usr/bin/env python
"""Part 3 — an adversarial denial-of-wallet attack against my own policy.

The threat: an attacker (or a runaway agent loop) drives repeated requests at my
most expensive rung — the frontier model — hoping to burn the inference budget.
This is the §9 attack: turn the budget into a denial-of-service. The defence is
the gateway's hard controller plus a per-session ceiling, and the test's job is
to show, on real calls, that:

  1. spend BEFORE the control is unbounded (the extrapolated counterfactual),
  2. spend AFTER the control flattens at the ceiling and never crosses it,
  3. every refused call is a visible HTTP 402 in the gateway's refusal recorder,
     carrying its projected cost and shortfall — the control that prevents work
     leaves a trace of the work it prevented.

Unlike p3 (which drives the runtime's own loop), this attacks the GATEWAY seam
directly with my ladder's frontier model, so it exercises the same controller a
production caller hits. It arms a fresh session budget, fires N rounds, and reads
the outcome back from /v1/status-style endpoints and /v1/refusals.

    uv run python proofs/attack_wallet.py --rounds 60 --ceiling 0.02

Exits non-zero if the ceiling was breached or a refusal went unrecorded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

GATEWAY = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111").rstrip("/")
OUT = Path(__file__).resolve().parent / "out"

# The attack pins the frontier rung — the most expensive way to answer — because
# that is what an attacker trying to burn a budget would reach for.
FRONTIER_PROVIDER = "bedrock"
FRONTIER_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
# A prompt that invites a long, expensive answer. max_tokens is set high so the
# worst-case projection the controller prices is large.
ATTACK_PROMPT = ("Write an exhaustive, multi-section options-trading strategy "
                 "guide covering iron condors, ratio spreads and calendars, with "
                 "worked P&L examples for each. Be thorough.")


def _arm_budget(client: httpx.Client, principal: str, ceiling: float) -> None:
    r = client.post(f"{GATEWAY}/v1/budget",
                    json={"principal": principal, "limit_usd": ceiling, "window": "lifetime"},
                    timeout=30)
    r.raise_for_status()


def _drop_budget(client: httpx.Client, principal: str) -> None:
    try:
        client.delete(f"{GATEWAY}/v1/budget/{principal}", timeout=30)
    except Exception:
        pass


def _spend(client: httpx.Client, principal: str) -> dict:
    r = client.get(f"{GATEWAY}/v1/budget/{principal}", timeout=30)
    r.raise_for_status()
    return r.json()


def _fire(client: httpx.Client, session: str, max_tokens: int) -> dict:
    body = {
        "messages": [{"role": "user", "content": ATTACK_PROMPT}],
        "provider": FRONTIER_PROVIDER,
        "model": FRONTIER_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "session": session,
    }
    try:
        r = client.post(f"{GATEWAY}/v1/chat", json=body, timeout=120)
    except Exception as exc:
        return {"status": -1, "error": f"{type(exc).__name__}: {exc}"}
    out = {"status": r.status_code}
    try:
        d = r.json()
    except Exception:
        return out
    if r.status_code == 200:
        out["cost"] = (d.get("cost") or {}).get("total_usd") or 0.0
    elif r.status_code == 402:
        det = d.get("detail", d)
        out["projected_usd"] = det.get("projected_usd")
        out["shortfall_usd"] = det.get("shortfall_usd")
        out["code"] = det.get("code")
    else:
        out["error"] = d
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=60)
    parser.add_argument("--ceiling", type=float, default=0.02)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--principal", default="session:wallet-attack")
    parsed = parser.parse_args(argv)

    OUT.mkdir(exist_ok=True)
    session = parsed.principal.split(":", 1)[-1]

    with httpx.Client() as client:
        _drop_budget(client, parsed.principal)
        _arm_budget(client, parsed.principal, parsed.ceiling)

        # A refusal-recorder baseline, so we count only THIS attack's refusals.
        before_refusals = client.get(f"{GATEWAY}/v1/refusals", params={"limit": 1},
                                     timeout=30).json().get("total", 0)

        admitted, refused, errors = [], [], []
        for i in range(parsed.rounds):
            res = _fire(client, session, parsed.max_tokens)
            if res["status"] == 200:
                admitted.append(res)
            elif res["status"] == 402:
                refused.append(res)
            else:
                errors.append(res)

        spend = _spend(client, parsed.principal)
        admitted_spend = round(sum(a.get("cost") or 0.0 for a in admitted), 8)
        # The counterfactual: if every round had been admitted at the mean
        # admitted cost, this is what the attack would have billed. Clearly
        # marked as extrapolation, not measurement.
        mean_cost = (admitted_spend / len(admitted)) if admitted else 0.0
        uncontrolled = round(mean_cost * parsed.rounds, 8)

        refusals_after = client.get(f"{GATEWAY}/v1/refusals", params={"limit": max(parsed.rounds, 50)},
                                    timeout=30).json()
        recorded_402 = [r for r in refusals_after.get("refusals", []) if r.get("status") == 402]
        new_recorded = refusals_after.get("total", 0) - before_refusals

        result = {
            "gateway": GATEWAY,
            "attack": {
                "principal": parsed.principal, "rounds": parsed.rounds,
                "ceiling_usd": parsed.ceiling, "frontier_model": FRONTIER_MODEL,
                "max_tokens": parsed.max_tokens,
            },
            "outcome": {
                "admitted": len(admitted), "refused": len(refused), "errors": len(errors),
                "admitted_spend_usd": admitted_spend,
                "ledger_spent_usd": spend.get("spent_usd") or spend.get("status", {}).get("spent_usd"),
                "remaining_usd": spend.get("remaining_usd") or spend.get("status", {}).get("remaining_usd"),
                "uncontrolled_extrapolated_usd": uncontrolled,
                "mean_admitted_cost_usd": round(mean_cost, 8),
            },
            "telemetry": {
                "new_refusals_recorded": new_recorded,
                "sample_recorded_402": recorded_402[:2],
            },
        }

        # --- assertions -----------------------------------------------------
        checks = []
        actual_spent = result["outcome"]["ledger_spent_usd"] or admitted_spend
        checks.append(("spend never crossed the ceiling",
                       actual_spent <= parsed.ceiling + 1e-9,
                       f"{actual_spent} <= {parsed.ceiling}"))
        checks.append(("the loop kept asking and was refused",
                       len(refused) > 0,
                       f"{len(admitted)} admitted, {len(refused)} refused of {parsed.rounds}"))
        checks.append(("every refusal is a 402 carrying its shortfall",
                       all(r.get("shortfall_usd") is not None for r in refused),
                       f"{len(refused)} refusals, all with shortfall"))
        checks.append(("refusals are visible in the recorder",
                       new_recorded >= len(refused) and len(recorded_402) > 0,
                       f"recorder gained {new_recorded}; loop saw {len(refused)}"))
        checks.append(("the control, not the attacker, stopped the spend",
                       len(refused) > 0 and actual_spent <= parsed.ceiling + 1e-9,
                       "spend flattened while requests kept arriving"))

        result["checks"] = [{"name": n, "passed": bool(p), "detail": d} for n, p, d in checks]
        passed = all(p for _, p, _ in checks)
        (OUT / "attack_wallet.json").write_text(json.dumps(result, indent=2))

        print(f"attack_wallet — {parsed.rounds} rounds at frontier against a ${parsed.ceiling} ceiling\n")
        print(f"  admitted           {len(admitted)}")
        print(f"  refused (402)      {len(refused)}")
        print(f"  errors             {len(errors)}")
        print(f"  measured spend     ${actual_spent:.8f}  (ceiling ${parsed.ceiling})")
        print(f"  uncontrolled est.  ${uncontrolled:.8f}  (EXTRAPOLATED, not measured)")
        print(f"  refusals recorded  {new_recorded} new in /v1/refusals\n")
        for n, p, d in checks:
            print(f"  [{'PASS' if p else 'FAIL'}] {n:52} {d}")
        print(f"\n  {'ALL CHECKS PASSED' if passed else 'CHECKS FAILED'} -> {OUT / 'attack_wallet.json'}")

        _drop_budget(client, parsed.principal)
        return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
