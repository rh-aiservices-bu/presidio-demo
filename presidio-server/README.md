# presidio-server — Presidio, served

A small HTTP service that runs Presidio and returns what Presidio produced. Nothing
more: it holds no state and contains no masking logic of its own.

| | NeMo rail | presidio-server |
|---|---|---|
| `POST /v1/checks` → masked text | yes | yes, byte-identical |
| `POST /analyze` → the detected spans | — | **yes** |
| `score_threshold` honoured when masking | no, pinned at 0.4 | yes |
| Entity list changes | ConfigMap write + pod roll (~30s) | next request |

`/v1/checks` is the slice of the NeMo API that `presidio-ui` uses, so this is a
**drop-in**: point `NEMO_URL` at it and nothing else changes.

`/analyze` is the addition that matters. NeMo computes those spans and discards them;
exposing them is what lets a caller do its own numbering (`<PERSON_1>`),
pseudonymisation or handle → value mapping — see
[`presidio-ui`](../presidio-ui/README.md), which does exactly that.

---

## Why this exists

NeMo's `mask_sensitive_data` action builds its anonymizer operators like this
([source](https://github.com/NVIDIA-NeMo/Guardrails/blob/develop/nemoguardrails/library/sensitive_data_detection/actions.py)):

```python
operators[entity] = OperatorConfig("replace")      # no new_value -> "<ENTITY_TYPE>"
```

`OperatorConfig("replace")` with no parameters is what produces `<PERSON>`, identically
for every match. Three walls follow from it:

1. **No configuration reaches it.** The `sensitive_data_detection` schema has
   `entities`, `recognizers` and `score_threshold` — and a `mask_token` field carrying
   the comment `# TODO: this is not currently in use.`, unchanged since the rail was
   written. Even the knob that looks like it would change the replacement does nothing.
2. **No mapping comes back.** The action does build
   `metadata={"text": ..., "masked_text": ...}`, and then `/v1/checks` drops the
   metadata at the API boundary. The response is `{status, content, rail, reason,
   tool_violations}` — the masked string and nothing else.
3. **Per-occurrence replacement needs a callback.** Presidio's `replace` takes one
   `new_value` per entity *type*, so numbering requires the `custom` operator — and
   Presidio invokes those callbacks in **reverse text order**, which numbers right to
   left unless you handle it.

A fix for (1) is in flight upstream (`mask_token` as a template supporting
`{entity_type}` and `{index}`). (2) is why this service exists: `/analyze` hands back
the spans so a caller can build the mapping itself. (3) is a caller's problem too, and
`presidio-ui` solves it the same way the upstream patch does — resolve the spans first,
then rewrite.

## What it does, and deliberately does not

Two Presidio calls are the whole engine:

```
analyze()            -> candidate spans, with confidence scores
anonymize(keep)      -> the spans that survived Presidio's conflict resolution,
                        as offsets into the submitted text
anonymize(replace)   -> the masked text, byte-identical to NeMo's rail
```

`app/engine.py` is 137 lines, and every one of them is a Presidio call, a docstring or
a dataclass to serialise the result.

**Why `keep`.** `analyze()` returns overlapping candidates — a `PERSON` inside a longer
`LOCATION`, an `EMAIL_ADDRESS` inside a `URL`. Choosing between them is real logic, and
Presidio already has it: anonymizing with the `keep` operator returns the text
unchanged plus the matches it kept. So `/analyze` reports Presidio's decision instead
of making its own, and callers don't reimplement overlap handling either.

**Where the naming went.** `<PERSON_1>`, surrogate names and the handle → value mapping
live in `presidio-ui`. They are application concerns: numbering only means anything
across a conversation, which needs session scope and a retention policy, and a shared
masking service is the wrong place for either — it would hold per-session PII for every
caller. This one holds nothing between requests.

### What Presidio does not give you

Worth stating plainly, because it is easy to assume otherwise. `anonymize()` returns
`EngineResult.items`, and that class's own docstring reads:

> List of PII entities and the indices of their replacements **in the anonymized text**.

Each `OperatorResult` carries `start`, `end`, `entity_type`, `operator` and `text` —
where `text` is the **replacement** (its `from_json` example labels it
`"resulted_text"`) and the offsets point into the **output**. Presidio tells you where
each replacement landed and what it is. It never tells you what was replaced.

There is no original → replacement mapping in Presidio, and
[its docs state it keeps no state](https://presidio.dataprivacystack.org/anonymizer/).
The only built-in reversal is `DeanonymizeEngine` with `decrypt`, which needs a key you
manage and yields ciphertext rather than readable handles.

A mapping falls out of `keep` only because an identity operator leaves the offsets
pointing at the original text, so `text[start:end]` is the original value. That is a
consequence of choosing that operator, not a Presidio feature.

### What detection is not

Presidio reports **occurrences, not identities**. `analyze()` finds every name
separately, with offsets — but nothing resolves which occurrences are the same person.
There is no coreference resolution:

```
"Dana Whitfield emailed the plant. Dana said the pump failed."
  -> two spans: "Dana Whitfield" and "Dana"
```

One human, two spans. Short forms, case variants and pronouns all split. A caller that
deduplicates by string equality inherits this: masking stays correct, but a mapping
reads as more people than were mentioned. Collapsing them needs entity resolution, a
different and much harder problem than detection.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/checks` | NeMo-compatible. `{status, content}` — masked text only, exactly like the rail. |
| `POST /analyze` | `{text, entities?}` → Presidio's surviving spans for that text. |
| `GET /healthz` | Liveness, and the config actually in force. |
| `GET /v1/rails/configs` | NeMo compatibility; `presidio-ui` polls it to tell when a config is live. |

```bash
curl -s localhost:8080/analyze -H 'Content-Type: application/json' \
  -d '{"text":"Dana Whitfield emailed Robert Smith, pnr 900116-5678"}'
```

```json
{
  "spans": [
    {"start":  0, "end": 14, "entity_type": "PERSON",          "text": "Dana Whitfield"},
    {"start": 23, "end": 35, "entity_type": "PERSON",          "text": "Robert Smith"},
    {"start": 41, "end": 52, "entity_type": "SE_PERSONNUMMER", "text": "900116-5678"}
  ],
  "entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "SE_PERSONNUMMER"]
}
```

`text` on each span is the caller's own input echoed back, so a caller never has to
slice by offset and cannot get the slicing wrong.

**`/analyze` returns PII locations**, which is a sharper tool than returning masked
text: anyone who can call it learns exactly where the sensitive data is. It leaks
nothing to the caller — who sent the text in the first place — but it is a reason to
keep this service in-cluster rather than public.

## Configuration

Environment:

| Variable | Default | Meaning |
|---|---|---|
| `CONFIG_PATH` | unset | NeMo-shaped `config.yaml` to read. Unset = built-in defaults. |
| `CONFIG_ID` | `presidio` | What `/v1/rails/configs` reports. |
| `MAX_CHARS` | `20000` | Request size cap. |
| `SPACY_MODEL` | `en_core_web_lg` | Must be installed in the image. |
| `LOG_LEVEL` | `INFO` | Content is never logged above `DEBUG`. |

`CONFIG_PATH` reads NeMo's own schema, so the same ConfigMap can feed both engines:

```yaml
rails:
  config:
    sensitive_data_detection:
      recognizers: [...]            # same shape as NeMo: PatternRecognizer.from_dict
      input:
        entities: [PERSON, EMAIL_ADDRESS, SE_PERSONNUMMER]
        score_threshold: 0.4
```

**A changed file is picked up without a restart.** Mounted ConfigMaps update in place
and the mtime is checked per request, so the UI's entity checkboxes take effect
immediately — no 30-second pod roll.

A `mask_token` in that config is **ignored here**: this service formats nothing. The
equivalent UI settings are `MASK_STYLE` and `MASK_TOKEN` — see
[`presidio-ui`](../presidio-ui/README.md).

Unlike NeMo, `score_threshold` is actually honored on the masking path
([NeMo pins it at 0.4](https://github.com/NVIDIA-NeMo/Guardrails/pull/1967)), so a
custom recognizer's score below 0.4 works here and silently never matches there.

## Run it

```bash
pip install -r requirements.txt
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
CONFIG_PATH=./config.yaml python -m uvicorn app.main:app --port 8080
```

Startup loads the spaCy model and takes tens of seconds; the readiness probe in the
chart allows for it.

```bash
podman build -t quay.io/<you>/presidio-server:0.0.1 .
podman push quay.io/<you>/presidio-server:0.0.1
```

Deploy with the chart:

```bash
helm upgrade --install presidio ../charts/presidio-guardrails \
  --set maskService.enabled=true \
  --set maskService.serveUi=true \
  --set maskService.image.repository=quay.io/<you>/presidio-server
```

`serveUi=true` repoints the UI's `NEMO_URL` here. The guardrails server can stay
deployed; this only moves where the UI sends its masking calls, so you can flip
between the two engines and compare.

## Resources

`en_core_web_lg` is ~590MB on disk and several hundred MB resident. The chart requests
`1Gi` and limits `2Gi` — the UI's `512Mi` is **not** enough and OOMKills while loading
the model. One uvicorn worker on purpose: the model is per-process, and Starlette
already runs the CPU-bound endpoints on a thread pool.
