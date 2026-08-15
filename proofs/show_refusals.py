#!/usr/bin/env python
"""Print the refusals Jaeger holds for a trace, read back from the backend.

"The refusal is visible in telemetry" is a claim about what a collector received,
not about what the exporter believed it sent, so this asks Jaeger. A refused call
never becomes a provider span — there was no call — so it appears three ways
instead, and all three are printed here:

    on the run span   s15.budget.refusals, and the spend at the moment it stopped
    as a span event   budget.refused, carrying the node and the controller's reason
    on the node span  status ERROR, because a refusal is a visible graph failure
                      and not a silent truncation

    uv run python proofs/show_refusals.py <trace-id> [--query http://localhost:16686]
"""

from __future__ import annotations

import argparse
import json
import urllib.request


def fetch(query: str, trace_id: str) -> dict:
    with urllib.request.urlopen(f"{query.rstrip('/')}/api/traces/{trace_id}", timeout=10) as response:
        payload = json.load(response)
    data = payload.get("data") or []
    if not data:
        raise SystemExit(f"trace {trace_id} not found at {query}")
    return data[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("trace_id")
    parser.add_argument("--query", default="http://localhost:16686")
    parsed = parser.parse_args()
    trace = fetch(parsed.query, parsed.trace_id)

    print(f"trace {parsed.trace_id}  ({len(trace.get('spans') or [])} spans in the backend)\n")
    refusals = 0
    for span in trace.get("spans") or []:
        tags = {t["key"]: t.get("value") for t in (span.get("tags") or [])}
        kind = tags.get("s15.span.kind")
        if kind == "run":
            print(f"run span   {span['operationName']}")
            for key in sorted(k for k in tags if k.startswith("s15.budget.")):
                print(f"             {key} = {tags[key]}")
        if tags.get("otel.status_code") == "ERROR" or tags.get("error"):
            print(f"ERROR span {span['operationName']:<36} [{kind}]  "
                  f"{tags.get('otel.status_description') or ''}")
        for log in span.get("logs") or []:
            fields = {f["key"]: f.get("value") for f in (log.get("fields") or [])}
            if fields.get("event") == "budget.refused":
                refusals += 1
                print(f"REFUSAL    node {fields.get('s15.node.id')}: {fields.get('s15.budget.reason')}")
                print(f"             spent {fields.get('s15.budget.spent')}, "
                      f"remaining {fields.get('s15.budget.remaining')}")
    print(f"\n{refusals} budget.refused event(s) present in the backend's copy of this trace")


if __name__ == "__main__":
    main()
