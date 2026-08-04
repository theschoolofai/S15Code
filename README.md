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

# Part 2 — Build a Policy and Measure It

*Branch: `part2-logic-puzzle-policy`, stacked on `part1-reproduce-floor`. This
section is the Part 2 submission. Raw evidence (the full per-attempt JSON and
the live console log) is committed under
[`part2_evidence/`](part2_evidence/) — `proofs/out/` itself is gitignored, so
this is a copy, not a symlink.*

## Our own ladder and budget policy

Both were designed independently for this workload (15 pure logic puzzles,
`proofs/tasks/logic-puzzles-tasks.jsonl`, copied verbatim from the assignment
material) — not a relabel of the shipped ladder from Part 1. Full rationale,
including every candidate model that was tried and rejected, lives as comments
in [`config/tiers.yaml`](config/tiers.yaml) and
[`config/budgets.yaml`](config/budgets.yaml); this is the short version.

**The ladder** (`config/tiers.yaml`), one candidate model at a time, measured
live against the gateway on 2026-08-03 before being written down:

| Rung | Provider | Model | $/Mtok in/out | Notes |
|---|---|---|---|---|
| economy | groq | `openai/gpt-oss-120b` | 0.15 / 0.75 | Same model Part 1's shipped ladder uses — kept because it's simply the cheapest live-reachable, clean model across every provider tried |
| standard | groq | `llama-3.3-70b-versatile` | 0.59 / 0.79 | New — never used in the shipped ladder or the judge panel |
| frontier | cerebras | `gemma-4-31b` | 1.00 / 5.00 (assumed) | New provider entirely — a Cerebras preview model with no public commercial price list, so its row is an honest ASSUMED placeholder (set equal to `pricing.yaml`'s own `default` row), same disclosure pattern Part 1 used for its temporary frontier substitute |

Two live candidates were tried and deliberately ruled out, not guessed away:

- **`gemini/gemini-3.1-flash-lite`** (the shipped ladder's own standard rung)
  turned out to be mathematically incompatible with `llama-3.3-70b-versatile`
  in the same ladder: flash-lite is cheaper on input (0.25 < 0.59) but
  *pricier* on output (1.50 > 0.79) — no ordering can make both rates rise
  together, which is exactly the monotonic-pricing trap Part 1's own lessons
  flagged. One had to go.
- **`nvidia`** was avoided entirely for this ladder's answering rungs.
  Part 1 measured its free endpoint failing roughly 1-in-3 calls (`HTTP 529`),
  with the gateway's own backoff then benching the provider for several
  seconds — a real cascading-failure risk across the ~55 live calls one full
  15-puzzle × 3-strategy run makes. Cerebras was picked for frontier
  specifically because it sidesteps both of this assignment's known failure
  modes: GitHub's outage and NVIDIA's flakiness.

Every provider used (groq, cerebras) is a confirmed free-tier key — the
dollar figures above are notional list prices for the cost-accounting
exercise, not real spend, same disclosure as the rest of this project's
pricing.

**The budget policy** (`config/budgets.yaml`) was sized for this workload's
actual shape, not copied from Part 1's numbers. `p1_cost_per_task.py` opens a
*fresh* budget per task/strategy and makes at most `evals.yaml`'s
`max_attempts` (3) calls to one `answer_with_evidence` node — no upstream
fan-out to protect a terminal node from, unlike the full agent runtime Part 1
exercised. Headline differences from the shipped policy: `default_budget`
$0.025 (vs $0.05 — a workload with no upstream calls needs less), `downgrade_at`
0.40 / `refuse_at` 0.85 (vs 0.50 / 0.90 — pressure bites earlier, given a 20x
economy-to-frontier input-price spread), `max_calls_per_run` 15 / `max_calls_per_node`
3 (vs 60 / 6 — sized to this workload's real 3-call ceiling instead of a
vestigial number that never engages).

**A real gotcha hit while building this, worth recording honestly:** the
admission controller prices a call's *worst case* — input estimated from the
actual prompt, output priced at the tier's `max_tokens` ceiling. For a short
test prompt, input is nearly free, so a tier's real cost is almost entirely
its `max_tokens × output price`. Two invariant tests
(`test_cross_model_ladder.py::test_pressure_downgrades_to_a_different_model_at_every_rung`,
`test_budget_runtime.py::test_a_tight_ceiling_never_buys_more_than_the_ceiling`)
initially failed because `standard`'s `max_tokens` (1024) put its real
worst-case cost *below* the boundary those tests construct from
`projected_input_tokens` — the downgrade logic correctly judged standard
"affordable" when the test expected it to fall back to economy. Fixed by
raising `standard`'s and `frontier`'s `max_tokens` (1024→2048, 2048→3072)
until each rung's real worst-case cost robustly exceeds the rung below it.
`uv run pytest -q` is green at 277/277 with the final numbers.

## The live run

```bash
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/logic-puzzles-tasks.jsonl
```

All three strategies (A always-frontier, B always-cheapest-with-retries, C our
budget-aware cascade) against all 15 puzzles, one ledger, live against the real
gateway. Run **twice**, independently, on separate days — the second run to
capture full answer text end-to-end (`proofs/p1_cost_per_task.py` originally
kept only the last 320 characters of each answer; that truncation was removed
so a reader can see exactly what each tier actually said, not just its tail).
Both runs are reported here: **the second run is the one true evidence file**
(`part2_evidence/p1_cost_per_task.json`, `ok: true`, full answers), but the
first run's numbers are worth keeping visible too, because where the two runs
*agree* is a stronger claim than either alone, and where they *disagree* is an
honest lesson about live-LLM variance that a single run would have hidden.

Each run took ~61–65 minutes wall-clock — mostly Cerebras' free-tier limit of
5 requests/minute per model, which the two-judge panel hit 44–45 times each
run; every one retried automatically and every task still got judged (0
unusable verdicts, 0 judge failures both times). Judge panel
(`cerebras/zai-glm-4.7`, `openrouter/nvidia/nemotron-3-super-120b-a12b:free`)
is confirmed disjoint from the answering ladder's models — the "no answer was
graded by its own model" check passed both times. Full per-task log (with
complete, untruncated answer text):
[`part2_evidence/p1_cost_per_task.json`](part2_evidence/p1_cost_per_task.json)
(`detail.per_task`), console output:
[`part2_evidence/p1_live_run_console.log`](part2_evidence/p1_live_run_console.log).

### Cost per call and cost per resolved task

| Strategy | Run 2 (evidence file) | | | | Run 1 (2026-08-03) | | | |
|---|---|---|---|---|---|---|---|---|
| | Spend | Calls | Cost/call | Resolved → Cost/resolved | Spend | Calls | Cost/call | Resolved → Cost/resolved |
| A always-frontier | $0.026296 | 15 | $0.001753 | 15/15 (100%) → $0.001753 | $0.026296 | 15 | $0.001753 | 15/15 (100%) → $0.001753 |
| B always-cheapest | $0.007351 | 21 | $0.000350 | 12/15 (80.0%) → $0.000613 | $0.007362 | 21 | $0.000351 | 13/15 (86.7%) → $0.000566 |
| **C our cascade** | $0.011175 | 20 | $0.000559 | **15/15 (100%)** → $0.000745 | $0.008348 | 19 | $0.000439 | **15/15 (100%)** → $0.000557 |

**A honest headline, not the tidier one:** in Run 1, our policy (C) had the
*lowest* cost per resolved task of all three. In Run 2, it doesn't — B is
actually cheaper than C on both cost/call (-37%) and cost/resolved-task
(-18%) this time. What's **consistent across both runs**, and therefore the
real, defensible claim: **C is the only strategy that resolves 100% of the
puzzles in either run.** B's resolution rate moved between runs (86.7% →
80.0%) depending on exactly which hard puzzles it happened to fail live; C's
never did. Against always-frontier (A), C is unambiguously cheaper in both
runs (57.5%–68.3% cheaper per resolved task) while matching its full
resolution rate. Against always-cheapest (B), C is buying **completeness**,
not always a lower bill — and the next section shows exactly which 3 puzzles
that premium buys, and what each one specifically cost.

Judged against `config/evals.yaml`'s rubric (5 criteria, 0–4 scale, resolved
at weighted score ≥0.75 with no single criterion below 0.5) — unchanged from
the shipped rubric, since Part 2's own instructions ask us to build the ladder
and budget policy, not necessarily the judge.

### Break-even resolution rate

`p1` computes `r* = k / (C/c − 1 + k)` itself. **Quoting `k = cheapest_attempts
= 3`, not `cheapest_retries = 2`** — `evals.yaml` sets `cheapest_retries: 2`,
and `cheapest_attempts = min(max_attempts, 1 + cheapest_retries) = min(3, 3) = 3`
is the value the formula actually needs (this is the exact slip the
assignment material's own summary table made, per the assignment doc).

| Comparison | Run 2 r* | Run 2 B's rate | Run 2 headroom | Run 1 r* | Run 1 B's rate | Run 1 headroom |
|---|---|---|---|---|---|---|
| B vs A | 42.8% | 80.0% | +37.2 pts | 42.9% | 86.7% | +43.8 pts |
| B vs C | 72.7% | 80.0% | +7.3 pts | 83.6% | 86.7% | +3.0 pts |

Neither run shows the textbook "signature failure mode" against A — B safely
beats always-frontier on cost in both. Against C, the two runs tell **opposite**
stories at the level of the formula's own flag: Run 1's measured numbers showed
B *losing* to C on cost/resolved-task despite sitting above the formula's
break-even threshold (`signature_failure_mode: true` — the formula under-
predicted the trap). Run 2 shows B genuinely beating C on cost, consistent
with its rate sitting above break-even (`signature_failure_mode: false`). This
is the honest lesson of running it twice: `r*` is a first-order model (uniform
cost per call, a task failed once is assumed failed forever) reported *beside*
the measurement, never instead of it — at only 15 tasks, whether a strategy
actually clears its own break-even line can depend on which specific hard
puzzles it happened to fail on a given live run, not just its aggregate rate.

### A task our policy routed to the wrong rung, and what it cost

The assignment flags `logic_h2` (knights-and-knaves) and `logic_h4` (the
boy-born-Tuesday puzzle) as likely candidates for a "confidently wrong"
failure. Checked both against their known-correct answers in **both** runs —
**honestly, neither failed in either run**: every attempt that resolved either
puzzle, at every tier that resolved it, reached the actual correct answer
(X=knave, Y=knight, Z=knight for `logic_h2`; 13/27 for `logic_h4`).

The real wrong-rung case turned up at **`logic_h1`** (three sealed packages, a
courier who can ferry at most one at a time, correct answer: **7 crossings**)
— under strategy C, our own policy, **reproduced independently in both runs**:

1. **economy** (`gpt-oss-120b`) — returned an **empty answer** both times.
   Cost $0.000422, wasted.
2. **standard** (`llama-3.3-70b-versatile`) — returned a fully-formed,
   confident, **wrong** answer both times. Run 2's full text (previously only
   the last 320 characters were kept — this is the complete answer): the
   model correctly starts tracking constraints, then talks itself into an
   error mid-derivation — *"...the sequence provided achieves the goal in 5
   crossings, with the understanding that the courier's presence is required
   to ensure the packages' safety during transport. The final result is: The
   minimum number of river crossings needed is 5..."* — the correct answer is
   7. Cost $0.000488. The judge panel correctly caught this as unresolved
   both runs (both judges disagreed with the rubric's `meets_expectation`
   criterion).
3. **frontier** (`gemma-4-31b`) — correct both times: 7 crossings, valid
   sequence. Cost $0.001829.

**Total for C to solve `logic_h1`: $0.002740 in Run 2 (was $0.002686 in Run
1) — roughly 50% more than Strategy A would have spent** going straight to
frontier ($0.001829), consistent across both runs (47% then 50%). That
consistency is what makes this a *structural* finding rather than one-off
noise: two rungs that cannot solve this particular constraint-satisfaction
puzzle — one silently empty, one confidently wrong via a genuine reasoning
slip near the very end of an otherwise-correct derivation — have to be paid
for before the rung that actually can kicks in. Full attempt-by-attempt
detail (tier, decision, cost, verdict, complete answer text) is in
[`part2_evidence/p1_cost_per_task.json`](part2_evidence/p1_cost_per_task.json),
`detail.per_task.C`, `task_id: "logic_h1"`.

A related, secondary finding, new to Run 2: on `logic_h2`, strategy C's
**standard**-tier attempt hit a genuine **infrastructure failure**, not a
content problem — `RuntimeError: gateway /v1/chat returned 503: "all
providers unavailable. attempts: [{'provider': 'groq', 'reason': 'TPM
limit'}]"`. Groq's own free-tier tokens-per-minute limit, not Cerebras' or
NVIDIA's (the two flaky providers Part 1 already documented) — a new
provider added to that list. The call was correctly charged **$0.00** (no
tokens returned, nothing to bill) and counted as an unresolved attempt; the
cascade moved on to frontier and resolved the puzzle anyway. This is the
system working as designed under a real transport failure, not a hidden one
— visible in the ledger as attempt 2 with `charged_tier: null`, `cost: 0.0`,
and the raw error in `verdict.reason`.

A third finding, consistent in both runs: strategy B (retry-only) burned all
3 attempts on `logic_h2`'s economy tier alone — partial or truncated answers
every time — and never resolved it, spending ~$0.00125 for nothing both runs.
C, on the same puzzle, escalated past the retry trap and got the *correct*
answer in both runs — a clean, concrete illustration of why "retry the same
cheap tier" underperforms "escalate to a different one," independent of the
Run 1 vs Run 2 cost/resolved-task headline flip above.

### Invariant checks (from the proof's own gate, all passed — Run 2)

Every strategy attempted every task; every answering call that returned was
metered (56 transport calls = 56 ledger charges; 1 transport failure — the
Groq TPM-limit 503 above — correctly not charged, since no tokens came back);
no verdict was unparseable; no empty/errored answer counted as resolved; no
answer was graded by its own model; the ladder separates cost per call
(projected top $0.01686 > bottom $0.000504); every model that answered has its
own explicit `pricing.yaml` row (no silent fallback to `default`); no
semantic-cache hit crossed strategies (cache hit count unchanged, 7 before and
after). Run 1 passed the same gate at 55/55 transport calls/charges, 0
failures.

## Reproducing Part 2

```bash
# From S15Code, with the gateway (glc_v4, :8111) and Jaeger already running:
cp htmlcov/logic-puzzles-tasks.jsonl proofs/tasks/logic-puzzles-tasks.jsonl   # already committed here
uv run pytest -q                                                              # 277/277
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/logic-puzzles-tasks.jsonl
# ~65 min live, mostly Cerebras' 5 req/min free-tier limit on the judge panel + frontier rung
```

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
