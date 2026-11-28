---
description: How narwhal deploy --target kubernetes runs the stage graph on a cluster, the inputs it reads, and how to recover each stage.
---

# Deploying on Kubernetes

`narwhal deploy --target kubernetes` runs the deployment stage graph on a Kubernetes cluster. The workstation drives `helm` and `kubectl`; the Narwhal Helm chart runs the engines, the router pair and one gate Job for each check. `plan`, `run`, `status` and `down` behave as on SSH hosts, and `run` resumes at the stage that failed:

```bash
narwhal deploy plan --target kubernetes --run runs/deploy/cluster
narwhal deploy run --target kubernetes --run runs/deploy/cluster
narwhal deploy status --target kubernetes --run runs/deploy/cluster
narwhal deploy down --target kubernetes --run runs/deploy/cluster
```

A run record belongs to one target. Resuming a run directory with the other target is refused.

## Inputs

The run reads the [Kubernetes launch inputs](../configuration/04-Deployment-Inputs.md#kubernetes-launch-inputs), the engine and fabric inputs, the [deployment run inputs](../configuration/04-Deployment-Inputs.md#deployment-run-inputs) and the cluster inputs that [Kubernetes target inputs](../configuration/04-Deployment-Inputs.md#kubernetes-target-inputs) lists: the namespace, the chart values file, each engine's node, and optionally the release, the kubeconfig context, the chart directory and the `kubectl` and `helm` executables.

The model sits at `/model` in every pod, so `NARWHAL_MODEL_DIR` is always `/model`. `NARWHAL_ROUTER_URL` defaults to the first router pod, `http://router-0.routers:8000`, because HAProxy forwards only the serving routes.

## How the stages run

Each stage that checks something in the cluster sets the chart's `gates` list to its Jobs and upgrades the release. Every upgrade reads the values the release runs and keeps its engines, their placement and the routers: only `install` changes the engines, `router-start` the routers, and `down` removes both. With monitoring on, each upgrade renders the Grafana dashboard of the installed Narwhal, unless the values file sets `monitoring.grafana.dashboard`. A Job runs once, within its kind's time limit. It receives its inputs in a ConfigMap mounted at `/narwhal-input`, which the run deletes once the Job finishes. Its outputs come back in its log: each file as a `narwhal-file` line, then a manifest of their SHA-256 values and sizes. A log whose files differ from the manifest fails the stage, so a log the kubelet rotated or truncated is never read as complete. Each Job also keeps its log, exit status and returned files on the state volume, in `deploy/<run-id>/stages/<stage>/<instance>/attempt-<n>/jobs/<job>/`, mounted at `/narwhal-keep`, and the run copies its log and files into the stage's attempt directory.

The Narwhal Jobs mount the whole state volume writable. A fabric Job runs the third-party `images.fabric`, so it mounts only its own keep directory, drops every capability but `IPC_LOCK`, which an RDMA sample needs, and may not gain more. The engine API key reaches only the Jobs that call engines or prepare their launch: `prepare`, `transfer-measure`, `finalize-fleet`, `profile`, `calibrate` and `preflight`.

The fabric stages run the same checks as on SSH hosts, each host script in a Job on the node's host network in `images.fabric`. A sample runs its server and client as two Jobs, the client once the server listens. On a backend that measures KV handoffs, `transfer-measure` runs the packaged measurement tool in a Job on the engine's node.

`plan` and a `status` that finds the run held by another runner start no Job. On SSH hosts `plan` observes each fabric route read-only. On a cluster, observing a route takes a Job on each node, so `plan` does not observe it: it prints `fabric-edge[<edge>]: skipped unless run finds a change; link[<edge>] not observed`, and its JSON row lists the fact under `unobserved`. A `status` held off by another runner reports the link `not observed`. `run` and `status` observe each route, and `run` measures an edge whose route changed again.

In the cluster, a stage that fails or times out leaves the `NarwhalFleet` `Stalled` until the next edit of its spec or a new value of its `narwhal.example.com/requested-at` annotation. A live change, such as a replaced engine pod, does not clear the stall. What fails a stage is usually a node's resources, an image or a Secret, which the run record does not track, so a run started by each live change would fail the same way again and again. Fix the cause, then set `requested-at` to run the stage again.

## Rerunning after a change

<!-- Generated from KUBERNETES_GRAPH in src/narwhal/deployment/kubernetes_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `access` | Once per run, from the management workstation |  | [Checking cluster access](../deploy/Kubernetes.md#checking-cluster-access) |
| `discover` | Once per run, from the management workstation | `access` | [Discovering the engine nodes](../deploy/Kubernetes.md#discovering-the-engine-nodes) |
| `engine-prepare` | Once for each engine pod | `discover` | [Preparing the engine pods](../deploy/Kubernetes.md#preparing-the-engine-pods) |
| `install` | Once per run, from the management workstation | `engine-prepare` | [Installing the chart](../deploy/Kubernetes.md#installing-the-chart) |
| `engine-start` | Once for each engine pod | `install` | [Starting the engine pods](../deploy/Kubernetes.md#starting-the-engine-pods) |
| `engine-check` | Once for each engine pod | `engine-start` | [Checking the engine pods](../deploy/Kubernetes.md#checking-the-engine-pods) |
| `cache-capture` | Once for each engine pod, on TensorRT-LLM or vLLM | `engine-check` | [Capturing the live cache layout](../deploy/03-Validate-Engines.md#capturing-the-live-cache-layout) |
| `transfer-measure` | Once for each engine pod, on SGLang | `engine-check` | [Measuring KV handoffs](../deploy/04-Qualify-Fabric.md#measuring-kv-handoffs) |
| `cache-groups` | Once per run, from the management workstation | `engine-check`, `cache-capture`, `transfer-measure` | [Deriving cache-equivalence groups](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric-budget` | Once for each cache-equivalence group | `cache-groups` | [Building the source budget](../deploy/04-Qualify-Fabric.md#building-the-source-budget) |
| `fabric-edge` | Once for each directed edge between the engines' nodes | `fabric-budget` | [Binding each sample to a directed route](../deploy/04-Qualify-Fabric.md#binding-each-sample-to-a-directed-route) |
| `attest-capture` | Once for each engine pod | `fabric-edge` | [Reading the sidecar's attestation](../deploy/Kubernetes.md#reading-the-sidecars-attestation) |
| `sidecar-start` | Once for each engine pod | `attest-capture` | [Checking the sidecar container](../deploy/Kubernetes.md#checking-the-sidecar-container) |
| `sidecar-verify` | Once for each engine pod | `sidecar-start` | [Verifying the sidecar](../deploy/05-Attest.md#verifying-the-sidecar) |
| `finalize-fleet` | Once per run, from the management workstation | `sidecar-verify` | [Finalizing the fleet contract](../deploy/05-Attest.md#finalizing-the-fleet-contract) |
| `profile` | Once per run, from the management workstation | `finalize-fleet` | [Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `calibrate` | Once per run, from the management workstation | `profile` | [Calibrating the first-token deadline](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) |
| `fleet-limits` | Once per run, from the management workstation | `calibrate` | [Reusing or repeating the calibration](../deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration) |
| `preflight` | Once per run, from the management workstation | `fleet-limits` | [Running preflight](../deploy/06-Profile-and-Preflight.md#running-preflight) |
| `router-start` | Once per run, from the management workstation | `preflight` | [Starting the router pair](../deploy/Kubernetes.md#starting-the-router-pair) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| Namespace, fleet, Kubernetes context or kubectl path | `NARWHAL_KUBERNETES_NAMESPACE`, the fleet's uid in `NARWHAL_KUBERNETES_FLEET_UID`, `NARWHAL_KUBERNETES_CONTEXT` and `NARWHAL_KUBECTL` | `access` and every stage after it |
| Engine node, model volume, image, checkpoint, launch policy or transport | The discovery fields of `.env`, the Kubernetes inputs of the engine pods and the images | `discover` and every stage after it |
| Narwhal release | The SHA-256 of the installed package's Python files, which build every object | `discover` and every stage after it |
| Engine API key Secret, or the key in it | The Secret name and key in `NARWHAL_KUBERNETES_API_KEY_SECRET`, and an HMAC of the key under the fleet's salt from a key-check Job, never the key itself | `engine-prepare`, `router-start` |
| Router control token Secret, or the token in it | The Secret name and key in `NARWHAL_KUBERNETES_CONTROL_TOKEN_SECRET`, and an HMAC of the token under the fleet's salt from a key-check Job, never the token itself | `router-start` |
| SLO targets or router overhead | `slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s` | `fleet-limits`, `preflight`, `router-start` |
| First-token deadline margin | The margin above the calibrated first-token candidate | `fleet-limits`, `preflight`, `router-start` |
| Profiling sweep or warmup | The prefill and decode sweep lengths, the decode concurrency and the warmup requests | `profile`, `preflight`, `router-start` |
| First-token calibration inputs | The input tokens, samples and observation timeout | `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Fabric budget workload | The prompt tokens, handoffs per second, burst, transfer budget and headroom | `transfer-measure` on SGLang when the prompt tokens change, `fabric-budget`, `fabric-edge` |
| Fabric test settings | The test port, and each engine's RDMA GID index and RoCE mode | `fabric-edge` |
| Router settings | The routers' storage, arguments and HAProxy settings, and the router URL | `router-start` |
| Prepared engine pod | The SHA-256 of the launch directory's `launch.json` and `kubernetes.json` | `install`; `engine-start`, `engine-check` for that engine |
| Engine pod replaced or its engine container restarted | The pod UID and node, and the engine container's ID, image ID and start, checked live on every run | `engine-check`, `cache-capture` on TensorRT-LLM or vLLM, `transfer-measure` on SGLang, `attest-capture`, `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet`, `preflight`, `router-start` to readmit the engine to a router pair that still runs |
| Engine cache geometry | The SHA-256 of `cache-layout.json` | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a node of the engine's group |
| Measured KV handoff size of an engine | The SHA-256 of the engine's `transfer.json` handoff measurement | `cache-groups`; `fabric-budget` for the engine's cache group; `fabric-edge` for every edge leaving a node of the engine's group |
| Fabric route of a directed node edge | The link fingerprint: roles, addresses, interfaces, both live routes, transport, tool version and test parameters, checked live on every run | `fabric-edge` for that edge; `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Attested binding digest of an engine | The `launch_digest`, or the `attestation_digest` when the attestation has no launch evidence | `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Engine contract fields of an engine | The `EngineContract` fields of its attestation | `attest-capture` for every engine; `finalize-fleet`, `profile`, `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Attestation sidecar container restarted | The sidecar container's ID, image ID and start, checked live on every run | `sidecar-start`, `sidecar-verify` for that engine; `finalize-fleet` |
| Router pod replaced or restarted | Each router pod's UID and its router container's ID and start, checked live on every run | `router-start` |

<!-- End of the generated stage tables. -->

Keep the engine API key in an immutable Secret whose name carries a version, such as `engine-key-v2`, and rotate it by creating the next Secret and pointing `apiKeySecret` at it. Each `run` also starts a key-check Job that holds the key and reports only its HMAC under a salt kept in the run directory, so a key edited in place still restarts the engines and the routers.

The fleet's `spec.controlTokenSecret` names the Secret and key of the routers' [control token](../HTTP-API.md#control-token), and the fleet requires it. Create it the same way, with a version in its name:

```bash
kubectl create secret generic router-token-v1 --namespace <namespace> \
  --from-literal=token="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

```yaml
spec:
  controlTokenSecret:
    name: router-token-v1
    key: token
```

Only the router containers and the gate Jobs that hold the token read it, through a `secretKeyRef`: the router-check Jobs of `router-start`, which readmit engines, and the key-check Job, which reports its HMAC beside the engine key's. The controller copies the reference and never reads the Secret. A new Secret, or a token edited in place, restarts only the routers. An environment that holds `NARWHAL_ROUTER_TOKEN` is refused.

## Checking cluster access

`access` reads the client and server versions, `helm version`, the namespace and the `kubectl auth can-i` answers for each verb and resource the run needs: get, list, create, update and delete on Secrets, where helm keeps each release revision and supersedes the last; get, list, create, patch and delete on the release's other kinds; get, list, patch and delete on pods, get on `pods/log` and get on nodes; and get, create and update on Leases, which the routers' Role grants and RBAC lets only an identity holding them grant. The server must run Kubernetes 1.27 or later, where pod scheduling gates are on by default. `access.json` records the context kubectl resolves, which is the current context when `NARWHAL_KUBERNETES_CONTEXT` is unset, its API server, the namespace and the release, which `down` checks. It reads each engine node's allocatable GPU and RDMA resources. It fails when the server is older, the identity may not do something the run needs, naming each verb and resource, or a node offers no GPU or RDMA device. `access.json` holds what it found. Fix the RBAC binding or the node, then run again.

## Discovering the engine nodes

`discover` runs two Jobs on each engine's node. The discovery Job runs the engine image with every GPU the node offers and the model volume. It reads the GPU inventory, the image's packages and environment, and the model configuration, and hashes the checkpoint. A probe Job on the host network reads the fabric interface's addresses. The run then builds the fleet, the launch records and the role files as `package` does for SSH hosts.

The discovery Job needs the node's GPUs, so it stays pending while engine pods hold them. Run `narwhal deploy down` first when discovery must run again. A Job that ran another image than `NARWHAL_ENGINE_IMAGE`, or a checkpoint that differs from `NARWHAL_MODEL_TREE_SHA256`, fails the stage. `inspection/` holds each engine's records and each Job's log.

## Preparing the engine pods

`engine-prepare` runs [`narwhal-engine prepare --backend kubernetes`](../cli/Engine.md#kubernetes-plans) for each engine, in a Job on its node with the model volume, and returns the launch directory to `launch/`. A failure there is a failed prepare: its message names the input to fix.

## Installing the chart

`install` upgrades the release with each engine's chart values, from the prepared launch directories, and with `placement`, the node each engine role runs on. The routers stay off. `values.json` holds the values, with each ConfigMap's content as its digest.

An engine pod that started before keeps its spec, so the release keeps the hook ConfigMaps a running pod mounts until `engine-start` replaces that pod.

## Starting the engine pods

The chart holds each new engine pod at the `narwhal.io/placement` scheduling gate. `engine-start` adds node affinity for the node discovery measured the engine on, removes the gate, and waits for the engine container to be ready. The removal tests that the gate is still at the index it read, so a gate another controller added stays. A pin that an earlier attempt half applied, with the affinity already naming this node, only has its gate removed; affinity for another node fails the stage, and deleting the pod lets the StatefulSet make a new one. It replaces a pod that runs on another node, and a pod of an older spec that runs another plan. It fails when the engine container cannot start; `pod.json` holds the pod's status.

A pod the controller replaces between runs waits at the gate until the next `run`, so an engine never serves from a node discovery did not measure.

## Checking the engine pods

`engine-check` copies the launch directory into `engines/<role>/launches/<pod UID>` on the state volume and runs `narwhal-engine check` on the pod's records there. The check works in a staging directory and renames it into place once it passes, so the pod's attestation sidecar, which waits for that directory, reads a checked plan. A rejected check is a failed gate: the Job's log names the record to inspect.

## Reading the sidecar's attestation

The sidecar captures the attestation once the launch directory exists. `cache-capture` reads the cache layout from the attestation it published, and `attest-capture` reads the attestation itself. `attest-capture` then checks that the engine's live cache resolved the layout its cache group's representative resolved, as an SSH capture does, and fails when it did not. A sidecar that publishes nothing within fifteen minutes fails the stage; read the sidecar container's log.

## Checking the sidecar container

`sidecar-start` waits for the attestation sidecar container to be ready and records its identity. `sidecar-verify` then runs `verify --pod-evidence` in a Job as the engines' service account, which reads the engine pod through the API as the sidecar does.

## Starting the router pair

`router-start` upgrades the release with the routers on and the fleet file `fleet-limits` wrote on the state volume, waits for both router pods, and records the health, readiness, state and one completion through HAProxy. Each router keeps its recovery state on its own volume, at `/narwhal-router/router-state.json`. After an engine pod is replaced, a router pair that still runs the same fleet readmits it instead of restarting.

The routers elect the primary through the Lease `narwhal-router` in the namespace, as [Electing the primary on Kubernetes](../operate/01-Start-Routers.md#electing-the-primary-on-kubernetes) describes, so no volume holds a lease. They run as the ServiceAccount `narwhal-router`. Its Role grants `get` and `update` on that one Lease, and `create` on Leases in the namespace, because RBAC cannot limit a create to one name. It grants nothing else, so a router can create another Lease but cannot read, renew, take over or delete one. A compromised router could still create a Lease under a name nothing has created yet and hold up that name's election, so run each fleet in a namespace of its own. Only the router container mounts the account's token, a projected token that expires after an hour.

On a fresh install, `router-0` finds no handoff and starts with `--first-start-primary`: it creates the Lease and serves one lease duration later. Every other router start is a standby of the other router. When both routers come back together with handoffs older than 30 seconds, as after `spec.stopped`, a recreated fleet or both pods restarting, one of them claims the Lease as a [cold start](../cli/Serve.md#standby-takeover-and-lease-fencing), within about 36 seconds of their start with the default timings.

The routers run as UID and GID 10001, the image's `narwhal` user, on a read-only root filesystem with no capabilities. They write only their own volume and a `/tmp` scratch directory. `fsGroup` makes the router volume writable on most storage, but not on `local-path` or other hostPath volumes. A router volume that an earlier development image wrote, when the `narwhal` user had another UID, is then not writable, and the router stops at start with an error that names the volume. Stop the routers and delete their claims, `router-state-router-0` and `router-state-router-1`; the pair then starts cold.

A topology spread constraint on `kubernetes.io/hostname`, with `maxSkew: 1` and `whenUnsatisfiable: DoNotSchedule`, keeps the two router pods on different nodes whenever more than one node can take them, so losing one node leaves a router. On a single node both still run. Nodes with a taint the routers do not tolerate, such as a control-plane node, do not count.

`down` upgrades the release without engines, routers or Jobs. The state volume stays, with the engines' records, and so does the Lease, which the routers free as they stop. It first checks that the run directory holds a Kubernetes run, that the environment reaches the context, API server, namespace and release the run's last passed `access` recorded, and that the release runs only the engines the run's last passed `install` gave it and the fleet of this run. Later attempts of `access` or `install` that failed or were invalidated change neither record. When a check fails, `down` says which and changes nothing. A run whose `access` never passed changed no release, and `down` says so.
