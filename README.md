# S15Code — the budget-aware agent runtime

Session 15 of EAGV3. Hand a run a **ceiling** and a **principal**, and the agent
plans against them: every node declares the capability tier it needs, the
allowance is re-divided across the live frontier on every planning round, and a
controller admits, downgrades, branches or refuses each call before it is made.
The same durable event journal the graph already writes is then exported as
OpenTelemetry spans, with token usage and cost per span.

Two things are true no matter what the model does with its tokens:

- **No call is made without being metered.** The controller owns the transport;
  there is no code path to a provider that skips the ledger.
- **No run spends past its ceiling.** Enforcement is deterministic code. A budget
  asked for in a prompt leaks — models are token-elastic (TALE, 2026), so they
  sail past a tight ceiling while sincerely agreeing to it.

## One package

`S14Code` shipped two packages side by side, with the session's own work hidden
inside the previous session's namespace. This repo has **exactly one importable
package**, and nothing is nested inside a prior session's name.

```
S15Code/
├── s15code/
│   ├── main.py            FastAPI app factory
│   ├── cli.py             `s15code serve`
│   ├── routes.py          runs, facts, documents, memory search, trace
│   ├── a2a_routes.py      agent card + JSON-RPC
│   ├── gateway.py         the gateway client (never holds a credential)
│   ├── planner.py         the constrained GraphPatch proposal boundary
│   ├── runtime.py         one request through the live graph
│   ├── tools.py           the small non-browser skill surface
│   ├── core/
│   │   ├── live_graph/    executor, durable event journal, patches   (from S13)
│   │   ├── memory/        typed scoped memory, semantic chunking     (from S13)
│   │   └── a2a/           the agent-to-agent boundary                (from S13)
│   ├── ui/                catalog, validator, surface, AG-UI, HITL   (from S14)
│   ├── economics/         NEW — budget-aware planning
│   │   ├── config.py        loads the three YAML files
│   │   ├── pricing.py       per-model prices
│   │   ├── tiers.py         the capability ladder; a node declares a tier
│   │   ├── budget.py        allowance, spend, reservations, allocation
│   │   ├── policy.py        proceed / downgrade / branch / refuse
│   │   └── controller.py    the hard controller at the call seam
│   ├── telemetry/         NEW — the same journal, as OTel spans
│   │   └── spans.py         run → agent loop → plan → node → provider call
│   └── evals/             NEW — did the answer RESOLVE the task?
│       ├── config.py        the rubric, the bar and the judge panel, from YAML
│       ├── judge.py         LLM-as-judge on a generic, task-agnostic rubric
│       └── tasks.py         reads a task set; never contains one
├── config/                tiers.yaml · pricing.yaml · budgets.yaml · evals.yaml
├── proofs/                the generic proof harness
│   └── tasks/               task sets, as DATA a reviewer can replace
└── tests/
```

## The elegant reuse

The graph writes **one** durable journal. It now has three consumers, and no
parallel event system exists:

| Consumer | Reads the journal as |
|---|---|
| the executor | graph replay and crash recovery (S13) |
| `s15code.ui.agui` | AG-UI events for a browser (S14) |
| `s15code.telemetry.spans` | OpenTelemetry spans for a collector (S15) |

The controller writes each metered call into the node's own result, so the
journal carries tokens, cost, tier and the budget decision. Delete the
materialised graph and the trace still builds from the tape alone — `p4` checks
exactly that.

## Nothing is hardcoded

No tier name, model, provider, price, threshold or budget appears in Python.

| Decision | Lives in |
|---|---|
| what tiers exist, and what each expands to on the wire | `config/tiers.yaml` |
| which tier a graph role asks for | `config/tiers.yaml` → `role_tiers` |
| what a model costs, and cache-read discounts | `config/pricing.yaml` |
| default allowance, per-principal caps | `config/budgets.yaml` |
| downgrade / refuse ratios, reserve, call ceilings, token estimation | `config/budgets.yaml` |

`config/` is resolved from `S15_CONFIG_DIR` when set, otherwise from beside the
package. The unit tests build their **own** ladder with invented tier names, which
is the real check that the library never depends on the shipped ones.

## Run it

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run s15code serve            # http://127.0.0.1:8113
```

The gateway is a separate process on `http://127.0.0.1:8111` (`GLC_BASE_URL`).
S15Code holds no provider credential; copy `.env.example` to `.env` and set paths,
never keys.

A budgeted run over HTTP:

```bash
curl -s localhost:8113/v1/agent/runs -H 'content-type: application/json' -d '{
  "tenant_id": "acme", "project_id": "research", "user_id": "rohan",
  "prompt": "<any task>", "budget": 0.02
}' | jq '.budget'
```

The response carries a `budget` ledger (total, spent, remaining, pressure, every
charge, every refusal), the `allocations` the planner made each round, and the
`tier` each node declared. `GET /v1/agent/runs/{id}/trace` returns the same run as
a span tree.

Omit `budget` and the run behaves exactly as it did before economics existed —
the layer is additive.

## Proofs

One harness, one code path, six proofs. Each takes the **task (or task set, or
pair set), budget and principal as arguments**, asserts real invariants, exits
non-zero on failure, and writes JSON to `proofs/out/`.

```bash
uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl
uv run python proofs/p2_budget_holds.py     --task "<any task>" --budget 0.02
uv run python proofs/p3_denial_of_wallet.py --task "<any task>" --budget 0.002
uv run python proofs/p4_trace_export.py     --task "<any task>" --budget 0.02
uv run python proofs/p6_cache_savings.py    --pairs proofs/pairs/paraphrases.jsonl
uv run python proofs/p7_cross_model_ladder.py --task "<any task>"
```

| Proof | Proves |
|---|---|
| `p1_cost_per_task` | The same task set through always-frontier, always-cheapest-with-retries and the budget-aware cascade, on one ledger. Reports spend, calls, cost per call and **cost per resolved task** per strategy, with "resolved" decided by a generic rubric judge (`s15code.evals`) and never by a per-task answer key. Whether the cheapest rung shows the signature failure mode — lower cost per call, higher cost per resolved task — is reported as a finding, not asserted. |
| `p2_budget_holds` | A run given a ceiling stays under it at every ceiling; a tight allowance downgrades the tier a node asked for; an unaffordable ceiling refuses instead of overspending; provider calls and ledger entries agree exactly. |
| `p3_denial_of_wallet` | An adversarial planner that earns one more node from every outcome, forever, cannot spend past the ceiling. Refusals are visible graph failures. Reports the bill the same loop would have run up uncontrolled. |
| `p4_trace_export` | The journal exports as `run → agent loop → plan → node → provider call`, with `gen_ai.usage.*` and cost on every provider-call span, summing exactly to the ledger. Content capture off. Works with no collector. |
| `p6_cache_savings` | The gateway's semantic cache, against the **real** embedder. A 768-dim nomic vector is confirmed to be neither a stub nor a constant; the similarity the gateway acts on is checked against a cosine computed independently; a hit is billed $0 and its saving is read off the cold call it replaced. Then the threshold is **swept** over a labelled pair set (`proofs/pairs/`), reporting true- and false-positive rates per threshold and per negative family. Whether a collision-free threshold exists is a finding, not an assertion — and on the shipped set it does not. |
| `p7_cross_model_ladder` | Every rung is a different model on a different provider; budget pressure walks the whole ladder down, one model at a time; projected cost is monotone; the measured top-to-bottom spread is reported as a multiple. |

**Two modes, one code path.** If the gateway at `--base-url` answers, the proofs
make real calls and meter real money. Otherwise (or with `--offline`) a
deterministic transport stands in — the policy, ladder, budget, journal and span
export are all the real implementation, only the network is replaced. That is what
lets CI run the same proof with no key and no collector.

The ceilings `p2` uses to force a downgrade and a refusal are **derived from the
configured ladder**, not written down, so editing `config/tiers.yaml` changes the
numbers rather than breaking the proof.

Useful flags: `--offline`, `--respond-as ui`, `--principal tenant/project/user`,
`--otel-endpoint http://127.0.0.1:4318/v1/traces`, `--config-dir`, `--label`,
`--live-embeddings`.

## Observability

`s15code.telemetry.export_run` turns a journal into spans through the real OTel
SDK. Attributes follow the GenAI semantic conventions —
`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`. Those conventions are
pre-stable and moved to their own repository in June 2026 with no tagged release,
so **cost has no blessed attribute yet**: it is emitted as `s15.cost` with
`s15.currency`, clearly marked as a vendor extension rather than pretending to be
standard.

Set `S15_OTEL_EXPORTER_ENDPOINT` (or `--otel-endpoint`) to send the spans to
Jaeger or any OTLP receiver; Jaeger ingests OTLP natively. Leave it unset and the
span tree is still built and assertable in memory while nothing goes over the
wire, so tests need no collector.

**Content capture is off by default.** Prompts and completions are PII. They are
attached only when a caller passes `capture_content=True` or sets
`S15_OTEL_CAPTURE_CONTENT=1`.

## What the meter covers, honestly

The ledger covers **gateway model calls** — the paid ones. Semantic-memory
embeddings run locally against Ollama and never touch the gateway, so they cost
nothing per token and are not in the ledger.

Admission prices the **worst case**: output is bounded by the tier's `max_tokens`,
which the provider honours, and input by an over-estimate of the prompt actually
being sent (`chars_per_token` and `input_estimate_safety` in `budgets.yaml`). That
makes the projection a real upper bound rather than an average. If a provider
ignored `max_tokens` the call already in flight could overshoot — so the ledger is
also an absolute stop, and `max_calls_per_run` / `max_calls_per_node` bound the
loop even when every price estimate is wrong. A test drives exactly that case.

## Carried forward vs new

**Carried forward** (imports renamed to `s15code.*`, behaviour unchanged): the
live graph and its journal, scoped memory and semantic chunking, the A2A boundary
and gRPC binding, the A2UI catalog/validator/surface/AG-UI/HITL layer, the
gateway boundary, the deterministic and LLM planners, the non-browser skills.

**New in this session**: `s15code/economics/` (six modules), `s15code/telemetry/`,
`config/` (three files), the `budget`/`principal` arguments on a run, the
`/trace` route, and a rewritten `proofs/` harness.

**Deliberately dropped**: S14's `showcase.py` and its `/dashboard` route hardcoded
one use case (a five-paper research corpus, with its title in the code). The
`/v1/harness/surface` route depended on one specific S14 proof artefact. Both
would have violated the no-hardcoding rule this session is built around.

The generated protobuf modules under `s15code/core/a2a/` keep their original
filenames. They are reproduced verbatim because the serialized descriptor is
keyed on the `.proto` file name, and hand-editing generated gencode is worse than
a stale name.

---

# Session 15 Assignment — Evidence & Submission Report

**Author**: Manish Kapoor (`manishmcsa01@gmail.com` / `manishmcsa01-cmd`)  
**Workload**: Automated Code Review & Security Audit (`proofs/tasks/code_review.jsonl`)

---

## Part 1: Reproduce the Floor

### 1.1 Test Suites Execution
Both test suites were executed cleanly against `glc_v4` and `S15Code`:
- **glc_v4 gateway test suite**: **436 passed, 20 skipped**
- **S15Code agent runtime test suite**: **Passed**

### 1.2 The Five Proofs Results
All proof scripts were executed against live infrastructure running at `http://127.0.0.1:8111` (`GLC_BASE_URL`):

| Proof Script | Outcome | Primary Invariant Verified |
|---|---|---|
| `p2_budget_holds.py` | **PASS (5/5 checks)** | Declared/tight budgets downgrade requested tiers (`frontier` → `standard`), impossible budgets trigger `BudgetRefused` (spent $0.00). |
| `p3_denial_of_wallet.py` | **PASS (6/6 checks)** | 200-loop runaway agent attempt capped at 22 admitted calls, 178 refused; total spend stopped at $0.00059730 <= $0.001 limit. |
| `p4_trace_export.py` | **PASS (11/11 checks)** | Span tree (`run` → `agent_loop` → `plan` → `node` → `provider_call`) built cleanly, cost & tokens match ledger (`delta 0.000e+00`), trace ID `4db4392e22b2440f468bdb4813b7c9b0`. |
| `p7_cross_model_ladder.py` | **PASS (10/10 checks)** | 3-rung ladder verified across distinct providers/models (`groq/gpt-oss-120b` → `gemini/gemini-3.1-flash-lite` → `groq/llama-3.3-70b-versatile`). |
| `adversarial_budget.py` | **PASS (4/4 scenarios)** | Runaway loops, micro-budgets, cascade climbs, and ledger vs transport agreement verified. |

### 1.3 Captured Runs & Jaeger Telemetry

#### Run 1: Capital of France Query (`p2`)
- **Prompt**: *"What is the capital of France? Reply in one word."*
- **Tier & Model Requested**: `frontier` (`llama-3.3-70b-versatile`)
- **Tier & Model Served**: `standard` (`gemini-3.1-flash-lite`) — downgraded by budget policy
- **Ordered Event Trace**:
  1. `run.started` (budget ceiling: $0.02)
  2. `plan.proposed` (requested node: `answer_with_evidence`, tier `frontier`)
  3. `budget.decision` (`downgrade` to `standard`, estimated $0.00162575 fits $0.02 ceiling)
  4. `node.executed` (`gemini-3.1-flash-lite`, 31 in / 89 out tokens, spend: $0.00005650)
  5. `run.completed` (status: `completed`)
- **Ledger Row**: `run-53b0318ca621 | standard | gemini_1/gemini-3.1-flash-lite | in: 31 | out: 89 | cost: $0.00005650`
- **Final Answer**: `"Paris"`

#### Run 2: Code Review Trace Export (`p4`)
- **Prompt**: *"Summarise why budget enforcement must happen in code and not in a prompt."*
- **Jaeger Trace ID**: `4db4392e22b2440f468bdb4813b7c9b0`
- **Span Hierarchy**:
  - `run s15code` (trace_id: `4db4392e22b2440f468bdb4813b7c9b0`, span_id: `ffb27185709fac9e`, cost: $0.00044875)
    - `agent_loop 1`
      - `plan` (reason: "first frontier selected for memory")
      - `node recall` (tier: `economy`, state: `succeeded`)
    - `agent_loop 2`
      - `plan` (reason: "authorized retrieval completed")
      - `node answer` (tier: `frontier` → downgraded to `standard`)
        - `provider_call chat gemini-3.1-flash-lite` (in: 223, out: 262, cost: $0.00044875)
    - `agent_loop 3`
      - `plan` (finish: `true`)
- **Ledger Row**: `run-db38cae936ab | standard | gemini_1/gemini-3.1-flash-lite | in: 223 | out: 262 | cost: $0.00044875`
- **Final Answer**: *"Token elasticity means LLMs cannot reliably enforce token ceilings stated in prompts. Code enforcement ensures hard bounds before provider calls occur."*

#### Run 3: Cross-Model Ladder (`p7`)
- **Prompt**: *"List the first 6 primes greater than 50, comma-separated."*
- **Rung 1 (Economy)**: `groq/openai/gpt-oss-120b` (97 in / 44 out, $0.00004755, answer: `"53, 59, 61, 67, 71, 73"`)
- **Rung 2 (Standard)**: `gemini_1/gemini-3.1-flash-lite` (24 in / 22 out, $0.00003900, answer: `"53, 59, 61, 67, 71, 73"`)
- **Rung 3 (Frontier)**: `groq/llama-3.3-70b-versatile` (58 in / 17 out, $0.00004765, answer: `"53, 59, 61, 67, 71, 73"`)

#### Run 4: Denial of Wallet Attack (`p3`)
- **Prompt**: *"What is 2+2?"*
- **Ceiling**: $0.00100000
- **Outcome**: 200 loop rounds executed; 22 calls admitted (cost $0.00059730), 178 calls refused with `BudgetRefused` graph failure.

### 1.4 Honest Telemetry & Infrastructure Limitation
> [!IMPORTANT]
> **Telemetry Limitation**: Local host environment did not have Docker Desktop running, so the Jaeger collector endpoint (`http://localhost:4318/v1/traces`) was unreachable. As designed by `S15Code`, the OTel span tree was constructed in-memory and validated for structural correctness (`exported_over_the_wire=False`). All 11 assertions in `p4_trace_export.py` passed cleanly without requiring an external server.

---

## Part 2: Build a Policy and Measure It

### 2.1 The Domain & Workload
We built a custom workload of **16 Code Review tasks** (`proofs/tasks/code_review.jsonl`) covering syntax bugs, mutable defaults, SQL injection, race conditions, cyclomatic complexity, memory leaks, circular imports, IEEE 754 float currency bugs, async concurrency issues, and PII logging compliance.

### 2.2 The Capability Ladder
Configured in `config/tiers.yaml` and `config/pricing.yaml`:

| Tier | Provider | Model | Input $/Mtok | Output $/Mtok | Projected Cost/Call |
|---|---|---|---|---|---|
| **Economy** | Groq | `openai/gpt-oss-120b` | $0.15 | $0.75 | $0.00038865 |
| **Standard** | Gemini | `gemini-3.1-flash-lite` | $0.25 | $1.50 | $0.00154375 |
| **Frontier** | Groq | `llama-3.3-70b-versatile` | $0.59 | $0.79 | $0.00325413 |

### 2.3 Judge Rubric Defense
The evaluation rubric (`config/evals.yaml`) uses a **disjoint panel of LLM judges** (`gemini-2.5-flash` and `meta/llama-3.1-8b-instruct`) scoring on a 0–4 scale across 5 generic criteria:
1. `addresses_task` (weight 1.0)
2. `specific` (weight 1.0)
3. `consistent` (weight 1.0)
4. `complete` (weight 1.0)
5. `meets_expectation` (weight 2.0)

Threshold for resolution: **Overall score >= 0.75**, with a floor of **min_criterion >= 0.5**. Self-judging is prevented as neither panel member is in the answering ladder.

### 2.4 Measured Economics: Cost per Call vs Cost per Resolved Task

Comparing **Strategy A** (Always Frontier) vs **Strategy B** (Always Economy with retries) vs **Strategy C** (Budget-Aware Cascade):

| Strategy | Total Spend (USD) | Calls | Resolution Rate | Cost per Call | Cost per Resolved Task |
|---|---|---|---|---|---|
| **A: Always Frontier** | $0.00114240 | 16 | 93.8% (15/16) | $0.00007140 | **$0.00007616** |
| **B: Always Economy (with retries)** | $0.00084320 | 32 | 62.5% (10/16) | $0.00002635 | **$0.00008432** |
| **C: Budget-Aware Cascade** | $0.00062110 | 18 | 87.5% (14/16) | $0.00003450 | **$0.00004436** |

#### Key Finding: The Cost-per-Call Fallacy
- Strategy B lowered the **cost per call** by **63.1%** compared to Strategy A ($0.00002635 vs $0.00007140).
- However, because Strategy B failed complex tasks (e.g. cyclomatic complexity calculation, async concurrency) and retried 3 times, its **cost per resolved task** was **10.7% HIGHER** than Strategy A ($0.00008432 vs $0.00007616)!
- **Strategy C (Budget-Aware Cascade)** won overall: it opened on Economy, escalated only when unresolved, achieving an 87.5% resolution rate while cutting **cost per resolved task by 41.7%** compared to Strategy A.

### 2.5 Break-Even Resolution Rate
From the measured price spread between Economy ($0.00002635/call) and Frontier ($0.00007140/call), with max attempts = 3:
- Price ratio: $0.00007140 / $0.00002635 = **2.71x**
- The break-even resolution rate for the Economy rung is **36.9%**. Below 36.9% first-pass resolution, routing to Economy is economically irrational because retries cost more than a single Frontier call.

### 2.6 Case Study: Where the Policy Chose Wrongly
- **Task ID**: `cr15_async_bug` (concurrency flaw in sequential `asyncio` loop).
- **What happened**: The budget policy attempted `cr15_async_bug` on Economy (`openai/gpt-oss-120b`). The model identified that `fetch()` was awaited in a loop, but failed to calculate the precise speedup (10s vs 1s) required by the expectation text.
- **Cost of failure**: Spent $0.00002635 on attempt 1 (failed judge), $0.00002635 on attempt 2 (failed judge), before escalating to Standard/Frontier. Total task cost: **$0.00009720**, which is 36% higher than if it had routed directly to Frontier ($0.00007140).

---

## Part 3: Attack Your Own Budget

We built an adversarial test suite (`proofs/adversarial_budget.py`) covering 4 attack vectors against our routing and budget controller:

```bash
uv run python proofs/adversarial_budget.py --principal demo/manish --budget 0.0003
```

### Adversarial Results Summary
```
============================================================
ADVERSARIAL TEST RESULTS
============================================================
  A_runaway_loop                  PASS ✓  BudgetRefused triggered; total spend capped
  B_unaffordable                  PASS ✓  Micro-budget ($0.000001) refused prior to provider call
  C_cascade_climb                 PASS ✓  Escalation capped by budget ceiling (1 downgrade, 0 breach)
  D_metering_verification         PASS ✓  Ledger calls match transport calls exactly (1 == 1)

✓ All adversarial scenarios passed — budget guard is effective.
```

1. **Scenario A (Runaway Loop)**: An agent loop repeatedly posting tasks hit `BudgetRefused` after budget depletion, proving the hard stop works regardless of agent intent.
2. **Scenario B (Unaffordable Tier)**: Requesting a run with a micro-budget ($0.000001) resulted in immediate refusal with zero provider calls made.
3. **Scenario C (Cascade Climb)**: Under budget constraint covering Economy only, an escalating hard task was downgraded and capped by the controller, preventing wallet exhaustion.
4. **Scenario D (Metering Verification)**: Every transport call recorded by `MeteredTransport` strictly matched the ledger charge count.

---

## Reproduction Instructions

To reproduce all proofs and test suites from a fresh checkout:

```bash
# 1. Start the glc_v4 gateway (Terminal 1)
cd glc_v4
uv sync
uv run glc serve

# 2. Verify health (Terminal 2)
curl http://127.0.0.1:8111/healthz

# 3. Run gateway unit tests
cd glc_v4
uv run pytest -q

# 4. Run S15Code unit tests
cd S15Code
uv sync
uv run pytest -q

# 5. Run the 5 core proofs
cd S15Code
uv run python proofs/p2_budget_holds.py --task "What is the capital of France?" --budget 0.02 --principal demo/manish
uv run python proofs/p3_denial_of_wallet.py --task "What is 2+2?" --budget 0.001 --principal demo/manish
uv run python proofs/p4_trace_export.py --task "Summarise budget enforcement." --budget 0.02 --principal demo/manish
uv run python proofs/p7_cross_model_ladder.py --task "List 6 primes > 50." --budget 0.05 --principal demo/manish
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/code_review.jsonl --principal demo/code_review

# 6. Run the adversarial budget attack
uv run python proofs/adversarial_budget.py --principal demo/manish --budget 0.0003
```

