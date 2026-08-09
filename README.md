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

## Part 1 — Reproduce the floor

Both suites green against this checkout: **glc_v4 455 passed, 1 skipped** ·
**S15Code 277 passed**. All five proofs run live against the gateway and pass —
`p1_cost_per_task`, `p2_budget_holds`, `p3_denial_of_wallet`, `p4_trace_export`
(the trace is fetched back **from Jaeger by ID** and its span costs sum to the
ledger with zero delta), and `p7_cross_model_ladder`.

### Four captured runs

Captured by `proofs/capture_floor_runs.py` (machine-generated, committed as
[`evidence/floor_runs.json`](evidence/floor_runs.json) — prompt, tier, model,
ordered events, Jaeger trace ID, ledger rows and final answer for each). All
four ran through `POST /v1/agent/runs` with a budget and a principal; the
answering role requests **frontier** in every case, so what got *served* is the
budget controller's decision. Every run's ordered event trace follows the same
journal shape: `run_started → graph_patched → task_started(recall) →
task_succeeded(recall) → graph_patched → task_started(answer) →
task_{succeeded|failed}(answer) → graph_patched`.

| | run 1 | run 2 | run 3 | run 4 |
|---|---|---|---|---|
| Prompt | capital of Australia (name only) | 240 L of 15% salt → 9%: litres to replace | why budgets must be enforced in code (2 sentences) | compare two approaches to bounding spend |
| Budget | $0.02 | $0.02 | $0.0004 | $0.0000004 |
| Tier requested | frontier | frontier | frontier | frontier |
| Served | `gemini_1` / `gemini-3.1-flash-lite` (**downgraded**) | `gemini_1` / `gemini-3.1-flash-lite` (**downgraded**) | — (**refused pre-call**) | — (**refused pre-call**) |
| Status / answer | completed — "Canberra" | completed — 96 litres, working shown | failed, `BudgetRefused` | failed, `BudgetRefused` |
| Jaeger trace ID | `f24b3de29ad91948cb875322063097ed` | `d8251f6f1fa8eecbd6f6172a8aadd4dd` | `6cffd57db1b349367c56425585cff2ab` | `6c8cfbeb2c5cb91f3cc21581640daeae` |
| Ledger | 1 row, $0.0000860 | 1 row, $0.00043725 | **0 rows, $0** | **0 rows, $0** |
| Span cost == ledger | ✓ exact | ✓ exact | ✓ (both zero) | ✓ (both zero) |

The refused runs are the controller working: the answer node **failed visibly**
(`task_failed`, `BudgetRefused`) with zero provider contact, zero spans of kind
`provider_call`, zero ledger rows.

### One honest limitation the traces exposed

In runs 1 and 2 the answering role asked for **frontier** and was silently
served **standard** — the worst-case projection for the frontier rung did not
fit the allowance, so the controller downgraded. The run response looks
completely successful; only the trace (requested tier vs `gen_ai.request.model`
on the provider-call span) reveals that the policy quietly substituted a
cheaper model. A dashboard aggregating "success rate" would never show this:
**the tier the caller asked for and the tier that answered are different facts,
and only span-level telemetry keeps both.**

## Evidence — a routing & budget policy for an options-trading desk

This is the Session 15 assignment, worked end to end on a real workload. The
ladder, the task file, the judge panel and the three custom scripts below are the
submission; every number here was generated by those scripts against the live
gateway (glc_v4), a live Jaeger, and AWS Bedrock + Groq + Gemini, and re-derivable
with the commands in **[Reproduce it](#reproduce-it-from-a-fresh-checkout)**. No
figure is hand-typed.

> **Prices and models drift.** Measured 2026-08-08 on this deployment. The shipped
> frontier rung (GitHub Models `openai/gpt-4.1`) was **retired**, so the ladder is
> repointed at AWS Bedrock `claude-sonnet-4-5` — a config edit, exactly the
> quarterly turnover the session warns about. See
> [gateway_setup/](gateway_setup/) for the one gateway patch this required.

### 1. The workload and the ladder

**Workload** — 28 tasks from an Indian index-options trading desk
(`proofs/tasks/options_mixed.jsonl`), in two deliberately different halves:

- **18 computational tasks** (`options_trading.jsonl`): premium and margin math,
  spread breakevens and max-loss, expiry-date/weekday logic. Every answer is a
  **computable fact**, verified in Python before the file was written, so the
  judge's weight-2 success criterion scores against ground truth rather than
  fluency.
- **10 high-reasoning tasks** (`options_reasoning.jsonl`): multi-part
  deliverables with no single-number answer — an RMS square-off compliance
  assessment against a written broker policy, a stop-loss-that-didn't-fire
  diagnosis, a backtest code review with a planted look-ahead bug, a
  position-sizing critique, an expiry-day hold-or-close memo, the STT exercise
  trap, a covered-call-sold-as-a-hedge review, fund-marketing due diligence, a
  production incident report, and a two-strategy interaction diagnosis. Each
  expectation names the specific arithmetic, causal chain and failure modes a
  resolving answer must contain ("an answer that X fails"), so the rubric judge
  scores substance.

The mix is the point: the two halves stress different capabilities, and they get
measurably different routing behaviour out of the same ladder (§2, §3).

**Ladder** (`config/tiers.yaml`) — three genuinely different models on three
different providers, monotone in measured price:

| Rung | Provider | Model | List $/Mtok (in/out) |
|---|---|---|---|
| economy | Groq | `openai/gpt-oss-120b` | 0.15 / 0.75 |
| standard | Gemini | `gemini-3.1-flash-lite` | 0.25 / 1.50 |
| frontier | AWS Bedrock | `claude-sonnet-4-5` | 3.00 / 15.00 |

The answering role `answer_with_evidence` floors at **frontier**; the budget
controller downgrades under pressure. `p7_cross_model_ladder` confirms each rung is
served by the model its config names and that a downgrade changes the *model*, not
just the token cap.

### 2. Cost per call vs cost per resolved task, against an always-frontier baseline

Three strategies over the same 28 tasks, one ledger, `$0.10` per-task ceiling
(chosen so the always-frontier baseline is actually admitted at frontier). "Resolved"
is decided by a two-model judge panel — **Amazon Nova Pro + Llama 3.3 70B**, both
disjoint from the answering ladder so no model grades its own output (`p1` asserts
this). Bar: weighted score ≥ 0.75 with no criterion below 0.5. The run carried
zero transport failures: every attempt below is a real model answer, really judged.

| Strategy | Spend | Calls | Cost/call | Resolved | **Cost/resolved task** |
|---|---|---|---|---|---|
| A — always frontier (`claude-sonnet-4-5`) | $0.272106 | 28 | $0.0097181 | **28/28** | $0.0097181 |
| B — always cheapest + 2 retries (`gpt-oss-120b`) | $0.011196 | 38 | $0.0002946 | 24/28 | $0.0004665 |
| C — **my budget-aware cascade** | $0.011383 | 32 | $0.0003557 | **28/28** | **$0.0004066** |

Three results, one per strategy:

**The signature failure mode, observed.** `p1` prints `FINDING: signature
failure mode OBSERVED` for B vs C: always-cheapest is **17.2% cheaper per call
and 14.7% MORE expensive per resolved task**. B's retry loop burned 38 calls and
still left four reasoning tasks unresolved — `r04` (sizing critique), `r05`
(expiry memo), `r06` (STT trap), `r07` (hedge review) each got three genuine
economy attempts, all judged insufficient. Switch the metric from cost per call
to cost per resolved task and the ranking between B and C reverses. This is the
exact trap the session was written to demonstrate, measured live — and it only
appears on the reasoning half of the workload (see §3).

**The cascade earns its keep.** C resolves **28/28 for 95.8% less per resolved
task than the frontier baseline**. Its escalation gate fired on exactly the four
tasks the cheap rung genuinely could not resolve, spending $0.0064 (57% of its
total) climbing economy→standard on those — and nothing anywhere else. Economy
alone handled all 18 computational tasks and six of the ten reasoning tasks.

**The frontier baseline is not a truth oracle.** A resolved 28/28 here, but on
the previous measurement of the computational half it dropped `o12` (iron-condor
max loss, truth 10,500; it answered 12,750) while the cheapest model got it
right at 1/30th the price. Per the session's §12 caveat, single-run resolution
counts carry roughly one task of uncertainty — temperature 0 does not make
verdicts reproducible — which is exactly why the cost conclusions above rest on
the aggregate, not on any single task.

### 3. Break-even resolution rate for this ladder — and where the workload sits against it

The cheap rung becomes more expensive per resolved task once its resolution rate
falls below `r* = k / (C/c − 1 + k)`, where `C/c` is the measured frontier/economy
cost-per-call spread and `k` the calls a failed task consumes.

- **Measured spread `C/c` = 32.98×** on the mixed run (the list-price ratio is
  20×; frontier's long answers to the reasoning tasks widened the measured
  spread — the reason to measure rather than trust the price page).
- With `k = 2`: **`r* = 5.9%`**. With `k = 3`: **`r* = 8.6%`**.
- The economy rung resolved **85.7%** (24/28) — far above break-even against
  *frontier*.

That is the two-sided result the break-even formula actually predicts, split by
task type:

- **Against the frontier baseline** the ladder is enormously wide (33×), so
  always-cheapest stays economical even while failing 4 tasks in 28 — the trap
  cannot spring against A.
- **Against the cascade** the effective spread is narrow (C's cost/call is only
  1.21× B's, because C also opens on economy), so the same 85.7% resolution rate
  sits BELOW the ~89% break-even on that comparison — and the trap springs:
  B is cheaper per call and dearer per resolved task than C.

On the computational half alone (earlier measurement, same configs, in
`evidence/`), economy resolved **18/18** and the whole ladder was dead weight:
one workload, two regimes, and the task *type* — not the price list — decides
which regime you are in.

### 4. A Jaeger trace with per-span cost

`proofs/capture_floor_runs.py` runs four budgeted agent tasks and reads each one's
span tree back **from Jaeger by trace ID**. The moderate-math run
(`trace_id d8251f6f1fa8eecbd6f6172a8aadd4dd`) returns 10 spans in the hierarchy
`run → agent_loop → plan → node → provider_call`; the provider-call span carries
`gen_ai.request.model=gemini-3.1-flash-lite` and `s15.cost=0.00043725`, and the
span costs **sum to the ledger exactly** (`p4` asserts a zero delta and that no
prompt/response text leaves the process). The two runs given a sub-cent ceiling
were **refused before any provider call** — status `failed`, zero spans of kind
`provider_call`, zero ledger rows.

### 5. Adversarial test — denial of wallet (`proofs/attack_wallet.py`)

A runaway loop fires **40 requests at the frontier model** against a **$0.02**
session ceiling:

| | |
|---|---|
| Admitted | 2 |
| Refused (HTTP 402) | **38** |
| Measured spend | **$0.01563** (under ceiling) |
| Uncontrolled, extrapolated | **$0.3126** (20× the ceiling) — marked as extrapolation, not measurement |
| Refusals recorded in `/v1/refusals` | **38 / 38** |

The controller — not the attacker's restraint — stopped the spend, and **every
refusal is visible**: each `402` carries `projected_usd` and `shortfall_usd` and is
stored by the gateway's boundary refusal recorder (the §9 "safest outcome was the
least visible" fix). Note the admission is worst-case: after 2 real calls the
*projected* next call exceeds the remaining budget, so it refuses while actual
spend is still below the ceiling — conservative by design.

Full machine-readable log of the attack — every admitted call's cost, every
402's projected/shortfall numbers, and a sample of the recorder's stored
envelopes — is committed at
[`evidence/attack_wallet.json`](evidence/attack_wallet.json). Spend **before**
the control is the extrapolated uncontrolled row above (clearly marked); spend
**after** is the measured $0.01563 flatline against the $0.02 ceiling while 38
further requests kept arriving and were refused.

### 6. A case my policy got wrong, and what it cost

Two honest failures, both measured:

**(a) Capability is not monotonic — and my ladder's price order is wrong for
part of this workload.** `proofs/probe_rungs.py` asks every rung each of the 18
computational tasks directly and checks the answer against a hidden key (used
only by the probe, never by the judge). Scores: **economy 18/18, standard 15/18,
frontier 14/18.** On four hard tasks (`o11`, `o12`, `o14`, `o15` — condor
max-profit/max-loss, ratio-spread profit and upper breakeven) a **cheaper rung
was right where the frontier model was wrong**. Example o15 (ratio-spread upper
breakeven, truth **24190**): economy answered `24190`, standard `24210`,
frontier `24400`. The p1 measurements confirm it live: on the computational
half, the always-frontier arm's only dropped task (`o12`, truth 10,500; it
answered 12,750) was answered correctly by the economy model at 1/30th the
price. A ladder that trusts price as a proxy for competence routes these *up*
to a model that does them *worse*. My cascade opens cheap, so it mostly dodged
this — but a policy that had floored hard computational tasks at frontier would
have paid ~33× more to be **less** accurate on them.

**(b) The cascade pays double on every task it escalates — and its trigger is a
verdict, which costs money too.** On the mixed run, **57% of strategy C's total
spend ($0.0064 of $0.0114)** went to the four reasoning tasks it escalated
(`r04`–`r07`): each paid for a full economy answer, a judge verdict calling it
insufficient, and then a full standard answer. That is the cascade's structural
cost — it buys 4 extra resolved tasks for roughly 2× their standard-rung price,
which is still 24× cheaper than having sent everything to frontier, but it means
the policy's savings depend on the escalation gate staying rare. A workload
where most tasks escalate would erase the advantage; §3's break-even arithmetic
says exactly when.

An earlier measurement also exposed an infrastructure version of the same
defect: the cascade escalated on economy-rung `503`s (client-side rate-limit
guards, not wrong answers), paying standard-rung prices for tasks the economy
model provably answers. Two gateway fixes (an honest TPM guard read from the
provider's own rate-limit headers, and a wait-out-the-window loop for pinned
providers instead of an instant refusal) removed the false escalations — the
final run carries **zero transport failures** — but the policy-level lesson
stands: **a cascade must distinguish "the model failed" from "the transport
failed", and only the first justifies climbing**.

**(c) Part of what the ladder billed as "capability" was my own token cap.**
Every economy attempt on the four escalated tasks stopped at exactly
`stop_reason=max_tokens` — the rung's 512-token output cap, sized for short
computational answers, not multi-part deliverables (two attempts returned
**zero visible characters**: the fully-billed empty answer that opens the
session). `proofs/probe_truncation.py` re-asks the same model the same four
tasks at a deliverable-sized cap (1500): r04 completes cleanly (`end_turn`) and
r05 recovers **all** of its required content — so at least some escalations
bought a bigger *model* when the task only needed a bigger *cap*, at ~4× the
price. But not all: r06/r07 still miss required content at 1500, so genuine
capability gaps remain, and the two probe runs disagreed on which tasks recover
— the §12 non-reproducibility caveat applies to this probe too. The fix
(borrowed from the strongest peer submission on this point): **size each rung's
`max_tokens` to the task class's answer shape, because max_tokens is not just a
cost cap — it is the worst case the admission controller prices AND a hidden
failure mode for any answer that does not fit.** Left as configured here so the
published numbers stay honest to the policy as measured; re-running with a
right-sized economy cap is a one-line config change a reviewer can try.

### 7. The meta-cost, stated

Judging the mixed experiment cost **$0.230 across 140 calls — 0.78× the entire
answer spend of all three strategies combined** ($0.2947). The reasoning tasks'
long answers made grading itself expensive. Evaluation is not free, and a router
that spends a model call to grade a model call has to justify it. `p1` reports
this line rather than hiding it.

### Reproduce it from a fresh checkout

```bash
# 0. Gateway (glc_v4) + Jaeger, in their own repos. The gateway needs one patch to
#    add the AWS Bedrock provider used by the frontier rung and the judge panel:
git clone https://github.com/theschoolofai/glc_v4.git && cd glc_v4
git apply /path/to/S15Code/gateway_setup/glc_v4_bedrock_provider.patch
cp .env.example .env      # add GROQ_API_KEY, GEMINI_API_KEY_1..N; Bedrock uses the AWS default profile
uv sync
./scripts/jaeger_local.sh --start                          # Jaeger on :16686 / OTLP :4318 (no Docker needed)
GLC_OTEL_EXPORTER_ENDPOINT=http://localhost:4318 uv run glc serve   # :8111

# 1. This repo
cd /path/to/S15Code && uv sync
export GLC_BASE_URL=http://127.0.0.1:8111
export S15_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces
uv run s15code serve                                        # :8113

# 2. Suites + the five proofs (Part 1 floor)
(cd ../glc_v4 && uv run pytest -q)                          # 455 passed, 1 skipped
uv run pytest -q                                            # 277 passed
uv run python proofs/p1_cost_per_task.py    --tasks proofs/tasks/mixed.jsonl --principal demo/you
uv run python proofs/p2_budget_holds.py     --task "state the max loss of an iron condor" --budget 0.02 --principal demo/you
uv run python proofs/p3_denial_of_wallet.py --task "say ok" --budget 0.02 --principal demo/you
S15_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces \
  uv run python proofs/p4_trace_export.py   --task "state the max loss of an iron condor" --budget 0.02 --principal demo/you
uv run python proofs/p7_cross_model_ladder.py --task "state the upper breakeven of a ratio spread" --principal demo/you
S15_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4318/v1/traces \
  uv run python proofs/capture_floor_runs.py                # 4 captured runs -> proofs/out/floor_runs.json

# 3. The policy, measured against the always-frontier baseline (Part 2).
#    The headline table is the full 28-task mixed workload; the computational
#    half alone reproduces the "ladder is dead weight on pure computation" finding.
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/options_mixed.jsonl \
  --budget 0.10 --principal tradon/s15/mixed --label _mixed
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/options_trading.jsonl \
  --budget 0.10 --principal tradon/s15/options --label _options
uv run python proofs/probe_rungs.py                         # per-rung correctness vs a hidden key
uv run python proofs/probe_truncation.py                    # capability failure vs token-cap failure (§6c)

# 4. The adversarial test (Part 3)
uv run python proofs/attack_wallet.py --rounds 40 --ceiling 0.02 --max-tokens 512
```

Every proof exits non-zero on failure and writes machine-readable JSON to
`proofs/out/`. Secrets, `.env` and real user data are never committed.

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
