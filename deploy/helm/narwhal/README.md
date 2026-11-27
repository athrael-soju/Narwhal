# Narwhal Helm chart

The chart runs the fleet controller for one namespace, and what the controller does not own:

- the controller Deployment, its ServiceAccount, Role and RoleBinding, and a ClusterRole that reads nodes and the `NarwhalFleet` CRD
- the namespace's admission policy, which bounds what a fleet can run
- the engines' ServiceAccount and a fleet-editor Role
- the state PersistentVolumeClaim, which `helm uninstall` keeps, and the router pair's lease claim
- the router pair, the StatefulSet `router`, each started as a standby of the other
- a NetworkPolicy in front of the routers
- Prometheus and Grafana, when `monitoring.enabled` is set
- a `NarwhalFleet`, when `fleet` is set
- or [direct engines](#direct-engines) that run on CPUs without attestation

Install the [CRD chart](../narwhal-crds/README.md) first. The controller builds the engines, the gate Jobs and HAProxy from the namespace's `NarwhalFleet` and owns them through owner references, so the chart renders none of them. The cluster needs Kubernetes 1.30 or later, the GPU and RDMA device plugins the fleet names, and a ReadWriteMany storage class for the state and lease claims. The lease volume must support `flock`, coherent reads and atomic rename across nodes. Install each release in a namespace of its own.

## Controller

The controller runs as one pod with `strategy: Recreate`, as the non-root user `controller.runAsUser` (10001 by default) with a read-only root filesystem, no capabilities and the `RuntimeDefault` seccomp profile. The pod mounts no ServiceAccount token; the controller container alone mounts a projected token that lasts an hour, with the cluster's CA, at the usual ServiceAccount path. It mounts the state claim at `/narwhal-state` and reads the chart's settings from `/etc/narwhal/controller.json`: the state claim's name, the engines' ServiceAccount `<release>-engine`, the model volume and RuntimeClass allow-lists below, and the admission policy's name.

Its Role in the namespace grants:

| Resources | Verbs |
| --- | --- |
| `narwhalfleets`; `narwhalfleets/status` | get, list, watch; patch |
| `jobs` | get, list, watch, create, delete |
| `pods`; `pods/log` | get, list, watch, patch, delete; get |
| `configmaps`, `services`, `statefulsets`, `deployments` | get, list, watch, create, patch, delete |
| `leases` | create; get and update on `controller.narwhal.example.com` only |
| `events.k8s.io` events | create, patch |

The ClusterRole `<namespace>-<release>-narwhal-nodes` grants get and list on nodes, and get by name on the CRD `narwhalfleets.narwhal.example.com` and on the namespace's admission policy and its binding. The controller has no verb on Secrets, RBAC objects or `pods/exec`: it copies the fleet's `apiKeySecret` reference into the pods that need the key and never reads the Secret.

The engines' ServiceAccount `<release>-engine` may get the namespace's pods and nothing else. The attestation sidecars and the sidecar-verify Jobs run as it.

## Who may write the fleet

Writing the `NarwhalFleet` lets the controller start hostNetwork engine and fabric pods on the nodes the fleet names and give them the Secret that `apiKeySecret` names, so it amounts to read access to every Secret in the namespace. Grant it with the Role `<release>-fleet-editor`, which the chart creates without binding it. Kubernetes' `edit` and `admin` roles do not include it.

The admission policy `<namespace>-narwhal` and its binding apply to every pod, Job, StatefulSet, Deployment and ReplicaSet the namespace creates or changes, and to ephemeral containers. They hold the rules of the Pod Security baseline level:

- Only the controller's engine StatefulSet and its fabric and `transfer-measure` gate Jobs, and their pods, use the host network or add `IPC_LOCK`. No pod adds another capability.
- No pod is privileged, shares the host's process or IPC namespace, binds a host port, unmasks `/proc`, runs a Windows host process, or sets an SELinux user, role or a type outside the container types, an unconfined AppArmor profile, a sysctl outside the baseline's safe list, or a RuntimeClass outside `runtimeClassNames`.
- Every pod but an engine pod runs the `RuntimeDefault` or a `Localhost` seccomp profile. An engine pod may run unconfined, as the ROCm launch does.
- Every image is named by registry digest.
- The volume named `model` follows the allow-lists below, as the controller's own check does, and every container mounts it read-only. Every other volume is a ConfigMap, Secret, emptyDir, projected, downward API or PersistentVolumeClaim volume, so no other pod mounts a host path.

The policy trusts the engine, router, gate and HAProxy labels only on an object whose sender it trusts: a workload the controller's ServiceAccount sends, a ReplicaSet the Deployment controller makes, and a pod the Job, StatefulSet or ReplicaSet controller makes for its owner. Anyone else who sends a pod or workload with those labels is refused, and nobody can add them to an existing pod. A pod's spec cannot change after it is created, so an update to a pod is checked only for its images and ephemeral containers.

Two kinds of access still reach past the policy, so keep `edit` and `admin` in the fleet's namespace to the people who install the chart:

- The `edit` role may impersonate the namespace's ServiceAccounts and request their tokens, so its holders can act as the controller and send what the controller may send.
- The routers' NetworkPolicy also admits pods labelled as Prometheus. The chart runs Prometheus, so the policy cannot tie that label to one sender, and anyone who can create pods in the namespace can give a pod it. With `monitoring.enabled` off, the NetworkPolicy leaves that label out; scrape with a cluster Prometheus through `networkPolicy.monitoring` instead to close it.

The namespace runs with the Pod Security `privileged` level: the `baseline` level refuses the engine pods' host network and `IPC_LOCK`, and Pod Security cannot exempt single pods. If the cluster enforces `baseline` by default, label the namespace `pod-security.kubernetes.io/enforce=privileged`. The admission policy then enforces the baseline's rules on every other pod.

Helm creates the policy after the controller, and `helm uninstall` keeps the policy and its binding, so they outlive the controller and the workloads a fleet leaves behind. The controller may get both by name, to wait for them before it creates anything. Delete them by hand once the namespace no longer runs a fleet.

`modelVolumeSources` allows only `persistentVolumeClaim` by default. A hostPath also needs a directory in `modelHostPathPrefixes`, an NFS export a server in `modelNfsServers`, and a CSI volume a driver in `modelCsiDrivers`. Each list is empty by default, which keeps that source off.

## The fleet

`fleet` holds a `NarwhalFleet`'s `spec`, which `values.schema.json` checks against the CRD's schema; the API server checks the CRD's CEL rules when Helm applies it. The chart names the fleet `fleet` and marks it `argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true`, so one Argo CD Application can hold both charts. Leave `fleet` out to apply the fleet with `narwhal deploy --target kubernetes` or another tool.

## Direct engines

`directEngines` runs engines that start straight from their image, as `narwhal dev` starts them on a host, in place of a fleet's attested engines. They run on CPUs and take no launch plan, no attestation and no gate Jobs, so the fleet that routes over them has no `engine_contract`. The router admits them as it admits the engines of `narwhal dev`. Use them to test the chart or a cluster without GPUs. A production fleet needs the attested engines of a `NarwhalFleet`.

`narwhal.deployment.direct_engine_set.direct_values` derives the values from a backend's direct start, which only the llama.cpp backend has. Each pod runs the command that start renders, listening on port 8080 at its pod address, with the model volume read-only at `/model` and the claim `narwhal-engine-shared` at `/narwhal-shared`. Every engine shares that ReadWriteMany claim for the slot files of its KV handoff. The engine's thread count follows its whole-CPU limit. The fleet file's engines, with their URLs `http://engine-<n>.<service>.<namespace>.svc.<cluster domain>:8080`, come from `direct_engine_set.fleet_engines`, and its profiles are nominal, measured as `narwhal dev up` measures them.

## Values

Keep a filled values file in the ignored `config/helm/` directory. `values.schema.json` refuses a release that leaves out any value without a default.

| Value | Meaning |
| --- | --- |
| `images.narwhal` | The Narwhal image by registry digest, `<repository>@sha256:<hex>`, which the controller and the routers run |
| `controller.runAsUser`, `controller.runAsGroup` | The controller's non-root user and group, default 10001. The group owns the run directories on the state volume |
| `controller.resources`, `controller.nodeSelector`, `controller.tolerations` | The controller pod's resources and placement |
| `fleet` | A `NarwhalFleet` spec, under [The fleet](#the-fleet) |
| `directEngines` | Engines that start directly, from `direct_values` |
| `state.claimName`, `state.storageClassName`, `state.size` | The state claim, default `narwhal-state`, and its ReadWriteMany storage class and size |
| `lease.storageClassName`, `lease.size` | The lease claim `narwhal-lease` |
| `routers.enabled` | Run the router pair, default `true` |
| `routers.fleet` | The fleet file's path on the state volume |
| `routers.storageClassName`, `routers.size` | Each router's own claim, for its journal and state handoff |
| `routers.args` | More `narwhal-serve` options, such as `["--lease-ttl", "6"]` |
| `haproxy.clusterDomain` | The cluster's DNS domain, default `cluster.local` |
| `credentials.engineApiKey` | `{"env", "secretName", "key"}`: the Secret key the routers read into the variable the fleet file's `engine.engine_api_key_env` names |
| `modelVolumeSources`, `modelHostPathPrefixes`, `modelNfsServers`, `modelCsiDrivers` | The model volume allow-lists, under [Who may write the fleet](#who-may-write-the-fleet) |
| `runtimeClassNames` | The RuntimeClasses a pod in the namespace may name, such as `["nvidia"]`. Empty by default, which allows only the default runtime |
| `networkPolicy.enabled` | Admit connections to the routers only from HAProxy, the gate Jobs, the other router and Prometheus, default `true` |
| `networkPolicy.monitoring` | More NetworkPolicy peers that may reach the routers, such as a cluster Prometheus's namespace |
| `monitoring` | Prometheus and Grafana, under [Monitoring](#monitoring) |

## Volumes

| Workload | State volume | Other volumes |
| --- | --- | --- |
| Controller | The whole volume, writable, at `/narwhal-state` | Its settings at `/etc/narwhal` |
| Engine pod, from the controller | Its own directory, `engines/<role>/<pod UID>/`, at `/narwhal-state`. The engine container mounts it read-only. The sidecar also mounts the launch directory that checked the pod, `engines/<role>/launches/<pod UID>/`, read-only | The model volume, read-only |
| Gate Job, from the controller | The whole volume, writable, at `/narwhal-state`; a fabric Job mounts only its own output directory | A discovery Job mounts the model volume read-only at `/model` |
| Direct engine pod | None | The model volume, read-only, and `narwhal-engine-shared` |
| Router | The whole volume, read-only, at `/narwhal-state` | The lease at `/narwhal-lease/router.lease`, its own claim at `/narwhal-router` |

The fleet file's `recovery.state_path` belongs under `/narwhal-router`, which holds each router's state alone; a relative path lands there too.

## Routers

The routers are `router-0` and `router-1` in the headless Service `routers`. Each runs from `/narwhal-router` with `--router-id` set to its pod name, listening on port 8000 at its pod address with its journal at `/narwhal-router/journal.jsonl`. [`files/router-start.py`](files/router-start.py) decides how each one starts, as [Starting a router pair](../../../docs/operate/01-Start-Routers.md#starting-a-router-pair) does:

- On the first install, `router-0` finds no handoff at its `recovery.state_path` and no lease file. It starts without `--standby-of` and claims the free lease.
- `router-1`, and `router-0` once either file exists, start with `--standby-of` set to the other router, as a supervisor restarts one. A primary start while the other router held the lease would leave `router-0` a standby that never takes over.

When both routers stop and start again, each is a standby of the other, and the first to find the lease free resumes from the handoff at its state path. That handoff must be at most `--standby-max-handoff-age` seconds old, as [Lease behavior](../../../docs/operate/01-Start-Routers.md#lease-behavior) states, so when both routers stay down longer than that, neither takes over on its own.

The routers elect the primary through the Lease `narwhal-router`, as [Electing the primary on Kubernetes](../../../docs/operate/01-Start-Routers.md#electing-the-primary-on-kubernetes) describes. The chart makes their ServiceAccount, Role and RoleBinding `narwhal-router`, because the controller holds no RBAC verbs. The Role grants `get` and `update` on that Lease and `create` on Leases, since RBAC cannot limit a create to one name. Only the router container mounts the ServiceAccount's token. Each router runs as UID 10001 on a read-only root filesystem and writes only its own claim at `/narwhal-router` and a `/tmp` scratch directory.

The controller runs HAProxy in front of the routers. It forwards only `POST /v1/completions`, `POST /v1/chat/completions`, `GET /v1/models`, `GET /health`, `GET /ready` and `GET /narwhal/targets`, and answers 404 to every other route. The NetworkPolicy `<release>-routers` admits connections to the routers' port 8000 only from HAProxy, the gate Jobs, the other router, the chart's Prometheus and the peers in `networkPolicy.monitoring`, so the control routes are reachable only from those. A NetworkPolicy matches pods by label and cannot check who made a pod; [Who may write the fleet](#who-may-write-the-fleet) states which labels the admission policy reserves. A NetworkPolicy needs a network plugin that enforces it.

## Monitoring

With `monitoring.enabled: true`, the chart runs Prometheus and Grafana from the files `make observe` stages: [`prometheus.yml`](../../../src/narwhal/observability/stack/prometheus.yml), [`prometheus-alerts.yml`](../../../src/narwhal/observability/stack/prometheus-alerts.yml) and the Grafana provisioning under [`src/narwhal/observability/stack/grafana`](../../../src/narwhal/observability/stack/grafana). The chart links them from `files/observability/`, so there is one copy, and a chart test fails when the mounted files differ from what `make observe` stages for the same routers.

Prometheus scrapes `router-0` and `router-1` by their pod DNS names and reads the engine targets from `/narwhal/targets` through the Service `haproxy`, which reaches the active router. Each engine is scraped once, as `make observe` does for a router pair, and the alert rules and dashboard read the pair as they do there. With `fleet` set, Prometheus also scrapes each engine pod's attestation sidecar at `engine-<n>.engines.<namespace>.svc:<attestation port>`, for the kubelet restart counts of the pod's containers and the `NarwhalHostDown` alert. Every series gets the `fleet` label. The Services `prometheus` (port 9090) and `grafana` (port 3000) serve them in the namespace. Grafana asks everyone to sign in unless `monitoring.grafana.anonymous` is set, which lets anyone who reaches it view the dashboards, as under `make observe`. It runs without the image renderer.

The dashboard needs rendering first, because its queries come from the installed engine backends:

```bash
python -m narwhal.observability.stack.chart_dashboard config/helm/dashboard.json
helm install fleet deploy/helm/narwhal -f config/helm/values.yaml \
  --set-file monitoring.grafana.dashboard=config/helm/dashboard.json
```

`--console-url` adds the fleet control link and markers that `make observe` adds.

| Value | Meaning |
| --- | --- |
| `monitoring.enabled` | Run Prometheus and Grafana, default `false` |
| `monitoring.fleet` | The `fleet` label of every series, so one Prometheus and one dashboard can hold several fleets. Default: the release name |
| `monitoring.alertmanagerUrl` | The Alertmanager that Prometheus sends alerts to, `http(s)://HOST:PORT` with an optional path. Without one, alerts show only on the Prometheus alerts page |
| `monitoring.prometheus.image`, `monitoring.grafana.image` | The images by registry digest. Required with monitoring |
| `monitoring.grafana.dashboard` | The rendered dashboard. Required with monitoring |
| `monitoring.grafana.admin` | `{"secretName", "userKey", "passwordKey"}`: the Secret that holds the Grafana admin login. Required with monitoring |
| `monitoring.grafana.anonymous` | Let anyone who reaches Grafana view the dashboards without signing in, default `false` |
| `monitoring.prometheus.storage`, `monitoring.grafana.storage` | `{"storageClassName", "size"}` for a ReadWriteOnce data claim. Without one, the data lasts only as long as the pod |
| `monitoring.prometheus.resources`, `monitoring.grafana.resources` | Container `requests` and `limits` |

Prometheus keeps 30 days of samples, as under `make observe`. A changed configuration or dashboard restarts the pod that reads it.

## Checking the chart

`make chart-check` installs the pinned helm and kubeconform into `local/bin`, renders the chart for a release with a fleet and for one with direct engines, validates the manifests against the Kubernetes API schemas and runs `tests/deployment/test_helm_chart.py`.

`python -m tests.deployment.chart_live --kubeconfig <file> --namespace <ns> --release <name>` checks an installed release against an API server: what the controller's Role and ClusterRole grant and refuse, the objects the controller builds and the pods the admission policy must refuse, and its model volume rules against the controller's.

[Needs verification: `make chart-e2e` still installs the chart with `images.haproxy` and expects the chart's HAProxy in front of direct engines, which the chart no longer renders; task 26 moves it onto both charts.] `make chart-e2e` installs the pinned kind and kubectl as well, and runs the chart on a one-node kind cluster with two llama.cpp direct engines. It builds the Narwhal image from the repository's `Dockerfile` and downloads the default `narwhal dev` template's model into `local/cache/chart-e2e/`. A Job measures the engines' nominal profiles and writes the fleet file, which stands in for the gate Jobs. The test then checks a completion through the Service `haproxy`. Under a steady stream of completions it deletes the router pod that holds the lease, and it fails unless the other router takes the lease, every request succeeds and no request waits longer than the 10 seconds HAProxy holds one. It needs Docker and about 6 GB of free memory, takes a few minutes, and deletes its cluster and image at the end.
