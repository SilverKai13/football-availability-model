{{/* Names and labels shared by every template. */}}

{{- define "aiq.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "aiq.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "aiq.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "aiq.labels" -}}
helm.sh/chart: {{ include "aiq.chart" . }}
app.kubernetes.io/name: {{ include "aiq.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "aiq.imageTag" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: availabilityiq
{{- end }}

{{/* Selector labels for one component: api, ui or scoring. */}}
{{- define "aiq.selectorLabels" -}}
app.kubernetes.io/name: {{ include "aiq.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "aiq.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "aiq.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "aiq.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{- define "aiq.image" -}}
{{- printf "%s:%s" .Values.image.repository (include "aiq.imageTag" .) }}
{{- end }}

{{- define "aiq.artifactsClaim" -}}
{{- default (printf "%s-artifacts" (include "aiq.fullname" .)) .Values.artifacts.persistence.existingClaim }}
{{- end }}
