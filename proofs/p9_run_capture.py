#!/usr/bin/env python
"""p9 — capture one run whole, as evidence rather than as a summary.

The other proofs each assert one property and report the numbers behind it. This
one asserts almost nothing and records everything, because "reproduce the floor"
asks for six specific artefacts about a single run and they live in six different
places:

    the prompt                 the argument the run was given
    the tier and model chosen  the controller's decision, and what answered
    the ordered event trace    the durable journal, in sequence order
    the Jaeger trace id        the id the span export used, fetched back
    the ledger rows            every charge and every refusal
    the final answer           what the run actually said

Nothing here re-derives any of that. Each artefact is read from the one component
that owns it, which is the point: if the ledger and the spans and the journal
disagreed, this file would show it rather than paper over it.

    uv run python proofs/p9_run_capture.py --config-dir config/navigation \\
        --task "<any task>" --budget 0.02 --principal nav/s15/run1 --label run1
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Args, Proof, main, run_task, sync, transport_for  # noqa: E402
from p4_trace_export import fetch_trace, query_base  # noqa: E402

from s15code.telemetry import export_run  # noqa: E402

#: Journal payload keys worth keeping in the ordered event trace. The whole
#: payload can carry an entire answer, and an evidence file that inlines the
#: answer four times is not evidence, it is noise.
EVENT_FIELDS = ("role", "tier", "state", "reason", "decision", "requested_tier", "model", "provider")


def event_line(event: dict[str, Any]) -> dict[str, Any]:
    """One journal event, flattened to what a reader needs to follow the run."""
    payload = event.get("payload") or {}
    kept = {key: payload[key] for key in EVENT_FIELDS if key in payload}
    # The controller writes its per-node decisions and charges into the node's
    # own result, so the tier actually served is visible on the event that
    # carried it rather than only in the ledger.
    for decision in payload.get("budget_decisions") or []:
        kept.setdefault("decision", decision.get("action"))
        kept.setdefault("requested_tier", decision.get("requested_tier"))
        kept.setdefault("tier", decision.get("tier"))
    for charge in payload.get("metered_calls") or []:
        kept.setdefault("model", charge.get("model"))
        kept.setdefault("provider", charge.get("provider"))
    return {
        "sequence": event.get("sequence"),
        "kind": event.get("kind"),
        "node_id": event.get("node_id"),
        **kept,
    }


def run(args: Args) -> Proof:
    transport, mode, detail = transport_for(args)
    proof = Proof(name="p9_run_capture", args=args, mode=mode, mode_detail=detail)

    with tempfile.TemporaryDirectory(prefix="s15-p9-") as workspace:
        outcome = sync(run_task(args, budget=args.budget, transport=transport,
                                data_dir=Path(workspace) / "run"))

    journal, budget = outcome.journal, outcome.budget
    export = export_run(journal, budget=budget, endpoint=args.otel_endpoint,
                        principal=args.principal)
    totals = export.totals()
    trace_id = (totals["trace_ids"] or [None])[0]

    # --- 1. the prompt -----------------------------------------------------
    proof.fact("prompt", args.task)
    proof.fact("budget / principal", f"{args.budget} {budget.get('currency', 'USD')}  {args.principal}")

    # --- 2. the tier and model chosen --------------------------------------
    charges = budget.get("charges") or []
    for charge in charges:
        proof.fact(f"call {charge['sequence']} tier/model", (
            f"role {charge['role']}  requested {charge.get('requested_tier')} -> served {charge['tier']}"
            f"  ({charge['decision']})  {charge.get('provider')} / {charge.get('model')}"
        ))

    # --- 3. the ordered event trace ----------------------------------------
    events = [event_line(event) for event in journal["events"]]
    proof.fact("journal events", f"{len(events)} events, sequences "
                                 f"{events[0]['sequence'] if events else '-'}"
                                 f"..{events[-1]['sequence'] if events else '-'}")
    proof.fact("event kinds", json.dumps(
        {kind: sum(1 for e in events if e["kind"] == kind) for kind in dict.fromkeys(e["kind"] for e in events)}
    ))

    # --- 4. the Jaeger trace id --------------------------------------------
    base = query_base(args)
    backend = {"attempted": False}
    proof.fact("jaeger trace id", trace_id or "(no provider call, so no trace)")
    if base and trace_id and export.exported_over_the_wire:
        backend = {"attempted": True, **fetch_trace(base, trace_id)}
        proof.fact("jaeger url", f"{base}/trace/{trace_id}")
        proof.fact("jaeger spans", len(backend.get("trace", {}).get("spans", []))
                   if backend.get("ok") else f"NOT FOUND: {backend.get('reason')}")

    # --- 5. the ledger rows -------------------------------------------------
    for charge in charges:
        proof.fact(f"ledger row {charge['sequence']}", (
            f"{charge['input_tokens']}in/{charge['output_tokens']}out  "
            f"${charge['cost']:.8f} charged  ${charge['projected_cost']:.8f} projected  "
            f"{charge['latency_ms']:.0f} ms  node {charge['node_id']}"
        ))
    # `refusals` in a snapshot is a COUNT; `refusal_log` is the rows themselves.
    refusals = budget.get("refusal_log") or []
    for index, refusal in enumerate(refusals, start=1):
        proof.fact(f"ledger refusal {index}", (
            f"{refusal.get('node_id')}: {refusal.get('reason')}  "
            f"(requested {refusal.get('requested_tier')}, projected "
            f"${float(refusal.get('projected_cost') or 0.0):.8f}, "
            f"remaining ${float(refusal.get('remaining') or 0.0):.8f})"
        ))
    proof.fact("ledger total", (
        f"spent ${budget.get('spent', 0.0):.8f} of ${budget.get('total', 0.0):.8f}  "
        f"pressure {budget.get('pressure', 0.0):.3f}  calls {len(charges)}  "
        f"refusals {len(refusals)}"
    ))

    # --- 6. the final answer ------------------------------------------------
    answer = str(outcome.result.get("answer") or outcome.result.get("text") or "")
    proof.fact("final answer", answer.replace("\n", " ")[:400] + ("..." if len(answer) > 400 else ""))
    proof.fact("wall clock", f"{outcome.seconds:.1f}s")

    # --- the few things worth asserting even here ---------------------------
    # A refused run legitimately has no answer — that IS the outcome, and it is
    # the one a budget exists to produce. What must never happen is a run that
    # spent money and has neither an answer nor a recorded refusal.
    proof.check("the run either answered or recorded why it did not",
                bool(answer.strip()) or bool(refusals),
                f"{len(answer)} answer characters, {len(refusals)} refusals")
    proof.check("every provider call that returned is metered",
                outcome.transport_calls == len(charges),
                f"{outcome.transport_calls} transport calls, {len(charges)} ledger rows, "
                f"{outcome.transport_failures} transport failures")
    proof.check("the run stayed inside its ceiling",
                budget.get("spent", 0.0) <= budget.get("total", 0.0) + 1e-12,
                f"{budget.get('spent', 0.0):.8f} <= {budget.get('total', 0.0):.8f}")
    proof.check("span costs sum to the ledger",
                abs(totals["cost"] - budget.get("spent", 0.0)) < 1e-12,
                f"spans {totals['cost']:.8f} vs ledger {budget.get('spent', 0.0):.8f}")
    if backend.get("attempted"):
        proof.check("the trace is retrievable from Jaeger by id", bool(backend.get("ok")),
                    backend.get("query"))

    proof.record("prompt", args.task)
    proof.record("events", events)
    proof.record("ledger", budget)
    proof.record("spans", export.as_dict()["spans"])
    proof.record("trace", {"id": trace_id, "query_base": base,
                           "ui": f"{base}/trace/{trace_id}" if base and trace_id else None,
                           "backend_spans": len(backend.get("trace", {}).get("spans", []))
                           if backend.get("ok") else 0})
    proof.record("answer", answer)
    proof.record("totals", totals)
    return proof


if __name__ == "__main__":
    main(run, __doc__ or "")
