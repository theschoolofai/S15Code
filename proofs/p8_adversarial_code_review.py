#!/usr/bin/env python
"""p8 — an adversarial test against THIS policy, not the shipped one.

Part 3 of the assignment asks for an attack on the policy you built, showing the
spend before your control and the refusal after it, with the refusal visible in
telemetry. `p3` proves the shipped controller stops a runaway loop; this proves
the CODE REVIEW policy in `config_code_review/` stops one, and it targets the two
places where that policy is deliberately weaker than the shipped one.

**What is actually being attacked.** `config_code_review/budgets.yaml` loosens two
thresholds on purpose, and an attacker should be aimed at exactly those:

  downgrade_at        0.50 -> 0.65   so a two-rung cascade can finish before
                                     pressure starts pulling it back down
  max_calls_per_node  6    -> 4      one attempt per rung, plus one

And `config_code_review/tiers.yaml` gives every rung the SAME max_tokens, which
narrows the ladder from the shipped 26x to 2.3x. That matters here: downgrading is
a *cost control* only to the extent the rungs differ in price. On a 26x ladder,
dropping a rung cuts spend by 96%. On this one it cuts it by 57%. So this policy
leans much harder on the absolute ceiling and on the call counters than the
shipped one does, and this proof measures whether those still hold.

**Three attacks, each aimed at a different control:**

A. UNAFFORDABLE TIER — ask for the dearest rung with a ceiling below even the
   cheapest rung's projection. Tests admission: the correct behaviour is zero
   calls and a refusal, not a downgrade to something that still does not fit.

B. RUNAWAY LOOP — a planner that earns a new node from every outcome, forever.
   Tests the ceiling and `max_calls_per_run`. Run twice: once with a ceiling so
   high it never binds (the "before"), once with the real policy (the "after"),
   so the saving is MEASURED rather than extrapolated.

C. SINGLE-NODE RETRY STORM — one node that calls the model in a loop internally.
   The run-level ceiling would eventually stop it, but only after it had spent;
   `max_calls_per_node` is what stops one bad node from consuming the whole run's
   allowance. This is the control that was tightened from 6 to 4, so it is the one
   most worth attacking.

Every assertion is on the real graph, the real journal, the real controller and
the real span exporter.

    python proofs/p8_adversarial_code_review.py --config-dir config_code_review \\
        --budget 0.01 --base-url http://127.0.0.1:8111
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from harness import OUT, Args, Proof, economics, parse, sync, transport_for  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    BudgetRefused,
)
from s15code.runtime import metered  # noqa: E402

#: Rounds the uncontrolled ("before") loop is allowed. This is a real spend against
#: real providers, so it is small on purpose: enough to establish a per-call cost
#: to compare against, not enough to be the denial-of-wallet incident it models.
DEFAULT_UNCONTROLLED_ROUNDS = 12
#: Safety net for the controlled loop. If the controller works, spend stopped long
#: before this; if this is ever reached, the proof itself gives up rather than the
#: policy.
DEFAULT_LOOP_LIMIT = 120
#: Rounds to extrapolate the uncontrolled bill over, for the headline number.
DEFAULT_PROJECTION = 10_000

ATTACK_PROMPT = (
    "Review this function and report every defect you can find, then expand on each "
    "one in as much detail as possible:\n\n"
    "def total(items):\n    t = 0\n    for i in range(1, len(items)):\n        t += items[i]\n"
    "    return t / len(items)\n"
)
ATTACK_SYSTEM = "You are a code reviewer. Answer in full."


class RunawayPlanner:
    """The adversary: every outcome earns one more node, always at the dearest rung."""

    def __init__(self, task: str, *, skill: str, tier: str, limit: int) -> None:
        self.task, self.skill, self.tier, self.limit = task, skill, tier, limit
        self.rounds = 0
        self.last_selection = {"mode": "adversarial_runaway"}

    def _node(self, index: int) -> TaskSpec:
        return TaskSpec(f"loop_{index}", self.skill,
                        {"query": f"{self.task} (iteration {index})"},
                        {"agent": self.skill, TIER_KEY: self.tier})

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        if event.kind == "run_started":
            return GraphPatch(add=(self._node(1),), reason="adversary opens the loop")
        if event.kind in ("task_succeeded", "task_failed"):
            self.rounds += 1
            if self.rounds >= self.limit:
                return GraphPatch(finish=True,
                                  reason="proof safety stop; the controller already halted spend")
            return GraphPatch(add=(self._node(len(graph.nodes) + 1),),
                              reason="adversary spins another iteration")
        return GraphPatch()


class SingleNodePlanner:
    """One node only. The spending happens inside it, so the run-level counters
    are not what has to catch this — the per-node counter is."""

    def __init__(self, task: str, *, skill: str, tier: str) -> None:
        self.task, self.skill, self.tier = task, skill, tier
        self.last_selection = {"mode": "adversarial_single_node"}

    async def plan(self, graph: GraphSnapshot, event: Event) -> GraphPatch:
        if event.kind == "run_started":
            return GraphPatch(add=(TaskSpec("greedy", self.skill, {"query": self.task},
                                            {"agent": self.skill, TIER_KEY: self.tier}),),
                              reason="a single greedy node")
        if event.kind in ("task_succeeded", "task_failed"):
            return GraphPatch(finish=True, reason="the one node is done, however it ended")
        return GraphPatch()


async def _drive(args: Args, planner_kind: str, *, ceiling: float, tier: str,
                 loop_limit: int, data_dir: Path, inner_calls: int = 1) -> dict[str, Any]:
    """Run one attack against the real graph, journal and controller."""
    config = economics(args)
    transport, mode, detail = transport_for(args)
    store = GraphStore(data_dir / "graph.sqlite")
    run_id = f"attack_{planner_kind}"
    budget = config.budget(principal=args.principal, amount=ceiling, run_id=run_id)
    gateway = BudgetedGateway(transport, budget=budget, policy=config.policy(),
                              pricing=config.pricing, ladder=config.ladder)
    llm = gateway.as_text_llm()

    async def work(task: TaskSpec) -> dict[str, Any]:
        """One adversarial step. Attack C makes several calls from ONE node."""
        last: dict[str, Any] = {}
        refused = 0
        for _ in range(inner_calls):
            try:
                last = await llm(task.input["query"], ATTACK_SYSTEM)
            except BudgetRefused:
                refused += 1
                if inner_calls == 1:
                    raise
                break
        return {"text": last.get("text", ""), "provider": last.get("provider"),
                "model": last.get("model"), "tier": last.get("tier"),
                "inner_refusals": refused}

    if planner_kind == "single_node":
        inner_planner: Any = SingleNodePlanner(ATTACK_PROMPT, skill="content", tier=tier)
    else:
        inner_planner = RunawayPlanner(ATTACK_PROMPT, skill="content", tier=tier, limit=loop_limit)

    planner = BudgetAwarePlanner(inner=inner_planner, ladder=config.ladder, budget=budget,
                                 reserve_fraction=config.thresholds.reserve_fraction)
    store.start(run_id, context={"prompt": ATTACK_PROMPT})
    try:
        report = await LiveGraphExecutor(store, planner, {"content": metered(work)},
                                         max_workers=1).run(run_id)
        snapshot = store.snapshot(run_id)
        journal = {
            "run_id": run_id, "finished": snapshot.finished, "nodes": snapshot.nodes,
            "edges": snapshot.edges,
            "events": [{"sequence": e.sequence, "kind": e.kind, "node_id": e.node_id,
                        "payload": e.payload} for e in store.events(run_id)],
        }
    finally:
        store.close()

    refused_nodes = [
        nid for nid, node in snapshot.nodes.items()
        if node["state"] == "failed"
        and "BudgetRefused" in str((node.get("result") or {}).get("error", ""))
    ]
    return {
        "mode": mode, "mode_detail": detail, "config": config, "journal": journal,
        "budget": budget.snapshot(), "transport_calls": transport.calls,
        "transport_failures": transport.failures,
        "nodes": len(snapshot.nodes), "finished": report.finished,
        "rounds": getattr(inner_planner, "rounds", 1),
        "refused_nodes": refused_nodes,
        "inner_refusals": sum(
            int(((node.get("result") or {}).get("inner_refusals") or 0))
            for node in snapshot.nodes.values()
        ),
    }


def run(args: Args, *, uncontrolled_rounds: int, loop_limit: int, projection: int) -> Proof:
    config = economics(args)
    ladder = config.ladder
    top = ladder.most_capable
    proof = Proof(name="p8_adversarial_code_review", args=args, mode="", mode_detail={})

    # ── BEFORE: the same loop with a ceiling so high it never binds ──────────
    # "Uncontrolled" is modelled as an allowance far above anything the loop can
    # reach in the rounds allowed, so admission never refuses and the loop spends
    # freely. Rounds are capped by the PROOF, not by the policy, which is exactly
    # the distinction being drawn.
    with tempfile.TemporaryDirectory(prefix="s15-p8-before-") as workspace:
        before = sync(_drive(args, "runaway", ceiling=1_000.0, tier=top.name,
                             loop_limit=uncontrolled_rounds, data_dir=Path(workspace)))

    # ── AFTER: the real policy ───────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="s15-p8-after-") as workspace:
        after = sync(_drive(args, "runaway", ceiling=args.budget, tier=top.name,
                            loop_limit=loop_limit, data_dir=Path(workspace)))

    # ── ATTACK A: a ceiling below even the cheapest rung's projection ────────
    unaffordable = bottom_projection(config) * 0.5
    with tempfile.TemporaryDirectory(prefix="s15-p8-unaff-") as workspace:
        unaff = sync(_drive(args, "single_node", ceiling=unaffordable, tier=top.name,
                            loop_limit=2, data_dir=Path(workspace)))

    # ── ATTACK C: one node, many calls ───────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="s15-p8-node-") as workspace:
        greedy = sync(_drive(args, "single_node", ceiling=args.budget, tier=top.name,
                             loop_limit=2, data_dir=Path(workspace), inner_calls=20))

    proof.mode = after["mode"]
    proof.mode_detail = after["mode_detail"]

    b_before, b_after = before["budget"], after["budget"]
    per_call = b_before["spent"] / b_before["calls"] if b_before["calls"] else 0.0
    uncontrolled_bill = per_call * projection
    thresholds = config.thresholds

    proof.fact("ladder", " < ".join(ladder.names))
    proof.fact("ladder spread", f"{top_projection(config) / bottom_projection(config):.2f}x "
                                f"({bottom_projection(config):.8f} -> {top_projection(config):.8f})")
    proof.fact("policy under attack", f"downgrade_at {thresholds.downgrade_at} · "
                                      f"refuse_at {thresholds.refuse_at} · "
                                      f"max_calls_per_run {thresholds.max_calls_per_run} · "
                                      f"max_calls_per_node {thresholds.max_calls_per_node}")
    proof.fact("BEFORE  uncontrolled", f"{b_before['calls']} calls, spent {b_before['spent']:.8f}, "
                                       f"{b_before['refusals']} refusals, "
                                       f"{uncontrolled_rounds} rounds allowed by the PROOF")
    proof.fact("AFTER   controlled", f"{b_after['calls']} calls, spent {b_after['spent']:.8f} "
                                     f"of {b_after['total']:.8f}, {b_after['refusals']} refusals, "
                                     f"{after['rounds']} rounds attempted")
    proof.fact("spend prevented", f"{b_before['spent'] - b_after['spent']:.8f} over "
                                  f"{uncontrolled_rounds} rounds")
    proof.fact("cost per call (measured)", f"{per_call:.8f}")
    proof.fact("uncontrolled bill", f"~{uncontrolled_bill:.4f} over {projection} rounds "
                                    f"(extrapolated from the BEFORE run)")
    proof.fact("attack A  unaffordable ceiling",
               f"{unaffordable:.8f} (half the cheapest rung's projection) -> "
               f"{unaff['budget']['calls']} calls, {unaff['budget']['refusals']} refusals, "
               f"spent {unaff['budget']['spent']:.8f}")
    proof.fact("attack C  single greedy node",
               f"20 calls attempted from one node -> {greedy['budget']['calls']} admitted, "
               f"{greedy['inner_refusals']} refused inside the node "
               f"(max_calls_per_node {thresholds.max_calls_per_node})")

    # ── the refusal must be visible in TELEMETRY, not only in the ledger ─────
    from s15code.telemetry import export_run  # noqa: PLC0415

    export = export_run(after["journal"], budget=b_after, principal=args.principal,
                        endpoint=args.otel_endpoint)
    spans = export.as_dict()["spans"]
    failed_spans = [s for s in spans if str(s.get("status", "")).lower() not in ("", "ok", "unset")]
    node_spans = [s for s in spans if s.get("kind") == "node"]
    provider_spans = [s for s in spans if s.get("kind") == "provider_call"]
    trace_ids = sorted({s.get("trace_id") for s in spans if s.get("trace_id")})

    # The local span dict carries `status` ("ERROR") but not the status DESCRIPTION,
    # which is where the controller writes *why* it refused. That description does
    # go over the wire, so the strong claim — "telemetry says this was refused for
    # budget, not merely that it failed" — has to be checked against the backend
    # rather than against the in-memory export. Same approach p4 takes.
    backend_refusals: list[str] = []
    backend_checked = False
    if args.otel_endpoint and trace_ids:
        backend_refusals, backend_checked = _backend_refusal_reasons(trace_ids[0])

    proof.fact("telemetry", f"{len(spans)} spans, {len(node_spans)} node, "
                            f"{len(provider_spans)} provider_call, {len(failed_spans)} non-ok")
    proof.fact("otlp endpoint", args.otel_endpoint or "(not exported over the wire)")
    if trace_ids:
        proof.fact("trace ids", trace_ids)
    if backend_checked:
        proof.fact("refusal reason in the backend",
                   f"{len(backend_refusals)} spans name BudgetRefused; "
                   f"e.g. {backend_refusals[0][:110]!r}" if backend_refusals
                   else "none found")

    # ── checks ───────────────────────────────────────────────────────────────
    proof.check("the control demonstrably prevented spend the same loop would have made",
                b_after["spent"] < b_before["spent"],
                f"controlled {b_after['spent']:.8f} < uncontrolled {b_before['spent']:.8f}")
    proof.check("the ceiling held under an unbounded loop",
                b_after["spent"] <= b_after["total"],
                f"spent {b_after['spent']:.8f} <= {b_after['total']:.8f}")
    proof.check("admitted calls are bounded by the configured run ceiling",
                thresholds.max_calls_per_run == 0
                or b_after["calls"] <= thresholds.max_calls_per_run,
                f"{b_after['calls']} <= {thresholds.max_calls_per_run}")
    proof.check("the loop kept asking and was refused",
                b_after["refusals"] > 0 and after["rounds"] > b_after["calls"],
                f"{after['rounds']} rounds, {b_after['calls']} admitted, "
                f"{b_after['refusals']} refused")
    proof.check("ATTACK A: an unaffordable ceiling refuses without spending anything",
                unaff["budget"]["calls"] == 0 and unaff["budget"]["spent"] == 0.0
                and unaff["budget"]["refusals"] > 0,
                f"{unaff['budget']['calls']} calls, spent {unaff['budget']['spent']:.8f}, "
                f"{unaff['budget']['refusals']} refusals")
    proof.check("ATTACK C: one greedy node cannot consume the whole run allowance",
                thresholds.max_calls_per_node == 0
                or greedy["budget"]["calls"] <= thresholds.max_calls_per_node,
                f"{greedy['budget']['calls']} admitted <= max_calls_per_node "
                f"{thresholds.max_calls_per_node}")
    proof.check("a refusal is a visible graph failure, not a silent truncation",
                len(after["refused_nodes"]) > 0,
                f"{len(after['refused_nodes'])} nodes failed with BudgetRefused")
    proof.check("the refusal is visible in TELEMETRY as a failed span",
                len(failed_spans) >= len(after["refused_nodes"]) > 0,
                f"{len(failed_spans)} non-ok spans for {len(after['refused_nodes'])} refused nodes")
    if backend_checked:
        # The point of this one: a trace that only says "this node failed" is not
        # enough to audit a budget. The backend must carry WHY.
        proof.check("the backend says the node was REFUSED FOR BUDGET, not merely that it failed",
                    len(backend_refusals) > 0,
                    f"{len(backend_refusals)} spans name BudgetRefused in their status description")
    proof.check("a refused node emits NO provider_call span, because nothing was sent",
                len(provider_spans) == b_after["calls"],
                f"{len(provider_spans)} provider_call spans == {b_after['calls']} admitted calls")
    proof.check("every provider call that returned is metered",
                after["transport_calls"] == b_after["calls"],
                f"{after['transport_calls']} transport calls == {b_after['calls']} charges "
                f"({after['transport_failures']} gateway errors, no tokens to charge)")
    return proof


def _backend_refusal_reasons(trace_id: str) -> tuple[list[str], bool]:
    """Fetch the trace back and return the status descriptions that name a refusal.

    Returns (reasons, checked). ``checked`` is False when the backend could not be
    reached at all, so a missing collector degrades to "not asserted" rather than
    to a false failure — the same courtesy the rest of the suite extends.
    """
    import os  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    base = os.getenv("GLC_TRACE_UI", "http://127.0.0.1:16686").rstrip("/")
    try:
        # The exporter is asynchronous; give the collector a moment to ingest.
        for _ in range(10):
            response = httpx.get(f"{base}/api/traces/{trace_id}", timeout=8.0)
            if response.status_code == 200 and (response.json().get("data") or []):
                break
            time.sleep(1)
        else:
            return [], False
        data = response.json().get("data") or []
        if not data:
            return [], False
    except Exception:                                          # noqa: BLE001
        return [], False

    reasons = []
    for span in data[0].get("spans", []):
        tags = {t.get("key"): t.get("value") for t in span.get("tags", [])}
        description = str(tags.get("otel.status_description") or "")
        if "BudgetRefused" in description or "refus" in description.lower():
            reasons.append(description)
    return reasons, True


def projection_for(config, tier_name: str) -> float:
    tier = config.ladder.tier(tier_name)
    price = config.pricing.price_for(tier.price_model)
    return (tier.projected_input_tokens * price.input
            + tier.request.get("max_tokens", tier.projected_output_tokens) * price.output) / 1e6


def top_projection(config) -> float:
    return projection_for(config, config.ladder.most_capable.name)


def bottom_projection(config) -> float:
    return projection_for(config, config.ladder.cheapest.name)


def main() -> None:
    # Same two-stage parse the other proofs use: this proof's own flags first,
    # then the shared harness flags (--task, --budget, --config-dir, ...).
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--uncontrolled-rounds", type=int, default=DEFAULT_UNCONTROLLED_ROUNDS)
    extra.add_argument("--loop-limit", type=int, default=DEFAULT_LOOP_LIMIT)
    extra.add_argument("--projection-rounds", type=int, default=DEFAULT_PROJECTION)
    known, rest = extra.parse_known_args()
    args = parse(__doc__ or "", rest)
    sys.exit(run(args, uncontrolled_rounds=known.uncontrolled_rounds,
                 loop_limit=known.loop_limit, projection=known.projection_rounds).finish())


if __name__ == "__main__":
    main()
