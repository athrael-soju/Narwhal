---
description: Run a Narwhal fleet on Kubernetes from a NarwhalFleet resource and the fleet controller, and read, recover, monitor and secure it.
---

# Deploying on Kubernetes

On Kubernetes, a `NarwhalFleet` resource in a namespace holds the fleet's deployment inputs, and the fleet controller that the narwhal chart installs in that namespace runs the stage graph for it. Each check runs as a gate Job, the same checks `narwhal deploy` runs on SSH hosts, with the same rerun rules and evidence. The controller builds the engine StatefulSet, the router pair and HAProxy, owns each object it makes through an owner reference to the fleet, and writes what it observed to the fleet's status.

The fleet is the only thing you write. Put it in the chart's values, apply it with `kubectl apply` or a GitOps tool, or let `narwhal deploy --target kubernetes` apply it from a workstation. [NarwhalFleet reference](NarwhalFleet.md) lists every field.

## Cluster requirements

The cluster admin provides:

- Kubernetes 1.30 or later. Both charts require it, and the admission policy is a `ValidatingAdmissionPolicy`.
- A GPU device plugin and an RDMA device plugin on every engine node. Each engine pod requests its GPUs and one HCA under the extended resource names in `spec.engines.resources`, and `access` fails on an engine node that offers no GPU or no HCA under those names.
- A ReadWriteMany storage class for the state volume, which the controller, the engine pods, the gate Jobs and the routers on every node mount. The routers also need a storage class for a ReadWriteOnce volume each.
- A volume that serves the staged checkpoint, a PersistentVolumeClaim in the namespace by default.
- A network plugin that enforces NetworkPolicy. The chart's policy is what keeps the routers' control routes away from everything but HAProxy, the gate Jobs, the other router and monitoring.
- Images in a registry the nodes pull from, each pinned by digest: the engine image, a Narwhal image built from the repository's `Dockerfile`, HAProxy, and an image with `ib_write_bw` for the fabric Jobs, plus Prometheus and Grafana when the chart runs them. Narwhal publishes no image.
- A namespace for each fleet, labelled `pod-security.kubernetes.io/enforce=privileged` when the cluster enforces the Pod Security `baseline` level by default. The engine pods need the host network and `IPC_LOCK`, which `baseline` refuses, and the chart's admission policy applies the baseline's other rules instead.

The workstation needs `helm` and `kubectl`.

## Installing the charts

Install the CRD chart once for the cluster, then the narwhal chart in the fleet's namespace:

```bash
helm install narwhal-crds deploy/helm/narwhal-crds
helm install narwhal deploy/helm/narwhal --namespace <namespace> --values config/helm/<fleet>.yaml
```

The CRD chart keeps the CRD on `helm uninstall`, and `helm upgrade` updates it. Upgrade it to the narwhal chart's version first: a controller whose schema differs from the installed CRD's stalls with reason `SchemaStale` and starts nothing. The [narwhal chart's README](https://github.com/athrael-soju/Narwhal/blob/main/deploy/helm/narwhal/README.md) lists its values, and [Getting started](Getting-Started.md#deploying-on-kubernetes) gives a complete values file.

The chart creates the controller, its RBAC, the namespace's admission policy, the engines' ServiceAccount, the routers' ServiceAccount and Lease Role, the routers' NetworkPolicy, the state claim and the monitoring, and the fleet when `fleet` is set. It renders no engine, Job, router or HAProxy object.

## Applying the fleet

Create the Secrets the fleet names before the fleet. The routers' control token is required, and the engine API key is optional:

```bash
kubectl create secret generic router-token-v1 --namespace <namespace> \
  --from-literal=token="$(python -c 'import secrets; print(secrets.token_hex(32))')"
kubectl create secret generic engine-key-v1 --namespace <namespace> \
  --from-literal=key="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

Then give the controller the fleet in one of these ways. Each holds the same `spec`.

- **In the chart's values.** Set `fleet` to the spec. The chart names the fleet `fleet` and checks the spec against the CRD's schema before it renders. `helm upgrade` with changed values is an edit.
- **Through GitOps.** One Argo CD Application can hold both charts: the chart's fleet carries `argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true`, so the sync does not fail before the CRD exists. In Flux, make the narwhal HelmRelease `dependsOn` the CRD release. The controller changes nothing the chart rendered and writes only the fleet's status, so the sync finds no drift. [Needs verification: task 22, an Argo CD or Flux sync with no drift on the live cluster]
- **With `kubectl apply`.** Leave `fleet` out of the values and apply a `NarwhalFleet` named `fleet` yourself, such as the one in the [reference](NarwhalFleet.md):

  ```bash
  kubectl apply --server-side --namespace <namespace> -f config/helm/fleet.yaml
  ```

- **From a workstation.** `narwhal deploy run --target kubernetes --run DIR` builds the spec from the deployment variables, applies it server-side as the field manager `narwhal-cli`, follows its status and copies the evidence into `DIR`. It follows a fleet whose spec another field manager owns, such as Helm, Argo CD or Flux, and never writes that spec: it writes only the `narwhal.example.com/export-request` annotation that asks for the evidence. [`narwhal deploy`](../cli/Deploy.md) describes the subcommands.

A namespace holds one fleet, and the API server refuses a fleet with another name. The controller refuses it too, with reason `NameRefused`.

Keep each Secret immutable and give its name a version, such as `engine-key-v2`. To rotate the engine key or the control token, create the next Secret and point `spec.engines.apiKeySecret` or `spec.controlTokenSecret` at it. A key-check Job holds both credentials and reports only their HMACs under a salt kept in the run directory, so a credential edited in place also counts as a change. A new engine key restarts the engines and the routers, and a new control token restarts only the routers. The controller runs the key check after each edit or new `requested-at` value, and otherwise every `controller.keyCheckPeriodSeconds`, 900 by default.

Only the containers that call engines and the key-check Job read the engine key, and only the routers, the router-check Jobs of `router-start` and the key-check Job read the control token, each through a `secretKeyRef`. The controller copies the references and never reads either Secret. A `controlTokenSecret` with the name and key of `engines.apiKeySecret` is refused, by the CRD and as `InputRefused`. The controller cannot see two Secrets that hold the same value, so the routers compare the token with the engine key when they start, and refuse to start when they match.

## Reading the status

`kubectl get narwhalfleet` shows whether the fleet serves, whether it is ready, the stage its latest pass runs or stopped at, and how many engines passed `engine-check`:

```bash
kubectl get narwhalfleet --namespace <namespace>
```

```text
NAME    SERVING   READY   STAGE          ENGINES   AGE
fleet   true      True    router-start   2/2       5h12m
```

`kubectl get narwhalfleet --watch` follows each change, and `kubectl describe narwhalfleet fleet` prints every condition with its reason and message. `kubectl wait --for=condition=Ready narwhalfleet/fleet` waits for the fleet to become ready.

The controller writes the status only when a condition or count changes. `status.observedGeneration` is the generation the latest pass started from, so a fleet whose `metadata.generation` is ahead of it has an edit the controller has not acted on yet. `Ready`, `Reconciling` and `Stalled` follow the kstatus conventions, so exactly one of them is `True` unless the fleet is stopped:

| Condition | `True` when | Reasons |
| --- | --- | --- |
| `Ready` | A pass for the current generation has passed every stage up to `spec.until` and its live checks hold | `Passed` when true. Otherwise the reason of the condition that is true: `Running`, `Retrying`, `Stopping`, `Stalled` or `Stopped` |
| `Reconciling` | A pass runs, or one will | `Running` while a stage runs, `Retrying` after an error the controller retries, such as an unreachable API server or an interrupted attempt, `Stopping` while `spec.stopped` takes effect. `Ready`, `Stalled` or `Stopped` when false |
| `Stalled` | No pass runs until something changes | `StageFailed`, `InputRefused`, `SchemaStale`, `AdmissionPolicyMissing`, `RouterStorageImmutable`, `NameRefused`, `HelpersSurvived`, as below. `Ready`, `Reconciling` or `Stopped` when false |
| `Stopped` | `spec.stopped` is true and the fleet's workloads are gone | `Stopped`, `Stopping` while they go, `Running` |

| `Stalled` reason | Cause | What clears it |
| --- | --- | --- |
| `StageFailed` | A stage failed or exceeded its time limit. The message names the stage instance and its failure | An edit of the spec, or a new `requested-at` value |
| `InputRefused` | An input is missing or malformed, or outside the cluster admin's allow-lists for model volumes and RuntimeClasses. The message names each problem | An edit that fixes it |
| `SchemaStale` | The installed CRD holds another schema than the one the controller was built with | Upgrading `narwhal-crds` to the narwhal chart's version |
| `AdmissionPolicyMissing` | The namespace's admission policy or its binding is missing, or the binding does not deny | Installing or upgrading the narwhal chart |
| `RouterStorageImmutable` | `spec.routers` asks for a storage class or size that the router StatefulSet's volume claim template does not hold. The message names the StatefulSet and claims | Putting `spec.routers` back, or deleting the StatefulSet `router` and the claims `router-state-router-0` and `router-state-router-1`, which loses the routers' handoff state |
| `NameRefused` | The fleet is not named `fleet` | Applying it as `fleet` |
| `HelpersSurvived` | Processes an earlier runner started for a stage survived their cleanup | The next pass that finds them gone |

Each stage the run record holds also has a condition, typed `stage.narwhal.example.com/<stage>`. Its reason is the run-record status of its worst instance, in CamelCase and in this order from worst: `Failed`, `Timeout`, `Interrupted`, `Cancelled`, `Running`, `Invalidated`, `Passed`. Its status is `True` for `Passed`, `Unknown` for `Running` and `False` for the rest. Its message gives the reasons the stage last ran, such as an input that changed, and for a failed instance its failure first. `status.stages.<stage>` counts the instances that passed, of all the stage runs, with the time one last passed. Per-instance detail stays in the run record.

## Evidence and logs

The controller keeps each fleet's run directory on the state volume, at `/narwhal-state/runs/<namespace>/<fleet uid>/`: the [run record](../telemetry/06-Deployment-Runs.md) `run.json`, and each stage attempt's evidence in `stages/<stage>/<instance>/attempt-<n>/`, with the log of each of its Jobs in `logs/<job>.log`. Each gate Job also keeps its own output, exit status and returned files on the volume, in `/narwhal-state/deploy/<run-id>/stages/<stage>/<instance>/attempt-<n>/jobs/<job>/`. A gate mounts only that directory, its attempt's work directory and the directories it reads, each by its own subPath; no gate, engine or router pod mounts the volume's root or a run directory. A fleet deleted and made again under its name gets a new uid and a new directory, and `status.previousRuns` names the earlier ones the controller kept. Each pass keeps the newest `spec.retainRuns` directories of the namespace, 3 by default, and deletes the rest.

`narwhal deploy status --target kubernetes --run DIR` copies the run record and the evidence of every finished attempt into the workstation's run directory through export Jobs, without exec. The workstation sets the fleet's `narwhal.example.com/export-request` annotation to a nonce and the attempts it already holds; the controller creates the Job from its own image, user and group, names it under that nonce in `status.exports`, and mounts only `run.json` and the attempt directories asked for, read-only. The run directory's root, with the salt that keys the record's digests and the key check's salt and HMACs, is never mounted. Once the Job's pod succeeds, the controller reads its log and adds the pod's name and the log's SHA-256 and size to the answer. The workstation reads that pod's log, imports nothing unless the digest matches, and checks every file against the log's manifest. An admin can also read the volume through the controller pod:

```bash
uid=$(kubectl get narwhalfleet fleet --namespace <namespace> -o jsonpath='{.metadata.uid}')
kubectl exec --namespace <namespace> deploy/<release>-controller -- \
  cat /narwhal-state/runs/<namespace>/$uid/run.json
```

The logs stay where Kubernetes keeps them:

| Process | Log |
| --- | --- |
| Controller | `kubectl logs deploy/<release>-controller`: each pass, stage instance, adopted Job and stall |
| Gate Job | `kubectl logs job/<job>`, while the Job exists. The run record names each attempt's Jobs under `jobs`, and `kubectl get jobs -l narwhal.example.com/gate=<kind>` lists them by kind |
| Engine pod | `kubectl logs engine-<n> -c engine`; the init containers `checkpoint` and `runtime-check` check the checkpoint and the runtime first |
| Attestation sidecar | `kubectl logs engine-<n> -c attestation` |
| Router | `kubectl logs router-0 -c router`; the request journal is `/narwhal-router/journal.jsonl` on the router's own volume |
| HAProxy | `kubectl logs deploy/haproxy` |

The controller deletes an attempt's Jobs once the attempt passes. A failed attempt's Jobs stay, so `kubectl logs job/<job>` reads them, until the next attempt of the stage starts or the fleet stops. The attempt directory keeps every Job's log either way. Add `--previous` to read the log of a container the kubelet restarted.

## Recovery

A failed or timed-out stage leaves the fleet `Stalled` with reason `StageFailed` until the next edit of its spec or a new value of its `narwhal.example.com/requested-at` annotation. A live change, such as a replaced engine pod, does not clear the stall. What fails a stage is usually a node's resources, an image or a Secret, which the run record does not track, so a pass started by each live change would fail the same way again and again. Fix the cause, then ask for the stage again:

```bash
kubectl annotate narwhalfleet fleet --namespace <namespace> --overwrite \
  narwhal.example.com/requested-at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

The next pass resumes at the failed stage, in a new attempt. A fleet that Helm or a GitOps tool owns can carry the annotation in its manifest; any new value counts.

An edit takes effect at once when it changes an input whose rerun rule names the stage that runs: the controller cancels that stage, deletes its Jobs, records the attempt `cancelled`, and starts a new pass from the edited spec. Any other edit waits for the running stage instance to end. [Rerunning after a change](#rerunning-after-a-change) lists what each input reruns.

A pass that reruns `discover` takes every GPU of each engine's node for its discovery Jobs, which run pinned to the node. While the engine pods hold those GPUs, the kubelet refuses the Jobs and the stage fails. A new Narwhal release reruns `discover`, and so does each change the [rerun table](#rerunning-after-a-change) lists against it, so stop the fleet first, make the change, then start it again.

`spec.stopped: true` stops the fleet. The controller cancels a running stage, deletes the routers, HAProxy, the engines and then the Jobs, in that order, waits for their pods to go, and sets `Stopped`. The state volume, the router volumes and the run directory stay. Setting `spec.stopped` back to `false` starts a pass from the run record: it applies the engines and the routers again and reruns the stages whose live checks no longer hold.

The fleet has no finalizer. Deleting it, directly or with `helm uninstall` of a release that holds it, leaves the removal of everything it owns to the garbage collector, which deletes in no order, so nothing waits on the controller and no fleet is stuck terminating. These objects have no owner and stay:

- the state claim, which the chart keeps, with every run directory
- the router claims `router-state-router-0` and `router-state-router-1`, with the routers' handoff state
- the routers' Lease `narwhal-router` and the controller's Lease `controller.narwhal.example.com`
- the admission policy `<namespace>-narwhal` and its binding, which `helm uninstall` keeps
- the CRD, which the CRD chart keeps

A fleet made again under the name starts a new run directory, finds the routers' handoff state on their claims, and finds the old router Lease, which expires because nobody renews it. Delete the router claims and the Lease by hand to start the pair cold, and delete the admission policy and its binding once the namespace runs no fleet.

If the fleet is gone for good after `helm uninstall`, delete the state claim, or the whole namespace. Its run directories hold the salts that key the run records' digests and the key check's HMACs, and the admission policy is all that keeps other pods from mounting it.

A restarted controller adopts what its predecessor left running. The Deployment uses `strategy: Recreate`, and only the holder of the controller Lease acts. Every Job's name follows from the fleet's uid, the stage instance, the attempt and the Job's place in it, and the run record names each Job before the controller creates it. The next holder records the running attempt `interrupted`, finds its Jobs by those names, and waits for them instead of starting them again, so an interrupted attempt never stalls the fleet. A controller that loses the Lease, or receives SIGTERM, exits at once and touches no Job. Every Job delete propagates in the background, so a deleted Job takes its pods with it. When the controller's node is lost, the replacement pod starts only once the old pod is force-deleted or the node is tainted `node.kubernetes.io/out-of-service`.

## Engine restarts

The kubelet restarts a failed engine container in its pod. The pod's attestation sidecar stops once the engine process it serves ends, and when the kubelet restarts it, it captures the new engine container's attestation before it serves, as [Engine restart and process replacement](../operate/03-Restart-Engines.md) describes. The router finds the new process and readmits the engine itself.

The controller reads each engine pod's UID and its engine and sidecar containers' IDs, image IDs and starts on every pass, and reconciles every 60 seconds. A restarted engine container reruns `engine-check`, `cache-capture` or `transfer-measure`, `attest-capture`, `sidecar-start` and `sidecar-verify` for that engine, then `finalize-fleet`, `preflight` and `router-start`, which readmits the engine to a router pair that still runs instead of restarting it. A restarted sidecar reruns `sidecar-start`, `sidecar-verify` and `finalize-fleet`.

The engine StatefulSet uses `updateStrategy: OnDelete`, so a changed pod template replaces no running pod. A deleted engine pod comes back held at the `narwhal.example.com/placement` scheduling gate, and the next pass's `engine-start` pins it to the node discovery measured, so an engine never serves from a node discovery did not measure. A stalled fleet runs no pass, so a pod replaced while the fleet is stalled waits at the gate until the stall clears.

## Monitoring

The chart's monitoring runs in one of two modes. [Monitoring](https://github.com/athrael-soju/Narwhal/blob/main/deploy/helm/narwhal/README.md#monitoring) in the chart's README lists every value.

- **The chart's own Prometheus and Grafana.** With `monitoring.enabled: true`, the chart runs both from the files `make observe` uses. Prometheus scrapes both routers, the engines that `/narwhal/targets` lists through HAProxy, and each engine pod's attestation sidecar, with the alert rules of `make observe`.
- **The cluster's Prometheus Operator and Grafana**, such as kube-prometheus-stack. With `monitoring.serviceMonitors.enabled` and `monitoring.dashboardConfigMap.enabled`, the chart runs neither. It renders `ServiceMonitor`, `PodMonitor` and `ScrapeConfig` objects that scrape the same targets with the same labels and interval, the alert rules as a `PrometheusRule`, and the dashboard as a ConfigMap for the Grafana dashboard sidecar. Put the labels the cluster's Prometheus selects by in `monitoring.serviceMonitors.labels`, and admit its namespace to the routers in `networkPolicy.monitoring`. [Needs verification: task 22, monitoring through `ServiceMonitor` objects in an existing Prometheus]

Both modes need the dashboard rendered for the installed engine backends:

```bash
python -m narwhal.observability.stack.chart_dashboard config/helm/dashboard.json
helm upgrade narwhal deploy/helm/narwhal --namespace <namespace> --values config/helm/<fleet>.yaml \
  --set-file monitoring.grafana.dashboard=config/helm/dashboard.json
```

Reach the chart's Grafana and Prometheus, and the fleet through HAProxy, with `kubectl port-forward`, each in its own terminal:

```bash
kubectl port-forward --namespace <namespace> svc/grafana 13000:3000
kubectl port-forward --namespace <namespace> svc/prometheus 19090:9090
kubectl port-forward --namespace <namespace> svc/haproxy 18000:8000
```

Then open `http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator` and sign in with the login in the Secret `monitoring.grafana.admin` names, unless `monitoring.grafana.anonymous` is set. Prometheus is at `http://127.0.0.1:19090`, and clients reach the serving routes at `http://127.0.0.1:18000`. HAProxy forwards only `POST /v1/completions`, `POST /v1/chat/completions`, `GET /v1/models`, `GET /health`, `GET /ready` and `GET /narwhal/targets`. Every series carries the `fleet` label, the release name unless `monitoring.fleet` sets another.

The dashboard's **Supervisor restarts** panel shows the kubelet restart counts of each engine pod's containers, which the sidecars export. The controller writes no deployment status for the routers to export, so the [Deployment row](../observability/05-Dashboard.md#deployment)'s stage, age, fabric and attestation panels stay empty for a Kubernetes fleet: read the stages from the fleet's status.

Cluster health stays with the cluster's own monitoring. Narwhal's Prometheus scrapes only the routers, the engines and the sidecars, so it holds no node GPU health, no PersistentVolumeClaim usage and no Job outcomes. Watch those through the cluster's GPU exporter, the kubelet's volume statistics and kube-state-metrics, and alert on them there. Narwhal ships neither kube-state-metrics nor an Alertmanager. Set `monitoring.alertmanagerUrl` to deliver the fleet's alerts to yours.

## Security and access

Each identity holds only what it uses.

**The controller** runs as the ServiceAccount `<release>-controller`. Its Role in the namespace grants:

| Resources | Verbs |
| --- | --- |
| `narwhalfleets`; `narwhalfleets/status` | get, list, watch; patch |
| `jobs` | get, list, watch, create, delete |
| `pods`; `pods/log` | get, list, watch, patch, delete; get |
| `configmaps`, `services`, `statefulsets`, `deployments` | get, list, watch, create, patch, delete |
| `leases` | create; get and update on `controller.narwhal.example.com` only |
| `events.k8s.io` events | create, patch |

Its ClusterRole `<namespace>-<release>-narwhal-nodes` grants get and list on nodes, for discovery, and get by name on the CRD `narwhalfleets.narwhal.example.com` and on the namespace's admission policy and its binding. It has no verb on Secrets, RBAC objects or `pods/exec`. The pod mounts no ServiceAccount token; the controller container alone mounts a projected token that lasts an hour. It runs as a non-root user, 10001 by default, on a read-only root filesystem, with no capabilities and the `RuntimeDefault` seccomp profile.

**Whoever writes the fleet** needs the Role `<release>-fleet-editor`, which the chart creates without binding it: get, list, watch, create, update, patch and delete on `narwhalfleets`, and get on `narwhalfleets/status`. Kubernetes' `edit` and `admin` roles do not include it. Bind it to each person or tool that writes the fleet:

```bash
kubectl create rolebinding fleet-editors --namespace <namespace> \
  --role <release>-fleet-editor --group <group>
```

**The workstation** that runs `narwhal deploy --target kubernetes` needs the fleet-editor Role, of which it uses get, create, patch and delete, and a Role with get on `pods` and `pods/log`. It reads the engine, sidecar and router pods for the live checks of `plan` and `status`, and the log of each export Job's pod the controller names. The controller creates those Jobs when the workstation sets the fleet's `narwhal.example.com/export-request` annotation, so the workstation holds no verb on Jobs, Secrets, RBAC, `pods/exec` or the fleet's status, and cannot start a pod of its own.

**The routers** run as the ServiceAccount `narwhal-router`. Its Role grants get and update on the Lease `narwhal-router`, and create on Leases, because RBAC cannot limit a create to one name. A compromised router could create a Lease under a name nothing uses yet, but cannot read, renew, take over or delete one. Only the router container mounts the token, a projected token that lasts an hour. The routers run as UID 10001 on a read-only root filesystem with no capabilities.

**The engines' ServiceAccount** `<release>-engine` may get the namespace's pods and nothing else. The attestation sidecars and the `sidecar-verify` Jobs run as it, to bind each engine process to its pod. No other gate Job mounts a token.

Write access to a `NarwhalFleet` is close to write access to the namespace's nodes. Whoever writes the fleet chooses the engine image, and that image receives the engine API key. The controller starts it in host-network engine pods with `IPC_LOCK` on the nodes the fleet names, starts the fleet's fabric image in host-network Jobs, and hands the engines and routers the Secrets that `apiKeySecret` and `controlTokenSecret` name. That amounts to read access to every Secret in the namespace. The fleet does not choose the code that writes the state volume: the routers, the gate Jobs, the export Jobs and the attestation sidecars run the controller's own image, the chart's `images.narwhal`, and the engine and fabric images mount only their own directories of the volume.

The namespace's admission policy bounds the rest: only the controller's engine StatefulSet and its fabric and `transfer-measure` Jobs may use the host network or add `IPC_LOCK`, no pod is privileged, every image is named by digest, and the model volume follows the admin's allow-lists. Only the controller's Deployment and the ReplicaSets and pods made for it run as the controller's ServiceAccount, and the policy takes a Deployment of that name as the controller's only from someone who may update the policy, as the chart's installer may. Only the controller's workloads and its Deployment mount the state claim, and of those only the controller's own pods mount its root. No one adds an ephemeral container to the controller's pod or to a pod the controller made, and only the Job controller makes a pod that names a Job as its controller or carries its `job-name` labels. The [chart's README](https://github.com/athrael-soju/Narwhal/blob/main/deploy/helm/narwhal/README.md#who-may-write-the-fleet) lists every rule.

What the chart cannot close:

- Kubernetes' `edit` and `admin` roles may impersonate the namespace's ServiceAccounts, request their tokens and exec into pods. Anyone with `edit`, `admin`, `impersonate` or `pods/exec` in the fleet's namespace therefore has the controller's power, including reading the state claim and starting host-network engine pods with `IPC_LOCK`. Keep them to the people who install the chart, and give the people who run fleets only the fleet-editor Role.
- Anyone who can create pods or Jobs in the namespace can mount its Secrets, the engine key's among them, as volumes: the admission policy allows Secret volumes. The workstation's role creates neither.
- Attestations carry no signature. The router trusts the attestation an engine's attestation URL serves once it matches the engine's contract, so a process that serves a copy of it from that address, such as another host-network workload on the node once the sidecar stops, receives the engine key.
- Anyone who can create pods in the namespace can give a pod the label the routers' NetworkPolicy admits for the chart's Prometheus. With the cluster's Prometheus, admitted through `networkPolicy.monitoring`, the NetworkPolicy leaves that label out.

Run each fleet in a namespace of its own, which runs nothing else.

## Rerunning after a change

<!-- Generated from KUBERNETES_GRAPH in src/narwhal/deployment/kubernetes_graph.py by tools/maintenance/stage_graph_doc.py. Edit the graph, then run the tool. -->

| Stage | Runs | After | Gate section |
| --- | --- | --- | --- |
| `access` | Once per run, for the whole fleet |  | [Checking cluster access](../deploy/Kubernetes.md#checking-cluster-access) |
| `discover` | Once per run, for the whole fleet | `access` | [Discovering the engine nodes](../deploy/Kubernetes.md#discovering-the-engine-nodes) |
| `engine-prepare` | Once for each engine pod | `discover` | [Preparing the engine pods](../deploy/Kubernetes.md#preparing-the-engine-pods) |
| `install` | Once per run, for the whole fleet | `engine-prepare` | [Applying the engines](../deploy/Kubernetes.md#applying-the-engines) |
| `engine-start` | Once for each engine pod | `install` | [Starting the engine pods](../deploy/Kubernetes.md#starting-the-engine-pods) |
| `engine-check` | Once for each engine pod | `engine-start` | [Checking the engine pods](../deploy/Kubernetes.md#checking-the-engine-pods) |
| `cache-capture` | Once for each engine pod, on TensorRT-LLM or vLLM | `engine-check` | [Capturing the live cache layout](../deploy/03-Validate-Engines.md#capturing-the-live-cache-layout) |
| `transfer-measure` | Once for each engine pod, on SGLang | `engine-check` | [Measuring KV handoffs](../deploy/04-Qualify-Fabric.md#measuring-kv-handoffs) |
| `cache-groups` | Once per run, for the whole fleet | `engine-check`, `cache-capture`, `transfer-measure` | [Deriving cache-equivalence groups](../deploy/03-Validate-Engines.md#deriving-cache-equivalence-groups) |
| `fabric-budget` | Once for each cache-equivalence group | `cache-groups` | [Building the source budget](../deploy/04-Qualify-Fabric.md#building-the-source-budget) |
| `fabric-edge` | Once for each directed edge between the engines' nodes | `fabric-budget` | [Binding each sample to a directed route](../deploy/04-Qualify-Fabric.md#binding-each-sample-to-a-directed-route) |
| `attest-capture` | Once for each engine pod | `fabric-edge` | [Reading the sidecar's attestation](../deploy/Kubernetes.md#reading-the-sidecars-attestation) |
| `sidecar-start` | Once for each engine pod | `attest-capture` | [Checking the sidecar container](../deploy/Kubernetes.md#checking-the-sidecar-container) |
| `sidecar-verify` | Once for each engine pod | `sidecar-start` | [Verifying the sidecar](../deploy/05-Attest.md#verifying-the-sidecar) |
| `finalize-fleet` | Once per run, for the whole fleet | `sidecar-verify` | [Finalizing the fleet contract](../deploy/05-Attest.md#finalizing-the-fleet-contract) |
| `profile` | Once per run, for the whole fleet | `finalize-fleet` | [Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) |
| `calibrate` | Once per run, for the whole fleet | `profile` | [Calibrating the first-token deadline](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) |
| `fleet-limits` | Once per run, for the whole fleet | `calibrate` | [Reusing or repeating the calibration](../deploy/06-Profile-and-Preflight.md#reusing-or-repeating-the-calibration) |
| `preflight` | Once per run, for the whole fleet | `fleet-limits` | [Running preflight](../deploy/06-Profile-and-Preflight.md#running-preflight) |
| `router-start` | Once per run, for the whole fleet | `preflight` | [Starting the router pair](../deploy/Kubernetes.md#starting-the-router-pair) |

| Change | Recorded as | Reruns |
| --- | --- | --- |
| Namespace or fleet, such as a fleet deleted and made again | The fleet's namespace and uid | `access` and every stage after it |
| Engine node, model volume, image, checkpoint, launch policy or transport | The fleet's `images`, and its `engines` fields other than `apiKeySecret` and each node's `rdma` | `discover` and every stage after it |
| Narwhal release | The SHA-256 of the installed package's Python files, which build every object | `discover` and every stage after it |
| Engine API key Secret, or the key in it | `engines.apiKeySecret`, and an HMAC of the key under the fleet's salt from a key-check Job, never the key itself | `engine-prepare`, `router-start` |
| Router control token Secret, or the token in it | `controlTokenSecret`, and an HMAC of the token under the fleet's salt from a key-check Job, never the token itself | `router-start` |
| SLO targets or router overhead | `slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s` | `fleet-limits`, `preflight`, `router-start` |
| First-token deadline margin | The margin above the calibrated first-token candidate | `fleet-limits`, `preflight`, `router-start` |
| Profiling sweep or warmup | The prefill and decode sweep lengths, the decode concurrency and the warmup requests | `profile`, `preflight`, `router-start` |
| First-token calibration inputs | The input tokens, samples and observation timeout | `calibrate`, `fleet-limits`, `preflight`, `router-start` |
| Fabric budget workload | The prompt tokens, handoffs per second, burst, transfer budget and headroom | `transfer-measure` on SGLang when the prompt tokens change, `fabric-budget`, `fabric-edge` |
| Fabric test settings | The test port, and each engine's RDMA GID index and RoCE mode | `fabric-edge` |
| Router settings | The fleet's `routers` and `haproxy` | `router-start` |
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

A Job runs once, within its kind's time limit, and `spec.timeouts` limits how long a whole stage may run. It receives its inputs in a ConfigMap mounted at `/narwhal-input`, which the controller deletes once the Job finishes. Its outputs come back in its log: each file as a `narwhal-file` line, then a manifest of their SHA-256 values and sizes. A log whose files differ from the manifest fails the stage, so a log the kubelet rotated or truncated is never read as complete.

The Narwhal Jobs mount the whole state volume writable. A fabric Job runs the third-party `images.fabric`, so it mounts only its own keep directory, drops every capability but `IPC_LOCK`, which an RDMA sample needs, and may not gain more. The engine API key reaches only the key check and the Jobs that call engines: `transfer-measure`, `sidecar-verify`, `finalize-fleet`, `profile`, `calibrate` and `preflight`.

A Job or pod that cannot run fails its stage at once: a container whose configuration the kubelet cannot build, such as one that names a missing Secret, a Job pod the kubelet refuses at admission on its pinned node, such as `OutOf<resource>`, and an engine or router pod that no node can schedule. A router pod may wait up to five minutes for a node, as for a cluster autoscaler.

### Checking cluster access

`access` checks that the namespace holds the fleet the pass runs for, by uid, and that each engine's node exists and offers at least one GPU and one HCA under the fleet's resource names. It fails naming each missing node or resource, and `access.json` holds the namespace, the fleet and each node's allocatable resources. Fix the node or `spec.engines.nodes`.

### Discovering the engine nodes

`discover` runs two Jobs on each engine's node. The discovery Job runs the engine image with every GPU the node offers and the model volume. It reads the GPU inventory, the image's packages and environment, and the model configuration, and hashes the checkpoint. A probe Job on the host network reads the fabric interface's addresses. The controller then builds the fleet, the launch records and the role files as `package` does for SSH hosts.

A Job that ran another image than `spec.engines.image`, or a checkpoint whose tree digest differs from `spec.engines.model.treeSha256`, fails the stage. `inspection/` holds each engine's records and each Job's log.

### Preparing the engine pods

`engine-prepare` runs [`narwhal-engine prepare --backend kubernetes`](../cli/Engine.md#kubernetes-plans) for each engine, in a Job on its node with the model volume, and returns the launch directory to `launch/`. A failure there is a failed prepare: its message names the input to fix. Every engine of a fleet must transfer KV over `ucx_rdma`, so the fleet's `engines.launch` sets `NARWHAL_TRANSFER_TRANSPORT=ucx_rdma` and `NARWHAL_TRANSFER_NET_DEVICES=<hca>:<port>`; the pod uses the HCA the device plugin allocates and keeps only the port.

### Applying the engines

`install` applies the engine StatefulSet `engine`, its headless Service `engines` and the hook ConfigMaps of each prepared launch, then deletes the engine objects the fleet no longer holds. A hook ConfigMap that a running engine pod mounts stays until `engine-start` replaces that pod, which keeps the spec it started with. `objects.json` holds what was applied, with each ConfigMap's content as its digest.

### Starting the engine pods

Each new engine pod waits at the `narwhal.example.com/placement` scheduling gate. `engine-start` adds node affinity for the node discovery measured the engine on, removes the gate, and waits for the engine container to be ready. The removal tests that the gate is still at the index it read, so a gate another controller added stays. A pin that an earlier attempt half applied, with the affinity already naming this node, only has its gate removed; affinity for another node fails the stage, and deleting the pod lets the StatefulSet make a new one. It replaces a pod that runs on another node, and a pod of an older template that runs another plan. It fails when the engine container cannot start; `pod.json` holds the pod's status.

### Checking the engine pods

`engine-check` copies the launch directory into `engines/<role>/launches/<pod UID>` on the state volume and runs `narwhal-engine check` on the pod's records there. The check works in a staging directory and renames it into place once it passes, so the pod's attestation sidecar, which waits for that directory, reads a checked plan. A rejected check is a failed gate: the Job's log names the record to inspect.

### Reading the sidecar's attestation

The sidecar captures the attestation once the launch directory exists. `cache-capture` reads the cache layout from the attestation it published, and `attest-capture` reads the attestation itself. `attest-capture` then checks that the engine's live cache resolved the layout its cache group's representative resolved, as an SSH capture does, and fails when it did not. A sidecar that publishes nothing within fifteen minutes fails the stage; read the sidecar container's log.

### Checking the sidecar container

`sidecar-start` waits for the attestation sidecar container to be ready and records its identity. `sidecar-verify` then runs `verify --pod-evidence` in a Job as the engines' ServiceAccount, which reads the engine pod through the API as the sidecar does.

### Starting the router pair

`router-start` applies the router StatefulSet `router`, its headless Service `routers` and HAProxy, with the fleet file `fleet-limits` wrote on the state volume, waits for both router pods, and checks the health, readiness, state and one completion through HAProxy in a router-check Job. Each router keeps its recovery state and journal on its own volume, at `/narwhal-router`. After an engine pod is replaced, a router pair that still runs the same fleet and objects readmits it instead of restarting.

The routers elect the primary through the Lease `narwhal-router` in the namespace, as [Electing the primary on Kubernetes](../operate/01-Start-Routers.md#electing-the-primary-on-kubernetes) describes, so no volume holds a lease.

On a fresh fleet, `router-0` finds no handoff and starts with `--first-start-primary`: it creates the Lease and serves one lease duration later. Every other router start is a standby of the other router. When both routers come back together with handoffs older than 30 seconds, as after `spec.stopped`, a recreated fleet or both pods restarting, one of them claims the Lease as a [cold start](../cli/Serve.md#standby-takeover-and-lease-fencing), within about 36 seconds of their start with the default timings.

The routers write only their own volume and a `/tmp` scratch directory. `fsGroup` makes the router volume writable on most storage, but not on `local-path` or other hostPath volumes. A router volume that an earlier development image wrote, when the `narwhal` user had another UID, is then not writable, and the router stops at start with an error that names the volume. Stop the fleet and delete the claims `router-state-router-0` and `router-state-router-1`; the pair then starts cold.

A topology spread constraint on `kubernetes.io/hostname`, with `maxSkew: 1` and `whenUnsatisfiable: DoNotSchedule`, keeps the two router pods on different nodes whenever more than one node can take them, so losing one node leaves a router. On a single node both still run. Nodes with a taint the routers do not tolerate, such as a control-plane node, do not count.
