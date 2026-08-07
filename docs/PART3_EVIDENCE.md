# Part 3 — Attack your own budget

**Measured 2026-08-07**, live against `glc_v4` on `127.0.0.1:8111`.
**Raw:** `proofs/out/p1_cost_per_task_attack.json`,
`proofs/out/p3_denial_of_wallet_live.json`,
`proofs/out/p3_denial_of_wallet_live-tight.json`.

Two attacks. The first is the shipped runaway loop, which produced a result the
proof's own gate misreads. The second is written specifically against the
weakness Part 2 measured in this policy.

---

## Attack 1 — the runaway loop, and the defence that fired instead

`p3_denial_of_wallet.py` drives an adversarial planner that earns one more node
from every outcome, forever. Run twice, against the same policy, differing only
in the ceiling.

### Run A — $0.002 ceiling, 200 rounds

```
loop rounds 200   admitted 4   refusals 0   spent 0.00123345 / 0.00200000
transport failures 196

[PASS] the ceiling held under an unbounded loop            spent 0.00123345 <= 0.00200000
[PASS] admitted calls are bounded by the call ceiling      4 <= 60
[FAIL] the loop kept asking and was refused                200 rounds, 4 admitted, 0 refused
[FAIL] a refusal is a visible graph failure                0 nodes failed with BudgetRefused
[PASS] the controller, not the loop, is what stopped the spend
```

**196 of 200 rounds were provider errors, not budget refusals.** A tight loop
exhausted every provider's per-minute quota within seconds. And the mechanism is
self-defeating in a way worth stating precisely: **failed calls report no tokens,
so they are not charged; unspent budget never depletes; a controller with budget
remaining has no grounds to refuse.** Rate limiting suppressed the spend, which
suppressed the very control under test.

The budget controller was never exercised. Yet the gate
*"the controller, not the loop, is what stopped the spend"* **passed** — because
it only checks that spend stopped early, not *what* stopped it. It attributes to
the controller an outcome that provider quotas produced.

This is the session's own claim that "rate limiting remains the primary defence",
observed as an experimental confound rather than a reassurance.

### Run B — $0.0005 ceiling, 60 rounds

To exercise the controller, the budget has to deplete faster than the rate limits
bite. One successful economy call is enough:

```
loop rounds 60   admitted 1   refusals 59   spent 0.00036555 / 0.00050000
transport failures 0

[PASS] the ceiling held under an unbounded loop                       spent 0.00036555 <= 0.00050000
[PASS] admitted calls are bounded by the configured call ceiling      1 <= 60
[PASS] the loop kept asking and was refused                           60 rounds, 1 admitted, 59 refused
[PASS] a refusal is a visible graph failure, not a silent truncation  59 nodes failed with BudgetRefused
[PASS] every provider call that returned is metered                   1 call == 1 charge, 0 gateway errors
[PASS] the controller, not the loop, is what stopped the spend        spend stopped after 1 of 60 attempts
```

**Before the control:** the loop requested work 60 times and would have continued
indefinitely. **After it:** 1 call admitted, 59 refused, spend flat at
`$0.00036555` against a `$0.00050000` ceiling. The loop never stopped asking. The
agent never decided to become careful.

### Which defence binds is a function of budget ÷ rate limit

The same policy, the same attack, the same code path — and the outcome is
governed by whether the money runs out before the quota does. A test that does
not control for this cannot tell you which of your defences is working.

---

## Attack 2 — verbosity-induced escalation

Written against this policy specifically, from what Part 2 measured.

### The vulnerability

Part 2 established that a truncated economy answer fails the quality check and
escalates, and that on `c06_cache_discount` this cost **7.0×** what the economy
rung alone would have. The controller cannot distinguish *"the model was wrong"*
from *"the model ran out of room"*.

So an attacker needs neither a loop nor an unaffordable request. Every request
below is individually cheap, entirely legitimate, and would pass any spend
ceiling. They simply have long **correct** answers.

### The design

`proofs/tasks/verbosity_attack.jsonl` — six attack requests paired with six
controls from the same arithmetic domain, no harder. Two controls are literally
the summary line of an attack task's output (`b01` is the last line of `a02`;
`b03` is the last line of `a04`). **Only the required output length differs**,
which is what isolates verbosity as the attack surface.

Run through strategy C, the budget-aware cascade, on the wide ladder.

### The result

| task | arm | attempts | cost | tiers climbed | output tokens |
|---|---|---|---|---|---|
| a01_integers_200 | attack | 1 | 0.00035205 | economy | 435 |
| a02_times_table_13 | attack | 1 | 0.00040710 | economy | 506 |
| a03_fizzbuzz_150 | attack | 1 | 0.00027240 | economy | 324 |
| **a04_powers_of_two** | attack | **2** | **0.00292160** | **economy → standard** | **512**, 992 |
| a05_digit_sums | attack | 1 | 0.00038460 | economy | 475 |
| **a06_numbers_in_words** | attack | **2** | **0.00164830** | **economy → standard** | **512**, 484 |
| b01_product | control | 1 | 0.00003675 | economy | 17 |
| b02_sum_to_200 | control | 1 | 0.00004815 | economy | 31 |
| b03_two_to_sixty | control | 1 | 0.00004830 | economy | 32 |
| b04_divisible_both | control | 1 | 0.00005790 | economy | 43 |
| b05_digit_sum_99 | control | 1 | 0.00004755 | economy | 31 |
| b06_count_two_digit | control | 1 | 0.00005085 | economy | 36 |

```
attack    n=6  calls=8  spend=0.00598605  mean/request $0.00099767  resolved 6/6
control   n=6  calls=6  spend=0.00028950  mean/request $0.00004825  resolved 6/6

AMPLIFICATION  20.7x per request
```

**All twelve resolved.** The attack does not degrade quality — it inflates cost
while looking like a completely successful workload.

### Decomposition: only half of this is the attack

| group | n | mean/request | vs control |
|---|---|---|---|
| control | 6 | $0.00004825 | 1.00× |
| attack, no escalation | 4 | $0.00035404 | **7.34×** — verbosity alone |
| attack, escalated | 2 | $0.00228495 | **47.36×** — verbosity + ladder climb |

**Escalation multiplier on top of verbosity: 6.45×** — which independently
reproduces Part 2's 7.0× on `c06_cache_discount` by the same mechanism, on a
different task set.

The 7.34× is honest cost: longer answers cost more, and the operator got longer
answers. The **6.45×** on top is the defect — money spent re-answering a question
the cheap rung had already answered, discarded because it hit a ceiling.

### The attack is probabilistic, and trivially made deterministic

Only 2 of 6 attack requests crossed the 512-token cap. But look at how close the
others came:

```
a02_times_table_13   506 tokens  (  6 from the cap)
a05_digit_sums       475 tokens  ( 37 from the cap)
a01_integers_200     435 tokens  ( 77 from the cap)
a03_fizzbuzz_150     324 tokens  (188 from the cap)
```

`a02` missed by **six tokens**. An attacker does not need to know the ceiling —
they only need to ask for more. "Integers 1 to 400" instead of 1 to 200, or
"FizzBuzz to 300", moves every one of these over the line. The 47× case becomes
the whole workload.

### Why a spend ceiling does not stop this

Each request is affordable, so admission passes. The ceiling still holds in the
sense that it is never breached — it simply gets consumed **20.7× faster** by
traffic that is indistinguishable from legitimate use. A `$0.05` run budget that
serves 1,000 control requests serves **48**.

The defences that do apply are `max_calls_per_node` (already 6) and
`max_calls_per_run` (60), which bound the climbing rather than the spend. Both
held here.

---

## The refusal is visible — in two places, and neither is the obvious one

Part 3 requires the refusal to be visible in telemetry. It is, but the two
records are at different layers and **neither sees the other**.

### 1. In the graph, and in the run's own ledger

All 59 refusals failed their node with `BudgetRefused`, each carrying a receipt:

```json
{ "node_id": "loop_2",
  "reason": "cheapest tier economy projects 0.000389, run holds 0.000134 (headroom 0.000010)",
  "action": "refuse", "requested_tier": "frontier", "tier": null, "model": null,
  "spent": 0.00036555, "remaining": 0.00013445, "pressure": 0.7311,
  "projected_cost": 0.0003891 }
```

A reader can see the run was cut short and why. Contrast a silent truncation,
where the loop stops producing and the trace looks like a run that finished early.

### 2. At the gateway — but only for gateway-issued refusals

| | refusals recorded |
|---|---|
| S15 controller during the 60-round run | **59** |
| gateway `GET /v1/refusals` during that run | **0** |

The gateway's recorder is not broken. It watches HTTP 402s, and S15's controller
refuses *before* any HTTP request is made, so no 402 is ever issued. Armed with
its own ceiling, the gateway path works exactly as designed:

```
POST /v1/budget   {"principal":"session:p3-gw","limit_usd":0.00003,"window":"lifetime"}
POST /v1/chat     max_tokens 16   -> HTTP 200, cost 2.295e-05
POST /v1/chat     max_tokens 2048 -> HTTP 402 budget_exceeded
                     projected_usd 0.0051203  remaining_usd 7.05e-06  shortfall_usd 0.00511325
GET  /v1/refusals -> total 1, full envelope retained
```

### 3. Not in the trace at all

Confirmed by the Part 1 evidence runs: a refused run emits **9 spans** where an
admitted one emits **10**, differing by exactly the `provider_call` span
(`LIMITATIONS.md` §1.7). Admission runs before the provider is contacted, so
there is nothing to attach a span to.

**The operational consequence.** An operator watching the gateway dashboard — the
natural place to look — sees an untouched budget and zero refusals while 59
refusals have occurred one layer up, in a different process. The system's most
protective behaviour is recorded in the one place nobody is watching.

---

## What this policy would need next

1. **Do not read truncation as a quality failure.** `finish_reason == "max_tokens"`
   is a capacity signal, not a wrong answer. Retry the same rung with a larger
   ceiling before climbing to a dearer model. This alone removes the 6.45×.
   The gateway already returns `stop_reason`; nothing records it
   (`LIMITATIONS.md` §1.1).
2. **Bound output length per principal**, not just spend. The attack is a
   token-volume attack wearing legitimate clothes.
3. **Surface refusals across both layers.** A single pane that shows runtime
   `BudgetRefused` events beside gateway 402s.
4. **Fix `p3`'s attribution gate.** *"The controller stopped the spend"* must
   check that refusals occurred, not merely that spend stopped — otherwise a
   rate-limited run reports a working budget controller that never ran.

---

## Reproduce

```bash
cd agent_economics

# Attack 1 -- rate limits win
uv run python proofs/p3_denial_of_wallet.py --task "Compare two approaches to bounding agent spend." \
  --budget 0.002 --principal you/p3-live --base-url http://127.0.0.1:8111 --label live

# Attack 1 -- the controller wins
uv run python proofs/p3_denial_of_wallet.py --task "Compare two approaches to bounding agent spend." \
  --budget 0.0005 --principal you/p3-tight --base-url http://127.0.0.1:8111 \
  --loop-limit 60 --label live-tight

# Attack 2 -- verbosity-induced escalation
uv run python proofs/p1_cost_per_task.py --tasks proofs/tasks/verbosity_attack.jsonl \
  --strategies C --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --principal you/p3-attack --label attack
```

`--base-url` is mandatory — see `LIMITATIONS.md` §G.
