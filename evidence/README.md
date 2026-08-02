# Evidence

Raw artefacts behind the numbers in the repository README's assignment section.
Everything here was produced by the proofs in `proofs/` against a live gateway on
2026-08-02; nothing was edited by hand.

`proofs/out/` is gitignored because it is a scratch directory that every run
overwrites. These are the copies that back the claims, kept deliberately.

| File | Produced by |
|---|---|
| `part1_evidence.md` / `.json` | `proofs/part1_evidence.py` — the four captured runs |
| `p1_cost_per_task_part1.json` | `proofs/p1_cost_per_task.py --tasks proofs/tasks/mixed.jsonl` |
| `p2_budget_holds_part1.json` | `proofs/p2_budget_holds.py` |
| `p3_denial_of_wallet_part1.json` | `proofs/p3_denial_of_wallet.py` |
| `p4_trace_export_part1.json` | `proofs/p4_trace_export.py` |
| `p6_cache_savings_part1.json` | `proofs/p6_cache_savings.py --pairs proofs/pairs/paraphrases.jsonl` |
| `p7_cross_model_ladder_part1.json` | `proofs/p7_cross_model_ladder.py` |
| `p8_policy_tradeops_part2.json` | `proofs/p8_policy_tradeops.py --config-dir config/tradeops` |
| `p9_wallet_attack_part3.json` | `proofs/p9_wallet_attack.py --config-dir config/tradeops` |
| `logs/*.log` | stdout of the runs above, verbatim |

Each JSON carries the same shape: the arguments the proof was given, the
economics configuration it resolved, the facts it measured, every check with its
verdict, and a `detail` block with the per-task rows.

No API key, `.env`, credential or personal datum appears in any of these files.
The task text and the model answers are the workload's own, written for this
assignment.
