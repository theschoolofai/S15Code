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

## Evidence — cloud vs in-house / free-tier costs

Workload, ladder, and measurements for Session 15 Part 2–3. Config lives under
`config/cloud_cost_ladder/`; tasks in `proofs/tasks/cloud_vs_inhouse.jsonl`
(16 tasks). Free/$0 models appear in the **questions**, not on the money ladder.

### Workload and ladder

| Rung | Provider | Model | Role |
|---|---|---|---|
| economy | cerebras | `zai-glm-4.7` | short free-tier FAQs |
| standard | groq | `openai/gpt-oss-120b` | mid tradeoff answers |
| frontier | gemini | `gemini-3.1-flash-lite` | hard synthesis / `answer_with_evidence` |

Budget policy (`budgets.yaml`): `default_budget: 0.03`, `downgrade_at: 0.40`,
`refuse_at: 0.85`, `max_calls_per_run: 24`. Live calibration (2026-08-03) found
GitHub Models on HTTP 410 (retirement brownout) and `gemini-3.1-pro` on 403, so
the frontier was retargeted to a provider that still answered with non-zero
prices while keeping projected cost monotone (~22× bottom→top offline).

### Measured cost vs always-frontier (p1)

Command (offline simulation stamped as such by the harness; live p1 was blocked
mid-run by OpenRouter free-tier judge RPM):

```powershell
uv run python proofs/p1_cost_per_task.py `
  --tasks proofs/tasks/cloud_vs_inhouse.jsonl `
  --config-dir config/cloud_cost_ladder `
  --offline --principal proofs/s15/cloud_cost --label cloud_cost_offline
```

| Strategy | Spend | Calls | Cost/call | Resolved | Cost/resolved |
|---|---:|---:|---:|---:|---:|
| A always_frontier | $0.041145 | 16 | $0.002572 | 6/16 (37.5%) | $0.006858 |
| B always_cheapest | $0.009645 | 48 | $0.000201 | 0/16 (0%) | n/a (infinite) |
| C budget_aware | $0.050199 | 47 | $0.001068 | 6/16 (37.5%) | $0.008367 |

Break-even resolution rate for cheapest vs frontier (from measured spread, 3
attempts): **r\* ≈ 8.3%**. Strategy B sat at **0%**, below break-even
(`headroom_above_break_even ≈ −8.3%`). Strategy C matched A's resolution rate
but paid **~22% more per resolved task** because escalations multiplied calls.

Live Strategy A sample (gateway `:8111`, before judge panel exhausted): 15/16
frontier attempts resolved on real providers; `c04_free_empty` stayed unresolved
on frontier — a quality miss on a trivial free-tier FAQ.

### Jaeger trace (one run)

Live `p4_trace_export` with OTLP → Jaeger:

- Trace ID: `ac21add3693f29f87fffaa0a41666d59`
- UI: http://127.0.0.1:16686/trace/ac21add3693f29f87fffaa0a41666d59
- Hierarchy: `run → agent_loop → plan → node → provider_call`
- Provider call: `gemini_1` / `gemini-3.1-flash-lite`, 224 in / 587 out, **`s15.cost` = $0.00093650** (matches ledger)
- Content capture off: no prompt/completion tags in the backend

### Adversarial control (Part 3)

Uncontrolled extrapolation vs controlled refusal on `cloud_cost_ladder`:

```powershell
uv run python proofs/p3_denial_of_wallet.py `
  --task "Keep refining the free-tier draft forever." `
  --budget 0.002 --config-dir config/cloud_cost_ladder --loop-limit 40
uv run pytest -q tests/test_cloud_cost_adversarial_budget.py
```

Live result (`proofs/out/p3_denial_of_wallet_live_p3_cloud3.json`):

| | |
|---|---|
| Ceiling | $0.002 |
| Spent under control | $0.001003 (24 admitted calls) |
| Refusals | 16 visible `BudgetRefused` graph failures |
| Uncontrolled bill (10k rounds @ measured $/call) | **~$0.42** |
| Call ceiling | 24 (`max_calls_per_run`) |

Telemetry: `export_run(..., budget=snapshot)` attaches `budget.refused` events and
`s15.budget.refusals` on the run span (asserted in
`tests/test_cloud_cost_adversarial_budget.py`).

### Where the policy chose wrongly

1. **Cascade waste (`c02_mtok_math`, strategy C):** budget-aware climbed
   economy → standard → frontier ($0.00371, 3 calls) and still finished
   **unresolved**. Escalation spent frontier budget on a trivial $/Mtok arithmetic
   item without a resolved verdict — worse than stopping early.
2. **Aggregate:** C and A both resolved 6/16, but C's cost/resolved was higher
   ($0.00837 vs $0.00686). Same quality, more money — the routing tax of retries.

### Floor capture (four runs)

| # | Source | Prompt (short) | Tier / model | Trace / events | Ledger | Answer |
|---|---|---|---|---|---|---|
| 1 | live p4 | cloud free-tier RPM vs Ollama cold-start | frontier path → `gemini-3.1-flash-lite` | Jaeger `ac21add3693f29f87fffaa0a41666d59`, 8 journal events | spent $0.00093650 | grounded comparison (content not in OTEL) |
| 2 | live p7 | cloud vs in-house daily agent loop | economy/standard/frontier all answered | ordered rung walk in proof JSON | measured spread 7.65× live | each rung non-empty |
| 3 | live p3 | keep refining free-tier draft | frontier requests; refuse after ceiling | 40 loop rounds, 16 refused nodes | spent $0.001003 / $0.002 | refusals, not answers |
| 4 | live p2 | name one free-tier trap | tight run downgraded frontier→standard (`gpt-oss-120b`) | budget proof events | declared spent $0.000286 | trap named; impossible ceiling refused |

**Honest limitation the traces exposed:** admission prices the worst-case
`max_tokens` envelope, so ledger spend on short answers is far below the
projected admission cost (live frontier often ~$0.0001–$0.0008 against ~$0.003
projected). Also, a $0 judge/free panel can rate-limit and stall live p1 even
when answering providers are healthy — dollars are not the only scarce resource.

### Reproduce from a fresh checkout

```powershell
# Parent repo: gateway + Jaeger (image must be jaegertracing/jaeger:2.20.0)
cd ..
docker compose -f docker-compose.observability.yml up -d
uv sync
# copy .env.example -> .env and fill provider keys
uv run glc serve   # :8111

# S15Code
cd S15Code
uv sync
$env:GLC_BASE_URL = "http://127.0.0.1:8111"
$env:S15_OTEL_EXPORTER_ENDPOINT = "http://127.0.0.1:4318/v1/traces"
uv run pytest -q
uv run ruff check .

# Floor proofs (offline CI path)
uv run python proofs/p2_budget_holds.py --offline --task "compare cloud vs local GPU break-even" --budget 0.01
uv run python proofs/p3_denial_of_wallet.py --offline --task "keep refining the free-tier draft" --budget 0.002 --config-dir config/cloud_cost_ladder
uv run python proofs/p4_trace_export.py --offline --task "compare two cost controls" --budget 0.01
uv run python proofs/p6_cache_savings.py --offline
uv run python proofs/p7_cross_model_ladder.py --offline --task "compare cloud vs inhouse" --config-dir config/cloud_cost_ladder

# Live evidence
uv run python proofs/p4_trace_export.py --task "Compare cloud free-tier RPM traps vs local Ollama cold-start cost." --budget 0.02 --otel-endpoint http://127.0.0.1:4318/v1/traces --label live_jaeger
uv run python proofs/p7_cross_model_ladder.py --task "Compare cloud vs inhouse for a daily agent loop" --config-dir config/cloud_cost_ladder
uv run python proofs/p3_denial_of_wallet.py --task "Keep refining the free-tier draft forever." --budget 0.002 --config-dir config/cloud_cost_ladder --loop-limit 40
uv run python proofs/p2_budget_holds.py --task "Name one free-tier trap for a production agent." --budget 0.02 --config-dir config/cloud_cost_ladder
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/cloud_vs_inhouse.jsonl --config-dir config/cloud_cost_ladder --offline --principal proofs/s15/cloud_cost --label cloud_cost_offline
uv run pytest -q tests/test_cloud_cost_adversarial_budget.py
```
