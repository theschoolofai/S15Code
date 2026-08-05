# Part 3 — Attack Your Own Budget

Target: **the Part 2 policy** ([`config/part2`](../config/part2)), not the shipped one.
Harness: [`proofs/part3_attack.py`](../proofs/part3_attack.py) (written for this part).
Log: [`evidence/logs/part3_attack.json`](../evidence/logs/part3_attack.json) — all
four attacks with their budget snapshots, refusal logs and span totals.
Executed 2026-08-03, live against glc_v4, spans exported to Jaeger.

Part 2's measurement, which this attacks, is in the
[repository README](../README.md#evidence--a-budget-aware-policy-for-single-defect-code-review).

```bash
uv run python proofs/part3_attack.py --budget 0.004 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
```

Four attacks, each driven through the **real** controller, live graph, journal
and telemetry. Nothing is mocked except the provider in A4, which is the point
of A4.

| | Attack | Result |
|---|---|---|
| A1 | ask for a rung the ceiling cannot buy | refused before any provider contact |
| A2 | force every node to demand frontier | silently walked down the ladder |
| A3 | unbounded runaway loop | stopped at the call ceiling |
| A4 | provider consumes tokens **then** fails | **control defeated** |

---

## 0. The attack found a defect before it found anything else

The first run reported the loaded thresholds rather than the written ones, and
they did not match `config/part2/budgets.yaml`:

| Threshold | Written | **Actually loaded** |
|---|---|---|
| `downgrade_at` | 0.85 | 0.50 |
| `refuse_at` | 0.95 | 0.90 |
| `max_calls_per_run` | 60 | **0 (unlimited)** |
| `max_calls_per_node` | 6 | **0 (unlimited)** |
| `headroom_fraction` | 0.02 | 0.0 |

**Cause:** `PolicyThresholds.from_mapping()` is handed the whole budgets mapping
and reads these keys from the **top level**. I had nested them under a
`thresholds:` block. Every one was ignored in favour of library defaults —
**no error, no warning, no log line.**

So Part 2 was measured with default thresholds, and both call ceilings were
**disabled**. The only reason that did not matter is that Part 2's per-task
ceiling ($0.012) was never approached; maximum observed spend on a task was
$0.00405. The measurement stands, but it stands by luck rather than by the
policy I thought I had written.

Fixed by flattening the keys. Verified loaded:
`downgrade_at 0.85 · refuse_at 0.95 · max_calls_per_run 60 · max_calls_per_node 6`.

**This is the single most useful thing Part 3 produced.** A silently inert
budget config is exactly the failure the session warns about, and it was found
by an adversarial harness printing configuration *as loaded* rather than as
written. All results below are from the re-run with thresholds active.

---

## 1. A1 — unaffordable tier

Ceiling of $0.0000001 against a ladder whose cheapest rung projects $0.000306.

```
ceiling          $0.00000010
spent            $0.00000000
admitted calls   0
refusals         1
nodes / refused  1 / 1
```

**Spend before the control:** the node requested `frontier`, worst-case
projection **$0.008** — 80,000× the ceiling.
**After the control:** `$0.00000000`, zero provider calls.

The controller walked the ladder to its bottom rung, found even that
unaffordable, and refused rather than downgrading into a call it could not pay
for. Refusal reason, verbatim from the journal:

> `cheapest tier economy projects 0.000306, run holds 0.000000 (headroom 0.000000)`

The refusal is a **visible graph failure** — the node ended `failed` with
`BudgetRefused`, not silently skipped.

## 2. A2 — force the cascade to climb its whole ladder

Eight nodes, every one demanding `frontier`.

```
ceiling          $0.00400000
spent            $0.00030600
admitted calls   8
refusals         0
cost per call    $0.00003825
```

**The attack failed to cost anything.** Eight frontier requests projecting
$0.008 each — **$0.064 demanded against a $0.004 ceiling, a 16× overrun** — were
each admitted at `economy` instead. Measured cost per call ($0.0000383) is the
economy rate, confirming every node was downgraded.

Worth noting as a *quality* finding rather than a cost one: the policy converted
a 16× budget overrun into silent capability reduction, with **zero refusals**.
Nothing in the run signals that eight nodes got a cheaper model than they asked
for, unless you read the per-node `decision` field. A caller watching only
`refusals` would see a clean run.

## 3. A3 — runaway loop

A planner that earns one more node from every outcome, forever, always at the
dearest rung. 150 rounds before the harness's own safety stop.

```
ceiling          $0.00400000
spent            $0.00229275     (57% of ceiling — never reached it)
admitted calls   60              == max_calls_per_run
refusals         90
nodes / refused  150 / 90
cost per call    $0.00003821
```

**Spend before the control:** extrapolated from this run's own measured rate,
**~$0.38 over 10,000 rounds**, and unbounded thereafter — the loop never
terminates on its own.
**After the control:** $0.00229275, and it stopped climbing at call 60.

The refusal reason is the interesting part:

> `run call ceiling reached (60/60)`

**The money ceiling never bound.** The run was stopped by `max_calls_per_run`,
the threshold that had been silently disabled before section 0's fix. With
defaults it ran to 86 calls and stopped on spend pressure instead. So the fix
changed which brake engaged, and the cheaper brake engaged first — which is the
correct ordering, because a call-count ceiling holds even when every price
estimate is wrong.

## 4. A4 — a provider that consumes tokens and then fails

**This attack defeated the control.**

A transport that generates 400 output tokens per call and *then* raises a
timeout — the real failure mode where a provider does the work, will bill for
it, and the response never arrives.

```
ceiling                    $0.00400000
spent (ledger)             $0.00000000
admitted calls             0
refusals                   0
nodes                      40

provider side:
  attempts                 40
  output tokens consumed   16,000
  would be billed          $0.12984000
  ledger recorded          $0.00000000
  UNMETERED                $0.12984000
```

**$0.13 of real tokens consumed against a $0.004 ceiling — a 32× overrun, with
the ledger reading zero and the controller reporting a clean run.**

Three failures compound:

1. **The meter only records successes.** `MeteredTransport` documents the
   assumption explicitly: a call that raised "consumed no tokens the gateway
   could report, so [is] deliberately not charged." True of a refused
   connection. False of a provider that generated 400 tokens and timed out on
   read.
2. **Because nothing is charged, the budget never advances.** `spent` stays at
   $0.00, pressure stays at 0, and `refuse_at` can never trigger. The economic
   brake is disabled by the very failure it should be catching.
3. **`max_calls_per_run` did not help either.** It counts *charges*, not
   *attempts*. Forty attempts produced zero charges, so the call ceiling read
   0/60 throughout. The loop stopped only because my harness capped it at 40.
   Uncapped, it runs forever, spending real money at full rate, reporting $0.

**The ceiling "held" only in the sense that a broken speedometer reads zero.**

### What would fix it

- Count **attempts**, not charges, against `max_calls_per_run` / `_per_node`. A
  ceiling that a failing provider can bypass is not a ceiling.
- Charge the **projected** cost of a call that failed after dispatch, and
  reconcile against the provider's own usage record later. Better to
  over-charge a failure and correct it than to under-charge and never notice.
- Treat a provider raising *after* dispatch differently from one refusing the
  connection. glc_v4 already distinguishes these at the gateway; the S15 side
  collapses both into "failure, no tokens".

---

## 5. The refusals are visible in telemetry

Every attack was exported over the wire and **fetched back from Jaeger by ID**,
so this is what the collector holds, not what the process built in memory.

| Attack | Trace ID | Spans | `s15.budget.refusals` | `budget.refused` events in backend |
|---|---|---|---|---|
| A1 | `5fb5f0e569642b236eeb6a1f4b0fb799` | 6 | **1** | **1** |
| A2 | `40ecdaddd43ffc90784f6fa4c01bda87` | 35 | 0 | 0 |
| A3 | `b2381514db35e3a5e646c60bfb9ab46d` | 513 | **90** | **90** |
| A4 | `bfd257c9f6028dfc9aff252d3ebe0820` | 123 | 0 | 0 |

A refused call never becomes a provider span — there is no provider call to
span. So refusals are carried as **span events on the run span**, each with the
reason, spend and remaining allowance. Read back from Jaeger:

```
trace 5fb5f0e5...  root span "run a1_unaffordable"
  s15.budget.refusals = 1
  s15.cost            = 0
  budget.refused: "cheapest tier economy projects 0.000306,
                   run holds 0.000000 (headroom 0.000000)"

trace b2381514...  root span "run a3_runaway"
  s15.budget.refusals = 90
  s15.cost            = 0.00229275
  budget.refused: "run call ceiling reached (60/60)"   x90
```

An exhausted run is auditable: the count, every individual reason, and the spend
at the moment of refusal all survive the wire.

**And the telemetry reproduces A4's blind spot.** A4's trace shows
`refusals = 0` and `cost = 0` across 123 spans — a clean, cheap, successful-
looking run that consumed $0.13. **Observability inherits the meter's gaps.**
You cannot see in a trace what was never recorded, so a dashboard is not an
independent check on the ledger; it is the same claim rendered differently.

---

## 6. Summary

| Attack | Demanded | Allowed | Held? |
|---|---|---|---|
| A1 unaffordable tier | $0.008 projected vs $0.0000001 ceiling (80,000×) | $0.00 | **yes** |
| A2 full ladder climb | $0.064 vs $0.004 ceiling (16×) | $0.000306 | **yes**, by downgrade |
| A3 runaway loop | unbounded, ~$0.38/10k rounds | $0.00229 | **yes**, at call 60 |
| A4 fail after tokens | $0.13 consumed vs $0.004 ceiling (32×) | ledger says $0.00 | **NO** |

Three of four attacks were contained inside the ceiling by deterministic code, but
by **three different mechanisms**, and only one of them refused outright. The
distinction matters, so it should not be flattened into "refused before any
provider was contacted":

| Attack | Contained how | Provider contacted | Refusals |
|---|---|---|---|
| A1 | **refused outright** — the ladder was walked to its cheapest rung and even that did not fit | **no**, 0 calls, $0.00 | 1 |
| A2 | **downgraded** — every node was served a cheaper rung than it demanded | yes, 8 calls, $0.000306 | **0** |
| A3 | **admitted then refused** — spend proceeded until the call ceiling bound | yes, 60 calls, $0.00229 | 90 |

Only A1 kept every provider untouched. A2 and A3 both spent money; they were
contained rather than blocked, which is the correct behaviour but a weaker claim.

Where a refusal did occur it is visible in the journal, the graph state and the
exported trace. **A2 produced none at all** — a 16x overrun absorbed silently,
which §2 argues is the more uncomfortable of the two outcomes.

The fourth was not stopped, and the honest conclusion is the one Part 3 exists
to surface: **the budget controller is exactly as good as the meter beneath it.**
Admission arithmetic, ladder walking and refusal logic were all correct in A4 —
they simply never ran, because a provider that fails after doing the work
reports nothing for them to act on. Every guarantee in this system is
conditional on the assumption that unsuccessful calls are free, and that
assumption is false for the most expensive kind of failure.

Real spend for Part 3: **$0.0026** of live traffic (A1–A3, all free-tier
providers). A4 consumed no real money because its provider was simulated — which
is the only reason it was safe to run.
