# Part 1 — reproducing the floor

Everything below was produced against **live providers** on 2026-08-11, with the
gateway at `http://127.0.0.1:8111`, the runtime at `http://127.0.0.1:8113`, and
Jaeger v2 at `http://localhost:16686`. No proof ran in offline mode; each printed
`(live mode)` and named the gateway it used.

## Test suites

```
glc_v4     455 passed, 1 skipped
S15Code    277 passed
```

Both exceed the counts in the handout (445 / 244) because these forks carry newer
commits than the text was written against.

## The five proofs

All five pass live. Each writes machine-readable JSON to `proofs/out/`.

| Proof | Result | The number that matters |
|---|---|---|
| `p1_cost_per_task` | 9/9 checks | budget-aware is **51% cheaper per resolved task** than always-frontier |
| `p2_budget_holds` | 5/5 checks | 4 ceilings; tight → downgrade, impossible → refuse at **$0.00000000 spent** |
| `p3_denial_of_wallet` | 6/6 checks | 200 loop rounds, **5 admitted, 195 refused**, ceiling held |
| `p4_trace_export` | 15/15 checks | span costs vs ledger: **delta 0.000e+00** |
| `p7_cross_model_ladder` | 10/10 checks | 3 models, projected spread **26.2x**, measured **1.85x** |

### p1 in full

```
                    spend      calls  cost/call    resolved  cost/RESOLVED
A always-frontier   0.0060651   12    0.00050542    12/12     0.00050542
B always-cheapest   0.0029477   14    0.00021055    11/12     0.00026797
C budget-aware      0.0029643   13    0.00022802    12/12     0.00024702
```

The signature failure mode was **OBSERVED**, in the B-vs-C comparison:

```
B vs C    cost per call     -7.7%     B looks cheaper
          cost per resolved +8.5%     B is actually dearer
```

Break-even, derived from the measured price spread rather than assumed:

```
B vs A    break-even 68.2%   B resolved 92%   -> 23.5% headroom, B wins
B vs C    break-even 94.5%   B resolved 92%   -> below the line, B loses
```

One task decided it. `t04_dates` (moderate): B stayed on `economy` for three
attempts and never resolved it; C escalated `economy -> standard` and did, for
$0.0011977 — its most expensive task, and what bought 12/12.

## The four captured runs

Each run below records the prompt, the tier and model chosen, the ordered event
trace, the Jaeger trace id, the ledger rows, and the final answer.

> `proofs/out/` is gitignored (`.gitignore:14`), so the raw JSON is deliberately
> not part of this submission — it is regenerable output, and the section below is
> the evidence of record. Re-running the commands at the end of this document
> rewrites `proofs/out/part1_captured_runs.json` locally with the full answers.
> Trace ids will differ on a fresh run; Jaeger's storage is in-memory and does not
> survive a restart, so screenshot anything you need to keep.

Every run declared the role `answer_with_evidence`, which `config/tiers.yaml`
maps to the `frontier` tier.

| Run | id | status | tier / model | spent | trace id |
|---|---|---|---|---|---|
| 1 baseline | `run-b60977effcb1` | completed | frontier / gemini-2.5-flash | $0.0015644 | `efebbd99b7e27d516b343a7b57a4507a` |
| 2 long answer | `run-f1278d608225` | completed | frontier / gemini-2.5-flash | $0.0048218 | `fd20f5984451ae54e2db189239d0c940` |
| 3 refused | `run-85cc74fc6e9b` | **failed** | — none reached — | **$0.0000000** | `dd132e10eaff6b20812193ccbedebc56` |
| 4 moderate | `run-23a60c4c7daf` | completed | frontier / gemini-2.5-flash | $0.0009596 | `35570c2e343a56391e6a45203227bf41` |

All four traces are retrievable by id from the Jaeger query API.

### Ordered event trace

Runs 1, 2 and 4 (identical shape):

```
run_started -> graph_patched -> task_started(recall) -> task_succeeded(recall)
  -> graph_patched -> task_started(answer) -> task_succeeded(answer) -> graph_patched
```

Run 3 differs at exactly one event — `task_failed(answer)` in place of
`task_succeeded`. The refusal is an ordinary visible graph failure, not a silent
truncation.

### Run 3, the refusal, in detail

Budget $0.0000005. The controller priced the **cheapest** rung and still could
not fit it:

```
node          answer
requested     frontier
action        refuse
reason        "cheapest tier economy projects 0.000437,
               run holds 0.000000 (headroom 0.000000)"
calls         0
spent         0.0
```

The strongest evidence is in the span tree. Compare span kinds:

```
runs 1,2,4   {run:1, agent_loop:3, plan:3, node:2, provider_call:1}
run 3        {run:1, agent_loop:3, plan:3, node:2}          <- no provider_call
```

There is no provider-call span because no provider was contacted. The refusal
happened before the network, not after it.

### Answer length drives cost, not the model

All three successful runs used the same model on the same rung:

```
run 4   257 in / 353 out    961 chars    $0.0009596
run 1  1173 in / 485 out   2113 chars    $0.0015644
run 2   656 in / 1850 out  8291 chars    $0.0048218
```

A 5x cost range from one model, decided entirely by how much it wrote.

## One honest limitation the traces exposed

**Measuring the work cost more than doing the work.**

`p1`'s judge panel consumed 52 calls and **$0.01389100**. All three answering
strategies combined consumed **$0.01197700**. Grading was **16% more expensive
than everything it graded**, and it also drove 21 transport retries.

This is not a footnote about test overhead. The entire value proposition of a
routing policy is "spend less to get the same resolved tasks" — but knowing
whether you achieved that requires an LLM judge whose cost is the same order of
magnitude as the savings. A policy that saves 51% per resolved task, verified by
an apparatus costing more than the work, has not obviously saved anything at the
system level. Any honest cost-per-resolved-task figure should state whether
verification is inside or outside the boundary. Here it is outside.

### Three supporting observations

**1. Worst-case admission over-reserves by ~7x.** Every captured run projected
about $0.0107 and actually cost $0.0010–$0.0048:

```
run 1   projected 0.0107716   actual 0.0015644    6.9x
run 2   projected 0.0105400   actual 0.0048218    2.2x
run 4   projected 0.0103582   actual 0.0009596   10.8x
```

The projection is a true upper bound, which is what makes refusal reliable — but
it means the controller will refuse calls that would comfortably have fit. On a
$0.02 ceiling this reserves half the allowance for a single call.

**2. Designed spread 26.2x, measured spread 1.85x.** On short tasks every rung
emits a similar short answer, so the ladder's cost separation nearly vanishes.
Break-even is ~3.8% computed from projected costs and ~54% computed from measured
ones. Only tasks with substantial answers recover the design spread — as run 2
demonstrates.

**3. The floor demonstrates mechanism, not pressure.** Every captured run
produced a two-node graph and exactly one billed call. Budget re-division across
a live frontier, cascade escalation and multi-node contention are all exercised by
the proofs but not by a default run.

## Deviations from the handout, and why

**The frontier rung was replaced.** The shipped ladder's rung 3 was
`github / openai/gpt-4.1`. GitHub Models now returns HTTP 410
`github_models_retirement_brownout` for every request, so it cannot serve. Free
Gemini has no quota for `gemini-3.1-pro` either (429). Rung 3 is now
`gemini / gemini-2.5-flash`, priced from glc_v4's own table so the ledger and
config cannot disagree.

An intermediate attempt, `cerebras / zai-glm-4.7`, **failed** `p7` and is recorded
in `config/tiers.yaml` rather than deleted: its output rate (0.50) sits below the
economy rung's (0.75), so the measured ladder came out inverted at 0.59x. A rung
that costs less than the one beneath it is not a frontier.

**Three commands in the handout silently do nothing.** Each returns success:

| Handout says | Reality |
|---|---|
| `export GLC_OTEL_EXPORTER_ENDPOINT=…` | no code reads it; the real name is `OTEL_EXPORTER_OTLP_ENDPOINT` (`glc/telemetry/otel.py:27`). The exporter is a deliberate no-op when unconfigured, so traces vanish with no error. |
| `{"budget_usd": 0.02}` on `/v1/agent/runs` | the field is `budget`. `RunBody` does not forbid extra keys, so `budget_usd` is discarded and **the run executes with no ceiling**, returning HTTP 200. Verified: `budget: null` in the response. |
| `proofs/*.py` without `--base-url` | proofs default to port **8112**; the gateway is on 8111. `harness.py:161-164` silently substitutes a deterministic offline transport when the gateway is unreachable, so the proof passes with fabricated numbers and no trace. |

That a system whose stated premise is *"enforcement is structural, not
aspirational"* accepts an unbudgeted run without complaint is itself worth noting.

## Reproducing this from a fresh checkout

```bash
# 1. Jaeger. No Docker needed; this is the project's own fallback.
glc_v4/scripts/jaeger_local.sh --start

# 2. Gateway. Keys go in glc_v4/.env — never committed.
cd glc_v4 && uv sync && uv run glc serve          # 8111

# 3. Runtime.
cd S15Code && uv sync && uv run s15code serve     # 8113

# 4. Suites.
cd glc_v4  && uv run pytest -q
cd S15Code && uv run pytest -q

# 5. The five proofs. --base-url is REQUIRED or they run offline silently.
cd S15Code
B=http://127.0.0.1:8111
uv run python proofs/p7_cross_model_ladder.py --task "<task>"          --base-url $B
uv run python proofs/p2_budget_holds.py       --task "<task>" --budget 0.02  --base-url $B
uv run python proofs/p3_denial_of_wallet.py   --task "<task>" --budget 0.002 --base-url $B
uv run python proofs/p4_trace_export.py       --task "<task>" --budget 0.02  --base-url $B \
    --otel-endpoint http://127.0.0.1:4318/v1/traces
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/mixed.jsonl --base-url $B

# 6. A captured run. Note `budget`, not `budget_usd`.
curl -s -X POST http://127.0.0.1:8113/v1/agent/runs -H 'Content-Type: application/json' \
  -d '{"prompt":"<task>","budget":0.02,"principal":"course/s15/you",
       "tenant_id":"course","project_id":"s15","user_id":"you","agent_id":"assistant"}'

# 7. Push that run to Jaeger and get its trace id. Runs do NOT export on their own.
curl "http://127.0.0.1:8113/v1/agent/runs/<run_id>/trace?endpoint=http://127.0.0.1:4318/v1/traces"
```

## Cost of the whole exercise

```
153 gateway calls    $0.048193
```

All on free provider tiers, so nothing was billed. Every dollar figure in this
document is computed from `pricing.yaml` rate cards against real reported token
counts — the control logic is real, the invoice is not.
