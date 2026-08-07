# Part 1 — Reproduce the floor

**Measured 2026-08-06 / 2026-08-07**, Windows 11, live against `glc_v4` on
`127.0.0.1:8111`, S15Code runtime on `127.0.0.1:8113`, Jaeger v2.20.0 in WSL2.

---

## 1. Both suites

| suite | observed | assignment claimed | delta |
|---|---|---|---|
| `glc_v4` | **447 passed**, 9 skipped | 445 | +2 |
| `agent_economics` | **275 passed, 2 failed** | 244 | +31, 2 red |

The two failures are **pre-existing**, not caused by this repo's ladder changes —
both reproduce identically against the pristine config extracted from git:

| test | cause |
|---|---|
| `tests/test_runtime_regressions.py::test_birthday_creates_two_real_calendar_artifacts` | `removeprefix("file://")` on `file:///C:/...` yields `/C:/Users/...` → `\C:\Users\...`, invalid on Windows. Correct on POSIX. |
| `s15code/core/a2a/tests/test_hardening.py::test_official_subscription_resumes_waiting_graph_and_maps_cancel` | A2A gRPC race: observed `TASK_STATE_WORKING`, expected `TASK_STATE_COMPLETED` |

---

## 2. The proofs

There are **six**, not five — the assignment's §15 table omits
`p6_cache_savings.py`, which exists and runs. There is no `p5`.

| proof | offline | live | note |
|---|---|---|---|
| `p1_cost_per_task` | ✅ | ✅ 9/9 | shipped `mixed.jsonl`, below |
| `p2_budget_holds` | ✅ 5/5 | ✅ 5/5 | four ceilings, downgrade + refusal |
| `p3_denial_of_wallet` | ✅ 6/6 | ⚠️ 4/6 → ✅ 6/6 | see `PART3_EVIDENCE.md` |
| `p4_trace_export` | ✅ 11/11 | ⚠️ 12/14 | two timing failures, §5 below |
| `p6_cache_savings` | ✅ 4/4 | ✅ 13/13 | real embedder, 4/10 paraphrases cached |
| `p7_cross_model_ladder` | ✅ 10/10 | ✅ 10/10 | ladder liveness gate |

### p7 — the ladder is real

```
economy   groq    openai/gpt-oss-120b              projected 0.00039045  charged 0.00008295
standard  gemini  gemini-3.5-flash-lite            projected 0.00257290  charged 0.00017590
frontier  nvidia  nvidia/nemotron-3-ultra-550b     projected 0.01477140  charged 0.00031140
asked frontier, allowance for economy   -> served economy on groq
asked frontier, allowance for standard  -> served standard on gemini_1
projected spread 37.8x     measured spread 3.75x
```

Every rung served by the model its config names; no two rungs the same model;
three distinct providers; a downgrade changes the **model**, not the token budget.

**The 10× gap between projected and measured spread is the headline.** A price
page describes token rates; a ledger describes answers. See `PART2_EVIDENCE.md`
§3, where the ladder is measured to invert entirely.

### p1 — the shipped task set, live

`--tasks proofs/tasks/mixed.jsonl`, single cerebras judge, 9/9 gates, 989 s.

| strategy | spend | calls | cost/call | resolved | cost/resolved |
|---|---|---|---|---|---|
| A always-frontier | 0.00532080 | 12 | 0.00044340 | **11/12** | 0.00048371 |
| B always-cheapest | 0.00247995 | 13 | 0.00019077 | **12/12** | 0.00020666 |
| C budget-aware | 0.00215475 | 12 | 0.00017956 | **12/12** | 0.00017956 |

`signature failure mode NOT OBSERVED` — B is cheaper on *both* metrics, so there
is nothing to warn about on this set. That reproduces the session's own published
result for `mixed.jsonl`, independently and on a rebuilt ladder.

Two things differ from the published reference, and both point the same way as
Part 2:

* **The frontier rung resolved 11 of 12, the cheapest rung 12 of 12.** The
  reference records both at 12/12. The frontier's miss is a hard task. Capability
  is not monotonic along the price ladder — measured here on the shipped set as
  well as on our own.
* **The budget-aware cascade never left the economy rung** (`tiers economy`), so
  it resolved everything on the first attempt and came out cheapest of the three
  at `$0.00017956` per resolved task — 2.7× cheaper than always-frontier.

Judge meta-cost: 24 calls, `$0.01261360`, **1.27×** the combined answering spend
of all three strategies, with 27 transport retries and 13 verdicts reused.

### p6 — the semantic cache, live

13/13, including the gates that make the saving believable: the embedder is a
real 768-float `ollama/nomic-embed-text` vector (deterministic on repeat, cosine
0.658 on unrelated text — not a constant stub), 4 of 10 paraphrases served from
cache at `$0.00` with `price_source: semantic-cache-hit`, `$0.00041055` and 881
tokens never billed, and all three namespace guards (different model, different
system prompt, different tenant) correctly **miss** on the same question.

---

## 3. The four evidence runs

Ceilings are `p2`'s, derived live from the configured ladder rather than invented.
Full artifacts in `proofs/out/evidence/`.

| # | run | ceiling | tier / model chosen | decision | ledger | trace id |
|---|---|---|---|---|---|---|
| 1 | comfortable | 0.02 | frontier / `nemotron-3-ultra-550b` | proceed | spent 0.000555, 229in/116out, 1 call | `aa402254c5bf1beffcfc8c9bb2201472` |
| 2 | **downgrade** | 0.01353475 | **standard / `gemini-3.5-flash-lite`** | **downgrade** | spent 0.00027810, 377in/66out, 1 call | `8734cc7906b9f93c8ef18c1739c068ee` |
| 3 | **refusal** | 0.00000056 | — | **refuse** | spent 0, **0 calls, 1 refusal** | `ad1f7ed2fcc084f2569596897c6d8bd0` |
| 4 | unbudgeted | *(omitted)* | `gemini-3.5-flash-lite`, **`tier=None`** | — | **cost 0, 0 provider calls** | `29e3eb93f42ab3214410efe046d6d987` |

Prompt, identical across all four: *"Compare two approaches to bounding agent spend."*

Run 2 is a genuine downgrade — the node asked for `frontier` and was served
`standard`, a change of **model and provider**, recorded as
`decision: downgrade`.

Run 3's refusal receipt:

```json
{ "node_id": "answer", "action": "refuse", "requested_tier": "frontier",
  "reason": "cheapest tier economy projects 0.000489, run holds 0.000001 (headroom 0.000000)" }
```

**The budget field is `budget`, not `budget_usd`.** The assignment's §14 example
uses the latter; `RunBody` sets no `extra="forbid"`, so Pydantic silently drops
it and returns HTTP 200 with an *unbudgeted* run. Three of these four runs would
have been quietly worthless.

### Span structure per run

```
1_comfortable  spans=10  {plan:3, node:2, agent_loop:3, provider_call:1, run:1}  cost 0.000555
3_refusal      spans= 9  {plan:3, node:2, agent_loop:3,                  run:1}  cost 0
4_unbudgeted   spans= 9  {plan:3, node:2, agent_loop:3,                  run:1}  cost 0
```

Nine of ten spans cost nothing because nine bought nothing. Structure is free;
only the `provider_call` span is billable — which is precisely the span the
ledger can be checked against.

---

## 4. The honest limitation

Full catalogue in `LIMITATIONS.md`, with seven trace-derived findings and eight
environmental ones. **The one submitted for Part 1:**

> **The trace cannot distinguish a truncated answer from a complete one.**

`gen_ai.response.finish_reasons` is absent from every span. A `provider_call`
span records input tokens, output tokens, cost and reasoning effort — but never
why generation stopped. A call that hit `max_tokens` and truncated mid-sentence,
and one that finished cleanly on `end_turn`, are **identical in the trace**: same
kind, same attribute set, same full charge.

Measured on the frontier rung at `max_tokens 512`, three samples:

| dial | output tokens | visible content | hidden reasoning channel |
|---|---|---|---|
| unset | 486 / 392 / 459 | 483 / 450 / 499 chars | 2070 / 1540 / 1693 chars |
| `off` | 95 | 598 chars | 0 |

The unset run peaked at **486 of 512 — twenty-six tokens short of the ceiling**.
One more verbose sample crosses it and the trace records `output_tokens: 512`, a
cost, and nothing else.

This is not academic. Part 2 measured 18 truncated attempts on the wide ladder,
17 of which failed, and Part 3 turned the same blindness into a **20.7× cost
amplification attack**. The gateway already returns `stop_reason` in its response
envelope; the value exists and is simply not recorded on the span.

*The trace bills you for a truncation it cannot show you.*

---

## 5. Where the floor did not fully reproduce

Reported rather than smoothed over.

**`p4` — 12 of 14 gates.** Two failures, both timing, both demonstrated to be a
race rather than data loss. `p4` queries Jaeger immediately after export with no
wait or retry; 8 of 10 spans had been ingested on one run, 7 of 10 on the next.
The missing spans were *parents*, so their children read back with
`parent=None` and the hierarchy gate failed as a consequence of the completeness
gate. Re-querying the same trace seconds later returns all 10 spans with every
parent resolved. The twelve substantive gates pass on every run — including
`span costs sum to the ledger: delta 0.000e+00`, `span token counts match the
ledger: 229 == 229`, and `no prompt or completion text reached the backend`.

**`p4`'s query URL is unreachable on this platform.** It derives the Jaeger
address from the OTLP endpoint host, so `--otel-endpoint http://127.0.0.1:4318/...`
makes it query `http://127.0.0.1:16686` — which fails, because Jaeger binds its
query server to the IPv6 wildcard and WSL will not relay that to the IPv4
literal. `S15_TRACE_QUERY_URL=http://localhost:16686` fixes it.
(`LIMITATIONS.md` §C.)

**`p1` silently simulates.** It defaults to port **8112** while every other proof
and `GLC_PORT` use 8111, and no proof loads `.env`, so `GLC_BASE_URL` is never
read on that path. A live-intended run against a healthy gateway produced a
complete cost table, a break-even calculation and a `signature failure mode
OBSERVED` finding — **all simulated**, exit code 0, `ok: true`, every gate green.
Always pass `--base-url`. (`LIMITATIONS.md` §G.)

**Two more retired models.** The shipped frontier rung `github/openai/gpt-4.1`
was retired 2026-07-30 — the day `tiers.yaml` recorded its measurement — and
`GEMINI_MODEL`'s default `gemini-2.5-flash` is retired for new users, which fails
every *unbudgeted* run. (`LIMITATIONS.md` §A, §A2.)

**The Jaeger fallback cannot run on Windows** despite Jaeger shipping a Windows
binary, and ships with CRLF endings that break it under WSL. (`LIMITATIONS.md` §B.)

---

## 6. Reproduce

```bash
# services
wsl -d Ubuntu -- bash -lc "~/jaeger_local.sh"
cd glc_v4 && uv run glc serve                    # :8111
cd agent_economics && uv run s15code serve       # :8113

# suites
cd glc_v4 && uv run pytest -q
cd agent_economics && uv run pytest -q

# proofs (--base-url is mandatory for p1; harmless elsewhere)
cd agent_economics
uv run python proofs/p7_cross_model_ladder.py --task "In exactly three sentences, explain why a budget must be enforced in code rather than in a prompt." --base-url http://127.0.0.1:8111 --label live
uv run python proofs/p2_budget_holds.py     --task "Compare two approaches to bounding agent spend." --budget 0.02 --base-url http://127.0.0.1:8111 --label live
uv run python proofs/p6_cache_savings.py    --pairs proofs/pairs/paraphrases.jsonl --base-url http://127.0.0.1:8111 --label live
S15_TRACE_QUERY_URL=http://localhost:16686 \
  uv run python proofs/p4_trace_export.py   --task "Compare two approaches to bounding agent spend." --budget 0.02 --base-url http://127.0.0.1:8111 --otel-endpoint http://127.0.0.1:4318/v1/traces --label live
```

Jaeger UI is `http://localhost:16686` — **not** the IPv4 literal.
