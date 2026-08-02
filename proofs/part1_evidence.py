#!/usr/bin/env python
"""Part 1 evidence capture: four budgeted runs, recorded in full.

The assignment asks for six things per run, and asks for them together rather
than scattered across six tools:

    the prompt · the tier and model chosen · the ordered event trace ·
    the Jaeger trace id · the ledger rows · the final answer

So this walks four prompts chosen to exercise different paths through the same
runtime — a plain answer, a memory write, a structured/format request, and a
multi-step reasoning task — and writes one markdown table per run plus the whole
thing as JSON. It asserts nothing that the numbered proofs already assert; it is
a recorder, and it is deliberately separate from them for that reason.

Each run goes through the real runtime with a real ceiling and a real principal,
the journal is exported through the real OTel SDK, and when a collector is
reachable the trace is fetched back out of it by id so the recorded trace id is
one a reviewer can actually open.

    python proofs/part1_evidence.py --budget 0.02 --principal tradelab/s15/sujit \
        --otel-endpoint http://127.0.0.1:4328/v1/traces
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from harness import OUT, Args, economics, parse, run_task, sync, transport_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from p4_trace_export import fetch_trace, query_base  # noqa: E402

from s15code.economics.controller import DECISION_KEY, METER_KEY  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

#: Four prompts, chosen to take different routes through the same graph rather
#: than to be four of the same thing. Nothing in the runtime reads this list;
#: swap them for four of your own and the recorder works unchanged.
RUNS: tuple[tuple[str, str], ...] = (
    ("plain_answer",
     "In exactly two sentences, explain why a spend ceiling must be enforced in code "
     "rather than asked for in a prompt."),
    ("memory_write",
     "Remember that our margin-shortfall penalty review happens on the first Monday of "
     "every month, then tell me what you have recorded."),
    ("structured_format",
     "Return a single JSON object and nothing else, with exactly two keys: \"tier\" set to "
     "the string \"economy\" and \"max_tokens\" set to the number 512."),
    ("multi_step",
     "A client shorts 300 shares at 1,450 rupees, then covers 200 at 1,410 and 100 at 1,475. "
     "Ignoring charges, what is the net realised profit or loss in rupees?"),
)


def ledger_rows(budget: dict[str, Any]) -> list[dict[str, Any]]:
    """The ledger, one row per charge, in the order it was billed."""
    return [
        {
            "node_id": charge.get("node_id"),
            "role": charge.get("role"),
            "requested_tier": charge.get("requested_tier"),
            "tier": charge.get("tier"),
            "decision": charge.get("decision"),
            "provider": charge.get("provider"),
            "model": charge.get("model"),
            "input_tokens": charge.get("input_tokens"),
            "output_tokens": charge.get("output_tokens"),
            "projected_cost": charge.get("projected_cost"),
            "cost": charge.get("cost"),
            "latency_ms": charge.get("latency_ms"),
        }
        for charge in budget.get("charges") or []
    ]


def event_trace(journal: dict[str, Any]) -> list[dict[str, Any]]:
    """The ordered event trace, with the meter and decision fields kept.

    Payloads are summarised rather than dumped: the point of this column is the
    ORDER and the economics of each step, and a full payload would bury both.
    """
    rows = []
    for event in journal.get("events") or []:
        payload = event.get("payload") or {}
        calls = payload.get(METER_KEY) or []
        decisions = payload.get(DECISION_KEY) or []
        rows.append({
            "sequence": event.get("sequence"),
            "kind": event.get("kind"),
            "node_id": event.get("node_id"),
            "tier": payload.get("tier") or (calls[0].get("tier") if calls else None),
            "model": calls[0].get("model") if calls else None,
            "cost": sum(float(c.get("cost") or 0.0) for c in calls) or None,
            "budget_decisions": [d.get("action") for d in decisions] or None,
            "error": payload.get("error"),
        })
    return rows


def final_answer(result: dict[str, Any]) -> str:
    """Whatever the run put in front of the user, however it was shaped."""
    for key in ("answer", "text", "response", "output"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    graph = (result.get("graph") or {}).get("nodes") or {}
    for node_id in reversed(list(graph)):
        node_result = (graph[node_id] or {}).get("result") or {}
        for key in ("answer", "text"):
            value = node_result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def capture_one(args: Args, name: str, prompt: str, data_dir: Path) -> dict[str, Any]:
    """One run, recorded. Nothing here changes what the runtime would have done."""
    transport, mode, detail = transport_for(args)
    run_args = Args(**{**args.__dict__, "task": prompt})
    outcome = sync(run_task(run_args, budget=args.budget, transport=transport,
                            data_dir=data_dir / name))
    journal, budget = outcome.journal, outcome.budget
    export = export_run(journal, budget=budget, endpoint=args.otel_endpoint,
                        service_name="s15code-agent", principal=args.principal)
    trace_ids = sorted({f"{span.context.trace_id:032x}" for span in export.spans})

    backend: dict[str, Any] = {"fetched": False}
    base = query_base(args)
    if base and trace_ids and export.exported_over_the_wire:
        found = fetch_trace(base, trace_ids[0])
        backend = {
            "fetched": bool(found.get("ok")),
            "query": found.get("query"),
            "spans": len((found.get("trace") or {}).get("spans") or []),
            "reason": found.get("reason"),
            "ui": f"{base}/trace/{trace_ids[0]}",
        }

    rows = ledger_rows(budget)
    return {
        "name": name,
        "mode": mode,
        "mode_detail": detail,
        "prompt": prompt,
        "run_id": journal.get("run_id"),
        "tiers_chosen": [row["tier"] for row in rows],
        "requested_tiers": [row["requested_tier"] for row in rows],
        "models_chosen": [row["model"] for row in rows],
        "providers": [row["provider"] for row in rows],
        "decisions": [row["decision"] for row in rows],
        "event_trace": event_trace(journal),
        "trace_ids": trace_ids,
        "trace_backend": backend,
        "ledger_rows": rows,
        "ledger_total": budget.get("spent"),
        "ceiling": budget.get("total"),
        "refusals": budget.get("refusals"),
        "downgrades": budget.get("downgrades"),
        "final_answer": final_answer(outcome.result),
        "seconds": outcome.seconds,
        "transport_calls": outcome.transport_calls,
        "transport_failures": outcome.transport_failures,
    }


def as_markdown(captures: list[dict[str, Any]], config_summary: dict[str, Any]) -> str:
    """The evidence, in the shape a README section wants it."""
    out = ["# Part 1 evidence — four budgeted runs", ""]
    out += [f"- ladder: `{' < '.join(config_summary['tier_order'])}`",
            f"- tier models: `{json.dumps(config_summary['tier_models'])}`",
            f"- mode: `{captures[0]['mode']}`" if captures else "", ""]
    for cap in captures:
        out += [f"## Run {cap['name']} — `{cap['run_id']}`", "",
                f"**Prompt.** {cap['prompt']}", "",
                f"**Tier and model chosen.** requested `{cap['requested_tiers']}` → "
                f"served `{cap['tiers_chosen']}` on `{cap['models_chosen']}` "
                f"(provider `{cap['providers']}`, decision `{cap['decisions']}`)", "",
                f"**Jaeger trace id.** `{(cap['trace_ids'] or ['-'])[0]}` — "
                f"{cap['trace_backend'].get('spans', 0)} spans read back from the backend"
                f" at {cap['trace_backend'].get('query') or 'no collector'}", "",
                "**Ordered event trace.**", "",
                "| seq | event | node | tier | model | cost | decisions |",
                "|---|---|---|---|---|---|---|"]
        for row in cap["event_trace"]:
            cost = f"{row['cost']:.8f}" if row["cost"] else ""
            out.append(f"| {row['sequence']} | {row['kind']} | {row['node_id'] or ''} | "
                       f"{row['tier'] or ''} | {row['model'] or ''} | {cost} | "
                       f"{','.join(row['budget_decisions'] or [])} |")
        out += ["", "**Ledger rows.**", "",
                "| node | role | requested | served | decision | provider/model | in | out | projected | charged |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for row in cap["ledger_rows"]:
            out.append(
                f"| {row['node_id']} | {row['role']} | {row['requested_tier']} | {row['tier']} | "
                f"{row['decision']} | {row['provider']}/{row['model']} | {row['input_tokens']} | "
                f"{row['output_tokens']} | {row['projected_cost']:.8f} | {row['cost']:.8f} |")
        out += ["", f"Ledger total **{cap['ledger_total']:.8f}** of a "
                    f"{cap['ceiling']:.8f} ceiling; {cap['refusals']} refusals, "
                    f"{cap['downgrades']} downgrades, {cap['seconds']:.1f}s wall clock.", "",
                "**Final answer.**", "", "```", (cap["final_answer"] or "(empty)").strip(), "```", ""]
    return "\n".join(out)


def main() -> None:
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument("--out-name", default="part1_evidence")
    known, rest = extra.parse_known_args()
    args = parse(__doc__ or "", rest)
    config = economics(args)

    captures = []
    with tempfile.TemporaryDirectory(prefix="s15-part1-") as workspace:
        for name, prompt in RUNS:
            print(f"  capturing {name} ...", flush=True)
            cap = capture_one(args, name, prompt, Path(workspace))
            captures.append(cap)
            print(f"    tier={cap['tiers_chosen']} model={cap['models_chosen']} "
                  f"spent={cap['ledger_total']:.8f} trace={(cap['trace_ids'] or ['-'])[0]} "
                  f"backend_spans={cap['trace_backend'].get('spans', 0)}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    summary = config.describe()
    (OUT / f"{known.out_name}.json").write_text(
        json.dumps({"config": summary, "runs": captures}, indent=2, default=str), encoding="utf-8")
    (OUT / f"{known.out_name}.md").write_text(as_markdown(captures, summary), encoding="utf-8")
    print(f"\nwrote {OUT / (known.out_name + '.json')}")
    print(f"wrote {OUT / (known.out_name + '.md')}")
    missing = [cap["name"] for cap in captures if not cap["trace_ids"]]
    if missing:
        print(f"WARNING: no trace id captured for {missing}")
    sys.exit(0)


if __name__ == "__main__":
    main()
