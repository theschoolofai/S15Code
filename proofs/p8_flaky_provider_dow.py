#!/usr/bin/env python
"""p8 — a provider that fails AFTER consuming tokens is not free money.

Part 3 of the S15 assignment asks for an attack that "arranges for a provider
to fail after consuming tokens." This is that attack, and it is not contrived:
`s15code/economics/controller.py`'s own `MeteredTransport` docstring says a
raised exception is deliberately never charged, reasoning that a failed call
"consumed no tokens the gateway could report." That is an assumption about the
FAILURE MODE, not a law: a slow stream cut off after the provider already
produced (and, on its own account, billed) most of its output looks identical
to the controller as any other exception.

The consequence is structural, not cosmetic. `RunBudget.calls` — what
`max_calls_per_run` / `max_calls_per_node` in `policy.py` both gate on — only
increments inside `RunBudget.charge()`, which only runs after a transport call
RETURNS. A transport that always raises after "generating" output therefore:

  - never moves `budget.spent` (no charge is ever recorded — the dollar
    ceiling looks perfectly safe), AND
  - never trips the call ceiling either (calls == 0 forever, however many
    times the loop goes round) —

so an adversarial retry loop against a flaky-after-generating provider can run
literally forever with a ledger that reports $0.00 spent and 0/60 calls used,
while a REAL such provider would be billing real money the whole time. This
proof measures that "true bill" with a shadow ledger the flaky transport keeps
honestly for itself (representing the provider's own invoice, which S15Code
never sees), and shows it climbing unbounded while S15Code's own numbers sit
at zero.

The control is `s15code/economics/attempt_guard.py` — `AttemptGuardedTransport`,
written for this assignment, added as one new file with no edit to the shipped
`controller.py` or `budget.py`. It counts every ATTEMPT (success or failure)
against a run- and node-scoped ceiling, so it caps the loop even when the
provider never once honestly reports what it did.

    python proofs/p8_flaky_provider_dow.py --task "<anything>" --budget 0.02 --offline
    python proofs/p8_flaky_provider_dow.py --task "<anything>" --budget 0.02 --offline --no-guard
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness import Args, Proof, economics, parse, sync  # noqa: F401

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
    MeteredTransport,
)
from s15code.economics.attempt_guard import AttemptGuardedTransport  # noqa: E402
from s15code.runtime import metered  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

DEFAULT_LOOP_LIMIT = 500


class DropsAfterGeneratingTransport:
    """A provider that ALWAYS generates real output, then ALWAYS drops the
    connection before the caller sees it — every call looks like ordinary
    network flakiness to `MeteredTransport`, but real tokens were produced.

    Keeps its own shadow ledger (`true_calls`, `true_cost`) of what an honest
    provider invoice would show. S15Code's own budget never sees this ledger —
    that gap is exactly what is under test.
    """

    def __init__(self, *, pricing, model: str = "openai/gpt-4.1", wanted_output: int = 900) -> None:
        self.pricing, self.model, self.wanted_output = pricing, model, wanted_output
        self.true_calls = 0
        self.true_cost = 0.0
        self.true_input_tokens = 0
        self.true_output_tokens = 0

    async def chat(self, *, prompt: str, system: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        request = dict(request or {})
        # The provider genuinely does the work: tokens are produced and, on the
        # provider's OWN account, billed. hashlib stands in for "an LLM ran."
        hashlib.sha256((prompt + system).encode()).hexdigest()
        input_tokens = len(prompt) // 4 + len(system) // 4
        output_tokens = min(self.wanted_output, int(request.get("max_tokens") or self.wanted_output))
        cost = self.pricing.cost(self.model, input_tokens=input_tokens, output_tokens=output_tokens)
        self.true_calls += 1
        self.true_cost += cost
        self.true_input_tokens += input_tokens
        self.true_output_tokens += output_tokens
        # ...and then the connection drops AFTER generation, before the caller
        # ever sees a token count. This is the realistic failure S15Code's own
        # MeteredTransport docstring did not model: not "no tokens produced",
        # but "tokens produced, response lost."
        raise ConnectionError(
            f"stream dropped after generating {output_tokens} output tokens "
            f"(provider-side bill: ${cost:.8f}, never reported to the caller)"
        )

    def shadow_ledger(self) -> dict[str, Any]:
        return {
            "true_calls": self.true_calls, "true_cost": self.true_cost,
            "true_input_tokens": self.true_input_tokens, "true_output_tokens": self.true_output_tokens,
        }


class RunawayPlanner:
    """The adversary: every outcome (success OR failure) earns one more node."""

    def __init__(self, task: str, *, skill: str, tier: str, limit: int) -> None:
        self.task, self.skill, self.tier, self.limit = task, skill, tier, limit
        self.rounds = 0

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
                return GraphPatch(finish=True, reason="proof safety stop")
            return GraphPatch(add=(self._node(index),), reason="adversary retries after a dropped stream")
        return GraphPatch()


@dataclass
class Outcome:
    budget: dict[str, Any]
    shadow: dict[str, Any]
    guard: dict[str, Any] | None
    transport_calls: int
    transport_failures: int
    rounds: int
    nodes: int
    finished: bool
    refused_nodes: int
    journal_events: int
    trace_ids: list[str] = field(default_factory=list)
    span_count: int = 0
    span_kinds: dict[str, int] = field(default_factory=dict)
    error_status_spans: int = 0
    exported_over_the_wire: bool = False


async def drive(args: Args, *, loop_limit: int, use_guard: bool, data_dir: Path) -> Outcome:
    config = economics(args)
    flaky = DropsAfterGeneratingTransport(pricing=config.pricing)
    metered_transport = MeteredTransport(flaky)
    guard: AttemptGuardedTransport | None = None
    if use_guard:
        guard = AttemptGuardedTransport(
            metered_transport,
            max_attempts_per_run=config.thresholds.max_calls_per_run,
            max_attempts_per_node=config.thresholds.max_calls_per_node,
        )
        transport_for_gateway = guard
    else:
        transport_for_gateway = metered_transport

    store = GraphStore(data_dir / "graph.sqlite")
    run_id = "flaky-dow"
    budget = config.budget(principal=args.principal, amount=args.budget, run_id=run_id)
    gateway = BudgetedGateway(transport_for_gateway, budget=budget, policy=config.policy(),
                              pricing=config.pricing, ladder=config.ladder)
    llm = gateway.as_text_llm()

    async def work(task: TaskSpec) -> dict[str, Any]:
        result = await llm(task.input["query"], "Reply briefly. Controlled adversarial test.")
        return {"text": result.get("text", ""), "provider": result.get("provider"),
                "model": result.get("model"), "tier": result.get("tier")}

    planner = BudgetAwarePlanner(
        inner=RunawayPlanner(args.task, skill="content", tier=config.ladder.most_capable.name, limit=loop_limit),
        ladder=config.ladder, budget=budget, reserve_fraction=config.thresholds.reserve_fraction,
    )
    store.start(run_id, context={"prompt": args.task})
    try:
        report = await LiveGraphExecutor(store, planner, {"content": metered(work)}, max_workers=1).run(run_id)
        snapshot = store.snapshot(run_id)
        events = list(store.events(run_id))
    finally:
        store.close()

    refused_nodes = sum(
        1 for node in snapshot.nodes.values()
        if node["state"] == "failed" and "BudgetRefused" in str((node.get("result") or {}).get("error", ""))
    )

    journal = {
        "run_id": run_id,
        "finished": snapshot.finished,
        "nodes": snapshot.nodes,
        "edges": snapshot.edges,
        "events": [
            {"sequence": e.sequence, "kind": e.kind, "node_id": e.node_id, "payload": e.payload}
            for e in events
        ],
    }
    export = export_run(journal, budget=budget.snapshot(), endpoint=args.otel_endpoint,
                        service_name="s15code-p8-adversarial", principal=args.principal)
    trace_ids = sorted({span.get_span_context().trace_id for span in export.spans})
    span_kinds: dict[str, int] = {}
    refused_span_count = 0
    for span in export.spans:
        kind = str(dict(span.attributes or {}).get("s15.span.kind", "?"))
        span_kinds[kind] = span_kinds.get(kind, 0) + 1
        if span.status and span.status.status_code.name == "ERROR":
            refused_span_count += 1

    return Outcome(
        budget=budget.snapshot(), shadow=flaky.shadow_ledger(),
        guard=guard.snapshot() if guard else None,
        transport_calls=metered_transport.calls, transport_failures=metered_transport.failures,
        rounds=planner.inner.rounds, nodes=len(snapshot.nodes), finished=report.finished,
        refused_nodes=refused_nodes, journal_events=len(journal["events"]),
        trace_ids=[format(t, "032x") for t in trace_ids], span_count=len(export.spans),
        span_kinds=span_kinds, error_status_spans=refused_span_count,
        exported_over_the_wire=export.exported_over_the_wire,
    )


def run(args: Args, *, loop_limit: int, use_guard: bool) -> Proof:
    with tempfile.TemporaryDirectory(prefix="s15-p8-") as workspace:
        outcome = sync(drive(args, loop_limit=loop_limit, use_guard=use_guard, data_dir=Path(workspace)))

    proof = Proof(name="p8_flaky_provider_dow", args=args, mode="offline (attack is a fault-injection transport, not a network mode)",
                  mode_detail={"guard_active": use_guard, "loop_limit": loop_limit})

    proof.fact("guard active", use_guard)
    proof.fact("S15Code ledger: spent", f"${outcome.budget['spent']:.8f}")
    proof.fact("S15Code ledger: admitted calls", outcome.budget["calls"])
    proof.fact("S15Code ledger: refusals", outcome.budget["refusals"])
    proof.fact("loop rounds attempted", outcome.rounds)
    proof.fact("nodes refused (BudgetRefused, visible in journal)", outcome.refused_nodes)
    proof.fact("shadow ledger: true provider calls", outcome.shadow["true_calls"])
    proof.fact("shadow ledger: TRUE bill the flaky provider would send", f"${outcome.shadow['true_cost']:.8f}")
    if outcome.guard:
        proof.fact("attempt guard: run attempts counted", outcome.guard["run_attempts"])
        proof.fact("attempt guard: refusals raised", len(outcome.guard["refusals"]))
    proof.fact("jaeger trace id(s)", outcome.trace_ids)
    proof.fact("spans exported", outcome.span_count)
    proof.fact("span kinds", outcome.span_kinds)
    proof.fact("spans carrying ERROR status (refusals, visible in telemetry)", outcome.error_status_spans)
    proof.fact("exported over the wire to Jaeger", outcome.exported_over_the_wire)

    proof.check(
        "the run traced end to end (run -> agent_loop -> plan -> node hierarchy present)",
        {"run", "node"}.issubset(outcome.span_kinds.keys()),
        outcome.span_kinds,
    )
    if use_guard:
        proof.check(
            "refusals are visible in the exported telemetry, not just the local journal",
            outcome.error_status_spans > 0,
            f"{outcome.error_status_spans} spans carry ERROR status "
            f"({outcome.refused_nodes} nodes refused in the journal)",
        )

    # --- the finding, either way ------------------------------------------ #
    if not use_guard:
        proof.check(
            "WITHOUT the guard: S15Code's own ledger reports zero spend no matter how long the loop runs",
            outcome.budget["spent"] == 0.0 and outcome.budget["calls"] == 0,
            f"spent=${outcome.budget['spent']:.8f}, calls={outcome.budget['calls']} after {outcome.rounds} rounds",
        )
        proof.check(
            "WITHOUT the guard: the call ceiling never trips, because it counts charges, not attempts",
            outcome.rounds >= loop_limit,
            f"loop ran all {outcome.rounds}/{loop_limit} rounds before the PROOF's own safety stop, "
            f"not the controller, ended it",
        )
        proof.check(
            "WITHOUT the guard: the TRUE bill (what a real flaky provider invoices) is unbounded and invisible",
            outcome.shadow["true_cost"] > outcome.budget["spent"],
            f"true bill ${outcome.shadow['true_cost']:.8f} vs S15Code's own ledger "
            f"${outcome.budget['spent']:.8f} — a gap the ledger cannot see",
        )
    else:
        proof.check(
            "WITH the guard: the adversary keeps asking, but almost every ask is refused before it costs anything",
            outcome.refused_nodes > 0 and outcome.shadow["true_calls"] < outcome.rounds,
            f"{outcome.rounds} rounds attempted, only {outcome.shadow['true_calls']} ever reached the "
            f"(still flaky) provider, {outcome.refused_nodes} refused pre-flight by AttemptGuardedTransport",
        )
        proof.check(
            "WITH the guard: refusals are visible graph failures, not silent truncation",
            outcome.refused_nodes > 0,
            f"{outcome.refused_nodes} nodes failed with BudgetRefused",
        )
        cap = max(1, outcome.guard["max_attempts_per_run"]) if outcome.guard else 1
        proof.check(
            "WITH the guard: the TRUE bill is bounded by the attempt ceiling, not the loop length",
            outcome.shadow["true_calls"] <= cap + 1,
            f"{outcome.shadow['true_calls']} true provider calls, attempt ceiling {cap}",
        )

    proof.record("budget", outcome.budget)
    proof.record("shadow_ledger", outcome.shadow)
    proof.record("attempt_guard", outcome.guard)
    proof.record("transport", {"calls": outcome.transport_calls, "failures": outcome.transport_failures})
    proof.record("journal_events", outcome.journal_events)
    return proof


def main() -> None:
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--loop-limit", type=int, default=DEFAULT_LOOP_LIMIT)
    extra.add_argument("--no-guard", action="store_true", help="run the attack WITHOUT the control, to show the gap")
    known, rest = extra.parse_known_args()
    args = parse(__doc__ or "", rest)
    sys.exit(run(args, loop_limit=known.loop_limit, use_guard=not known.no_guard).finish())


if __name__ == "__main__":
    main()
