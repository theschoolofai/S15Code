# Part 2 — Build a policy and measure it

Live `p1` on 18 ShopNova triage tickets.  
**Log:** [`p1_live_rerun.log`](./p1_live_rerun.log)  
**Machine JSON (gitignored):** `proofs/out/p1_cost_per_task_support.json`  
**Summary:** [`p1_summary.json`](./p1_summary.json)

Gateway: `http://127.0.0.1:8111` · Config: `config/support_triage`  
Judges: OpenRouter **`openai/gpt-4o-mini`** (paid; free `:free` models were exhausted at 0/day).

## Judge rubric (defendable)

From `config/support_triage/evals.yaml`:

| Criterion | Weight | Role |
|---|---|---|
| addresses_task | 1 | Answers the ticket asked |
| specific | 1 | Committed, not evasive |
| consistent | 1 | Internally coherent |
| complete | 1 | Actionable as delivered |
| meets_expectation | 2 | Matches the task’s success criterion |

Resolved iff weighted score ≥ **0.75** and no criterion < **0.5**. Panel is disjoint from the answering ladder (groq / gemini / cerebras).

## Measured costs (live)

| Strategy | Spend | Calls | Cost/call | Resolved | Cost/resolved |
|---|---|---|---|---|---|
| **A** always_frontier (`zai-glm-4.7`) | $0.00161600 | 14 | $0.00011543 | **13/18** | $0.00012431 |
| **B** always_cheapest (`gpt-oss-120b`) | $0.00319110 | 18 | $0.00017728 | **18/18** | $0.00017728 |
| **C** budget_aware (cascade) | $0.00343535 | 19 | $0.00018081 | **18/18** | $0.00019085 |

Signature failure mode (cheaper/call but dearer/resolved for B): **NOT OBSERVED**.

Notes: A had **4 transport failures** late in the run (`st15`–`st18` billed $0) — frontier provider blips, not judge rejects. Judge meta-cost: 90 calls, **$0.10994** (evaluation cost ≫ answering cost).

## Break-even resolution rate

From p1 finding, with cheap-rung retries `k = 3`:

| Comparison | Break-even `r*` | B’s resolution rate | Position |
|---|---|---|---|
| B vs A | **100.0%** | 100% | at the line (spread inverted: frontier $/call < economy) |
| B vs C | **97.5%** | 100% | **+2.5 pp above** `r*` — cascade cannot beat always-cheapest here |

Economy resolved everything first try, so retries never ran; cost/call and cost/resolved move together for B.

## Wrong routing case

### 1) Cascade overpay — `st14_partial_refund_math`

| | B always_cheapest | C budget_aware |
|---|---|---|
| Path | economy ×1 | economy → **standard** (2 attempts) |
| Spend | **$0.00019515** | **$0.00041540** |
| Verdict | resolved | resolved |

Policy escalated after an unresolved economy attempt; standard then passed. Net: **~$0.00022 extra (~2.1×)** for the same resolved ticket. A confidence/escalation miss — pay for two rungs when one more economy retry (strategy B’s policy) would have been enough on this set overall, or a better first-pass prompt.

### 2) Capability not price-monotonic — `st11_angry_escalation`

| | A frontier | B/C economy |
|---|---|---|
| Spend | $0.00014450 | ~$0.00026 |
| Verdict | **unresolved** | **resolved** |

Frontier charged a real call and still failed the judge; economy resolved. Routing “up” would have been the wrong instinct for this ticket class.

## Commands

```bash
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/support_triage.jsonl \
  --config-dir config/support_triage --strategies A,B,C \
  --principal support/p1 --label support --budget 0.05 \
  --base-url http://127.0.0.1:8111
```
