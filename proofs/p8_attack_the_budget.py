#!/usr/bin/env python
"""p8 -- attack a budget policy and prove the refusal is visible, not just real.

p3_denial_of_wallet.py already proves a runaway loop cannot spend past its
ceiling. This proof exists for two things p3 does not do:

1. **A configurable target tier.** p3 always hammers ``ladder.most_capable``.
   That is the right adversary when every rung is reachable, but a rung can
   go down for reasons that have nothing to do with the policy under test --
   see docs/part1_validation.md Section 4, where the shipped ladder's own
   frontier rung returned a live 410 from GitHub mid-session. ``--tier``
   lets the same adversarial loop be pointed at whichever rung is actually
   reachable, so "the controller holds under attack" is never confused with
   "a third-party API happened to be up."
2. **The refusal has to show up in the trace, not just the ledger.** p3
   never calls ``s15code.telemetry.export_run``, so nothing it proves is
   visible outside the JSON it writes. This proof builds real OTel spans
   from the same run and -- when ``--otel-endpoint`` names a live collector
   -- reads the trace back out of the backend by id, exactly as
   ``p4_trace_export.py`` does, and checks specifically that a REFUSED node
   appears as an ERROR-status span in what the backend actually stored.

The comparison this is meant to produce is BEFORE/AFTER, not just PASS/FAIL:
run this proof once against a config with no effective ceiling (a large
budget, a large call cap) to show spend and calls climbing unchecked round
after round with zero refusals, then again against the real policy config to
show the same adversary hitting a wall almost immediately, with every
refusal recorded and traced.

    python proofs/p8_attack_the_budget.py --task "<anything>" --budget 1000 \\
        --config-dir config/UNCONTROLLED --tier standard --loop-limit 30
    python proofs/p8_attack_the_budget.py --task "<anything>" \\
        --config-dir config/part2_billing_support --tier standard \\
        --otel-endpoint http://127.0.0.1:4318/v1/traces
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from harness import Args, Proof, economics, parse, sync, transport_for  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p4_trace_export import fetch_trace, query_base  # noqa: E402

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
)
from s15code.runtime import metered  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

DEFAULT_LOOP_LIMIT = 60
DEFAULT_PROJECTION = 10_000


class RunawayPlanner:
    """The adversary: every outcome earns one more node, at a fixed tier."""

    def __init__(self, task: str, *, skill: str, tier: str, limit: int) -> None:
        self.task, self.skill, self.tier, self.limit = task, skill, tier, limit
        self.rounds = 0
        self.last_selection = {"mode": "adversarial_runaway", "target_tier": tier}

    def _node(self, index: int) -> TaskSpec:
        return TaskSpec(f"loop_{index}", self.skill,
                        {"query": f"{self.task} (iteration {index})"},
                        {"agent": self.skill, TIER_KEY: self.tier})

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        if event.kind == "run_started":
            return GraphPatch(add=(self._node(1),), reason="adversary starts the loop")
        if event.kind in ("task_succeeded", "task_failed"):
            self.rounds += 1
            index = len(graph.nodes) + 1
            if self.rounds >= self.limit:
                return GraphPatch(finish=True, reason="proof safety stop; the controller already halted spend")
            return GraphPatch(add=(self._node(index),), reason="adversary spins another iteration")
        return GraphPatch()


async def drive_attack(args: Args, *, target_tier: str, loop_limit: int, data_dir: Path) -> dict[str, Any]:
    config = economics(args)
    transport, mode, detail = transport_for(args)
    store = GraphStore(data_dir / "graph.sqlite")
    run_id = "attack"
    budget = config.budget(principal=args.principal, amount=args.budget, run_id=run_id)
    gateway = BudgetedGateway(transport, budget=budget, policy=config.policy(),
                              pricing=config.pricing, ladder=config.ladder)
    llm = gateway.as_text_llm()

    async def work(task: TaskSpec) -> dict[str, Any]:
        result = await llm(task.input["query"], "Reply in one short sentence. This is a controlled attack test.")
        return {"text": result.get("text", ""), "provider": result.get("provider"),
                "model": result.get("model"), "tier": result.get("tier")}

    planner = BudgetAwarePlanner(
        inner=RunawayPlanner(args.task, skill="content", tier=target_tier, limit=loop_limit),
        ladder=config.ladder, budget=budget, reserve_fraction=config.thresholds.reserve_fraction,
    )
    store.start(run_id, context={"prompt": args.task})
    try:
        report = await LiveGraphExecutor(store, planner, {"content": metered(work)},
                                         max_workers=1).run(run_id)
        snapshot = store.snapshot(run_id)
        journal = [{"sequence": e.sequence, "kind": e.kind, "node_id": e.node_id, "payload": e.payload}
                   for e in store.events(run_id)]
    finally:
        store.close()
    return {
        "mode": mode, "mode_detail": detail, "config": config,
        "budget": budget.snapshot(), "transport_calls": transport.calls,
        "transport_failures": transport.failures, "transport_errors": transport.errors[:5],
        "nodes": len(snapshot.nodes), "rounds": planner.inner.rounds, "finished": report.finished,
        "journal": {"run_id": run_id, "finished": report.finished, "events": journal},
        "failed_nodes": sum(1 for node in snapshot.nodes.values() if node["state"] == "failed"),
        "refused_nodes": sum(
            1 for node in snapshot.nodes.values()
            if node["state"] == "failed" and "BudgetRefused" in str((node.get("result") or {}).get("error", ""))
        ),
    }


def run(args: Args, *, target_tier: str, loop_limit: int, projection: int) -> Proof:
    with tempfile.TemporaryDirectory(prefix="s15-p8-") as workspace:
        outcome = sync(drive_attack(args, target_tier=target_tier, loop_limit=loop_limit,
                                    data_dir=Path(workspace)))

    config = outcome["config"]
    budget = outcome["budget"]
    proof = Proof(name="p8_attack_the_budget", args=args, mode=outcome["mode"],
                  mode_detail=outcome["mode_detail"])

    cost_per_call = budget["spent"] / budget["calls"] if budget["calls"] else 0.0
    uncontrolled = cost_per_call * projection

    proof.fact("attacked config", str(config.directory))
    proof.fact("target tier", target_tier)
    proof.fact("ceiling", f"{budget['total']:.8f} {budget['currency']}")
    proof.fact("max_calls_per_run", config.thresholds.max_calls_per_run)
    proof.fact("spent", f"{budget['spent']:.8f}")
    proof.fact("admitted calls", budget["calls"])
    proof.fact("refusals", budget["refusals"])
    proof.fact("loop rounds", outcome["rounds"])
    proof.fact("refused nodes", outcome["refused_nodes"])
    proof.fact("cost per call", f"{cost_per_call:.8f}")
    proof.fact("uncontrolled bill", f"~{uncontrolled:.4f} over {projection} rounds (extrapolated)")
    proof.fact("transport failures", outcome["transport_failures"])
    if outcome["transport_errors"]:
        proof.fact("sample transport errors", outcome["transport_errors"])

    proof.check("the ceiling held under an unbounded loop",
                budget["spent"] <= budget["total"],
                f"spent {budget['spent']:.8f} <= {budget['total']:.8f}")
    proof.check("admitted calls are bounded by the configured call ceiling",
                config.thresholds.max_calls_per_run == 0
                or budget["calls"] <= config.thresholds.max_calls_per_run,
                f"{budget['calls']} <= {config.thresholds.max_calls_per_run}")
    proof.check("every provider call that returned is metered",
                outcome["transport_calls"] == budget["calls"],
                f"{outcome['transport_calls']} transport calls == {budget['calls']} charges "
                f"({outcome['transport_failures']} gateway errors, no tokens to charge)")

    # --- telemetry: build the spans, and read a refusal back out of the backend --- #
    export = export_run(outcome["journal"], budget=budget, endpoint=args.otel_endpoint,
                        principal=args.principal)
    spans = export.as_dict()["spans"]
    totals = export.totals()
    proof.fact("spans", totals["spans"])
    proof.fact("trace ids", totals["trace_ids"])
    proof.fact("otlp endpoint", args.otel_endpoint or "(none: spans built, nothing sent)")

    # `SpanExport.as_dict()` exposes each span's status code (ERROR/UNSET/OK)
    # but not the status description text, so "is this ERROR span specifically
    # a BudgetRefused" is checked against the graph snapshot's own error string
    # (outcome["refused_nodes"]) and confirmed by node-count: in this synthetic
    # attack every node failure IS a refusal, so the counts must match exactly.
    error_node_spans = [s for s in spans if s["kind"] == "node" and s["status"] == "ERROR"]
    proof.check("a refusal is a visible ERROR-status span, not a silent truncation",
                outcome["refused_nodes"] == len(error_node_spans),
                f"{outcome['refused_nodes']} refused nodes, {len(error_node_spans)} ERROR-status node spans")

    base = query_base(args)
    trace_id = totals["trace_ids"][0] if totals["trace_ids"] else None
    if not (base and trace_id and export.exported_over_the_wire):
        proof.fact("backend round trip", "not attempted")
        proof.check("refusal visible in the backend (skipped: no collector configured)", True,
                    f"otel_endpoint={args.otel_endpoint or None}")
    else:
        found = fetch_trace(base, trace_id)
        proof.fact("backend query", found["query"])
        proof.check("a real trace lands in the backend, fetched back by id", found["ok"],
                    found.get("reason") or f"{len(found['trace']['spans'])} spans returned")
        if found["ok"]:
            backend_spans = found["trace"]["spans"]
            backend_refused = [
                s for s in backend_spans
                if any(t.get("key") == "otel.status_description"
                       and "BudgetRefused" in str(t.get("value") or "") for t in (s.get("tags") or []))
            ]
            proof.fact("backend spans", len(backend_spans))
            proof.fact("backend refused spans", len(backend_refused))
            proof.check("the refusal survives the wire: an ERROR span naming BudgetRefused is in the backend",
                        bool(outcome["refused_nodes"]) == bool(backend_refused),
                        f"{outcome['refused_nodes']} refused nodes, {len(backend_refused)} backend spans "
                        f"carrying BudgetRefused in their status")
            proof.record("backend_trace", found["trace"])

    proof.record("budget", budget)
    proof.record("journal_events", len(outcome["journal"]["events"]))
    proof.record("projection_rounds", projection)
    proof.record("uncontrolled_bill_extrapolated", uncontrolled)
    return proof


def main() -> None:
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--loop-limit", type=int, default=DEFAULT_LOOP_LIMIT)
    extra.add_argument("--projection-rounds", type=int, default=DEFAULT_PROJECTION)
    extra.add_argument("--tier", default=None,
                        help="ladder rung to hammer. Defaults to the ladder's most_capable rung.")
    known, rest = extra.parse_known_args()
    args = parse(__doc__ or "", rest)
    target_tier = known.tier or economics(args).ladder.most_capable.name
    sys.exit(run(args, target_tier=target_tier, loop_limit=known.loop_limit,
                projection=known.projection_rounds).finish())


if __name__ == "__main__":
    main()
