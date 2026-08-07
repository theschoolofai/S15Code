# Session 15 Assignment — AI Engineering Routing Policy

**Author:** Avinash Anad  
**Domain:** AI/ML Engineering cost calculations and agent economics  
**Task set:** 16 tasks (4 trivial, 6 moderate, 6 hard)  
**Mode:** Offline (deterministic simulation)

## The Workload and the Ladder

### Task Domain

AI engineering tasks spanning tokenization, cost calculations, budget projections, caching economics, trace hierarchies, metric cardinality, and the break-even formula itself. The domain was chosen because the session teaches these concepts — a routing policy for this workload must handle both simple factual recall and multi-step numerical reasoning.

### The Ladder

| Rung | Model | Provider | $/Mtok in/out | max_tokens |
|------|-------|----------|---------------|------------|
| Economy | openai/gpt-oss-120b | Groq | 0.15 / 0.75 | 512 |
| Standard | gemini-3.1-flash-lite | Gemini | 0.25 / 1.50 | 1024 |
| Frontier | openai/gpt-4.1 | GitHub | 2.00 / 8.00 | 4096 |

Each rung is a different model on a different provider. Budget downgrades change the model, not the token limit.

### Budget Policy

- Default budget: $0.05 per run
- Downgrade at 50% spend
- Refuse at 90% spend
- Reserve 20% for the terminal node
- Max 60 calls per run, 6 per node
- Conservative input estimation: chars/4 × 1.25 safety factor

## Measured Cost per Call and Cost per Resolved Task

| Strategy | Spend (USD) | Calls | Cost/Call | Resolved | Cost/Resolved Task |
|----------|------------|-------|-----------|----------|-------------------|
| A: Always frontier | $0.23547 | 16 | $0.01472 | 16/16 | $0.01472 |
| B: Always cheapest (3 retries) | $0.01862 | 46 | $0.00040 | 1/16 | $0.01862 |
| C: Budget-aware cascade | $0.24364 | 43 | $0.00567 | 16/16 | $0.01523 |

### The Signature Failure Mode: OBSERVED

Strategy B was **97.2% cheaper per call** than A, but **26.5% more expensive per resolved task**. The economy rung resolved only 1 of 16 tasks (ae05_batch_cost). It failed every trivial, every hard, and 5 of 6 moderate tasks — the 512-token ceiling was insufficient for the simulated task complexity.

Strategy C (budget-aware cascade) resolved all 16 tasks by escalating from economy → standard → frontier as needed, at a cost per resolved task of $0.01523 — 3.5% more than always-frontier but 18.2% less than always-cheapest.

## Break-Even Resolution Rate

Using the formula: `r* = k / (C/c - 1 + k)`

- Price ratio C/c = $0.01472 / $0.00040 = 36.4×
- Retry attempts k = 3
- **r* = 3 / (36.4 - 1 + 3) = 3 / 38.4 = 7.8%**

The economy rung resolved 6.25% of tasks — **below** the 7.8% break-even threshold. This confirms the signature failure mode: the cheap rung needed to resolve at least 7.8% to beat the frontier on cost per resolved task, and it fell short.

The wide ladder (36.4× spread) means the trap threshold is very low — even a rung that fails 92% of tasks could still be cheaper per resolved task. But when it fails 94%, as it did here, retries burn through the savings.

## Where the Policy Chose Wrongly

**ae05_batch_cost** (moderate difficulty) reveals the cascade's weakness. This task asks for a straightforward multiplication ($0.50/Mtok × 1M input tokens + $1.50/Mtok × 400k output tokens = $1.10).

- Strategy B solved it on the economy rung in 1 attempt for $0.00037
- Strategy C also solved it on economy in 1 attempt for $0.00037
- Strategy A used the frontier rung for $0.00398 — **10.8× more expensive** for the same correct answer

The always-frontier strategy overpaid on this task. A smarter policy would classify simple arithmetic tasks as economy-eligible, avoiding the frontier charge. This is the inverse of the signature failure: the expensive model wastes money on easy work.

## Test Suite Results

```
pytest: 277 passed in 57.22s
```

## Proof Results (All Passed)

| Proof | Result | Key Finding |
|-------|--------|-------------|
| p1_cost_per_task | ALL CHECKS PASSED | Signature failure mode observed: B 97.2% cheaper/call, 26.5% dearer/resolved |
| p2_budget_holds | ALL CHECKS PASSED | Ceiling held; 1 downgrade (frontier→standard); 1 refusal at impossible budget |
| p3_denial_of_wallet | ALL CHECKS PASSED | 200 rounds, 2 admitted, 198 refused, spent $0.00193 of $0.002 ceiling |
| p4_trace_export | ALL CHECKS PASSED | Span costs sum exactly to ledger; content capture off; hierarchy complete |
| p7_cross_model_ladder | ALL CHECKS PASSED | Each rung is a different model; downgrades change model; 82.7× measured spread |

## Adversarial Test: Denial of Wallet (p3)

An unbounded loop sent 200 requests against a $0.002 ceiling:

- **2 calls admitted**, 198 refused
- Spent $0.00193 (96.5% of ceiling)
- Every refusal became a visible `BudgetRefused` graph failure
- The controller stopped the spend, not the loop
- Extrapolated uncontrolled bill: ~$9.65 over 10,000 rounds

The budget ceiling held. The controller refused work before exceeding the limit.

## Trace Hierarchy (p4)

One run exported as:
```
run → agent_loop → plan → node → provider_call
```

- 10 spans total: 1 run, 3 agent_loop, 3 plan, 2 node, 1 provider_call
- gen_ai.usage.* attributes on every provider call
- Span cost total: $0.00161 = ledger spent: $0.00161 (delta: 0.000)
- Content capture: OFF by default
- Trace ID: `559c54501eee59601c00df49baa8d9a1`

## Honest Limitation

The offline mode uses a deterministic transport — the simulated "difficulty" is derived from a hash of the task text, not from actual model capability. This means the resolution pattern (economy fails, frontier succeeds) is a property of the simulation, not a measurement of real model behavior. The numbers exercise every code path and prove the invariants hold, but they are not evidence of how these specific models would perform on these specific tasks. Real evidence requires live calls against the gateway with real providers.

## Commands to Reproduce

```bash
git clone https://github.com/AvinashAnad/S15Code.git
cd S15Code
git checkout assignment/avi-routing-policy
uv sync

# Tests
uv run pytest -q

# Proofs
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/ai_engineering.jsonl --principal avi/s15/p1 --offline --label avi_ai_eng
uv run python proofs/p2_budget_holds.py --task "Calculate the total API cost for 1000 calls at 0.50 per million input tokens with 3000 input tokens each" --budget 0.02 --principal avi/s15/p2 --offline --label avi
uv run python proofs/p3_denial_of_wallet.py --task "Explain model routing in one sentence" --budget 0.002 --principal avi/s15/p3 --offline --label avi
uv run python proofs/p4_trace_export.py --task "List three benefits of prompt caching" --budget 0.02 --principal avi/s15/p4 --offline --label avi
uv run python proofs/p7_cross_model_ladder.py --task "What is the average speed if you travel 60km in 2 hours?" --principal avi/s15/p7 --offline --label avi
```
