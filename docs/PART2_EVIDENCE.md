# Part 2 — Evidence

**Workload:** LLM and cloud cost engineering, 17 tasks (4 trivial / 5 moderate / 8 hard).
**Measured 2026-08-07**, live against `glc_v4` on `127.0.0.1:8111`.
**Raw:** `proofs/out/p1_cost_per_task_wide.json`, `proofs/out/p1_cost_per_task_narrow.json`.

Two arms, identical in every respect except the token ceiling. Same task file,
same three models, same three providers, same prices, same budget policy, same
single judge. 9 of 9 gates green in both.

---

## 1. The workload and the ladder

`proofs/tasks/llm_cost_engineering.jsonl` — every answer is a number that can be
checked by hand, so the rubric's heaviest criterion rests on arithmetic rather
than taste. All 17 expectations were independently recomputed before any
measurement; where a plausible wrong answer exists the expectation names it.

The traps are the session's own lessons: unweighted rate averaging (`c05`),
cache reads treated as free (`c06`), retries multiplying calls (`c07`), pricing
an average output length instead of `max_tokens` (`c09`), cost-per-call versus
cost-per-resolved-task (`c10`, `c11`), a cascade paying for every rung (`c12`),
invisible reasoning tokens treated as unbilled (`c14`), headline input rates when
output dominates (`c16`).

| rung | provider | model | $/Mtok in/out |
|---|---|---|---|
| economy | groq | `openai/gpt-oss-120b` | 0.15 / 0.75 |
| standard | gemini | `gemini-3.5-flash-lite` | 0.30 / 2.50 |
| frontier | nvidia | `nvidia/nemotron-3-ultra-550b-a55b` | 0.60 / 3.60 |

The frontier rung is not the shipped one. `github/openai/gpt-4.1` was retired on
2026-07-30 — the same day `tiers.yaml` recorded its measurement. See
`LIMITATIONS.md` §A.

---

## 2. Cost per call vs cost per resolved task

### Arm 1 — wide ladder (`config/`, economy ceiling 512)

| strategy | spend | calls | cost/call | resolved | **cost/resolved** |
|---|---|---|---|---|---|
| A always-frontier | 0.00509460 | 17 | 0.00029968 | 10/17 | 0.00050946 |
| B always-cheapest | 0.00914595 | 27 | 0.00033874 | 14/17 | **0.00065328** |
| C budget-aware | 0.01086335 | 21 | 0.00051730 | 17/17 | 0.00063902 |

### Arm 2 — narrow ladder (`config_narrow/`, all ceilings 1024)

| strategy | spend | calls | cost/call | resolved | **cost/resolved** |
|---|---|---|---|---|---|
| A always-frontier | 0.00674340 | 17 | 0.00039667 | 10/17 | 0.00067434 |
| B always-cheapest | 0.00710205 | 19 | 0.00037379 | 16/17 | **0.00044388** |
| C budget-aware | 0.01154775 | 19 | 0.00060778 | 17/17 | 0.00067928 |

Resolution by difficulty:

| | trivial | moderate | hard | total |
|---|---|---|---|---|
| A, both arms | 4/4 | **1/5** | 5/8 | 10/17 |
| B, wide | 4/4 | 5/5 | 5/8 | 14/17 |
| B, narrow | 4/4 | 5/5 | 7/8 | 16/17 |
| C, both arms | 4/4 | 5/5 | 8/8 | 17/17 |

---

## 3. The measured ladder is inverted

Projected, the ladder spreads **32.5×** with frontier dearest. Measured:

| arm | strategy | mean in | mean out | cost/call |
|---|---|---|---|---|
| wide | A frontier | 165.6 | **55.6** | 0.00029968 |
| wide | B economy | 220.1 | **407.6** | 0.00033874 |
| narrow | A frontier | 165.6 | 82.6 | 0.00039667 |
| narrow | B economy | 219.3 | 454.5 | 0.00037379 |

**In the wide arm the frontier rung is cheaper per call than the economy rung.**
The economy model writes 7.3× more output tokens, which more than cancels the
frontier's 4.8× output rate.

The session records a projected 84.3× spread measuring 4.55×. Here the spread did
not merely compress — it changed sign. Any routing policy built from a price page
would have had the direction of its own ladder wrong.

---

## 4. Break-even

`r* = k / (C/c − 1 + k)`, with k = 3 (`cheapest_retries: 2`, so a failed task
consumes three calls).

| arm | comparison | cost/call Δ | cost/resolved Δ | B rate | r\* | headroom | signature |
|---|---|---|---|---|---|---|---|
| wide | B vs A | +13.0% | **+28.2%** | 0.824 | 0.8562 | **−0.0326** | false¹ |
| wide | B vs C | −34.5% | **+2.2%** | 0.824 | 0.7719 | +0.0516 | **TRUE** |
| narrow | B vs A | −5.8% | −34.2% | 0.941 | 0.7886 | +0.1525 | false |
| narrow | B vs C | −38.5% | −34.7% | 0.941 | 0.7859 | +0.1553 | false |

¹ B was *dearer* per resolved task than A but also dearer per call, so it fails
the signature's first half. It is simply worse on both — a stronger result than
the trap, not a weaker one.

**The signature failure mode was OBSERVED in the wide arm, B versus C**: cost per
call down 34.5% while cost per resolved task rose 2.2%. Switch the denominator
from calls to resolved tasks and the ranking reverses — exactly the effect this
session was written to demonstrate, on our own workload.

**Where we sit:** in the wide arm B resolved 82.4% against a break-even of 85.6%
— **3.3 points below**, which is why it lost. In the narrow arm B resolved 94.1%
against 78.9%, a headroom of +15.3 points, and won on both metrics.

---

## 5. What actually moved the verdict — and why the hypothesis was wrong

The narrow arm was built to test whether a narrower price spread makes the trap
appear. **That hypothesis was wrong, and the reason is more useful than the
hypothesis.**

Attempts that stopped at exactly their tier's `max_tokens`:

| arm | economy ceiling | truncated attempts | of which failed |
|---|---|---|---|
| wide | 512 | **18** | 17 |
| narrow | 1024 | **4** | 4 (all `c11`) |

Raising the economy ceiling from 512 to 1024 eliminated 14 of 18 truncations, and
B's resolution rose 82.4% → 94.1%, carrying it from below break-even to well
above it. B's calls fell 27 → 19 and the cascade's escalations fell 4 → 1.

**Strategy A is the control**: 10/17 in both arms, unchanged, because the frontier
model writes 5–24 output tokens and never approaches any ceiling.

So the binding constraint on the cheap rung was never its capability or its price
— it was its token ceiling. *The cap that makes a rung cheap is what broke it.*

**Confound, stated plainly.** Equalising `max_tokens` changed two things at once:
it raised economy's ceiling *and* narrowed the projected spread 32.5× → 4.6×,
because the projection is a function of the ceiling. These are not separable in
this design. The evidence that the ceiling dominates is that A was unaffected and
B's truncations fell 14 → 3; but a clean separation would need a third arm
varying price alone.

---

## 6. Capability is not monotonic

Tasks the **cheapest** rung resolved and the **dearest** did not: **5** in the
wide arm, **7** in the narrow. On moderate tasks the frontier scored **1 of 5**
where economy scored **5 of 5**.

What the frontier actually answered (wide arm):

| task | truth | frontier | out tokens |
|---|---|---|---|
| c05_blended_rate | $0.6316 | $0.6231 | 8 |
| c06_cache_discount | 0.003900 | 0.002900 | 9 |
| c08_total_bill | $4.30 | $3.30 | 6 |
| c09_worst_case_projection | 0.018346 | 0.018360 | 9 |
| c11_break_even_rate | 21.8% | 42.9% | 6 |
| c15_mixed_routing | $2.2700 / $0.0023 | $0.4730 / $0.0005 | 24 |
| c16_crossover_point | 600 | 1000 | 5 |

Every one is an arithmetic slip, not a misunderstanding — `0.018360` is
`0.018346` with two digits transposed, `$3.30` is `$4.30` off by exactly a
dollar. And every one is **5–24 output tokens**: a bare number, no working.

### The mechanism is our own `reasoning: "off"`

That dial is set on the frontier rung to avoid the invisible-token billing bug
(`LIMITATIONS.md` §1.1: unset, this model spends 392–486 output tokens with
1540–2070 characters in a channel the caller never sees). It also removes the
model's scratchpad. The frontier rung attempts multi-step arithmetic in one shot
with nowhere to think, while the economy model reasons out loud across 250–500
visible tokens and gets it right.

**Two of this session's lessons are in direct tension, and this measures the
trade:** the dial that stops you paying for invisible tokens is the same dial
that costs you correctness on multi-step work.

### The rubric caught all of them

Every frontier miss scored **exactly 0.6667** — 4 on all four generic criteria
(addresses task, specific, consistent, complete) and 0 on `meets_expectation`.
4/6 = 0.667, below the 0.75 bar, and caught by the 0.5 per-criterion floor. This
is precisely the "fluent, specific, internally consistent, complete answer to the
wrong question" case the floor exists for. Remove the floor and reweight slightly
and all seven pass.

---

## 7. Where the policy chose wrongly, and what it cost

### `c06_cache_discount` — the cascade paid 7× for an answer it already had

```
B  economy   251 out   $0.00022155   RESOLVED   overall 1.0
C  economy   512 out   $0.00041730   FAILED     overall 0.5   <- truncated at the cap
C  standard  432 out   $0.00112770   RESOLVED   overall 1.0
   C total   $0.00154500  =  7.0x what B spent, for the same final answer
```

Same model, same prompt, temperature 0. One run wrote 251 tokens and finished;
the other ran to exactly 512 and was cut off. The cascade read the truncation as
a quality failure and escalated.

The escalation was not wrong given what the controller could see — the answer
genuinely was incomplete. The policy was wrong one level up: **a rung whose
ceiling truncates a correct answer will be escalated past, and the cost of that
is paid every time output length happens to run long.** Output length at
temperature 0 is not deterministic, so this is a coin flip priced at 7×.

### `c11_break_even_rate` — where the ladder earned its money

The one task that resisted the higher ceiling. Truncated at 1024 three times in
B, which never resolved it. In the narrow arm the cascade climbed all three rungs
and the frontier finally answered correctly:

```
C  economy   1024 out  $0.00080595  FAILED    overall 0.1667
C  standard  1020 out  $0.00260610  FAILED    overall 0.1667
C  frontier   891 out  $0.00333000  RESOLVED  overall 1.0
   C total   $0.00674205  =  2.8x what B spent — and B never got it
```

Reported alongside `c06` deliberately. The same escalation rule that wasted 7× on
`c06` is what resolved `c11`. A cascade cannot tell the two cases apart at the
moment it decides.

---

## 8. Judge and meta-cost

Single judge, `cerebras/zai-glm-4.7`, disjoint from all three ladder rungs. The
gate *"no answer was graded by its own model"* passed in both arms.

| arm | judge calls | judge spend | answering spend (A+B+C) | ratio |
|---|---|---|---|---|
| wide | 48 | 0.02611970 | 0.02510390 | **1.04×** |
| narrow | 46 | 0.02609820 | 0.02539320 | **1.03×** |

**The headline cost-per-resolved-task figures exclude the judge.** Answering spend
and judging spend are reported side by side, following the shipped convention.
The ratio here is ~1.0× rather than the 7.0× the session reports, because a
single judge is used rather than a two-model panel.

---

## 9. Honest limits of this measurement

1. **Single judge, so no agreement metric.** Forced by rate limits: `openrouter`
   allows 50 requests/day and two arms need ~94 judge_b calls. Independence still
   holds; panel disagreement does not.
2. **The arms are confounded** — see §5. `max_tokens` and projected spread moved
   together by construction.
3. **Verdicts are not reproducible.** A resolved 10/17 in both arms but on
   *different tasks*: `c11` and `c13` swapped between runs at temperature 0. Read
   every count with ±1–2 tasks of noise. `c06` shows the same effect within a
   single arm — B resolved it, C did not, same model and prompt.
4. **112 judge transport retries** across the two arms (68 + 64), all cerebras
   throttling. Retried rather than counted as verdicts, but it means the panel
   spent much of its wall clock failing and re-asking.
5. **The judge is weaker than what it grades.** Chosen for disjointness, not
   capability.
6. **`meets_expectation` is a soft answer key** — prose in the task file, judged
   by a model, not a unit test.
7. **No transport failures occurred on the answering path in either arm**
   (`errored attempts: none`). The concern that a 429 would be read as an
   unresolved verdict and escalate a rung is real in principle — `p1` catches
   transport exceptions and passes them to the judge as errored answers — but it
   did not fire here, and no claim rests on it.

---

## 10. Reproduce

```bash
cd agent_economics
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/llm_cost_engineering.jsonl \
  --principal you/p2-wide --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --label wide

uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/llm_cost_engineering.jsonl \
  --principal you/p2-narrow --judge-provider cerebras --judge-model zai-glm-4.7 \
  --base-url http://127.0.0.1:8111 --config-dir ./config_narrow --label narrow
```

**`--base-url` is not optional.** `p1` defaults to port 8112 while the gateway
serves 8111, and on failure it falls back to a simulated transport that still
reports `ALL CHECKS PASSED` with exit 0. See `LIMITATIONS.md` §G. Check the
`(live mode)` line before believing any number above.

Wall clock: 2476 s and 2286 s.
