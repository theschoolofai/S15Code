# Raw results

Machine-readable output from the live runs. `proofs/out/` is gitignored by repo
convention, so these are copies of the runs the evidence sections cite.

Only **live** runs are here. Simulated (`_floor-offline`) and duplicate (`_mine`)
runs are omitted.

| file | what |
|---|---|
| `proofs/p7_cross_model_ladder_live.json` | ladder liveness: each rung served by the model its config names |
| `proofs/p2_budget_holds_live.json` | four ceilings; downgrade and refusal |
| `proofs/p4_trace_export_live.json` | span-to-ledger reconciliation, content-capture sweep |
| `proofs/p6_cache_savings_live.json` | semantic cache against the real embedder |
| `proofs/p3_denial_of_wallet_live.json` | runaway loop where rate limits bound first |
| `proofs/p3_denial_of_wallet_live-tight.json` | runaway loop where the controller bound: 59 refusals |
| `proofs/p1_cost_per_task_floor-live.json` | shipped `mixed.jsonl` task set |
| `proofs/p1_cost_per_task_wide.json` | Part 2 arm 1, economy ceiling 512 |
| `proofs/p1_cost_per_task_narrow.json` | Part 2 arm 2, all ceilings 1024 |
| `proofs/p1_cost_per_task_attack.json` | Part 3 verbosity attack, no ceiling |
| `proofs/p1_cost_per_task_attack-capped.json` | Part 3 verbosity attack, $0.0006 ceiling |
| `runs/*.json` | the four Part 1 evidence runs: request, response, ledger, event trace |
| `jaeger_trace_f3990d82.json` | one trace as returned by Jaeger's `/api/traces/{id}` |

Every file records its own `mode` — check it says `live` before trusting a number.
See `../LIMITATIONS.md` §G for why that matters.

No API keys, tokens or `.env` content appear in any of these; all were swept
before committing.
