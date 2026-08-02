# Part 1 evidence — four budgeted runs

- ladder: `economy < standard < frontier`
- tier models: `{"economy": "openai/gpt-oss-120b", "standard": "gemini-3.1-flash-lite", "frontier": "openai/gpt-4.1"}`
- mode: `live`

## Run plain_answer — `run-68bef7a9dae1`

**Prompt.** In exactly two sentences, explain why a spend ceiling must be enforced in code rather than asked for in a prompt.

**Tier and model chosen.** requested `['frontier']` → served `['standard']` on `['gemini-3.1-flash-lite']` (provider `['gemini_1']`, decision `['downgrade']`)

**Jaeger trace id.** `31e0fe23e60a4e5adfddc79e2e11f681` — 10 spans read back from the backend at http://127.0.0.1:16700/api/traces/31e0fe23e60a4e5adfddc79e2e11f681

**Ordered event trace.**

| seq | event | node | tier | model | cost | decisions |
|---|---|---|---|---|---|---|
| 1 | run_started |  |  |  |  |  |
| 2 | graph_patched |  |  |  |  |  |
| 3 | task_started | recall |  |  |  |  |
| 4 | task_succeeded | recall |  |  |  |  |
| 5 | graph_patched |  |  |  |  |  |
| 6 | task_started | answer |  |  |  |  |
| 7 | task_succeeded | answer | standard | gemini-3.1-flash-lite | 0.00014475 | downgrade |
| 8 | graph_patched |  |  |  |  |  |

**Ledger rows.**

| node | role | requested | served | decision | provider/model | in | out | projected | charged |
|---|---|---|---|---|---|---|---|---|---|
| answer | answer_with_evidence | frontier | standard | downgrade | gemini_1/gemini-3.1-flash-lite | 231 | 58 | 0.00162875 | 0.00014475 |

Ledger total **0.00014475** of a 0.02000000 ceiling; 0 refusals, 1 downgrades, 1.3s wall clock.

**Final answer.**

```
Enforcing a spend ceiling in code ensures that programmatic guardrails are applied consistently and automatically, preventing human error or prompt injection from bypassing financial limits. Relying solely on a prompt is insufficient because it lacks the technical authority to terminate processes or block API calls once a predefined budget threshold is reached.
```

## Run memory_write — `run-c31225a895e0`

**Prompt.** Remember that our margin-shortfall penalty review happens on the first Monday of every month, then tell me what you have recorded.

**Tier and model chosen.** requested `['frontier']` → served `['standard']` on `['gemini-3.1-flash-lite']` (provider `['gemini_2']`, decision `['downgrade']`)

**Jaeger trace id.** `380c4d7ac3e2530b1f57de1143fffd68` — 13 spans read back from the backend at http://127.0.0.1:16700/api/traces/380c4d7ac3e2530b1f57de1143fffd68

**Ordered event trace.**

| seq | event | node | tier | model | cost | decisions |
|---|---|---|---|---|---|---|
| 1 | run_started |  |  |  |  |  |
| 2 | graph_patched |  |  |  |  |  |
| 3 | task_started | recall |  |  |  |  |
| 4 | task_started | remember |  |  |  |  |
| 5 | task_succeeded | recall |  |  |  |  |
| 6 | graph_patched |  |  |  |  |  |
| 7 | task_succeeded | remember |  |  |  |  |
| 8 | graph_patched |  |  |  |  |  |
| 9 | task_started | answer |  |  |  |  |
| 10 | task_succeeded | answer | standard | gemini-3.1-flash-lite | 0.00010575 | downgrade |
| 11 | graph_patched |  |  |  |  |  |

**Ledger rows.**

| node | role | requested | served | decision | provider/model | in | out | projected | charged |
|---|---|---|---|---|---|---|---|---|---|
| answer | answer_with_evidence | frontier | standard | downgrade | gemini_2/gemini-3.1-flash-lite | 261 | 27 | 0.00163675 | 0.00010575 |

Ledger total **0.00010575** of a 0.02000000 ceiling; 0 refusals, 1 downgrades, 1.1s wall clock.

**Final answer.**

```
You told me that your margin-shortfall penalty review happens on the first Monday of every month (source: proof://harness).
```

## Run structured_format — `run-6850171a0605`

**Prompt.** Return a single JSON object and nothing else, with exactly two keys: "tier" set to the string "economy" and "max_tokens" set to the number 512.

**Tier and model chosen.** requested `['frontier']` → served `['standard']` on `['gemini-3.1-flash-lite']` (provider `['gemini_1']`, decision `['downgrade']`)

**Jaeger trace id.** `fccd74b5339ed13a62a546e9b11aded6` — 10 spans read back from the backend at http://127.0.0.1:16700/api/traces/fccd74b5339ed13a62a546e9b11aded6

**Ordered event trace.**

| seq | event | node | tier | model | cost | decisions |
|---|---|---|---|---|---|---|
| 1 | run_started |  |  |  |  |  |
| 2 | graph_patched |  |  |  |  |  |
| 3 | task_started | recall |  |  |  |  |
| 4 | task_succeeded | recall |  |  |  |  |
| 5 | graph_patched |  |  |  |  |  |
| 6 | task_started | answer |  |  |  |  |
| 7 | task_succeeded | answer | standard | gemini-3.1-flash-lite | 0.00009475 | downgrade |
| 8 | graph_patched |  |  |  |  |  |

**Ledger rows.**

| node | role | requested | served | decision | provider/model | in | out | projected | charged |
|---|---|---|---|---|---|---|---|---|---|
| answer | answer_with_evidence | frontier | standard | downgrade | gemini_1/gemini-3.1-flash-lite | 247 | 22 | 0.00163125 | 0.00009475 |

Ledger total **0.00009475** of a 0.02000000 ceiling; 0 refusals, 1 downgrades, 1.0s wall clock.

**Final answer.**

```
{
  "tier": "economy",
  "max_tokens": 512
}
```

## Run multi_step — `run-6a87b31ebd13`

**Prompt.** A client shorts 300 shares at 1,450 rupees, then covers 200 at 1,410 and 100 at 1,475. Ignoring charges, what is the net realised profit or loss in rupees?

**Tier and model chosen.** requested `['frontier']` → served `['standard']` on `['gemini-3.1-flash-lite']` (provider `['gemini_2']`, decision `['downgrade']`)

**Jaeger trace id.** `e2028790a5c48f9e6d16f053ac183c4c` — 10 spans read back from the backend at http://127.0.0.1:16700/api/traces/e2028790a5c48f9e6d16f053ac183c4c

**Ordered event trace.**

| seq | event | node | tier | model | cost | decisions |
|---|---|---|---|---|---|---|
| 1 | run_started |  |  |  |  |  |
| 2 | graph_patched |  |  |  |  |  |
| 3 | task_started | recall |  |  |  |  |
| 4 | task_succeeded | recall |  |  |  |  |
| 5 | graph_patched |  |  |  |  |  |
| 6 | task_started | answer |  |  |  |  |
| 7 | task_succeeded | answer | standard | gemini-3.1-flash-lite | 0.00046075 | downgrade |
| 8 | graph_patched |  |  |  |  |  |

**Ledger rows.**

| node | role | requested | served | decision | provider/model | in | out | projected | charged |
|---|---|---|---|---|---|---|---|---|---|
| answer | answer_with_evidence | frontier | standard | downgrade | gemini_2/gemini-3.1-flash-lite | 265 | 263 | 0.00163200 | 0.00046075 |

Ledger total **0.00046075** of a 0.02000000 ceiling; 0 refusals, 1 downgrades, 1.6s wall clock.

**Final answer.**

```
To calculate the net realised profit or loss, we determine the difference between the total proceeds from the short sale and the total cost of covering the shares.

**1. Short Sale Proceeds:**
*   300 shares sold at 1,450 rupees = 300 × 1,450 = **435,000 rupees**

**2. Cost of Covering:**
*   200 shares covered at 1,410 rupees = 200 × 1,410 = 282,000 rupees
*   100 shares covered at 1,475 rupees = 100 × 1,475 = 147,500 rupees
*   Total cost = 282,000 + 147,500 = **429,500 rupees**

**3. Net Realised Profit/Loss:**
*   Profit/Loss = Total Proceeds - Total Cost
*   435,000 - 429,500 = **5,500 rupees**

The client has a net realised profit of **5,500 rupees**.
```
