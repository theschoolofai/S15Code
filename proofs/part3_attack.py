#!/usr/bin/env python
"""Part 3 — attack the Part 2 policy.

Four attacks against `config/part2`, each run through the REAL controller, the
REAL live graph and the REAL journal. Nothing here is mocked except, in A4, the
provider itself — which is the point of A4.

    A1  unaffordable_tier   ask for a rung the ceiling cannot buy
    A2  full_ladder_climb   force the cascade through every rung, repeatedly
    A3  runaway_loop        a planner that earns one more node from every outcome
    A4  fail_after_tokens   a provider that reports usage and THEN raises

For each attack the report shows:

  * what the attack WOULD have spent with no controller ("uncontrolled"),
    computed from the attack's own measured cost per call, not from a guess;
  * what it actually spent;
  * the refusal that stopped it;
  * the refusal as it appears in OpenTelemetry.

Usage:
    python proofs/part3_attack.py --budget 0.004
    python proofs/part3_attack.py --budget 0.004 --otel-endpoint http://127.0.0.1:4318/v1/traces
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import DEFAULT_BASE_URL, OUT, MeteredTransport, gateway_reachable  # noqa: E402

from s15code.core.live_graph import (  # noqa: E402
    Event,
    GraphPatch,
    GraphSnapshot,
    GraphStore,
    LiveGraphExecutor,
    TaskSpec,
)
from s15code.economics import (  # noqa: E402
    TIER_KEY,
    BudgetAwarePlanner,
    BudgetedGateway,
    EconomicsConfig,
)
from s15code.gateway import GatewayClient  # noqa: E402
from s15code.runtime import metered  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

CONFIG_DIR = "config/part2"
PROJECTION_ROUNDS = 10_000
LOOP_LIMIT = 150

TASK = "Review this function and name its single defect: def f(x): return x[len(x)-1-1:]"


# --------------------------------------------------------------------------- #
# A4's adversary: a provider that bills you and then fails
# --------------------------------------------------------------------------- #
class FailAfterTokensTransport:
    """Reports token usage, then raises — the expensive failure mode.

    `MeteredTransport` documents the assumption this attacks: a call that raised
    "consumed no tokens the gateway could report, so [is] deliberately not
    charged". That is true of a connection refused. It is NOT true of a provider
    that streams 400 tokens and dies, or a gateway that times out reading a
    response the provider already generated and will bill for.

    Every call here consumes `tokens` and then raises. If the ledger stays at
    zero while the imaginary provider bills for real, the meter has a hole.
    """

    def __init__(self, tokens: int = 400) -> None:
        self.tokens = tokens
        self.consumed_input = 0
        self.consumed_output = 0
        self.attempts = 0

    async def chat(self, *, prompt: str, system: str,
                   request: dict[str, Any] | None = None) -> dict[str, Any]:
        self.attempts += 1
        # The provider does the work and would bill for it...
        self.consumed_input += len(prompt) // 4
        self.consumed_output += self.tokens
        # ...and then the connection dies before the response is returned.
        raise TimeoutError(
            f"provider generated ~{self.tokens} output tokens then the read timed out"
        )


# --------------------------------------------------------------------------- #
# A3's adversary
# --------------------------------------------------------------------------- #
class RunawayPlanner:
    """Every outcome earns one more node, always at the dearest rung."""

    def __init__(self, task: str, *, skill: str, tier: str, limit: int) -> None:
        self.task, self.skill, self.tier, self.limit = task, skill, tier, limit
        self.rounds = 0
        self.last_selection = {"mode": "part3_runaway"}

    def _node(self, i: int) -> TaskSpec:
        return TaskSpec(f"loop_{i}", self.skill, {"query": f"{self.task} (iteration {i})"},
                        {"agent": self.skill, TIER_KEY: self.tier})

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        if event.kind == "run_started":
            return GraphPatch(add=(self._node(1),), reason="adversary starts the loop")
        if event.kind in ("task_succeeded", "task_failed"):
            self.rounds += 1
            if self.rounds >= self.limit:
                return GraphPatch(finish=True, reason="harness safety stop; controller already halted spend")
            return GraphPatch(add=(self._node(len(graph.nodes) + 1),),
                              reason="adversary spins another iteration")
        return GraphPatch()


# --------------------------------------------------------------------------- #
# A2's adversary
# --------------------------------------------------------------------------- #
class ClimbEveryRungPlanner:
    """Asks for the dearest rung on every node, N nodes, so the cascade must
    walk the ladder down for each of them under a shrinking allowance."""

    def __init__(self, task: str, *, skill: str, tier: str, nodes: int) -> None:
        self.task, self.skill, self.tier, self.nodes = task, skill, tier, nodes
        self.made = 0
        self.last_selection = {"mode": "part3_climb"}

    def _node(self, i: int) -> TaskSpec:
        return TaskSpec(f"climb_{i}", self.skill, {"query": f"{self.task} (variant {i})"},
                        {"agent": self.skill, TIER_KEY: self.tier})

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        if event.kind == "run_started":
            self.made = 1
            return GraphPatch(add=(self._node(1),), reason="climb starts")
        if event.kind in ("task_succeeded", "task_failed"):
            if self.made >= self.nodes:
                return GraphPatch(finish=True, reason="climb complete")
            self.made += 1
            return GraphPatch(add=(self._node(self.made),), reason="next climb node")
        return GraphPatch()


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
async def drive(planner_factory, transport, config, *, budget_usd: float,
                principal: str, run_id: str, data_dir: Path,
                workers: int = 1) -> dict[str, Any]:
    store = GraphStore(data_dir / f"{run_id}.sqlite")
    budget = config.budget(principal=principal, amount=budget_usd, run_id=run_id)
    gateway = BudgetedGateway(transport, budget=budget, policy=config.policy(),
                              pricing=config.pricing, ladder=config.ladder)
    llm = gateway.as_text_llm()

    async def work(task: TaskSpec) -> dict[str, Any]:
        result = await llm(task.input["query"], "Name the single defect. Be brief.")
        return {"text": result.get("text", ""), "provider": result.get("provider"),
                "model": result.get("model"), "tier": result.get("tier")}

    planner = BudgetAwarePlanner(
        inner=planner_factory(config), ladder=config.ladder, budget=budget,
        reserve_fraction=config.thresholds.reserve_fraction,
    )
    store.start(run_id, context={"prompt": TASK})
    try:
        report = await LiveGraphExecutor(store, planner, {"content": metered(work)},
                                         max_workers=workers).run(run_id)
        snapshot = store.snapshot(run_id)
        journal = [{"sequence": e.sequence, "kind": e.kind, "node_id": e.node_id,
                    "payload": e.payload} for e in store.events(run_id)]
    finally:
        store.close()

    refused = [n for n in snapshot.nodes.values()
               if n["state"] == "failed"
               and "BudgetRefused" in str((n.get("result") or {}).get("error", ""))]
    return {
        "run_id": run_id,
        "budget": budget.snapshot(),
        "nodes": len(snapshot.nodes),
        "rounds": getattr(planner.inner, "rounds", getattr(planner.inner, "made", 0)),
        "finished": report.finished,
        "refused_nodes": len(refused),
        "failed_nodes": sum(1 for n in snapshot.nodes.values() if n["state"] == "failed"),
        "journal": {"run_id": run_id, "events": journal},
        "transport_calls": getattr(transport, "calls", None),
        "transport_failures": getattr(transport, "failures", None),
    }


async def attack_unaffordable(config, transport, budget_usd, principal, data_dir):
    """A1 — a ceiling below even the cheapest rung's projection."""
    cheapest = config.ladder.cheapest
    tiny = 0.0000001
    out = await drive(
        lambda c: ClimbEveryRungPlanner(TASK, skill="content",
                                        tier=c.ladder.most_capable.name, nodes=1),
        transport, config, budget_usd=tiny, principal=principal,
        run_id="a1_unaffordable", data_dir=data_dir)
    out["attack"] = "A1 unaffordable_tier"
    out["ceiling_requested"] = tiny
    out["cheapest_rung"] = cheapest.name
    return out


async def attack_climb(config, transport, budget_usd, principal, data_dir):
    """A2 — every node demands frontier; the allowance forces a walk down."""
    out = await drive(
        lambda c: ClimbEveryRungPlanner(TASK, skill="content",
                                        tier=c.ladder.most_capable.name, nodes=8),
        transport, config, budget_usd=budget_usd, principal=principal,
        run_id="a2_climb", data_dir=data_dir)
    out["attack"] = "A2 full_ladder_climb"
    return out


async def attack_runaway(config, transport, budget_usd, principal, data_dir):
    """A3 — unbounded loop at the dearest rung."""
    out = await drive(
        lambda c: RunawayPlanner(TASK, skill="content",
                                 tier=c.ladder.most_capable.name, limit=LOOP_LIMIT),
        transport, config, budget_usd=budget_usd, principal=principal,
        run_id="a3_runaway", data_dir=data_dir)
    out["attack"] = "A3 runaway_loop"
    return out


async def attack_fail_after_tokens(config, budget_usd, principal, data_dir, tokens=400):
    """A4 — provider consumes tokens then raises. Uses its own transport."""
    evil = FailAfterTokensTransport(tokens=tokens)
    wrapped = MeteredTransport(evil)
    out = await drive(
        lambda c: RunawayPlanner(TASK, skill="content",
                                 tier=c.ladder.most_capable.name, limit=40),
        wrapped, config, budget_usd=budget_usd, principal=principal,
        run_id="a4_fail_after_tokens", data_dir=data_dir)
    out["attack"] = "A4 fail_after_tokens"
    price = config.pricing.price_for(config.ladder.most_capable.price_model)
    unbilled = (evil.consumed_input * price.input + evil.consumed_output * price.output) / 1e6
    out["provider_side"] = {
        "attempts": evil.attempts,
        "input_tokens_consumed": evil.consumed_input,
        "output_tokens_consumed": evil.consumed_output,
        "would_be_billed_usd": unbilled,
        "ledger_recorded_usd": out["budget"]["spent"],
        "unmetered_usd": unbilled - out["budget"]["spent"],
    }
    return out


async def main_async(args) -> int:
    config = EconomicsConfig.load(args.config_dir)
    live = not args.offline and gateway_reachable(args.base_url)
    if not live:
        print("ERROR: Part 3 must run against a live gateway. "
              f"{args.base_url} is not reachable.", file=sys.stderr)
        return 2

    results = []
    with tempfile.TemporaryDirectory(prefix="s15-part3-") as ws:
        data = Path(ws)
        # A1-A3 share one live transport (one event loop, one httpx client).
        transport = MeteredTransport(GatewayClient(args.base_url))
        results.append(await attack_unaffordable(config, transport, args.budget,
                                                 args.principal, data))
        results.append(await attack_climb(config, transport, args.budget,
                                          args.principal, data))
        results.append(await attack_runaway(config, transport, args.budget,
                                            args.principal, data))
        # A4 needs its own hostile transport.
        results.append(await attack_fail_after_tokens(config, args.budget,
                                                      args.principal, data))

    # ---- telemetry: every attack exported, refusals included ---------------
    for r in results:
        exp = export_run(r["journal"], budget=r["budget"], endpoint=args.otel_endpoint,
                         service_name="s15code-part3-attack", principal=args.principal)
        totals = exp.totals()
        root_events = []
        for node in [exp.tree]:
            root_events = [(name, dict(attrs)) for name, attrs in getattr(node, "events", [])]
        r["telemetry"] = {
            "trace_ids": list(totals.get("trace_ids") or []),
            "spans": totals.get("spans"),
            "exported_over_the_wire": exp.exported_over_the_wire,
            "endpoint": exp.endpoint,
            "root_budget_refusals": root_events,
        }

    report = {
        "proof": "part3_attack",
        "config_dir": args.config_dir,
        "ladder": {t: {"model": config.ladder._tiers[t].request.get("model"),
                       "provider": config.ladder._tiers[t].request.get("provider")}
                   for t in config.ladder._order},
        "thresholds": {
            "max_calls_per_run": config.thresholds.max_calls_per_run,
            "max_calls_per_node": config.thresholds.max_calls_per_node,
            "refuse_at": config.thresholds.refuse_at,
            "downgrade_at": config.thresholds.downgrade_at,
        },
        "projection_rounds": PROJECTION_ROUNDS,
        "attacks": results,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "part3_attack.json"
    path.write_text(json.dumps(report, indent=1, default=str))

    # ---- console --------------------------------------------------------- #
    print(f"\npart3_attack  (live)  config={args.config_dir}")
    print(f"  ladder     {' < '.join(config.ladder._order)}")
    print(f"  ceilings   max_calls_per_run={config.thresholds.max_calls_per_run} "
          f"max_calls_per_node={config.thresholds.max_calls_per_node}")
    ok = True
    for r in results:
        b = r["budget"]
        cpc = b["spent"] / b["calls"] if b["calls"] else 0.0
        print(f"\n  {r['attack']}")
        print(f"    ceiling            ${b['total']:.8f}")
        print(f"    spent              ${b['spent']:.8f}")
        print(f"    admitted calls     {b['calls']}")
        print(f"    refusals           {b['refusals']}")
        print(f"    nodes / refused    {r['nodes']} / {r['refused_nodes']}")
        if cpc:
            print(f"    cost per call      ${cpc:.8f}")
            print(f"    UNCONTROLLED bill  ~${cpc * PROJECTION_ROUNDS:.2f} "
                  f"over {PROJECTION_ROUNDS} rounds (from this run's own rate)")
        held = b["spent"] <= b["total"] + 1e-12
        print(f"    ceiling held       {held}")
        t = r["telemetry"]
        print(f"    telemetry          trace={t['trace_ids']} spans={t['spans']} "
              f"exported={t['exported_over_the_wire']}")
        print(f"    refusal events     {len(t['root_budget_refusals'])} on the run span")
        for name, attrs in t["root_budget_refusals"][:2]:
            print(f"        {name}: {attrs.get('s15.budget.reason','')[:88]}")
        if "provider_side" in r:
            p = r["provider_side"]
            print(f"    PROVIDER SIDE      {p['attempts']} attempts consumed "
                  f"{p['output_tokens_consumed']} output tokens")
            print(f"                       would be billed ${p['would_be_billed_usd']:.8f}, "
                  f"ledger recorded ${p['ledger_recorded_usd']:.8f}")
            print(f"                       UNMETERED ${p['unmetered_usd']:.8f}")
        ok = ok and held
    print(f"\n  {'ALL CEILINGS HELD' if ok else 'A CEILING WAS BREACHED'} -> {path}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Part 3 — attack the Part 2 policy")
    p.add_argument("--budget", type=float, default=0.004)
    p.add_argument("--principal", default="part3/attacker")
    p.add_argument("--config-dir", default=CONFIG_DIR)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--otel-endpoint", default=os.getenv("S15_OTEL_EXPORTER_ENDPOINT"))
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
