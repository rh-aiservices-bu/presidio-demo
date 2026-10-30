# presidio-ui

A chatbot that makes PII masking visible by offering three modes for the same message:

| Mode | Sent to the LLM | Recorded in MLflow |
|---|---|---|
| **No masking** | original | original |
| **Mask for the model** | masked | masked (same as what was sent) |
| **Mask for the logs** | **original** | masked user text **and** masked reply |

The third is the interesting one — you get a full-fidelity answer that uses the real
name, while the trace contains only placeholders. Verified end to end:

```
streamed    : Logged: Dana Whitfield (PNR: 900116-5678, dana.w@example.com) called about the pump.
logged_user : Log that <PERSON> (pnr <SE_PERSONNUMMER>, <EMAIL_ADDRESS>) called about the pump.
logged_reply: Logged: <PERSON> (PNR: <SE_PERSONNUMMER>, <EMAIL_ADDRESS>) called about the pump.
```

This exists because the lab's `water-plant-ui` cannot show any of this: it calls
`/v1/guardrail/checks` for an allow/block verdict and then sends Hermes the original,
unmasked text. See `../README.md` → "Known issues → The UI is not covered".

## Layout

```
app/
  config.py      settings from env; fails fast on missing keys
  masking.py     masking-service client: /v1/checks, and /analyze for spans
  handles.py     naming the spans: numbering, surrogates, the per-session registry
  llm.py         AsyncOpenAI wrapper; streams, never sends `stop`, sanitizes errors
  tracing.py     MLflow wiring + the span processor that implements "mask for the logs"
  pipeline.py    the three-mode logic and span authoring
  entities.py    entity catalog, ConfigMap read/patch for the Apply button
  schemas.py     request/response models
  main.py        FastAPI routes, lifespan, SSE
  static/        index.html, app.js, style.css
scripts/         local-run.sh, verify.sh

Deployment lives in the Helm chart at ../charts/presidio-guardrails, not here.
```

## Build and deploy

```bash
podman build --platform linux/amd64 -t quay.io/rlundber/presidio-ui:0.0.1 .
podman push quay.io/rlundber/presidio-ui:0.0.1
helm upgrade --install presidio ../charts/presidio-guardrails -n presidio-demo \
  --set ui.image.repository=quay.io/rlundber/presidio-ui --set ui.image.tag=0.0.3
./scripts/verify.sh     # walks all three modes and prints what each recorded
```

To iterate on the UI without a cluster:

```bash
export MODEL_API_KEY=$(oc get secret hermes-env -n user-pk2dq-agentops \
    -o jsonpath='{.data.MODEL_API_KEY}' | base64 -d)
./scripts/local-run.sh      # tracing off, masking via the public NeMo route
```

## How "mask for the logs" works

This is the part worth understanding, because the placement is deliberate.

**"Mask for the model" happens on the request path.** We mask before calling the LLM.
Nothing trace-specific is involved — the span simply records what was sent.

**"Mask for the logs" happens on the trace path**, in a global MLflow span processor
(`tracing.presidio_ui_trace_guard`). `mlflow.tracing.configure(span_processors=[...])`
registers a callback that MLflow invokes **once per span, synchronously inside
`LiveSpan.end()`, before the span is queued for export**. So it sees *every* span
regardless of who created it.

That is the whole reason for the indirection. The obvious alternative — mask the values
we pass to `set_inputs()`/`set_outputs()` — only ever covers spans this app authors. As
soon as there is agentic work, most spans come from instrumentation we don't create
(tool calls, retrievers, sub-agent steps) and PII in tool arguments sails straight
through. The processor approach covers those unchanged. Demonstrated:

```
--- mode=none ---
  pretend.tool   mark=-          in={'query': 'Dana Whitfield, dana.w@example.com, ...'}
--- mode=logs ---
  pretend.tool   mark=processor  in={'query': '<PERSON>, <EMAIL_ADDRESS>, <PHONE_NUMBER>, pnr <SE_PERSONNUMMER>'}
```

`pretend.tool` is a span the app never authored, and masking recurses into nested
structures (`{'rows': [{'note': '<PERSON>, ...'}]}`).

`POST /debug/leak-test` (needs `LEAK_TEST_ENABLED=true`) reproduces exactly that in the
cluster. **Re-run it as the regression test when tools are added.**

### Why it is fail-closed despite MLflow swallowing errors

`mlflow.tracing.utils.processor.apply_span_processors` wraps each processor call in
`try/except` and **discards the exception** — the span would then export *unmasked*. A
span processor therefore cannot be a security guarantee on its own.

The fix is that this only applies to exceptions which *escape* our function. So the
processor catches everything itself and scrubs the span to a placeholder. We never
raise, so the swallow never applies. A registry miss is likewise treated as `logs` and
scrubbed — if the registry ever broke wholesale every trace would become placeholders:
the demo looks broken but nothing leaks, which is the correct direction of failure.

### Cost

The processor is synchronous on the span-end path and a NeMo call is ~26–37ms.
Negligible for a four-span chat turn; roughly 1–2s for a 50-span agent run before cache
hits. Three mitigations: an LRU cache keyed on exact text (agentic spans echo the same
strings constantly, so the hit rate is high), skipping traces not in `logs` mode, and a
local regex pre-filter so text with no PII shape never pays for a round trip.

## Handles and the session mapping

`<PERSON_1>` is **this app's** work, not Presidio's and not the masking service's.
Presidio has no notion of entity instance numbering; `presidio-server` only detects.
The naming lives in `app/handles.py` and the split is deliberate:

```
presidio-server   POST /analyze   -> Presidio's spans for the text
presidio-ui       handles.py      -> what to call each span, and remembering it
```

Numbering only means something across a conversation, which needs session scope and a
retention policy. A shared masking service is the wrong place for either — it would
have to hold per-session PII for every caller. This app already handles the user's
original text, so it is the right place.

### How a turn is masked

1. `masking.analyze_text()` posts the message to `/analyze` and gets spans back.
2. `handles.split_words()` splits `PERSON` spans into one span per word (default — see
   Granularity).
3. `handles.Registry.assign()` issues a handle per `(entity_type, value)`, reusing any
   this session already has.
4. `handles.apply()` rewrites the text back to front, so each replacement leaves the
   offsets before it untouched, and returns the mapping.

Step 3 is why handles are stable. Without a registry, numbering restarts every request:
"Dana" alone is `<PERSON_1>`, and so is "Mike" alone in the next turn — a session-wide
mapping built from that is simply wrong. The registry is a `TTLCache` keyed on the
`session_id` the browser generates per tab (regenerated by **Clear chat**), bounded and
expiring so an abandoned session cannot pin PII forever.

With no `session_id` the registry falls back to per-request numbering rather than
sharing state between callers.

### Styles

`MASK_STYLE`, and optionally `MASK_TOKEN` to override the format. For `<NAME_1>`
instead of `<PERSON_1>`, set `MASK_TOKEN="<NAME_{index}>"`.

| `MASK_STYLE` | Output | Mapping? |
|---|---|---|
| `typed` | `<PERSON>` | **No** — every match of a type collapses to one string, so there is nothing a mapping could say |
| `indexed` (default) | `<PERSON_1>` | Yes |
| `surrogate` | `Peter Jackson` | Yes |

`MASK_TOKEN` is a template over `{entity_type}` and `{index}`: `"[{entity_type}-{index}]"`
gives `[PERSON-1]`. It is validated at startup, so a typo is a failed boot rather than a
surprise on the first masked message. A mapping is only returned when every handle is
distinct — `"PERSON_{index}"` has no `{entity_type}`, so the first `PERSON` and the first
`EMAIL_ADDRESS` both become `PERSON_1`, and the app logs a warning and withholds the
mapping rather than returning one with a repeated handle.

**Before using `surrogate`:** a pseudonymised record is indistinguishable from an
unmasked one — someone reading a trace sees "Peter Jackson" with no way to know it is
not a real operator's name. The fake value is also a deterministic function of the real
one (seeded from it, so a person reads consistently across turns), which means anyone
who can run this can confirm a guess. It keeps a model's output natural; it is not a
substitute for masking in a published record. Entity types with no generator
(`SE_PERSONNUMMER`, `US_SSN`, …) fall back to the indexed handle rather than inventing a
plausible identifier.

### Granularity: why names are split into words

`MASK_GRANULARITY` decides how much of a detected entity gets one handle. Default is
`word`.

```
Sara Banderby met Erwan Granger. Sara liked Erwan.

span   <PERSON_1> met <PERSON_2>. <PERSON_3> liked Erwan.
       PERSON_1='Sara Banderby', PERSON_2='Erwan Granger', PERSON_3='Sara'

word   <NAME_1> <NAME_2> met <NAME_3> <NAME_4>. <NAME_1> liked Erwan.
       NAME_1='Sara', NAME_2='Banderby', NAME_3='Erwan', NAME_4='Granger'
```

Split handles are labelled **`NAME`**, not `PERSON`: a word of a name is not a person,
and `<PERSON_1> <PERSON_2>` for one human reads as two. Only the rendering changes —
the mapping still reports `entity_type: PERSON`, and so does anything downstream.
`DEFAULT_WORD_LABELS` in `handles.py` holds the mapping (`PERSON → NAME`); types with
no entry keep their own name, and `MASK_TOKEN` composes with it
(`"[{entity_type}-{index}]"` gives `[NAME-1]`).

Presidio reports occurrences, not identities, so "Sara Banderby" and a later bare
"Sara" arrive as two unrelated spans. Naming each **word** makes the shared word share
its handle, which is as close to linking the two mentions as you get without
coreference resolution.

**The trade is the opposite error.** Two different people sharing a first name share
that handle: "Sara Banderby" and "Sara Lee" both hold `<PERSON_1>` for "Sara", so the
mapping asserts they are one person. `span` over-counts, `word` under-counts, and
neither is identity resolution. `word` is the default because partial references are
far more common in prose than first-name collisions; `MASK_GRANULARITY=span` swaps the
trade.

Splitting applies only to `MASK_WORD_ENTITIES` (default `PERSON`). Emails, personnummer
and the like are never split — a word is not a meaningful unit there, and they keep
their own labels:

```
Dana Whitfield, dana.w@example.com, pnr 900116-5678
  -> <NAME_1> <NAME_2>, <EMAIL_ADDRESS_1>, pnr <SE_PERSONNUMMER_1>
```

> **`word` pairs badly with `surrogate`.** Each name word gets its own full fake name,
> so "Sara Banderby met Erwan Granger" becomes "Kathleen Kramer Monica Massey met
> Cameron Carey Megan Hernandez" — four invented people for two real ones. Use
> `MASK_GRANULARITY=span` with `MASK_STYLE=surrogate`.

A side benefit: word-level makes a mis-detection visible. Presidio reads
`"Sara stabbed Erwan"` as a single PERSON, which at span level masks the whole clause
to one opaque `<PERSON_3>`. At word level it becomes `<NAME_1> <NAME_5> <NAME_3>` with
`NAME_5='stabbed'` in the mapping — the names still link to their first mentions, and
the bogus entity is plain to see.

### Deduplication is case-insensitive, and still not identity

Two spans share a handle when their text matches case-insensitively, so a later "sara"
reuses the handle issued for "Sara" (the mapping shows the spelling seen first). That is
still string equality, not identity — nothing in this stack resolves which occurrences
are the same person:

Nicknames and short forms that are not substrings still split: "Robert" and "Bob" are
two handles, and so are "Dana" and "Ms Whitfield". Masking stays correct — nothing
leaks — but a mapping can read as more people than were mentioned.

### The panel

*Who became who* is the second block in the left rail, listing every handle the session
has masked against what it stood for. It accumulates across turns and clears with
**Clear chat**. It appears only when the engine serves `/analyze`: `masking.py` probes
once per process, a NeMo server answers 404, and the answer is cached on
`Deps.mapping_supported` — against NeMo the panel says so rather than showing an empty
list.

**Three deliberate constraints:**

* **`model` mode only.** In `logs` mode the model receives the original and only the
  trace is redacted, so a visible handle → value table would be precisely the key that
  undoes the redaction that mode exists to demonstrate. `pipeline.py` withholds the
  mapping there, and the panel explains why instead of going blank.
* **Never in a trace.** The mapping travels in the SSE `meta` event only. No span
  carries it, so the span processor in `tracing.py` never sees it.
* **Not persisted.** The registry is in-memory with a TTL; nothing is written to disk
  and the browser keeps its copy only for the life of the tab.

What is **not** built: rehydrating the model's reply. In `model` mode the answer comes
back full of `<PERSON_1>` and the panel is what you read it with; substituting the real
names back into the reply text would be the natural next step.

## Entity checkboxes

The grid lists all 20 entities — Presidio's 19 built-ins plus `SE_PERSONNUMMER`.

**Per-request entity selection is not possible in this build.** The request schema
exposes `guardrails.config`, described as *"Inline guardrails configuration to use
instead of config_id"*, but the `/v1/checks` handler reads only
`body.guardrails.config_ids`; `grep -noE 'body\.guardrails\.[a-z_]+' api.py` finds
exactly one other reference in the whole file, to `config_id`. The inline field is
accepted, validated and discarded, so an inline config with no rails at all still masks
everything. It is advertised API surface that was never wired up.

So Apply has to write a config. The clean part is *which* config: this app owns a
dedicated `presidio-ui-guardrails-config`, separate from the lab's
`nemo-guardrails-config` (`northgate`). `northgate` is also consumed by
`water-plant-ui`, so letting these checkboxes rewrite it would silently change that
app's behaviour.

Apply therefore:

1. patches `presidio-ui-guardrails-config` (the UI needs `get`/`patch` on that one
   ConfigMap and nothing else);
2. the TrustyAI operator notices the `trustyai.opendatahub.io/nemo-config-hash` change
   on the guardrails pod template and **rolls the pod itself** — which is why no
   Deployment RBAC is required;
3. the UI polls `/v1/rails/configs` until `presidio-ui` answers again, about 30 seconds.

Checkbox state is read back *from the ConfigMap*, so the UI shows what is actually
loaded rather than optimistic local state.

The chart renders the same config from `values.yaml`. Because the Apply button rewrites
the ConfigMap in place, `helm upgrade` reverts it — put anything permanent in
`values.yaml`, or set `ui.entityEditing=false` to remove the button.

## A leak worth remembering

v0.0.2 exported `"Hi my name is Robert"` into a trace unmasked, while masking the reply
in the same span. The cause was a "cheap local pre-filter" that decided which strings
were worth sending to NeMo:

```python
_PII_SHAPED = re.compile(r"\d|@|\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")   # WRONG
```

That sentence has no digit, no `@`, and no two *adjacent* capitalised words, so it was
never sent for masking at all. The reply only got masked by luck: `"Northgate Water"`
happens to match. Presidio's NER sees a PERSON from sentence context; a regex cannot.

The lesson is that a pre-filter on a redaction path must never produce a **false
negative**, because a skipped string is exported verbatim. The filter is now limited to
what is provably safe to skip — text with no alphanumeric characters at all, which no
supported entity can occur in. Everything else goes to NeMo through the LRU cache.

This costs more NeMo calls than the old filter. That is the correct trade.

## Traps this code works around

Each of these cost real debugging time. They are load-bearing, not stylistic.

**`MLFLOW_TRACKING_URI` must be set, even though we export via OTLP.** Without it,
`mlflow.start_span()` → `_resolve_experiment_uc_location()` → `get_tracking_uri()` →
`_get_default_tracking_uri()` → `_has_existing_mlruns_data()` imports the full-mlflow
`FileStore`, which needs `sqlparse`. The slim `mlflow-tracing` package doesn't ship it,
the import raises, `start_span`'s broad `except Exception` swallows it, and **every span
silently becomes a `NoOpSpan`** — no traces at all, nothing logged above DEBUG, and a
`/healthz` that looks perfectly healthy. `install_tracing` sets the variable and then
asserts the warm-up span is a `LiveSpan`, so this can never be silent again. Check
`mlflow.span_type` in `/healthz`.

**Don't `RUN chmod` in the Containerfile.** The `ubi9/python-312` base image already
runs as `USER 1001`, while `COPY` lands files as `root:root`, so a `RUN chmod -R` fails
the build with `Operation not permitted`. It isn't needed anyway: `COPY`'s default
0644/0755 is world-readable, which is exactly what an arbitrary UID requires. This
Containerfile uses `COPY --chown=1001:0` and no chmod. Verified by running the image as
a UID that owns nothing, with a read-only root filesystem and all capabilities dropped:

```
$ podman run --rm --user 65000:0 --read-only --tmpfs /tmp localhost/presidio-ui \
      sh -c 'id; python -c "import app.main; print(\"import OK\")"'
uid=65000(65000) gid=0(root) groups=0(root)
import OK
```

(Testing the real OpenShift UID locally needs more than rootless podman's default
subuid range of 65536 — `--user 1001000000` fails with `cannot setresuid`. Any non-1001
UID proves the same property.)

**`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf` is mandatory.** MLflow defaults the
OTLP protocol to `grpc`; our endpoint is HTTPS `/v1/traces`, so without this nothing
exports.

**Never set `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.** MLflow's cert comes from the
service-serving CA, but the MaaS model endpoint has a public cert. Pointing the global
variables at the service CA alone breaks outbound model calls — the mistake
`hermes-plain` works around with an initContainer that concatenates both bundles. This
app instead passes the CA per-client via `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE`, so the
system trust store stays intact and no initContainer is needed.

**MLflow's REST path has a doubled `/mlflow`**: `<base>/mlflow/api/2.0/mlflow/...`. The
bare `/api/2.0/mlflow/...` form 404s. OTLP ingest, by contrast, is at the server *root*.

**`MLFLOW_URL` is browser-only.** It sits behind an OAuth proxy that answers writes with
a login page as HTTP 200. Writes must use `MLFLOW_REST_BASE`.

**Never call `mlflow.tracing.reset()`** — it wipes `span_processors`, silently disabling
masking.

**Never use `mlflow.tracing.configure()` as a context manager per request.** It mutates
an unguarded module global, so one request's exit clobbers another's config. Register
once at startup, function form only. (`validate_span_processors` also rejects bound
methods — `co_argcount != 2` — so the processor is a module-level function.)

**Never send `stop` to the model.** It is Bedrock-backed via litellm and rejects it with
*"This model doesn't support the stopSequences field"*. This is also why
`/v1/chat/completions` on the guardrails server fails in this lab: its dialog rails send
stop sequences.

**MLflow auth is a Kubernetes token.** The server runs `--app-name=kubernetes-auth`, so
every call becomes a SelfSubjectAccessReview against `mlflow.kubeflow.org/experiments`,
scoped by the `X-MLflow-Workspace` header. `get`+`list` are enough to read and not
enough to write. **Denials are cached for 300s**, so a pod that starts before its
RoleBinding keeps 403-ing for five minutes after you fix it — which is why `apply.sh`
gates on `oc auth can-i` rather than just ordering the applies.

No token refresh logic is needed: the projected volume requests 3607s but this cluster
issues the token with a **365-day** expiry, so the value read at startup stays valid.

## Things deliberately not done

**Single-turn only.** In "mask for the model" mode the history sent to the LLM must be
the *masked* history, so the browser cannot be the source of truth — it needs a
server-side per-session record of what was actually sent. Worth adding, but it is a real
design decision rather than a loose end.

**No `mlflow.openai.autolog()`.** Spans are authored by hand. Autolog writes raw stream
chunks into span *events*, which the processor does not touch — enabling it would
reintroduce a leak in `logs` mode. It also adds no message-level attributes beyond
inputs/outputs, so the cost is about twelve lines.

**PII that only ever flows between tools** — never reaching a span's inputs or outputs —
would escape the processor. Not reachable today; it becomes real with tools.

## Known limitations

- **Presidio misses lowercase names.** Verified against the guardrails service itself:
  `"Hi my name is Robert"` and `"my name is ROBERT"` both mask, `"my name is robert"`
  returns `status: passed`. That is spaCy NER recall, not something this app can fix
  from the outside — a deny-list recognizer would help for known names, but general
  recall on lowercase text stays imperfect. Worth saying out loud when demoing.

- **"Mask for the model" degrades answers.** A masked prompt loses the entity the user
  asked about; in testing the model invented `<PARTY>` from a `<PERSON>` placeholder.
  That is the trade-off the three modes exist to demonstrate.
- **No checksum validation on personnummer**, and phone numbers need a plausible area
  code (`212-555-0199`, not `555-0147`). Both inherited from the guardrails config — see
  `../README.md` → Known issues.
- **The trace list preview is unverified.** Span detail is confirmed masked; whether
  MLflow's trace *list* derives its preview from the same (masked) root-span attributes
  was not checked against server code. Worth a look on first deploy — it would be a
  preview-only leak.
- **Adding the `presidio-ui` entry touches the Argo-managed `NemoGuardrails` CR.** It
  holds today because auto-sync is off on `user-pk2dq-guardrails`, but an upstream push
  to the workshop repo would revert it.
