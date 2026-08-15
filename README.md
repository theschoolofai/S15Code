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
## Part 2 — what the policy costs, and what it costs you

### The judge, and how wrong it is

Cost per resolved task divides money by a number two language models produced.
That makes the panel the load-bearing component of the whole measurement: if it
is 20% too generous, every figure below is 20% too low and no amount of careful
metering upstream repairs it. So the rubric is stated, and then it is tested.

**The rubric** ([`config/navigation/evals.yaml`](config/navigation/evals.yaml)),
five criteria scored 0–4:

| criterion | weight | why this weight |
|---|---|---|
| `addresses_task` | 1.0 | answers the question asked, not a neighbouring one |
| `specific` | 1.0 | commits to a result instead of describing how one might proceed |
| `consistent` | 1.0 | no step contradicting another, nothing truncated |
| `complete` | 1.0 | every part of a multi-part task covered |
| `meets_expectation` | **3.0** | delivers the value the task's success criterion names |

An answer resolves its task at a weighted normalised score of **≥ 0.75** with
**no criterion below 0.6**.

Two departures from the shipped rubric, both workload-specific. `meets_expectation`
is weighted 3.0 rather than 2.0, because on a workload of unit conversions and
ETA arithmetic *whether the number is right* is the task; presentation is worth
something but not three quarters of the score. At 3.0 the four generic criteria
can no longer outvote the one that checks the value. And `min_criterion` rises
from 0.5 to 0.6, which on a 0–4 scale is the difference between "2 out of 4
survives" and "every criterion must reach 3" — an answer that half-satisfies the
success criterion has not half-resolved the task, it has produced a wrong number
with good manners.

**The panel** is two models, both disjoint from every rung of the answering
ladder, so no answer is ever graded by the model that wrote it:

| member | model | why |
|---|---|---|
| `judge_local` | `phi4:latest` on local Ollama | genuinely independent — different lab, different weights, no shared training run with anything on the ladder — and unmetered, so quota never decides which tasks get judged |
| `judge_hosted` | `gemini-3-flash-preview` | strong enough to work the task out itself; shares a **lab** with the ladder, which is disclosed rather than argued away |

### Auditing the judge against an answer key

`proofs/p10_judge_audit.py` collects a real answer at each rung for all 18 tasks,
then labels every answer **twice**: mechanically, from
[`proofs/keys/navigation_key.jsonl`](proofs/keys/navigation_key.jsonl), which
knows the right answer and nothing else about the task; and by the panel, with
exactly the rubric `p1` uses. The key is read by this proof and by nothing else —
no routing decision consults it, and `p1` never sees it.

```
answers graded                                36 of 36 (0 judge failures)
panel agrees with the key                     36/36 = 100.0%
FALSE RESOLVE (panel passed a wrong answer)   0
FALSE UNRESOLVE (panel failed a right answer) 0
judge_local  vs key                           33/33 = 100.0%
judge_hosted vs key                           32/32 = 100.0%
panel members disagreed with each other       0/36
```

Both directions matter and both are zero. A false resolve inflates the
resolution rate and makes cost per resolved task look **too low** — the
flattering error. A false unresolve does the opposite and makes a cascade look
more necessary than it is. On this workload the panel committed neither.

Three caveats I would not want a reader to discover for themselves. This is 36
answers on one task family, not a general claim that a 14B local model is a
reliable judge. The tasks have crisp right answers, which is the easiest possible
grading job — the panel was never asked to judge an essay. And `judge_local` and
`judge_hosted` agreeing 36 times out of 36 means this panel's disagreement rate
is **unmeasured rather than low**: nothing here tells you what happens when they
diverge, because on this set they never did.

### Cost per call, and cost per resolved task

**How these numbers were obtained, and what that costs them.** `p1` — the proof
that runs all three strategies end to end — did not finish. Part way through, the
Gemini free tier's **daily** quota ran out on every `flash` model: HTTP 429 on the
frontier rung while `gemini-3.1-flash-lite` still answered. A daily quota is not
something pacing or retries get around, and a single-provider ladder has no
second provider to fail over to — which is exactly the cost of the constraint
declared at the top of `tiers.yaml`, arriving as a bill rather than a caveat.

What survived is a complete set of **measured** rows. Before the wall, `p10` had
collected for all 18 tasks at **both** rungs: a real answer, its real ledger cost,
and a panel verdict — 36 metered calls, judged.
[`p11_strategy_costs.py`](proofs/p11_strategy_costs.py) composes the three
strategies out of those measured rows.

| | composed from |
|---|---|
| **A** always-frontier | each task's measured frontier row, once — **all measured** |
| **B** always-cheapest | each task's measured economy row, retried up to 3× when unresolved — **measured, plus one assumption** |
| **C** budget-aware | each task's measured economy row, escalated to its measured frontier row when unresolved — **all measured** |

B is the only one needing an assumption: a retry was never actually made, so its
retry cost rests on the claim that re-asking the same model the same question at
temperature 0 returns the same answer — billing again and buying nothing. That
claim is **measured, not assumed**: `p11` re-asks the economy rung and reports
whether the answers come back byte-identical. **3 of 3 were identical.**

```
A always_frontier   spend $0.01186400  calls 18  cost/call $0.00065911  resolved 14/18 (78%)  cost/resolved $0.00084743
B always_cheapest   spend $0.01017000  calls 22  cost/call $0.00046227  resolved 16/18 (89%)  cost/resolved $0.00063563
C budget_aware      spend $0.00823650  calls 20  cost/call $0.00041182  resolved 16/18 (89%)  cost/resolved $0.00051478
```

**Against the always-frontier baseline:**

| | cost per call | cost per resolved task |
|---|---|---|
| B always-cheapest | **−29.9%** | **−25.0%** |
| C budget-aware | **−37.5%** | **−39.3%** |

**The headline is not the saving.** It is that **the cheap rung resolved more
tasks than the dear one** — 16 of 18 against 14 of 18. That is the session's
fourth lesson landing on my own ladder: price did not order these two models by
competence at my workload.

And it is only *mostly* true, which matters. Three of the frontier rung's four
failures were **HTTP 429/503 on the call itself**, not wrong answers. Counting
only calls that returned, frontier resolved **14 of 15 (93%)** against economy's
**16 of 18 (89%)** with zero failures. So:

- **per successful call**, the frontier model is genuinely the better model;
- **as delivered**, it resolved fewer tasks, because a fifth of its calls never
  came back;
- and it costs **1.85×** the cheap rung per successful call ($0.00079093 against
  $0.00042722).

An availability failure and a capability failure are indistinguishable in a
resolution rate, and only one of them is the model's fault. Both are the user's
problem.

### The break-even resolution rate

From the measured spread, using the first-order model in
`p1.break_even_resolution_rate` — the cheap rung spends one call on a task it
resolves and all `k` attempts on one it never resolves, so at resolution rate `r`
its cost per resolved task is `c·(r + (1−r)·k) / r`, and setting that equal to the
dear strategy's measured `C` gives

```
r* = k / (C/c − 1 + k)
   = 3 / ($0.00084743/$0.00046227 − 1 + 3)
   = 78.3%
```

**Break-even: 78.3%. The cheap rung measured 88.9%. Headroom: +10.6 points.**

Below 78.3% the cheap rung's retries would cost more per *resolved* task than
simply buying the frontier rung every time, while still looking cheaper per call
— the signature failure mode. This ladder sits above that line, but not by much,
and the margin is thin *because the ladder is short*: a 1.85× measured spread
means the cheap rung has to be nearly as good to be worth it. On the shipped 79×
ladder the break-even rate would be near 3%, and almost any cheap rung would pay.

**When this stops being worth it:** if the cheap rung's resolution rate on this
workload falls below **78.3%** — three more failures out of eighteen — the
cascade stops earning its keep and always-frontier becomes the cheaper policy per
resolved task. That is the number to re-measure when either model changes.

### The case my policy got wrong

**C escalated twice, paid for both, and rescued neither.**

| task | economy | escalated to frontier | outcome |
|---|---|---|---|
| `nav12` isochrone | FAIL, $0.00044475 | $0.00000000 — **the frontier call 429'd** | still unresolved |
| `nav14` haversine | FAIL, $0.00079525 | $0.00054650 | **still unresolved** — wrong at both rungs |

Escalation spent **$0.00054650 extra and resolved zero additional tasks.**

The sharper way to say it: the correct policy on this workload is **"ask the
cheap rung once and stop."** That costs $0.00769000 for 16 resolved tasks =
**$0.00048063 per resolved task**. My cascade costs **$0.00051478** — so my
budget-aware policy is **7.1% worse than not escalating at all**, while beating
always-frontier by 39%.

It is wrong for a specific, diagnosable reason: escalation only pays when the
dearer rung can solve what the cheaper one could not, and on this task set there
is **no such task**. `nav14` defeats both rungs; `nav12` would have needed a
frontier call that never returned. A cascade is a bet that the rungs fail on
*different* tasks, and here their failures are nested, not disjoint. The fix is
not a better ladder — it is to escalate only on task classes where the rungs have
been *measured* to diverge, which for this workload is none of them yet.

## Part 3 — attacking my own budget

[`proofs/p8_adversarial.py`](proofs/p8_adversarial.py) runs four attacks and
reports, for each, the spend **before** the control and the outcome **after** it.
A fifth is documented below but is not in `p8`, because it does not attack the
controller — it walks past it.

Artefact: [`evidence/part3/p8_adversarial_navigation.json`](evidence/part3/p8_adversarial_navigation.json).

### D. A provider that fails after consuming tokens — the one that got through

This is the attack worth reading, because the controller did **not** stop it and
finding that out is the entire point of writing an attack rather than a demo.

The ledger charges from the token counts in a **response**. A provider that
generates the answer, burns the tokens and *then* drops the connection returns
nothing to price — so it is never charged. And `max_calls_per_run` /
`max_calls_per_node` cannot see it either, because those count **charges**, and a
failed call never becomes one. A provider failing this way can be handed work
forever with no counter moving.

Measured, same attack twice, same provider, same rounds:

| | calls reaching the provider | real tokens burned | unbilled spend | ledger charges | refusals |
|---|---|---|---|---|---|
| **BEFORE** (attempt ceilings off) | 6 of 8 | 252 in / 330 out | **$0.00055800** | 0 | **0 — nothing stopped it** |
| **AFTER** (attempt ceilings on) | 5 | — | $0.00046500 | 0 | **3** |

The refusal, in the controller's own words:

```
node attempt ceiling reached (5/5); 0 of those attempts were billable
```

**The fix.** `RunBudget.record_attempt()` is now called *before* the transport is
touched, and `max_attempts_per_run` / `max_attempts_per_node` bound it. Both
default to `0` (disabled), so no configuration that predates this changes
behaviour. `tests/test_attempt_ceilings.py` pins the fix **and pins the bug** —
one test asserts the call ceilings *fail* to see an unbillable call, so a future
change that starts charging for failed calls has to come and argue with a red
test rather than quietly closing the finding.

The ledger still refuses to *charge* for these calls, and that is deliberate: a
charge needs a response to price, and inventing one would corrupt the meter to
flatter the alarm. What changed is that the exposure is **bounded** instead of
invisible — worst case `30 attempts × $0.012398 = $0.371940`, a number that fits
in a risk register.

### A. A runaway loop

60 rounds of a loop that earns another node from every outcome, against the
`nav/s15/adversary` principal (ceiling $0.002):

```
after the control:  60 rounds, 0 admitted, 30 refused, spent $0.00000000 of $0.00200000
```

The ceiling held at **zero spend**, and what stopped it was the *attempt* ceiling
from attack D — 30 attempts, then refusal — which is the new control doing work
it was not written for. Uncontrolled, the same 60 rounds at the frontier rung's
measured $0.00079093 per successful call would have cost about **$0.047**.

`p8`'s own uncontrolled sample recorded **0 successful calls** on this run
because the daily quota was already spent, so its extrapolation is $0.00 and that
check fails. The projection above is therefore taken from `p11`'s measured
per-call price rather than from a sample `p8` could not collect — stated here
rather than papered over.

### B. Demanding a tier the principal cannot afford

Both ceilings are derived from the ladder, not written down, and the outcome is
read from the controller's **decision** rather than from whether a provider
happened to answer — what is on trial is the policy, not the provider's uptime.

| ceiling | expected | decided | spent |
|---|---|---|---|
| $0.00468133 (above economy, below frontier) | downgrade | **`downgrade` → economy** | $0.00 |
| $0.00041150 (below every rung) | refuse | **`refuse`** | $0.00 |

### C. Forcing the cascade up its whole ladder — blocked, and saying so

This attack drives one node to escalate until it runs out of ladder and then out
of per-node calls. It **did not complete**: the economy rung was served once, and
all four frontier attempts returned HTTP 429/503 without reaching a model.

Its check fails with the reason attached — *"4 attempts never reached a provider,
so this measures provider availability rather than the policy"*. A failing check
that names why it failed is worth more than a passing check that quietly measured
the wrong thing. It needs a quota window to run, and the command is unchanged.

### E. The typo that disables the ceiling entirely

No loop, no cascade, no exhausted provider. One wrong field name.

| request field | result |
|---|---|
| `"budget_usd": 0.02` — **as the session's run instructions write it** | **no `budget` block at all; the run executed unbudgeted** |
| `"budget": 0.02` — as `RunBody` declares it | ceiling $0.02, one charge of $0.00090400, principal `course/s15/student-01` |

`RunBody` in `s15code/routes.py` declares `budget`, and the model does not set
`extra="forbid"`, so pydantic silently discards the unknown key and the run
proceeds with no ceiling while the caller believes one was set. Every control in
this submission lives downstream of a budget object that, on this path, was never
created.

The one-line fix is `model_config = ConfigDict(extra="forbid")` on `RunBody`,
turning a silent unbudgeted run into a 422. It is **not** applied in this pull
request because it changes the public API's error behaviour for every existing
caller — the maintainer's call, not mine. Evidence:
[`evidence/part3/http_budget_usd.json`](evidence/part3/http_budget_usd.json) and
[`http_budget_with_charge.json`](evidence/part3/http_budget_with_charge.json).

### The refusals are visible in the telemetry

Not in the exporter's own copy — read back **out of Jaeger**:

```
$ uv run python proofs/show_refusals.py 1cb82699efb9a145f511dbc60d0b08fe

ERROR span node answer  [node]  BudgetRefused: budget refused a frontier call for answer:
                                cheapest tier economy projects 0.000858,
                                run holds 0.000400 (headroom 0.000008)
run span   s15.budget.refusals = 1 · s15.budget.spent = 0 · s15.budget.total = 0.0004
REFUSAL    node answer: cheapest tier economy projects 0.000858, run holds 0.000400
             spent 0, remaining 0.0004

1 budget.refused event(s) present in the backend's copy of this trace
```

A refusal appears three ways, because a refused call never becomes a provider
span: as `s15.budget.refusals` on the run span, as a `budget.refused` **span
event** carrying the node and the controller's reason, and as **status ERROR** on
the node span — a visible graph failure, not a silent truncation. In `p8`'s
runaway loop, all **30** refusals were present as span events.
## Reproducing all of it from a fresh checkout

Four terminals. No secret, `.env` or user data appears in this pull request.

### 0. Three upstream defects you will hit first

All three were found by running the documented instructions, and each one fails
in a way that looks like something else.

**a. The Jaeger image pin does not resolve.** `glc_v4/docker-compose.observability.yml`
pins `jaegertracing/all-in-one:2.20.0`. `all-in-one` is the **v1** image and has
no 2.x tags at all; Jaeger v2 ships as `jaegertracing/jaeger`. The version is
real, the repository is not.

```bash
sed -i '' 's|jaegertracing/all-in-one:2.20.0|jaegertracing/jaeger:2.20.0|' \
  glc_v4/docker-compose.observability.yml
```

**b. `OPENROUTER_API_KEY` is read under a different name.** `glc/providers.py`
reads `OPEN_ROUTER_API_KEY`, with an inner underscore, which appears nowhere in
`.env.example`, the README or the docs, and is inconsistent with `GROQ_API_KEY`,
`CEREBRAS_API_KEY` and `NVIDIA_API_KEY` beside it. A correctly named key loads
fine and the provider is silently never built — `/v1/status` simply does not list
it. Only relevant if you route through OpenRouter; the panel below does not.

**c. `budget_usd` silently disables the ceiling.** The run instructions post
`{"prompt": ..., "budget_usd": 0.02, ...}`, but `RunBody` in `s15code/routes.py`
declares the field as **`budget`**, and the model does not set `extra="forbid"`.
Pydantic drops the unknown key, `budget` stays `None`, and the run executes with
no ceiling while the caller believes one was set. Measured both ways in
[`evidence/part3/`](evidence/part3/):

| request field | result |
|---|---|
| `"budget_usd": 0.02` | **no `budget` block at all — the run was unbudgeted** |
| `"budget": 0.02` | ceiling $0.02, one charge of $0.00090400, principal attributed |

### 1. Jaeger

```bash
git clone https://github.com/theschoolofai/glc_v4.git
git clone https://github.com/ashwanibindroo-personal/S15Code.git
# apply fix (a) above, then:
docker compose -f glc_v4/docker-compose.observability.yml up -d
curl -s localhost:16686/api/services      # {"data":[],...} means it is up
```

### 2. The local judge

The judge panel is half local, so nothing about grading depends on a quota.

```bash
ollama serve &
ollama pull phi4          # 9.1 GB
```

### 3. The gateway

`glc_v4/.env` needs one Gemini key. `LLM_ORDER` and `OLLAMA_MODEL` are passed on
the command line here so the checked-out `.env` needs no editing.

```bash
cd glc_v4 && uv sync
LLM_ORDER=gemini,ollama OLLAMA_MODEL=phi4:latest OLLAMA_URL=http://localhost:11434 \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 OTEL_SERVICE_NAME=glc-v4-gateway \
  uv run glc serve

curl -s 127.0.0.1:8111/v1/status | jq '.order'   # ["gemini","ollama"]
```

### 4. The suites

```bash
cd glc_v4   && uv run pytest -q     # 444 passed, 12 skipped
cd S15Code  && uv sync && uv run pytest -q && uv run ruff check .
                                    # 283 passed
```

### 5. Calibrate before spending

Run this first. It refuses to let anything else depend on a ladder it has not
verified, and if a model id has been retired or a free-tier quota exhausted since
this was written, this is where you find out — not forty minutes into `p1`.

```bash
cd S15Code
export S15_OTEL_EXPORTER_ENDPOINT=http://localhost:4318/v1/traces
export S15_OTEL_SERVICE_NAME=s15code-agent
uv run python proofs/p0_calibration.py --config-dir config/navigation
```

### 6. The five proofs, live, on the navigation policy

```bash
uv run python proofs/p2_budget_holds.py     --config-dir config/navigation --budget 0.02 \
  --principal nav/s15/p2 --task "A truck 4.30 m tall is routed under a bridge tagged maxheight=4.2. May it pass?"
uv run python proofs/p3_denial_of_wallet.py --config-dir config/navigation --budget 0.001 \
  --principal nav/s15/p3 --label exhaust --task "Keep refining the ETA for a 42 km leg at 90 km/h until it is perfect."
uv run python proofs/p4_trace_export.py     --config-dir config/navigation --budget 0.02 \
  --principal nav/s15/p4 --otel-endpoint http://localhost:4318/v1/traces --task "<any task>"
uv run python proofs/p7_cross_model_ladder.py --config-dir config/navigation --principal nav/s15/p7 --task "<any task>"
```

`p3` is run at $0.001 rather than a rounder number for a reason worth keeping: at
$0.01 the loop is stopped by Gemini's rate limiter before the budget ever binds —
187 of 200 calls returned transport errors and the ceiling was never reached, so
the proof failed for a reason that had nothing to do with budgets. A ceiling only
proves anything when it is the thing that binds first.

### 7. The four Part 1 runs

```bash
uv run python proofs/p9_run_capture.py --config-dir config/navigation \
  --otel-endpoint http://localhost:4318/v1/traces \
  --budget 0.02   --principal nav/s15/run1 --label run1 --task "<truck under a bridge>"
uv run python proofs/p9_run_capture.py ... --budget 0.001  --label run2   # forces a branch
uv run python proofs/p9_run_capture.py ... --budget 0.0004 --label run3   # forces a refusal
uv run python proofs/p9_run_capture.py ... --budget 0.02   --label run4

# then read the traces back OUT of Jaeger, by id
uv run python proofs/render_trace.py  <trace-id>
uv run python proofs/show_refusals.py <trace-id>
```

### 8. The measurement, the judge audit and the attacks

```bash
uv run python proofs/p10_judge_audit.py --config-dir config/navigation \
  --tasks proofs/tasks/navigation.jsonl --key proofs/keys/navigation_key.jsonl --label navigation
uv run python proofs/p1_cost_per_task.py --config-dir config/navigation \
  --tasks proofs/tasks/navigation.jsonl --principal nav/s15/p1 \
  --base-url http://127.0.0.1:8111 --label navigation
uv run python proofs/p8_adversarial.py --config-dir config/navigation \
  --base-url http://127.0.0.1:8111 --otel-endpoint http://localhost:4318/v1/traces
```

**`p1`'s default `--base-url` is port 8112, not 8111.** Pass it explicitly or the
proof quietly decides the gateway is unreachable and runs a **simulation** — it
says so, stamping `simulated: true` and withholding its finding, but it exits 0
either way and the numbers look plausible.

Every proof writes JSON to `proofs/out/` and exits non-zero when a check fails.
The artefacts behind every number in this section are committed under
[`evidence/`](evidence/), since `proofs/out/` is gitignored.

**Wall clock, so nobody thinks it has hung:** `p10` takes about 15 minutes and
`p1` about 40, and both are dominated by the local judge — `phi4` generates at
roughly 13 tokens/second on an M-series laptop. Its `max_tokens` is set to 400
rather than the hosted judge's 800 for exactly this reason; at 800 a full `p1`
run takes over three hours to produce the same verdicts.

## What this submission does not establish

Collected in one place, because a reader should not have to reconstruct them.

**The measurement is small.** Eighteen tasks, one workload, one day. Every
percentage here has a denominator of 18, so one task is 5.6 points. The
break-even margin of +10.6 points is **two tasks wide**.

**Part 2's strategies are composed, not run end to end.** Every cost, token count
and verdict is measured; the *sequencing* into A/B/C is arithmetic, and B's retry
cost additionally assumes a temperature-0 retry reproduces the answer — verified
3/3, which is a small sample. `p1` runs the real thing and is committed and
working; it needs a quota window, not a fix.

**The judge is 36 answers of evidence, not a general result.** The panel agreed
with the answer key 36/36 with zero errors in either direction, on tasks with
crisp right answers — the easiest grading job there is. Its two members also
agreed with each other 36/36, so this panel's **disagreement rate is unmeasured
rather than low**. And `p1`'s verdicts came from `judge_local` alone, because the
hosted member competed with the answer path for the same rate-limited keys.

**The ladder is single-provider, and that is what broke the study.** One quota
took every rung down together. The two rungs also differ by only 1.85× in
measured cost, so the break-even bar is high and the conclusions would not
transfer to a ladder with real price separation.

**Three attacks are complete; one is not.** D, A and B ran and are reported with
numbers. C could not reach a provider and says so in its own failing check.

**The traces cannot answer "where did the time go".** Only provider-call spans
carry real timings; everything above them sits on a 1 ms-per-event synthetic
clock, which places `agent loop 2` before `agent loop 1`.

**Nothing here was run twice.** No confidence intervals, no repeated trials. At
temperature 0 the answers are reproducible; the *verdicts*, the transport
failures and the wall-clock are not.
