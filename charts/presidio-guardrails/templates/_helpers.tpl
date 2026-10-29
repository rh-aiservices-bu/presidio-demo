{{- define "presidio.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "presidio.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "presidio.name" . -}}
{{- end -}}
{{- end -}}

{{- define "presidio.labels" -}}
app.kubernetes.io/name: {{ include "presidio.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "presidio.ui.name" -}}
{{- printf "%s-ui" (include "presidio.fullname" .) -}}
{{- end -}}

{{- define "presidio.guardrails.name" -}}
{{- printf "%s-nemo" (include "presidio.fullname" .) -}}
{{- end -}}

{{- define "presidio.guardrails.configMapName" -}}
{{- printf "%s-config" (include "presidio.guardrails.name" .) -}}
{{- end -}}

{{/* In-cluster URL of the masking service. The operator names the Service after the CR. */}}
{{- define "presidio.guardrails.url" -}}
{{- printf "http://%s:80" (include "presidio.guardrails.name" .) -}}
{{- end -}}

{{- define "presidio.mlflow.workspace" -}}
{{- default .Release.Namespace .Values.mlflow.workspace -}}
{{- end -}}

{{- define "presidio.mlflow.otlpEndpoint" -}}
{{- if .Values.mlflow.otlpEndpoint -}}
{{- .Values.mlflow.otlpEndpoint -}}
{{- else -}}
{{- printf "%s/v1/traces" (trimSuffix "/" .Values.mlflow.restBase) -}}
{{- end -}}
{{- end -}}

{{/*
  The TrustyAI operator always names the container "nemo-guardrails", whatever the CR
  is called. Service and Route do take the CR name.
*/}}
{{- define "presidio.guardrails.container" -}}
nemo-guardrails
{{- end -}}
