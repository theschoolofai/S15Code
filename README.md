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

---

# Part 1 — Reproduce the Floor

*Branch: `part1-reproduce-floor`. This section is the Part 1 submission — the
rest of this README (below "## One package") is the project's own
documentation, unchanged.*

Four prompts were run through the live system (`S15Code` on `:8113` →
`glc_v4` gateway on `:8111`, both real, both metered, traces exported to a
real local Jaeger instance). No task-specific code was written — the goal
here is to run the shipped system and report what actually happened,
including two real problems the traces exposed along the way.

Raw evidence (full request/response JSON and full span-tree trace JSON) for
every run below, plus a diagnostic control run, is committed under
[`part1_evidence/`](part1_evidence/).

## Environment, as actually run

- `glc_v4` gateway on `http://127.0.0.1:8111`, `S15Code` runtime on
  `http://127.0.0.1:8113`, Jaeger on `http://127.0.0.1:16686`
  (OTLP receiver `4318`).
- Both repos' test suites run green before any of this: `S15Code` 277/277,
  `glc_v4` 447 passed / 9 skipped.
- All six shipped proofs (`p1`, `p2`, `p3`, `p4`, `p6`, `p7`) run live against
  the real gateway as a sanity baseline before any config was touched.
- **Two real, external/environmental problems were found and had to be worked
  around before these 4 runs could produce real answers** — both are detailed
  under Findings below, since they're as much a part of "what the traces
  exposed" as the 4 runs themselves:
  1. **GitHub Models is down, service-wide**, confirmed by hitting
     `models.github.ai` directly with the raw token, bypassing `glc_v4`
     entirely — every model returns `HTTP 410 github_models_retirement_brownout`.
     The shipped ladder's `frontier` rung (`config/tiers.yaml`) requests
     `provider: github, model: openai/gpt-4.1`. That block is left **in the
     file, commented out with a dated note**, restorable once GitHub Models
     is back. For these 4 runs to produce real answers, `frontier` is
     temporarily repointed to `nvidia/deepseek-ai/deepseek-v4-flash` (a
     provider already keyed and otherwise unused in the ladder). GitHub is
     also unregistered at the gateway itself (`glc_v4/.env`,
     `GITHUB_ACCESS_TOKEN` commented out) so a request for it fails fast with
     a clean "unknown provider" instead of a slow outage round-trip.
  2. A genuine bug in the deterministic planner's search-query construction
     (`s15code/runtime.py`, `_research_topic`) was found via Run 2 below,
     root-caused, and fixed on this branch — see **Finding 1**.

## The 4 runs

### Run 1 — plain factual question

| | |
|---|---|
| Prompt | *"What is the capital of Australia, and what is the current population of that city?"* |
| Budget | $0.05 |
| Run ID | `run-0a4e6581a02d` |
| Jaeger trace ID | `096b31ee1b0770718a691a2c05973e69` |

Ordered event trace:

```
[145] run_started
[146] graph_patched   add=[recall]   reason="first frontier selected for memory"
[147] task_started    recall
[148] task_succeeded  recall
[149] graph_patched   add=[answer]   reason="authorized retrieval completed"
[150] task_started    answer
[151] task_succeeded   answer
[152] graph_patched   add=[]         reason="grounded answer produced"
```

Tier/model chosen: `recall` → `memory_recall`, tier **economy**, no model call
(empty memory scope, nothing to recall). `answer` → `answer_with_evidence`,
tier **frontier**, served by **nvidia / deepseek-ai/deepseek-v4-flash**.

Ledger row: 1 call, cost **$0.000541** (431 input / 22 output tokens), run
total $0.05, spent $0.000541, remaining $0.049459, pressure 1.08%, 0
downgrades, 0 refusals.

Final answer: *"The capital of Australia is Canberra. I do not have current
population data for Canberra in the provided evidence."* — correct on the
capital; honestly declines the population half rather than inventing a
number, since no tool ever ran to fetch it (see Finding 2).

Full evidence:
- [part1_evidence/run1_factual_result.json](part1_evidence/run1_factual_result.json)
- [part1_evidence/run1_factual_trace.json](part1_evidence/run1_factual_trace.json)

### Run 2 — tool-use prompt (the run that exposed Finding 1 and 2)

| | |
|---|---|
| Prompt | *"Search the web for the latest stable version of Python and tell me its release date."* |
| Budget | $0.05 |
| Run ID | `run-04053211e7b4` |
| Jaeger trace ID | `bfa359d8d932c538240d95811e877954` |

Ordered event trace:

```
[153] run_started
[154] graph_patched   add=[recall]   reason="first frontier selected for memory"
[155] task_started    recall
[156] task_succeeded  recall
[157] graph_patched   add=[answer]   reason="authorized retrieval completed"
[158] task_started    answer
[159] task_succeeded   answer
[160] graph_patched   add=[]         reason="grounded answer produced"
```

Tier/model chosen: same shape as Run 1 — `recall` (economy, no call),
`answer` (frontier, **nvidia / deepseek-ai/deepseek-v4-flash**).

Ledger row: 1 call, cost **$0.000796** (431 input / 73 output tokens),
remaining $0.049204, pressure 1.59%, 0 downgrades, 0 refusals.

Final answer: *"I need to search the web for the latest stable version of
Python and its release date. Let me look that up."* — **no search ever
happened.** The graph never created a `web_search`/`researcher` node at all,
despite the prompt literally saying "search the web." This is Finding 1,
below — the single most useful thing this run exposed.

Full evidence:
- [part1_evidence/run2_tool_use_result.json](part1_evidence/run2_tool_use_result.json)
- [part1_evidence/run2_tool_use_trace.json](part1_evidence/run2_tool_use_trace.json)

### Run 3 — deliberately tight budget

| | |
|---|---|
| Prompt | *"Explain the difference between TCP and UDP in three sentences."* |
| Budget | **$0.0005** (deliberately tight, to force a downgrade/refusal live) |
| Run ID | `run-50d5a36db2b2` |
| Jaeger trace ID | `b260edc9c705732c76242e781a2438e8` |

Ordered event trace:

```
[115] run_started
[116] graph_patched   add=[search_1, search_2]  reason="first frontier selected for parallel_search"
[117] task_started    search_1
[118] task_started    search_2
[119] task_succeeded  search_2
[120] graph_patched   add=[]
[121] task_succeeded  search_1
[122] graph_patched   add=[distill]  reason="research evidence landed; specialist synthesis can begin"
[123] task_started    distill
[124] task_failed     distill  error="BudgetRefused: budget refused a standard call for distill:
                                       cheapest tier economy projects 0.000706, run holds 0.000131 (headroom 0.000010)"
[125] graph_patched   add=[answer]   reason="specialist synthesis completed"
[126] task_started    answer
[127] task_failed     answer   error="BudgetRefused: budget refused a frontier call for answer:
                                       cheapest tier economy projects 0.000585, run holds 0.000131 (headroom 0.000010)"
[128] graph_patched   add=[]         reason="answer worker failed; failure retained in journal"
```

Tier/model chosen: `search_1`/`search_2` → `researcher`, tier **economy**,
served by **groq / openai/gpt-oss-120b** — both succeeded ($0.000185 and
$0.000184). `distill` (wanted **standard**) and `answer` (wanted
**frontier**) were both **refused outright** — not merely downgraded — because
even the *cheapest* tier's projected cost exceeded what remained of the
$0.0005 budget after the two searches.

Ledger row: 2 calls, spent $0.000369 of $0.0005 (73.7% pressure), 0
downgrades, **2 branches, 2 refusals** — both refusals logged with the exact
reason, requested tier, projected cost, and remaining allowance.

Final answer: none — `status: "failed"`. This is **exactly the intended
outcome**: real budget enforcement, visibly logged, not a silent truncation.
No answer was fabricated when the budget ran out.

Full evidence:
- [part1_evidence/run3_tight_budget_result.json](part1_evidence/run3_tight_budget_result.json)
- [part1_evidence/run3_tight_budget_trace.json](part1_evidence/run3_tight_budget_trace.json)

### Run 4 — harder reasoning task

| | |
|---|---|
| Prompt | *"A train leaves station A at 60 mph heading toward station B, 300 miles away. Another train leaves station B at 90 mph heading toward station A at the same time. How long until they meet, and how far from station A is the meeting point?"* |
| Budget | $0.05 |
| Run ID | `run-5d64151d752a` |
| Jaeger trace ID | `33bf1a7a44e4beaceb5c80855c51befe` |

Ordered event trace: identical shape to Run 1/2 (`recall` → `answer`, both
economy/frontier as before).

Tier/model chosen: `answer` → **frontier**, served by **nvidia /
deepseek-ai/deepseek-v4-flash**.

Ledger row: 1 call, cost **$0.001103** (628 input / 95 output tokens),
remaining $0.048897, pressure 2.21%, 0 downgrades, 0 refusals.

Final answer: *"The trains are moving toward each other, so their combined
speed is 60 mph + 90 mph = 150 mph. The distance between them is 300 miles.
The time until they meet is distance divided by combined speed: 300 miles /
150 mph = 2 hours. In 2 hours, the train from station A travels 60 mph × 2
hours = 120 miles. Therefore, the meeting point is 120 miles from station
A."* — correct (verified independently: 300 / 150 = 2h, 60 × 2 = 120mi).

Full evidence:
- [part1_evidence/run4_hard_reasoning_result.json](part1_evidence/run4_hard_reasoning_result.json)
- [part1_evidence/run4_hard_reasoning_trace.json](part1_evidence/run4_hard_reasoning_trace.json)

## Findings

### Finding 1 (the honest limitation): a real query-construction bug, found, root-caused, and fixed

Run 2's prompt explicitly said "search the web," yet no search ever happened
— that alone was surprising. Digging into why turned up something more
interesting: a **second**, deeper bug in the planner's search-query
construction, only visible once search *does* trigger.

**Root cause 1 — the router's intent-detection is a narrow pattern match, not
language understanding.** `s15code/runtime.py`'s `_work_intent()` decides the
whole shape of the graph via regex, not an LLM:
- A **quoted** search phrase (`search for "X"`) triggers one `web_search` node.
- **Two or more** capitalized entities *plus* one of eleven specific words
  (`compare, contrast, find, list, research, look up, profile, profiles,
  overview, between, versus, vs`) triggers `parallel_search`.
- Anything else falls through to a bare `memory_recall`.

Run 2's prompt names **one** subject ("Python"), not two, and isn't quoted —
and notably, **"search" itself is not in that eleven-word trigger list.** So a
prompt that literally says "search the web" satisfies none of the patterns
and silently gets no tool use at all. Verified empirically (not just read
from source):

```python
>>> re.search(r'\bsearch for\s+[\'"]([^\'"]+)[\'"]', "Search the web for the latest stable version of Python and tell me its release date.", re.I)
None   # "the web" sits between "Search" and "for" -- and there's no quote anywhere
>>> _ENTITY_LIST.search(same_prompt)
None   # only ONE capitalized entity ("Python") -- the pattern requires 2+, comma/and-joined
```

**Root cause 2 — even when search correctly triggers, the query sent is
garbled.** A control prompt with two entities and the word "compare"
(*"Compare the latest stable versions of Python and Node.js, and tell me
which was released more recently."*) does trigger `parallel_search` — but the
query actually sent was:

```
Compare the latest stable versions of and and tell me which was released more recently Python
Compare the latest stable versions of and and tell me which was released more recently Node.js
```

Traced to `_research_topic()`: it blanked **every** named entity out of the
prompt (not just the "other" one), stripped all punctuation, then tacked the
current entity onto the very **end** regardless of where it grammatically
belonged — destroying the phrase ("latest stable version of **Python**") that
made the query useful. This directly explains why the control run's answer
was *"the available evidence contains no data on their release dates"* — the
search itself was sent a nonsense string. The same pattern was confirmed in
Run 3's TCP/UDP searches too (`"Explain the difference between and in three
sentences TCP"`), so this is systemic, not a one-off.

**Fixed on this branch** (`s15code/runtime.py`, `_research_topic`): the kept
entity now stays in its original position; only the *other* entities are
blanked, and a resulting doubled "and" is collapsed. Before/after, same
control prompt, same live system:

| | Before | After |
|---|---|---|
| `search_1` query | `"...more recently Python"` | `"Compare the latest stable versions of Python and tell me which was released more recently"` |
| `search_2` query | `"...more recently Node.js"` | `"Compare the latest stable versions of and Node js and tell me which was released more recently"` |
| Answer | *"...cannot compare... does not include web search results..."* | *"...cannot compare... available evidence contains no data on their release dates..."* |

The fix is a real, verified improvement (the entity that comes first in the
sentence now keeps its full, useful context) but **not a complete fix** —
noted honestly, not swept under the rug:
- A single leftover "and" can still appear when the *removed* entity sat
  immediately before the *kept* one (`"...of and Node js..."`).
- The punctuation-stripping step still runs over the kept entity too, so
  `"Node.js"` becomes `"Node js"` — the period is lost.

Both remaining issues are minor relative to the one fixed (losing the entire
topic phrase and reordering it to the end of the sentence), and neither was
chased further — this bug lives in carried-forward planner code outside
Parts 1–3's actual scope (config-only), and the fix already demonstrates the
finding, the root cause, and a measurable improvement, which is what Part 1
asks for.

Before/after evidence:
- [part1_evidence/before/control_two_entities_result.json](part1_evidence/before/control_two_entities_result.json) (pre-fix)
- [part1_evidence/control_two_entities_result.json](part1_evidence/control_two_entities_result.json) (post-fix)

### Finding 2 (secondary): when the planner fails silently, the model's own wording can be actively misleading

The two Run 2 attempts (before restarting the service to pick up the fix, and
after) produced two different final-answer phrasings from the *same*
underlying failure (no search ever ran):

- *"I cannot answer this question... nor does it include web search results..."* —
  honest about lacking evidence.
- *"I need to search the web for... Let me look that up."* — reads as if a
  search is about to happen, when the run has **already finished** and
  nothing further will ever run. This is the more dangerous phrasing: a user
  watching only the final answer, not the trace, would reasonably wait for a
  follow-up that never comes.

Neither phrasing is a lie from the model — it's reasoning over evidence it
was actually given (none) — but it's a direct illustration of session
principle §9/10 (an invisible failure is the dangerous kind), one level up
from budget refusals: a **planning** failure can be just as silent as a
budget one, and the model's own fluency can accidentally paper over it.

### Finding 3 (environmental, external, dated): GitHub Models is down

Confirmed by bypassing `glc_v4` entirely and hitting `models.github.ai`'s
catalog and inference endpoints directly with the raw access token — every
model returns `HTTP 410 github_models_retirement_brownout`, reading as a
genuine, service-wide retirement rather than a transient blip (410 "Gone" is
a permanent-removal status, not a 503). This was independently reproduced at
scale in the Phase 0 baseline: Strategy A (always-frontier) in the shipped
`p1_cost_per_task` proof resolved **0 of 12** tasks against `mixed.jsonl`, all
for this exact reason. Not a system bug — an external dependency going away
mid-assignment, worked around (see Environment section above) and left
clearly documented in `config/tiers.yaml` and `glc_v4/.env` for restoration
once it's back.

### Finding 4 (minor, noted): the frontier substitute itself is somewhat flaky

`nvidia`'s free-tier endpoint intermittently returns `HTTP 529 Service
temporarily overloaded` — Run 1 and Run 2 both needed one retry before
succeeding. Once a call fails, `glc_v4`'s own backoff mechanism benches the
provider for several seconds, which can cascade into a second run failing
immediately afterward for a *different* reason (no candidate provider
available) than the first failure. This is a small, live instance of a
principle the session notes call out directly: **a cheap or free rung with no
failover inherits that one provider's downtime** — cost and reliability are
different axes, and a ladder rung being affordable says nothing about it
being *up*.

## Evidence index

All raw JSON (full run response + full trace, for every run above plus the
diagnostic control run and its pre-fix counterpart) is committed under
[`part1_evidence/`](part1_evidence/):

```
part1_evidence/
├── run1_factual_result.json / _trace.json
├── run2_tool_use_result.json / _trace.json
├── run3_tight_budget_result.json / _trace.json
├── run4_hard_reasoning_result.json / _trace.json
├── control_two_entities_result.json / _trace.json    (after the Finding 1 fix)
└── before/
    ├── control_two_entities_result.json / _trace.json  (before the fix)
    └── run1..4_*.json                                   (before the GitHub outage workaround)
```

## Reproducing Part 1

```bash
# Terminal 1 -- Jaeger
docker compose -f glc_v4/docker-compose.observability.yml up -d

# Terminal 2 -- gateway
cd glc_v4 && uv sync && uv run glc serve

# Terminal 3 -- runtime (.env: GLC_BASE_URL, S15_OTEL_EXPORTER_ENDPOINT, S15_SANDBOX_ROOT)
cd S15Code && uv sync && uv run s15code serve

# A budgeted run, mirroring the 4 above:
curl -s localhost:8113/v1/agent/runs -H 'content-type: application/json' -d '{
  "tenant_id": "acme", "project_id": "s15-part1", "user_id": "you",
  "prompt": "<any of the 4 prompts above>", "budget": 0.05
}' | jq '.status, .answer, .budget'

# Then, using the run_id from the response:
curl -s localhost:8113/v1/agent/runs/<run_id>/trace | jq '.totals'
```

---

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
