#!/usr/bin/env python3
"""Send text through the NeMo Guardrails input rails and show the masked result.

    python check.py "Patient personnummer 900116-5678 was admitted."
    python check.py                 # runs the built-in sample set

Hits /v1/checks, which returns the text AFTER rails processing:

    {"status": "modified", "content": "Patient personnummer <SE_PERSONNUMMER> ..."}

status is one of:
    passed    nothing matched, content unchanged
    modified  something was masked
    blocked   a detect-style rail stopped it (never with mask-only flows)

Note: /v1/guardrail/checks (what the water-plant UI calls) returns a verdict
with no content field, so masking is invisible there. See ../README.md.
"""

import json
import os
import sys

import requests

NEMO = os.environ.get("NEMO_URL", "").rstrip("/")
if not NEMO:
    raise SystemExit(
        "set NEMO_URL to your guardrails route, e.g.\n"
        "  export NEMO_URL=https://nemo-guardrails-<ns>.apps.<cluster>"
    )
CONFIG_ID = os.environ.get("CONFIG_ID", "presidio")
MODEL = os.environ.get("MODEL", "qwen3-235b-a22b")

SAMPLES = [
    "Patient personnummer 900116-5678 was admitted.",
    "Operator ID 19900116-5678 logged the reading.",
    "Short form 9001165678 on the form.",
    "Dana Whitfield, dana.w@example.com, 212-555-0199, pnr 900116+5678",
    "What is the current turbidity at the inlet?",
]


def check(text, timeout=30):
    """Return {'status': ..., 'content': ...} for one message."""
    resp = requests.post(
        f"{NEMO}/v1/checks",
        json={
            "model": MODEL,
            # config_ids (plural), nested under "guardrails". A top-level
            # "config_id" is silently IGNORED by /v1/checks.
            **({"guardrails": {"config_ids": [CONFIG_ID]}} if CONFIG_ID else {}),
            "messages": [{"role": "user", "content": text}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    texts = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else SAMPLES
    for text in texts:
        try:
            res = check(text)
        except requests.RequestException as exc:
            print(f"IN : {text}\nERR: {exc}\n")
            continue
        print(f"IN : {text}")
        print(f"OUT: [{res['status']}] {res.get('content', '')}\n")


if __name__ == "__main__":
    main()
