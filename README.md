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

## Evidence — S15 assignment (Aaryan)

Everything below is a **live run against real providers** (Groq + Gemini free
tier) through `glc_v4`, with real Jaeger traces fetched back by ID. Reproduce
it end to end with the commands in §7.

New files: `proofs/tasks/support_tickets.jsonl`, `config/support_desk/`,
`s15code/economics/attempt_guard.py`, `s15code/economics/patient_transport.py`,
`proofs/p8_flaky_provider_dow.py`, `proofs/repro_floor_runs.py`,
`tests/test_attempt_guard.py`, `tests/test_patient_transport.py`.
Two shipped files were patched, both additively and both explained below:
`proofs/harness.py` and `proofs/p1_cost_per_task.py`.

**On the money.** Both keys are free tier, so the dollar figures are
**list-price notional cost**, not an invoice anyone received. That is the right
thing to measure for a routing policy — the question is what the ladder *would*
cost at scale — but no money changed hands and the README does not pretend
otherwise.

### 1. Reproduce the floor

Both suites, then the five proofs, then four runs captured end to end.

```
glc_v4    445 passed, 11 skipped
S15Code   298 passed          (280 shipped + 18 new, tests/test_patient_transport.py)
```

| proof | result |
|---|---|
| `p2_budget_holds` | **PASS** — ceiling held at every allowance; a tight allowance downgraded `frontier`→`standard`, changing the served model from `gemini-3.1-flash-lite` to `openai/gpt-oss-120b`; an unaffordable ceiling refused with 0 calls |
| `p3_denial_of_wallet` | **PASS** — 200 adversarial rounds, 60 admitted, **140 refused**, spent `$0.01153125` against a `$0.10` ceiling. Extrapolated uncontrolled bill: **~$1.92 over 10,000 rounds** |
| `p4_trace_export` | **PASS** — trace `d45ee21d5dc51ce4d5e854c7797078bb`, 10 spans, fetched back from Jaeger; span costs sum to the ledger exactly (delta 0.000e+00); no prompt text on the wire |
| `p7_cross_model_ladder` | **PASS** — every rung served by the model its config names; a downgrade changes the **model**, not just the token cap; **4.88× measured spread** |
| `p1_cost_per_task` | **PASS** — see §2 |

**Four runs, captured** (`proofs/out/repro_floor_runs.json`). Every trace ID is
queryable in Jaeger.

| run | prompt | tier → model | ledger | Jaeger trace |
|---|---|---|---|---|
| 1 declared `$0.02` | REST vs gRPC tradeoffs | frontier → `gemini-3.1-flash-lite` (proceed) | 1 charge, `$0.00081175` | `8e5cd69dd5af651318eec8f58a17524f` |
| 2 tight `$0.0015` | research project plan | standard → `openai/gpt-oss-120b` (**downgrade**) | 1 charge, `$0.00081015` | `b65bc17fa98cbbb78071dab9d826d881` |
| 3 trace shape `$0.02` | semantic cache explanation | frontier → `gemini-3.1-flash-lite` (proceed) | 1 charge, `$0.00044450` | `de8a5f88dc6967a58b4dac1e0a3dbe3d` |
| 4 generous `$0.05` | incident postmortem outline | frontier → `gemini-3.1-flash-lite` (proceed) | 1 charge, `$0.00091625` | `ffe71bb269e270e29afbc7837ec30dee` |

All four produce the identical 8-event journal: `run_started → graph_patched →
task_started → task_succeeded → graph_patched → task_started → task_succeeded
→ graph_patched`, and all four returned real prose answers.

#### One honest limitation the traces exposed

**A trace cannot tell you a call never happened.** On the first live attempt,
`p2`/`p3`/`p4`/`p7` all failed and three of four floor runs recorded **zero
charges** — yet every one still emitted a well-formed 9-span trace with a valid
trace ID, a correct `run → agent_loop → plan → node` hierarchy, and a ledger
that summed perfectly to `$0.00`. Every invariant the telemetry checks held,
because the telemetry's invariants are about *internal consistency*, and a run
that spends nothing is trivially consistent with itself.

The traces were structurally perfect and economically empty. What was actually
happening was 60 transport failures against 23 successful calls (Groq `TPM
limit`, Gemini `RPM quota burned`) — invisible in the span tree, because a call
that never reached a provider produces no `provider_call` span to look wrong.
The only thing that caught it was noticing the *absence* of a span kind:
`['agent_loop', 'node', 'plan', 'run']` with no `provider_call`.

That is a real gap in trace-based cost observability. A dashboard summing
`s15.cost` across these traces would have reported `$0.00` and a 100% success
rate, and been wrong about the second. **Cost telemetry needs a denominator** —
calls attempted, not just calls made — or an empty run is indistinguishable
from a cheap one.

That gap is what `s15code/economics/patient_transport.py` closes, and §2
explains why it also silently corrupted the headline number.

### 2. My task class, my ladder, my measurement

**Workload.** `proofs/tasks/support_tickets.jsonl` — 18 customer-support
tickets (8 trivial, 6 moderate, 4 hard) asking for a ready-to-send reply.
Trivial end: password resets, refund status. Hard end: exact date arithmetic
(`s15`), resolving two explicitly contradictory stated policies (`s16`),
two-order dollar arithmetic (`s17`), tiered-discount eligibility (`s18`).
My own domain, not the shipped `mixed.jsonl`.

**Ladder** (`config/support_desk/tiers.yaml`), every model name verified
callable against the live APIs before being written down:

| rung | provider | model | $/Mtok in/out |
|---|---|---|---|
| economy | groq | `openai/gpt-oss-20b` | 0.10 / 0.50 |
| standard | groq | `openai/gpt-oss-120b` | 0.15 / 0.75 |
| frontier | gemini | `gemini-3.1-flash-lite` | 0.25 / 1.50 |

Economy and standard are the same family at 20B and 120B, so one rung up is a
straight capacity increase; the top rung is a different lab on a different
provider. **`role_tiers` gives the graph a gradient** — the terminal node that
drafts the reply asks for `frontier`, upstream recall/retrieval/summarising ask
for `economy`. Without that gradient there is nothing to downgrade (see §6).

**Judge** (`config/support_desk/evals.yaml`): the shipped rubric verbatim —
`addresses_task`, `specific`, `consistent`, `complete`, `meets_expectation`
(weight 2.0), threshold 0.75, no criterion below 0.5. Only the panel changed,
to `groq/qwen3.8-27b` + `groq/qwen3.6-27b`. **Why I can defend it:** the rubric
is task-agnostic and config-owned, the only per-task input is the
`expectation` string in the data file, both judges are a different model family
(Qwen) from every rung (GPT-OSS, Gemini), and `no answer was graded by its own
model` is machine-checked and passed. Panel disagreement is recorded per
verdict, and an unparseable verdict is a *judge* failure that can never
silently become a task outcome — 0 occurred.

#### Cost per call vs cost per resolved task

18 tasks × 3 strategies, 57 metered calls, 0 transport failures, wall clock
2072s.

| strategy | spend | calls | **cost/call** | resolved | **cost/resolved** |
|---|---|---|---|---|---|
| **A** always-frontier *(baseline)* | `$0.00495250` | 18 | `$0.00027514` | **18/18** | `$0.00027514` |
| **B** always-cheapest (≤3 retries, same rung) | `$0.00369110` | 20 | `$0.00018455` | 17/18 | `$0.00021712` |
| **C** budget-aware cascade *(my policy)* | `$0.00340595` | 19 | **`$0.00017926`** | **18/18** | **`$0.00018922`** |

**My policy beats the frontier baseline by 31.2% per resolved task while
matching it at 18/18.** It also beats the always-cheapest baseline on *both*
axes — 2.95% cheaper per call and 14.7% cheaper per resolved task — because
escalating once beats retrying a model that has already failed.

Resolution by difficulty: A and C both `{trivial 8/8, moderate 6/6, hard 4/4}`.
B: `{trivial 8/8, moderate 5/6, hard 4/4}`.

**The judge cost 4.3× the work it judged.** 94 judge calls, `$0.02138715` —
against `$0.01204955` of total answering spend across all three strategies.
LLM-as-judge is not free, and on a task set this cheap the measurement
apparatus dominates the thing measured. It is reported separately and never
folded into cost-per-resolved-task, which is the only reason the headline
number means anything.

### 3. Break-even resolution rate

`p1` computes it from the measured spread, not from a guess:

- **B vs A: break-even = 85.9%.** The cheap rung must resolve at least 85.9% of
  tasks for its lower cost per call to survive its retry overhead. B measured
  **94.4%** — **+8.5pp of headroom**, so on this task set the cheap rung is
  genuinely worth it against the frontier baseline.
- **B vs C: break-even = 99.2%.** Against my cascade the bar is far higher,
  and B sits at 94.4% — **4.7pp *below* break-even.** Retrying the cheap rung
  loses to escalating off it.

**Where I sit:** C resolves 100%, so it is above every break-even in the table.
The honest reading is that the margin is thin — a 4.88× measured ladder spread
means the routing decision is worth less here than on a wider ladder, and a
task set with a heavier hard tail would move all three of these numbers.

### 4. A Jaeger trace with span hierarchy and costs

`p4_trace_export`, trace **`d45ee21d5dc51ce4d5e854c7797078bb`**, fetched back
from Jaeger's own API (not merely asserted from the exporter):

```
run                                        s15.cost = 0.00081175
└── agent loop
    ├── plan
    └── node
        └── provider call    gen_ai.provider.name  = gemini_1
                             gen_ai.request.model  = gemini-3.1-flash-lite
                             gen_ai.usage.input_tokens  = 223
                             gen_ai.usage.output_tokens = 504
                             s15.cost              = 0.00081175
```

10 spans emitted, 10 in the backend. Span costs sum to the ledger with delta
`0.000e+00`; token counts match exactly (223 == 223). Content capture off —
none of `input.messages`, `output.messages`, `prompt`, `completion` appears in
any backend tag. `s15.cost` is a vendor extension, deliberately not
masquerading as a standard GenAI attribute.

### 5. The adversarial test: before, and refused after

`proofs/p8_flaky_provider_dow.py`. Not the shipped `p3` — this attacks a gap
`p3` does not reach. `controller.py`'s `MeteredTransport` never charges a call
that raises, reasoning it "consumed no tokens the gateway could report." That
is an assumption about the *failure mode*, and it is false for a provider that
generates output and only then drops the connection. Because `RunBudget.calls`
— what `max_calls_per_run` gates on — only increments inside `charge()`, and
`charge()` only runs after a call *returns*, such a provider is never charged
**and never trips the call ceiling either**.

| | S15Code's ledger | TRUE bill (shadow ledger) | refusals | Jaeger trace |
|---|---|---|---|---|
| **before** (`--no-guard`) | `$0.00000000`, 0 calls | **`$1.08570000`** over 150 rounds, unbounded | **0** | `c6044df89576340553843d5cab1c5eb8` |
| **after** (guard active) | `$0.00000000`, 0 calls | **`$0.43428000`**, capped at 60 attempts | **90** | `6e25a0c6849c71380fb7b52cbfd4c5cb` |

Before the control, the loop ran all 150 rounds and only the proof's own safety
stop ended it — the ledger reported `$0.00` and `0/60` calls used the entire
time, while a real provider would have invoiced `$1.09`. After it, 90 of 150
asks are refused pre-flight and the true bill is bounded by the attempt
ceiling rather than the loop length.

**The refusal is visible in telemetry**, not just asserted: both traces carry
453 spans with **150 ERROR-status spans**, and the guarded run additionally
shows **90 nodes failed with `BudgetRefused`** in the journal. Both trace IDs
were fetched back from Jaeger and hold every span the run emitted.

`AttemptGuardedTransport` counts every *attempt*, success or failure, against
the run's existing ceilings — so it caps the loop even when the provider never
once honestly reports what it did. No edit to `controller.py` or `budget.py`.

### 6. Cases the policy got wrong

**The routing mistake, with its price.** `s12_two_issues_one_ticket` — a
customer asking two unrelated questions in one message — is the single task all
three strategies disagreed on:

| strategy | path | resolved | cost |
|---|---|---|---|
| A always-frontier | `frontier` | ✅ | `$0.00032675` |
| **C my policy** | `economy` → `standard` | ✅ | **`$0.00060875`** |
| B always-cheapest | `economy` ×3 | ❌ | `$0.00082590` |

**My policy chose wrongly and it cost 86% more than the baseline on that task.**
The cascade gambled on the cheap rung, lost, and paid `$0.00060875` where going
straight to frontier would have cost `$0.00032675` — `$0.000282` wasted. The
gamble is only worth it because it pays off on the other 17 tasks; on this one
it was strictly worse than not routing at all. That is the trade the policy
makes, priced.

Worth noting what B's column shows: `$0.00082590` spent to resolve *nothing*.
The cheap rung's failure mode is not that it is cheap, it is that its retries
are uncorrelated — the same model given the same prompt three times produced
three answers that failed the same criterion. Escalation beats repetition.

**The measurement mistake, which was worse.** My first live run reported
always-frontier at **0/18 resolved**. That number was about Google's rate
limiter, not the model. The shipped harness paces and retries the *judge* path
— `judge.py` argues explicitly that a rate limit "is a TRANSPORT failure, not a
judge failure… giving up and calling the task unresolved would not be" honest —
but never applies that reasoning to the *answer* path, where a 429 became an
empty answer that the judge then scored unresolved. A provider's per-minute
quota was flowing straight into the resolution rate, which is the denominator
of the headline number.

`patient_transport.py` fixes it: retry transient failures (429/TPM/RPM/
cooldown/502/503/timeout) with backoff, honouring any wait the provider itself
names; raise permanent ones (404, unknown provider, 401) immediately, since
waiting on a model that does not exist only burns clock. It never fabricates a
response — exhausting retries re-raises the original error.

The result is the interesting part: in the successful run, **`0 waits, 0
retries, 0 permanent failures`**. Pacing alone (4s between answer calls) meant
no retry was ever needed. Not bursting was strictly cheaper than recovering
from a burst, and the fix that mattered was the boring one.

### 7. Reproduce all of it from a fresh checkout

```bash
git clone https://github.com/theschoolofai/glc_v4.git
git clone https://github.com/theschoolofai/S15Code.git   # this branch

# Jaeger
docker compose -f glc_v4/docker-compose.observability.yml up -d
#   or, with no Docker: glc_v4/scripts/jaeger_local.sh --start

# Gateway. Free-tier Groq + Gemini keys are enough; S15Code holds no credential.
cd glc_v4 && uv sync
cat > .env <<'EOF'
GEMINI_API_KEY_1=<your key>
GROQ_API_KEY=<your key>
LLM_ORDER=gemini,groq
EOF
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run glc serve &
curl -s localhost:8111/v1/status | jq -r '.order[]'    # expect: gemini, groq

cd ../S15Code && uv sync
export GLC_BASE_URL=http://127.0.0.1:8111
export S15_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces
CFG=config/support_desk

# both suites (strip the OTLP endpoint: some tests assert tracing is OFF)
( cd ../glc_v4 && env -u OTEL_EXPORTER_OTLP_ENDPOINT uv run pytest -q )
env -u S15_OTEL_EXPORTER_ENDPOINT uv run pytest -q

# Part 1 — the five proofs and the four captured runs
T="Summarize the tradeoffs between REST and gRPC for an internal microservice."
uv run python proofs/p2_budget_holds.py       --task "$T" --budget 0.02 --config-dir $CFG
uv run python proofs/p3_denial_of_wallet.py   --task "$T" --budget 0.10 --config-dir $CFG
uv run python proofs/p4_trace_export.py       --task "$T" --budget 0.02 --config-dir $CFG \
    --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/p7_cross_model_ladder.py --task "$T" --config-dir $CFG
uv run python proofs/repro_floor_runs.py --task unused --config-dir $CFG \
    --otel-endpoint http://127.0.0.1:4318/v1/traces

# Part 2 — the measurement (~35 min; the pacing is what keeps it valid)
uv run python proofs/p1_cost_per_task.py \
    --tasks proofs/tasks/support_tickets.jsonl \
    --principal support/triage/live --config-dir $CFG --label live \
    --answer-pace 4 --answer-retries 5 --answer-backoff 10

# Part 3 — adversarial, before and after the control
uv run python proofs/p8_flaky_provider_dow.py --task "Draft a support reply." \
    --budget 0.02 --config-dir $CFG --offline --no-guard --loop-limit 150
uv run python proofs/p8_flaky_provider_dow.py --task "Draft a support reply." \
    --budget 0.02 --config-dir $CFG --offline --loop-limit 150 \
    --otel-endpoint http://127.0.0.1:4318/v1/traces

open http://localhost:16686   # every trace ID above is queryable here
```

Machine-readable results land in `proofs/out/`, which is gitignored as
regenerated scratch. A point-in-time copy of the artefacts behind every number
above is committed to **`evidence/`** — `evidence/proofs/*.json` (every check,
every ledger row, every trace ID, the full per-task breakdown) and
`evidence/logs/*.log` (the console output of the run that produced them), so a
reviewer can verify the claims rather than take them on trust.
`gateway.log` is deliberately excluded: it is server-side noise and the file
most likely to have captured something that should not be published.

No secret, `.env`, or real user data appears anywhere in this branch, and the
push script refuses to commit if anything key-shaped is staged.
