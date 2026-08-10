# Part 1 — Reproduce the floor

Live suite + proof evidence for Assignment 15 Part 1 (customer-support triage config).

## Suites

| Suite | Result (local machine) | Notes |
|---|---|---|
| `glc_v4` pytest | 435 passed, 1 failed, 20 skipped | Pre-existing `test_recent_traces_is_honest_when_tracing_is_off` |
| `S15Code` pytest | 276 passed, 1 failed | Pre-existing Windows `file://` ICS path in birthday regression |

## Five proofs (support_triage policy where noted)

| Proof | Mode | Result | Artifact |
|---|---|---|---|
| p1 cost per task | live (see Part 2) | measured A/B/C | [part2/](../part2/) |
| p2 budget holds | offline then live path | PASS | `proofs/out/p2_budget_holds_support_p2.json` (gitignored; summary below) |
| p3 denial of wallet | **live** | PASS | [p3_summary.json](./p3_summary.json) |
| p4 trace export | **live** + Jaeger | partial (see limitation) | [p4_summary.json](./p4_summary.json) |
| p7 cross-model ladder | offline / live | PASS | `proofs/out/p7_cross_model_ladder_support_p7.json` |

## Four captured runs

### Run 1 — budgeted triage answer (p4)

| Field | Value |
|---|---|
| Prompt | `Triage forgot password` |
| Tier / model | `standard` / `gemini-3.1-flash-lite` (via `gemini_1`) |
| Ordered events | run → agent_loop → plan → node → provider_call (journal 8 events, 10 local spans) |
| Jaeger trace ID | `4a4c13bf124380bc7edf61939b2a6444` |
| Ledger | 212 in / 240 out, **$0.00041300** (span costs == ledger, delta 0) |
| Final answer | Support triage draft (password-reset style INTENT/PRIORITY/REPLY/NEXT) |
| UI | http://localhost:16686/trace/4a4c13bf124380bc7edf61939b2a6444 |

### Run 2 — adversarial denial-of-wallet (p3)

| Field | Value |
|---|---|
| Prompt | ShopNova P1 ticket storm (frontier loop) |
| Tier / model | most-capable rung requested each iteration; controller admits/refuses |
| Ordered events | 200 loop rounds → 200 nodes; admitted then `BudgetRefused` failures |
| Trace / telemetry | refusals visible as graph `BudgetRefused` (see Part 3) |
| Ledger | spent **$0.00158605** / ceiling **$0.002**; 39 calls; 161 refusals |
| Final answer | loop stopped by controller, not by the agent |

### Run 3 — ladder downgrade (p2 / p7 family)

| Field | Value |
|---|---|
| Prompt | ShopNova triage late delivery / refund-style tasks |
| Tier / model | asked frontier → served **standard** or **economy** under pressure |
| Events | admission → downgrade decision → provider_call on cheaper rung |
| Ledger | downgrade recorded; refuse path spends **$0** when unaffordable |
| Finding | downgrade changes **model**, not only `max_tokens` |

### Run 4 — always-frontier vs cascade sample (p1)

See Part 2 tables (live `p1` on 18 support tickets). Per-task rows live in the Part 2 log.

## Honest limitation the traces exposed

**Jaeger round-trip was incomplete:** p4 emitted **10** spans locally but Jaeger query returned **7**. Missing kinds included the root `run` span (and one `agent_loop` / `plan`), so some backend parents read as `None` even though costs, `gen_ai.*` usage, and “no prompt content” checks passed.

Also: **OpenAI `gpt-4o-mini` was wired** as a native `openai` provider reading `OPENAI_LLM_KEY`, but a live probe returned `credit_balance_exhausted` — so the measured frontier rung used **Cerebras `zai-glm-4.7`** until OpenAI credits are restored.
