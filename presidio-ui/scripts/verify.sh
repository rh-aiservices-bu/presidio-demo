#!/usr/bin/env bash
# End-to-end checks against the deployed Route.
#
# Walks the three modes with a message containing four maskable entities and prints
# what each mode recorded, so the difference is visible without opening MLflow.
set -euo pipefail

NS="${NS:-user-pk2dq-agentops}"
HOST="${HOST:-$(oc get route presidio-ui -n "$NS" -o jsonpath='{.spec.host}')}"
BASE="https://$HOST"
MSG="${MSG:-Summarise this for the shift log: Dana Whitfield, dana.w@example.com, 212-555-0199, pnr 900116-5678 reported the pump fault.}"

echo "=== /healthz ==="
curl -sS "$BASE/healthz" | python3 -m json.tool
echo

for mode in none model logs; do
  echo "=== mode: $mode ==="
  payload=$(MSG="$MSG" MODE="$mode" python3 -c '
import json, os
print(json.dumps({"message": os.environ["MSG"], "mode": os.environ["MODE"]}))')
  # Pull the final meta frame out of the SSE stream and show what was recorded.
  curl -sS -N -X POST "$BASE/api/chat" \
       -H 'Content-Type: application/json' -d "$payload" \
  | python3 -c '
import json, sys
meta = None
for line in sys.stdin:
    if line.startswith("data: "):
        try:
            ev = json.loads(line[6:])
        except ValueError:
            continue
        if ev.get("type") == "meta":
            meta = ev
        elif ev.get("type") == "error":
            print("  ERROR:", ev.get("detail")); sys.exit(0)
if not meta:
    print("  no meta frame returned"); sys.exit(0)
print("  mask_status  :", meta["mask_status"])
print("  logged_user  :", meta["logged_user"])
print("  logged_reply :", (meta["logged_reply"] or "")[:160])
print("  trace_id     :", meta.get("trace_id"))
print("  trace_url    :", meta.get("trace_url"))'
  echo
done

echo "Expected: none -> both original; model -> logged_user masked;"
echo "          logs -> logged_user AND logged_reply masked, while the streamed"
echo "          answer above used the real name."
