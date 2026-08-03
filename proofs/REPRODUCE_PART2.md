# Reproducing the code-review policy evidence from a fresh checkout

Every command below was run to produce the figures in the README evidence
section. No step needs a credential in this repository: `S15Code` holds none, and
the gateway (`glc_v4`) is a separate process that owns the keys.

## 0. Prerequisites

```bash
# Jaeger (optional — needed only for the trace evidence)
docker compose -f ../glc_v4/docker-compose.observability.yml up -d
# UI on http://localhost:16686, OTLP on 4317 (gRPC) and 4318 (HTTP)

# the gateway, in its own terminal, from the glc_v4 checkout
cd ../glc_v4
uv sync
cp .env.example .env          # add at least GEMINI_API_KEY_1; never commit this
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
uv run glc serve              # http://127.0.0.1:8111
```

The ladder used here spans three providers, so reproducing the full result needs
`GROQ_API_KEY` (economy), `GEMINI_API_KEY_1` (standard) and
`OPEN_ROUTER_API_KEY` (frontier) in the gateway's `.env`. The judge panel adds
`CEREBRAS_API_KEY` and `NVIDIA_API_KEY`. All are free-tier except OpenRouter,
which meters real credit.

```bash
cd S15Code
uv sync
uv run pytest -q               # 277 passed
uv run ruff check .
```

## 1. The measurement — three strategies over 15 tasks

```bash
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/codereview.jsonl \
  --config-dir config/part2 \
  --principal part2/codereview \
  --base-url http://127.0.0.1:8111 \
  --label part2_codereview
```

Writes `proofs/out/p1_cost_per_task_part2_codereview.json`. ~31 min wall clock.
**Pass `--base-url` explicitly**: an unreachable gateway falls back to a
deterministic offline transport and prints a full table of simulation rather than
failing.

## 2. The fairness correction — frontier with equal attempts

Strategy A is capped at one attempt while B and C take three. This variant sets
`strategies.start: most_capable` so the baseline gets the same three attempts.

```bash
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/codereview.jsonl \
  --config-dir config/part2_frontier_fair \
  --strategies C \
  --principal part2/frontier-fair \
  --base-url http://127.0.0.1:8111 \
  --label frontier_fair
```

The most expensive run here: every call lands on the frontier rung.

## 3. The budget/quality curve — same ladder, tighter ceiling

`config/part2_descend` is identical except `default_budget: 0.004`, which is
below frontier's worst-case projection, so frontier can never be admitted.

```bash
uv run python proofs/p1_cost_per_task.py \
  --tasks proofs/tasks/codereview.jsonl \
  --config-dir config/part2_descend \
  --strategies C \
  --principal part2/descend \
  --base-url http://127.0.0.1:8111 \
  --label descend
```

## 4. The adversarial test — four attacks on this policy

```bash
uv run python proofs/part3_attack.py \
  --budget 0.004 \
  --config-dir config/part2 \
  --base-url http://127.0.0.1:8111 \
  --otel-endpoint http://127.0.0.1:4318/v1/traces
```

Writes `proofs/out/part3_attack.json` and exports one trace per attack. Exits
non-zero if any ceiling is breached.

## 5. Is the judge the noise source?

Generates one answer per task, then submits the byte-identical answer to the
panel ten times. Both judges are free-tier, so this costs nothing.

```bash
uv run python proofs/part2_judge_noise.py \
  --trials 10 \
  --config-dir config/part2 \
  --base-url http://127.0.0.1:8111
```

## 6. The trace evidence — one run, span hierarchy with costs

```bash
# runtime, in its own terminal, pointed at THIS policy
export GLC_BASE_URL=http://127.0.0.1:8111
export S15_OTEL_EXPORTER_ENDPOINT=http://localhost:4318/v1/traces
export S15_OTEL_SERVICE_NAME=s15code-part2
export S15_CONFIG_DIR=config/part2
uv run s15code serve            # http://127.0.0.1:8113

# one budgeted run
curl -s localhost:8113/v1/agent/runs -H 'content-type: application/json' -d '{
  "prompt": "<paste any task from proofs/tasks/codereview.jsonl>",
  "budget": 0.012,
  "tenant_id": "part2", "project_id": "codereview",
  "user_id": "evidence", "agent_id": "reviewer"
}' | jq '{run_id, status, budget}'

# export its spans and read the trace id back
curl -s "localhost:8113/v1/agent/runs/<RUN_ID>/trace?endpoint=http://127.0.0.1:4318/v1/traces" \
  | jq '.totals'

# fetch it back OUT of Jaeger, which is what makes it evidence
curl -s "http://localhost:16686/api/traces/<TRACE_ID>" | jq '.data[0].spans | length'
```

`S15_CONFIG_DIR` is what makes the runtime use this policy rather than the
shipped one. Omit it and the run uses `config/`.

## Notes

- `proofs/out/` is gitignored, so all figures are inlined in the README rather
  than referenced as files.
- Jaeger stores traces in memory; restarting it discards them. Trace ids in the
  README are from the recorded runs and will not resolve on a fresh Jaeger.
- Content capture is off by default (`S15_OTEL_CAPTURE_CONTENT` unset), so no
  prompt or completion text reaches the collector. The adversarial proof asserts
  this.
