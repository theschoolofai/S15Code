# Limitations — S15 Part 1, "reproduce the floor"

**Measured 2026-08-07** against the live gateway (`glc_v4` on `127.0.0.1:8111`), the
S15Code runtime (`127.0.0.1:8113`) and Jaeger v2.20.0 (OTLP `4318`, UI `16686`).

Everything in §1 is derived from spans actually stored in the backend. Every
claim is marked **OBSERVED** (measured in these traces) or **INFERRED** (follows
from the attribute schema, but no run in this corpus exercised it).

---

## 0. The corpus these findings come from

```
service   s15code
traces    2
spans     20
```

| span kind | count | carries `s15.cost` |
|---|---|---|
| `plan` | 6 | 0 |
| `agent_loop` | 6 | 0 |
| `node` | 4 | 0 |
| `provider_call` | 2 | 2 |
| `run` | 2 | 2 |

Both traces are single-call, `tier=frontier`, `s15.budget.downgrades: 0`,
`s15.budget.refusals: 0`. **No downgrade and no refusal has been observed yet**,
which is why §1.5 and §1.7 are marked INFERRED. Running the evidence runs at
ceilings `0.01353475` (downgrade) and `0.00000056` (refusal) would move both.

Reference traces:

```
b9248434f7dd927d281558595e528445   10 spans  5813.0 ms  229in/104out  cost 0.00051180
d45b7c2c8382487b8b53bb77ad635edb   10 spans  6230.0 ms  229in/154out  cost 0.00069180
```

A representative `provider_call` span, in full:

```
gen_ai.operation.name             'chat'
gen_ai.provider.name              'nvidia'
gen_ai.request.model              'nvidia/nemotron-3-ultra-550b-a55b'
gen_ai.request.reasoning_effort   'off'
gen_ai.response.model             'nvidia/nemotron-3-ultra-550b-a55b'
gen_ai.usage.input_tokens         229
gen_ai.usage.output_tokens        154
s15.budget.decision               'proceed'
s15.budget.remaining              0.0193082
s15.cost                          0.0006918
s15.cost.projected                0.0149562
s15.currency                      'USD'
s15.node.id                       'answer'
s15.span.kind                     'provider_call'
s15.tier                          'frontier'
```

---

## 1. What the traces exposed

### 1.1 The trace cannot distinguish a truncated answer from a complete one — OBSERVED

`gen_ai.response.finish_reasons` is **absent from all 20 spans**. The
`provider_call` span records input tokens, output tokens, cost and reasoning
effort, but never why generation stopped.

A call that hit its `max_tokens` ceiling and truncated mid-sentence, and one that
finished cleanly on `end_turn`, are therefore **identical in the trace**: same
span kind, same attribute set, same full charge.

This is not hypothetical on this ladder. Probing the frontier rung with the
reasoning dial unset, at `max_tokens: 512`, over three samples:

| dial | output tokens | visible content | hidden reasoning channel |
|---|---|---|---|
| unset | 486 / 392 / 459 | 483 / 450 / 499 chars | 2070 / 1540 / 1693 chars |
| `off` | 95 | 598 chars | 0 |

The unset run peaked at **486 of 512 — twenty-six tokens short of the ceiling**.
One more verbose sample crosses it, and the trace would record
`output_tokens: 512`, a cost, and nothing else. That is the session's own
empty-string bug, and the observability layer is blind to precisely it.

**This is the limitation submitted for Part 1.** The trace bills you for a
truncation it cannot show you.

*Fix:* emit `gen_ai.response.finish_reasons` on every `provider_call` span. The
gateway already returns `stop_reason` in its response envelope, so the value is
present and simply not recorded.

### 1.2 Transport failures and retries leave no trace at all — OBSERVED

```
s15.retries       ABSENT       error           ABSENT
s15.attempt       ABSENT       error.message   ABSENT
spans carrying any error marker: 0 of 20
```

Two real transport failures occurred during this session's probing and neither
produced a span:

* NVIDIA `503 ResourceExhausted: Worker local total request limit reached (32/32)`
* Cerebras `429 We're experiencing high traffic right now`

A run in which the economy rung failed on a 429 and escalated to frontier shows
**one frontier `provider_call` and nothing else**. The failed attempt is absent,
so an escalation caused by provider capacity is indistinguishable in the trace
from an escalation caused by an unresolved answer.

This matters beyond observability. In `proofs/p1_cost_per_task.py`, a transport
exception is caught and passed to the judge as an errored answer, which is scored
unresolved, which advances the tier — so a 429 escalates a rung and is charged
for. The judge path retries transport failures (`evals.yaml`: `retries: 5`,
`retry_backoff_seconds: 15`); the answering path does not. The trace is where you
would normally catch this, and it cannot.

*Fix:* open a span for every attempt, successful or not, carrying `s15.attempt`
and the error class.

### 1.3 `s15.cost` carries two different meanings under one key — OBSERVED

| span | `s15.cost` |
|---|---|
| `provider_call` | `0.0006918` — this call's charge |
| `run` | `0.0006918` — the run **total** |

Same key, two semantics, no discriminator on the key itself. Summing every
`s15.cost` in a trace double-counts. This was not caught by reading the schema —
it was caught by writing a renderer that summed all four values in the corpus and
reported exactly twice the true figure. With a single provider call the two
numbers are numerically identical, so nothing warns you.

The only reliable disambiguator is filtering on `s15.span.kind == 'provider_call'`
or on the presence of `gen_ai.request.model`. `p4`'s reconciliation does this
correctly; a reader writing their own analysis will not necessarily.

*Fix:* name the run-level aggregate distinctly, e.g. `s15.run.cost_total`.

### 1.4 Latency is recorded but never decomposed — OBSERVED

The `provider_call` span is 6230 ms of a 6230 ms run. There is no
`s15.queue_ms` / `s15.generation_ms` split, so wait time and generation time are
indistinguishable.

Measured evidence that this is consequential: `z-ai/glm-5.2` on NVIDIA returned a
**one-word answer in 121 seconds**, and a three-sentence answer in 156 seconds.
Output length was irrelevant — it was almost entirely queue. In a trace that is
indistinguishable from a slow model doing real work, yet the two have opposite
remedies: change provider versus change model.

*Fix:* record time-to-first-token alongside total duration.

### 1.5 A downgrade is only half-visible — INFERRED

The `provider_call` span carries `s15.tier` (the tier **served**) and
`s15.budget.decision`, but **`s15.requested_tier` is absent**. The `run` span
carries `s15.budget.downgrades` as a bare count.

So a downgraded call can report *that* it was downgraded, but not *from what*.
"Asked frontier, served standard" — the sentence Part 1's second evidence run
exists to demonstrate — is only half-recorded on the span that would carry it.

Marked INFERRED: no downgrade has occurred in this corpus
(`s15.budget.downgrades: 0` on both runs).

*Fix:* add `s15.requested_tier` beside `s15.tier`.

### 1.6 `s15.tier` appears on spans that never called a provider — OBSERVED

The `node recall` span carries `s15.tier: 'economy'` with a 1.0 ms duration and no
usage attributes — it contacted no provider. Tallying tiers across all spans
yields `{economy: 2, frontier: 4}`, while only **2 provider calls** occurred, both
frontier.

Any per-tier count taken from spans rather than from `provider_call` spans
specifically will overstate activity, and will attribute economy-tier "usage" to
runs that never spent a cent at that rung.

### 1.7 A refusal leaves no span — OBSERVED

Confirmed directly by the Part 1 evidence runs. Two runs of the same graph on the
same prompt, differing only in ceiling:

```
1_comfortable  ceiling 0.02        spans=10  {plan:3, node:2, agent_loop:3, provider_call:1, run:1}  cost 0.000555
3_refusal      ceiling 0.00000056  spans= 9  {plan:3, node:2, agent_loop:3,                  run:1}  cost 0
```

Identical structure minus exactly the `provider_call` span. Admission runs before
the provider is contacted, so a refused call has nothing to attach a span to and
no ledger row, because nothing was billed.

The refusal *is* recorded — but only in the run's own `budget.refusal_log`, and
richly:

```json
{ "node_id": "answer", "action": "refuse", "requested_tier": "frontier",
  "reason": "cheapest tier economy projects 0.000489, run holds 0.000001 (headroom 0.000000)",
  "projected_cost": 0.0003891, "remaining": 0.00013445, "pressure": 0.7311 }
```

### 1.8 The two budget layers cannot see each other — OBSERVED

`GET /v1/refusals` on the gateway is described as the place a refusal becomes
visible. It does not see runtime refusals at all.

| | refusals recorded |
|---|---|
| S15 controller (`p3 --budget 0.0005`, 60 rounds) | **59** — `BudgetRefused` nodes + `refusal_log` |
| gateway `/v1/refusals` during that run | **0** |

The two controllers sit at different layers. S15's refuses *before* any HTTP
request is made, so the gateway never issues a 402 and its recorder sees nothing.
The gateway's own recorder works correctly when its own ceiling is armed — a
`POST /v1/budget` with `limit_usd: 0.00003` followed by an oversized call returns
402 with `projected_usd 0.0051203`, `remaining_usd 7.05e-06`,
`shortfall_usd 0.00511325`, and `/v1/refusals` then reports `total: 1`.

So an operator watching the gateway dashboard sees an untouched budget and zero
refusals while 59 refusals have occurred one layer up, in a different process.
That is the observability paradox with an extra turn: not merely that a refusal
leaves no span, but that the most likely dashboard is watching the wrong layer.

### 1.9 An unbudgeted run spends real money and reports none of it — OBSERVED

Worse than the refusal case, because a refusal correctly costs nothing.

```
4_unbudgeted   no ceiling   spans=9   provider_calls=0   cost=0
```

That run **succeeded**: the `answer` node reports `state=succeeded`,
`provider=gemini_1`, `model=gemini-3.5-flash-lite`, and it returned a 195-character
answer. A real billable provider call was made. The trace records **no
`provider_call` span** and **cost 0**, and there is no ledger row.

The cause is structural: charges are recorded by the budget controller, and
without a budget there is no controller in the path. Note also `tier=None` on that
node — the unbudgeted path bypasses the ladder entirely.

Every cost dashboard, every reconciliation, and every per-tenant attribution
silently excludes all unbudgeted traffic.

### 1.10 A run has no stable trace id — OBSERVED

`GET /v1/agent/runs/{id}/trace` re-exports the journal on every call and mints a
**new** trace id each time. The same run queried three times:

```
query 1: a449adf7d961ce853c6e396b06a34dd9
query 2: 3989002e11ace36bc25338f4f72b4442
query 3: 278556dc0dfea80f1d9afda584481ef2
```

Two consequences. There is no durable "the Jaeger trace ID" for a run to cite as
evidence — only *a* trace id from one particular export. And every view duplicates
the trace in the backend: after this session's evidence runs, Jaeger held **11
traces for 5 distinct runs**, with `run-0e4227fe8e3a` present in **5 copies**.

Anyone counting traces, or summing cost across traces, will over-count by however
many times someone looked.

---

## 2. What the traces got right

Content capture is genuinely off. Swept across all 20 spans:

```
gen_ai.input.messages     0/20
gen_ai.output.messages    0/20
gen_ai.prompt             0/20
gen_ai.completion         0/20
```

No prompt or completion text reached the backend. `p4` gates on this and passes.
Worth stating alongside the failures: the privacy guarantee holds.

---

## Appendix — Part 1 findings that did *not* come from traces

Recorded separately because the assignment asks specifically for what the traces
exposed. These surfaced while reproducing the floor.

### A. The frontier rung was retired the day the ladder was measured

`config/tiers.yaml` records its three rungs as measured on **2026-07-30**. GitHub
Models — which served the frontier rung, `openai/gpt-4.1` — was **fully retired on
2026-07-30**, after brownouts on 16 and 23 July.

Verified dead on 2026-08-06: **17 model IDs across 7 vendors** all returned
`HTTP 410 github_models_retirement_brownout`, as did `GET /catalog/models` and a
request naming no model at all. A deliberately invalid ID returned 410 rather than
404, proving the endpoint short-circuits before model resolution.

The ladder was benchmarked on the last day its top rung existed. Repointed to
`nvidia/nemotron-3-ultra-550b-a55b`; see the `tiers.yaml` header for the full
record and the rejected candidates.

### A2. A second retired model, on the path taken when no budget is given

`GEMINI_MODEL` defaults to `gemini-2.5-flash`. Google has retired it:

```
gateway /v1/chat returned 502: gemini HTTP 404:
"This model models/gemini-2.5-flash is no longer available to new users."
```

This only bites on the **unbudgeted** path. A budgeted run reads its model from
`tiers.yaml` (`gemini-3.5-flash-lite`) and works; an unbudgeted run has no tier —
see §1.9, `tier=None` — so it falls through to the provider default and 404s. The
whole run fails with `status: failed`.

The economics layer is therefore additive only where the provider defaults happen
to still be live. Removing the budget does not give you "the run as it was
before"; it gives you a different model-selection path with its own rot.

Two retired models in one week's measurement, on a repo whose config was written
2026-07-30. Setting `GEMINI_MODEL=gemini-3.5-flash-lite` fixes it, after which the
unbudgeted run reports `status: completed`.

*Also worth recording:* `glc_v4/.env` shipped with no trailing newline, so
appending a variable with `>>` silently concatenates it onto `GLC_PORT=8111`,
producing `GLC_PORT=8111GEMINI_MODEL=...`. Nothing warns; the port simply becomes
unparseable.

### B. The Jaeger fallback script cannot run on the platform it is needed for

`glc_v4/scripts/jaeger_local.sh` maps only `Darwin` and `Linux` and exits 2
otherwise. On Windows `uname -s` returns `MINGW64_NT-10.0-26200`, so the script
refuses — **despite Jaeger publishing a `windows-amd64` binary**. It also ships
with CRLF line endings, which break it under WSL bash (`\r: command not found`).

Additionally, `--status` and `--stop` only work for instances started with
`--start`, because both go through a pidfile that a foreground instance never
writes. Recovering from a stale foreground Jaeger requires `pkill`.

### C. Jaeger's query API is unreachable on the IPv4 loopback literal

Jaeger binds its query server to the IPv6 wildcard `[::]:16686`. WSL's port
forwarder does not relay that to Windows `127.0.0.1`, while the OTLP receivers
bound to `127.0.0.1:4318` forward correctly.

| endpoint | `127.0.0.1` | `localhost` |
|---|---|---|
| OTLP 4318 | HTTP 200 | HTTP 200 |
| query 16686 | unreachable | HTTP 200 |

Anyone using the IPv4 literal concludes Jaeger is down while it is running
normally.

**This is not cosmetic — it fails a shipped gate.** `p4` derives its query URL
from the OTLP endpoint's host, so with the documented
`--otel-endpoint http://127.0.0.1:4318/v1/traces` it queries
`http://127.0.0.1:16686` and the backend round-trip gate fails outright:

```
[PASS] spans were exported over the wire                    exported_over_the_wire=True
[FAIL] a real trace lands in the backend, fetched back by id
       URLError: <urlopen error [WinError 10061] ... actively refused it>
```

Export succeeds (4318 is IPv4-bound and forwards); only the read-back fails.
Workaround: `S15_TRACE_QUERY_URL=http://localhost:16686`.

### H. `p4`'s backend gates race the ingestion pipeline

With the query URL corrected, two gates still fail — and they fail
*non-deterministically*, which is the tell:

| run | spans in backend | gate |
|---|---|---|
| 1 | 8 of 10 | FAIL |
| 2 | 7 of 10 | FAIL |

```
[FAIL] the backend holds every span this process emitted   8 in backend == 10 emitted
[FAIL] the hierarchy survives the wire (parents read from the backend)
       [('agent loop 1', 'agent_loop', 'None'), ('agent loop 2', 'agent_loop', 'None')]
```

`p4` queries Jaeger immediately after exporting, with no wait and no retry. The
spans that had not yet been ingested were *parents*, which is why their children
read back with `parent=None` — the hierarchy gate fails as a direct consequence
of the completeness gate, not independently.

**Demonstrated to be a race, not a drop.** Re-querying trace
`a4e8282fa12d8c39bc6b67821c3d3213` a few seconds later returns all **10** spans
with every parent resolved:

```
run run-c2718bab3ee4                       2660.0ms
  agent loop 2 -> node answer -> chat nemotron-3-ultra-550b   229in/78out  cost 0.0004182
  agent loop 1 -> plan, node recall
  agent loop 3 -> plan
```

The other twelve gates pass on every run, including the substantive ones: span
costs sum to the ledger with `delta 0.000e+00`, token counts match, a real trace
is fetched back by id, and no prompt or completion text reached the backend.

*Fix:* poll the backend with a bounded retry before asserting completeness — the
export is asynchronous and the assertion is synchronous, so the gate is
unwinnable as written on any machine where ingestion takes longer than the
round-trip.

### D. Two shipped tests fail on a clean checkout

| test | cause |
|---|---|
| `tests/test_runtime_regressions.py::test_birthday_creates_two_real_calendar_artifacts` | `removeprefix("file://")` on `file:///C:/...` yields `/C:/Users/...` → `\C:\Users\...`, invalid on Windows. Correct on POSIX. |
| `s15code/core/a2a/tests/test_hardening.py::test_official_subscription_resumes_waiting_graph_and_maps_cancel` | A2A gRPC race: observed `TASK_STATE_WORKING`, expected `TASK_STATE_COMPLETED` |

Both reproduce against the pristine config extracted from git, so neither is
caused by the ladder changes in this repo.

### E. Observed test counts differ from the assignment's

| suite | observed | claimed | delta |
|---|---|---|---|
| `glc_v4` | 447 passed, 9 skipped | 445 | +2 |
| `agent_economics` | 275 passed, 2 failed | 244 | +31, 2 red |

### G. `p1` silently produces a simulation and reports ALL CHECKS PASSED

The most consequential finding of Part 2's setup, and the one closest in shape to
what this session is about: a green result that means nothing.

Two independent defects combine.

**The port default is inconsistent within the repo.**

```
proofs/p1_cost_per_task.py:73   DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8112")
proofs/harness.py:48            DEFAULT_BASE_URL = os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")
```

`p1` defaults to **8112**. Every other proof — and `glc_v4`'s own `GLC_PORT`,
and `S15Code`'s `.env.example` — uses **8111**. (The `127.0.0.1:8112` reference in
the course material is therefore not stale, as first assumed: it is `p1`'s real
default.)

**No proof loads `.env`.** `load_dotenv` is called only in `s15code/main.py`,
which the proofs never import. So `GLC_BASE_URL` set in `agent_economics/.env` is
never read on the proof path; only a shell export works.

**The combined effect.** Running `p1` with no `--base-url`, on a machine where
the gateway is healthy on its documented port, produces:

```
p1_cost_per_task  (offline mode)
transport {"reason": "http://127.0.0.1:8112 unreachable", "simulated": true,
           "warning": "offline numbers are a deterministic simulation, NOT evidence"}
...
[PASS] every strategy attempted every task in the file
[PASS] every answering provider call that returned is metered   108 transport calls, 108 ledger charges
... 8 of 8 gates green
ALL CHECKS PASSED -> proofs/out/p1_cost_per_task_wide.json
```

Exit code **0**. Every gate green. A full cost table, a break-even calculation,
and a `signature failure mode OBSERVED` finding — all of it simulated.

The output *does* say "offline mode" and does carry the warning, so this is not
concealment. But the exit code, the gate list and the JSON `"ok": true` all say
success, and a reader skimming for `ALL CHECKS PASSED` — or any CI job keying on
the exit code — sees a passing live measurement. The fallback is a convenience for
keyless CI; it should not be reachable by accident on a machine that has a
gateway.

*Fix, in descending order of value:*

1. Make `p1` import `DEFAULT_BASE_URL` from `harness.py` rather than redefining it.
2. Fail rather than fall back when `--offline` was not requested: an unreachable
   gateway on an explicitly live run is an error, not a mode change.
3. Record `mode` in the exit status, or gate on it, so `ok: true` cannot mean
   "simulated" on a run the caller asked to be live.

Workaround until then: **always pass `--base-url` explicitly**, and check the
`mode` line in the output before believing any number.

### F. The shipped semantic-cache pair set has no safe threshold

`p6` reports the labelled set is **not separable**: the highest-scoring
`different` pair (`diff03`, 1.0000) outscores the lowest-scoring `same` pair
(0.1826), so no threshold admits every paraphrase without also admitting at least
one wrong answer. At the configured 0.95: TPR 0.00, FPR 0.12 (2 of 16 wrong-answer
collisions). No threshold in the swept range 0.85–0.99 reaches zero collisions.
