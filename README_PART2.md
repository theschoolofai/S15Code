# S15Code — Part 2

A standalone companion to [README.md](README.md) that covers only **Part 2**
of this submission. The full session-15 context — how the budget-aware
runtime, the tier ladder, the durable event journal and the OTel span export
fit together — lives in the main README; everything below assumes that
plumbing is understood. Part 3 (attacking the same policy) is in
[README_PART3.md](README_PART3.md).

**TL;DR** — this branch adds a new task class (18 IT-ticket-triage tickets),
its own ladder and budget policy under [config/part2/](config/part2/), and
measures the policy against an always-frontier baseline. On the same 18
tickets my policy resolves the same **100 %** as always-frontier while
spending **25.3 %** less per resolved task. Every number is the ledger's
number, every trace lands in Jaeger, and every artefact this document
references lives under [proofs/out/](proofs/out/).

---

## 1. The workload

**IT ticket triage** — given a short ticket description, return a JSON object
`{category, severity, first_step}`. Structured output, judgeable, deliberately
different from the essay-style mixed set.

Task file: [proofs/tasks/tickets.jsonl](proofs/tasks/tickets.jsonl) — 18 tasks,
labelled 6 trivial / 6 moderate / 6 hard. Difficulty in offline mode is the
hash bucket the shipped `SimulatedGateway` in
[proofs/p1_cost_per_task.py](proofs/p1_cost_per_task.py) derives from
`sha256(task_text)[:8] mod 3600 + 200`, chosen to line up with the Part 2
ladder cutoffs so each bucket has real separation between rungs. The
pick script that balanced the buckets is at
[proofs/_pick_tickets.py](proofs/_pick_tickets.py).

## 2. The ladder ([config/part2/tiers.yaml](config/part2/tiers.yaml))

| rung | model | max_tokens | projected worst-case admission |
|---|---|---:|---:|
| economy | `openai/gpt-oss-120b` (groq) | 1500 | **$0.001185** |
| standard | `gemini-3.1-flash-lite` (gemini) | 2600 | $0.003920 |
| frontier | `openai/gpt-4.1` (github) | 4096 | **$0.033568** |

Three different models on three different providers. Same models the shipped
ladder uses — this Part is about the *policy*, not about repointing rungs —
but `max_tokens` is right-sized for the short structured output of ticket
triage. **Projected spread top/bottom: 28.3×.**

## 3. The policy ([config/part2/budgets.yaml](config/part2/budgets.yaml))

Five deliberate changes vs the shipped policy, each documented in the file
next to the value:

- `default_budget: 0.05` — sits above frontier's $0.0336 projected
  admission so an always-frontier baseline actually admits on frontier and
  the comparison is honest.
- `downgrade_at: 0.40` (was 0.50) — a three-field JSON rarely earns
  frontier's price when standard would satisfy the same rubric.
- `headroom_fraction: 0.05` (was 0.02) — small output = calls that project
  close to the ceiling are more likely to actually land close to it.
- `max_calls_per_node: 3` (was 6) — a ticket-triage node that needs three
  tries is a signal to stop, not to retry.
- `reserve_fraction: 0.10` (was 0.20) — no long upstream chain to protect
  the terminal node from.

Everything else — the OWASP-shaped denial-of-wallet defence at
`max_calls_per_run: 60`, the `refuse_at: 0.90` line — is kept, and
[README_PART3.md](README_PART3.md) is the check that they still hold under
an adversarial planner.

Reuse rule is the same as the rest of the repo: nothing in `s15code` names a
tier, a model, a price or a threshold. Editing the four files under
[config/part2/](config/part2/) is the entire policy change.

## 4. Measured against the always-frontier baseline

Command:

```powershell
$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
uv run python proofs/p1_cost_per_task.py `
    --tasks proofs/tasks/tickets.jsonl --config-dir config/part2 `
    --principal "you/part2" --offline --label part2
```

Numbers straight out of
[proofs/out/p1_cost_per_task_part2.json](proofs/out/p1_cost_per_task_part2.json)
(raw run log: [proofs/out/_p1_part2.log](proofs/out/_p1_part2.log)):

| strategy | opens on | resolved | calls | cost / call | **cost / resolved task** | total spend |
|---|---|---:|---:|---:|---:|---:|
| **A** always-frontier | frontier | 18 / 18 (100 %) | 18 | $0.014731 | **$0.014731** | $0.26516 |
| **B** always-cheapest, ≤ 2 tries | economy | 6 / 18 (33 %) | 30 | $0.001005 | **$0.005026** | $0.03015 |
| **C** budget-aware cascade *(this policy)* | economy → standard → frontier | 18 / 18 (100 %) | 36 | $0.005501 | **$0.011003** | $0.19805 |

- **C beats A by 25.3 % on cost per resolved task** at the same 100 %
  resolution rate. Total workload savings: **$0.06711** on 18 tickets.
- B's cost per resolved is even lower, but B only resolves 33 % — the other
  12 tickets are unaddressed. Any downstream cost of handling those (a human,
  a retry queue) is not in this number.

Judge is the shipped rubric judge in [s15code/evals/judge.py](s15code/evals/judge.py)
against the panel in [config/part2/evals.yaml](config/part2/evals.yaml) —
`cerebras/zai-glm-4.7` and `openrouter/nvidia/nemotron-3-super-120b-a12b:free`,
disjoint from every rung of the answering ladder, so no answer is graded by
its own model. All 84 answering calls were metered; all 84 rows in the
ledger.

## 5. Break-even, and where this policy sits

The break-even resolution rate for cheap-with-retries B vs frontier-once A is
the resolution rate at which B's total spend across attempts equals A's spend
across one attempt, per resolved task. From the measured ladder spread and 2
attempts allowed:

$$
r_{\text{break-even}} \approx \frac{\text{attempts}_B \cdot (\text{cost/call}_B)}{\text{cost/call}_A} = \frac{2 \cdot \$0.001005}{\$0.014731} \approx 0.128
$$

**Break-even = 12.8 % resolution.** My workload's B strategy sits at **33 %**,
comfortably above break-even — B's cheap-and-give-up policy really is cheaper
per resolved task on the tasks it resolves. It just leaves two thirds of the
workload on the floor.

My policy C sits at **100 %** resolution, so it is nowhere near a break-even
line — the interesting question for C is not "is it worth it vs B" (it is)
but "is it worth its extra spend vs A" (yes, 25.3 % cheaper per resolved).

## 6. One task the policy chose wrongly, and what it cost

[proofs/out/p1_cost_per_task_part2.json](proofs/out/p1_cost_per_task_part2.json)
shows the per-task numbers. The clearest wrong case:

**`tk13_share_drive`** (hard bucket) — same task, same resolved verdict:

| strategy | attempts on this task | cost |
|---|---|---:|
| A always-frontier | 1 (`frontier`) | **$0.02219** |
| C budget-aware *(mine)* | 3 (`economy` → `standard` → `frontier`) | **$0.02726** |

C paid **$0.00507 more per resolved (22.8 % surcharge)** for the identical
outcome, because it burned two low-tier calls before landing on the tier that
could actually resolve the task. That is the *tax C's cascade pays for
optimism when a hard task looks like a trivial one*.

Across all six hard tickets the pattern repeats:

- A on hard: $0.14018 total ($0.02336 avg)
- C on hard: $0.17062 total ($0.02844 avg)
- **C paid $0.03044 more than A on the hard third of the workload.**

That loss is compensated by C's savings on the trivial third (economy
resolves them at ~3 % of frontier's price, saving ~$0.086) — so C still
wins overall. But if the workload shifted to majority-hard, this policy
would lose to A. A production version would need a light classifier in front
of the ladder — even a fifty-line rule set — to skip economy for tickets it
can already tell are hard.

## 7. One run's Jaeger trace with per-span cost

Script: [proofs/capture_part2_trace.py](proofs/capture_part2_trace.py). One
hard ticket (`tk13_share_drive`) run under `config/part2` with a
`--budget 0.05` ceiling; spans exported to the local Jaeger, then fetched
back by id.

- **Jaeger trace id**: `96fac5e7b2e0e3dd912292062959c625`
- **Jaeger view URL**: `http://127.0.0.1:16686/trace/96fac5e7b2e0e3dd912292062959c625`
- **Backend returned 10 spans** with the expected mix of `run`,
  `agent_loop`, `plan`, `node`, `provider_call`.
- The `chat openai/gpt-4.1` provider-call span carries
  `gen_ai.usage.input_tokens=289`, `gen_ai.usage.output_tokens=4000`,
  `gen_ai.request.model=openai/gpt-4.1`, `gen_ai.provider.name=offline_1`,
  and `s15.cost=0.03257800`.
- Sum of `s15.cost` across every provider-call span = **$0.03257800**;
  ledger `spent` = **$0.03257800** — the trace's per-span costs sum
  exactly to the budget ledger.

Full dump: [proofs/out/part2_trace_with_costs.json](proofs/out/part2_trace_with_costs.json).
Raw run log: [proofs/out/_part2_trace.log](proofs/out/_part2_trace.log).

---

## Reproducing Part 2 from a fresh checkout

```powershell
git clone <this repo>
cd S15Code
uv sync

# (optional) start a local Jaeger; without it, everything still runs but the
# "the trace lands in Jaeger" round-trip in the capture script is skipped.
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest

$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:S15_OTEL_SERVICE_NAME="s15code-part2"

# Measurement + one Jaeger trace with per-span costs
uv run python proofs/p1_cost_per_task.py `
    --tasks proofs/tasks/tickets.jsonl --config-dir config/part2 `
    --principal "you/part2" --offline --label part2
uv run python proofs/capture_part2_trace.py
```

Artefacts land under [proofs/out/](proofs/out/).

## What is in Part 2 of this PR

- New task class: [proofs/tasks/tickets.jsonl](proofs/tasks/tickets.jsonl)
- New config: [config/part2/tiers.yaml](config/part2/tiers.yaml) ·
  [config/part2/budgets.yaml](config/part2/budgets.yaml) ·
  [config/part2/pricing.yaml](config/part2/pricing.yaml) ·
  [config/part2/evals.yaml](config/part2/evals.yaml)
- New helpers under `proofs/`: [_pick_tickets.py](proofs/_pick_tickets.py) ·
  [capture_part2_trace.py](proofs/capture_part2_trace.py)
- Machine-readable evidence: [p1_cost_per_task_part2.json](proofs/out/p1_cost_per_task_part2.json) ·
  [part2_trace_with_costs.json](proofs/out/part2_trace_with_costs.json)
- Raw logs alongside each JSON: [_p1_part2.log](proofs/out/_p1_part2.log) ·
  [_part2_trace.log](proofs/out/_part2_trace.log)
