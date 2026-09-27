{{/*
Common labels
*/}}
{{- define "kiwiki.labels" -}}
app.kubernetes.io/name: {{ include "kiwiki.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Replikas brauchen einen geteilten Rate-Limit-Speicher

Die Fehlversuchs-Zaehler liegen bei KIWIKI_RATE_LIMIT_STORE=memory im Prozess-
speicher. Bei mehreren Replikas zaehlt dann jeder Pod fuer sich: die
Installation startet gesund, meldet sich ready und die Drosselung wirkt um den
Faktor der Replikas SCHWAECHER statt staerker — ohne ein einziges Log.

Deshalb bricht das Rendern hier ab, statt die Luecke zu installieren. Wer
mehrere Replikas wirklich braucht, setzt einen RWX-PVC und sqlite.
*/}}
{{- define "kiwiki.assertSharedRateLimitStore" -}}
{{- $replicas := int (.Values.replicaCount | default 1) -}}
{{- $store := (.Values.env.KIWIKI_RATE_LIMIT_STORE | default "memory") | lower -}}
{{- if and (gt $replicas 1) (ne $store "sqlite") -}}
{{- fail (printf "replicaCount=%d verlangt einen geteilten Rate-Limit-Speicher: setze env.KIWIKI_RATE_LIMIT_STORE=sqlite und nutze ein ReadWriteMany-Volume. Sonst zaehlt jeder Pod fuer sich und die Drosselung wird um den Faktor %d schwaecher." $replicas $replicas) -}}
{{- end -}}
{{- if and (gt $replicas 1) (ne (.Values.persistence.accessMode | default "ReadWriteOnce") "ReadWriteMany") -}}
{{- fail (printf "replicaCount=%d braucht persistence.accessMode=ReadWriteMany, damit alle Pods dieselbe SQLite-Datei sehen. Ist: %s" $replicas (.Values.persistence.accessMode | default "ReadWriteOnce")) -}}
{{- end -}}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "kiwiki.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kiwiki.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create chart name and version as used for the chart label
*/}}
{{- define "kiwiki.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common name
*/}}
{{- define "kiwiki.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Full name
*/}}
{{- define "kiwiki.fullname" -}}
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
