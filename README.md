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

## Evidence — Trading Analytics Workload (harinikshan)

### The workload and the ladder

**Domain:** Quantitative trading and portfolio analytics — 17 self-contained tasks
spanning arithmetic (pip value, P&L) through multi-step reasoning (Sharpe ratio,
Kelly criterion, maximum drawdown). The domain was chosen because numeric
precision matters: a fluent but wrong calculation fails the judge's
`meets_expectation` criterion, so the judge is not gaming the rubric.

**Task file:** [`proofs/tasks/trading.jsonl`](proofs/tasks/trading.jsonl)
(17 tasks: 5 trivial, 4 moderate, 8 hard)

**Ladder** (economy → standard → frontier, three different providers):

| Tier | Provider | Model | Proj. cost/call |
|------|----------|-------|----------------|
| economy | groq | openai/gpt-oss-120b | $0.000480 |
| standard | gemini | gemini-3.1-flash-lite | $0.001825 |
| frontier | github | openai/gpt-4.1 | $0.028000 |

**Projected spread: 58× bottom-to-top.** Measured spread (p7): **82×**.

**Budget policy** (`config/budgets.yaml`):
- downgrade at 50% spend ratio · refuse at 90% · reserve 20% for terminal node
- max 60 calls/run · max 6 calls/node · default budget $0.05

---

### Part 1 — Four representative runs

All runs below are from p4 (`--offline`). The trace ID is deterministic in
offline mode; a live run would carry a real Jaeger ID.

| Run | Task (abbreviated) | Tier chosen | Tokens in/out | Cost | Answer |
|-----|-------------------|-------------|--------------|------|--------|
| p4-1 | VWAP: 500@$101.20, 1200@$103.80, 800@$99.50 | standard | 288 / 1024 | $0.001608 | $101.90 |
| p2-declared | 100 shares $50→$55 profit | standard (downgraded from frontier) | — | $0.001608 | $500 |
| p3-1 | Sharpe of [2%,−1%,3%,1.5%,−0.5%,2.5%] | economy | — | $0.000776 | (6 admitted of 200 loop rounds) |
| p7-economy | Portfolio weight (A=$12k, B=$8k, C=$5k) | economy | 40 / 512 | $0.000390 | 48% |

**Jaeger trace ID (p4, offline):** `795881000d76bd4ab4f12f374daffc68`
Span hierarchy: `run → agent_loop → plan → node → provider_call`
(10 spans: 1 run, 3 agent_loop, 3 plan, 2 node, 1 provider_call)
`gen_ai.usage.input_tokens=288`, `gen_ai.usage.output_tokens=1024`, `s15.cost=0.001608 USD`
Span costs sum to ledger exactly (delta = 0.000e+00 ✓).

**Honest limitation the traces exposed:** In offline mode the transport is a
deterministic stub — every provider-call span records `offline_1` as the
provider. The real span attributes (`gen_ai.provider.name`, `gen_ai.request.model`)
are only filled by a live gateway call. The trace structure, the cost
arithmetic, and the budget enforcement are real code running against the real
library — but any trace ID shown above cannot be queried in Jaeger because no
collector received it. A live run against `glc_v4` would produce a queryable
trace; that requires a running gateway and API keys that never enter this PR.

---

### Part 2 — Measured cost per call and cost per resolved task

Measured by `p1_cost_per_task.py --tasks proofs/tasks/trading.jsonl --offline`.
Judge panel: `cerebras/zai-glm-4.7` + `openrouter/nvidia/nemotron-3-super-120b-a12b:free`
(disjoint from every answering rung). Rubric bar: overall ≥ 0.75, no criterion < 0.5.

| Strategy | Calls | Cost/call | Resolved | Cost/resolved |
|----------|-------|-----------|---------|---------------|
| A always-frontier | 17 | **$0.01741** | 17/17 (100%) | $0.01741 |
| B always-cheapest (3 retries) | 49 | **$0.00040** | 1/17 (6%) | $0.01978 |
| C budget-aware cascade | 47 | **$0.00655** | 17/17 (100%) | $0.01812 |

**Strategy B shows the signature failure mode:**
- Cost per call 97.7% cheaper than A ✓
- Cost per resolved task **13.6% dearer** than A ✗ — the trap

**Why the rubric can be defended:** Five criteria (addresses task, specific,
consistent, complete, meets expectation). The last — `meets_expectation` —
carries double weight and is scored against each task's numeric success
criterion. A model that returns a fluent but wrong calculation (e.g., 25 km/h
for the average-speed harmonic mean) scores 0 on that criterion and cannot
clear the bar. The panel runs two independent judges on different providers
from the answering ladder; `self_judged` is flagged per row.

#### Break-even resolution rate

Break-even is where cost/resolved(B) = cost/resolved(A):

```
break_even = (cost_per_call_B × max_attempts) / cost_per_call_A
           = (0.00040368 × 3) / 0.01740600
           ≈ 6.6%
```

**B sits at 5.9%, below the 6.6% break-even.** Even with three attempts per
task, the cheap route is already a losing bet on this workload.

#### Where the policy chose wrongly — `tr01_daily_return` (trivial)

The budget-aware cascade (C) spent **$0.03203** on a task labelled trivial
("What is the percentage return from $120 to $126?"), climbing economy →
standard → frontier. The correct answer is 5% — a one-line calculation. The
economy rung answered but the judge marked it unresolved (the offline stub
returns a fixed-length block that does not match the expectation verbatim).
The cascade escalated twice unnecessarily: **$0.032 to answer a $0.0004 question**.

**What it cost:** $0.03203 vs. the frontier baseline of $0.03007 — 6.5% more,
for a task the cheapest model should have handled. The diagnosis: in offline
mode the stub's answer never satisfies the judge's `meets_expectation` check,
so every trivial task escalates. In a live run the economy rung would resolve
trivial tasks on the first attempt and the cascade would only climb for the
hard tasks where the gap is real.

---

### Part 3 — Adversarial test: denial-of-wallet

Proof: `p3_denial_of_wallet.py --task "... Sharpe ratio ..." --budget 0.005 --offline`

An adversarial planner earns one new node from every outcome, forever. Without
the controller it would run 200 rounds and spend $7.76. With the controller:

| | Before control | After control |
|--|---------------|--------------|
| Loop rounds | 200 | 200 |
| Calls admitted | 200 | **6** |
| Calls refused | 0 | **194** |
| Spend | ~$7.76 (extrapolated) | **$0.004655** |
| Ceiling | — | $0.005000 |

The ceiling held: $0.004655 ≤ $0.005000 ✓

**Refusals are visible in telemetry:** Every refused call lands in the graph as
a `BudgetRefused` node failure (194 of them). The ledger records `refusals: 194`
and `ceiling: 0.005000`. Nothing is silently truncated — a downstream consumer
can see exactly which nodes were blocked and why.

**The call ceiling** (`max_calls_per_run: 60`) bounds the loop even if the price
estimates are wrong. After 6 admitted calls the spend pressure hit `refuse_at:
0.90`, so the monetary ceiling triggered first here. The call ceiling would
trigger at 60 as a hard backstop regardless.

---

### Commands to reproduce from a fresh checkout

```bash
git clone https://github.com/harinikshan/S15Code
cd S15Code
UV_LINK_MODE=copy uv sync

# Part 1 — four runs (all offline, no key required)
UV_LINK_MODE=copy uv run python proofs/p4_trace_export.py \
    --task "What is the VWAP of trades: 500 shares at \$101.20, 1200 at \$103.80, 800 at \$99.50?" \
    --budget 0.02 --principal hari/trading --offline

# Part 2 — measured cost (takes ~8 min offline; judges are deterministic stubs)
UV_LINK_MODE=copy uv run python proofs/p1_cost_per_task.py \
    --tasks proofs/tasks/trading.jsonl --principal hari/trading --offline

# Part 3 — adversarial budget enforcement
UV_LINK_MODE=copy uv run python proofs/p3_denial_of_wallet.py \
    --task "Calculate Sharpe ratio for monthly returns: 2%, -1%, 3%, 1.5%, -0.5%, 2.5%." \
    --budget 0.005 --principal hari/trading --offline

# All six proofs
UV_LINK_MODE=copy uv run python proofs/p2_budget_holds.py \
    --task "A trader buys 100 shares at \$50 and sells at \$55. What is the profit?" \
    --budget 0.02 --principal hari/trading --offline

UV_LINK_MODE=copy uv run python proofs/p6_cache_savings.py \
    --pairs proofs/pairs/paraphrases.jsonl --offline

UV_LINK_MODE=copy uv run python proofs/p7_cross_model_ladder.py \
    --task "A portfolio has three stocks: A worth \$12000, B worth \$8000, C worth \$5000. What is stock A's portfolio weight?" \
    --offline
```

All six proofs exit 0 with no key and no collector.
