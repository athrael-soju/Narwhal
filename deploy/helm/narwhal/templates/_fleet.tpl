{{- /* Generated from src/narwhal/deployment/fleet_resource.py by tools/maintenance/crd_schema.py. Edit the schema, then run the tool. */}}
{{/* The fleet resource's names, and the rules the admission policy shares with the controller. */}}
{{- define "narwhal.fleet.apiVersion" -}}narwhal.example.com/v1alpha1{{- end -}}
{{- define "narwhal.fleet.kind" -}}NarwhalFleet{{- end -}}
{{- define "narwhal.fleet.name" -}}fleet{{- end -}}
{{- define "narwhal.fleet.plural" -}}narwhalfleets{{- end -}}
{{- define "narwhal.fleet.crdName" -}}narwhalfleets.narwhal.example.com{{- end -}}
{{- define "narwhal.fleet.controllerLease" -}}controller.narwhal.example.com{{- end -}}
{{- define "narwhal.fleet.gateLabel" -}}narwhal.example.com/gate{{- end -}}
{{- define "narwhal.fleet.modelVolume" -}}model{{- end -}}
{{- define "narwhal.fleet.modelVolumeSources" -}}["persistentVolumeClaim", "hostPath", "nfs", "csi"]{{- end -}}
{{- define "narwhal.fleet.hostNetworkGates" -}}["fabric-probe", "fabric-server", "fabric-client", "transfer-measure"]{{- end -}}
{{- define "narwhal.fleet.hostPathRefused" -}}^/(proc|sys|dev|run|var/run|etc|boot|root|home|host|var/log|var/snap|opt/cni|var/lib/(kubelet|etcd|containerd|docker|containers|cri-o|crio|rancher|k0s|cni))(/|$)|^/(var|var/lib|opt)${{- end -}}
{{- define "narwhal.fleet.normalisedPath" -}}^(/[^/\x00-\x1f\x7f-\x9f]+)+${{- end -}}
{{- define "narwhal.fleet.dotSegment" -}}(^|/)\.\.?(/|$){{- end -}}
{{- define "narwhal.fleet.digest" -}}^[^\s@\x00-\x1f\x7f-\x9f]+@sha256:[0-9a-f]{64}${{- end -}}
