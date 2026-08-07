#!/usr/bin/env python
"""Capture one Part 2 run end-to-end, export its OTel spans to Jaeger, and
read the trace back by id so the span hierarchy with per-span costs can be
verified against a real backend.

The run uses ``config/part2`` and the offline transport, so the numbers are
deterministic and the span export is the real implementation — only the
network answer is simulated. The Jaeger UI URL for the trace is printed so a
reviewer can open it in a browser.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
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
from s15code.telemetry.spans import (  # noqa: E402
    COST, GEN_AI_INPUT_TOKENS, GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER, GEN_AI_REQUEST_MODEL, SPAN_KIND,
)

OUT = Path(__file__).resolve().parent / "out"
OTLP = os.getenv("S15_OTEL_EXPORTER_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
JAEGER = os.getenv("S15_TRACE_QUERY_URL", "http://127.0.0.1:16686").rstrip("/")

# A hard ticket from proofs/tasks/tickets.jsonl. Picked deliberately: hard tasks
# are where the cascade climbs the whole ladder (economy -> standard -> frontier),
# so the trace shows the interesting shape a triage question actually produces.
TASK = (
    "Ticket: user cannot access shared drive S:. "
    "Return JSON {category,severity,first_step}."
)


def fetch(trace_id: str, *, attempts: int = 12, pause: float = 1.0) -> dict:
    url = f"{JAEGER}/api/traces/{trace_id}"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            spans = ((data.get("data") or [{}])[0]).get("spans") or []
            if spans:
                return {"ok": True, "spans": spans, "query": url}
        except urllib.error.URLError:
            pass
        except Exception:
            pass
        time.sleep(pause)
    return {"ok": False, "reason": "no spans returned", "query": url}


def hierarchy(spans: list, capture: dict) -> list[dict]:
    tags = {s["spanID"]: {t["key"]: t.get("value") for t in (s.get("tags") or [])} for s in spans}
    by_id = {s["spanID"]: s for s in spans}
    rows = []
    for s in spans:
        t = tags.get(s["spanID"], {})
        parent = None
        for ref in (s.get("references") or []):
            if ref.get("refType") == "CHILD_OF":
                parent = ref.get("spanID")
        rows.append({
            "span_id": s["spanID"], "name": s["operationName"], "parent": parent,
            "kind": t.get(SPAN_KIND, "?"),
            "duration_us": s.get("duration"),
            "cost": t.get(COST),
            "model": t.get(GEN_AI_REQUEST_MODEL),
            "provider": t.get(GEN_AI_PROVIDER),
            "input_tokens": t.get(GEN_AI_INPUT_TOKENS),
            "output_tokens": t.get(GEN_AI_OUTPUT_TOKENS),
        })
    return rows


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s15-part2-trace-") as tmp:
        workspace = Path(tmp)
        runtime = AgentRuntime(root=workspace)
        runtime.memory.embedder = DeterministicEmbedder(128)
        transport = MeteredTransport(OfflineTransport())
        scope = MemoryScope("demo", "part2", "you", None)
        started = time.time()
        result = await runtime.run(
            prompt=TASK,
            scope=scope,
            source_uri="proof://part2-trace",
            source_author="capture-part2-trace",
            respond_as="text",
            budget=0.05,
            principal="you/part2",
            transport=transport,
            economics=EconomicsConfig.load(ROOT / "config" / "part2"),
        )
        seconds = time.time() - started
        journal = {
            "run_id": result["run_id"],
            "finished": (result.get("graph") or {}).get("finished", False),
            "nodes": (result.get("graph") or {}).get("nodes") or {},
            "edges": (result.get("graph") or {}).get("edges") or (),
            "events": result.get("events") or [],
        }
        export = export_run(journal, budget=result.get("budget"),
                            endpoint=OTLP, service_name="s15code-part2",
                            principal=result.get("principal"))
        runtime.close()

    trace_ids = export.totals()["trace_ids"]
    trace_id = trace_ids[0] if trace_ids else "(none)"
    backend = fetch(trace_id) if trace_id != "(none)" else {"ok": False, "reason": "no trace id"}

    P = print
    P(f"\ntask     : {TASK}")
    P(f"budget   : 0.05 USD  principal: you/part2  config: config/part2")
    P(f"seconds  : {seconds:.3f}   run_id: {result['run_id']}   status: {result.get('status')}")
    budget = result.get("budget") or {}
    P(f"spend    : ${budget.get('spent', 0):.6f}  calls: {budget.get('calls', 0)}  "
      f"downgrades: {budget.get('downgrades', 0)}  refusals: {budget.get('refusals', 0)}")

    P("\nledger rows (per provider call):")
    for i, c in enumerate(budget.get("charges") or [], 1):
        P(f"  #{i}  node={c.get('node_id')}  req={c.get('requested_tier')} -> "
          f"served={c.get('tier')}  model={c.get('model')}  "
          f"in={c.get('input_tokens')} out={c.get('output_tokens')} "
          f"cost=${c.get('cost'):.8f}  decision={c.get('decision')}")

    P(f"\nJaeger trace id  : {trace_id}")
    P(f"Jaeger view URL  : {JAEGER}/trace/{trace_id}")
    if backend.get("ok"):
        rows = hierarchy(backend["spans"], journal)
        P(f"Jaeger backend   : {len(rows)} spans returned by {backend['query']}")
        P("\nspan hierarchy (kind, name, parent -> cost/tokens on provider calls):")
        for r in rows:
            parent = r["parent"] or "(root)"
            base = f"  [{r['kind']:<12}] {r['name']:<28}  parent={parent[:8]}  dur={r['duration_us']}us"
            if r["kind"] == "provider_call":
                base += (f"  cost=${float(r['cost'] or 0):.8f}"
                         f"  model={r['model']}  in={r['input_tokens']} out={r['output_tokens']}")
            P(base)
        sum_cost = sum(float(r["cost"] or 0) for r in rows if r["kind"] == "provider_call")
        P(f"\nsum of provider-call span cost = ${sum_cost:.8f}  vs ledger spent = ${budget.get('spent', 0):.8f}")
    else:
        P(f"Jaeger backend   : NOT AVAILABLE ({backend.get('reason')})  query={backend.get('query')}")

    payload = {
        "task": TASK,
        "budget": 0.05,
        "principal": "you/part2",
        "config_dir": str((ROOT / "config" / "part2").as_posix()),
        "run_id": result["run_id"],
        "status": result.get("status"),
        "spend": budget.get("spent"),
        "charges": budget.get("charges"),
        "trace_id": trace_id,
        "jaeger_url": f"{JAEGER}/trace/{trace_id}",
        "backend": backend if backend.get("ok") else {"ok": False, "reason": backend.get("reason")},
        "backend_spans": hierarchy(backend["spans"], journal) if backend.get("ok") else [],
    }
    (OUT / "part2_trace_with_costs.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    P(f"\nWrote {OUT / 'part2_trace_with_costs.json'}")


if __name__ == "__main__":
    asyncio.run(main())
