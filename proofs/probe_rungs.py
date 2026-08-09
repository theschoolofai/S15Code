#!/usr/bin/env python
"""Cross-rung correctness probe: ask every rung of the ladder each task directly,
and check the answer against a verified ground-truth key.

p1 measures cost per RESOLVED task through the generic judge. This probe answers
a sharper question the judge cannot: for THIS task, which rungs are actually
right? The key here is a hidden answer sheet used only by this probe (never by
the answering ladder or the judge), so it can catch a case where price does not
order competence — the session's §11 lesson — in this task domain.

    uv run python proofs/probe_rungs.py

Writes proofs/out/rung_probe.json.
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
TASKS = Path(__file__).resolve().parent / "tasks" / "options_trading.jsonl"

# The three rungs of config/tiers.yaml, named so the probe follows the ladder.
RUNGS = [
    ("economy", "groq", "openai/gpt-oss-120b", {"reasoning": "off"}),
    ("standard", "gemini", "gemini-3.1-flash-lite", {"reasoning": "off"}),
    ("frontier", "bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", {}),
]

SYSTEM = "Answer the user directly and concisely. State the final numeric result plainly."

# Ground truth: the set of number/word tokens an answer MUST contain to be
# correct. Verified in Python before being written. Used ONLY here.
TRUTH: dict[str, list[str]] = {
    "o01_premium": ["9000"],
    "o02_atm_strike": ["23850", "23,850"],
    "o03_moneyness": ["out-of-the-money", "out of the money", "otm"],
    "o04_pct_move": ["1.00", "1%"],
    "o05_lots_capital": ["13"],
    "o06_bull_call_be": ["23880", "23,880"],
    "o07_bull_call_maxloss": ["6000", "6,000"],
    "o08_expiry_weekly": ["thursday", "12 march", "march 12", "12 mar"],
    "o09_pnl_expiry": ["7500", "7,500"],
    "o10_brokerage": ["160"],
    "o11_condor_maxprofit": ["4500", "4,500"],
    "o12_condor_maxloss": ["10500", "10,500"],
    "o13_condor_be": ["23740", "23,740"],   # also expects 24060, checked below
    "o14_ratio_profit": ["14250", "14,250"],
    "o15_ratio_ube": ["24190", "24,190"],
    "o16_return_on_margin": ["42.86", "42.85", "42.9"],
    "o17_expiry_holiday": ["wednesday", "25 march", "march 25", "25 mar"],
    "o18_straddle_be": ["78850", "78,850"],  # also 81150, checked below
}
# Tasks that require BOTH tokens present.
TRUTH_SECOND: dict[str, list[str]] = {
    "o13_condor_be": ["24060", "24,060"],
    "o18_straddle_be": ["81150", "81,150"],
}


def _norm(text: str) -> str:
    """Normalise before matching: some models write 24 060 (thin space), 24{,}165
    (LaTeX) or 24,060 for the same number. A scorer that misses those would blame
    the model for its typography."""
    t = unicodedata.normalize("NFKC", text.lower())
    t = t.replace("{,}", "")
    t = re.sub(r"(?<=\d)[ ,   ](?=\d)", "", t)
    return t


def _correct(task_id: str, text: str) -> bool:
    low = _norm(text)
    first = any(tok.lower() in low for tok in TRUTH.get(task_id, []))
    if task_id in TRUTH_SECOND:
        second = any(tok.lower() in low for tok in TRUTH_SECOND[task_id])
        return first and second
    return first


async def _ask(client: httpx.AsyncClient, provider: str, model: str, extra: dict, task: str) -> dict:
    body = {"messages": [{"role": "user", "content": task}],
            "provider": provider, "model": model, "max_tokens": 1024,
            "temperature": 0, **extra}
    try:
        r = await client.post(f"{GATEWAY}/v1/chat", json=body, timeout=90)
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}", "text": "", "cost": 0.0}
        d = r.json()
        return {"text": d.get("text") or "", "cost": (d.get("cost") or {}).get("total_usd") or 0.0,
                "model": d.get("model"), "output_tokens": d.get("output_tokens")}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "text": "", "cost": 0.0}


async def main() -> int:
    tasks = []
    for line in TASKS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tasks.append(json.loads(line))

    OUT.mkdir(exist_ok=True)
    rows = []
    async with httpx.AsyncClient() as client:
        for t in tasks:
            entry = {"id": t["id"], "difficulty": t["difficulty"], "rungs": {}}
            for rung, provider, model, extra in RUNGS:
                res = await _ask(client, provider, model, extra, t["task"])
                res["correct"] = _correct(t["id"], res.get("text", "")) if not res.get("error") else False
                entry["rungs"][rung] = {
                    "correct": res["correct"],
                    "cost": round(res.get("cost") or 0.0, 8),
                    "error": res.get("error"),
                    "answer_tail": (res.get("text") or "")[-120:].replace("\n", " "),
                }
            rows.append(entry)
            line_out = "  ".join(
                f"{r}={'OK ' if entry['rungs'][r]['correct'] else 'BAD'}"
                for r in ("economy", "standard", "frontier")
            )
            print(f"{t['id']:22} {t['difficulty']:9} {line_out}")

    # Where is price NOT monotone in competence? A cheaper rung right where a
    # dearer rung is wrong.
    inversions = []
    order = ["economy", "standard", "frontier"]
    for row in rows:
        for i, lo in enumerate(order):
            for hi in order[i + 1:]:
                if row["rungs"][lo]["correct"] and not row["rungs"][hi]["correct"] \
                        and not row["rungs"][hi]["error"]:
                    inversions.append({"id": row["id"], "cheaper_right": lo,
                                       "dearer_wrong": hi})
    result = {"gateway": GATEWAY, "rows": rows, "capability_inversions": inversions,
              "per_rung_score": {
                  r: sum(1 for row in rows if row["rungs"][r]["correct"]) for r in order}}
    (OUT / "rung_probe.json").write_text(json.dumps(result, indent=2))
    print("\nper-rung correct:", result["per_rung_score"], "of", len(rows))
    print("capability inversions (cheap right, dear wrong):", inversions)
    print(f"wrote {OUT / 'rung_probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
