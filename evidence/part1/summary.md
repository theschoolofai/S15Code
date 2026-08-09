# Part 1: Evidence Capture

Four baseline runs through the live system (S15Code + GLC v4 + Jaeger).
Ladder: cerebras/zai-glm-4.7 (economy) → gemini/gemini-3.1-flash-lite (standard) → openrouter/gpt-4.1 (frontier).

| Task | Difficulty | Answer | Correct? | Tier Used | Cost (USD) | Run ID | Trace ID |
|------|-----------|--------|----------|-----------|------------|--------|----------|
| t01 delay_minutes | trivial | 75 | Yes | frontier | $0.000508 | run-9c4f46d43de8 | 0b4683f5e34eac4520904678834d69e6 |
| t08 evidence_conflict | moderate | {"claim_status":"contradicted","verified_event":"passenger_no_show"} | Yes | frontier | $0.000910 | run-7afb61e9e1cf | f2c4f2ad68a27221b4da9cc6cbcdb14f |
| t11 rebooking_option | hard | A | Yes | frontier | $0.001474 | run-bab43e1364d6 | 01ab16b47f76a04ba171c561e41316fd |
| t15 trace_diagnosis | hard | correct JSON | Yes | frontier | $0.002000 | run-68c6716d8b4e | b9a06554e75891217cd1e46cd8ac27b1 |

## Key Observation

All four tasks routed to frontier regardless of difficulty. The role_tiers mapping
sends every answer_with_evidence node to frontier. A trivial arithmetic task
($0.000508) used the same model as a hard multi-constraint reasoning task ($0.002000).
The 4x cost difference is purely from token count, not model selection.

This is the inefficiency Part 2 will quantify.
