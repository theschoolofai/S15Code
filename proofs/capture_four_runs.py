#!/usr/bin/env python
"""Capture four runs end-to-end for Part 1: prompt, tier and model chosen,
ordered event trace, Jaeger trace ID, ledger rows, and final answer.

Runs sit on the exact same code path as ``proofs/harness.py``, so the numbers
here are the same numbers the proofs assert against — just formatted so a
reviewer can read one page and see what happened.

Offline transport is used deliberately so the run is deterministic; spans are
exported over the wire to whatever ``S15_OTEL_EXPORTER_ENDPOINT`` names (a
local Jaeger during Part 1) and read back by trace id.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import OfflineTransport  # noqa: E402

from s15code.core.memory import MemoryScope  # noqa: E402
from s15code.core.memory.embeddings import DeterministicEmbedder  # noqa: E402
from s15code.economics import EconomicsConfig, MeteredTransport  # noqa: E402
from s15code.runtime import AgentRuntime  # noqa: E402
from s15code.telemetry import export_run  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OTLP = os.getenv("S15_OTEL_EXPORTER_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
JAEGER_UI = os.getenv("S15_TRACE_QUERY_URL", "http://127.0.0.1:16686").rstrip("/")

TASK_A = "What is the capital city of Australia? Reply with the city name only."
TASK_B = (
    "A cyclist rides 30 km out at 20 km/h and returns along the same 30 km at 30 km/h. "
    "What is the average speed for the whole round trip, in km/h? Give one number."
)

SCENARIOS = [
    # (label, task, budget) — spans the four interesting controller states.
    ("generous_frontier", TASK_A, 1.00),   # plenty of headroom -> frontier
    ("declared_downgrade", TASK_B, 0.02),   # 2c ceiling -> standard rung
    ("tight_downgrade", TASK_B, 0.005),     # tighter -> economy rung
    ("impossible_refuse", TASK_B, 0.00001), # below every rung -> refusal
]


async def one_run(label, task, budget, workspace):
    runtime = AgentRuntime(root=workspace)
    runtime.memory.embedder = DeterministicEmbedder(128)
    transport = MeteredTransport(OfflineTransport())
    scope = MemoryScope("demo", "part1", "you", None)
    started = time.time()
    result = await runtime.run(
        prompt=task,
        scope=scope,
        source_uri="proof://capture",
        source_author="capture-four-runs",
        respond_as="text",
        budget=budget,
        principal="demo/you",
        transport=transport,
        economics=EconomicsConfig.load(),
    )
    seconds = time.time() - started
    journal = {
        "run_id": result["run_id"],
        "finished": (result.get("graph") or {}).get("finished", False),
        "nodes": (result.get("graph") or {}).get("nodes") or {},
        "edges": (result.get("graph") or {}).get("edges") or (),
        "events": result.get("events") or [],
    }
    # Re-export from the tape so we know the trace id we can query in Jaeger.
    export = export_run(journal, budget=result.get("budget"),
                        endpoint=OTLP, service_name="s15code-part1",
                        principal=result.get("principal"))
    runtime.close()
    return {
        "label": label,
        "task": task,
        "budget": budget,
        "seconds": round(seconds, 3),
        "result": result,
        "export": export,
        "transport_calls": transport.calls,
    }


def fetch_jaeger(trace_id, attempts=12, pause=1.0):
    url = f"{JAEGER_UI}/api/traces/{trace_id}"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            spans = ((data.get("data") or [{}])[0]).get("spans") or []
            if spans:
                return {"ok": True, "spans_in_backend": len(spans), "query": url}
        except urllib.error.URLError as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(pause)
            continue
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(pause)
            continue
        time.sleep(pause)
    return {"ok": False, "reason": "no spans returned by backend", "query": url}


def event_trace(events):
    ordered = []
    for e in events:
        payload = e.get("payload") or {}
        # keep the payload lean so the report is readable.
        keep = {k: payload[k] for k in ("add", "connect", "reason", "node_id", "state",
                                        "provider", "model", "tier", "cost", "input_tokens",
                                        "output_tokens", "decision", "requested_tier")
                if k in payload}
        ordered.append({
            "seq": e.get("sequence"),
            "kind": e.get("kind"),
            "node_id": e.get("node_id"),
            "payload": keep,
        })
    return ordered


def ledger_rows(budget_snapshot):
    if not budget_snapshot:
        return []
    return [
        {
            "seq": i + 1,
            "node_id": c.get("node_id"),
            "requested_tier": c.get("requested_tier"),
            "tier": c.get("tier"),
            "model": c.get("model"),
            "provider": c.get("provider"),
            "input_tokens": c.get("input_tokens"),
            "output_tokens": c.get("output_tokens"),
            "cost": c.get("cost"),
            "decision": c.get("decision"),
        }
        for i, c in enumerate(budget_snapshot.get("charges") or [])
    ]


def refusals(budget_snapshot):
    if not budget_snapshot:
        return []
    return list(budget_snapshot.get("refusal_log") or [])


def format_run(rec, header_index):
    r = rec["result"]
    export = rec["export"]
    budget = r.get("budget") or {}
    trace_ids = export.totals()["trace_ids"]
    trace_id = trace_ids[0] if trace_ids else "(none)"
    jaeger = fetch_jaeger(trace_id) if trace_id != "(none)" else {"ok": False, "reason": "no trace"}

    lines = []
    P = lines.append
    P(f"\n{'=' * 88}")
    P(f"RUN {header_index}: {rec['label']}")
    P("=" * 88)
    P(f"prompt      : {rec['task']}")
    P(f"budget      : {rec['budget']} USD   principal: {r.get('principal')}   respond_as: text")
    P(f"run id      : {r['run_id']}   status: {r.get('status')}   seconds: {rec['seconds']}")

    # tier and model chosen (per node)
    agents = ((r.get("trace") or {}).get("agents") or {})
    P("\nnodes (tier / model chosen):")
    for node_id, meta in agents.items():
        tier = meta.get("tier") or "-"
        model = meta.get("model") or "-"
        provider = meta.get("provider") or "-"
        state = meta.get("state") or "-"
        P(f"  {node_id:<14}  state={state:<10} tier={tier:<10} provider={provider:<12} model={model}")

    # ordered event trace
    P("\nordered event trace (journal):")
    for ev in event_trace(r.get("events") or []):
        payload = json.dumps(ev["payload"], separators=(",", ":")) if ev["payload"] else ""
        P(f"  #{ev['seq']:>2}  {ev['kind']:<18} node={ev['node_id'] or '-':<12} {payload}")

    # ledger rows
    P("\nledger rows (budget.charges):")
    rows = ledger_rows(budget)
    if not rows:
        P("  (no charges — the controller refused every admission)")
    for row in rows:
        P(f"  #{row['seq']}  node={row['node_id']}  req={row['requested_tier']} -> served={row['tier']}"
          f"  model={row['model']}  in={row['input_tokens']}  out={row['output_tokens']}"
          f"  cost=${row['cost']:.8f}  decision={row['decision']}")
    refs = refusals(budget)
    if refs:
        P("  refusal log:")
        for ref in refs:
            P(f"    - {ref}")

    P(f"\nledger totals: spent=${budget.get('spent', 0):.8f} of ${budget.get('total', 0):.6f}"
      f"  calls={budget.get('calls', 0)}  downgrades={budget.get('downgrades', 0)}"
      f"  refusals={budget.get('refusals', 0)}")

    # Jaeger trace id + backend round-trip
    P(f"\nJaeger trace id : {trace_id}")
    P(f"Jaeger view URL : {JAEGER_UI}/trace/{trace_id}")
    if jaeger.get("ok"):
        P(f"Jaeger backend  : {jaeger['spans_in_backend']} spans returned by {jaeger['query']}")
    else:
        P(f"Jaeger backend  : NOT AVAILABLE ({jaeger.get('reason')})  query={jaeger.get('query')}")

    # final answer
    answer = (r.get("answer") or "").strip() or "(no answer produced)"
    P("\nfinal answer:")
    for line in answer.splitlines() or [answer]:
        P(f"  {line}")

    return "\n".join(lines), {
        "label": rec["label"],
        "run_id": r["run_id"],
        "prompt": rec["task"],
        "budget": rec["budget"],
        "principal": r.get("principal"),
        "status": r.get("status"),
        "nodes": agents,
        "events": event_trace(r.get("events") or []),
        "ledger": {
            "total": budget.get("total"),
            "spent": budget.get("spent"),
            "calls": budget.get("calls"),
            "downgrades": budget.get("downgrades"),
            "refusals": budget.get("refusals"),
            "charges": ledger_rows(budget),
            "refusal_log": refusals(budget),
        },
        "trace_id": trace_id,
        "jaeger": jaeger,
        "answer": r.get("answer") or "",
    }


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    import tempfile
    records = []
    with tempfile.TemporaryDirectory(prefix="s15-part1-") as tmp:
        workspace = Path(tmp)
        for i, (label, task, budget) in enumerate(SCENARIOS):
            rec = await one_run(label, task, budget, workspace / label)
            records.append(rec)

    all_text = []
    all_json = []
    for i, rec in enumerate(records, 1):
        text, data = format_run(rec, i)
        all_text.append(text)
        all_json.append(data)

    report = "\n".join(all_text) + "\n"
    (OUT / "part1_four_runs.txt").write_text(report, encoding="utf-8")
    (OUT / "part1_four_runs.json").write_text(json.dumps(all_json, indent=2, default=str), encoding="utf-8")
    print(report)
    print(f"\nWrote: {OUT / 'part1_four_runs.txt'}")
    print(f"Wrote: {OUT / 'part1_four_runs.json'}")


if __name__ == "__main__":
    asyncio.run(main())
