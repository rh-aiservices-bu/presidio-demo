#!/usr/bin/env bash
# Run the UI locally with tracing disabled and masking via the public NeMo route.
#
# Tracing needs an in-cluster ServiceAccount token and the MLflow service CA, so it is
# simply off here (no OTEL_EXPORTER_OTLP_TRACES_ENDPOINT). Everything else -- all three
# modes, the audit panel, streaming -- works, which is enough to iterate on the UI.
#
# Needs MODEL_API_KEY. Pull it from the cluster without printing it:
#   export MODEL_API_KEY=$(oc get secret hermes-env -n user-pk2dq-agentops \
#       -o jsonpath='{.data.MODEL_API_KEY}' | base64 -d)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${MODEL_API_KEY:?set MODEL_API_KEY first (see the comment in this script)}"

export LLM_BASE_URL="${LLM_BASE_URL:-https://maas-rhdp.apps.maas.redhatworkshops.io/v1}"
export LLM_MODEL="${LLM_MODEL:-qwen3-235b-a22b}"
export NEMO_URL="${NEMO_URL:-https://nemo-guardrails-user-pk2dq-agentops.apps.ocp.v9nk5.sandbox1255.opentlc.com}"
# Empty => the guardrails server's default config (northgate), which is what is
# reachable when you have not set guardrails.configId to something else.
export NEMO_CONFIG_ID="${NEMO_CONFIG_ID:-}"
export LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
unset OTEL_EXPORTER_OTLP_TRACES_ENDPOINT MLFLOW_REST_BASE NAMESPACE GUARDRAILS_CONFIGMAP

echo "http://127.0.0.1:8080  (tracing disabled, entity editing hidden)"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
