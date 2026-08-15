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

# Evidence: a routing and budget policy for map/navigation reasoning

Contributed by **Ashwani Bindroo** for the Session 15 assignment. Everything below
was measured against a live `glc_v4` gateway and a live Jaeger, on 2026-08-15.
Every number has a JSON artefact behind it under [`evidence/`](evidence/), and
every artefact is written by a proof that exits non-zero when its checks fail.

**What is added to this repository**

| Path | What it is |
|---|---|
| `proofs/tasks/navigation.jsonl` | the workload: 18 map/navigation reasoning tasks |
| `config/navigation/` | the policy: ladder, prices, budget thresholds, judge rubric |
| `proofs/p0_calibration.py` | measures the ladder before any price is written down |
| `proofs/p8_adversarial.py` | four attacks on the policy, with spend before and refusal after |
| `proofs/p9_run_capture.py` | one run captured whole: the six Part 1 artefacts |
| `proofs/p10_judge_audit.py` | audits the judge against an answer key |
| `proofs/keys/navigation_key.jsonl` | that answer key — read by `p10` and nothing else |
| `proofs/render_trace.py`, `proofs/show_refusals.py` | read traces back **out of** Jaeger |
| `s15code/economics/` + `tests/test_attempt_ceilings.py` | attempt ceilings, which close a hole `p8` found |

## The workload

Eighteen map and navigation reasoning tasks: unit conversions, ETA arithmetic,
OSM tag semantics, turn-restriction interpretation, vehicle-dimension checks,
tile maths, isochrones, geofences, map-matching and route costing. Labelled
`trivial` (2), `easy` (5), `moderate` (6) and `hard` (5), where the label records
intent and nothing branches on it.

Three properties were deliberate:

- **Self-contained.** Every number a task needs is in the task. Nothing asks what
  the speed limit on a named road is, because that measures whether a model
  memorised a gazetteer, and the cheap rung would lose for a reason that has
  nothing to do with capability.
- **Firm expectations.** Each task carries a success criterion naming the value
  the answer must deliver, so the rubric's `meets_expectation` criterion has
  something to be strict about. Where a method genuinely admits spread — a
  hand-computed great-circle distance — the expectation states the tolerance.
- **Traps on purpose.** `nav05` sits half a degree inside a compass-sector
  boundary, `nav07` is the harmonic-mean trap, `nav16` separates two routes by
  eighteen cents, `nav09` turns on what a restriction relation does *not* forbid.

Writing the file was itself a lesson. Two tasks were wrong when first written and
were caught only by reading what the models actually answered: `nav05` asked for
an 8-point compass sector but expected **WSW**, which exists only on a 16-point
rose, and `nav03` called a 250 m radius a "gentle sweep at motorway speed" when
it is 3.1 m/s² of lateral acceleration. Both would have marked correct answers
wrong. An expectation is as capable of being wrong as an answer is.

## The ladder, and why it has two rungs

| rung | model | rate in/out $/Mtok | max_tokens | reasoning | worst-case projected call |
|---|---|---|---|---|---|
| `economy` | `gemini-3.1-flash-lite` | 0.25 / 1.50 | 512 | off | **$0.000823** |
| `frontier` | `gemini-3.5-flash` | 0.50 / 3.00 | 4096 | low | **$0.012398** |

**15.1× spread in projected cost, from a 2× spread in published rates** — the
rest is the output ceiling, since a 4096-token budget can cost eight times what a
512-token one can.

Two rungs, not three, and the reason was measured rather than assumed
([`evidence/part1/p0_calibration.json`](evidence/part1/p0_calibration.json)):

```
gemini-3.1-flash-lite   answers                                    ✓
gemini-3.5-flash        answers, 12/12                             ✓
gemini-3.7-flash        answers, but 3 of 12 calls return HTTP 503
gemini-3.1-flash        HTTP 404 — no such model exists at 3.1
gemini-3.1-pro-preview  HTTP 429 — free_tier_input_token_count exhausted
gemini-2.5-flash/-pro   HTTP 404 — retired
```

Everything reachable on this key is a `flash` or a `flash-lite`, and Google
publishes exactly two rates across them. Two distinct prices is two rungs. A
third could have been manufactured by putting another flash model on the ladder
at the same rate with a different token ceiling, and was not, because a rung
whose price separation comes entirely from `max_tokens` is the same-model ladder
this session already criticises, wearing a different model's name.

`gemini-3.7-flash` is newer, faster (6.8 s against 11.7 s) and identically
priced, and it is **not** the top rung because it is not *available* enough to be
a baseline: a 25% HTTP 503 rate on the rung every other number is compared
against does not measure a model's quality, it measures Google's capacity
planning. Availability chose this rung, not capability.

**What this ladder buys and what it does not.** It buys real price separation
across two genuinely different models, so a downgrade changes which model
answers. It does not buy provider diversity: one outage or one quota takes both
rungs down together, and `p7`'s "rungs are spread across providers" check fails
against it — correctly, and reported below rather than suppressed.

## The budget policy

Every threshold is in [`config/navigation/budgets.yaml`](config/navigation/budgets.yaml)
and each was chosen against the two projections above.

| setting | value | why |
|---|---|---|
| `default_budget` | $0.02 | must clear the frontier rung's $0.012398 worst case plus headroom, or the top rung is structurally unreachable. The true floor is $0.01265 |
| `reserve_fraction` | 0.25 | the answering node is the only one whose output a user sees; a well-funded pipeline that runs out of money at the answer has bought nothing |
| `downgrade_at` | 0.55 | |
| `refuse_at` | 0.85 | tightened from 0.90: these tasks have one right answer, so a half-funded run that limps to a wrong number has spent the money *and* failed |
| `max_calls_per_run` / `_node` | 24 / 3 | a navigation answer takes one call plus a validation pass; a 25th call is looping, not working |
| `max_attempts_per_run` / `_node` | 30 / 5 | **new** — see Part 3, these count attempts rather than charges |
| `role_tiers` | everything `economy` except `answer_with_evidence` and `coder_validator` | mechanical roles are not where a wrong answer comes from |

## Part 1 — reproducing the floor

**Both suites**

```
glc_v4     444 passed, 12 skipped     (docs say 445; one is environment-dependent)
S15Code    283 passed                 (277 shipped + 6 added with the attempt ceilings)
```

**The five proofs.** The shipped `config/` ladder answers on groq / gemini /
github, and this deployment has only a Gemini key, so the shipped ladder is run
`--offline` — the deterministic transport, with the real policy, ladder, journal
and span export — and the *navigation* ladder is run live against the gateway.
Both sets of artefacts are in `evidence/part1/`.

| proof | shipped config, offline | navigation config, live |
|---|---|---|
| `p1_cost_per_task` | ✅ all checks | ✅ see Part 2 |
| `p2_budget_holds` | ✅ | ✅ downgrade at a tight ceiling, refusal at an impossible one |
| `p3_denial_of_wallet` | ✅ | ✅ 4 calls admitted, then 196 refusals |
| `p4_trace_export` | ✅ (no collector needed) | ✅ trace fetched back from Jaeger by id |
| `p7_cross_model_ladder` | ✅ | ⚠️ **2 checks fail** — see below |

`p2` live, with ceilings derived from the ladder rather than written down:

```
declared     ceiling 0.02000000  spent 0.00035650  calls 1  refusals 0  tiers frontier
generous     ceiling 0.13224533  spent 0.00035650  calls 1  refusals 0  tiers frontier
tight        ceiling 0.00881400  spent 0.00013625  calls 1  downgrades 1  tiers economy
impossible   ceiling 0.00000082  spent 0.00000000  calls 0  refusals 1    tiers -
```

**`p7` fails two checks against this ladder, and both failures are true.**
"The rungs are spread across providers" fails because they are not — one key, one
provider. "The top rung measurably costs more than the bottom rung" fails because
on that particular task the frontier rung answered in 28 output tokens where
economy spent 339, so the dearer rung charged **less** ($0.000111 against
$0.000522, a 0.21× spread). A ladder is ordered by price per token; it is not
ordered by what a model chooses to say.

### Four runs, captured whole

Each run records the six artefacts Part 1 asks for. Full JSON in
`evidence/part1/p9_run_capture_run{1..4}.json`; span trees, rendered back out of
Jaeger, in `evidence/part1/jaeger_run{1..4}_span_tree.txt`.

| | run 1 | run 2 | run 3 | run 4 |
|---|---|---|---|---|
| **prompt** | truck 4.30 m under `maxheight=4.2` | two routes priced on time/toll/fuel | tiles at zoom 14 | conditional access window + unload |
| **budget** | $0.02 | $0.001 | $0.0004 | $0.02 |
| **requested tier** | frontier | frontier | frontier | frontier |
| **decision** | `proceed` | **`branch`** | **`refuse`** | `proceed` |
| **model served** | gemini-3.5-flash | gemini-3.1-flash-lite | — | gemini-3.5-flash |
| **ledger** | 255in/127out, $0.00050850 | 275in/322out, $0.00055175 | — | 276in/138out, $0.00055200 |
| **spent / ceiling** | 0.0005/0.02, pressure 0.025 | 0.00055/0.001, pressure 0.552 | **0.0/0.0004** | 0.00055/0.02, pressure 0.028 |
| **Jaeger trace** | `4d81f168733e6ebd6a23f96b4a9f56a3` | `03f6c7d4cdb014ce895d432732048c12` | `1cb82699efb9a145f511dbc60d0b08fe` | `bddb7d6c7e6e5129873ecf02697958aa` |
| **backend spans** | 10 | 10 | 9 | 10 |
| **answer** | "No — 4.30 m exceeds 4.2 m by 0.10 m" ✓ | Route B, with all three cost components ✓ | *(refused, no answer)* | "No; may enter 10:00; unloading finishes 10:25" ✓ |

Ordered event trace, identical in shape across the four runs (run 3 differs at
event 7):

```
1 run_started → 2 graph_patched → 3 task_started → 4 task_succeeded
→ 5 graph_patched → 6 task_started → 7 task_succeeded │ task_failed (run 3)
→ 8 graph_patched
```

Run 1's span hierarchy, read back out of Jaeger, with cost on the provider call:

```
run run-d7ae3df9ca2b                     [run]          12238.0 ms  $0.00050850
  └─ agent loop 2                        [agent_loop]   12238.0 ms
    └─ node answer                       [node]         12238.0 ms
      └─ chat gemini-3.5-flash           [provider_call] 12238.0 ms
                          gemini_1/gemini-3.5-flash  255in/127out  $0.00050850
    └─ plan                              [plan]             0.0 ms
  └─ agent loop 1                        [agent_loop]       2.0 ms
    └─ plan                              [plan]             0.0 ms
    └─ node recall                       [node]             1.0 ms
  └─ agent loop 3                        [agent_loop]       0.0 ms
    └─ plan                              [plan]             0.0 ms

sum of provider_call span costs                          $0.00050850
run span s15.cost (the ledger total)                     $0.00050850
they agree                                               yes
```

Run 3's refusal, also read back from the backend
([`evidence/part1/jaeger_run3_refusal.txt`](evidence/part1/jaeger_run3_refusal.txt)):

```
ERROR span node answer  [node]  BudgetRefused: budget refused a frontier call for answer:
                                cheapest tier economy projects 0.000858,
                                run holds 0.000400 (headroom 0.000008)
run span   s15.budget.refusals = 1 · s15.budget.spent = 0 · s15.budget.total = 0.0004
REFUSAL    node answer: cheapest tier economy projects 0.000858, run holds 0.000400
```

### One honest limitation the traces exposed

**The cost axis of these traces is measured. The time axis is fabricated
everywhere except on provider calls, and it is fabricated in a way that puts
events in an impossible order.**

Sorting run 1's spans by the start time Jaeger holds:

```
    0.0 ms  dur= 12238.0 ms  chat gemini-3.5-flash    [provider_call]
    0.0 ms  dur= 12238.0 ms  node answer              [node]
    0.0 ms  dur= 12238.0 ms  agent loop 2             [agent_loop]
    0.0 ms  dur= 12238.0 ms  run run-d7ae3df9ca2b     [run]
    1.0 ms  dur=     0.0 ms  plan                     [plan]
    1.0 ms  dur=     2.0 ms  agent loop 1             [agent_loop]
    2.0 ms  dur=     1.0 ms  node recall              [node]
    7.0 ms  dur=     0.0 ms  agent loop 3             [agent_loop]
```

**Agent loop 2 starts at 0.0 ms and agent loop 1 starts at 1.0 ms.** Loop 1 ran
first — it must have, it produced the plan that created loop 2's node — but the
trace says otherwise, and a reader doing latency analysis would conclude the
loops overlap.

The cause is in `s15code/telemetry/spans.py` and the code is candid about it:
*"The journal orders events but does not time them."* The exporter anchors a
synthetic clock to the first metered call's real `started_at` and then lays every
untimed span out at `SEQUENCE_TICK_SECONDS = 0.001` per journal event. So the
whole tree is pinned to the moment the *provider call* began, and everything that
truly happened before it is placed *after* it, one millisecond apart.

This does not affect a single cost number in this submission — costs come from
the ledger, and the spans reconcile with it exactly, four times out of four. It
does mean these traces cannot answer "where did the wall clock go", which is one
of the two questions people open Jaeger to ask. Anyone reading them for latency
is reading the exporter's arithmetic, not the run's behaviour.
