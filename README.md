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

## Part 2 — build a policy and measure it

The domain, the ladder and the policy were built specifically for this Part;
the `mixed.jsonl` set that ships with the repo is untouched.

### 1. The workload

**IT ticket triage** — given a short ticket description, return a JSON object
`{category, severity, first_step}`. Structured output, judgeable, deliberately
different from the essay-style mixed set.

Task file: [proofs/tasks/tickets.jsonl](proofs/tasks/tickets.jsonl) — 18 tasks,
labelled 6 trivial / 6 moderate / 6 hard. Difficulty in offline mode is the
hash bucket the shipped `SimulatedGateway` in
[proofs/p1_cost_per_task.py](proofs/p1_cost_per_task.py) derives from
`sha256(task_text)[:8] mod 3600 + 200`, chosen to line up with the Part 2
ladder cutoffs so each bucket has real separation between rungs. The
pick script that balanced the buckets is at
[proofs/_pick_tickets.py](proofs/_pick_tickets.py).

### 2. The ladder (`config/part2/tiers.yaml`)

| rung | model | max_tokens | projected worst-case admission |
|---|---|---:|---:|
| economy | `openai/gpt-oss-120b` (groq) | 1500 | **$0.001185** |
| standard | `gemini-3.1-flash-lite` (gemini) | 2600 | $0.003920 |
| frontier | `openai/gpt-4.1` (github) | 4096 | **$0.033568** |

Three different models on three different providers. Same models the shipped
ladder uses — this Part is about the *policy*, not about repointing rungs —
but `max_tokens` is right-sized for the short structured output of ticket
triage. **Projected spread top/bottom: 28.3×.**

### 3. The policy (`config/part2/budgets.yaml`)

Five deliberate changes vs the shipped policy, each documented in the file
next to the value:

- `default_budget: 0.05` — sits above frontier's $0.0336 projected
  admission so an always-frontier baseline actually admits on frontier and the
  comparison is honest.
- `downgrade_at: 0.40` (was 0.50) — a three-field JSON rarely earns
  frontier's price when standard would satisfy the same rubric.
- `headroom_fraction: 0.05` (was 0.02) — small output = calls that project
  close to the ceiling are more likely to actually land close to it.
- `max_calls_per_node: 3` (was 6) — a ticket-triage node that needs three
  tries is a signal to stop, not to retry.
- `reserve_fraction: 0.10` (was 0.20) — no long upstream chain to protect
  the terminal node from.

Everything else — the OWASP-shaped denial-of-wallet defence at
`max_calls_per_run: 60`, the `refuse_at: 0.90` line — is kept, and Part 3
below is the check that they still hold.

Reuse rule is the same as the rest of the repo: nothing in `s15code` names a
tier, a model, a price or a threshold. Editing the four files under
`config/part2/` is the entire policy change.

### 4. Measured against the always-frontier baseline

Command:

```powershell
$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
uv run python proofs/p1_cost_per_task.py `
    --tasks proofs/tasks/tickets.jsonl --config-dir config/part2 `
    --principal "you/part2" --offline --label part2
```

Numbers straight out of
[proofs/out/p1_cost_per_task_part2.json](proofs/out/p1_cost_per_task_part2.json):

| strategy | opens on | resolved | calls | cost / call | **cost / resolved task** | total spend |
|---|---|---:|---:|---:|---:|---:|
| **A** always-frontier | frontier | 18 / 18 (100%) | 18 | $0.014731 | **$0.014731** | $0.26516 |
| **B** always-cheapest, ≤2 tries | economy | 6 / 18 (33%) | 30 | $0.001005 | **$0.005026** | $0.03015 |
| **C** budget-aware cascade *(this policy)* | economy → standard → frontier | 18 / 18 (100%) | 36 | $0.005501 | **$0.011003** | $0.19805 |

- **C beats A by 25.3% on cost per resolved task** at the same 100%
  resolution rate.  Total workload savings: **$0.06711** on 18 tickets.
- B's cost per resolved is even lower, but B only resolves 33% — the other 12
  tickets are unaddressed.  Any downstream cost of handling those (a human, a
  retry queue) is not in this number.

Judge is the shipped rubric judge in [s15code/evals/judge.py](s15code/evals/judge.py)
against the panel in [config/part2/evals.yaml](config/part2/evals.yaml) —
`cerebras/zai-glm-4.7` and `openrouter/nvidia/nemotron-3-super-120b-a12b:free`,
disjoint from every rung of the answering ladder, so no answer is graded by its
own model. All 84 answering calls were metered; all 84 rows in the ledger.

### 5. Break-even, and where this policy sits

The break-even resolution rate for cheap-with-retries B vs frontier-once A is
the resolution rate at which B's total spend across attempts equals A's spend
across one attempt, per resolved task. From the measured ladder spread and 2
attempts allowed:

$$
r_{\text{break-even}} \approx \frac{\text{attempts}_B \cdot (\text{cost/call}_B)}{\text{cost/call}_A} = \frac{2 \cdot \$0.001005}{\$0.014731} \approx 0.128
$$

**Break-even = 12.8% resolution.**  My workload's B strategy sits at **33%**,
comfortably above break-even — B's cheap-and-give-up policy really is cheaper
per resolved task on the tasks it resolves.  It just leaves two thirds of the
workload on the floor.

My policy C sits at **100%** resolution, so it is nowhere near a break-even
line — the interesting question for C is not "is it worth it vs B" (it is) but
"is it worth its extra spend vs A" (yes, 25.3% cheaper per resolved).

### 6. One task the policy chose wrongly, and what it cost

[proofs/out/p1_cost_per_task_part2.json](proofs/out/p1_cost_per_task_part2.json)
shows the per-task numbers. The clearest wrong case:

**tk13_share_drive** (hard bucket) — same task, same resolved verdict:

| strategy | attempts on this task | cost |
|---|---|---:|
| A always-frontier | 1 (`frontier`) | **$0.02219** |
| C budget-aware *(mine)* | 3 (`economy` → `standard` → `frontier`) | **$0.02726** |

C paid **$0.00507 more per resolved (22.8% surcharge)** for the identical
outcome, because it burned two low-tier calls before landing on the tier that
could actually resolve the task. That is the *tax C's cascade pays for
optimism when a hard task looks like a trivial one*.

Across all six hard tickets the pattern repeats:

- A on hard: $0.14018 total ($0.02336 avg)
- C on hard: $0.17062 total ($0.02844 avg)
- C paid **$0.03044 more than A on the hard third of the workload**

That loss is compensated by C's savings on the trivial third
(economy resolves them at ~3% of frontier's price, saving ~$0.086) — so C
still wins overall.  But if the workload shifted to majority-hard, this
policy would lose to A.  A production version would need a light classifier
in front of the ladder — even a fifty-line rule set — to skip economy for
tickets it can already tell are hard.

### 7. One run's Jaeger trace with per-span cost

Script: [proofs/capture_part2_trace.py](proofs/capture_part2_trace.py). One
hard ticket (`tk13_share_drive`) run under `config/part2` with a
`--budget 0.05` ceiling; spans exported to the local Jaeger, then fetched
back by id:

- **Jaeger trace id**: `96fac5e7b2e0e3dd912292062959c625`
- **Jaeger view URL**: `http://127.0.0.1:16686/trace/96fac5e7b2e0e3dd912292062959c625`
- **Backend returned 10 spans** with the expected mix of `run`,
  `agent_loop`, `plan`, `node`, `provider_call`.
- The `chat openai/gpt-4.1` provider-call span carries
  `gen_ai.usage.input_tokens=289`, `gen_ai.usage.output_tokens=4000`,
  `gen_ai.request.model=openai/gpt-4.1`, `gen_ai.provider.name=offline_1`,
  and `s15.cost=0.03257800`.
- Sum of `s15.cost` across every provider-call span = **$0.03257800**;
  ledger `spent` = **$0.03257800** — the trace's per-span costs sum exactly
  to the budget ledger.

Full dump: [proofs/out/part2_trace_with_costs.json](proofs/out/part2_trace_with_costs.json).

## Part 3 — attack your own budget

Adversarial test against the Part 2 policy: an unbounded planner earns one
more node from every outcome, forever, even after refusals. Command:

```powershell
$task = "Adversarial: an unbounded planner will earn one more node from every outcome, forever, even after refusals. Return JSON {category,severity,first_step}."
uv run python proofs/p3_denial_of_wallet.py `
    --task $task --budget 0.002 --config-dir config/part2 `
    --principal "you/part2-attacker" --offline --label part2
```

Numbers straight out of
[proofs/out/p3_denial_of_wallet_part2.json](proofs/out/p3_denial_of_wallet_part2.json):

| metric | before control | after control |
|---|---:|---:|
| Loop rounds executed | 200 (would be unbounded) | 200 (loop ran, controller answered) |
| Nodes planned | 200 | 200 |
| Nodes admitted | *(unbounded — every node* | **1** *(only one attempt fit under the ceiling)* |
| Refusals | *would be zero — no controller* | **199** |
| Spend | ~$11.33 extrapolated over 10 000 rounds | **$0.001133** — **0.01%** of that |
| Ceiling | none | **$0.002000** — spend stayed under |

All six invariants pass:

- **The ceiling held.** `$0.001133 ≤ $0.002000` — not one cent over.
- **Admitted calls bounded by `max_calls_per_run: 60`**, and in practice
  stopped much earlier by the projection check.
- **The loop kept asking and was refused.** 200 rounds, 1 admitted, 199
  refused.
- **A refusal is a visible graph failure**, not a silent truncation: 199
  nodes ended in state `failed` with `BudgetRefused` as the cause.
- **Every provider call that returned is metered.** Transport calls == ledger
  charges == 1.
- **The controller, not the loop, stopped the spend.** The runaway ran to its
  own 200-round test cap; it did not run out of budget in some other way.

Refusals are visible in the telemetry too. Every refused node produces a
`refusal_log` entry in the budget snapshot, of the shape:

```json
{
  "node_id": "planned_node_2",
  "reason": "cheapest tier economy projects 0.001185, run holds 0.000842 (headroom 0.000100)",
  "spent": 0.001133,
  "remaining": 0.000867,
  "pressure": 0.567,
  "action": "refuse",
  "requested_tier": "frontier",
  "tier": null,
  "model": null,
  "projected_cost": 0.001185,
  "allowance": 0.000842,
  "ladder_steps": 0
}
```

Every refusal is attributed (principal `you/part2-attacker`), reasoned
(which projection failed which threshold), and countable (the ledger's
`refusals` counter is exact). The span export carries the same attribution:
each refused node produces a `node` span with `s15.node.state=failed` and
a `s15.budget.decision=refuse` attribute.

## Reproducing everything from a fresh checkout

```powershell
git clone <this repo>
cd S15Code
uv sync
uv run pytest -q                                          # 277 passed, 2 known-flaky
cd glc_v4 ; uv run pytest -q ; cd ..                      # 447 passed, 9 skipped

# Part 1
$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:S15_OTEL_SERVICE_NAME="s15code-proofs"
uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl --principal "demo/you" --offline --label part1
uv run python proofs/p2_budget_holds.py     --task "A cyclist rides 30 km at 20 km/h and returns at 30 km/h; average speed?" --budget 0.02 --principal "demo/you" --offline --label part1
uv run python proofs/p3_denial_of_wallet.py --task "A cyclist rides 30 km at 20 km/h and returns at 30 km/h; average speed?" --budget 0.002 --principal "demo/you" --offline --label part1
uv run python proofs/p4_trace_export.py     --task "A cyclist rides 30 km at 20 km/h and returns at 30 km/h; average speed?" --budget 0.02 --principal "demo/you" --offline --otel-endpoint http://127.0.0.1:4318/v1/traces --label part1
uv run python proofs/p7_cross_model_ladder.py --task "A cyclist rides 30 km at 20 km/h and returns at 30 km/h; average speed?" --principal "demo/you" --offline --label part1
uv run python proofs/capture_four_runs.py

# Part 2
$env:S15_OTEL_SERVICE_NAME="s15code-part2"
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/tickets.jsonl --config-dir config/part2 --principal "you/part2" --offline --label part2
uv run python proofs/capture_part2_trace.py

# Part 3
$task = "Adversarial: an unbounded planner will earn one more node from every outcome, forever, even after refusals. Return JSON {category,severity,first_step}."
uv run python proofs/p3_denial_of_wallet.py --task $task --budget 0.002 --config-dir config/part2 --principal "you/part2-attacker" --offline --label part2
```

A local Jaeger listening on `4318` (OTLP HTTP) and `16686` (query UI) is
enough for the trace round-trip; without it the proofs still pass, only the
"a real trace lands in Jaeger" claim in `p4` is skipped rather than checked.
Set `S15_OTEL_EXPORTER_ENDPOINT` unset to silence the exporter entirely.

Every proof artefact lands under [proofs/out/](proofs/out/) and is safe to
commit — no secrets, no `.env`, no real user data ever leaves the box.
