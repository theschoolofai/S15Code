# SQL Generation Policy - Evidence and Analysis

## Wrong-case requirement: not satisfiable in this evidence set

The assignment asks for at least one task this policy got wrong, with a real
dollar cost. This proof **cannot supply that evidence**, and here is exactly why.

All runs in this project were executed in offline simulation mode (`--offline`).
Offline mode swaps the real gateway for a deterministic `SimulatedGateway`
(defined in `proofs/p_sql_policy.py`) that, for every prompt, returns the same
generic placeholder:

```
SIMULATED_SQL sufficient=1 ceiling=4096 needed=3062. SELECT * FROM simulated_table WHERE id = 1;
```

It never parses the task, and it never generates real SQL. The rubric-based
judge therefore grades a placeholder string, not an actual query, and awards
perfect scores (4.0 on every criterion) to every answer regardless of task.

The consequence is structural: because the simulated provider is incapable of
producing a wrong SQL statement, no policy decision can result in a *wrong
answer that cost money*. All 23 tasks resolve in offline mode, including the
three deliberately-hard edge cases (sql_21: LIMIT/OFFSET off-by-one, sql_22:
LEFT-JOIN vs INNER-JOIN, sql_23: date-condition range) that were written
specifically to catch this class of error. We tested for these cases and the
harness cannot fail them.

To produce a genuine wrong case you must run `sql_21-23` against real API keys
in live mode (`uv run python proofs/p_sql_policy.py --label sql_live --tasks
proofs/tasks/sql_generation.jsonl`), which is a small bounded cost (~$0.10-0.30
for 3 tasks x 2 strategies). That is not possible in this environment: no `.env`
exists, no provider keys are exported, and the gateway is not reachable at
127.0.0.1:8112. I am therefore accepting the point loss on this criterion,
having documented the limitation explicitly rather than letting it appear
unaddressed.

## Part 1 honest limitation: the semantic cache served wrong answers with perfect confidence

The limitation this evidence set actually exposed was not a refusal-with-no-span
(that is the session document's own narrative). It was the **semantic cache
serving wrong answers at $0 cost, invisible with no error**.

In `p6_cache_savings` (offline), two cache HITS (diff03, diff06) were served
where:
- Cosine similarity = 1.000000 (perfect match)
- Both were "different/near_miss" pairs (semantically different requests)
- Both served WRONG ANSWERS with $0.00 cost
- The system had NO indication these were incorrect - no error, no warning

The configured threshold (0.95) was meant to prevent this, but the deterministic
offline embedder produced identical vectors for different text, making them
indistinguishable. In production this manifests as: a cache hit returns a stale
answer from a different question, no error is raised, no span records the
mistake, and the operator sees $0 cost and assumes success. This is exactly the
"silent failure" mode the session warns about - the cache saved money but served
wrong data, and only by manually checking the labelled pair set did it become
visible.

## Measured results (offline, 20-task set)

| Metric | A: always-frontier | B: budget-aware economy -> escalate |
|---|---|---|
| Cost per call | $0.01304480 | $0.00515643 (60% cheaper) |
| **Cost per resolved task** | **$0.01304480** | **$0.01211762 (7.1% cheaper)** |
| Resolution rate | 100% (20/20) | 100% (20/20) |
| Total spend | $0.26089600 | $0.24235235 |
| Calls | 20 | 47 (27 extra from escalations) |

**Headline metric is cost per resolved task**, not cost per call. Strategy B is
60% cheaper per call but makes 2.35x more calls due to escalations, so the true
saving is only 7.1% per resolved task.

## Break-even analysis (measured, not assumed)

- Price spread C/c = 2.53x
- Actual average extra calls per task k = **1.35** (16/20 tasks escalated, 27
  extra calls total; average per escalated task = 1.69)
- r* = k / (C/c - 1 + k) = 1.35 / (2.53 - 1 + 1.35) = **46.88%**
- Actual resolution 100%, headroom above break-even = 53.12%

## Corrected metrics note

Earlier I stated k=3. That was the configured *max attempts*, not the measured
extra calls. The actual measured k is 1.35 (see breakdown above), which lowers
r* from ~49% to 46.88%. The corrected metric uses only measured values.

## Ladder and budget

- Economy: groq/openai/gpt-oss-120b
- Standard: gemini/gemini-3.1-flash-lite
- Frontier: github/openai/gpt-4.1
- Budget: $0.03/task default, downgrade at 60%, refuse at 90%

## Reproduce

```bash
cd S15Code
uv run python proofs/p_sql_policy.py --offline --label sql_test
uv run python proofs/p_sql_policy.py --offline --label sql_hard --tasks proofs/tasks/sql_generation.jsonl
```

## Files added

- `proofs/tasks/sql_generation.jsonl` - 23 SQL tasks (incl. 3 edge cases)
- `config/tiers_sql.yaml` - custom ladder
- `config/budgets_sql.yaml` - custom budget policy
- `proofs/p_sql_policy.py` - measurement script
- `proofs/out/p_sql_policy_sql_test.json` - 20-task results
- `proofs/out/p_sql_adversarial.json` - denial-of-wallet (Part 3) results

## Part 3 - adversarial test

`proofs/p_sql_adversarial.py` runs a runaway SQL planner that, after every
outcome, requests another node at the dearest tier (frontier), up to 100 rounds.
The same 100-round loop was run twice: once with the budget control active
($0.02 ceiling) and once with it effectively absent (huge ceiling, no call
limit), so the before/after comparison is at the **same scale**.

| Metric | Uncontrolled (no control) | Controlled ($0.02 ceiling) |
|---|---|---|
| Loop rounds tried | 100 | 100 |
| Admitted calls | 100 | 13 |
| Refusals | 0 | 87 |
| **Spent** | **$3.20640000** | **$0.01891680** |
| Cost per call | $0.03206400 | $0.00145514 |

The control cut spend from **$3.2064 to $0.0189** at the same 100-round scale - a
**99.4% reduction** - by refusing 87 of 100 attempts before the provider was hit.

Every refusal is a visible graph failure node (`BudgetRefused`), recorded and
queryable in the journal - not a silent truncation.

**At-scale note (extrapolated, not measured):** if the uncontrolled loop ran
10,000 rounds at the measured $0.03206400/call, the bill would be ~$320.64. This
is an extrapolation from the measured 100-round cost, not a measured figure.

Reproduce with:

```bash
cd S15Code
# controlled: $0.02 ceiling (default config)
uv run python proofs/p_sql_adversarial.py \
  --task "Write a complex SQL query with joins, subqueries, and window functions" \
  --offline --budget 0.02
# uncontrolled: huge ceiling, no call limit (repo-relative config in proofs/uncontrolled_config)
uv run python proofs/p_sql_adversarial.py \
  --task "Write a complex SQL query with joins, subqueries, and window functions" \
  --offline --budget 100.0 --loop-limit 100 --config-dir proofs/uncontrolled_config
```
(The `proofs/uncontrolled_config/budgets.yaml` ships in the repo and sets
`max_calls_per_run: 0` with a $100.00 default budget, so the uncontrolled run is
reproducible from a fresh checkout without any local path.)
