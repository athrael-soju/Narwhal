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
{{- $e := .Values.engines -}}
{{- if ne $e.namespace .Release.Namespace -}}
{{- fail (printf "the engine URLs name namespace %s; install the release there, not in %s" $e.namespace .Release.Namespace) -}}
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
{{- range .Values.gates -}}
{{- if and (hasPrefix "fabric-" .kind) (not $.Values.images.fabric) -}}
{{- fail (printf "gate %s needs images.fabric, the image with ib_write_bw" .name) -}}
{{- end -}}
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
