{{/*
Expand the name of the chart.
Avoids double-naming (e.g. url-shortener-url-shortener) if the release name
already contains the chart name.
*/}}
{{- define "url-shortener.fullname" -}}
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

{{/*
Common labels — applied to every resource.
*/}}
{{- define "url-shortener.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: url-shortener
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — used for pod selection.
Must never change after first deploy (would break rolling updates).
*/}}
{{- define "url-shortener.selectorLabels" -}}
app.kubernetes.io/name: url-shortener
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name — use override from values or default to fullname.
*/}}
{{- define "url-shortener.serviceAccountName" -}}
{{- if .Values.serviceAccount.name }}
{{- .Values.serviceAccount.name }}
{{- else }}
{{- include "url-shortener.fullname" . }}
{{- end }}
{{- end }}

{{/*
Redis master host — assembled from release name and namespace.
Matches the Service name created by bitnami/redis subchart.
*/}}
{{- define "url-shortener.redisHost" -}}
{{- printf "%s-redis-master.%s.svc.cluster.local" (include "url-shortener.fullname" .) .Release.Namespace -}}
{{- end }}

{{/*
PostgreSQL primary host — assembled from release name and namespace.
Matches the Service name created by bitnami/postgresql subchart.
*/}}
{{- define "url-shortener.postgresHost" -}}
{{- printf "%s-postgresql.%s.svc.cluster.local" (include "url-shortener.fullname" .) .Release.Namespace -}}
{{- end }}
