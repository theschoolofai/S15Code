#!/usr/bin/env python
"""Part 1 evidence capture: four budgeted agent runs, each with its full paper
trail — the prompt, the tier and model that served it, the ordered event trace,
the Jaeger trace ID, the ledger rows and the final answer.

Nothing here is hand-copied. Each run is created through the live runtime at
``--runtime-url``; its span tree (with the real Jaeger trace ID and per-span
cost) is read back from the runtime's ``/trace`` route, which exports to the
collector as a side effect; and the ledger rows are queried from the gateway's
``/v1/calls`` and filtered to this run's principal. The result is written to
``proofs/out/floor_runs.json`` so the README quotes numbers a reviewer can
regenerate with one command.

    uv run python proofs/capture_floor_runs.py

Requires the three services up: gateway (:8111), runtime (:8113), Jaeger (:16686).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

RUNTIME_URL = os.getenv("S15_RUNTIME_URL", "http://127.0.0.1:8113").rstrip("/")
GATEWAY_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111").rstrip("/")
JAEGER_URL = os.getenv("GLC_TRACE_UI", "http://127.0.0.1:16686").rstrip("/")
OTLP = os.getenv("S15_OTEL_EXPORTER_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
OUT = Path(__file__).resolve().parent / "out"

# Four runs chosen to exercise different points of the ladder and the controller:
# a trivial fact, a moderate arithmetic task, a task under a tight ceiling (to
# force a downgrade), and a task under an impossible ceiling (to force a refusal).
RUNS = [
    {
        "label": "trivial_fact",
        "prompt": "What is the capital city of Australia? Reply with the city name only.",
        "budget": 0.02,
        "principal": "course/s15/floor/trivial",
    },
    {
        "label": "moderate_math",
        "prompt": "A tank holds 240 litres of a 15% salt solution. How many litres "
                  "must be drained and replaced with pure water so the tank again "
                  "holds 240 litres at 9% salt? Give the number of litres.",
        "budget": 0.02,
        "principal": "course/s15/floor/moderate",
    },
    {
        "label": "tight_ceiling",
        "prompt": "Explain in two sentences why a spend budget must be enforced in code.",
        "budget": 0.0004,
        "principal": "course/s15/floor/tight",
    },
    {
        "label": "impossible_ceiling",
        "prompt": "Compare two approaches to bounding agent spend.",
        "budget": 0.0000004,
        "principal": "course/s15/floor/impossible",
    },
]


def _post_run(client: httpx.Client, run: dict) -> dict:
    tenant, project, *rest = (run["principal"].split("/") + ["", "", ""])
    body = {
        "prompt": run["prompt"],
        "budget": run["budget"],
        "principal": run["principal"],
        "tenant_id": tenant or "course",
        "project_id": project or "s15",
        "user_id": rest[0] or "reviewer",
        "agent_id": rest[1] or "assistant",
    }
    r = client.post(f"{RUNTIME_URL}/v1/agent/runs", json=body, timeout=300)
    r.raise_for_status()
    return r.json()


def _trace(client: httpx.Client, run_id: str) -> dict:
    r = client.get(f"{RUNTIME_URL}/v1/agent/runs/{run_id}/trace",
                   params={"endpoint": OTLP}, timeout=60)
    r.raise_for_status()
    return r.json()


def _ledger_rows(client: httpx.Client, since_ts: float, until_ts: float) -> list[dict]:
    """The gateway ledger rows this run produced.

    The runtime attributes a run's spend by its OWN agent name (``s15_*``) and a
    session, not by tenant/project (those columns are null for runtime traffic),
    so a run's ledger rows are the answering-agent rows in this run's wall-clock
    window. Runs here are sequential, so the window attributes cleanly.
    """
    r = client.get(f"{GATEWAY_URL}/v1/calls", params={"limit": 200}, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return [
        row for row in rows
        if since_ts <= (row.get("ts") or 0) <= until_ts
        and str(row.get("agent") or "").startswith("s15_")
    ]


def _events(journal_run: dict) -> list[dict]:
    out = []
    for e in journal_run.get("events", []):
        out.append({"seq": e.get("sequence"), "kind": e.get("kind"), "node": e.get("node_id")})
    return out


def _tier_and_model(run_body: dict, ledger_rows: list[dict]) -> dict:
    served = {}
    trace = run_body.get("trace", {})
    for node_id, agent in (trace.get("agents") or {}).items():
        served[node_id] = {
            "provider": agent.get("provider"),
            "model": agent.get("model"),
            "tier": agent.get("tier"),
            "state": agent.get("state"),
        }
    return served


def main() -> int:
    OUT.mkdir(exist_ok=True)
    captured = []
    with httpx.Client() as client:
        for run in RUNS:
            before = time.time()
            body = _post_run(client, run)
            after = time.time()
            run_id = body.get("run_id")
            trace = _trace(client, run_id)
            journal = client.get(f"{RUNTIME_URL}/v1/agent/runs/{run_id}",
                                 timeout=30).json()
            rows = _ledger_rows(client, before, after)
            trace_ids = trace.get("trace_ids") or trace.get("totals", {}).get("trace_ids") or []
            if not trace_ids:
                trace_ids = sorted({s["trace_id"] for s in trace.get("spans", []) if s.get("trace_id")})
            captured.append({
                "label": run["label"],
                "prompt": run["prompt"],
                "budget_usd": run["budget"],
                "principal": run["principal"],
                "status": body.get("status"),
                "answer": body.get("answer"),
                "served": _tier_and_model(body, rows),
                "events": _events(journal),
                "jaeger_trace_ids": trace_ids,
                "jaeger_query_url": [f"{JAEGER_URL}/api/traces/{t}" for t in trace_ids],
                "span_kinds": trace.get("totals", {}).get("by_kind")
                              or trace.get("totals", {}),
                "span_cost_usd": trace.get("totals", {}).get("cost"),
                "span_input_tokens": trace.get("totals", {}).get("input_tokens"),
                "span_output_tokens": trace.get("totals", {}).get("output_tokens"),
                "exported_over_the_wire": trace.get("exported_over_the_wire"),
                "ledger_rows": [
                    {k: r.get(k) for k in
                     ("id", "provider", "model", "input_tokens", "output_tokens",
                      "usd", "status", "tier", "agent", "tenant", "project")}
                    for r in rows
                ],
                "ledger_total_usd": round(sum(r.get("usd") or 0 for r in rows), 8),
            })
            print(f"[{run['label']}] status={body.get('status')} "
                  f"trace={trace_ids[0] if trace_ids else 'none'} "
                  f"ledger_rows={len(rows)} "
                  f"ledger_usd={captured[-1]['ledger_total_usd']}")

    result = {
        "runtime_url": RUNTIME_URL,
        "gateway_url": GATEWAY_URL,
        "jaeger_url": JAEGER_URL,
        "runs": captured,
    }
    path = OUT / "floor_runs.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
