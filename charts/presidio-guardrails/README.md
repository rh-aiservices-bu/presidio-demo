# presidio-guardrails

PII masking with Microsoft Presidio inside a NeMo Guardrails server, plus an optional
UI that shows *where* the masking happens.

```bash
helm install presidio . -n presidio-demo --create-namespace
```

## Two independent pieces

| | Needs | Install |
|---|---|---|
| **Masking service** | the TrustyAI operator | `--set ui.enabled=false` |
| **Demo UI** | an LLM endpoint + key, optionally MLflow | `--set guardrails.enabled=false` to reuse an existing service |

The masking service on its own needs **no secret and no LLM** — verified: with zero
secrets in the namespace,

```
IN : Hi my name is Robert, email dana.w@example.com, pnr 900116-5678, call 212-555-0199
OUT: Hi my name is <PERSON>, email <EMAIL_ADDRESS>, pnr <SE_PERSONNUMMER>, call <PHONE_NUMBER>
```

The UI needs a key for the model it chats with:

```bash
oc create secret generic maas-env --from-literal=MODEL_API_KEY='<your-key>'
```

## Prerequisites

| | Check |
|---|---|
| TrustyAI operator + `NemoGuardrails` CRD | `oc get crd nemoguardrails.trustyai.opendatahub.io` |
| A guardrails image exposing `/v1/checks` | see below — older images cannot return masked text |
| MLflow (UI tracing only) | `oc get svc mlflow -n redhat-ods-applications` |

```bash
oc exec deploy/<release>-nemo -c nemo-guardrails -- \
  curl -s localhost:8000/openapi.json | python3 -c 'import sys,json;print(list(json.load(sys.stdin)["paths"]))'
```

`/v1/checks` is the only endpoint returning masked **text**. `/v1/guardrail/checks`
returns a verdict with no `content` field, and older images lack `/v1/checks` entirely.

## Configuring what gets masked

Entities and recognizers come from `values.yaml`, so no template editing:

```bash
helm upgrade presidio . -n presidio-demo --set 'guardrails.entities={PERSON,EMAIL_ADDRESS}'
```

Changing the config changes a hash on the guardrails pod template, and the operator
rolls the pod for you — about 30 seconds.

Custom recognizers are passed straight to Presidio's `PatternRecognizer.from_dict()`,
so full regexes work (the NVIDIA docs only show `deny_list`). The chart ships a Swedish
personnummer pattern as an example.

## Things worth knowing

**`guardrails.llmBackedRails` is off by default, deliberately.** The mask rails never
call an LLM, but the server *validates at config-load time* that `api_key_env_var`
resolves, and fails the whole config with `Model API Key environment variable
'OPENAI_API_KEY' not set` if it does not. With the flag off the config declares
`models: []` and no key is needed. Turn it on only for self-check or dialog rails — and
then the secret must really exist.

**Keep custom recognizer scores at 0.5 or above.** `mask_sensitive_data` ignores the
configured `score_threshold` and pins the analyzer at a hardcoded 0.4, so anything
lower silently never matches.

**The operator always names the container `nemo-guardrails`**, whatever the CR is
called. Service and Route do take the release-derived name.

**Helm and the Apply button.** The UI's entity checkboxes rewrite the guardrails
ConfigMap at runtime, and `helm upgrade` reverts that to `values.yaml`. Use the
checkboxes to experiment; put anything permanent in `values.yaml`. Set
`ui.entityEditing=false` to drop the button and its RBAC.

**`mlflow.createInstance` is cluster-scoped.** Off by default. Only for clusters that
have the RHOAI `mlflowoperator` but no MLflow server.

**RBAC is created before workloads**, which matters: MLflow's `kubernetes-auth` plugin
caches *denials* for 300 seconds, so a pod that starts before its RoleBinding keeps
403-ing for five minutes after you fix it. Helm's ordering avoids that.

## Values

`helm show values .` prints the full set with comments. The ones you will actually change:

| Value | Default |
|---|---|
| `guardrails.entities` | PERSON, EMAIL_ADDRESS, PHONE_NUMBER, SE_PERSONNUMMER |
| `guardrails.configId` | `presidio` — what clients pass as `config_ids` |
| `ui.image.repository` / `.tag` | `quay.io/rlundber/presidio-ui` / `0.0.3` |
| `llm.baseUrl` / `llm.model` | the workshop MaaS gateway |
| `mlflow.enabled` | `true` |
