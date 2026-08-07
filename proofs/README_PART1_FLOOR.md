# Part 1 — Reproducing the Floor (evidence)

This document records the reproduction of the existing system: both test suites,
all five proofs, four detailed runs with their traces, and one honest limitation
the telemetry exposed.

## Test suites

| Suite | Result |
|---|---|
| `glc_v4` (`uv run pytest -q`) | 444 passed, 12 skipped |
| `S15Code` (`uv run pytest -q`) | 277 passed |

## The five proofs

All proofs take task/budget/principal as CLI arguments and run against work they
have never seen. All were run in offline mode (deterministic transport; the
policy, ladder, budget, journal and span export are the real implementation).

| Proof | Result |
|---|---|
| `p1_cost_per_task` | 12 tasks x 3 strategies. Signature failure mode NOT observed: always-frontier $0.01268/resolved, always-cheapest $0.00384/resolved (3/12 resolved), budget-aware $0.01255/resolved (12/12). Judge meta-cost 66 calls, $0.0101. |
| `p2_budget_holds` | 0 breaches; tight allowance downgraded frontier→standard; unaffordable ceiling refused (0 calls, 1 refusal, $0.0). |
| `p3_denial_of_wallet` | $0.002 ceiling: 2 admitted, 198 refused, spent $0.00193. Ceiling held. |
| `p4_trace_export` | trace_id `4de9837a8667f8f9343d4f5e5c43c269`; span cost $0.00160350 == ledger $0.00160350 (perfect reconciliation). |
| `p7_cross_model_ladder` | projected spread 84.9x, measured 83.03x; every rung a different model. |

## Four detailed runs

### Run 1 — Strategy A (always-frontier), task t01_capital (trivial)
- **Prompt:** "Write the capital of France." (task t01_capital)
- **Tier requested:** frontier · **Model served:** openai/gpt-4.1
- **Event trace:** run_started → plan → node t01_capital#A1 → provider_call → task_succeeded
- **Jaeger trace_id:** `4de9837a8667f8f9343d4f5e5c43c269`
- **Ledger row:** cost $0.02526200, 1 call, tier frontier
- **Final answer:** resolved (overall 1.0)

### Run 2 — Strategy B (always-cheapest), task t01_capital (trivial)
- **Prompt:** "Write the capital of France."
- **Tier requested:** economy · **Model served:** openai/gpt-oss-120b
- **Event trace:** run_started → plan → node t01_capital#B1 → provider_call → task_failed → retry (x3, all economy)
- **Jaeger trace_id:** `4de9837a8667f8f9343d4f5e5c43c269`
- **Ledger row:** cost $0.00120015, 3 calls, tier economy
- **Final answer:** unresolved (overall 0.25) — simulated output insufficient

### Run 3 — Strategy C (budget-aware), task t01_capital (trivial)
- **Prompt:** "Write the capital of France."
- **Tier requested:** economy (start) · **Model served:** economy → standard → frontier
- **Event trace:** run_started → plan → node t01_capital#C1 (economy) → task_failed → escalate → standard → task_failed → escalate → frontier → task_succeeded
- **Jaeger trace_id:** `4de9837a8667f8f9343d4f5e5c43c269`
- **Ledger row:** cost $0.02722480, 3 calls, tiers [economy, standard, frontier]
- **Final answer:** resolved (overall 1.0)

### Run 4 — Budget refusal (p2 impossible ceiling)
- **Prompt:** "<any task>"
- **Tier requested:** frontier · **Allowance:** $0.00000056 (impossible)
- **Event trace:** run_started → plan → node → **refused** (BudgetRefused, no provider call)
- **Jaeger trace_id:** (p2 run, refusal recorded as graph failure)
- **Ledger row:** 0 calls, 1 refusal, $0.0 spent
- **Final answer:** refused (not downgraded — too cheap even for economy)

## Honest limitation the traces exposed

**The semantic cache served wrong answers with perfect confidence.**

In `p6_cache_savings`, two cache HITS (diff03, diff06) were served where:
- Cosine similarity = 1.000000 (perfect match)
- Both were "different/near_miss" pairs (semantically different requests)
- Both served WRONG ANSWERS with $0.00 cost
- The system had NO indication these were incorrect — no error, no warning

The configured threshold (0.95) was meant to prevent this, but the deterministic
offline embedder produced identical vectors for different text, making them
indistinguishable. In production this manifests as: a cache hit returns a stale
answer from a different question, no error is raised, no span records the
mistake, and the operator sees $0 cost and assumes success. This is exactly the
"silent failure" mode the session warns about — the cache saved money but served
wrong data, and only by manually checking the labelled pair set did it become
visible.

## Reproduce

```bash
cd S15Code
uv run pytest -q                       # S15Code suite
cd ../glc_v4 && uv run pytest -q       # glc_v4 suite

# proofs (offline)
uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl
uv run python proofs/p2_budget_holds.py     --task "<any task>" --budget 0.02
uv run python proofs/p3_denial_of_wallet.py --task "<any task>" --budget 0.002
uv run python proofs/p4_trace_export.py     --task "<any task>" --budget 0.02
uv run python proofs/p7_cross_model_ladder.py --task "<any task>"