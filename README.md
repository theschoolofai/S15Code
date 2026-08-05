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

## Evidence: Reproduce the Floor (Both Suites, Five Proofs, Four Traced Runs)

Full detail and every raw number lives in `docs/part1_validation.md`; this
section is the operator-facing summary. Everything below is from a real run
against a live `glc_v4` gateway and a live Jaeger, on 2026-08-05 — no
`--offline` pass.

### Both suites

```
cd glc_v4  && uv run pytest -q     # 448 passed, 8 skipped
cd S15Code && uv run pytest -q     # 277 passed
```

### The five proofs, live

| Proof | Result |
|---|---|
| `p1_cost_per_task.py` | PASS (9/9 checks) |
| `p2_budget_holds.py` | PASS (5/5 checks) |
| `p3_denial_of_wallet.py` | PASS (6/6 checks) |
| `p4_trace_export.py` | PASS (14/14 checks, incl. a real Jaeger round trip) |
| `p7_cross_model_ladder.py` | **FAILED** (7/9 checks) — see the honest limitation below |

### Four runs, traced end to end

Four self-contained prompts, each run through the real `AgentRuntime`
against the live gateway with OTel export on — prompt, tier requested vs.
served, the ordered event trace, the real Jaeger trace ID, the ledger row,
and the final answer, for every one:

| # | Prompt | Tier requested → served | Jaeger trace ID | Cost | Answer |
|---|---|---|---|---|---|
| 1 | "Convert 100 kilometers to miles." | frontier → **standard** (downgrade) | `82a58c0635b39776a11d7680ab041b74` | $0.00007825 | "100 kilometers is approximately 62.1371 miles." |
| 2 | "Explain what a circuit breaker pattern is, in one paragraph." | frontier → **standard** (downgrade) | `9a0e3e5c99115abc73ff43a944f66d9c` | $0.00025000 | A circuit-breaker-pattern explanation (state machine, trip threshold, half-open retry) |
| 3 | "List the capital cities of France, Japan, and Brazil." | 3×economy (search) + standard (distill) + frontier→**standard** (answer, downgrade) | `2b61e2111189d77ef4343a9661d6f5f1` | $0.0017575 total, 5 ledger rows | Paris / Tokyo / Brasília, each cited |
| 4 | "What is the boiling point of water in Celsius and Fahrenheit?" | frontier → **standard** (downgrade) | `5ea9b7448cc9a6c440a0ea674824da21` | $0.00009700 | "100 degrees Celsius or 212 degrees Fahrenheit." |

Every `answer` node in all four runs requested `frontier` and was downgraded
to `standard` under a $0.02 per-node allowance — the ordered event trace,
full ledger rows (provider/model/tokens/decision), and complete answer text
for each run are in `docs/part1_validation.md` Section 3.

### One honest limitation the traces exposed

**The `frontier` rung (`github`/`openai/gpt-4.1`) is dead — a live, dated
fact, not a config mistake.** `p7_cross_model_ladder.py`'s frontier rung
failed with a real `502` wrapping GitHub's own `410 Gone`:
`"github_models_retirement_brownout"` — GitHub is mid-retirement of this API
as of this run. Two measured, downstream effects: `p7` failed its exit code
(no frontier measurement means "top rung costs more" has nothing to compare
against), and a live rerun of `p1_cost_per_task.py`'s always-frontier
strategy resolved **0/12 tasks** — every frontier call transport-failed, so
it never got billed and never resolved anything, making its own cost-per-
resolved-task comparison undefined (`n/a`) rather than silently wrong. The
control layer's own behavior under that failure — downgrade where an
allowance permits it, refuse where it doesn't, and report the gap (two real
`[FAIL]` lines, an honest `n/a`) rather than paper over it — is itself
evidence the layer works; the ladder's config naming a now-dead provider is
what needs updating, not the enforcement mechanism. Full analysis, plus a
second, smaller finding (a node's graph-level trace metadata reports the
*requested* tier even after a downgrade — only the ledger row shows what was
actually *served*), is in `docs/part1_validation.md` Section 4.

### Reproduce

```bash
cd glc_v4 && uv sync && cp .env.example .env && uv run glc serve &   # :8111
docker compose -f glc_v4/docker-compose.observability.yml up -d       # Jaeger
cd S15Code && uv sync
uv run pytest -q && (cd ../glc_v4 && uv run pytest -q)

uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/mixed.jsonl \
  --principal reviewer --base-url http://127.0.0.1:8111
uv run python proofs/p2_budget_holds.py --task "<any task>" --budget 0.02 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/p3_denial_of_wallet.py --task "<any task>" --budget 0.002 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/p4_trace_export.py --task "<any task>" --budget 0.02 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/p7_cross_model_ladder.py --task "<any task>" \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
```

## Evidence: a Policy, Measured and Attacked (Loomstack Billing-Support Triage)

A worked case study proving out this session's whole claim — routing, budgeting,
tracing — on a task class and a ladder this exercise built, not the shipped
`config/`. Full detail and every raw number lives in `docs/part1_validation.md`,
`docs/part2_makeapolicy.md` and `docs/part3_attack_the_budget.md`; this section
is the operator-facing summary.

### The workload and the ladder

18 support tickets for a fictional SaaS billing desk
(`proofs/tasks/part2_billing_support.jsonl`) — classify a ticket and apply a
six-rule refund/billing policy correctly, including six tickets built as
deliberate traps (a false "duplicate charge" claim, an exact-boundary usage
percentage, a rule that only applies to enterprise accounts, a genuinely
ambiguous request, two policy rules that conflict, and two invoices whose
failed-payment clocks must be tracked independently).

A dedicated ladder and budget (`config/part2_billing_support/`), not an edit to
the shipped one:

| Rung | Provider / model | $/Mtok in / out |
|---|---|---|
| economy | `groq` / `openai/gpt-oss-120b` | 0.15 / 0.75 |
| standard | `gemini` / `gemini-3.1-flash-lite` | 0.25 / 1.50 |
| frontier | `openrouter` / `deepseek/deepseek-chat` | 0.2574 / 1.029 |

The shipped ladder's frontier rung (`github`/`openai/gpt-4.1`) was measured dead
mid-session — a live `410 Gone`, "a scheduled retirement brownout," from GitHub's
own API (`docs/part1_validation.md` Section 4) — so this ladder's frontier rung
points at a provider confirmed reachable the same day, priced from a live
fetch of OpenRouter's own pricing page rather than copied or guessed. Budget:
`default_budget: 0.01` per ticket, sized off the ladder's own projected worst
case with roughly 8x headroom (`config/part2_billing_support/budgets.yaml`).

### Cost per call and cost per resolved task, against always-frontier

Measured live (`proofs/p1_cost_per_task.py`), on the 14 tasks the frontier
baseline actually got to attempt before an unrelated `openrouter` daily rate
limit cut it off mid-run (the fair subset; full accounting and the raw 18-task
numbers are in `part2_makeapolicy.md` Section 4.1):

| Strategy | Cost/call | Cost/resolved | Resolved |
|---|---|---|---|
| Always-frontier (baseline) | $0.00017469 | $0.00017469 | 14/14 (100%) |
| **My policy** | **$0.00015330 (−12.24%)** | **$0.00020047 (+14.76%)** | 13/14 (92.9%) |

My policy is cheaper per call and **more expensive per resolved task** than
always calling the frontier model. That is not a bug in the measurement — see
break-even below.

### Break-even resolution rate

```
r* = k / (C/c - 1 + k) = 3 / (0.00017469/0.00015330 - 1 + 3) = 95.56%
```

(`k` = 3 max attempts, `c` = my policy's cost/call, `C` = the baseline's
cost/resolved — the identical `break_even_resolution_rate()` function
`proofs/p1_cost_per_task.py` itself uses, not a reimplementation.) My policy's
measured resolution rate is **92.86%** — **1.87 points below its own
break-even rate**. Below `r*`, cost per call falls while cost per resolved task
rises, which is exactly what the table above shows. On this ladder's price
spread (1.7x, frontier to economy — the shipped ladder's was 13x) and this task
class's short structured replies, cascading loses to simply calling the
frontier model every time.

### A Jaeger trace, span hierarchy with costs

One real trace from the adversarial run below
(`085f12c4e067ccb46dd7a0c3156e2f26`, `http://127.0.0.1:16686/api/traces/085f12c4e067ccb46dd7a0c3156e2f26`),
103 spans: `run: 1, agent_loop: 31, plan: 31, node: 30, provider_call: 10`. One
admitted call's span, read back from the backend itself:

```
gen_ai.provider.name: gemini_1        gen_ai.request.model: gemini-3.1-flash-lite
gen_ai.usage.input_tokens: 25         gen_ai.usage.output_tokens: 9
s15.cost: 0.00001975                  s15.cost.projected: 0.0006085
s15.budget.decision: proceed          s15.tier: standard
```

### The adversarial test: spend before control, refusal after

`proofs/p8_attack_the_budget.py` (new proof) drives a runaway loop — one more
node from every outcome, forever, requesting the same tier every round — twice:
once against an unbounded stand-in config
(`config/part2_billing_support_uncontrolled/`, `default_budget: 1000`,
`max_calls_per_run: 100000`) and once against the real, deployed policy.

| | Ceiling | Admitted calls | Spent | Refusals |
|---|---|---|---|---|
| **Before control** | $1000 | 30 / 30 rounds | $0.00059475 | 0 |
| **After control** | $0.01 | 10 / 30 rounds | $0.00019775 | 20 |

Every one of the 20 refused rounds is a real `ERROR`-status span, fetched back
out of Jaeger by trace id, carrying the exact reason:

```
otel.status_description: "BudgetRefused: budget refused a standard call
  for loop_16: run call ceiling reached (10/10)"
```

A second attack — requesting a tier with a ceiling below what a single token
could cost (`proofs/p2_budget_holds.py`'s `impossible` allowance, run against
this same policy) — is refused in 91ms with **zero calls and zero transport
calls**: the admission gate refuses on the *projected* cost, before any
provider is ever contacted, so this half of the attack holds regardless of
which providers happen to be reachable. Full write-up, including one honest
complication (a still-live `openrouter` rate limit that caused unrelated
transport failures elsewhere in that same run), in
`docs/part3_attack_the_budget.md`.

### A case this policy got wrong

`bs08_refund_in_window_high_usage` — my policy escalated economy → standard →
a rate-limited frontier attempt, spending **$0.00040244 across 3 attempts**,
and still finished unresolved. Always-frontier answered it correctly in one
**$0.00018658** call — 2.16x cheaper than my policy's failed attempt. Root
cause (`part2_makeapolicy.md` Section 7): the cheaper tiers gave a
*substantively correct* answer (deny the refund, offer a prorated credit) but
picked a different action label than the task's exact `expectation` string
named, out of two labels that overlap under my own policy text — a taxonomy
ambiguity that spending more on a stronger model cannot reliably fix, because
tier is the wrong lever for a labeling-convention gap.

### Reproduce from a fresh checkout

```bash
# 1. the gateway (separate repo, sibling directory)
cd glc_v4 && uv sync && cp .env.example .env   # fill in provider keys
uv run glc serve &                              # http://127.0.0.1:8111

# 2. (optional but required for the trace evidence) Jaeger
docker compose -f glc_v4/docker-compose.observability.yml up -d
# UI: http://localhost:16686   OTLP: http://localhost:4318

# 3. this repo
cd S15Code && uv sync
uv run pytest -q                                # 277 passed

# 4. the measurement (cost per call/resolved, break-even)
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/part2_billing_support.jsonl \
  --principal part2/billing_support \
  --config-dir config/part2_billing_support \
  --base-url http://127.0.0.1:8111

# 5. the adversarial test — before, then after
uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support_uncontrolled \
  --principal part3/attack --tier standard --loop-limit 30

uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support \
  --principal part3/attack --tier standard --loop-limit 30 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces

# 6. the unaffordable-tier refusal (no live gateway required for the claim itself)
uv run python proofs/p2_budget_holds.py \
  --task "Draft a one-sentence reply to a billing question." \
  --budget 0.01 --principal part3/attack \
  --config-dir config/part2_billing_support
```

Every command above exits non-zero on a failed check and writes machine-readable
JSON to `proofs/out/`. `--otel-endpoint` is optional everywhere it appears —
omit it and every proof still builds and asserts its spans in-process, just
sends nothing over the wire.

No secrets, `.env`, or real user data are anywhere in this evidence: every
config file above is priced from a publicly documented rate, every task is
about a fictional product ("Loomstack"), and every principal string
(`part2/billing_support`, `part3/attack`) is a synthetic label with no
connection to a real tenant.
