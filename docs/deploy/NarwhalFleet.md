---
description: Every field of the NarwhalFleet custom resource, its status and its printer columns, generated from the CRD schema.
---

# NarwhalFleet reference

A `NarwhalFleet` holds the deployment inputs of one fleet. The [fleet controller](Kubernetes.md) runs the stage graph for it and writes what it observed to its status. A namespace holds one fleet, named `fleet`.

The API group `narwhal.example.com` is a placeholder. `example.com` is reserved by RFC 2606, so nobody else can own the name, but the group and the version `v1alpha1` will change before the resource reaches `v1beta1`, once the project owns a domain. Every label, annotation, condition type, scheduling gate and Lease name derives from the group, so all of them change with it, and each fleet will need moving to the new group.

The CRD chart, `deploy/helm/narwhal-crds`, installs the schema below. `kubectl explain narwhalfleet.spec` prints the same descriptions from a cluster.

This fleet runs two engines on two nodes. Every image is pinned by digest, and both Secrets exist before the fleet does:

```yaml
apiVersion: narwhal.example.com/v1alpha1
kind: NarwhalFleet
metadata:
  name: fleet
  namespace: narwhal
spec:
  images:
    narwhal: registry.example.com/narwhal@sha256:<64 hex digits>
    haproxy: registry.example.com/haproxy@sha256:<64 hex digits>
    fabric: registry.example.com/perftest@sha256:<64 hex digits>
  engines:
    image: registry.example.com/engine@sha256:<64 hex digits>
    model:
      name: <served model name>
      treeSha256: <model_tree_sha256 of the staged checkpoint>
    modelVolume:
      persistentVolumeClaim:
        claimName: models
    resources:
      gpu: gpu.example.com/gpu
      rdma: rdma.example.com/hca
    ports:
      engine: 8001
      attestation: 8101
      sideChannel: 5601
      ucxTcpPortRange: 20000-20099
    nodes:
      - node: gpu-node-a
        fabricInterface: eth1
      - node: gpu-node-b
        fabricInterface: eth1
    apiKeySecret:
      name: engine-key-v1
      key: key
    launch:
      - name: NARWHAL_TRANSFER_TRANSPORT
        value: ucx_rdma
      - name: NARWHAL_TRANSFER_NET_DEVICES
        value: mlx5_0:1
  slo:
    ttftS: "2.0"
    tpotS: "0.1"
    routerOverheadS: "0.02"
  firstTokenMarginS: "0.5"
  profiling:
    prefillLens: [512, 2048, 8192]
    decodeInputLens: [512, 2048, 8192]
    warmupRequests: 2
  calibration:
    inputTokens: [512, 2048, 8192]
    samples: 100
    observationTimeoutS: "120"
  fabric:
    workload:
      promptTokens: 8192
      handoffsPerS: "4"
      burst: 8
      transferBudgetS: "0.5"
      headroom: "1.5"
    testPort: 18515
  routers:
    storageClassName: standard
    size: 1Gi
  controlTokenSecret:
    name: router-token-v1
    key: token
```

Decimal fields such as `slo.ttftS` are strings, as the Kubernetes API conventions keep floating point out of a spec. The values above show the shape only. [Deployment inputs](../configuration/04-Deployment-Inputs.md#kubernetes-target-inputs) names the variable each field becomes, and the sections it links explain how to choose them.

Every engine of a fleet transfers KV over `ucx_rdma`, which `engines.launch` sets as above. Two engines may share a node. Engine pods run on the node's host network, so give each engine after the first its own ports in its entry of `engines.nodes`, and give each its GPUs with `NARWHAL_NODE_<n>_GPU_IDS` in `engines.launch`:

```yaml
    nodes:
      - node: gpu-node-a
        fabricInterface: eth1
      - node: gpu-node-a
        fabricInterface: eth2
        ports:
          engine: 8002
          attestation: 8102
          sideChannel: 5602
          ucxTcpPortRange: 20100-20199
    launch:
      - name: NARWHAL_NODE_1_GPU_IDS
        value: "0"
      - name: NARWHAL_NODE_2_GPU_IDS
        value: "1"
```

The API server refuses an engine's port inside its own UCX TCP port range. The controller refuses, with reason `InputRefused`, a fleet whose engines on one node share a port or overlapping UCX TCP port ranges, or whose engine port is `fabric.testPort`.

Each GPU node runs the engines of one fleet only. The engine pods use the host network, and the controller sees only its own namespace, so two fleets on one node would take each other's ports and GPUs. Give each fleet its own nodes in `engines.nodes`. `discover` fails, rather than starting an engine, when another workload holds the node's GPUs or listens on the engine's ports, as [Discovering the engine nodes](Kubernetes.md#discovering-the-engine-nodes) describes.

## Fields

<!-- Generated from SCHEMA in src/narwhal/deployment/fleet_resource.py by tools/maintenance/fleet_reference_doc.py. Edit the schema, then run the tool. -->

`apiVersion: narwhal.example.com/v1alpha1`, `kind: NarwhalFleet`, scope `Namespaced`, plural `narwhalfleets`, category `narwhal`. The API server refuses a fleet that breaks these rules:

- a namespace holds one NarwhalFleet, and it is named fleet.
- controlTokenSecret names the Secret and key of engines.apiKeySecret; keep the control token in its own Secret.

### Spec

A field marked Yes is required whenever the field that holds it is set.

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `spec.images` | object | Yes | The third-party images the fleet runs besides the engine's, each by registry digest. The routers, gate Jobs and attestation sidecars run the controller's own image, which the narwhal chart's images.narwhal pins. |
| `spec.images.haproxy` | string | Yes | The HAProxy image. At most 512 characters. |
| `spec.images.fabric` | string | Yes | The image the fabric Jobs run ib_write_bw from. At most 512 characters. |
| `spec.engines` | object | Yes | The engine pods. |
| `spec.engines.image` | string | Yes | The engine image by registry digest. At most 512 characters. |
| `spec.engines.model` | object | Yes | The checkpoint the engines serve. API server rule: set file and fileSha256 together. |
| `spec.engines.model.name` | string | Yes | The served model name. 1 to 256 characters. |
| `spec.engines.model.treeSha256` | string | Yes | The checkpoint's model_tree_sha256. At most 64 characters. |
| `spec.engines.model.file` | string |  | For an engine that serves one model file, its path in the model volume. At most 1024 characters. API server rule: must not hold a . or .. segment. |
| `spec.engines.model.fileSha256` | string |  | The model file's SHA-256. At most 64 characters. |
| `spec.engines.modelVolume` | object | Yes | The volume that serves the staged checkpoint to every engine pod, read-only. Set exactly one source. API server rule: set exactly one of persistentVolumeClaim, hostPath, nfs, csi. |
| `spec.engines.modelVolume.persistentVolumeClaim` | object |  | A PersistentVolumeClaim in the fleet's namespace. |
| `spec.engines.modelVolume.persistentVolumeClaim.claimName` | string | Yes | The claim's name. At most 253 characters. |
| `spec.engines.modelVolume.persistentVolumeClaim.readOnly` | boolean |  | Always true: engines read the model and never write it. Default `true`. API server rule: the model volume is mounted read-only. |
| `spec.engines.modelVolume.hostPath` | object |  | A directory on every engine node. The controller refuses it unless the cluster admin lists hostPath in the narwhal chart's modelVolumeSources and a prefix that holds it in modelHostPathPrefixes, which is empty by default. |
| `spec.engines.modelVolume.hostPath.path` | string | Yes | The directory's normalised absolute path on the node. At most 4096 characters. API server rule: must not hold a . or .. segment. API server rule: must not be or lie in a node's system, kubelet or container runtime directory. |
| `spec.engines.modelVolume.hostPath.type` | string | Yes | Directory: the kubelet refuses a path that is not an existing directory. One of `Directory`. |
| `spec.engines.modelVolume.nfs` | object |  | An NFS export. The controller refuses it unless the cluster admin lists nfs in the narwhal chart's modelVolumeSources and its server in modelNfsServers. |
| `spec.engines.modelVolume.nfs.server` | string | Yes | The NFS server's host name or address. At most 253 characters. |
| `spec.engines.modelVolume.nfs.path` | string | Yes | The exported directory. At most 4096 characters. API server rule: must not hold a . or .. segment. |
| `spec.engines.modelVolume.nfs.readOnly` | boolean |  | Always true: engines read the model and never write it. Default `true`. API server rule: the model volume is mounted read-only. |
| `spec.engines.modelVolume.csi` | object |  | A CSI ephemeral volume. The controller refuses it unless the cluster admin lists csi in the narwhal chart's modelVolumeSources and its driver in modelCsiDrivers. |
| `spec.engines.modelVolume.csi.driver` | string | Yes | The CSI driver's name. At most 63 characters. |
| `spec.engines.modelVolume.csi.readOnly` | boolean |  | Always true: engines read the model and never write it. Default `true`. API server rule: the model volume is mounted read-only. |
| `spec.engines.modelVolume.csi.volumeAttributes` | map of strings |  | The driver's volume attributes. At most 32 entries. API server rule: an attribute name holds at most 253 characters. |
| `spec.engines.resources` | object | Yes | The device plugins' extended resources, and the engine pods' RuntimeClass. API server rule: gpu and rdma must name two resources. |
| `spec.engines.resources.gpu` | string | Yes | The GPU device plugin's extended resource name, <domain>/<name>. At most 316 characters. API server rule: the kubernetes.io and k8s.io domains are reserved. |
| `spec.engines.resources.rdma` | string | Yes | The RDMA device plugin's extended resource name, which hands each engine pod one HCA. At most 316 characters. API server rule: the kubernetes.io and k8s.io domains are reserved. |
| `spec.engines.resources.runtimeClassName` | string |  | The RuntimeClass of nodes whose default container runtime exposes no GPUs. At most 253 characters. |
| `spec.engines.ports` | object | Yes | The ports each engine pod serves on, unless its entry of nodes sets its own. API server rule: the engine, attestation and side channel ports must differ. API server rule: the engine, attestation and side channel ports must lie outside the UCX TCP port range. |
| `spec.engines.ports.engine` | integer | Yes | The engine's HTTP port. From 1 to 65535. |
| `spec.engines.ports.attestation` | integer | Yes | The attestation sidecar's HTTP port. From 1 to 65535. |
| `spec.engines.ports.sideChannel` | integer | Yes | The port the engine's KV connector listens on for its peers' handshakes. From 1 to 65535. |
| `spec.engines.ports.ucxTcpPortRange` | string | Yes | LOW-HIGH, the ports UCX TCP listeners take. At most 11 characters. |
| `spec.engines.nodes` | list of objects | Yes | The engines, one entry each. 2 to 64 items. |
| `spec.engines.nodes[].node` | string | Yes | The Kubernetes node's name. At most 253 characters. |
| `spec.engines.nodes[].fabricInterface` | string | Yes | The network interface that carries the engine's KV transfers. At most 15 characters. |
| `spec.engines.nodes[].rdma` | object |  | The RDMA port settings of a ucx_rdma engine. |
| `spec.engines.nodes[].rdma.gidIndex` | integer |  | The GID index of the HCA port. From 0 to 255. |
| `spec.engines.nodes[].rdma.roceMode` | string |  | v2 or v1 for RoCE, or infiniband for a native InfiniBand port. One of `v2`, `v1`, `infiniband`. |
| `spec.engines.nodes[].ports` | object |  | This engine's ports in place of engines.ports. Engine pods run on the node's host network, so engines that share a node need ports no other engine there uses, which the controller checks. API server rule: the engine, attestation and side channel ports must lie outside the UCX TCP port range. |
| `spec.engines.nodes[].ports.engine` | integer |  | The engine's HTTP port. From 1 to 65535. |
| `spec.engines.nodes[].ports.attestation` | integer |  | The attestation sidecar's HTTP port. From 1 to 65535. |
| `spec.engines.nodes[].ports.sideChannel` | integer |  | The port the engine's KV connector listens on for its peers' handshakes. From 1 to 65535. |
| `spec.engines.nodes[].ports.ucxTcpPortRange` | string |  | LOW-HIGH, the ports UCX TCP listeners take. At most 11 characters. |
| `spec.engines.apiKeySecret` | object |  | The Secret that holds the engines' API key. Name a new Secret to rotate the key. |
| `spec.engines.apiKeySecret.name` | string | Yes | The Secret's name. At most 253 characters. |
| `spec.engines.apiKeySecret.key` | string | Yes | The key in the Secret. At most 253 characters. |
| `spec.engines.launch` | list of objects |  | More launch inputs of the engine backend, as the variables discovery reads, such as NARWHAL_ENGINE_BACKEND or NARWHAL_TRANSFER_NET_DEVICES. A name another field of the fleet sets, or that names a credential, is refused. At most 64 items. Each item's `name` is unique. |
| `spec.engines.launch[].name` | string | Yes | The variable's name. At most 128 characters. API server rule: another field of the fleet sets this variable, or it names a credential, which belongs in engines.apiKeySecret or controlTokenSecret. |
| `spec.engines.launch[].value` | string | Yes | The variable's value. At most 4096 characters. |
| `spec.slo` | object | Yes | The fleet's service-level objective, in seconds. |
| `spec.slo.ttftS` | string | Yes | The time-to-first-token target. At most 16 characters. API server rule: must be greater than 0. |
| `spec.slo.tpotS` | string | Yes | The time-per-output-token target. At most 16 characters. API server rule: must be greater than 0. |
| `spec.slo.routerOverheadS` | string | Yes | The router's own share of each request. At most 16 characters. |
| `spec.firstTokenMarginS` | string | Yes | Seconds the first-token deadline adds to the calibrated candidate. At most 16 characters. API server rule: must be greater than 0. |
| `spec.profiling` | object | Yes | The profiling sweep. |
| `spec.profiling.prefillLens` | list of integers | Yes | The prompt lengths of the prefill sweep. 1 to 64 items. |
| `spec.profiling.decodeInputLens` | list of integers | Yes | The input lengths of the decode sweep. 1 to 64 items. |
| `spec.profiling.decodeConcurrency` | list of integers |  | The concurrency levels of the decode sweep. 1 to 64 items. |
| `spec.profiling.warmupRequests` | integer | Yes | Requests sent before measuring. From 0 to 100000. |
| `spec.calibration` | object | Yes | The first-token calibration. |
| `spec.calibration.inputTokens` | list of integers | Yes | The input lengths calibration measures. 1 to 64 items. |
| `spec.calibration.samples` | integer | Yes | Samples per group, at least the 100 that preflight accepts. From 100 to 100000. |
| `spec.calibration.observationTimeoutS` | string | Yes | Seconds calibration waits for one observation. At most 16 characters. API server rule: must be greater than 0. |
| `spec.fabric` | object | Yes | Fabric qualification. |
| `spec.fabric.workload` | object | Yes | The workload the fabric budget sizes each edge for. |
| `spec.fabric.workload.promptTokens` | integer | Yes | Prompt tokens per KV handoff. From 1 to 10000000. |
| `spec.fabric.workload.handoffsPerS` | string | Yes | Peak remote KV handoffs per second on each directed edge, for each source cache group. At most 16 characters. API server rule: must be greater than 0. |
| `spec.fabric.workload.burst` | integer | Yes | Handoffs in a burst. From 1 to 1000000. |
| `spec.fabric.workload.transferBudgetS` | string | Yes | Seconds in which one burst must transfer. At most 16 characters. API server rule: must be greater than 0. |
| `spec.fabric.workload.headroom` | string | Yes | Multiplier on the required rate. At most 16 characters. API server rule: must be at least 1. |
| `spec.fabric.testPort` | integer | Yes | The TCP port the fabric test listens on. From 1 to 65535. |
| `spec.routers` | object | Yes | The router pair. |
| `spec.routers.storageClassName` | string | Yes | The storage class of each router's volume. At most 253 characters. |
| `spec.routers.size` | string | Yes | The size of each router's volume. At most 32 characters. |
| `spec.routers.args` | list of strings |  | More narwhal-serve options, such as --lease-ttl 6. At most 32 items. |
| `spec.controlTokenSecret` | object | Yes | The Secret that holds the routers' control token, which the routes that change membership, trust or lifecycle require: at least 32 random bytes, as 64 hex digits or 43 base64url characters. Name a new Secret to rotate the token. |
| `spec.controlTokenSecret.name` | string | Yes | The Secret's name. At most 253 characters. |
| `spec.controlTokenSecret.key` | string | Yes | The key in the Secret. At most 253 characters. |
| `spec.haproxy` | object |  | The HAProxy load balancer in front of the routers. |
| `spec.haproxy.replicas` | integer |  | HAProxy pods. From 1 to 16. Default `1`. |
| `spec.haproxy.clusterDomain` | string |  | The cluster's DNS domain. At most 253 characters. Default `cluster.local`. |
| `spec.timeouts` | object |  | Seconds a stage may run. |
| `spec.timeouts.defaultSeconds` | integer |  | Every stage's limit. From 1 to 604800. |
| `spec.timeouts.stages` | object |  | One stage's limit, by stage name. |
| `spec.timeouts.stages.<stage>` | integer |  | The stage's limit, for each stage by name. From 1 to 604800. |
| `spec.until` | string |  | The last stage to run; every stage by default. One of `access`, `discover`, `engine-prepare`, `install`, `engine-start`, `engine-check`, `cache-capture`, `transfer-measure`, `cache-groups`, `fabric-budget`, `fabric-edge`, `attest-capture`, `sidecar-start`, `sidecar-verify`, `finalize-fleet`, `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start`. |
| `spec.stopped` | boolean |  | Stop the routers, HAProxy, engines and Jobs, keeping the volumes. Default `false`. |
| `spec.retainRuns` | integer |  | Run directories kept, the current one included. From 1 to 16. Default `3`. |

### Status

The controller writes the status; nothing else may.

| Field | Type | Description |
| --- | --- | --- |
| `status.observedGeneration` | integer | The generation the latest pass started from. At least 0. |
| `status.conditions` | list of objects | Ready, Reconciling, Stalled, Stopped, and one per stage. At most 24 items. Each item's `type` is unique. |
| `status.conditions[].type` | string | The condition's type. At most 316 characters. |
| `status.conditions[].status` | string | True, False or Unknown. One of `True`, `False`, `Unknown`. |
| `status.conditions[].observedGeneration` | integer | The generation the condition was set from. At least 0. |
| `status.conditions[].lastTransitionTime` | string | When the status last changed. At most 64 characters. |
| `status.conditions[].reason` | string | A CamelCase reason. 1 to 1024 characters. |
| `status.conditions[].message` | string | What the reason means for this fleet. At most 32768 characters. |
| `status.serving` | boolean | The routers serve through HAProxy. |
| `status.stage` | string | The stage the latest pass runs or stopped at. At most 63 characters. |
| `status.engines` | string | Engines checked, of those the fleet names. At most 16 characters. |
| `status.stages` | object | Each stage's instances. |
| `status.stages.<stage>` | object | One stage's instances, for each stage by name. |
| `status.stages.<stage>.passed` | integer | Instances that passed. From 0 to 1000000. |
| `status.stages.<stage>.total` | integer | Instances the stage runs. From 0 to 1000000. |
| `status.stages.<stage>.lastPassedTime` | string | When an instance last passed. At most 64 characters. |
| `status.previousRuns` | list of strings | The fleet UIDs of the run directories an earlier fleet of this name left. At most 16 items. |
| `status.exports` | list of objects | The export Jobs the controller made for the latest narwhal.example.com/export-request values. At most 4 items. Each item's `nonce` is unique. |
| `status.exports[].nonce` | string | The request's nonce. At most 32 characters. |
| `status.exports[].job` | string | The export Job's name. At most 63 characters. |
| `status.exports[].uid` | string | The export Job's UID. At most 36 characters. |
| `status.exports[].pod` | string | The export Job's pod the controller read. At most 253 characters. |
| `status.exports[].sha256` | string | The SHA-256 of the pod's log. At most 64 characters. |
| `status.exports[].size` | integer | The size of the pod's log in bytes. From 0 to 2147483647. |
| `status.exports[].error` | string | Why the export failed. At most 1024 characters. |

`kubectl get narwhalfleet` prints these columns:

| Column | From |
| --- | --- |
| Serving | `.status.serving` |
| Ready | `.status.conditions[?(@.type=="Ready")].status` |
| Stage | `.status.stage` |
| Engines | `.status.engines` |
| Age | `.metadata.creationTimestamp` |

<!-- End of the generated field tables. -->

The cluster admin can narrow the spec further in the narwhal chart's values: `modelVolumeSources` with `modelHostPathPrefixes`, `modelNfsServers` and `modelCsiDrivers` for `engines.modelVolume`, and `runtimeClassNames` for `engines.resources.runtimeClassName`. The controller sets `Stalled` with reason `InputRefused` on a fleet outside them, and the namespace's admission policy refuses such a pod. [Conditions and reasons](Kubernetes.md#reading-the-status) lists what each condition reports.
