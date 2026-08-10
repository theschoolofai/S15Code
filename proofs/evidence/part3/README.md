# Part 3 — Attack your own budget

## Adversarial test

Script: [`proofs/p_adversarial_support.py`](../../p_adversarial_support.py)  
Wraps `p3_denial_of_wallet` against `config/support_triage` with a tight ceiling.

```bash
uv run python proofs/p_adversarial_support.py --budget 0.002 --principal support/adversarial --base-url http://127.0.0.1:8111
```

Attack shape: **runaway loop** that keeps requesting the most expensive rung.

## Before the control (counterfactual)

| Quantity | Value |
|---|---|
| Measured cost/call (admitted) | ~$0.00004067 |
| Extrapolated uncontrolled bill over 10 000 rounds | **~$0.41** |
| Loop behaviour | never stops asking |

## After the control (measured)

| Quantity | Value |
|---|---|
| Ceiling | **$0.002** |
| Spend | **$0.00158605** (≤ ceiling) |
| Admitted calls | 39 (≤ `max_calls_per_run` 40) |
| Refusals | **161** |
| Loop rounds | 200 |

## Refusal visible in telemetry

- Graph nodes fail with **`BudgetRefused`** (161 refused nodes) — not a silent truncated answer.
- Ledger records refusals separately from charges.
- Summary JSON: [p3_summary.json](../part1/p3_summary.json)

PASS criteria from the live proof: ceiling held; refusals > 0; transport calls == charged calls; controller stopped spend before the loop did.
