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

## Evidence — Assignment 15 (three submission links)

Workload: **ShopNova customer-support triage** (18 tickets).  
Policy: [`config/support_triage/`](config/support_triage/).  
Tasks: [`proofs/tasks/support_triage.jsonl`](proofs/tasks/support_triage.jsonl).  
Committed logs/summaries: [`proofs/evidence/`](proofs/evidence/) (raw `proofs/out/*.json` is gitignored).

| Ladder rung | Provider | Model |
|---|---|---|
| economy | groq | `openai/gpt-oss-120b` |
| standard | gemini | `gemini-3.1-flash-lite` |
| frontier | cerebras | `zai-glm-4.7` *(OpenAI `gpt-4o-mini` via `OPENAI_LLM_KEY` is wired in glc_v4 but returned `credit_balance_exhausted` on probe)* |

Budget policy: `downgrade_at: 0.40`, `refuse_at: 0.85`, `max_calls_per_run: 40`.

### Part 1 — Reproduce the floor

**GitHub evidence folder:** [`proofs/evidence/part1/`](proofs/evidence/part1/)

- Both pytest suites run locally (see part1 README for pass/fail notes).
- Five proofs exercised; **four runs** captured (prompt, tier/model, events, Jaeger ID, ledger, answer).
- **Honest limitation:** Jaeger returned 7/10 spans for trace `4a4c13bf124380bc7edf61939b2a6444` — costs and GenAI attrs still matched the ledger; root hierarchy was incomplete on the backend.

Details + tables: [part1/README.md](proofs/evidence/part1/README.md) · [p4_summary.json](proofs/evidence/part1/p4_summary.json) · [p3_summary.json](proofs/evidence/part1/p3_summary.json)

### Part 2 — Policy measurement

**GitHub evidence folder:** [`proofs/evidence/part2/`](proofs/evidence/part2/)

Live `p1` (OpenRouter judges = paid `openai/gpt-4o-mini`; free OpenRouter SKUs were at 0/day):

| Strategy | Spend | Calls | Cost/call | Resolved | Cost/resolved |
|---|---|---|---|---|---|
| A always_frontier | $0.001616 | 14 | $0.000115 | 13/18 | $0.000124 |
| B always_cheapest | $0.003191 | 18 | $0.000177 | **18/18** | $0.000177 |
| C budget_aware | $0.003435 | 19 | $0.000181 | **18/18** | $0.000191 |

- Break-even `r*`: **97.5%** (B vs C); B sits at **100%** (+2.5 pp). Signature trap **not** observed.
- Wrong cases: **`st14`** cascade paid ~2.1× vs always-cheapest for the same resolve; **`st11`** frontier failed while economy resolved.
- Full write-up + log: [part2/README.md](proofs/evidence/part2/README.md) · [p1_summary.json](proofs/evidence/part2/p1_summary.json) · [p1_live_rerun.log](proofs/evidence/part2/p1_live_rerun.log)

```bash
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/support_triage.jsonl \
  --config-dir config/support_triage --strategies A,B,C \
  --principal support/p1 --label support --budget 0.05 \
  --base-url http://127.0.0.1:8111
```

### Part 3 — Adversarial budget attack

**GitHub evidence folder:** [`proofs/evidence/part3/`](proofs/evidence/part3/)

- Attack: runaway loop via [`proofs/p_adversarial_support.py`](proofs/p_adversarial_support.py)
- **Before control:** ~$0.41 extrapolated over 10k rounds
- **After control:** spent $0.00158605 ≤ $0.002; 39 admitted / 161 refused
- Refusals visible as **`BudgetRefused`** on the graph

Details: [part3/README.md](proofs/evidence/part3/README.md)

### Reproduce from a fresh checkout

```bash
git clone https://github.com/Prerit-112/glc_v4.git
git clone https://github.com/Prerit-112/S15Code.git
docker compose -f glc_v4/docker-compose.observability.yml up -d
# glc_v4/.env: GEMINI_*, GROQ_*, CEREBRAS_*, OPEN_ROUTER_*, optional OPENAI_LLM_KEY
# S15Code/.env: GLC_BASE_URL=http://127.0.0.1:8111 (no provider keys)

cd glc_v4 && uv sync && set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 && uv run glc serve
cd S15Code && uv sync && uv run s15code serve

cd S15Code
uv run pytest -q
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/support_triage.jsonl \
  --config-dir config/support_triage --strategies A,B,C --principal support/p1 --label support --base-url http://127.0.0.1:8111
uv run python proofs/p_adversarial_support.py --base-url http://127.0.0.1:8111
uv run python proofs/p4_trace_export.py --task "Triage forgot password" --budget 0.02 \
  --config-dir config/support_triage --principal support/p4 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces --base-url http://127.0.0.1:8111
```

Secrets, `.env`, and real customer data never enter the pull request.

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
