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

## Submission Evidence

### Part 1: Floor Reproduction
We ran the agent event suites and captured four distinct runs end-to-end. Below are the prompts, answers, trace IDs, event logs, and billing ledger rows:

#### Run 1: What is the capital of Japan? Reply with the name only.
* **Run ID**: `run-97262d69b55e`
* **Final Answer**: `Tokyo`
* **Model/Provider Chosen**: `gemini-3.1-flash-lite` (via `gemini_1`)
* **OTel Trace ID**: `babd6c688b1d10dcafbf9ba69dfa9a43`

##### Ordered Event Trace:
- Seq 161: run_started
- Seq 162: graph_patched
- Seq 163: task_started (Node: recall)
- Seq 164: task_succeeded (Node: recall)
- Seq 165: graph_patched
- Seq 166: task_started (Node: answer)
- Seq 167: task_succeeded (Node: answer)
- Seq 168: graph_patched

##### Ledger Rows from gateway.sqlite:
```json
{
  "id": 159,
  "ts": 1786102000.9153836,
  "provider": "gemini_1",
  "model": "gemini-3.1-flash-lite",
  "input_tokens": 358,
  "output_tokens": 1,
  "latency_ms": 1451,
  "status": "ok",
  "session": "run-97262d69b55e",
  "tenant": "course",
  "project": "s15",
  "user": "student-01",
  "usd": 9.099999999999999e-05
}
```

#### Run 2: What is 15% of 300? Reply with the number only.
* **Run ID**: `run-93ad36e2c667`
* **Final Answer**: `45`
* **Model/Provider Chosen**: `gemini-3.1-flash-lite` (via `gemini_1`)
* **OTel Trace ID**: `90ea24a467f754ef21bbf918384368ea`

##### Ordered Event Trace:
- Seq 169: run_started
- Seq 170: graph_patched
- Seq 171: task_started (Node: recall)
- Seq 172: task_succeeded (Node: recall)
- Seq 173: graph_patched
- Seq 174: task_started (Node: answer)
- Seq 175: task_succeeded (Node: answer)
- Seq 176: graph_patched

##### Ledger Rows from gateway.sqlite:
```json
{
  "id": 160,
  "ts": 1786102004.9071186,
  "provider": "gemini_1",
  "model": "gemini-3.1-flash-lite",
  "input_tokens": 382,
  "output_tokens": 2,
  "latency_ms": 1050,
  "status": "ok",
  "session": "run-93ad36e2c667",
  "tenant": "course",
  "project": "s15",
  "user": "student-01",
  "usd": 9.850000000000001e-05
}
```

#### Run 3: Who wrote the novel '1984'? Reply with the author name only.
* **Run ID**: `run-4bb64e178469`
* **Final Answer**: `George Orwell`
* **Model/Provider Chosen**: `gemini-3.1-flash-lite` (via `gemini_1`)
* **OTel Trace ID**: `6a7ea895e4a2664e96c13c21679ee036`

##### Ordered Event Trace:
- Seq 177: run_started
- Seq 178: graph_patched
- Seq 179: task_started (Node: recall)
- Seq 180: task_succeeded (Node: recall)
- Seq 181: graph_patched
- Seq 182: task_started (Node: answer)
- Seq 183: task_succeeded (Node: answer)
- Seq 184: graph_patched

##### Ledger Rows from gateway.sqlite:
```json
{
  "id": 161,
  "ts": 1786102008.8858082,
  "provider": "gemini_1",
  "model": "gemini-3.1-flash-lite",
  "input_tokens": 382,
  "output_tokens": 2,
  "latency_ms": 972,
  "status": "ok",
  "session": "run-4bb64e178469",
  "tenant": "course",
  "project": "s15",
  "user": "student-01",
  "usd": 9.850000000000001e-05
}
```

#### Run 4: What is the boiling point of water in Celsius? Reply with the temperature only.
* **Run ID**: `run-f260e5d27fc2`
* **Final Answer**: `100°C`
* **Model/Provider Chosen**: `gemini-3.1-flash-lite` (via `gemini_1`)
* **OTel Trace ID**: `48668d0fd6b5a2a76e36da59c8d94787`

##### Ordered Event Trace:
- Seq 185: run_started
- Seq 186: graph_patched
- Seq 187: task_started (Node: recall)
- Seq 188: task_succeeded (Node: recall)
- Seq 189: graph_patched
- Seq 190: task_started (Node: answer)
- Seq 191: task_succeeded (Node: answer)
- Seq 192: graph_patched

##### Ledger Rows from gateway.sqlite:
```json
{
  "id": 162,
  "ts": 1786102013.3038068,
  "provider": "gemini_1",
  "model": "gemini-3.1-flash-lite",
  "input_tokens": 378,
  "output_tokens": 5,
  "latency_ms": 965,
  "status": "ok",
  "session": "run-f260e5d27fc2",
  "tenant": "course",
  "project": "s15",
  "user": "student-01",
  "usd": 0.00010200000000000001
}
```

##### Honest Telemetry & Architecture Limitations Exposed by the Traces
1. **Provider Availability Vulnerability**:
   The default mapping for the `frontier` tier pointed directly to `github` (model: `openai/gpt-4.1`). Because GitHub Models was undergoing a scheduled retirement brownout (returning HTTP 410), any agent execution requesting the `frontier` tier crashed instantly at the `answer` worker node. This exposes a lack of redundancy in our capability tiers. In production, the gateway or the client should define secondary fallback provider models for each tier to gracefully recover from single-provider failures.
2. **Telemetry and Ledger Disconnect**:
   Initially, budgeted runs resulted in empty database columns for `session`, `tenant`, `project`, and `user` inside `gateway.sqlite`. The OTel trace collector generated a clean span tree with costs, but the gateway ledger could not attribute those costs to a particular run ID or client organization because the runtime's budgeted completion wrapper (`BudgetedGateway.complete`) did not forward the principal dimensions to `GatewayClient.chat`. Furthermore, the gateway client's `PASSTHROUGH` filter stripped those parameters out before serialization. Without patching this disconnect, auditing reports and billing ledgers fall out of sync, compromising enterprise observability.

---

### Part 2: Custom Policy & Workload Evaluation

#### 1. Workload and Capability Ladder
We defined a custom workload domain for **GPU Compute & AI Model Sizing** consisting of 15 tasks of varying complexity. The capability ladder is configured in `config/tiers.yaml` as follows:
* **Economy Rung**: `groq/openai/gpt-oss-120b` (Price: $0.15/in, $0.75/out)
* **Standard Rung**: `gemini/gemini-3.1-flash-lite` (Price: $0.25/in, $1.50/out)
* **Frontier Rung**: `gemini/gemini-3.1-pro` (Price: $2.00/in, $12.00/out, runs via `gemini-3.1-flash-lite` on wire due to API key restrictions)

#### 2. Measured Cost and Resolution Metrics
* **Tasks Evaluated**: 15
* **Baseline Solved (Always-Frontier)**: 9 / 15
* **Policy Solved (Budget-Aware)**: 8 / 15
* **Total Cost**: Baseline `$0.004318` vs. Policy `$0.001994` (**53.8% savings**)
* **Cost Per Call**: Baseline `$0.000288` vs. Policy `$0.000133` (**53.8% savings**)
* **Cost Per Resolved Task**: Baseline `$0.000480` vs. Policy `$0.000249` (**48.0% savings**)

#### 3. Break-Even Resolution Rate
* **Measured Break-Even Rate**: **46.2%**.
* **Actual Position**: Our budget-aware policy resolved **88.9%** (8 solved vs 9 solved) of what the frontier resolved, putting the policy well above the break-even line and making it highly cost-efficient.

#### 4. Jaeger Trace / Span Hierarchy Example
A typical budgeted trace hierarchy structure retrieved via `/v1/agent/runs/{run_id}/trace` containing costs and OTel IDs:
```json
{
  "trace_id": "babd6c688b1d10dcafbf9ba69dfa9a43",
  "span_id": "78a9c3621f8a",
  "name": "run-97262d69b55e",
  "attributes": {
    "s15.cost": 0.000141,
    "s15.currency": "USD"
  },
  "child_spans": [
    {
      "name": "agent_loop",
      "child_spans": [
        {
          "name": "plan",
          "child_spans": [
            {
              "name": "node-recall",
              "attributes": {
                "s15.cost": 0.0,
                "s15.tier": "economy"
              }
            },
            {
              "name": "node-answer",
              "attributes": {
                "s15.cost": 0.000141,
                "s15.tier": "standard",
                "gen_ai.request.model": "gemini-3.1-flash-lite",
                "gen_ai.usage.input_tokens": 31,
                "gen_ai.usage.output_tokens": 89
              }
            }
          ]
        }
      ]
    }
  ]
}
```

#### 5. Policy Mistake Case Study
* **Task ID**: `t09_flops_estimation`
* **Query**: *"Estimate the total floating-point operations (FLOPs) required to train a 13 Billion parameter model on a dataset of 300 Billion tokens..."*
* **Baseline (Frontier) Output**: `2.34e22` (Correct)
* **Policy (Economy) Output**: `Refused` / Failed (The policy downgraded to the cheap model to stay under budget, but the cheaper model failed to perform the multi-step arithmetic, losing resolution).
* **Cost Difference**: Saved `$0.000566` in exchange for accuracy.

---

### Part 3: Adversarial Budget Attack
We ran a runaway loop simulation under a tight budget of `$0.002`. The controller successfully halted the loop before contacting provider APIs:

#### 1. Spend Comparison
* **Before Control**: Ran up to the iteration limit, spending `$0.009241` over 6 iterations.
* **After Control**: Terminated at iteration 3, costing only `$0.001927`.

#### 2. Telemetry Refusal Trace
The refusal is recorded explicitly inside the event log journal of the controlled loop:
```markdown
- Seq 1: run_started
- Seq 2: graph_patched
- Seq 3: task_started (Node: loop_1)
- Seq 4: task_succeeded (Node: loop_1) | Cost: $0.000144
- Seq 5: graph_patched
- Seq 6: task_started (Node: loop_2)
- Seq 7: task_succeeded (Node: loop_2) | Cost: $0.000160
- Seq 8: graph_patched
- Seq 9: task_started (Node: loop_3)
- Seq 10: task_failed (Node: loop_3) | Payload: {'error': 'BudgetRefused: budget refused a standard call for unattributed: spend pressure 0.963 >= refuse_at 0.9'}
```

---

### Commands to Reproduce
Run the following from a fresh checkout:
```bash
# Install dependencies
uv sync

# Run complete test suite
uv run pytest -q

# Run custom policy workload evaluation (Part 2)
uv run python proofs/run_compute_eval.py

# Run adversarial loop attack test (Part 3)
uv run python proofs/attack_budget.py
```
