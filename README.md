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

## Part 1 — reproduce the floor

Run both suites and the five proofs, then capture four runs end-to-end. Results
from a local reproduction on 2026-08-05 are recorded below; every artefact this
section refers to lives under `proofs/out/`.

### Test suites

| suite | command | result |
|---|---|---|
| `glc_v4` | `cd glc_v4 && uv run pytest -q` | **447 passed, 9 skipped** (1m 55s) |
| `S15Code` | `cd .. && uv run pytest -q` | **277 passed, 2 failed** (1m 35s) |

The two S15Code failures are pre-existing and environment-specific rather than
regressions:

- `tests/test_budget_runtime.py::test_the_trace_route_serves_the_span_tree`
  asserts `exported_over_the_wire is False`, but the runtime fires OTLP export
  best-effort on every completed run — with `S15_OTEL_EXPORTER_ENDPOINT` set
  (as it is for the proofs) the assertion is stale.
- `tests/test_runtime_regressions.py::test_birthday_creates_two_real_calendar_artifacts`
  does `Path(uri.removeprefix("file://"))` which on Windows leaves a leading `\`
  before the drive letter (`\C:\...`) — an invalid path, not a runtime bug.

### The five proofs

Local Jaeger was running (`OTLP` on `:4318`, UI on `:16686`); the gateway was
not, so every proof used the deterministic offline transport. Commands and
verdicts:

```bash
$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:S15_OTEL_SERVICE_NAME="s15code-proofs"

uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl --principal "demo/you" --offline --label part1
uv run python proofs/p2_budget_holds.py     --task "<the cyclist task>"      --budget 0.02 --principal "demo/you" --offline --label part1
uv run python proofs/p3_denial_of_wallet.py --task "<the cyclist task>"      --budget 0.002 --principal "demo/you" --offline --label part1
uv run python proofs/p4_trace_export.py     --task "<the cyclist task>"      --budget 0.02 --principal "demo/you" --offline \
    --otel-endpoint http://127.0.0.1:4318/v1/traces --label part1
uv run python proofs/p7_cross_model_ladder.py --task "<the cyclist task>"    --principal "demo/you" --offline --label part1
```

| proof | verdict | headline number |
|---|---|---|
| `p1_cost_per_task_part1.json` | **PASS**, signature failure mode NOT observed | B resolved 25% of tasks; the trap begins below 8.6% (derived) |
| `p2_budget_holds_part1.json` | **PASS** across declared/generous/tight/impossible | declared $0.02 → spent $0.00161 on `standard` (1 downgrade) |
| `p3_denial_of_wallet_part1.json` | **PASS** — the controller, not the loop, stopped the spend | 200 rounds, 2 admitted, 198 refused, spend capped at $0.00194 ≤ $0.002 |
| `p4_trace_export_part1.json` | **11 of 13 PASS** — 2 backend round-trip claims fail (see limitation) | 10 spans emitted, 6 arrived; three parent pointers came back `None` |
| `p7_cross_model_ladder_part1.json` | **PASS** — 3 models across 3 providers, downgrade changes the model | 82.04× measured spread ($0.000391 → $0.032096) |

### Four runs, captured

Scripted via `proofs/capture_four_runs.py`, which uses the same runtime code
path as every proof and re-exports each run's journal so a Jaeger trace id is
returned. Full text and JSON are at `proofs/out/part1_four_runs.{txt,json}`.

The four scenarios span the four controller states the system can be in:
admit-at-requested-tier, downgrade under moderate pressure, downgrade under
tighter pressure, and refuse when no rung fits.

| # | scenario | budget | tier requested → served | model | Jaeger trace id | ledger |
|---|---|---:|---|---|---|---|
| 1 | `generous_frontier` | $1.00 | frontier → **frontier** | `openai/gpt-4.1` | `73e31e8c613d6fac4b8200b79e7118d7` (10 spans in backend) | 1 charge, 285 in / 4000 out, **$0.03257** |
| 2 | `declared_downgrade` | $0.02 | frontier → **standard** (downgrade) | `gemini-3.1-flash-lite` | `4367b0661b6ab0590977703dc6cd19e6` (10 spans in backend) | 1 charge, 307 in / 1024 out, **$0.00161** |
| 3 | `tight_downgrade` | $0.005 | frontier → **standard** (downgrade) | `gemini-3.1-flash-lite` | `210760dcb1a858d24d6ec612e344671c` (10 spans in backend) | 1 charge, 307 in / 1024 out, **$0.00161** |
| 4 | `impossible_refuse` | $0.00001 | frontier → — (refused) | — | `ec85c920745091a921968bdf7ee4a52b` (9 spans in backend) | 0 charges, 1 refusal: *"cheapest tier economy projects $0.000442, run holds $0.000010"* |

Every run walked the same journal shape —
`run_started → graph_patched(+recall) → task_started(recall) → task_succeeded(recall)
→ graph_patched(+answer) → task_started(answer) → task_{succeeded,failed}(answer)
→ graph_patched(finish)` — differing only in the terminal event for the answer node
(succeeded for runs 1–3, failed for run 4). Final answers are the offline
transport's deterministic payload; the plan, tier decision, ledger arithmetic and
span export are the real implementation.

### One honest limitation the traces exposed

**Parent pointers do not survive the wire, and not every span the process emits
arrives.** From `p4_trace_export_part1.json`:

- The proof emitted **10** spans (`run + agent_loop + plan + node + provider_call + …`),
  but Jaeger returned only **6** for that trace id after a 12×1 s poll window.
  `SimpleSpanProcessor` plus Jaeger's async OTLP ingestion drop or delay a
  nontrivial fraction under load.
- Of the 6 that arrived, **three came back with parent `None`**: `agent loop 1`,
  `plan`, and `node answer` appear as *roots* in the backend rather than pointing
  to their real parents. The clean hierarchy check that passes in-process
  (`run → agent_loop → plan → node → provider_call`) does **not** survive into
  Jaeger's view — a reviewer opening the trace in the UI sees a fragmented
  forest, not the tree p4 asserted against the in-memory export.

The critical guarantees hold anyway: `gen_ai.usage.*` and `s15.cost` land intact
on the provider-call span, and no prompt or completion text leaks
(`capture_content` stays off). But the "clean waterfall in Jaeger" claim is
aspirational — what actually lands is the receipts, not the shape.
