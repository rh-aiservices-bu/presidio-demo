#!/usr/bin/env bash
# Send text through the NeMo Guardrails input rails and print the masked result.
#
#   ./check.sh "Patient personnummer 900116-5678 was admitted."
#   ./check.sh                      # runs the built-in sample set
#
# Uses /v1/checks, which returns the text AFTER rails processing.
# Do not use /v1/guardrail/checks -- it returns a verdict with no content.
set -euo pipefail

NEMO="${NEMO_URL:?set NEMO_URL to your guardrails route, e.g. https://nemo-guardrails-<ns>.apps.<cluster>}"
# Empty means "let the server use its default config".
CONFIG_ID="${CONFIG_ID:-presidio}"
MODEL="${MODEL:-qwen3-235b-a22b}"

check() {
  local text="$1"
  local payload
  payload=$(MODEL="$MODEL" CONFIG_ID="$CONFIG_ID" TEXT="$text" python3 -c '
import json, os
print(json.dumps({
    "model": os.environ["MODEL"],
    # config_ids (plural), nested under "guardrails". A top-level "config_id" is
    # silently IGNORED by /v1/checks -- the handler only reads guardrails.config_ids.
    **({"guardrails": {"config_ids": [os.environ["CONFIG_ID"]]}}
       if os.environ["CONFIG_ID"] else {}),
    "messages": [{"role": "user", "content": os.environ["TEXT"]}],
}))')
  printf 'IN : %s\n' "$text"
  printf 'OUT: '
  curl -sS -X POST "$NEMO/v1/checks" -H 'Content-Type: application/json' -d "$payload"
  printf '\n\n'
}

if [[ $# -gt 0 ]]; then
  check "$*"
else
  check "Patient personnummer 900116-5678 was admitted."
  check "Operator ID 19900116-5678 logged the reading."
  check "Dana Whitfield, dana.w@example.com, 212-555-0199, pnr 900116+5678"
  check "What is the current turbidity at the inlet?"
fi
