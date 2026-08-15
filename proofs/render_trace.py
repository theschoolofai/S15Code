#!/usr/bin/env python
"""Render a trace from Jaeger as a text tree, with tokens and cost per span.

The README has to show a span hierarchy with costs on it, and a screenshot of a
web UI is not evidence anyone can check. This reads the trace back out of the
BACKEND — not out of the exporter's own copy — so what it prints is what Jaeger
actually holds, and anyone with the trace id can print the same thing.

    uv run python proofs/render_trace.py <trace-id> [--query http://localhost:16686]
"""

from __future__ import annotations

import argparse
import json
import urllib.request

COST = "s15.cost"
KIND = "s15.span.kind"


def fetch(query: str, trace_id: str) -> dict:
    with urllib.request.urlopen(f"{query.rstrip('/')}/api/traces/{trace_id}", timeout=10) as response:
        payload = json.load(response)
    data = payload.get("data") or []
    if not data:
        raise SystemExit(f"trace {trace_id} not found at {query}")
    return data[0]


def render(trace: dict) -> str:
    spans = trace.get("spans") or []
    tags = {s["spanID"]: {t["key"]: t.get("value") for t in (s.get("tags") or [])} for s in spans}
    children: dict[str | None, list[dict]] = {}
    for span in spans:
        parent = None
        for ref in span.get("references") or []:
            if ref.get("refType") == "CHILD_OF":
                parent = ref.get("spanID")
        children.setdefault(parent, []).append(span)
    for bucket in children.values():
        bucket.sort(key=lambda s: s.get("startTime", 0))

    lines: list[str] = []
    # Only PROVIDER CALLS are summed. The run span carries the run's total as an
    # attribute too, so adding every span that has a cost on it double-counts the
    # whole run — which is exactly the kind of plausible, self-consistent wrong
    # number this session is about.
    total = 0.0
    root_total: float | None = None

    def walk(span: dict, depth: int) -> None:
        nonlocal total, root_total
        attrs = tags.get(span["spanID"], {})
        cost = attrs.get(COST)
        kind = str(attrs.get(KIND, "?"))
        if cost is not None and kind == "run":
            root_total = float(cost)
        duration_ms = (span.get("duration") or 0) / 1000.0
        bits = [f"{duration_ms:8.1f} ms"]
        if attrs.get("gen_ai.request.model"):
            bits.append(f"{attrs.get('gen_ai.provider.name')}/{attrs['gen_ai.request.model']}")
        if attrs.get("gen_ai.usage.input_tokens") is not None:
            bits.append(f"{attrs.get('gen_ai.usage.input_tokens')}in/"
                        f"{attrs.get('gen_ai.usage.output_tokens')}out")
        if cost is not None:
            if kind == "provider_call":
                total += float(cost)
            bits.append(f"${float(cost):.8f}")
        prefix = "  " * depth + ("└─ " if depth else "")
        lines.append(f"{prefix}{span['operationName']:<44} [{kind}]  " + "  ".join(bits))
        for child in children.get(span["spanID"], []):
            walk(child, depth + 1)

    for root in children.get(None, []):
        walk(root, 0)
    lines.append("")
    lines.append(f"{'sum of provider_call span costs':<46}  ${total:.8f}")
    if root_total is not None:
        lines.append(f"{'run span s15.cost (the ledger total)':<46}  ${root_total:.8f}")
        agrees = abs(root_total - total) < 1e-12
        lines.append(f"{'they agree':<46}  {'yes' if agrees else f'NO — delta ${root_total - total:.8f}'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("trace_id")
    parser.add_argument("--query", default="http://localhost:16686")
    parsed = parser.parse_args()
    print(render(fetch(parsed.query, parsed.trace_id)))


if __name__ == "__main__":
    main()
