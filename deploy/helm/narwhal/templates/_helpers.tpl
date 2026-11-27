{{/* Labels of one component of the release. */}}
{{- define "narwhal.selector" -}}
app.kubernetes.io/name: narwhal
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "narwhal.labels" -}}
{{ include "narwhal.selector" . }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end -}}

{{/* Refuse values that cannot make one fleet; the schema checks each value's form. */}}
{{- define "narwhal.checks" -}}
{{- if .Values.directEngines -}}
{{- include "narwhal.directChecks" . -}}
{{- else if .Values.engines -}}
{{- $e := .Values.engines -}}
{{- if and .Values.state.claimName (ne .Values.state.claimName $e.stateClaim) -}}
{{- fail (printf "state.claimName is %s and the engine plans name state claim %s; prepare the engines with NARWHAL_KUBERNETES_STATE_CLAIM set to state.claimName" .Values.state.claimName $e.stateClaim) -}}
{{- end -}}
{{- if ne $e.namespace .Release.Namespace -}}
{{- fail (printf "the engine URLs name namespace %s; install the release there, not in %s" $e.namespace .Release.Namespace) -}}
{{- end -}}
{{- if .Values.placement -}}
{{- range $n := until (int $e.count) -}}
{{- $role := printf "engine-%d" (add1 $n) -}}
{{- if not (hasKey $.Values.placement $role) -}}
{{- fail (printf "placement names no node for %s; every engine pod runs on the node discovery measured it on" $role) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ne $e.sidecarImage .Values.images.narwhal -}}
{{- fail "the engines' attestation sidecars run another Narwhal image than images.narwhal; prepare the engines with NARWHAL_KUBERNETES_SIDECAR_IMAGE set to it" -}}
{{- end -}}
{{- if eq $e.serviceName "routers" -}}
{{- fail "the engines' headless Service may not be named routers, the router pair's Service" -}}
{{- end -}}
{{- $key := .Values.credentials.engineApiKey -}}
{{- range $c := concat $e.podTemplate.initContainers $e.podTemplate.containers -}}
{{- range $c.env -}}
{{- with (.valueFrom | default dict).secretKeyRef -}}
{{- if or (not $key) (ne .name $key.secretName) (ne .key $key.key) -}}
{{- fail (printf "the engines read their API key from Secret %s key %s; set credentials.engineApiKey to that Secret and key, so the routers and gate Jobs send it" .name .key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if and .Values.placement (not .Values.engines) -}}
{{- fail "placement pins engine pods; set engines with it" -}}
{{- end -}}
{{- range .Values.gates -}}
{{- if and (hasPrefix "fabric-" .kind) (not $.Values.images.fabric) -}}
{{- fail (printf "gate %s needs images.fabric, the image with ib_write_bw" .name) -}}
{{- end -}}
{{- if and (hasPrefix "fabric-" .kind) (ne .kind "fabric-probe") (not $.Values.engines) -}}
{{- fail (printf "gate %s measures the engines' RDMA resource; set engines first" .name) -}}
{{- end -}}
{{- if and .serviceAccount (not $.Values.engines) -}}
{{- fail (printf "gate %s runs as the engines' service account; set engines first" .name) -}}
{{- end -}}
{{- end -}}
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

{{/* The state volume's claim, which the engines' values name. */}}
{{- define "narwhal.stateClaim" -}}
{{- if .Values.directEngines -}}
{{ .Values.directEngines.stateClaim }}
{{- else if .Values.engines -}}
{{ .Values.engines.stateClaim }}
{{- else -}}
{{ required "set state.claimName, the state claim's name, for a release without engines" .Values.state.claimName }}
{{- end -}}
{{- end -}}

{{/* The engine pod: the role-neutral template, with the engine image, resource limits and
model volume the values name. */}}
{{- define "narwhal.enginePod" -}}
{{- $e := .Values.engines -}}
{{- $spec := deepCopy $e.podTemplate -}}
{{- $limits := dict $e.gpuResource (toString $e.gpus) $e.rdmaResource "1" -}}
{{- range $c := concat $spec.initContainers $spec.containers -}}
{{- if and (hasKey $c "image") (kindIs "invalid" $c.image) -}}
{{- $_ := set $c "image" $e.image -}}
{{- end -}}
{{- if and (hasKey $c "resources") (kindIs "invalid" $c.resources) -}}
{{- $_ := set $c "resources" (dict "limits" $limits) -}}
{{- end -}}
{{- end -}}
{{- range $v := $spec.volumes -}}
{{- if eq $v.name "model" -}}
{{- $_ := merge $v (deepCopy $e.modelVolume) -}}
{{- end -}}
{{- end -}}
{{- toYaml $spec -}}
{{- end -}}

{{/* The engines' API credential, for the containers that call engines. */}}
{{- define "narwhal.credentials" -}}
{{- with .Values.credentials.engineApiKey }}
- name: {{ .env }}
  valueFrom:
    secretKeyRef:
      name: {{ .secretName }}
      key: {{ .key }}
{{- end }}
{{- end -}}

{{- /* A list of strings as indented JSON array items, as json.dump(indent=2) writes them. */}}
{{- define "narwhal.jsonLines" -}}
{{- $lines := list -}}
{{- range . }}{{ $lines = append $lines (printf "      %s" (quote .)) }}{{ end -}}
{{- join ",\n" $lines -}}
{{- end }}
