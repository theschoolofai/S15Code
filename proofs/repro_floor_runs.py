#!/usr/bin/env python
"""Part 1 evidence: 4 runs, each captured end to end.

Not one of the six shipped proofs. p2/p3/p7 never call
`s15code.telemetry.export_run` themselves (only p4 does, because tracing is
what p4 is FOR), so this script drives four representative scenarios through
the real runtime exactly the way harness.py's own proofs do, and for each one
explicitly captures the six things the assignment's Part 1 asks for: the
prompt, the tier and model chosen, the ordered event trace, the Jaeger trace
ID, the ledger rows, and the final answer.

    python proofs/repro_floor_runs.py --offline
    python proofs/repro_floor_runs.py --offline --otel-endpoint http://127.0.0.1:4318/v1/traces
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from harness import OUT, Args, economics, parse, run_task, sync, transport_for  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s15code.telemetry import export_run  # noqa: E402

SCENARIOS = [
    {
        "label": "run1_declared_budget",
        "task": "Summarize the tradeoffs between REST and gRPC for an internal microservice.",
        "budget": 0.02,
        "principal": "floor/reproduce/run1",
    },
    {
        "label": "run2_tight_downgrade",
        "task": "Plan a short research project on bounding agent spend.",
        "budget": 0.0015,
        "principal": "floor/reproduce/run2",
    },
    {
        "label": "run3_trace_shape",
        "task": "Explain how a semantic cache reduces LLM spend.",
        "budget": 0.02,
        "principal": "floor/reproduce/run3",
    },
    {
        "label": "run4_frontier_ladder",
        "task": "Draft an incident postmortem outline.",
        "budget": 0.05,
        "principal": "floor/reproduce/run4",
    },
]


async def one(args: Args, scenario: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="s15-repro-") as workspace:
        transport, mode, detail = transport_for(args)
        outcome = await run_task(
            args, budget=scenario["budget"], transport=transport, data_dir=Path(workspace),
            task=scenario["task"],
        )
    journal = outcome.journal
    export = export_run(journal, budget=outcome.budget, endpoint=args.otel_endpoint,
                        service_name="s15code-floor-repro", principal=scenario["principal"])
    trace_ids = sorted({format(span.get_span_context().trace_id, "032x") for span in export.spans})

    calls = (outcome.result.get("budget") or {}).get("charges", [])
    events = journal["events"]

    return {
        "label": scenario["label"],
        "mode": mode,
        "prompt": scenario["task"],
        "budget_ceiling": scenario["budget"],
        "principal": scenario["principal"],
        "tier_and_model_chosen": [
            {"tier": c["tier"], "provider": c["provider"], "model": c["model"], "cost": c["cost"],
             "decision": c["decision"]}
            for c in calls
        ],
        "ordered_event_trace": [
            {"sequence": e["sequence"], "kind": e["kind"], "node_id": e.get("node_id")} for e in events
        ],
        "jaeger_trace_ids": trace_ids,
        "spans_exported": len(export.spans),
        "exported_over_the_wire": export.exported_over_the_wire,
        "ledger_rows": calls,
        "budget_snapshot": outcome.budget,
        "final_answer": str(outcome.result.get("answer") or "")[:2000],
    }


def main() -> None:
    args = parse(__doc__ or "")
    results = []
    for scenario in SCENARIOS:
        results.append(sync(one(args, scenario)))

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "repro_floor_runs.json"
    path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    for r in results:
        print(f"\n=== {r['label']} ({r['mode']} mode) ===")
        print(f"  prompt: {r['prompt']}")
        print(f"  tier/model chosen: {r['tier_and_model_chosen']}")
        print(f"  events: {[e['kind'] for e in r['ordered_event_trace']]}")
        print(f"  jaeger trace id(s): {r['jaeger_trace_ids']}")
        print(f"  spans exported: {r['spans_exported']} (over the wire: {r['exported_over_the_wire']})")
        print(f"  ledger rows: {len(r['ledger_rows'])} charge(s), spent ${r['budget_snapshot']['spent']:.8f}")
        print(f"  final answer (truncated): {r['final_answer'][:200]!r}")
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
