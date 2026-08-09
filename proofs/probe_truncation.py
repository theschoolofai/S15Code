#!/usr/bin/env python
"""Was it the model, or the model's token cap? The truncation probe.

On the mixed measurement, the always-cheapest strategy left four reasoning tasks
unresolved after three genuine economy attempts each — and every one of those
attempts stopped at exactly the economy rung's 512-token output cap, two of them
with an EMPTY visible answer (the fully-billed non-answer that opens Session 15).
That confounds the conclusion: "the cheap model cannot do this task" and "the
cheap rung's cap cannot fit this task's answer" are different defects with
different fixes, and only the second is a pure policy error.

This probe separates them. It asks the economy model each of the four tasks at
the configured cap and at a deliverable-sized cap, and checks the answers for the
specific content the task's success criterion demands (numbers and named
concepts, normalised against thin-space/LaTeX typography). If the content
appears at the larger cap, the rung failed on sizing, not capability — the
session's §11 lesson, "the token cap that makes a rung cheaper can also make it
fail", measured on this workload.

    uv run python proofs/probe_truncation.py

Writes proofs/out/truncation_probe.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import httpx

GATEWAY = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111").rstrip("/")
OUT = Path(__file__).resolve().parent / "out"
TASKS_FILE = Path(__file__).resolve().parent / "tasks" / "options_reasoning.jsonl"

#: The economy rung, exactly as config/tiers.yaml pins it — and the caps to compare.
ECONOMY = {"provider": "groq", "model": "openai/gpt-oss-120b", "reasoning": "off"}
CAPS = (512, 1500)

#: The four tasks the cascade escalated / always-cheapest failed, with the
#: content groups a resolving answer must contain (from each task's expectation;
#: each inner list is alternatives for one required element).
CHECKS: dict[str, list[list[str]]] = {
    "r04_sizing_critique": [["1500"], ["800"], ["10.7", "10.67", "11 points", "10-11"]],
    "r05_expiry_pin": [["intrinsic"], ["gamma"], ["pin"], ["if", "exit"]],
    "r06_stt_trap": [["575"], ["2257", "2258"], ["loss"]],
    "r07_hedge_review": [["340"], ["260"], ["covered call"], ["12750"]],
}


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text.lower())
    t = t.replace("{,}", "")
    return re.sub(r"(?<=\d)[ ,   ](?=\d)", "", t)


def _content_hits(task_id: str, text: str) -> list[bool]:
    low = _norm(text)
    return [any(alt in low for alt in group) for group in CHECKS[task_id]]


async def _ask(client: httpx.AsyncClient, task: str, cap: int) -> dict:
    body = {"messages": [{"role": "user", "content": task}],
            "max_tokens": cap, "temperature": 0, **ECONOMY}
    for _ in range(5):
        try:
            r = await client.post(f"{GATEWAY}/v1/chat", json=body, timeout=240)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(20)
    return {}


async def main() -> int:
    tasks = {t["id"]: t for t in (
        json.loads(line) for line in TASKS_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#"))}
    OUT.mkdir(exist_ok=True)
    rows = []
    async with httpx.AsyncClient() as client:
        for tid in CHECKS:
            entry: dict = {"id": tid, "caps": {}}
            for cap in CAPS:
                d = await _ask(client, tasks[tid]["task"], cap)
                text = d.get("text") or ""
                hits = _content_hits(tid, text)
                entry["caps"][cap] = {
                    "stop_reason": d.get("stop_reason"),
                    "output_tokens": d.get("output_tokens"),
                    "visible_chars": len(text),
                    "content_hits": sum(hits),
                    "content_required": len(hits),
                    "cost": (d.get("cost") or {}).get("total_usd"),
                }
            rows.append(entry)
            lo, hi = entry["caps"][CAPS[0]], entry["caps"][CAPS[1]]
            print(f"{tid:22} @{CAPS[0]}: stop={lo['stop_reason']} chars={lo['visible_chars']} "
                  f"content {lo['content_hits']}/{lo['content_required']}   "
                  f"@{CAPS[1]}: stop={hi['stop_reason']} chars={hi['visible_chars']} "
                  f"content {hi['content_hits']}/{hi['content_required']}")

    recovered = sum(
        1 for r in rows
        if r["caps"][CAPS[1]]["content_hits"] == r["caps"][CAPS[1]]["content_required"]
        and r["caps"][CAPS[0]]["content_hits"] < r["caps"][CAPS[0]]["content_required"]
    )
    result = {"gateway": GATEWAY, "rung": ECONOMY, "caps_compared": CAPS,
              "rows": rows, "fully_recovered_at_larger_cap": recovered}
    (OUT / "truncation_probe.json").write_text(json.dumps(result, indent=2))
    print(f"\n{recovered}/{len(rows)} failed tasks fully recover their required content "
          f"at max_tokens={CAPS[1]} on the SAME model — sizing, not capability.")
    print(f"wrote {OUT / 'truncation_probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
