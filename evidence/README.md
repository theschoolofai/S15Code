# Committed evidence artifacts

Raw JSON outputs from the live runs quoted in the top-level README's Evidence
section, committed so a reviewer can inspect the numbers without re-running.
`proofs/out/` itself stays gitignored (the repo's convention); everything here
regenerates from the commands in the README's "Reproduce it" section.

| File | Produced by | What it holds |
|---|---|---|
| `floor_runs.json` | `proofs/capture_floor_runs.py` | Part 1's four captured runs: prompt, tier/model served, ordered events, Jaeger trace IDs, ledger rows, final answers. Two runs complete (span cost == ledger cost exactly); two refused pre-call with zero spend. |
| `p1_cost_per_task_mixed_summary.json` | `proofs/p1_cost_per_task.py --tasks proofs/tasks/options_mixed.jsonl` | **The headline measurement**: 28 mixed tasks (18 computational + 10 high-reasoning), three strategies, zero transport failures. Carries the `signature failure mode OBSERVED` finding — always-cheapest is 17.2% cheaper per call and 14.7% dearer per resolved task than the cascade. Trimmed of long answer excerpts; the full output regenerates on demand. |
| `p1_cost_per_task_options_summary.json` | `proofs/p1_cost_per_task.py --tasks proofs/tasks/options_trading.jsonl` | The computational-half-only measurement: economy resolves 18/18 and the ladder is dead weight — the contrast that shows task *type*, not price, decides the routing regime. |
| `rung_probe.json` | `proofs/probe_rungs.py` | Per-rung correctness on the 18 computational tasks against a hidden answer key. The capability-inversion evidence: economy 18/18, standard 15/18, frontier 14/18. |
| `attack_wallet.json` | `proofs/attack_wallet.py` | Part 3's denial-of-wallet attack: 40 rounds, 2 admitted, 38 refused (HTTP 402), spend $0.01563 vs $0.02 ceiling, all 38 refusals in `/v1/refusals`. |
| `p2_budget_holds__options.json` | `proofs/p2_budget_holds.py` | Ceiling holds / downgrade / refusal on a trading task. |
| `p7_cross_model_ladder__options.json` | `proofs/p7_cross_model_ladder.py` | Each rung served by its configured model; downgrade changes the model; measured 19.5× top-to-bottom spread. |

All figures were measured 2026-08-08/09 against a live glc_v4 (+ Bedrock patch,
see `gateway_setup/`), live Jaeger, and real provider calls on Groq / Gemini /
AWS Bedrock. No secrets, no `.env`, no user data.
