# Part 1 Validation — Reproduce the Floor

Ran 2026-08-05 against a live stack, not the offline simulator: `glc_v4`
gateway up on `http://127.0.0.1:8111`, Jaeger up on `16686`/`4317`/`4318`
(`docker compose -f glc_v4/docker-compose.observability.yml`), OTLP export
pointed at `http://127.0.0.1:4318/v1/traces`. Every artifact below is a real
run against real providers, not a `--offline` pass — the JSON backing every
number lives in `S15Code/proofs/out/*_part1*.json`.

## 1. Both Suites

```
cd glc_v4    && uv run pytest -q     # 448 passed, 8 skipped
cd S15Code   && uv run pytest -q     # 277 passed
```

Zero failures in either suite.

## 2. The Five Proofs, Live

| Proof | Command | Result |
|---|---|---|
| `p1_cost_per_task.py` | `--tasks proofs/tasks/mixed.jsonl --principal part1/floor --base-url http://127.0.0.1:8111` | **PASS** (9/9 checks) — see 2.1 for why `--base-url` had to be given explicitly |
| `p2_budget_holds.py` | `--task "Convert 100 kilometers to miles." --budget 0.02 --principal part1/floor --otel-endpoint http://127.0.0.1:4318/v1/traces` | **PASS** (5/5 checks) |
| `p3_denial_of_wallet.py` | `--task "Explain what a circuit breaker pattern is, in one paragraph." --budget 0.002 --principal part1/floor --otel-endpoint http://127.0.0.1:4318/v1/traces` | **PASS** (6/6 checks) |
| `p4_trace_export.py` | `--task "List the capital cities of France, Japan, and Brazil." --budget 0.02 --principal part1/floor --otel-endpoint http://127.0.0.1:4318/v1/traces` | **PASS** (14/14 checks, including the real Jaeger round trip) |
| `p7_cross_model_ladder.py` | `--task "What is the boiling point of water in Celsius and Fahrenheit?" --principal part1/floor --otel-endpoint http://127.0.0.1:4318/v1/traces` | **FAILED** (7/9 checks) — the `frontier` rung's provider is down; see Section 4 |

### 2.1 `p1_cost_per_task.py`'s default port doesn't match the other four proofs

Running the exact command from this repo's own `README.md` / the task
description —

```
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/mixed.jsonl --principal "part1/floor"
```

— silently produced **simulated** numbers even with the gateway fully up,
because `p1_cost_per_task.py:73` hardcodes its own
`DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8112")`,
while `harness.py:48` (shared by `p2`/`p3`/`p4`/`p7`) defaults to `8111` —
the port the gateway and every other proof actually use. Nothing on `8112`
is listening, so `gateway_reachable()` returns `False` and the proof falls
back to `OfflineTransport`, printing an `OFFLINE:` banner that's easy to
miss in a long run. Passing `--base-url http://127.0.0.1:8111` explicitly
fixes it (Section 2's table command). This is a real, reproducible
inconsistency in the proof suite's own defaults, not a one-off fluke.

**What changed once run live** (`part1_live` vs. the offline label from an
earlier session): strategy **A (always-frontier) went from 12/12 resolved,
$0.1521 spend to 0/12 resolved, $0.00 spend, 0 calls, 12/12 transport
failures** — every single frontier call failed transport, for the reason in
Section 4. `cost_per_resolved_task` for A is reported as `n/a` rather than a
number, and the finding's own `note` field is empty because the comparison
that matters (`B_vs_A`) is undefined when the denominator is zero resolved
tasks — the proof still passed, because "every strategy attempted every
task" and "an empty or errored answer never counted as resolved" don't
require A to succeed, only to be attempted and honestly reported.

## 3. Four Reproduced Runs

Four self-contained prompts (no dangling "attached document" this time —
see prior conversation turn), each run through the real `AgentRuntime`
against the live gateway with OTel export on. Captured per run: the prompt,
the tier requested vs. served per node, the ordered event trace, the Jaeger
trace ID, the ledger rows (`RunBudget.charges`), and the final answer text.

### Run 1 — `Convert 100 kilometers to miles.`

- **Nodes / tier requested → served:**
  - `recall` (`memory_recall`) — tier `economy`, no model (local memory lookup, no provider call)
  - `answer` (`answer_with_evidence`) — **requested `frontier`, served `standard`** (`gemini_1` / `gemini-3.1-flash-lite`), decision `downgrade`
- **Ordered event trace:** `run_started → graph_patched → task_started(recall) → task_succeeded(recall) → graph_patched → task_started(answer) → task_succeeded(answer) → graph_patched` (8 events)
- **Jaeger trace ID:** `82a58c0635b39776a11d7680ab041b74`
- **Ledger row:** `{node: answer, tier: standard, provider: gemini_1, model: gemini-3.1-flash-lite, input_tokens: 217, output_tokens: 16, cost: $0.00007825, decision: downgrade, requested_tier: frontier}`
- **Final answer:** "100 kilometers is approximately 62.1371 miles."

### Run 2 — `Explain what a circuit breaker pattern is, in one paragraph.`

- **Nodes / tier requested → served:**
  - `recall` (`memory_recall`) — tier `economy`, no model
  - `answer` (`answer_with_evidence`) — **requested `frontier`, served `standard`** (`gemini_1` / `gemini-3.1-flash-lite`), decision `downgrade`
- **Ordered event trace:** same 8-event shape as Run 1 (`run_started … task_started/succeeded(recall) … task_started/succeeded(answer) … graph_patched`)
- **Jaeger trace ID:** `9a0e3e5c99115abc73ff43a944f66d9c`
- **Ledger row:** `{node: answer, tier: standard, provider: gemini_1, model: gemini-3.1-flash-lite, input_tokens: 220, output_tokens: 130, cost: $0.00025000, decision: downgrade, requested_tier: frontier}`
- **Final answer:** "A circuit breaker pattern is a software design pattern used to improve system stability and resilience by preventing an application from repeatedly attempting to execute an operation that is likely to fail. It functions by wrapping a protected function call in a state machine that monitors for failures; when the number of failures exceeds a predefined threshold, the 'circuit' trips, and all subsequent calls to the service return an error or a fallback response immediately without attempting the operation. After a timeout period, the circuit enters a half-open state to test if the underlying service has recovered, allowing a limited number of requests to pass through to determine if the system is healthy enough to resume normal operations."

### Run 3 — `List the capital cities of France, Japan, and Brazil.`

- **Nodes / tier requested → served** (a five-node plan: three parallel searches, a distill, then answer):
  - `search_1`, `search_2`, `search_3` (`researcher`) — tier `economy`, served on `groq` / `openai/gpt-oss-120b`, decision `proceed` (no downgrade — economy was the request)
  - `distill` (`distiller`) — tier `standard`, served on `gemini_1` / `gemini-3.1-flash-lite`, decision `proceed`
  - `answer` (`answer_with_evidence`) — **requested `frontier`, served `standard`** (`gemini_1` / `gemini-3.1-flash-lite`), decision `downgrade`
- **Ordered event trace** (17 events): `run_started → graph_patched → task_started(search_1) → task_started(search_2) → task_started(search_3) → task_succeeded(search_1) → graph_patched → task_succeeded(search_3) → graph_patched → task_succeeded(search_2) → graph_patched → task_started(distill) → task_succeeded(distill) → graph_patched → task_started(answer) → task_succeeded(answer) → graph_patched` — note the three searches start in the recorded order `search_1, search_2, search_3` but *finish* `search_1, search_3, search_2`, i.e. the event tape preserves real completion order, not launch order.
- **Jaeger trace ID:** `2b61e2111189d77ef4343a9661d6f5f1` (23 spans; span hierarchy `run → agent_loop → plan → node → provider_call` verified, span cost sum matched the ledger to within `2.168e-19`)
- **Ledger rows** (5 charges, $0.0017575 total):
  1. `search_1`: economy, groq/`openai/gpt-oss-120b`, 402in/102out, $0.0001368, proceed
  2. `search_3`: economy, groq/`openai/gpt-oss-120b`, 411in/269out, $0.0002634, proceed
  3. `search_2`: economy, groq/`openai/gpt-oss-120b`, 342in/131out, $0.00014955, proceed
  4. `distill`: standard, gemini_1/`gemini-3.1-flash-lite`, 2858in/35out, $0.000767, proceed
  5. `answer`: standard, gemini_1/`gemini-3.1-flash-lite`, 1187in/96out, $0.00044075, downgrade (requested frontier)
- **Final answer:** "The capital cities of France, Japan, and Brazil are:\n\n* **France:** Paris [source: https://en.wikipedia.org/wiki/List_of_capitals_of_France]\n* **Japan:** Tokyo [source: https://en.wikipedia.org/wiki/Capital_of_Japan]\n* **Brazil:** Brasília [source: https://en.wikipedia.org/wiki/Capitals_of_Brazil]"

### Run 4 — `What is the boiling point of water in Celsius and Fahrenheit?`

- **Nodes / tier requested → served:**
  - `recall` (`memory_recall`) — tier `economy`, no model
  - `answer` (`answer_with_evidence`) — **requested `frontier`, served `standard`** (`gemini_1` / `gemini-3.1-flash-lite`), decision `downgrade`
- **Ordered event trace:** same 8-event shape as Runs 1–2
- **Jaeger trace ID:** `5ea9b7448cc9a6c440a0ea674824da21`
- **Ledger row:** `{node: answer, tier: standard, provider: gemini_1, model: gemini-3.1-flash-lite, input_tokens: 220, output_tokens: 28, cost: $0.00009700, decision: downgrade, requested_tier: frontier}`
- **Final answer:** "At standard atmospheric pressure (sea level), the boiling point of water is 100 degrees Celsius or 212 degrees Fahrenheit."

All four runs used a `$0.02` per-node allowance, which is enough for
`standard` but not `frontier` at this ladder's prices — every `answer` node
in these four runs was downgraded, never refused or branched, which is the
`downgrade` outcome from Section 6.2/9 of `s15architecture.md` confirmed
live rather than read from the code.

## 4. One Honest Limitation the Traces Exposed

**The `frontier` rung is currently unservable, and this is a live,
dated fact, not a configuration mistake.** `p7_cross_model_ladder.py`'s
`frontier` rung (provider `github`, model `openai/gpt-4.1`) failed with:

```
RuntimeError: gateway /v1/chat returned 502:
{"detail":"github failed: github HTTP 410: {\"error\":{\"code\":\"github_models_retirement_brownout\",
\"message\":\"GitHub Models is temporarily unavailable as part of a scheduled retirement brownout.\"}}"}
```

This is GitHub itself returning `410 Gone` for its Models API, wrapped by
the gateway as a `502`. It is not a bug in `glc_v4` or `S15Code` — it's an
upstream deprecation event happening as of this run (2026-08-05) — but it
has two concrete downstream effects the traces made visible rather than
hidden:

1. **`p7` failed its exit code.** `[FAIL] every rung answered` and
   `[FAIL] the top rung measurably costs more than the bottom rung` — the
   second check fails as a direct consequence of the first: with no
   `frontier` measurement, "top rung costs more" has nothing to compare.
   Both are honestly reported as failures, not silently skipped.
2. **`p1`'s strategy A (always-frontier) resolved 0/12 tasks** in the live
   run (Section 2.1) — all 12 frontier calls transport-failed for the same
   reason, so A never got billed and never resolved anything, which made
   the `B_vs_A` break-even comparison undefined (`n/a`) rather than
   wrong. `C` (budget-aware) never attempted frontier at all in the live
   run — with the allowances configured, cascade never escalated past
   `economy`, so it never *tried* the broken rung, which is arguably the
   controller working as intended (it never spent money finding out
   frontier was down).

**What this means for "reproducing the floor" honestly:** every proof that
depends on a live `frontier` call currently cannot fully pass against real
infrastructure — not because the economic control layer (routing, budget,
metering, tracing) is broken, but because one of the three rungs it's
configured to route to has been deprecated upstream mid-session. The
control layer's own behavior under that failure — downgrade where an
allowance permits it (Runs 1–4 above), refuse where it doesn't (Section
13/p3), and report the gap rather than paper over it (p7's two `[FAIL]`
lines, p1's `n/a` instead of a fabricated number) — is itself evidence the
layer is doing its job; the ladder's config (`tiers.yaml` naming `github` /
`openai/gpt-4.1` for `frontier`) is what needs updating, not the mechanism
enforcing it.

**A second, smaller limitation, also visible only by reading two sources
together:** every `answer` node's *graph*-level trace metadata
(`result["trace"]["agents"]["answer"]["tier"]`) reports `"frontier"` in all
four runs above — the tier the node *requested* — even though the *ledger*
row for that exact same call reports `tier: "standard"`, `decision:
"downgrade"`. `TIER_KEY` is written once into task metadata at plan time
(`s15code/economics/tiers.py:154`) and never updated after
`BudgetPolicy.decide()` downgrades the call, so a reviewer who reads only
the per-node graph/trace view — not the ledger — would report the wrong
model tier as having answered every one of these four runs. The ledger
(`budget["charges"][i]["tier"]`) is the only place the *served* tier is
recorded correctly; the node-level trace metadata is not a reliable source
for it.

## 5. Publication

This document existed before the README did. Once asked "Github Link to
Part 1 ... Need to see your README.md that describes your results/
findings," the gap was real: `README.md` only carried the Part 2/3 evidence
section (the billing-support policy and its adversarial test), never a
summary of this file's own results. Fixed by adding a "Reproduce the Floor"
section to `README.md` — both suites, the five-proof result table, all four
traced runs condensed into one table (prompt, tier requested→served,
Jaeger trace ID, cost, answer), the honest limitation above, and the
fresh-checkout reproduction commands — committed and pushed to the same
branch this document already lived on, updating both open pull requests at
once:

- README with the Part 1 section:
  `https://github.com/theschoolofai/S15Code/blob/part3-attack-the-budget/README.md`
- Upstream PR (the real submission target — this fork's `origin` is
  `deephazar-eva-ai/S15Code`, itself a fork of `theschoolofai/S15Code`, so a
  PR against the fork's own `main` never appears in the upstream's PR list):
  `https://github.com/theschoolofai/S15Code/pull/4`

The README section is the operator-facing summary; this file remains the
source of full detail — the complete ordered event trace and every ledger
row for all four runs, not just the condensed one-line-per-run table the
README carries.
