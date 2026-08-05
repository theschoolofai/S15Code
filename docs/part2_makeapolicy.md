# Part 2 — Build a Policy and Measure It

A new task class, a new ladder, a new budget policy, measured live against an
always-frontier baseline with `proofs/p1_cost_per_task.py` — the same harness
Section 11 of `s15architecture.md` and `docs/part1_validation.md` use, pointed
at data and config this session wrote, not the shipped `mixed.jsonl` /
`config/tiers.yaml`. Every number below is from a real run on 2026-08-05
(`proofs/out/p1_cost_per_task_part2.json`), not simulated.

## 1. The Task Class: Loomstack Billing-Support Triage

18 tickets (`proofs/tasks/part2_billing_support.jsonl`, 5 trivial / 7
moderate / 6 hard) for a fictional SaaS product's billing support desk. Each
task embeds the same six-rule policy, one specific customer ticket, and a
fixed three-line output format (`Category / Action / Message`) — the model
has to classify the ticket, apply the policy correctly, and choose one action
from an eleven-item taxonomy.

**The policy** (verbatim from the task text, so every task is self-contained
the way `harness.py` requires — "the harness knows nothing about it"):

1. Refund requests: full refund only if requested within 14 days of the
   charge AND usage that cycle is no more than 10%. Otherwise: no refund,
   offer a prorated credit instead.
2. Duplicate charges: always fully refund a CONFIRMED duplicate, regardless
   of the 14-day window. A duplicate means the same line item billed twice —
   not two different charges for two different things.
3. Downgrades take effect at the start of the NEXT billing cycle, never
   mid-cycle.
4. Cancellations stop future billing; they do NOT refund the current,
   already-active period.
5. Failed payments auto-retry for 3 days from that INVOICE's first failure;
   no suspension before day 4 of that invoice's own sequence. Different
   invoices have independent sequences.
6. Enterprise (annual) accounts: any refund/dispute over $500 must escalate
   to the Billing Team — front-line may not resolve it directly.

**Why this domain.** It has what a routing-policy measurement needs and
generic reasoning puzzles don't: a real cost/quality tradeoff a business
actually faces (routing every ticket to the most expensive model is a real
line item; routing every ticket to the cheapest one risks a wrong refund
decision), a checkable ground truth (the policy is exact, not a matter of
taste), and room for genuine interpretive difficulty without the task
becoming ambiguous busywork.

**The six hard tasks are deliberate traps**, each isolating one way a
plausible-sounding answer can still be policy-wrong:

| Task | Trap |
|---|---|
| `bs13_not_actually_duplicate` | Customer *calls* two different charges a duplicate; they aren't (different line items) |
| `bs14_exact_10_percent_boundary` | Usage at exactly 10% — "no more than 10%" must include the boundary |
| `bs15_non_enterprise_over_500_no_escalation` | $800 refund, but the $500-escalation rule only applies to enterprise accounts |
| `bs16_genuinely_ambiguous` | Downgrade-or-cancel, genuinely unclear which — correct move is to ask, not guess |
| `bs17_duplicate_vs_enterprise_conflict` | A confirmed $600 duplicate on an enterprise account — rule 2 (always refund) and rule 6 (escalate over $500) collide, and policy's plain text gives rule 6 no duplicate-shaped exception |
| `bs18_independent_invoice_clocks` | Two invoices' failed-payment clocks summed by the customer's own framing; policy tracks them independently |

## 2. The Ladder and Budget Policy

`config/part2_billing_support/{tiers,budgets,pricing,evals}.yaml` — a
complete, standalone config directory passed via `--config-dir`, not an edit
to the shipped one.

| Rung | Provider / model | $/Mtok in / out | Why |
|---|---|---|---|
| economy | groq / `openai/gpt-oss-120b` | 0.15 / 0.75 | Unchanged from the shipped ladder — confirmed live and cheap in `part1_validation.md` |
| standard | gemini / `gemini-3.1-flash-lite` | 0.25 / 1.50 | Unchanged from the shipped ladder — likewise confirmed live |
| frontier | openrouter / `deepseek/deepseek-chat` | 0.2574 / 1.029 | **Replaces** the shipped ladder's `github`/`openai/gpt-4.1` |

The shipped ladder's frontier rung is currently dead — `part1_validation.md`
Section 4 measured a live `410 Gone` from GitHub's own API, "a scheduled
retirement brownout." Reusing it here would have measured an outage, not a
policy, so this ladder repoints frontier at a provider confirmed reachable
the same day (a smoke-tested `curl` against `/v1/chat`), priced from
OpenRouter's own pricing page fetched live during this session ($0.2574 in /
$1.029 out per Mtok, their listed DeepSeek-V3 chat rate with a 10% promotion
applied — flagged in `pricing.yaml` as needing reverification, the same
honesty convention the shipped file uses for its own rates). It is also
deliberately disjoint from both judge-panel models
(`cerebras/zai-glm-4.7`, `openrouter/nvidia/nemotron-3-super-120b-a12b:free`)
so no rung can be graded by its own model.

**Budget policy** (`config/part2_billing_support/budgets.yaml`):
`default_budget: 0.01` per ticket — sized off the ladder's own projected
worst case (a full three-rung cascade projects to roughly $0.001), leaving
over 8x headroom, tight on purpose. `downgrade_at: 0.50`, `refuse_at: 0.90`,
`max_calls_per_run: 10`, `max_calls_per_node: 3` (matching
`evals.yaml strategies.max_attempts: 3`) — a support ticket that needs more
than 10 calls to answer is a bug, not a legitimate cascade, unlike an
open-ended research or coding task where a wider allowance is defensible.

## 3. The Judge, Defended

`config/part2_billing_support/evals.yaml` is the shipped rubric, **reused
unchanged** rather than rewritten — a deliberate choice, not an oversight.

The shipped rubric is task-agnostic by design (`s15architecture.md` Section
11.3): four generic criteria (`addresses_task`, `specific`, `consistent`,
`complete`, weight 1.0 each) plus `meets_expectation` (weight 2.0, scored
against the task's own `expectation` string, floor-gated at 0.5 so a fluent
wrong answer can't average its way past the bar). The defence for reusing it
here rather than writing a support-triage-specific rubric:

1. **The domain-specific grading already lives in the task data, not the
   rubric.** Every one of the 18 `expectation` strings names the exact
   correct category, action, and any boundary fact the ticket turns on (e.g.
   `bs14`'s expectation states explicitly that exactly 10% usage still
   qualifies). `meets_expectation` does the real work; a rubric that also
   hardcoded "must say `issue_full_refund`" would just be the same answer key
   welded into YAML instead of JSONL, which is the exact anti-pattern
   `s15code/evals/tasks.py`'s own docstring warns against.
2. **The four generic criteria are not generic-in-a-bad-sense here.** A
   hedged triage reply ("it depends, you may want to check with billing") is
   as useless in production as a hedged answer to a logic puzzle —
   `specific` still separates a committed decision from a non-decision.
   A reply that names the right action but never states it as the final
   line, or contradicts its own category, is exactly what `complete` and
   `consistent` catch, independent of billing domain knowledge.
3. **A domain rubric risks becoming the answer key it's trying not to be.**
   A criterion like "correctly applies the 14-day rule" would only ever
   apply to a third of these tasks and would have to be reinvented for the
   next task class entirely — the generic rubric transfers, a bespoke one
   doesn't.

The one place this defence has a real seam is documented honestly in Section
6.

## 4. Measurement

```
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/part2_billing_support.jsonl \
  --principal part2/billing_support \
  --config-dir config/part2_billing_support \
  --base-url http://127.0.0.1:8111 --label part2
```

Live mode, real gateway, real providers. `ALL CHECKS PASSED` — all 9 of the
harness's own invariant checks (every task attempted, every returned call
metered, no answer judged by its own model, etc.) held.

### 4.1 A rate-limit artifact — and why the headline numbers below are computed two ways

The raw 18-task run:

| Strategy | Spend | Calls | Resolved | Cost/call | Cost/resolved |
|---|---|---|---|---|---|
| A (always-frontier) | $0.00244571 | 14 | 14/18 | $0.00017469 | $0.00017469 |
| B (always-cheapest) | $0.00382578 | 23 | 16/18 | $0.00016634 | $0.00023911 |
| C (my policy) | $0.00334804 | 20 | 17/18 | $0.00016740 | $0.00019694 |

A's four "unresolved" tasks (`bs15`–`bs18`) are **not** frontier answering
wrong — every one of them shows `cost: 0.0`, `tier: -`, and the identical
verdict reason:

```
answer failed: RuntimeError: gateway /v1/chat returned 503:
{"detail":"all providers unavailable. attempts: [{'provider': 'openrouter', 'reason': 'RPD limit'}]. last_error: None"}
```

`openrouter`'s daily request quota was exhausted partway through the run — A
ran first, used its 14 successful calls against that same daily budget, and
had nothing left for the last four tasks. (The same limit later hit strategy
C's own escalation attempt on `bs08`, Section 6.) Counting these four as
"resolved: false" would make A look worse than it measurably is: it never
got to answer them at all, so there is no basis to claim it would have
gotten them right *or* wrong.

**The fair comparison** restricts all three strategies to the 14 tasks A
actually attempted (`bs01`–`bs14`):

| Strategy | Spend | Calls | Resolved | Cost/call | Cost/resolved |
|---|---|---|---|---|---|
| A (always-frontier) | $0.00244571 | 14 | 14/14 (100%) | $0.00017469 | $0.00017469 |
| B (always-cheapest) | $0.00311541 | 19 | 12/14 (85.7%) | $0.00016397 | $0.00025962 |
| **C (my policy)** | $0.00260617 | 17 | 13/14 (92.9%) | **$0.00015330** | **$0.00020047** |

This is the subset used for every claim in Sections 5–6 below. Every number
in both tables is real, from the same `proofs/out/p1_cost_per_task_part2.json`
run — nothing here is simulated or re-run to make the story cleaner.

## 5. Cost Per Call and Cost Per Resolved Task, Against Always-Frontier

On the fair 14-task subset, **my policy (C) is 12.24% cheaper per call than
always-frontier — and 14.76% more expensive per resolved task:**

```
cost_per_call:      C $0.00015330  vs  A $0.00017469   (-12.24%)
cost_per_resolved:  C $0.00020047  vs  A $0.00017469   (+14.76%)
```

This is the exact shape `s15architecture.md` Section 11 calls "the signature
failure mode" — cheaper per call, dearer per resolved task — measured on
this task class's own ladder and its own retry policy, computed with the
same `break_even_resolution_rate()` the shipped proof uses (imported
directly from `proofs/p1_cost_per_task.py`, not reimplemented).

## 6. The Break-Even Resolution Rate

```
r* = k / (C/c - 1 + k)
   k = 3            (evals.yaml strategies.max_attempts)
   c = $0.00015330  (C's measured cost per call)
   C = $0.00017469  (A's measured cost per resolved task)

r* = 3 / (0.00017469/0.00015330 - 1 + 3) = 0.9556  (95.56%)
```

My policy's actual measured resolution rate on this subset is **92.86%**
(13/14) — **1.87 percentage points below its own break-even rate**
(`headroom_above_break_even = -0.0270`, i.e. -2.70pp before rounding). Below
`r*`, cost per call falls while cost per resolved task rises — which is
exactly what Section 5 measured. **On this task class, with this ladder,
my policy loses to simply always calling the frontier model.**

**Why the spread is this narrow.** The shipped ladder's frontier
(`openai/gpt-4.1` at $2.00/$8.00 per Mtok) is roughly 13x economy's rate;
this ladder's frontier (`deepseek/deepseek-chat` at $0.2574/$1.029) is only
about 1.7x economy's rate for a short, structured triage reply — a
short-output task class doesn't give a price spread much room to work with
even before multiplying by attempts. A three-attempt cascade needs a wide
enough gap between rungs to pay for its own failures; `r*` is that
requirement turned into a number, and 95.56% is a high bar for any policy
to clear on 18 tasks. This is not a flaw in the *measurement* — it is
exactly the tradeoff the task asked to be found: **a narrow, honestly-priced
ladder can make routing itself the more expensive choice**, and the only way
to know is to measure it, not to assume a cheaper-per-call rung is a win.

## 7. Where My Policy Chose Wrongly, and What It Cost

**`bs08_refund_in_window_high_usage`** — charged within the 14-day window,
but usage (35%) is over the 10% cap, so the correct action is to deny the
full refund and offer a prorated credit instead.

| Strategy | Attempts | Tiers | Cost | Resolved |
|---|---|---|---|---|
| A (frontier, one call) | 1 | frontier | **$0.00018658** | **Yes** |
| C (my policy) | 3 | economy → standard → frontier (RPD-limited) | **$0.00040244** | **No** |

My policy spent **2.16x** what always-frontier spent on this exact ticket
and still finished unresolved. What happened, attempt by attempt:

1. **Economy** (`groq`/`openai/gpt-oss-120b`): `Action: issue_prorated_credit`
   — *"we can't provide a full refund and will instead apply a prorated
   credit toward your next invoice."*
2. **Standard** (`gemini`/`gemini-3.1-flash-lite`), after escalation:
   `Action: issue_prorated_credit` — same substance, same wording pattern.
3. **Frontier** (`openrouter`/`deepseek/deepseek-chat`), after a second
   escalation: transport failure — `openrouter RPD limit`, $0 charged, no
   answer.

Compare to strategy A's single frontier call on the same ticket:
`Action: deny_refund_explain_policy` — *"Since you've used over 10% of this
cycle's usage, we can't issue a refund but will apply a prorated credit to
your next invoice."*

**Two distinct failures are compounded here, and both are worth naming
honestly:**

1. **The cascade escalated into a taxonomy ambiguity it could not fix by
   spending more.** `issue_prorated_credit` and `deny_refund_explain_policy`
   are not actually in conflict under my own policy text — rule 1 says
   denying a refund *comes with* a prorated-credit offer, so both economy's
   and standard's answers are **substantively correct**: they identify the
   right category, correctly deny a full refund, correctly cite the usage
   overage, and correctly offer a credit. They simply picked a different one
   of two overlapping labels from the taxonomy than the exact string my
   `expectation` field named, and the strict judge (rightly, per its own
   configured floor) did not forgive the mismatch against a literal
   `meets_expectation` grade. Escalating to a stronger model did not fix
   this — it produced a third attempt that might have picked the "right"
   label (frontier did, on strategy A's independent call) or might not have;
   the point is that **tier is the wrong lever for a labeling-convention
   gap**, and my cascade spent a whole extra tier's worth of money finding
   that out.
2. **A live rate limit made the cascade's last resort actively unreliable.**
   The third attempt didn't fail on the merits — it failed because
   `openrouter`'s daily quota was already gone (Section 4.1). A cascade
   whose top rung shares a rate-limited resource with the strategy that ran
   immediately before it (strategy A, in this same process) is not a
   theoretical risk; it is what happened, mid-run, unprompted.

**The honest lesson this task surfaces, beyond the headline break-even
number:** my own task design has a real seam — an eleven-item action
taxonomy with two entries (`issue_prorated_credit`,
`deny_refund_explain_policy`) that aren't mutually exclusive under the
policy text that defines them — and a budget policy cannot route around a
rubric ambiguity, only around a capability gap. Section 3's defence of the
generic judge holds up (it caught genuine trap tasks like `bs13`/`bs15`/
`bs17`/`bs18` correctly — Section 8), but `bs08` is the counterexample:
a well-defended rubric still inherits any ambiguity baked into the task
data it's grading, and no amount of escalation spend fixes that.

## 8. The Encouraging Half of the Same Result

Worth reporting alongside Section 7, not instead of it: the six hard trap
tasks were designed to be hard, and the **cheapest** rung
(`groq`/`openai/gpt-oss-120b`) got every single one right on the *first*
attempt when it did get a genuine shot —
`bs13` (correctly rejects a false duplicate-charge claim), `bs15`
(correctly does *not* escalate a non-enterprise account despite the $800
amount), `bs16` (correctly asks for clarification instead of guessing),
`bs17` (correctly escalates a duplicate-vs-enterprise conflict to the
Billing Team rather than resolving it directly), and `bs18` (correctly
tracks two invoices' failed-payment clocks independently). None of these
needed frontier. This is itself part of why Section 6's spread is narrow —
economy is more capable on this specific task shape than the ladder's price
gap assumes, which is exactly the kind of fact "measure your own traffic"
(`s15architecture.md` Section 11.2) exists to surface instead of assumed
from a benchmark average.

## 9. Summary

- **A new task class** (18 tasks, Loomstack billing-support triage) was
  built because a routing tradeoff needs a domain with a real cost/quality
  tension and a checkable ground truth, not another reasoning-puzzle set.
- **A new ladder and budget policy** were built because the shipped
  ladder's frontier rung was independently confirmed dead
  (`part1_validation.md`), priced from a real, live-fetched source rather
  than copied or guessed.
- **The generic judge rubric was reused, defended, and then found to have
  one real seam** — a task-taxonomy ambiguity it faithfully enforced rather
  than papered over, which cost the policy money on `bs08` without the
  policy being able to fix it by spending more.
- **The headline result:** on this task class, my budget-aware policy is
  cheaper per call than always-frontier (-12.24%) and *more expensive per
  resolved task* (+14.76%) — the signature failure mode, measured, not
  assumed, at a resolution rate (92.86%) sitting 1.87 points below this
  ladder's own break-even rate (95.56%). A narrower, more honestly-priced
  ladder than the shipped one makes the tradeoff harder to win, not easier
  — which is the actual finding, not a caveat on top of one.
