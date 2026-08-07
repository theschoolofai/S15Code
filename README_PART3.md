# S15Code — Part 3

A standalone companion to [README.md](README.md) that covers only **Part 3**
of this submission — the adversarial test against the Part 2 policy. The
policy itself, the ladder and how they were measured are documented in
[README_PART2.md](README_PART2.md); everything below assumes those are in
place.

**TL;DR** — an unbounded planner that earns one more node from every outcome
(the classic denial-of-wallet attack) is run against my Part 2 policy at a
**$0.002** ceiling. It gets **one** call admitted, is refused **199** times,
spends **$0.001133** — **0.01 %** of what the same loop would have cost
uncontrolled — and every refusal is visible in both the ledger and the OTel
span export.

---

## The attack

The same runaway planner p3 uses on the shipped policy, pointed at
`config/part2` instead of the shipped config, with the Part 2 principal.
Command:

```powershell
$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:S15_OTEL_SERVICE_NAME="s15code-part2"

$task = "Adversarial: an unbounded planner will earn one more node from every outcome, forever, even after refusals. Return JSON {category,severity,first_step}."
uv run python proofs/p3_denial_of_wallet.py `
    --task $task --budget 0.002 --config-dir config/part2 `
    --principal "you/part2-attacker" --offline --label part2
```

Numbers straight out of
[proofs/out/p3_denial_of_wallet_part2.json](proofs/out/p3_denial_of_wallet_part2.json)
(raw run log: [proofs/out/_p3_part2.log](proofs/out/_p3_part2.log)):

| metric | before control | after control |
|---|---:|---:|
| Loop rounds executed | 200 (would be unbounded) | 200 (loop ran, controller answered) |
| Nodes planned | 200 | 200 |
| Nodes admitted | *(unbounded — every node)* | **1** *(only one attempt fit under the ceiling)* |
| Refusals | *(would be zero — no controller)* | **199** |
| Spend | ~$11.33 extrapolated over 10 000 rounds | **$0.001133** — **0.01 %** of that |
| Ceiling | none | **$0.002000** — spend stayed under |

## The six invariants that gate the exit code

All six p3 invariants pass:

- **The ceiling held.** `$0.001133 ≤ $0.002000` — not one cent over.
- **Admitted calls bounded by `max_calls_per_run: 60`**, and in practice
  stopped much earlier by the projection check.
- **The loop kept asking and was refused.** 200 rounds, 1 admitted, 199
  refused.
- **A refusal is a visible graph failure**, not a silent truncation: 199
  nodes ended in state `failed` with `BudgetRefused` as the cause.
- **Every provider call that returned is metered.** Transport calls == ledger
  charges == 1.
- **The controller, not the loop, stopped the spend.** The runaway ran to its
  own 200-round test cap; it did not run out of budget in some other way.

## Refusals are visible in the telemetry

Every refused node produces a `refusal_log` entry in the budget snapshot, of
the shape:

```json
{
  "node_id": "planned_node_2",
  "reason": "cheapest tier economy projects 0.001185, run holds 0.000842 (headroom 0.000100)",
  "spent": 0.001133,
  "remaining": 0.000867,
  "pressure": 0.567,
  "action": "refuse",
  "requested_tier": "frontier",
  "tier": null,
  "model": null,
  "projected_cost": 0.001185,
  "allowance": 0.000842,
  "ladder_steps": 0
}
```

Every refusal is:

- **attributed** — principal `you/part2-attacker` is the string the ledger and
  every span carry, so a downstream billing or auditing system can bucket the
  spend to the right party;
- **reasoned** — the entry names which projection failed against which
  threshold, not just "refused";
- **countable** — the ledger's `refusals` counter is exact and matches the
  199 failed nodes in the graph one-for-one.

The span export carries the same story: each refused node produces a `node`
span with `s15.node.state=failed` and `s15.budget.decision=refuse`, and no
child `provider_call` span, because no call was ever made. A reviewer opening
the trace in Jaeger sees the same 200-attempt / 1-admitted / 199-refused
shape the JSON above encodes — no separate refusal channel, no silent
truncation.

---

## Reproducing Part 3 from a fresh checkout

```powershell
git clone <this repo>
cd S15Code
uv sync

# (optional) start a local Jaeger; without it, everything still runs but the
# refused-node spans are only observable in-process, not in a backend UI.
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest

$env:S15_OTEL_EXPORTER_ENDPOINT="http://127.0.0.1:4318/v1/traces"
$env:S15_OTEL_SERVICE_NAME="s15code-part2"

$task = "Adversarial: an unbounded planner will earn one more node from every outcome, forever, even after refusals. Return JSON {category,severity,first_step}."
uv run python proofs/p3_denial_of_wallet.py `
    --task $task --budget 0.002 --config-dir config/part2 `
    --principal "you/part2-attacker" --offline --label part2
```

Artefacts land under [proofs/out/](proofs/out/). 

## What is in Part 3 of this PR

- Attack runner: [proofs/p3_denial_of_wallet.py](proofs/p3_denial_of_wallet.py)
  (unchanged from the shipped one — the whole point of Part 3 is that the
  same generic attack applies to any policy)
- Config it points at: [config/part2/](config/part2/) — same policy Part 2
  measured, no attack-specific hardening
- Machine-readable evidence: [proofs/out/p3_denial_of_wallet_part2.json](proofs/out/p3_denial_of_wallet_part2.json)
- Raw log: [proofs/out/_p3_part2.log](proofs/out/_p3_part2.log)
