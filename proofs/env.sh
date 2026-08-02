# Source before running a proof:  . ./proofs/env.sh
#
# The ports differ from the shipped defaults because 8111 / 8113 / 4318 / 16686
# were occupied on the machine these runs were measured on. Change them back if
# yours are free — nothing in the code assumes either set.
export GLC_BASE_URL=http://127.0.0.1:8121
export S15_OTEL_EXPORTER_ENDPOINT=http://127.0.0.1:4328/v1/traces
export S15_OTEL_SERVICE_NAME=s15code-agent
export S15_OTEL_CAPTURE_CONTENT=0

# Without this, p4 and p9 guess the query API from the OTLP host plus Jaeger's
# default port 16686. If something else is already serving 16686 it will answer,
# and it will answer 404 — which reads as "the trace never arrived" rather than
# "you asked the wrong backend".
export S15_TRACE_QUERY_URL=http://127.0.0.1:16700
