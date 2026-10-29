# NeMo Guardrails + Presidio — PII masking you can see

Masks personal data out of LLM traffic, and makes the trade-off visible: the same
message can be masked **before the model sees it**, masked **only on its way into the
logs**, or not masked at all.

```
you ──┬──────────────→ model          "No masking"
      └──────────────→ MLflow

you ──┬──[ mask ]────→ model          "Mask for the model"
      └──[ mask ]────→ MLflow          model never sees the real data

you ──┬──────────────→ model          "Mask for the logs"
      └──[ mask ]────→ MLflow          full-fidelity answer, redacted record
```

Masking is done by **Microsoft Presidio**, running inside a **NeMo Guardrails** server.
Presidio finds the entities; NeMo exposes them over HTTP so anything can call it.

---

## Components

Three pieces, usable independently.

| Piece | What it is | Where |
|---|---|---|
| **NeMo Guardrails + Presidio service** | The actual masking engine. An HTTP service that takes text and returns it with entities replaced by placeholders. **This is the core — everything else is optional.** | `charts/presidio-guardrails/` |
| **Example clients** | A curl script and a Python script that call the masking service directly. The fastest way to try it. | `examples/` |
| **presidio-ui** | A chatbot demonstrating the three modes above, with MLflow traces as evidence. | `presidio-ui/` |

If you only want PII masking as a service, you need the first two. The UI exists to make
the difference between masking *for the model* and masking *for the logs* legible.

---

## Dependencies

**To use the masking service that is already running** — nothing but `curl` or Python
with `requests`. See *Using the masking service on its own* below.

**To run the masking service yourself**, on an OpenShift cluster:

| Requirement | Why | Check |
|---|---|---|
| OpenShift / Kubernetes | hosts the service | `oc whoami` |
| **TrustyAI operator** with the `NemoGuardrails` CRD | creates and manages the guardrails server | `oc get crd nemoguardrails.trustyai.opendatahub.io` |
| A guardrails image exposing `/v1/checks` | the only endpoint that returns *masked text* — see *Server version matters* | `oc exec deploy/nemo-guardrails -- curl -s localhost:8000/openapi.json` |
| An OpenAI-compatible LLM endpoint | only needed by the UI; the mask rails make **zero** LLM calls | — |

The guardrails image already contains `presidio-analyzer`, `presidio-anonymizer`,
`spacy` and the `en_core_web_lg` model. You do not install Presidio yourself.

**To run the UI**, additionally:

| Requirement | Why |
|---|---|
| MLflow with `--app-name=kubernetes-auth` | stores traces; without it the UI still chats, untraced |
| A container registry you can push to | the UI ships as an image you build |
| Python 3.12, `podman` | building and local development |

**For local Presidio experiments without any cluster:**

```bash
pip install presidio-analyzer presidio-anonymizer spacy
python -m spacy download en_core_web_lg      # required: spacy ships no weights
```

If that download is blocked, install the wheel directly:

```bash
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
```

---

## Deploy

Everything ships as one Helm chart. The masking service and the UI are independent
toggles, so you can install either on its own.

```bash
# the UI needs a key for the model; the masking service does not
oc create secret generic maas-env --from-literal=MODEL_API_KEY='<your-key>'

helm install presidio charts/presidio-guardrails --namespace presidio-demo --create-namespace
```

### Just the masking service

```bash
helm install presidio charts/presidio-guardrails -n presidio-demo --create-namespace \
  --set ui.enabled=false
```

That is two objects: the rails ConfigMap and the `NemoGuardrails` CR. No secret needed —
the mask rails make no LLM calls.

### Useful values

| Value | Default | What it does |
|---|---|---|
| `guardrails.entities` | PERSON, EMAIL_ADDRESS, PHONE_NUMBER, SE_PERSONNUMMER | what gets masked |
| `guardrails.recognizers` | the Swedish personnummer pattern | how custom entities are found |
| `guardrails.configId` | `presidio` | the id clients pass as `config_ids` |
| `ui.enabled` | `true` | deploy the demo UI |
| `ui.image.repository` / `.tag` | `quay.io/rlundber/presidio-ui` | **change this to your own image** |
| `llm.baseUrl` / `llm.model` | the workshop MaaS gateway | the UI's model |
| `mlflow.enabled` | `true` | tracing; off means the UI still chats and masks, just records nothing |
| `mlflow.createInstance` | `false` | **cluster-scoped.** Only for clusters with the RHOAI mlflowoperator but no server |

```bash
helm show values charts/presidio-guardrails     # everything, with comments
helm template presidio charts/presidio-guardrails -n presidio-demo | less
```

### Helm and the Apply button

The UI's entity checkboxes rewrite the guardrails ConfigMap at runtime, and
`helm upgrade` reverts that to `values.yaml`. Use the checkboxes to experiment; put
anything you want to keep in `values.yaml`. Set `ui.entityEditing=false` to remove the
button and its RBAC entirely.

### Values you must change

| Value | Where |
|---|---|
| `ui.image.repository` | your registry |
| `llm.baseUrl`, `llm.model` | your model endpoint |
| `mlflow.restBase`, `mlflow.browserUrl` | your cluster's MLflow |

No credentials live in this repo. The LLM key is always read from a Kubernetes Secret
via `secretKeyRef`.

---

## Using the masking service on its own

**This is the part most people want.** The service takes text and returns it masked.
No LLM is involved, no tracing, no UI.

Call `POST /v1/checks`:

```bash
curl -s -X POST "$NEMO_URL/v1/checks" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "<any-model-name>",
    "guardrails": {"config_ids": ["presidio"]},
    "messages": [{"role": "user", "content": "Contact Dana Whitfield at dana.w@example.com or 212-555-0199"}]
  }'
```

```json
{"status":"modified","content":"Contact <PERSON> at <EMAIL_ADDRESS> or <PHONE_NUMBER>"}
```

`status` is `passed` (nothing found), `modified` (something was masked), or `blocked`
(a `detect`-style rail refused the text).

Note `config_ids` — **plural, and nested under `guardrails`**. A top-level `config_id`
is accepted, validated and then silently ignored, so a wrong shape here looks like it
worked while actually using the server's default config.

### The example scripts

Both live in `examples/` and take a message as an argument, or run a sample set:

```bash
export NEMO_URL=https://nemo-guardrails-<namespace>.apps.<your-cluster>

./examples/check.sh "Patient personnummer 900116-5678 was admitted."
# IN : Patient personnummer 900116-5678 was admitted.
# OUT: {"status":"modified","content":"Patient personnummer <SE_PERSONNUMMER> was admitted."}

pip install -r examples/requirements.txt
python examples/check.py          # runs the built-in sample set
```

`examples/check.py` exposes a `check(text)` function returning `{"status", "content"}` —
copy it into your own code; it is about fifteen lines.

### Server version matters

Guardrails images differ in which endpoints they expose, and this bites hard:

| Endpoint | Returns | Use it? |
|---|---|---|
| **`/v1/checks`** | `status` **+ `content`** (the masked text) | **Yes** |
| `/v1/guardrail/checks` | a verdict, **no `content` field** | No — masking is computed and discarded |
| `/v1/chat/completions` | an LLM answer | No — runs dialog rails, needs a working LLM |

Older images have no `/v1/checks` at all. Check before you build on it:

```bash
oc exec deploy/nemo-guardrails -c nemo-guardrails -- \
  curl -s localhost:8000/openapi.json | python3 -c 'import sys,json;print(list(json.load(sys.stdin)["paths"]))'
```

If `/v1/checks` is missing, that image cannot return masked text and you need a newer one.

---

## How the masking is actually applied

There are only **two places** anything is intercepted. Everything else is configuration.

### Point 1 — the request path (before the model)

The app masks the text, then sends the masked version to the LLM. The model never
receives the real data. Nothing trace-specific is involved: whatever is recorded
afterwards is already masked, because that is genuinely what was sent.

```python
sent = (await mask_text(user_text)).content     # NeMo /v1/checks
reply = await llm.chat(sent)                    # model sees <PERSON>, not the name
```

Used by **"Mask for the model"**. Fails **closed**: if the masking service is
unreachable the turn is refused rather than sent unmasked.

### Point 2 — the trace path (on the way into MLflow)

The model gets the **original** text, so the answer keeps full fidelity. Masking happens
later, as the trace is exported, in an MLflow **span processor**:

```python
mlflow.tracing.configure(span_processors=[trace_guard])
```

MLflow calls that function **once per span, before the span is exported**. It therefore
sees *every* span — including ones this app never created.

That last point is the whole reason for the indirection. Masking values as you record
them only covers spans you author yourself. As soon as there are tool calls, retrievers
or sub-agent steps, most spans come from instrumentation you do not control, and PII in
a tool argument goes straight through. The processor covers those unchanged.

Used by **"Mask for the logs"**. Fails by **scrubbing the span** to a placeholder — the
chat still works, because in this mode the model was always going to see the original.

### The three modes, side by side

| Mode | Model receives | MLflow records | Interception point |
|---|---|---|---|
| No masking | original | original | none |
| Mask for the model | **masked** | masked | request path |
| Mask for the logs | original | **masked** | trace path |

### Two details that matter

**A pre-filter on a redaction path must never guess.** An earlier version skipped
strings that "did not look like PII" to save network calls, and leaked
`"Hi my name is Robert"` — no digit, no `@`, no two adjacent capitalised words. Presidio
reads PERSON from sentence context; a regex cannot. Only text with no alphanumeric
characters at all is safe to skip.

**A span processor cannot be a security guarantee on its own.** MLflow wraps each
processor call in `try/except` and *discards* the exception, so a crash exports the span
unmasked. The processor therefore catches everything itself and scrubs on any failure —
it never raises, so the swallow never applies.

---

## Configuring what gets masked

Presidio ships 19 built-in recognizers. This repo adds a 20th for Swedish personnummer.

`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, `CRYPTO`, `DATE_TIME`,
`IBAN_CODE`, `IP_ADDRESS`, `LOCATION`, `MAC_ADDRESS`, `MEDICAL_LICENSE`, `NRP`,
`UK_NHS`, `URL`, `US_BANK_NUMBER`, `US_DRIVER_LICENSE`, `US_ITIN`, `US_PASSPORT`,
`US_SSN`, **`SE_PERSONNUMMER`**

Set `guardrails.entities` in the chart and upgrade. The entity list is what the rail
looks for; the recognizers are how it finds them — naming an entity with no recognizer
does nothing.

```bash
helm upgrade presidio charts/presidio-guardrails -n presidio-demo \
  --set 'guardrails.entities={PERSON,EMAIL_ADDRESS,SE_PERSONNUMMER}'
```

The operator rolls the guardrails pod for you, in about 30 seconds.

### Adding your own recognizer

The NVIDIA docs only show `deny_list`, but entries are passed straight to Presidio's
`PatternRecognizer.from_dict()`, so full regexes work:

```yaml
rails:
  config:
    sensitive_data_detection:
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
        entities: [PERSON, EMAIL_ADDRESS, PHONE_NUMBER, SE_PERSONNUMMER]
```

Two easy mistakes:

- **Use single quotes** for the regex. In double quotes every backslash needs escaping.
- **List the entity in `entities:` too.** Defining a recognizer alone does nothing.

`context` words boost confidence when they appear nearby — a bare match scores 0.6, but
0.95 next to the word "personnummer".

---

## Known issues

**`score_threshold` is ignored when masking.** `detect_sensitive_data` honours your
configured threshold; `mask_sensitive_data` calls the analyzer with no argument and
falls back to a hardcoded **0.4** — not the 0.2 the docs state. Keep custom pattern
scores at **0.5 or above** or they will silently never match.

**Presidio misses lowercase names.** Verified against the service: `Hi my name is
Robert` and `my name is ROBERT` both mask; `my name is robert` returns `status: passed`.
That is spaCy NER recall, not something configuration fixes.

**Phone numbers need a plausible area code.** `212-555-0199` and `+44 20 7946 0958`
match at 0.4; the reserved `555-01xx` form does not. Use a realistic number when testing.

**Bare 10-digit numbers match `PHONE_NUMBER`.** `order 4455667788` is masked. Drop
`PHONE_NUMBER` from the entity list if order numbers matter more than phones.

**No checksum validation.** The personnummer regex is structural only. Real Luhn
validation needs a Python recognizer subclass, which cannot be expressed in YAML.

**Never send `stop` to a Bedrock-backed model.** It is rejected with *"This model
doesn't support the stopSequences field"*. This is also why `/v1/chat/completions` on the
guardrails server fails in some environments — its dialog rails send stop sequences.

---

## Repo layout

```
charts/presidio-guardrails/    the whole deployment
  values.yaml                  every knob, commented
  templates/                   masking service, UI, RBAC, optional MLflow instance
examples/                      standalone clients for the masking service
  check.sh  check.py           send text, print what came back masked
presidio-ui/                   source for the UI image — see its own README
```

The guardrails ConfigMap is mounted, so **any config change needs a pod restart**:

```bash
oc rollout restart deployment/nemo-guardrails
```

Where the TrustyAI operator manages the server it does this for you, by watching a
config hash on the pod template.
