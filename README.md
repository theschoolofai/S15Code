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

# Evidence — a budget-aware routing policy for LLM cost engineering

Measured live 2026-08-07 against `glc_v4` on `127.0.0.1:8111`. Full working in
[`docs/PART1_EVIDENCE.md`](docs/PART1_EVIDENCE.md),
[`docs/PART2_EVIDENCE.md`](docs/PART2_EVIDENCE.md),
[`docs/PART3_EVIDENCE.md`](docs/PART3_EVIDENCE.md) and
[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md).

## 1. The workload and the ladder

`proofs/tasks/llm_cost_engineering.jsonl` — 17 tasks (4 trivial / 5 moderate /
8 hard) in LLM and cloud cost engineering. Every answer is a number checkable by
hand, so the rubric's heaviest criterion rests on arithmetic rather than taste.
All 17 expectations were independently recomputed before any measurement, and
each names the plausible wrong answer: unweighted rate averaging, cache reads
treated as free, retries multiplying calls, cost-per-call versus
cost-per-resolved-task.

| rung | provider | model | $/Mtok in/out |
|---|---|---|---|
| economy | groq | `openai/gpt-oss-120b` | 0.15 / 0.75 |
| standard | gemini | `gemini-3.5-flash-lite` | 0.30 / 2.50 |
| frontier | nvidia | `nvidia/nemotron-3-ultra-550b-a55b` | 0.60 / 3.60 |

The shipped frontier rung `github/openai/gpt-4.1` was **retired on 2026-07-30 —
the same day `tiers.yaml` recorded its measurement**. Verified dead: 17 model IDs
across 7 vendors all return HTTP 410, as does the catalog endpoint and a request
naming no model at all.

## 2. Cost per call vs cost per resolved task

Two arms, identical but for the token ceiling. Same tasks, models, prices, judge.

**Wide ladder** (`config/`, economy ceiling 512):

| strategy | spend | calls | cost/call | resolved | cost/resolved |
|---|---|---|---|---|---|
| A always-frontier *(baseline)* | 0.00509460 | 17 | 0.00029968 | 10/17 | 0.00050946 |
| B always-cheapest | 0.00914595 | 27 | 0.00033874 | 14/17 | **0.00065328** |
| C budget-aware | 0.01086335 | 21 | 0.00051730 | 17/17 | 0.00063902 |

**Narrow ladder** (`config_narrow/`, all ceilings 1024):

| strategy | spend | calls | cost/call | resolved | cost/resolved |
|---|---|---|---|---|---|
| A always-frontier *(baseline)* | 0.00674340 | 17 | 0.00039667 | 10/17 | 0.00067434 |
| B always-cheapest | 0.00710205 | 19 | 0.00037379 | 16/17 | **0.00044388** |
| C budget-aware | 0.01154775 | 19 | 0.00060778 | 17/17 | 0.00067928 |

**The signature failure mode was observed** — wide arm, B vs C: cost per call
**−34.5%** while cost per resolved task rose **+2.2%**. Change the denominator and
the ranking reverses.

**The measured ladder is inverted.** Projected 32.5× with frontier dearest;
measured, frontier costs *less per call* than economy (0.00029968 vs 0.00033874),
because economy writes 7.3× more output tokens (407.6 vs 55.6 mean).

## 3. Break-even, and where we sit

`r* = k / (C/c − 1 + k)`, k = 3.

| arm | comparison | B rate | r\* | headroom |
|---|---|---|---|---|
| wide | B vs A | 0.824 | 0.8562 | **−0.0326** |
| wide | B vs C | 0.824 | 0.7719 | +0.0516 |
| narrow | B vs A | 0.941 | 0.7886 | +0.1525 |

In the wide arm the cheap rung sat **3.3 points below** break-even, which is why
it lost. What moved it was not price — it was **truncation**: raising the economy
ceiling 512→1024 cut truncated attempts from **18 to 4** and lifted resolution
82.4% → 94.1%. Strategy A is the control at 10/17 in *both* arms, since it writes
5–24 output tokens and never nears a ceiling.

*Confound, stated plainly:* equalising `max_tokens` changed the ceiling and the
projected spread together, because the projection is a function of the ceiling.

## 4. One run's span hierarchy, with cost

Run `run-0e4227fe8e3a` (evidence run 1), service `s15code-agent`. The trace as
**fetched back from Jaeger's own query API** is committed at
[`docs/evidence/jaeger_trace_f3990d82.json`](docs/evidence/jaeger_trace_f3990d82.json)
— the raw `/api/traces/{id}` response, so it is verifiable without a running
backend:

```
run run-0e4227fe8e3a                          13007.0ms   [run total 0.000555]
  agent loop 2                                13007.0ms
    node answer                               13007.0ms
      chat nvidia/nemotron-3-ultra-550b-a55b  13007.0ms   cost=0.000555
                                                          229in/116out  tier=frontier
    plan                                          0.0ms
  agent loop 1                                    2.0ms
    plan                                          0.0ms
    node recall                                   1.0ms
  agent loop 3                                    0.0ms
    plan                                          0.0ms
                                        10 spans, 1 billable, sum 0.00055500
```

Nine of ten spans cost nothing because nine bought nothing. `p4` reconciles the
billable span against the ledger at **delta 0.000e+00**, and confirms no prompt
or completion text reached the backend.

*Why the JSON rather than a trace id:* Jaeger all-in-one stores traces in memory
and forgets them on restart, and `/v1/agent/runs/{id}/trace` mints a **new** trace
id on every call — three queries of this same run returned three different ids
(`docs/LIMITATIONS.md` §1.10). A trace id alone is perishable and not canonical;
the committed response is neither.

## 5. The adversarial test

`proofs/tasks/verbosity_attack.jsonl` — six requests whose **correct** answers are
long, paired with six controls from the same domain that are equally easy but
short. Every request is individually cheap and would pass any spend ceiling.

| group | n | mean/request | vs control |
|---|---|---|---|
| control | 6 | $0.00004825 | 1.00× |
| attack, no escalation | 4 | $0.00035404 | 7.34× — verbosity alone |
| attack, escalated | 2 | $0.00228495 | **47.36×** — verbosity + ladder climb |

**20.7× amplification per request**, all 12 resolved. The escalation multiplier of
**6.45×** independently reproduces the 7.0× measured on `c06` in Part 2. One
request missed the 512-token cap by **six tokens** — asking for "integers 1 to
400" makes the attack deterministic.

### The same attack, before and after the control

Re-run unchanged under a `$0.0006` per-task ceiling — sized to admit one economy
call and refuse the climb:

| task | uncapped | capped |
|---|---|---|
| a04_powers_of_two | `economy,standard` **$0.00292160** | `economy,`**REFUSED**`,`**REFUSED** $0.00041040 |
| a06_numbers_in_words | `economy,standard` **$0.00164830** | `economy,`**REFUSED**`,`**REFUSED** $0.00040920 |
| all 12 tasks | **$0.00627555**, resolved 12/12 | **$0.00252000**, resolved 10/12 |

**Spend cut 59.8%**, and every control task is untouched — still served at the
economy rung for the same price. Four refusals recorded, each with a reason:

```
budget refused a standard call for a04_powers_of_two#C2: cheapest tier economy projects ...
budget refused a frontier call for a04_powers_of_two#C3: ...
budget refused a standard call for a06_numbers_in_words#C2: ...
budget refused a frontier call for a06_numbers_in_words#C3: ...
```

**The trade is real and is not hidden:** the ceiling does not make the attack
free, it makes it *bounded*, and the price is two tasks that now go unanswered.
12/12 resolved becomes 10/12.

The shipped runaway-loop attack shows the same control from the other direction
(`p3`, 60 rounds, $0.0005 ceiling):

```
loop rounds 60   admitted 1   refusals 59   spent 0.00036555 / 0.00050000
[PASS] the loop kept asking and was refused
[PASS] a refusal is a visible graph failure    59 nodes failed with BudgetRefused
```

The refusal is visible in the run's `budget.refusal_log` and as a failed graph
node — but **not** in the gateway's `/v1/refusals` (which recorded **0** while S15
recorded 59; the two controllers sit at different layers), and **not** in the
trace: a refused run emits 9 spans where an admitted one emits 10, differing by
exactly the `provider_call` span.

## 6. One case the policy got wrong

`c06_cache_discount`:

```
B  economy   251 out   $0.00022155   RESOLVED   overall 1.0
C  economy   512 out   $0.00041730   FAILED     overall 0.5   <- truncated at the cap
C  standard  432 out   $0.00112770   RESOLVED   overall 1.0
   C total   $0.00154500  =  7.0x what B spent, for the same final answer
```

Same model, same prompt, temperature 0 — one run wrote 251 tokens and finished,
the other ran to exactly 512 and was cut off. The cascade read the truncation as a
quality failure and escalated. **A rung whose ceiling truncates a correct answer
will be escalated past, and output length at temperature 0 is not deterministic —
so this is a coin flip priced at 7×.**

Reported beside `c11_break_even_rate`, where the same rule climbed all three rungs
at 2.8× and reached an answer B never did. A cascade cannot tell the two cases
apart at the moment it decides.

Also measured: **capability is not monotonic.** The frontier rung resolved 1 of 5
moderate tasks where economy resolved 5 of 5. Its errors are arithmetic slips in
5–24 output tokens (`0.018360` for `0.018346`; `$3.30` for `$4.30`). The cause is
this policy's own `reasoning: "off"`, set to avoid the invisible-token billing
bug — it also removed the model's scratchpad. **The dial that stops you paying for
invisible tokens is the same dial that costs you correctness on multi-step work.**

Every frontier miss scored exactly **0.6667** — 4 on all four generic criteria, 0
on `meets_expectation`. Precisely the case the 0.5 per-criterion floor exists to
catch.

## 7. Reproduce from a fresh checkout

```bash
git clone <this fork> && cd agent_economics && uv sync
cp .env.example .env          # GLC_BASE_URL, S15_PORT, sandbox root; no provider keys
cd ../glc_v4 && uv sync && cp .env.example .env   # provider keys live here
uv run glc serve                                  # :8111

cd ../agent_economics
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/llm_cost_engineering.jsonl \
  --principal you/p2-wide --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --label wide

uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/llm_cost_engineering.jsonl \
  --principal you/p2-narrow --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --config-dir ./config_narrow --label narrow

uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/verbosity_attack.jsonl \
  --strategies C --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --principal you/p3-attack --label attack

uv run python proofs/p3_denial_of_wallet.py --task "Compare two approaches to bounding agent spend." \
  --budget 0.0005 --principal you/p3-tight --base-url http://127.0.0.1:8111 \
  --loop-limit 60 --label live-tight
```

**`--base-url` is mandatory.** `p1` defaults to port 8112 while the gateway serves
8111, and no proof loads `.env`, so `GLC_BASE_URL` is never read on that path. On
an unreachable gateway `p1` falls back to a simulated transport and still reports
`ALL CHECKS PASSED` with exit 0 — check the `(live mode)` line before believing
any number. See [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) §G.
