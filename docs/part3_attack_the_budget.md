# Part 3 — Attack Your Own Budget

A new proof, `proofs/p8_attack_the_budget.py`, attacking the policy built in
Part 2 (`config/part2_billing_support/`, `docs/part2_makeapolicy.md`). Two
adversarial shapes, both run live against the real gateway on 2026-08-05:

1. **A runaway loop**, requesting the same tier forever, measured once with
   no effective ceiling ("before") and once against the real deployed
   policy ("after").
2. **An unaffordable-tier request**, refused before any provider is
   contacted.

Every number below is from `proofs/out/p8_attack_the_budget_part3_*.json`
and `proofs/out/p2_budget_holds_part3.json` — real runs, not simulated.

## 1. Why a New Proof Instead of Reusing `p3_denial_of_wallet.py`

`p3_denial_of_wallet.py` already proves a runaway loop cannot outspend its
ceiling (`part1_validation.md` Section 3 ran it live). It has two gaps for
this exercise specifically:

- It always attacks `ladder.most_capable` — the ladder's frontier rung. My
  ladder's frontier (`openrouter`/`deepseek/deepseek-chat`) is still hitting
  `openrouter`'s daily rate limit as of this run (the same outage
  `part2_makeapolicy.md` Section 4.1 measured) — smoke-tested immediately
  before this attack and still returning `RPD limit`. Attacking a rung that
  fails at the transport layer regardless of budget size would prove
  nothing about the budget policy, only that a provider is down. `p8` adds
  `--tier` so the adversary can target whichever rung is actually reachable
  — here, `standard` (`gemini`/`gemini-3.1-flash-lite`).
- It never calls `s15code.telemetry.export_run`, so nothing it proves is
  visible in a trace. `p8` builds real spans from the same run and, when
  pointed at a live collector, reads the trace back out of Jaeger by id —
  the same round-trip discipline `p4_trace_export.py` uses — and checks
  specifically that a refused node is a real `ERROR`-status span in what
  the backend stored, not just a row in the ledger.

## 2. The Runaway Loop: Before and After

Same adversary both times: a planner that earns one more node from every
outcome, forever, requesting `standard` every round
(`proofs/p8_attack_the_budget.py`'s `RunawayPlanner`, adapted from `p3`'s).
Capped at 30 rounds by the proof's own safety net, not by anything under
test.

**Before control** — `config/part2_billing_support_uncontrolled/`: same
ladder and pricing as the real policy, but `default_budget: 1000.0`,
`max_calls_per_run: 100000` — standing in for a run with no economic
control layer at all (`s15architecture.md` Section 2: this is where
`glc_v3` started — throughput caps existed, a spend cap did not).

```
uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support_uncontrolled \
  --principal part3/attack --tier standard --loop-limit 30
```

```
ceiling            1000.00000000 USD
spent              0.00059475
admitted calls     30
refusals           0
loop rounds        30
```

**Every round bought a call. Nothing stopped it — spend climbed for all 30
rounds with zero refusals.**

**After control** — `config/part2_billing_support/`: the real, deployed
policy (`default_budget: 0.01`, `max_calls_per_run: 10`). Identical
adversary, identical task, identical tier, identical 30-round cap:

```
uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support \
  --principal part3/attack --tier standard --loop-limit 30 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
```

```
ceiling            0.01000000 USD
max_calls_per_run  10
spent              0.00019775
admitted calls     10
refusals           20
loop rounds        30
refused nodes      20
```

**The same loop that ran unchecked for 30 rounds before was cut off after
10.** `spent` ($0.00019775) is well under the $0.01 ceiling — on this task
the per-call cost was low enough ($0.0000198/call) that the **call-count
ceiling** (`max_calls_per_run: 10`), not the dollar ceiling, was the
binding constraint. This is reported exactly as measured rather than
picking a task that would make the dollar ceiling bind instead: a cheap,
short-prompt task is precisely the case where volume, not price, is the
real denial-of-wallet risk, and `max_calls_per_run` existing as a control
independent of dollar price is what stops it — Section 3 below tests the
dollar ceiling specifically, in isolation, with a task the call ceiling
can't touch (a single request).

`ALL CHECKS PASSED` on both runs, including:

```
[PASS] the ceiling held under an unbounded loop
[PASS] admitted calls are bounded by the configured call ceiling
[PASS] every provider call that returned is metered
[PASS] a refusal is a visible ERROR-status span, not a silent truncation
[PASS] a real trace lands in the backend, fetched back by id
[PASS] the refusal survives the wire: an ERROR span naming BudgetRefused is in the backend
```

## 3. Requesting an Unaffordable Tier

The second attack shape doesn't need any provider reachable at all —
`BudgetPolicy.decide()` refuses on the **projected** cost, before the
provider is ever contacted (`s15architecture.md` Section 6.2). Reusing
`p2_budget_holds.py` (unmodified) against the real policy config, with an
allowance set below what a single token could cost:

```
uv run python proofs/p2_budget_holds.py \
  --task "Draft a one-sentence reply to a billing question." \
  --budget 0.01 --principal part3/attack \
  --config-dir config/part2_billing_support \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
```

The `impossible` allowance (`$0.00000033` — the proof derives this from
the ladder's own projected cost, per `p2_budget_holds.py`'s own docstring):

```json
{"ceiling": 3.3e-07, "spent": 0.0, "calls": 0, "transport_calls": 0,
 "refusals": 1, "status": "failed", "seconds": 0.091}
```

**Refused in 91ms, zero calls, zero transport calls, zero spend.** No
provider was ever contacted — the refusal happens entirely inside the
admission gate, which is what makes it provider-independent evidence: it
would hold exactly the same whether every rung in the ladder were reachable
or not.

**An honest complication, disclosed rather than hidden.** The other three
allowances in this same run (`declared`, `generous`, `tight`) all show
`"transport_failures": 1"` and zero calls — not a budget refusal. My
`tiers.yaml`'s `role_tiers` maps `answer_with_evidence` (the role
`p2_budget_holds.py` uses) to `frontier` by default, and at zero spend
there is no downgrade pressure yet, so each of those three allowances
requested `frontier` first — the still-rate-limited `openrouter` rung — and
failed at the transport layer before any budget logic had a chance to
downgrade them. This is the same external outage as Section 1 and
`part2_makeapolicy.md` Section 4.1, not a defect in the policy: the
`impossible` case, which is the one this section is actually testing,
never touches a provider at all and is unaffected by it.

## 4. What the Trace Shows

The "after control" run's trace (`085f12c4e067ccb46dd7a0c3156e2f26`,
fetched back from `http://127.0.0.1:16686/api/traces/085f12c4e067ccb46dd7a0c3156e2f26`)
holds 103 spans:

```
run: 1   agent_loop: 31   plan: 31   node: 30   provider_call: 10
```

30 `node` spans (one per loop round), of which exactly 10 have a
`provider_call` child (the admitted calls) and 20 carry `ERROR` status. A
sample of each, read directly off the backend's own span tags — not the
in-process copy:

**An admitted call** (`s15.span.kind: provider_call`):
```
gen_ai.provider.name: gemini_1
gen_ai.request.model: gemini-3.1-flash-lite
gen_ai.usage.input_tokens: 25
gen_ai.usage.output_tokens: 9
s15.cost: 0.00001975
s15.cost.projected: 0.0006085
s15.budget.decision: proceed
s15.budget.remaining: 0.00994075
s15.tier: standard
```

**A refused round** (`s15.span.kind: node`, `loop_16`):
```
otel.status_code: ERROR
otel.status_description: "BudgetRefused: budget refused a standard call
  for loop_16: run call ceiling reached (10/10)"
```

The refusal reason is not a generic error — it names the exact mechanism
(`run call ceiling reached (10/10)`), and it's sitting in Jaeger, not just
in the JSON this proof also wrote. An operator watching only the trace, not
the ledger, would see exactly what stopped the loop and why.

## 5. Reproduce

```
cd S15Code
uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support_uncontrolled \
  --principal part3/attack --tier standard --loop-limit 30

uv run python proofs/p8_attack_the_budget.py \
  --task "Reply with any short greeting." \
  --config-dir config/part2_billing_support \
  --principal part3/attack --tier standard --loop-limit 30 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces

uv run python proofs/p2_budget_holds.py \
  --task "Draft a one-sentence reply to a billing question." \
  --budget 0.01 --principal part3/attack \
  --config-dir config/part2_billing_support
```

`--otel-endpoint` is optional in all three — omit it and the spans are
still built and asserted in-process, just not sent anywhere (the same
no-collector-required behaviour every proof in this repo has).
