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

## Evidence — a budget-aware policy for single-defect code review

Session 15 submission. One workload, one ladder, one budget policy, measured live
against `glc_v4` on 2026-08-02/03. Every figure below is from a recorded run;
reproduce commands are in [`proofs/REPRODUCE_PART2.md`](proofs/REPRODUCE_PART2.md).

**Headline: 70% cheaper per resolved task than always-frontier, at identical
quality — and the frontier rung earned nothing this measurement can attribute to
capability.**

---

### 1. The workload and the ladder

**Workload.** 15 single-defect code review tasks
([`proofs/tasks/codereview.jsonl`](proofs/tasks/codereview.jsonl)). Each snippet
contains exactly one planted defect; all are invented for this exercise. The
gradient is how much state a reader must hold: one line (easy, 5), two related
lines (moderate, 5), an execution-order or lifetime argument (hard, 5). Every
hard task names a **decoy** in its expectation — a fluent wrong answer — so a
confident near-miss scores zero rather than partial credit.

**Ladder** ([`config/part2/tiers.yaml`](config/part2/tiers.yaml)), each rung
called before its rate was written down:

| Rung | Provider | Model | $/Mtok in/out | max_tok | Worst case/call |
|---|---|---|---|---|---|
| economy | groq | `openai/gpt-oss-120b` | 0.15 / 0.75 | 400 | $0.000360 |
| standard | gemini | `gemini-3.1-flash-lite` | 0.25 / 1.50 | 600 | $0.001000 |
| frontier | openrouter | `openai/gpt-4.1` | 2.00 / 8.00 | 900 | $0.008000 |

Rungs rejected, because the reasons matter: `gemini-3.1-pro` 404s on a free-tier
key; `gemini-3.1-pro-preview` 429s (pro models are not in Gemini's free tier);
`github/openai/gpt-4.1` returns **410 — GitHub Models is in scheduled
retirement**; `nvidia/nemotron-3-ultra-550b` works and is the best technical
candidate but is *free at source*, so cost-per-resolved-task would be modelled
rather than measured, and a $0 rung cannot be exhausted by a dollar ceiling.

**Budget policy** ([`config/part2/budgets.yaml`](config/part2/budgets.yaml)):
ceiling **$0.012** per task = a full three-rung climb ($0.00936) + 28% headroom,
deliberately sized so escalation is *always* affordable and any quality loss is
attributable to routing rather than exhaustion. `downgrade_at 0.85`,
`refuse_at 0.95`, `max_calls_per_node 6`, `max_calls_per_run 60`.

**Judge** ([`config/part2/evals.yaml`](config/part2/evals.yaml)): 0–3 ordinal,
bar 0.8, split panel → unresolved. `identifies_defect` (weight 3),
`correct_fix` (2), `no_false_alarms` (1). **No fluency criterion** — on a task
with a right answer, scoring prose measures writing skill, correlates with model
size, and would hand frontier an advantage unrelated to finding the bug. Panel is
`cerebras/zai-glm-4.7` + `nvidia/nemotron-3-super-120b`, disjoint by model from
every rung, asserted by the harness and by
[`tests/test_part2_policy.py`](tests/test_part2_policy.py).

---

### 2. Cost per call and cost per resolved task

| Strategy | Ceiling | Attempts | Spend | **Cost/call** | Resolved | **Cost/resolved** |
|---|---|---|---|---|---|---|
| A always-frontier, 1 attempt | $0.012 | 15 | $0.026308 | $0.00175387 | 13/15 | $0.00202369 |
| **A′ frontier, fair 3 attempts** | $0.012 | 19 | $0.034669 | $0.00182470 | **14/15** | $0.00247637 |
| B always-cheapest | $0.012 | 21 | $0.005412 | $0.00025771 | 12/15 | **$0.00045099** |
| **C this policy** | $0.012 | 20 | $0.010270 | $0.00051349 | **14/15** | **$0.00073355** |
| D descending, tight ceiling | **$0.004** | 19 | $0.006900 | $0.00036317 | 13/15 | $0.00053079 |

**Logs.** A, B and C are one run —
[`p1_cost_per_task_part2_codereview.json`](evidence/logs/p1_cost_per_task_part2_codereview.json)
(9/9 checks). A′ is
[`p1_cost_per_task_frontier_fair.json`](evidence/logs/p1_cost_per_task_frontier_fair.json),
D is [`p1_cost_per_task_descend.json`](evidence/logs/p1_cost_per_task_descend.json).
Each records every per-task attempt with the tier charged, the verdict and its
panel agreement, so any figure above traces to the call behind it. Home paths are
replaced with `<repo>`/`<path>`; nothing else is altered.

**A′ is the honest baseline, and it exists because A was unfair.** The harness
caps strategy A at one attempt while B and C take three. Since these models are
not deterministic even at `temperature: 0` (§6), one sample is a materially weaker
draw. Given equal attempts the baseline reaches **14/15 and ties this policy** —
so the resolution-rate advantage an earlier draft claimed for C is withdrawn. What
survives is cost: **$0.00073 against $0.00248 for the same 14/15.**

**14/15 is the ceiling for every configuration.** `m05_float_equality` is failed
by all of them, including three consecutive frontier attempts at $0.00559 — the
dearest single task in the project.

**The frontier rung earned nothing.** The cheap rung has been observed resolving
14 of 15 across these runs, including `m04` (10/10 in the judge study) and both
hard tasks whose decoys were written to catch it. Every apparent exception
dissolved into sampling variance.

---

### 3. Break-even resolution rate, and where we sit

```
r* = k / (C/c − 1 + k)
```

| | value |
|---|---|
| measured price ratio `C/c` | **6.81** |
| calls consumed by a failed task `k` | 3 |
| **break-even resolution rate `r*`** | **34.1%** (harness reports 30.4% from a different denominator) |
| **measured failure rate** | **20%** |
| **headroom** | **14 percentage points** |

Below a ~66% resolution rate the cheap rung's retry loop costs more per resolved
task than always-frontier. We are comfortably clear, so the signature failure mode
(cheaper per call, dearer per resolved task) was **NOT OBSERVED**.

**`C/c` is a variable you set, not one you are given.** Capping frontier at
`max_tokens: 900` — a sensible cost control — narrowed the ratio from 9.2x to
6.8x and moved break-even from 27% to 34%, i.e. 7 points closer to the line.
Anyone reusing this rule must measure `C/c` on their own configuration.

---

### 4. Jaeger trace — one run's span hierarchy with costs

Run `run-ef452ecc57ad`, trace `b6c9a45f11800e413f67a874c2dfd571`, service
`s15code-part2`. **Fetched back out of Jaeger** via
`GET /api/traces/{id}`, not merely built in memory:

```
run run-ef452ecc57ad  (1691ms)   s15.cost=0.0003507  s15.provider_calls=1  s15.loops=3
    agent loop 2  (1691ms)
        node answer  (1691ms)
            chat openai/gpt-oss-120b  (1691ms)
                gen_ai.provider.name=groq  gen_ai.request.model=openai/gpt-oss-120b
                gen_ai.usage.input_tokens=338  gen_ai.usage.output_tokens=400
                s15.cost=0.0003507 USD  s15.tier=economy  s15.budget.decision=proceed
        plan  (0ms)
    agent loop 1  (2ms)
        plan  (0ms)
        node recall  (1ms)
    agent loop 3  (0ms)
        plan  (0ms)
```

All five levels present, `gen_ai.*` usage on the provider span, cost and the
routing decision alongside it.

**Content capture verified, not just asserted.** All 529 spans across this trace
and the two adversarial traces below were fetched back from Jaeger and scanned
for the four content attribute names (`input.messages`, `output.messages`,
`prompt`, `completion`) and for literal source text from the task snippets
(`def `, `return `, `os.replace`, `fsync`, `pool.remaining`). **Zero hits.** The
prompts are Python code, so a content leak would be unmistakable.

**Attribution, and an honest gap.** The gateway's own ledger
(`GET /v1/cost/by_principal`) attributes correctly by `agent`:

```
agent:s15_rubric_judge    104 calls  $0.067371  cerebras
agent:p1_budget_aware      18 calls  $0.033962  openrouter
agent:s15_answer          157 calls  $0.006006  gemini_1
```

That is a *separate process* from the one reporting §2, and it corroborates the
judge-overhead finding independently. But `tenant`, `project` and `user` rollups
are **empty**: `GatewayClient.PASSTHROUGH` does not forward them, so the runtime
populates 2 of the gateway's 5 dimensions. A multi-tenant deployment could not
bill this back.

---

### 5. The adversarial test — spend before the control, refusal after

[`proofs/part3_attack.py`](proofs/part3_attack.py), four attacks against this
policy, all through the real controller, graph and journal. Full analysis in
**[`part3/README.md`](part3/README.md)**; log:
[`evidence/logs/part3_attack.json`](evidence/logs/part3_attack.json).

| Attack | Demanded | Allowed | Held? |
|---|---|---|---|
| A1 unaffordable tier | $0.008 projected vs $0.0000001 ceiling (**80,000x**) | **$0.00**, 0 calls | yes |
| A2 force a full ladder climb | $0.064 vs $0.004 ceiling (**16x**) | $0.000306 | yes, by downgrade |
| A3 runaway loop | unbounded; ~$0.38/10k rounds extrapolated | $0.00229, stopped at call 60 | yes |
| A4 provider fails **after** consuming tokens | $0.13 consumed vs $0.004 ceiling (**32x**) | ledger reads **$0.00** | **no** |

Refusals **read back from Jaeger**, on the run span as `budget.refused` events:

```
trace 5fb5f0e5...  "run a1_unaffordable"   s15.budget.refusals = 1
  budget.refused: "cheapest tier economy projects 0.000306,
                   run holds 0.000000 (headroom 0.000000)"

trace b2381514...  "run a3_runaway"        s15.budget.refusals = 90
  budget.refused: "run call ceiling reached (60/60)"   x90
```

**A4 defeated the control, and the hole is in the shipped meter rather than this
policy.** `MeteredTransport` documents the assumption: a call that raised
"consumed no tokens the gateway could report, so [is] deliberately not charged."
True of a refused connection; false of a provider that generates 400 tokens and
times out on read. Three failures compound: nothing is charged, so `spent` stays
$0.00 and `refuse_at` can never fire; and `max_calls_per_run` counts *charges*,
so it read 0/60 across 40 attempts. The loop stopped only because the harness
capped it. **Fixes:** count attempts rather than charges; charge the projected
cost of a call that failed after dispatch and reconcile later; distinguish a
provider raising after dispatch from one refusing the connection.

**The telemetry reproduces the blind spot** — A4's trace shows `refusals=0`,
`cost=0` across 123 spans: a clean, cheap, successful-looking run that consumed
$0.13. Observability inherits the meter's gaps, so a dashboard is not an
independent check on the ledger.

**The attack also found a defect in this policy's own config.** The harness prints
thresholds *as loaded* rather than as written, and they disagreed: an earlier
`budgets.yaml` nested all eight under a `thresholds:` key, which
`PolicyThresholds.from_mapping()` does not read. Five were silently discarded —
including **both call ceilings, disabled for an entire measurement run** — with no
error or warning. Fixed, and now regression-tested.

---

### 6. A case this policy got wrong

**`h04_retry_budget` — $0.00071 wasted, a 3.1x overpay.**

| | Verdict | Overall | Agreement | Cost | Tiers |
|---|---|---|---|---|---|
| B | resolved | 1.000 | 1.0 | $0.00033 | economy |
| **C** | **unresolved** | 0.944 | **0.5** | $0.00104 | economy → standard |

Identical model, identical prompt, `temperature: 0`. C's economy answer scored
**0.944, well above the 0.8 bar**, and failed only because the two judges *split*
and `tie_break: unresolved` reads disagreement as failure. The policy then paid
for a second rung to redo work that was already good enough.

**So this ladder escalates on judge disagreement, not only on model failure — and
bills for it.**

Two further wrong choices, for completeness. `m05_float_equality`: climbed all
three rungs and failed at every one, **$0.00250 for nothing** — roughly 23x an
economy call — because the policy cannot distinguish "a better rung may succeed"
from "no rung will". And `m04`: the one task C beat both original baselines on,
which A′ also resolves, so that win is **non-determinism, not routing**.

Concentration is worth noting: **65% of this policy's spend went on 3 of 15
tasks.** Twelve resolved first try for a third of a cent between them.

---

### 7. Three findings that limit the measurement

**`temperature: 0` is not deterministic.** `gpt-oss-120b` returned different text
for byte-identical prompts — 1415/1415/1435/1451 characters across four calls,
four distinct SHA-256 digests. Every per-task result is therefore one draw, not a
measurement, and the two-task spread across five configurations sits inside that
noise.

**The judge's verdict is stable; its scoring is not.** I predicted the judge was
the noise source and tested it: one answer per task, submitted to the panel ten
times ([`proofs/part2_judge_noise.py`](proofs/part2_judge_noise.py), free-tier, $0;
log: [`evidence/logs/part2_judge_noise.json`](evidence/logs/part2_judge_noise.json),
which holds all 40 trials with their individual scores).

| Task | Verdict | Distinct scores | Panel split | Verdict flipped? |
|---|---|---|---|---|
| `m04_swallowed_error` | 10/10 resolved | 1.0000 ×10 | 0/10 | no |
| `h05_partial_write` | 10/10 resolved | 1.0000 ×10 | 0/10 | no |
| `m05_float_equality` | 0/10 resolved | 0.0833 ×8, 0.1667, **0.5833** | **1/10** | no |
| **`h04_retry_budget`** | **0/10 resolved** | 0.8889 ×9, **0.9444** | **1/10** | no |

**0 of 4 verdicts flipped**, so the model — not the judge — is the source of the
run-to-run variance in §2.

But the stability is conditional, and worth stating precisely: **clear-cut answers
are perfectly deterministic** (`m04` and `h05` returned byte-identical scores ten
times), while **marginal answers are not**. On both `m05` and `h04`, one trial in
ten had a judge change its individual opinion, and `m05`'s score ranged 0.083 to
0.583 — a 7x spread on identical input.

Those verdicts held only because both failure paths lead to the same outcome. On an
answer slightly better than `h04`'s, that 1-in-10 variance is enough to flip the
result.

**And `h04` exposes a defect in my rubric.** It scores **above the 0.8 bar on every
trial and fails 10 times out of 10**, by two distinct mechanisms:

- **9 of 10** at 0.8889 with *both judges agreeing* — the only decomposition of
  16/18 that fails is `identifies_defect` 3, `correct_fix` 3,
  `no_false_alarms` **1**. So the answer found the defect and fixed it correctly,
  and was vetoed by the weight-1 criterion. **`min_criterion` gives a weight-1
  criterion the same veto power as a weight-3 one.**
- **1 of 10** at 0.9444 (= 3/3/2, no floor breach) via a **panel split**, where
  `tie_break: unresolved` reads disagreement as failure — the same mechanism that
  cost $0.00071 in §6.

The rubric is left **as measured** rather than tuned after the fact. Correcting the
floor would raise the always-cheapest baseline and push further in the direction
this report already argues — exactly when a post-hoc change should be distrusted.

---

### 8. Conclusion, and when to stop using this

**On this workload the three-rung ladder is not justified.** 14/15 is the ceiling
and three routes reach it — always-frontier for $0.0347, this policy for $0.0103,
and a $0.004 ceiling forcing standard-only for $0.0069 at 13/15. The frontier
rung earned its 22x price on **zero** tasks attributable to capability, and on
`h04` the *standard* rung solved what frontier had just failed twice, so the rungs
are not a reliable capability ordering.

**What this policy degrades, precisely:** at the $0.012 default, nothing — it
matches always-frontier task for task. At $0.004, exactly one task
(`m04_swallowed_error`) for a further 10 points of saving.

**When it stops being worth it:** when the cheap rung's failure rate exceeds
**34%**. Measured 20%. Recompute after any change to models or token ceilings.

**The measurement's own cost is the sharpest finding.** The judge panel cost
$0.029 to grade $0.042 of work — **70% overhead** — and is also the least reliable
component. For a policy whose entire benefit is ~$0.016 per 15 tasks,
LLM-as-judge costs more than the decision it informs. Code review has a
deterministic oracle available: does the patch apply, compile, and make a failing
test pass. Any production version should use it.

Real money spent across all runs: **$0.090 of OpenRouter credit**, confirmed
against the provider's usage endpoint. Every other provider was free-tier.

---

### Reproducing this

[`proofs/REPRODUCE_PART2.md`](proofs/REPRODUCE_PART2.md) — every command from a
fresh checkout, including the gateway and Jaeger. `proofs/out/` is gitignored, so
all figures above are inlined rather than referenced.

```bash
uv run pytest -q                          # 312 passed (277 shipped + 35 policy)
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/codereview.jsonl \
  --config-dir config/part2 --base-url http://127.0.0.1:8111 --label part2_codereview
uv run python proofs/part3_attack.py --budget 0.004 --config-dir config/part2 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/part2_judge_noise.py --trials 10 --config-dir config/part2
```

### Changes to shipped files, and why

| File | Change |
|---|---|
| `config/tiers.yaml`, `config/evals.yaml`, `config/pricing.yaml` | GitHub Models returns **410 (retired)**, so the shipped frontier rung fails for everyone. Repointed to `openrouter/openai/gpt-4.1` — same model, same published rate. |
| `proofs/p1_cost_per_task.py` | `DEFAULT_BASE_URL` was hardcoded to `:8112` while `harness.py` uses `:8111`. An unreachable base URL silently falls back to the offline transport, so the proof printed a full table of **deterministic simulation** labelled OFFLINE instead of failing. Now imports the shared default. |

No secrets, `.env`, or user data are included. `.env` is gitignored and every
added file was scanned for key patterns.
