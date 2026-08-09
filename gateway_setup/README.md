# Gateway setup for the Session 15 evidence

The evidence in the top-level README runs against **glc_v4**, the gateway. glc_v4
ships with a frontier rung pointed at GitHub Models (`openai/gpt-4.1`), which was
**retired in 2026** — the gateway now returns `HTTP 400 unknown provider 'github'`
for it. This submission repoints the frontier rung (and the judge panel) at **AWS
Bedrock**, which the shipped gateway does not include a provider for.

`glc_v4_bedrock_provider.patch` adds that provider. Apply it to a fresh glc_v4
checkout:

```bash
git clone https://github.com/theschoolofai/glc_v4.git
cd glc_v4
git apply /path/to/S15Code/gateway_setup/glc_v4_bedrock_provider.patch
uv sync           # pulls in boto3
```

## What the patch changes (all in glc_v4, none in S15Code)

- **`glc/providers.py`** — a `BedrockProvider` using the AWS Bedrock Converse API.
  A single adapter serves Claude, Nova and Llama-on-Bedrock; the model is chosen by
  `BEDROCK_MODEL`. Auth uses the standard AWS credential chain (env / profile /
  IMDS), so **no key passes through the gateway**. It advertises `structured` output
  by asking for raw JSON in the system prompt and stripping any markdown fence —
  Converse has no `response_format` field, and without this the judge panel (which
  sends a JSON schema) skips Bedrock with `no_structured`.
- **`glc/routing/core.py`** — Bedrock rate-limit defaults and provider shortcuts,
  plus an honest Groq TPM guard: the shipped 6,000 tokens/min over a rolling 60s
  window was stricter than Groq's own enforcement (live `x-ratelimit-*` headers
  show an 8,000-token bucket with sub-second refill), so the gateway refused
  calls the provider would have served.
- **`glc/routes/chat.py`** — the pinned-provider wait loop now distinguishes
  failure classes: a rolling RPM/TPM window drains on its own, so the gateway
  waits it out (bounded by the existing 30s deadline) instead of returning an
  instant 503; RPD/daily-cap/backoff still fail fast. Without this, every
  strategy that pins the economy rung shows transport failures that look like
  capability failures.
- **`glc/providers.py` (healing)** — newer Anthropic models on Bedrock
  (opus-4.8+, opus-5) reject `temperature` as deprecated; the adapter drops the
  rejected field and retries once — the session's §2 "heal the request" pattern
  on one more provider dialect.
- **`glc/economics/pricing.yaml`** — per-model us-east-1 list prices for the Bedrock
  models used, so a Bedrock call reports real dollars instead of `$0`.
- **`pyproject.toml` / `uv.lock`** — `boto3`.

## Credentials

- `GROQ_API_KEY`, `GEMINI_API_KEY_1..N` in glc_v4's `.env` (never committed).
- Bedrock uses the **AWS default profile** already configured in the AWS CLI; the
  models must be enabled in the account's `us-east-1` region.

## Why a patch and not a PR

The assignment's pull request targets **S15Code**. The gateway change is a
dependency of the evidence, not part of the runtime, so it ships here as a patch a
reviewer can apply in one command rather than as a second PR against glc_v4.
