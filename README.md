# NeMo Guardrails + Presidio — PII masking you can see

Strips personal data out of LLM traffic, and makes the trade-off **visible**: the same
message can be masked before the model sees it, masked only on its way into the logs,
or not at all.

```
you ──┬──────────────→ model          "No masking"
      └──────────────→ MLflow

you ──┬──[ mask ]────→ model          "Mask for the model"
      └──[ mask ]────→ MLflow          the model never sees the real data

you ──┬──────────────→ model          "Mask for the logs"
      └──[ mask ]────→ MLflow          full-fidelity answer, redacted record
```

[Microsoft Presidio](https://presidio.dataprivacystack.org/) does the detection. Two
different services expose it over HTTP, and they are interchangeable — see
[Two engines](#two-engines).

---

## Components

Four pieces. Take only what you need.

| Piece | What it is | Where |
|---|---|---|
| **Guardrails masking service** | Presidio inside a NeMo Guardrails server. Text in, placeholders out. **The core — everything else is optional.** | `charts/presidio-guardrails/` |
| **Example clients** | A curl script and a ~15-line Python client. The fastest way to try it. | `examples/` |
| **presidio-server** | Drop-in alternative to the guardrails service. Same `/v1/checks`, plus `/analyze` — the spans NeMo computes and throws away. | `presidio-server/` |
| **presidio-ui** | A chatbot demonstrating the three modes, with MLflow traces as evidence. | `presidio-ui/` |

Want masking as a service? First two. Want to see *why the mode matters*? Add the UI.

---

## Try it in 60 seconds

Against a running service, no install:

```bash
export NEMO_URL=https://nemo-guardrails-<namespace>.apps.<your-cluster>

curl -s -X POST "$NEMO_URL/v1/checks" -H 'Content-Type: application/json' -d '{
  "model": "any-name",
  "guardrails": {"config_ids": ["presidio"]},
  "messages": [{"role": "user", "content": "Contact Dana Whitfield at dana.w@example.com or 212-555-0199"}]
}'
```

```json
{"status":"modified","content":"Contact <PERSON> at <EMAIL_ADDRESS> or <PHONE_NUMBER>"}
```

`status` is `passed` (nothing found), `modified` (something was masked) or `blocked` (a
`detect`-style rail refused the text).

> **`config_ids` is plural and nested under `guardrails`.** A top-level `config_id` is
> accepted, validated, then silently ignored — so the wrong shape looks like it worked
> while quietly using the server's default config.

Or use the scripts: `./examples/check.sh "Patient personnummer 900116-5678 was admitted."`

---

## Deploy

One Helm chart. Every piece is a toggle, so you can install any subset.

```bash
# Only the UI needs a model key; the masking services make zero LLM calls.
oc create secret generic maas-env --from-literal=MODEL_API_KEY='<your-key>' -n presidio-demo

helm install presidio charts/presidio-guardrails -n presidio-demo --create-namespace
```

Just the masking service — two objects, no secret:

```bash
helm install presidio charts/presidio-guardrails -n presidio-demo --create-namespace \
  --set ui.enabled=false
```

**Prerequisites**

| For | You need |
|---|---|
| Calling a running service | `curl`. Nothing else. |
| Running the masking service | OpenShift + the TrustyAI operator (`oc get crd nemoguardrails.trustyai.opendatahub.io`) and a guardrails image serving `/v1/checks` |
| Running presidio-server | Any Kubernetes. No operator, no CRD. |
| Running the UI | An OpenAI-compatible LLM endpoint, a registry you can push to, and MLflow for traces (optional — without it the UI still chats and masks) |

**Values you must change**

| Value | To |
|---|---|
| `ui.image.repository`, `maskService.image.repository` | your registry |
| `llm.baseUrl`, `llm.model` | your model endpoint |
| `mlflow.restBase`, `mlflow.browserUrl` | your cluster's MLflow |

`helm show values charts/presidio-guardrails` documents every knob. No credentials live
in this repo; the LLM key is always read from a Secret via `secretKeyRef`.

> The UI's entity checkboxes rewrite the guardrails ConfigMap at runtime, and
> `helm upgrade` reverts that. Experiment with the checkboxes; put anything you want to
> keep in `values.yaml`. `ui.entityEditing=false` removes the button and its RBAC.

---

## The three modes

| Mode | Model receives | MLflow records | Intercepted at |
|---|---|---|---|
| No masking | original | original | nowhere |
| Mask for the model | **masked** | masked | the request path |
| Mask for the logs | original | **masked** | the trace path |

Only **two** places in the code intercept anything. Everything else is configuration.

**The request path** — mask, then call the model:

```python
sent  = await mask(user_text)     # the model never sees the real data
reply = await llm.chat(sent)
```

Fails **closed**: if masking is unreachable the turn is refused, not sent unmasked.

**The trace path** — the model gets the original, and masking happens as the trace is
exported, in an MLflow span processor:

```python
mlflow.tracing.configure(span_processors=[trace_guard])
```

MLflow calls that once per span, before export, so it sees **every** span — including
ones this app never created.

That indirection is the whole point. Masking values as you record them only covers
spans you author. Add tool calls, retrievers or sub-agent steps and most spans come
from instrumentation you do not control, with PII sailing straight through a tool
argument. The processor covers those unchanged. Fails by **scrubbing the span**: the
chat still works, because in this mode the model was always going to see the original.

### Two lessons worth keeping

**A pre-filter on a redaction path must never guess.** An earlier version skipped text
that "didn't look like PII" and leaked `"Hi my name is Robert"` — no digit, no `@`, no
two adjacent capitalised words. Presidio reads PERSON from sentence context; a regex
cannot. Only text with *no alphanumeric characters at all* is safe to skip.

**A span processor is not a security guarantee by itself.** MLflow wraps each processor
call in `try/except` and *discards* the exception, exporting the span unmasked. So the
processor catches everything itself and scrubs on any failure — it never raises, so the
swallow never applies.

---

## Two engines

Both serve `/v1/checks`, so the UI's `NEMO_URL` is the only switch between them.

| | Guardrails rail | presidio-server |
|---|---|---|
| `POST /v1/checks` → masked text | yes | yes, byte-identical |
| `POST /analyze` → the detected spans | — | **yes** |
| `score_threshold` honoured when masking | no, pinned at 0.4 | yes |
| Changing the entity list | ConfigMap write + pod roll, ~30s | next request |
| Needs the TrustyAI operator | yes | no |

**presidio-server runs Presidio and returns what Presidio returned.** Two calls are the
whole engine:

```
analyze()           → candidate spans, with confidence scores
anonymize(keep)     → the spans that SURVIVED Presidio's conflict resolution
anonymize(replace)  → masked text, byte-identical to the rail
```

`keep` is the trick worth knowing. Detectors overlap constantly — a `PERSON` inside a
longer `LOCATION`, an `EMAIL_ADDRESS` inside a `URL` — and picking a winner is real
logic. Presidio already has it: `keep` returns the text unchanged plus the matches it
kept. So `/analyze` reports *Presidio's* decision instead of inventing its own.

### Who turns `<PERSON>` into `<PERSON_1>`

Not Presidio, and not the masking service. **The UI does.**

Presidio has no notion of entity instance numbering, and `anonymize()` never reports
what it replaced — its `items` carry the *replacement* plus offsets into the *output*.
There is no original → replacement mapping anywhere in Presidio.

So `presidio-ui/app/handles.py` asks `/analyze` for spans, names each one, and rewrites
the text itself. That placement is deliberate: numbering only means something across a
conversation, which needs session scope and a retention policy — and a shared masking
service holding per-session PII for every caller is a liability. The UI keeps a bounded,
expiring registry keyed on a browser-generated session id. The masking services keep
nothing.

| The UI talks to | Handles | Mapping shown |
|---|---|---|
| the guardrails rail | `<PERSON>`, `<PERSON>` | none — no `/analyze` to call |
| presidio-server | `<PERSON_1>`, `<PERSON_2>` | yes, in the rail panel |

Numbering is stable for the whole conversation, so one person keeps one handle:

```
turn 1   "Dana Whitfield called in"           →  <PERSON_1> called in
turn 2   "Mike Mikelson called in"            →  <PERSON_2> called in
turn 3   "Dana Whitfield called again"        →  <PERSON_1> called again
```

Styles are `MASK_STYLE` on the UI: `typed` (`<PERSON>`), `indexed` (`<PERSON_1>`,
default) or `surrogate` (`Cynthia Obrien` — a stable fake value). `MASK_TOKEN` overrides
the format with a template over `{entity_type}` and `{index}`. Details and the warning
about surrogates are in [`presidio-ui/README.md`](presidio-ui/README.md).

**Two deliberate limits.** The mapping is withheld in *Mask for the logs* — there the
model already has the original and only the trace is redacted, so showing a
handle → value table would hand over the key that undoes it. And `logs` mode records
plain `<PERSON>`, because its authoritative output is the synchronous span processor,
which sees spans with no session context and so can never be registry-aware.

### Detection finds occurrences, not identities

There is no coreference resolution anywhere in the stack:

```
"Dana Whitfield emailed the plant. Dana said the pump failed."
  →  <PERSON_1> emailed the plant. <PERSON_2> said the pump failed.
```

One human, two handles. Short forms, case variants and pronouns all split. Masking
stays correct — nothing leaks — but a mapping reads as more people than were mentioned.

---

## Configuring what gets masked

Presidio ships 19 recognizers; this repo adds a 20th:

`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, `CRYPTO`, `DATE_TIME`,
`IBAN_CODE`, `IP_ADDRESS`, `LOCATION`, `MAC_ADDRESS`, `MEDICAL_LICENSE`, `NRP`,
`UK_NHS`, `URL`, `US_BANK_NUMBER`, `US_DRIVER_LICENSE`, `US_ITIN`, `US_PASSPORT`,
`US_SSN`, **`SE_PERSONNUMMER`**

```bash
helm upgrade presidio charts/presidio-guardrails -n presidio-demo \
  --set 'guardrails.entities={PERSON,EMAIL_ADDRESS,SE_PERSONNUMMER}'
```

The entity list is *what to look for*; recognizers are *how to find it*. **Naming an
entity with no recognizer does nothing.**

### Your own recognizer

The NVIDIA docs only show `deny_list`, but entries go straight to Presidio's
`PatternRecognizer.from_dict()`, so full regexes work:

```yaml
recognizers:
  - name: "Swedish personnummer"
    supported_language: "en"
    supported_entity: "SE_PERSONNUMMER"
    patterns:
      - name: "personnummer"
        regex: '\b(?:19|20)?[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[-+]?[0-9]{4}\b'
        score: 0.6
    context: [personnummer, pnr, personal number]
input:
  entities: [PERSON, EMAIL_ADDRESS, SE_PERSONNUMMER]
```

Three easy mistakes:

- **Single-quote the regex.** In double quotes every backslash needs escaping.
- **List the entity under `entities:` too.** A recognizer alone does nothing.
- **Keep `score` ≥ 0.5** on the guardrails rail — see Gotchas.

`context` words boost confidence nearby: a bare match scores 0.6, but 0.95 next to the
word "personnummer".

---

## Gotchas

Each of these cost real debugging time.

| | |
|---|---|
| **`score_threshold` is ignored when masking** (guardrails rail only) | `mask_sensitive_data` calls the analyzer with no argument and gets a hardcoded **0.4** — not the 0.2 the docs claim. Keep custom pattern scores ≥ 0.5 or they silently never match. presidio-server honours the configured value. |
| **Presidio misses lowercase names** | `Hi my name is Robert` masks; `my name is robert` does not. That is spaCy NER recall, not a config problem. |
| **Phone numbers need a plausible area code** | `212-555-0199` matches; the reserved `555-01xx` form does not. Use realistic numbers when testing. |
| **Bare 10-digit numbers match `PHONE_NUMBER`** | `order 4455667788` gets masked. Drop `PHONE_NUMBER` if order numbers matter more. |
| **No checksum validation** | The personnummer regex is structural only. Luhn validation needs a Python recognizer subclass, which YAML cannot express. |
| **Not every guardrails image serves `/v1/checks`** | `/v1/guardrail/checks` returns a verdict with **no masked text**; `/v1/chat/completions` runs dialog rails and needs a working LLM. Older images have neither. Check with the command below. |

```bash
oc exec deploy/nemo-guardrails -c nemo-guardrails -- \
  curl -s localhost:8000/openapi.json | python3 -c 'import sys,json;print(list(json.load(sys.stdin)["paths"]))'
```

---

## Repo layout

```
charts/presidio-guardrails/   the whole deployment; values.yaml documents every knob
examples/                     standalone clients — check.sh, check.py
presidio-server/              Presidio over HTTP; app/engine.py is two Presidio calls
presidio-ui/                  the chatbot; app/handles.py does the naming
```

Both `presidio-ui/` and `presidio-server/` have their own README with the detail.

The guardrails ConfigMap is mounted, so **config changes need a pod restart** —
the TrustyAI operator does it for you by watching a hash on the pod template, and
presidio-server re-reads the file on the next request instead.
