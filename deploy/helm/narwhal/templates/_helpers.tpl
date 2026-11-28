{{/* Labels of one component of the release. */}}
{{- define "narwhal.selector" -}}
app.kubernetes.io/name: narwhal
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* The router pair's ServiceAccount, Role, RoleBinding and Lease. */}}
{{- define "narwhal.routerName" -}}
narwhal-router
{{- end -}}

{{- define "narwhal.labels" -}}
{{ include "narwhal.selector" . }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end -}}

{{/* Refuse direct engines that cannot make one fleet. */}}
{{- define "narwhal.directChecks" -}}
{{- $e := .Values.directEngines -}}
{{- if ne $e.namespace .Release.Namespace -}}
{{- fail (printf "the engine URLs name namespace %s; install the release there, not in %s" $e.namespace .Release.Namespace) -}}
{{- end -}}
{{- if eq $e.serviceName "routers" -}}
{{- fail "the engines' headless Service may not be named routers, the router pair's Service" -}}
{{- end -}}
{{- if eq $e.sharedMount $e.modelMount -}}
{{- fail "directEngines mounts the model and the shared directory at one path; give each its own" -}}
{{- end -}}
{{- end -}}

{{/* The state volume's claim: the direct engines' own, or the chart's. */}}
{{- define "narwhal.stateClaim" -}}
{{- if .Values.directEngines -}}
{{ .Values.directEngines.stateClaim }}
{{- else -}}
{{ .Values.state.claimName }}
{{- end -}}
{{- end -}}

{{/* The controller's ServiceAccount, Role, Deployment and settings. */}}
{{- define "narwhal.controller" -}}
{{ .Release.Name }}-controller
{{- end -}}

{{/* The engines' ServiceAccount, which their attestation sidecars and sidecar-verify Jobs run
as, and its Role. */}}
{{- define "narwhal.engineAccount" -}}
{{ .Release.Name }}-engine
{{- end -}}

{{/* The name of the release's cluster-scoped objects, unique across namespaces. */}}
{{- define "narwhal.clusterName" -}}
{{ .Release.Namespace }}-{{ .Release.Name }}-narwhal
{{- end -}}

{{- /* A list of strings as indented JSON array items, as json.dump(indent=2) writes them. */}}
{{- define "narwhal.jsonLines" -}}
{{- $lines := list -}}
{{- range . }}{{ $lines = append $lines (printf "      %s" (quote .)) }}{{ end -}}
{{- join ",\n" $lines -}}
{{- end }}

{{/* The rendered dashboard that Grafana serves, refused while it is not a rendered dashboard. */}}
{{- define "narwhal.dashboard" -}}
{{- $dashboard := .Values.monitoring.grafana.dashboard -}}
{{- if contains "<<" $dashboard -}}
{{- fail "monitoring.grafana.dashboard holds unrendered placeholders; write it with python -m narwhal.observability.stack.chart_dashboard" -}}
{{- end -}}
{{- if hasKey (fromJson $dashboard) "Error" -}}
{{- fail "monitoring.grafana.dashboard is not JSON; write it with python -m narwhal.observability.stack.chart_dashboard" -}}
{{- end -}}
{{- $dashboard -}}
{{- end -}}

{{/* A job's relabel_configs in prometheus.yml as Prometheus Operator relabelings, with the
fleet in place of <<fleet>>: {"configs", "fleet"}. */}}
{{- define "narwhal.relabelings" -}}
{{- $found := list -}}
{{- range .configs -}}
{{- $item := dict -}}
{{- range $key, $value := . -}}
{{- $_ := set $item (untitle (camelcase $key)) (ternary (replace "<<fleet>>" $.fleet (toString $value)) $value (kindIs "string" $value)) -}}
{{- end -}}
{{- $found = append $found $item -}}
{{- end -}}
{{- toJson $found -}}
{{- end -}}
