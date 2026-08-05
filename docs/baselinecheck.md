# Baseline Check — glc_v4 Observability Stack

Session log of getting the Jaeger/OTel observability stack and gateway
provider config working from a clean checkout, plus the test-isolation gap
it exposed. Working directory: `week15/glc_v4`.

## 1. `docker compose` — unknown shorthand flag: 'f' in -f

**Command:** `docker compose -f glc_v4/docker-compose.observability.yml up -d`

**Root cause:** the Docker Compose plugin wasn't installed on the host.
`docker` (from Ubuntu's `docker.io` package) didn't recognize `compose` as a
subcommand, so it tried to parse `-f` against the base `docker` CLI's own
flags — which has no such shorthand — producing the misleading
`unknown shorthand flag: 'f' in -f`.

**Fix:** `sudo apt-get install -y docker-compose-v2` (the `docker.io` repo
doesn't ship `docker-compose-plugin`; `docker-compose-v2` is the equivalent
package that installs the `compose` CLI plugin).

## 2. Jaeger image pull failure

**Error:**
```
failed to resolve reference "docker.io/jaegertracing/all-in-one:2.20.0": not found
```

**Root cause:** `glc_v4/docker-compose.observability.yml` pinned
`jaegertracing/all-in-one:2.20.0`. Jaeger's "v2" architecture (OTel
Collector-based) is real, but the project never shipped `2.x` Docker tags —
only `1.x` (up to `1.76.0` at time of writing) and `latest`, which resolves
to the same digest as `1.76.0`.

**Fix:** changed the image tag to `jaegertracing/all-in-one:1.76.0` in
`glc_v4/docker-compose.observability.yml`. Verified with `docker compose ps`
and `curl http://localhost:16686` (200 OK).

## 3. Gateway `/v1/chat` — unknown provider 'gemini'

**Error:**
```json
{"detail":"unknown provider 'gemini'. Try one of: [] or shortcuts [...]"}
```

**Root cause:** `Try one of: []` — zero providers were registered, not just
Gemini. `glc/providers.py::build_providers()` only registers a provider if
its API key env var is set, and no `.env` file existed in `glc_v4/` (only
`.env.example`), so every provider was skipped.

**Fix:** created `glc_v4/.env` from `.env.example` and filled in real
provider keys (Gemini, NVIDIA, Groq, Cerebras, OpenRouter, GitHub, etc.).

**Follow-up gotcha:** the gateway process (`glc serve`, started via
`uv run glc serve`) was already running from *before* `.env` was created.
`load_dotenv()` in `glc/main.py` only runs once at process import time, so
the live process never picked up the new file. Had to kill and restart the
process for the keys to take effect. Confirmed with:
```
curl -s -X POST http://localhost:8111/v1/chat -d '{"provider":"gemini","messages":[{"role":"user","content":"say hi"}]}'
```
→ `"provider":"gemini_1"` with a real completion.

## 4. Getting the trace ID of the last run

Three ways, once tracing is active:

1. **Response body** — every `/v1/chat` reply includes a `trace` field:
   `{"trace_id": "...", "span_id": "..."}`.
2. **Gateway's own ring buffer** — `GET /v1/traces/recent` (works without
   inspecting individual response bodies).
3. **Jaeger UI** — `http://localhost:16686`, service `glc-v4-gateway`, or
   deep-link directly to `http://localhost:16686/trace/<trace_id>`.

**Gotcha:** tracing was inactive (`"active": false, "exporters": ["none"]`)
because `.env` had no `OTEL_EXPORTER_OTLP_ENDPOINT`, even though the Jaeger
container was up. Added to `glc_v4/.env`:
```
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=glc-v4-gateway
GLC_TRACE_UI=http://localhost:16686
```
then restarted the gateway process again. Verified `trace` was populated
and `/v1/traces/recent` reported `"active": true`.

## 5. Test regression: `test_recent_traces_is_honest_when_tracing_is_off`

**Error:** `tests/test_observability.py:89: AssertionError` — captured
setup showed `telemetry: {'active': True, ...}` when the test expected
tracing off by default.

**Root cause:** `glc/main.py` calls `load_dotenv()` at import time. Once
`.env` had `OTEL_EXPORTER_OTLP_ENDPOINT` set (step 4), *every* test run —
not just manual ones — picked it up, because `tests/conftest.py`'s autouse
`_isolated_glc_state` fixture isolated config/DB paths but never scrubbed
OTEL env vars. The test suite was implicitly relying on the developer's
machine having no local OTel config, which is not a safe assumption.

**Fix:** in `glc_v4/tests/conftest.py`, added `monkeypatch.delenv(...)` for
the OTel-related env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`,
`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`, `OTEL_SERVICE_NAME`, `GLC_TRACE_UI`,
`GLC_JAEGER_UI`, `GLC_OTEL_CONSOLE`, `GLC_OTEL_CAPTURE_CONTENT`,
`GLC_OTEL_IN_MEMORY`) inside the existing autouse `_isolated_glc_state`
fixture, so the "tracing off by default" tests are isolated from local dev
config regardless of what's in `.env`.

**Verification:**
```
uv run pytest tests/test_observability.py -q   # 20 passed
uv run pytest -q                                 # 448 passed, 8 skipped
```

## Files touched

- `glc_v4/docker-compose.observability.yml` — image tag `2.20.0` → `1.76.0`
- `glc_v4/.env` — created from `.env.example`; added provider keys and
  `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME` / `GLC_TRACE_UI`
- `glc_v4/tests/conftest.py` — autouse fixture now scrubs OTel env vars

## Appendix: `uv run pytest -q` full output (glc_v4/)

```
........................................................................ [ 15%]
........................................................................ [ 31%]
........................................................................ [ 47%]
........................................................................ [ 63%]
........................................................................ [ 78%]
.....................................s.................................. [ 94%]
.................sssssss                                                [100%]
=============================== warnings summary ===============================
.venv/lib/python3.14/site-packages/fastapi/testclient.py:1
  /home/acer/Documents/DEEPAK/eva_april2026/week15/glc_v4/.venv/lib/python3.14/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
448 passed, 8 skipped, 1 warning in 54.30s
```

## Appendix: `uv run pytest -q` full output (S15Code/)

```
........................................................................ [ 25%]
........................................................................ [ 51%]
........................................................................ [ 77%]
.............................................................            [100%]
=============================== warnings summary ===============================
.venv/lib/python3.14/site-packages/fastapi/testclient.py:1
  /home/acer/Documents/DEEPAK/eva_april2026/week15/S15Code/.venv/lib/python3.14/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
277 passed, 1 warning in 62.90s (0:01:02)
```
