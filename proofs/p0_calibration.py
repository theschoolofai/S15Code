#!/usr/bin/env python
"""p0 — measure the ladder before writing its prices down.

Every other proof in this directory takes the ladder on trust: it reads
``tiers.yaml``, asks for a rung, and prices whatever comes back. That trust has
to be earned once, against the gateway that will actually serve the run, because
four things can be quietly false about a configured ladder and every one of them
silently corrupts a cost measurement downstream:

1. **A rung is not reachable.** A model id that 404s, or one whose free-tier
   quota is exhausted, turns a "downgrade" into a failed node and a cost
   comparison into a comparison of one rung with itself.
2. **A rung answers as a different model.** A gateway that falls back to another
   provider hands back an answer priced against a row that did not produce it.
   The ledger stays self-consistent and is wrong.
3. **A rung returns nothing, at full price.** A thinking model given a tight
   ``max_tokens`` can spend its whole output budget in the reasoning channel and
   return ``content: ""`` — a fully billed non-answer. Cost per resolved task
   would then be infinite for a reason that has nothing to do with the task.
4. **The ladder is not ordered.** If projected cost is not monotone along
   ``order``, "one rung down" is not a saving and the policy's whole vocabulary
   is meaningless.

This proof asserts all four, and reports the measured spread the README quotes.
It also calls every member of the judge panel, because a judge that cannot be
reached is not a lenient judge, it is a missing denominator.

    uv run python proofs/p0_calibration.py --config-dir config/navigation
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OUT, Args, Proof  # noqa: E402

from s15code.economics import EconomicsConfig  # noqa: E402
from s15code.evals import EvalsConfig  # noqa: E402
from s15code.gateway import GatewayClient  # noqa: E402

DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")

#: One identical prompt for every rung and every judge. Short, deterministic, and
#: about nothing in the task set — calibration measures the transport and the
#: price, not the workload.
CALIBRATION_PROMPT = (
    "In exactly three sentences, explain why a routing policy should be measured "
    "by cost per resolved task rather than cost per call."
)
CALIBRATION_SYSTEM = "You are a precise technical writer. Answer in exactly three sentences."


async def measure_one(
    client: GatewayClient, *, label: str, kind: str, request: dict[str, Any], pricing: Any
) -> dict[str, Any]:
    """One call, fully recorded: what was asked for, what answered, what it cost."""
    started = time.time()
    error, response = None, {}
    try:
        response = await client.chat(
            prompt=CALIBRATION_PROMPT, system=CALIBRATION_SYSTEM, request=dict(request)
        )
    except Exception as failure:  # a 404, an exhausted quota, a dead provider
        error = f"{type(failure).__name__}: {failure}"
    elapsed_ms = (time.time() - started) * 1000.0

    text = str(response.get("text") or "")
    served_model = response.get("model")
    requested_model = request.get("model")
    input_tokens = int(response.get("input_tokens") or 0)
    output_tokens = int(response.get("output_tokens") or 0)
    return {
        "label": label,
        "kind": kind,
        "requested_provider": request.get("provider"),
        "requested_model": requested_model,
        "served_provider": response.get("provider"),
        "served_model": served_model,
        # The gateway expands a logical provider into a numbered key pool
        # (gemini -> gemini_2), so provider identity is compared on the prefix
        # while MODEL identity is compared exactly — the model is what gets
        # priced, so that is the one that must not drift.
        "model_matches_request": bool(served_model and served_model == requested_model),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "measured_cost": pricing.cost(
            served_model or requested_model, input_tokens=input_tokens, output_tokens=output_tokens
        ),
        "latency_ms": float(response.get("latency_ms") or elapsed_ms),
        "answer_chars": len(text),
        "non_empty": bool(text.strip()),
        "answer_excerpt": text[:240],
        "stop_reason": response.get("stop_reason"),
        "error": error,
    }


async def measure_all(client: GatewayClient, config: EconomicsConfig, evals: EvalsConfig) -> dict[str, Any]:
    rungs = []
    for name in config.ladder.names:
        tier = config.ladder.tier(name)
        rungs.append(await measure_one(
            client, label=name, kind="rung",
            request=config.ladder.request_for(tier), pricing=config.pricing,
        ))
        print(f"  rung  {name:10} {rungs[-1]['served_model'] or 'FAILED':26} "
              f"{rungs[-1]['latency_ms']:8.0f} ms  ${rungs[-1]['measured_cost']:.8f}", flush=True)
    judges = []
    for member in evals.rubric.panel:
        judges.append(await measure_one(
            client, label=member.name, kind="judge",
            request=dict(member.request), pricing=config.pricing,
        ))
        print(f"  judge {member.name:10} {judges[-1]['served_model'] or 'FAILED':26} "
              f"{judges[-1]['latency_ms']:8.0f} ms  ${judges[-1]['measured_cost']:.8f}", flush=True)
    await client.close()
    return {"rungs": rungs, "judges": judges}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--config-dir", default=os.getenv("S15_CONFIG_DIR"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--label", default="", help="suffix for the JSON written to proofs/out/")
    return parser.parse_args(argv)


def run(parsed: argparse.Namespace) -> Proof:
    config = EconomicsConfig.load(parsed.config_dir)
    evals = EvalsConfig.load(parsed.config_dir)
    base_url = parsed.base_url.rstrip("/")

    args = Args(
        task=CALIBRATION_PROMPT, budget=0.0, principal="proofs/s15/p0", offline=False,
        base_url=base_url, otel_endpoint=None, respond_as="text",
        config_dir=parsed.config_dir, live_embeddings=False, label=parsed.label,
    )
    proof = Proof(name="p0_calibration", args=args, mode="live", mode_detail={"base_url": base_url})

    print(f"\np0: calibrating {len(config.ladder.names)} rungs and "
          f"{len(evals.rubric.panel)} judges against {base_url}\n")
    measured = asyncio.run(measure_all(GatewayClient(base_url), config, evals))
    rungs, judges = measured["rungs"], measured["judges"]

    policy = config.policy()
    for rung in rungs:
        tier = config.ladder.tier(rung["label"])
        rung["projected_cost"] = policy.project(tier)
        proof.fact(f"rung {rung['label']}", (
            f"{rung['served_model'] or 'UNREACHABLE'}  {rung['input_tokens']}in/{rung['output_tokens']}out  "
            f"${rung['measured_cost']:.8f} measured  ${rung['projected_cost']:.8f} projected  "
            f"{rung['latency_ms']:.0f} ms  {rung['answer_chars']} chars"
            + (f"  ERROR {rung['error'][:80]}" if rung["error"] else "")
        ))
    for judge in judges:
        proof.fact(f"judge {judge['label']}", (
            f"{judge['served_model'] or 'UNREACHABLE'}  {judge['latency_ms']:.0f} ms  "
            f"${judge['measured_cost']:.8f}"
            + (f"  ERROR {judge['error'][:80]}" if judge["error"] else "")
        ))

    measured_costs = [rung["measured_cost"] for rung in rungs]
    projected_costs = [rung["projected_cost"] for rung in rungs]
    measured_spread = (measured_costs[-1] / measured_costs[0]) if measured_costs[0] else None
    projected_spread = (projected_costs[-1] / projected_costs[0]) if projected_costs[0] else None
    proof.fact("projected spread", f"{projected_spread:.1f}x (worst case, what admission prices)"
               if projected_spread else "n/a")
    proof.fact("measured spread", f"{measured_spread:.1f}x (this prompt, what the ledger charged)"
               if measured_spread else "n/a")
    proof.fact("latency spread", f"{rungs[-1]['latency_ms'] / rungs[0]['latency_ms']:.1f}x"
               if rungs[0]["latency_ms"] else "n/a")

    # --- the four things a configured ladder can quietly be lying about ------
    unreachable = [rung["label"] for rung in rungs if rung["error"]]
    proof.check("every rung of the ladder is reachable", not unreachable,
                unreachable or f"{len(rungs)} rungs answered")

    empty = [rung["label"] for rung in rungs if not rung["error"] and not rung["non_empty"]]
    proof.check("no rung returns an empty answer at full price", not empty,
                empty or "every rung returned non-empty content")

    substituted = [
        f"{rung['label']}: asked {rung['requested_model']}, served {rung['served_model']}"
        for rung in rungs if not rung["error"] and not rung["model_matches_request"]
    ]
    proof.check("every rung is served by the model its config names", not substituted,
                substituted or "no silent model substitution")

    monotone = all(a < b for a, b in zip(projected_costs, projected_costs[1:]))
    proof.check("projected cost is monotone along the ladder order", monotone,
                {name: f"{cost:.8f}" for name, cost in
                 zip([rung["label"] for rung in rungs], projected_costs)})

    distinct_models = {rung["served_model"] for rung in rungs if rung["served_model"]}
    proof.check("each rung is a DIFFERENT model, so a downgrade changes the model",
                len(distinct_models) == len(rungs), sorted(distinct_models))

    judge_unreachable = [judge["label"] for judge in judges if judge["error"] or not judge["non_empty"]]
    proof.check("every judge on the panel answers", not judge_unreachable,
                judge_unreachable or [judge["served_model"] for judge in judges])

    ladder_models = {rung["served_model"] for rung in rungs if rung["served_model"]}
    judge_models = {judge["served_model"] for judge in judges if judge["served_model"]}
    proof.check("no judge shares a model with any rung of the ladder",
                not (ladder_models & judge_models),
                sorted(ladder_models & judge_models) or
                f"ladder {sorted(ladder_models)} vs judges {sorted(judge_models)}")

    # --- a finding, not an assertion ---------------------------------------
    # Whether MEASURED cost is ordered the same way as projected cost is a
    # property of what the models chose to write, not of the config. A dearer
    # rung that answers more briefly can cost less on a given prompt, and that is
    # worth reporting rather than asserting away.
    measured_monotone = all(a < b for a, b in zip(measured_costs, measured_costs[1:]))
    proof.fact("FINDING measured order matches projected order",
               "YES" if measured_monotone else
               "NO — a dearer rung answered more cheaply on this prompt than the ladder predicts")

    proof.record("rungs", rungs)
    proof.record("judges", judges)
    proof.record("prompt", {"prompt": CALIBRATION_PROMPT, "system": CALIBRATION_SYSTEM})
    proof.record("economics", config.describe())
    proof.record("spreads", {"projected": projected_spread, "measured": measured_spread})
    return proof


def main() -> None:
    parsed = parse_args()
    proof = run(parsed)
    OUT.mkdir(parents=True, exist_ok=True)
    sys.exit(proof.finish())


if __name__ == "__main__":
    main()
