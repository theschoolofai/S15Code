"""Write committed evidence summaries under proofs/evidence/ (not gitignored)."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "proofs" / "out"
EVIDENCE = ROOT / "proofs" / "evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)


def main() -> None:
    p1 = json.loads((OUT / "p1_cost_per_task_cloud_cost_offline.json").read_text(encoding="utf-8"))
    wrong = {
        "task": "c02_mtok_math",
        "strategy": "C budget_aware",
        "note": "climbed economy->standard->frontier and stayed unresolved",
        "cost_usd": 0.00371,
    }
    for row in p1["detail"]["per_task"].get("C", []):
        if row.get("task_id") == "c02_mtok_math":
            wrong.update(
                {
                    "cost_usd": row.get("cost"),
                    "tiers_charged": row.get("tiers_charged"),
                    "status": row.get("status"),
                    "attempt_count": row.get("attempt_count"),
                }
            )
            break
    keep_keys = (
        "A always_frontier",
        "B always_cheapest",
        "C budget_aware",
        "signature failure mode",
        "ladder",
        "tasks",
        "judge panel",
        "judge bar",
    )
    p1_out = {
        "proof": p1["proof"],
        "ok": p1["ok"],
        "mode": p1["mode"],
        "facts": {k: v for k, v in p1["facts"].items() if k in keep_keys},
        "finding": p1["detail"].get("finding"),
        "wrong_routing": wrong,
        "source_full_local": "proofs/out/p1_cost_per_task_cloud_cost_offline.json (gitignored)",
    }
    (EVIDENCE / "p1_cloud_cost_offline_summary.json").write_text(
        json.dumps(p1_out, indent=2), encoding="utf-8"
    )

    p4 = json.loads((OUT / "p4_trace_export_live_jaeger.json").read_text(encoding="utf-8"))
    tid = p4["facts"]["trace ids"][0]
    p4_out = {
        "proof": p4["proof"],
        "ok": p4["ok"],
        "arguments": p4["arguments"],
        "facts": p4["facts"],
        "jaeger_ui": f"http://127.0.0.1:16686/trace/{tid}",
        "source_full_local": "proofs/out/p4_trace_export_live_jaeger.json (gitignored)",
    }
    (EVIDENCE / "p4_live_jaeger_summary.json").write_text(
        json.dumps(p4_out, indent=2), encoding="utf-8"
    )
    print("wrote", list(EVIDENCE.glob("*.json")))


if __name__ == "__main__":
    main()
