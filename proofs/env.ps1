# Dot-source before running a proof:  . .\proofs\env.ps1
# Ports differ from the shipped defaults because 8111 / 8113 / 4318 / 16686 are
# taken on this machine by unrelated docker stacks.
$env:GLC_BASE_URL = "http://127.0.0.1:8121"
$env:S15_OTEL_EXPORTER_ENDPOINT = "http://127.0.0.1:4328/v1/traces"
$env:S15_OTEL_SERVICE_NAME = "s15code-agent"
$env:S15_OTEL_CAPTURE_CONTENT = "0"
$env:JAEGER_UI = "http://localhost:16700"
# Without this, p4 guesses the query API from the OTLP host and Jaeger's default
# port 16686 — which on this machine is a DIFFERENT project's Jaeger. It answers,
# and it answers 404, which reads as "the trace never arrived" rather than "you
# asked the wrong backend".
$env:S15_TRACE_QUERY_URL = "http://127.0.0.1:16700"
