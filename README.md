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


# Session 15 Assignment: Budget-Aware LLM Routing for Irregular Travel Operations

**Author:** Kumari Astha  
**Domain:** Airline disruption recovery — policy-safe passenger rebooking under time, inventory, and cost constraints  
**Ladder:** cerebras/zai-glm-4.7 (economy) → gemini/gemini-3.1-flash-lite (standard) → openrouter/openai/gpt-4.1 (frontier)

---

## The Workload

Fifteen tasks modelling an airline irregular-operations desk. Five trivial (classification, lookup), five moderate (structured JSON output, boundary-condition policy), five hard (multi-constraint rebooking, adversarial prompt injection, root-cause trace diagnosis). Every task has a single objectively verifiable answer and a rubric expectation precise enough for the judge panel to score 0–4.

Task file: [`proofs/tasks/travel_ops.jsonl`](proofs/tasks/travel_ops.jsonl)

## The Ladder

Three rungs, three providers, monotonically rising cost. Each rung was chosen for a specific class of work:

| Tier | Provider | Model | Input $/Mtok | Output $/Mtok | max_tokens | reasoning |
|------|----------|-------|-------------|--------------|------------|-----------|
| economy | cerebras | zai-glm-4.7 | 0.50 | 0.50 | 256 | off |
| standard | gemini | gemini-3.1-flash-lite | 0.25 | 1.50 | 768 | off |
| frontier | openrouter | openai/gpt-4.1 | 2.00 | 8.00 | 1536 | — |

`max_tokens` is set per-rung based on what the task class actually produces, not a round number. Tight ceilings make the projected cost closer to the real cost, which makes the policy's routing decisions better.

Config files: [`config/travel_ops/`](config/travel_ops/)

## The Budget Policy

| Parameter | Value | Why |
|-----------|-------|-----|
| default_budget | $0.03 | enough for frontier on the hardest tasks, tight enough that runaway loops downgrade and refuse |
| reserve_fraction | 0.20 | holds 20% so the terminal answer node is never starved by upstream research |
| downgrade_at | 0.50 | at 50% spend, start stepping down the ladder |
| refuse_at | 0.90 | at 90% spend, refuse all further calls |
| headroom_fraction | 0.02 | last admitted call never lands exactly on zero |
| max_calls_per_run | 40 | denial-of-wallet defence, independent of price estimates |
| max_calls_per_node | 5 | prevents a single retrying node from spinning |

Judge panel: groq/openai/gpt-oss-120b + nvidia/meta/llama-3.1-8b-instruct — disjoint from the answering ladder so no model judges its own output.

---

## Part 1: Reproduce the Floor

### Suites

| Suite | Result |
|-------|--------|
| GLC v4 pytest | 448 passed, 8 skipped, 1 warning (19.04s) |
| S15Code proofs p2, p3, p4, p7 | ALL CHECKS PASSED |

### Proofs

| Proof | What it tests | Result |
|-------|--------------|--------|
| p2_budget_holds | ceiling holds, downgrades work, exhaustion refuses, every call metered | 5/5 PASS |
| p3_denial_of_wallet | runaway loop capped at max_calls_per_run=40 | 6/6 PASS. 200 rounds, 40 admitted, 160 refused. Uncontrolled bill: ~$3.16 |
| p4_trace_export | journal → OTel → Jaeger pipeline, span costs sum to ledger | 15/15 PASS. Delta 0.000e+00 |
| p7_cross_model_ladder | each rung is distinct model/provider, downgrades change the model | 10/10 PASS. Projected spread 90×, measured spread 10.24× |

Proof outputs: [`proofs/out/`](proofs/out/)

### Four Evidence Captures

| Task | Difficulty | Answer | Correct? | Tier | Model | Cost (USD) | Run ID | Jaeger Trace ID |
|------|-----------|--------|----------|------|-------|------------|--------|-----------------|
| t01 delay_minutes | trivial | 75 | ✓ | frontier | openai/gpt-4.1 | $0.000508 | run-9c4f46d43de8 | 0b4683f5e34eac4520904678834d69e6 |
| t08 evidence_conflict | moderate | {"claim_status":"contradicted","verified_event":"passenger_no_show"} | ✓ | frontier | openai/gpt-4.1 | $0.000910 | run-7afb61e9e1cf | f2c4f2ad68a27221b4da9cc6cbcdb14f |
| t11 rebooking_option | hard | A | ✓ | frontier | openai/gpt-4.1 | $0.001474 | run-bab43e1364d6 | 01ab16b47f76a04ba171c561e41316fd |
| t15 trace_diagnosis | hard | correct JSON | ✓ | frontier | openai/gpt-4.1 | $0.002000 | run-68c6716d8b4e | b9a06554e75891217cd1e46cd8ac27b1 |

Full JSON for each run: [`evidence/part1/`](evidence/part1/)

### Jaeger Trace
Span tree for run-bab43e1364d6 (t11, hard multi-constraint rebooking). Duration 1.6s, 10 spans, depth 4:

![Jaeger span tree showing run → agent_loop_1 (plan, node recall) → agent_loop_2 (plan, node answer → chat openai/gpt-4.1) → agent_loop_3 (plan, finish)](evidence/jaeger_trace_t11_tree.png)

![Provider call span tags showing s15.cost, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens, gen_ai.provider.name, gen_ai.request.model](evidence/jaeger_trace_t11_tags.png)


### Honest Limitation

All four tasks routed to the frontier tier regardless of difficulty. The `role_tiers` mapping sends every `answer_with_evidence` node to frontier — routing is by graph role, not by task complexity. A trivial arithmetic task ($0.000508) used the same model as a hard multi-constraint reasoning task ($0.002000). The 4× cost difference comes purely from token count, not model selection. The budget policy never had a chance to downgrade because spend pressure never crossed 0.5 on any single run. This is the inefficiency Part 2 quantifies.

---

## Part 2: Measured Cost Per Call and Cost Per Resolved Task

### Strategy Comparison

p1_cost_per_task ran all 15 tasks through three strategies:

| Strategy | Total Spend | Calls | Resolved | Cost/Call | Cost/Resolved Task |
|----------|------------|-------|----------|-----------|-------------------|
| A: Always frontier | $0.00783 | 15 | 12/15 (80%) | $0.000522 | $0.000653 |
| B: Always cheapest (retries) | $0.00034 | 5 | 2/15 (13%) | $0.000069 | $0.000172 |
| C: Budget-aware cascade | $0.00348 | 19 | 13/15 (87%) | $0.000183 | $0.000267 |

**Strategy C (the cascade) wins on both axes.** It resolves the most tasks (13/15 — one more than frontier) at 59.1% lower cost per resolved task than always-frontier.

### Resolution by Difficulty

| Difficulty | A (frontier) | B (cheapest) | C (cascade) |
|-----------|-------------|-------------|-------------|
| trivial (5) | 4 | 2 | 4 |
| moderate (5) | 4 | 0 | 4 |
| hard (5) | 4 | 0 | 5 |

Strategy C resolved all 5 hard tasks. Strategy A missed one hard task (t15). Strategy B collapsed — 47 transport failures, most economy calls returned empty responses.

### Break-Even Resolution Rate

The break-even resolution rate is the point where the cheapest strategy's cost per resolved task equals the frontier's. Derived from the measured price spread and 3 retry attempts:

- **B vs A:** 26.2%. Strategy B would need to resolve at least 26.2% of tasks to become cheaper per resolved task than frontier. At 13%, B is far below break-even — it's just bad, not trapped.
- **B vs C:** 51.1%. Strategy B would need to resolve 51.1% of tasks to match the cascade's cost per resolved task.

**The signature failure mode (cheap model trap) was NOT observed.** The economy model's resolution rate (13%) was too low for the trap to appear. travel-ops task set is genuinely harder for small models.

### Where the Strategies Disagreed

Strategy C resolved t15 (disruption trace diagnosis) on economy/standard, while Strategy A **failed** on frontier. This was reproduced in a retest: frontier consistently fails this task while the cascade succeeds on standard. The frontier model over-explains the problem and produces verbose output that the judge scores poorly; standard gives a more direct answer.

Strategy C also resolved t12, t13, t14 (all hard tasks) on the economy tier in a single attempt — proving that some hard tasks don't need frontier at all.

### One Task the Policy Got Wrong

**t02_connection_validity** (trivial) failed across all three strategies. Root cause analysis:

1. **Cerebras rate limiting** (503, RPM quota burned) prevented the cascade's economy-tier attempt entirely on Strategy C's first attempt.
2. **Empty answer extraction** — even when the provider responded (Strategy A on frontier), the answer field was empty. The model returned verbose reasoning but p1's answer extraction produced an empty string. The same task succeeds through the full agent runtime (with the answer_with_evidence skill layer) because the skill's system prompt shapes the output.
3. **Judge panel split** on Strategy C's second attempt (gemini) — one judge scored it resolved, the other didn't. The `meets_expectation` criterion fell below the 0.5 floor.

**Cost wasted:** $0.000304 (A) + $0.000220 (B retries) + $0.000343 (C escalation) = $0.000867 across all strategies for zero resolved tasks.

This failure reveals two things: (a) rate-limiting on free-tier APIs is a measurement artifact that affects reproducibility, and (b) the skill layer (system prompts, output formatting) adds real value beyond model selection — the same model fails on a bare call but succeeds when wrapped in the agent's answer_with_evidence skill.

**t09_connection_failure_reason** (moderate) also failed across all strategies for similar reasons — empty answers on direct calls, successful through the full agent runtime.

Proof output: [`evidence/part2/p1_cost_per_task.json`](evidence/part2/p1_cost_per_task.json)

---

## Part 3: Adversarial Budget Tests

Four attacks against the budget policy, implemented in 
[`evidence/part3/p8_adversarial_budget_adversarial.json`](evidence/part3/p8_adversarial_budget_adversarial.json)


### Attack 1: Full Ladder Climb

Sent a hard task explicitly through each rung of the ladder (economy → standard → frontier).

| Tier | Cost | Provider/Model |
|------|------|---------------|
| economy | $0.000137 | cerebras/zai-glm-4.7 |
| standard | $0.000141 | gemini/gemini-3.1-flash-lite |
| frontier | $0.000788 | openrouter/openai/gpt-4.1 |

**Result:** All three tiers visited. Cost rises monotonically (5.7× from economy to frontier). The ladder is real — each rung routes to a different model at a different price. ✅

### Attack 2: Budget Exhaustion

Set a budget of $0.000038 — below the projected cost of a single economy call ($0.000528 projected).

**Result:** Zero calls admitted, one refusal. The controller refused all work rather than risking overspend. Spend: $0.00. The projection-based admission gate prevents calls whose worst case exceeds the budget. ✅

### Attack 3: Per-Node Call Ceiling

Gave a generous budget ($0.30) but called from the same `node_id` repeatedly, targeting `max_calls_per_node: 5`.

**Result:** 5 calls admitted, 6th refused. The call ceiling is independent of money — a retrying node cannot spin even with unlimited budget. ✅

### Attack 4: Provider Failure After Token Consumption (Known Gap)

**Documented, not demonstrated.** If a provider accepts a request, consumes tokens (incurring real cost), then returns a 5xx error, `MeteredTransport` records a failure with no charge. The budget ledger never sees the spend because `charge()` only runs on successful responses. The real money is gone at the provider but `budget.spent` is understated.

This is structural: the gateway reports token counts only on success, so the controller has no token count to price. **Mitigation:** monitor provider-side billing independently of the agent's ledger. 

Evidence output: [`evidence/part3/p8_adversarial_budget_adversarial.json`](evidence/part3/p8_adversarial_budget_adversarial.json)

---
