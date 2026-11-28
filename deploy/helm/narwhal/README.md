# Narwhal Helm chart

The chart runs the fleet controller for one namespace, and what the controller does not own:

- the controller Deployment, its ServiceAccount, Role and RoleBinding, and a ClusterRole that reads nodes and the `NarwhalFleet` CRD
- the namespace's admission policy, which bounds what a fleet can run
- the engines' ServiceAccount and a fleet-editor Role
- the state PersistentVolumeClaim, which `helm uninstall` keeps
- a NetworkPolicy in front of the controller's routers
- Prometheus and Grafana, when `monitoring.enabled` is set, or the monitors, alert rules and dashboard for [the cluster's own](#the-clusters-prometheus-and-grafana)
- a `NarwhalFleet`, when `fleet` is set
- or [direct engines](#direct-engines) that run on CPUs without attestation

[Deploying on Kubernetes](https://athrael-soju.github.io/Narwhal/deploy/Kubernetes/) takes a fleet through the controller from install to recovery, and [Getting started](https://athrael-soju.github.io/Narwhal/deploy/Getting-Started/#deploying-on-kubernetes) gives a complete values file. Install the [CRD chart](../narwhal-crds/README.md) first. The controller builds the engines, the gate Jobs, the router pair and HAProxy from the namespace's `NarwhalFleet` and owns them through owner references, so the chart renders none of them. The cluster needs Kubernetes 1.30 or later, the GPU and RDMA device plugins the fleet names, and a ReadWriteMany storage class for the state claim. Install each release in a namespace of its own, which runs nothing else: see [Who may write the fleet](#who-may-write-the-fleet).

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

The ClusterRole `<namespace>-<release>-narwhal-nodes` grants get and list on nodes, and get by name on the CRD `narwhalfleets.narwhal.example.com` and on the namespace's admission policy and its binding. The controller has no verb on Secrets, RBAC objects or `pods/exec`: it copies the fleet's `apiKeySecret` and `controlTokenSecret` references into the pods that need the key or the routers' control token, and never reads either Secret.

The engines' ServiceAccount `<release>-engine` may get the namespace's pods and nothing else. The attestation sidecars and the sidecar-verify Jobs run as it.

## Who may write the fleet

Writing the `NarwhalFleet` chooses the engine image, and that image receives the engine API key. It also lets the controller start hostNetwork engine and fabric pods on the nodes the fleet names and give the engines and routers the Secrets that `apiKeySecret` and `controlTokenSecret` name, so it amounts to read access to every Secret in the namespace. It does not choose the code that writes the state volume: the routers, gate Jobs, export Jobs and attestation sidecars run `images.narwhal`, the controller's own image, and the engine and fabric images mount only their own directories of the volume. Grant it with the Role `<release>-fleet-editor`, which the chart creates without binding it. Kubernetes' `edit` and `admin` roles do not include it.

The admission policy `<namespace>-narwhal` and its binding apply to every pod, Job, StatefulSet, Deployment and ReplicaSet the namespace creates or changes, and to ephemeral containers. They hold the rules of the Pod Security baseline level:

- Only the controller's engine StatefulSet and its fabric and `transfer-measure` gate Jobs, and their pods, use the host network or add `IPC_LOCK`. In those engine pods, the init container named `checkpoint` may add `CHOWN` and `FOWNER`, which it needs to hand the pod's evidence directory to the attestation sidecar's user, `sidecarUser`. No other container adds a capability.
- No pod is privileged, shares the host's process or IPC namespace, binds a host port, unmasks `/proc`, runs a Windows host process, or sets an SELinux user, role or a type outside the container types, an unconfined AppArmor profile, a sysctl outside the baseline's safe list, or a RuntimeClass outside `runtimeClassNames`.
- Every pod but an engine pod runs the `RuntimeDefault` or a `Localhost` seccomp profile. An engine pod may run unconfined, as the ROCm launch does.
- Every image is named by registry digest.
- Only the controller's Deployment `<release>-controller`, the ReplicaSets the Deployment controller makes for it and their pods run as the controller's ServiceAccount, so no other pod gets its token. The policy takes a Deployment of that name as the controller's only from someone who may update the policy, as the installer may, so a user with `edit` can neither change the controller's pod template nor make a Deployment of its name after `helm uninstall`.
- Only the router StatefulSet the controller applies, and the pods the StatefulSet controller makes for it, run as the routers' ServiceAccount `narwhal-router`, so no other pod gets the token that renews the router Lease.
- Only the controller's workloads, its Deployment, and the pods and ReplicaSets the Kubernetes controllers make for them mount the state claim, which holds the run directories, the salts that key their records and the key check's HMACs. A user who may create Jobs or pods in the namespace can mount neither the state claim nor the controller's token.
- Of those, only the controller's own pods mount the state claim's root. Every other container mounts it by subPath: a directory under `deploy/` or `engines/`, or, read-only, a run record or a stage's attempts under `runs/`. No gate, engine, router or export pod sees a run directory's salt or the key check's HMACs.
- No one adds an ephemeral container to the controller's pod or to a pod the controller made: one with the engine, router, gate or HAProxy label, one that runs as the controller's ServiceAccount, or one that mounts the state claim.
- Only the Job controller makes a pod that names a Job as its controller or carries the `job-name` or `batch.kubernetes.io/job-name` label, and no update gives a pod either. The workstation imports an export only from the pod the controller read, by the log's SHA-256, so a pod someone else makes cannot pass for an export Job's pod.
- The volume named `model` follows the allow-lists below, as the controller's own check does, and every container mounts it read-only. Every other volume is a ConfigMap, Secret, emptyDir, projected, downward API or PersistentVolumeClaim volume, so no other pod mounts a host path.

The policy trusts the engine, router, gate and HAProxy labels only on an object whose sender it trusts: a workload the controller's ServiceAccount sends, a ReplicaSet the Deployment controller makes, and a pod the Job, StatefulSet or ReplicaSet controller makes for its owner. Anyone else who sends a pod or workload with those labels is refused, and nobody can add them to an existing pod. A pod's spec cannot change after it is created, so an update to a pod is checked only for its images and ephemeral containers. Helm applies the chart's own workloads as the installing user, so none of them carries one of those labels: the direct engines are `direct-engine` pods, which the policy grants nothing.

Admission cannot stop a user who can act as the controller. Kubernetes' `edit` and `admin` roles may impersonate the namespace's ServiceAccounts, request their tokens and exec into pods, so anyone with `edit`, `admin`, `impersonate` or `pods/exec` in the fleet's namespace has the controller's power, including reading the state claim and starting host-network engine pods with `IPC_LOCK`. Those are what the policy leaves open, beside one more: anyone who may create pods or Jobs in the namespace can mount its Secrets as volumes, which the policy allows. Run each fleet in a namespace of its own, give the people who run fleets only the `<release>-fleet-editor` Role there, and keep `edit`, `admin` and exec in that namespace to the people who install the chart.

The routers' NetworkPolicy also admits pods labelled as Prometheus. The chart runs Prometheus, so the policy cannot tie that label to one sender, and anyone who can create pods in the namespace can give a pod it. With `monitoring.enabled` off or `monitoring.serviceMonitors.enabled` set, the NetworkPolicy leaves that label out; scrape with a cluster Prometheus through `networkPolicy.monitoring` instead to close it.

The namespace runs with the Pod Security `privileged` level: the `baseline` level refuses the engine pods' host network and `IPC_LOCK`, and Pod Security cannot exempt single pods. If the cluster enforces `baseline` by default, label the namespace `pod-security.kubernetes.io/enforce=privileged`. The admission policy then enforces the baseline's rules on every other pod.

Helm creates the policy after the controller, and `helm uninstall` keeps the policy and its binding, so they outlive the controller and the workloads a fleet leaves behind. The controller may get both by name, to wait for them before it creates anything. Delete them by hand once the namespace no longer runs a fleet. `helm uninstall` also keeps the state claim: if the fleet is gone for good, delete the claim, or the namespace, since its run directories hold the salts and the key check's HMACs. Helm applies the policy after the chart's workloads, and the API server takes a few seconds to use a changed policy, so a direct engine that needs a source an install or upgrade adds to the allow-lists is refused until you upgrade again.

`modelVolumeSources` allows only `persistentVolumeClaim` by default. A hostPath also needs a directory in `modelHostPathPrefixes`, an NFS export a server in `modelNfsServers`, and a CSI volume a driver in `modelCsiDrivers`. Each list is empty by default, which keeps that source off.

## The fleet

`fleet` holds a `NarwhalFleet`'s `spec`, which `values.schema.json` checks against the CRD's schema; the API server checks the CRD's CEL rules when Helm applies it. [NarwhalFleet reference](https://athrael-soju.github.io/Narwhal/deploy/NarwhalFleet/) lists every field. The chart names the fleet `fleet` and marks it `argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true`, so one Argo CD Application can hold both charts. Leave `fleet` out to apply the fleet with `narwhal deploy --target kubernetes` or another tool.

## Direct engines

`directEngines` runs engines that start straight from their image, as `narwhal dev` starts them on a host, in place of a fleet's attested engines. They run on CPUs and take no launch plan, no attestation and no gate Jobs, so the fleet that routes over them has no `engine_contract`. The router admits them as it admits the engines of `narwhal dev`. Use them to test the chart or a cluster without GPUs. A production fleet needs the attested engines of a `NarwhalFleet`.

The chart applies them itself, because a `NarwhalFleet` has no direct engines and the controller builds only attested ones. Their pods carry the component label `direct-engine`, use neither the host network nor a capability, and run the `RuntimeDefault` seccomp profile, so the admission policy admits them by its general rules and trusts nothing about them.

`narwhal.deployment.direct_engine_set.direct_values` derives the values from a backend's direct start, which only the llama.cpp backend has. Each pod runs the command that start renders, listening on port 8080 at its pod address, with the model volume read-only at `/model` and the claim `narwhal-engine-shared` at `/narwhal-shared`. Every engine shares that ReadWriteMany claim for the slot files of its KV handoff. The engine's thread count follows its whole-CPU limit. The fleet file's engines, with their URLs `http://engine-<n>.<service>.<namespace>.svc.<cluster domain>:8080`, come from `direct_engine_set.fleet_engines`, and its profiles are nominal, measured as `narwhal dev up` measures them.

## Values

Keep a filled values file in the ignored `config/helm/` directory. `values.schema.json` refuses a release that leaves out any value without a default.

| Value | Meaning |
| --- | --- |
| `images.narwhal` | The Narwhal image by registry digest, `<repository>@sha256:<hex>`. The controller runs it, and so do the routers, gate Jobs, export Jobs and attestation sidecars it makes |
| `controller.runAsUser`, `controller.runAsGroup` | The controller's non-root user and group, default 10001. The group owns the run directories on the state volume |
| `controller.keyCheckPeriodSeconds` | Seconds between the controller's checks of the engine API key, default 900. Each check runs a Job; an edit of the fleet or a new `requested-at` annotation also checks it |
| `controller.resources`, `controller.nodeSelector`, `controller.tolerations` | The controller pod's resources and placement |
| `fleet` | A `NarwhalFleet` spec, under [The fleet](#the-fleet) |
| `directEngines` | Engines that start directly, from `direct_values` |
| `state.claimName`, `state.storageClassName`, `state.size` | The state claim, default `narwhal-state`, and its ReadWriteMany storage class and size |
| `haproxy.clusterDomain` | The cluster's DNS domain in the addresses Prometheus scrapes, default `cluster.local` |
| `modelVolumeSources`, `modelHostPathPrefixes`, `modelNfsServers`, `modelCsiDrivers` | The model volume allow-lists, under [Who may write the fleet](#who-may-write-the-fleet) |
| `sidecarUser` | The non-root user each engine pod's attestation sidecar runs as, 10002 by default. The controller passes it to the engine pods |
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

## Routers

The controller runs the router pair, `router-0` and `router-1` in the headless Service `routers`, from the fleet's `routers` settings, as it runs the engines.

The routers elect the primary through the Lease `narwhal-router`, as [Electing the primary on Kubernetes](../../../docs/operate/01-Start-Routers.md#electing-the-primary-on-kubernetes) describes. The chart makes their ServiceAccount, Role and RoleBinding `narwhal-router`, because the controller holds no RBAC verbs. The Role grants `get` and `update` on that Lease and `create` on Leases, since RBAC cannot limit a create to one name. Only the router container mounts the ServiceAccount's token. Each router runs as UID 10001 on a read-only root filesystem and writes only its own claim at `/narwhal-router` and a `/tmp` scratch directory.

The controller runs HAProxy in front of the routers. It forwards only `POST /v1/completions`, `POST /v1/chat/completions`, `GET /v1/models`, `GET /health`, `GET /ready` and `GET /narwhal/targets`, and answers 404 to every other route. The NetworkPolicy `<release>-routers` admits connections to the routers' port 8000 only from HAProxy, the gate Jobs, the other router, the chart's Prometheus and the peers in `networkPolicy.monitoring`, so the control routes are reachable only from those. A NetworkPolicy matches pods by label and cannot check who made a pod; [Who may write the fleet](#who-may-write-the-fleet) states which labels the admission policy reserves. A NetworkPolicy needs a network plugin that enforces it.

## Monitoring

With `monitoring.enabled: true`, the chart runs Prometheus and Grafana from the files `make observe` stages: [`prometheus.yml`](../../../src/narwhal/observability/stack/prometheus.yml), [`prometheus-alerts.yml`](../../../src/narwhal/observability/stack/prometheus-alerts.yml) and the Grafana provisioning under [`src/narwhal/observability/stack/grafana`](../../../src/narwhal/observability/stack/grafana). The chart links them from `files/observability/`, so there is one copy, and a chart test fails when the mounted files differ from what `make observe` stages for the same routers.

Prometheus scrapes `router-0` and `router-1` by their pod DNS names and reads the engine targets from `/narwhal/targets` through the Service `haproxy`, which reaches the active router. Each engine is scraped once, as `make observe` does for a router pair, and the alert rules and dashboard read the pair as they do there. With `fleet` set, Prometheus also scrapes each engine pod's attestation sidecar at `engine-<n>.engines.<namespace>.svc:<attestation port>`, for the kubelet restart counts of the pod's containers and the `NarwhalHostDown` alert. Every series gets the `fleet` label. The Services `prometheus` (port 9090) and `grafana` (port 3000) serve them in the namespace. Grafana asks everyone to sign in unless `monitoring.grafana.anonymous` is set, which lets anyone who reaches it view the dashboards, as under `make observe`. It runs without the image renderer.

`monitoring.grafana.dashboard` needs rendering first, because its queries come from the installed engine backends: [Monitoring](https://athrael-soju.github.io/Narwhal/deploy/Kubernetes/#monitoring) gives the commands. `chart_dashboard --console-url` adds the fleet control link and markers that `make observe` adds.

| Value | Meaning |
| --- | --- |
| `monitoring.enabled` | Run Prometheus and Grafana, default `false` |
| `monitoring.fleet` | The `fleet` label of every series, so one Prometheus and one dashboard can hold several fleets. Default: the release name |
| `monitoring.alertmanagerUrl` | The Alertmanager that Prometheus sends alerts to, `http(s)://HOST:PORT` with an optional path. Without one, alerts show only on the Prometheus alerts page |
| `monitoring.prometheus.image`, `monitoring.grafana.image` | The images by registry digest. Required unless the cluster runs that service |
| `monitoring.grafana.dashboard` | The rendered dashboard. Required with monitoring, for the cluster's Grafana too |
| `monitoring.grafana.admin` | `{"secretName", "userKey", "passwordKey"}`: the Secret that holds the Grafana admin login. Required with the chart's Grafana |
| `monitoring.grafana.anonymous` | Let anyone who reaches Grafana view the dashboards without signing in, default `false` |
| `monitoring.prometheus.storage`, `monitoring.grafana.storage` | `{"storageClassName", "size"}` for a ReadWriteOnce data claim. Without one, the data lasts only as long as the pod |
| `monitoring.prometheus.resources`, `monitoring.grafana.resources` | Container `requests` and `limits` |
| `monitoring.serviceMonitors.enabled` | Leave Prometheus to the cluster's Prometheus Operator, default `false` |
| `monitoring.serviceMonitors.labels` | Labels for the monitors and the `PrometheusRule`, which the cluster's Prometheus selects them by |
| `monitoring.dashboardConfigMap.enabled` | Leave Grafana to the cluster, default `false` |
| `monitoring.dashboardConfigMap.label`, `monitoring.dashboardConfigMap.labelValue` | The label the Grafana dashboard sidecar watches, default `grafana_dashboard: "1"` |

Prometheus keeps 30 days of samples, as under `make observe`. A changed configuration or dashboard restarts the pod that reads it.

### The cluster's Prometheus and Grafana

On a cluster that already runs the Prometheus Operator and Grafana, such as kube-prometheus-stack, the chart can leave both to them:

```bash
helm upgrade --install narwhal deploy/helm/narwhal --namespace <namespace> \
  --values config/helm/<fleet>.yaml \
  --set monitoring.enabled=true \
  --set monitoring.serviceMonitors.enabled=true \
  --set monitoring.serviceMonitors.labels.release=kube-prometheus-stack \
  --set monitoring.dashboardConfigMap.enabled=true \
  --set-file monitoring.grafana.dashboard=config/helm/dashboard.json
```

The chart's Grafana reads only the chart's Prometheus, so the chart refuses `monitoring.serviceMonitors` without `monitoring.dashboardConfigMap`. `monitoring.dashboardConfigMap` alone keeps the chart's Prometheus.

With `monitoring.serviceMonitors.enabled`, the chart runs no Prometheus. It renders these objects from the same `prometheus.yml` and `prometheus-alerts.yml`:

| Object | What it scrapes or holds |
| --- | --- |
| `ServiceMonitor` `<release>-routers` | Both routers, through the Service `routers` on its port `http` |
| `PodMonitor` `<release>-sidecars` | The container `attestation` of each engine pod, at `engine-<n>.engines.<namespace>.svc:<attestation port>`. Rendered only with `fleet` set, which names the port |
| `ScrapeConfig` `<release>-engines` | The engines that `/narwhal/targets` lists, read through the Service `haproxy` on port 8000 |
| `PrometheusRule` `<release>-alerts` | The rules of `prometheus-alerts.yml`, unchanged |

With the fleet applied outside the chart, with `kubectl` or from a workstation, the chart renders no `PodMonitor`, so the cluster's Prometheus scrapes no attestation sidecar and `NarwhalHostDown` never fires. Keep the fleet in `fleet` to monitor the sidecars.

The monitors select what the controller builds by the labels `app.kubernetes.io/name: narwhal` and `app.kubernetes.io/component: router` or `engine`, and by the Service, container and port names above. `src/narwhal/deployment/kubernetes_objects.py` gives the objects those labels and names, and a chart test fails when the two drift apart. HAProxy serves no metrics of its own, so Prometheus reads only the engine targets through it.

Each target gets the job name, address and `fleet` label the bundled Prometheus gives it, and the 5-second scrape interval of `prometheus.yml`, which the router rules need: they count only samples of the last 30 seconds. The alert rules and the dashboard therefore read the same series as under the bundled stack. The Prometheus Operator also adds a `namespace` label.

The cluster's Prometheus picks up the objects whose labels match its `serviceMonitorSelector`, `podMonitorSelector`, `scrapeConfigSelector` and `ruleSelector`, so put those labels in `monitoring.serviceMonitors.labels`. `ScrapeConfig` is a `v1alpha1` kind, and a Prometheus without a `scrapeConfigSelector` scrapes no engine. The routers' NetworkPolicy admits the cluster's Prometheus only through `networkPolicy.monitoring`.

Several fleets can share one Prometheus: each series carries its fleet's `fleet` label, and every rule matches and aggregates within one fleet. Each release's `PrometheusRule` holds the same rules, so each raises the same alerts for every fleet, and Alertmanager merges the identical ones.

With `monitoring.dashboardConfigMap.enabled`, the chart runs no Grafana. It renders `monitoring.grafana.dashboard` as the ConfigMap `<release>-dashboard`, labelled for the Grafana dashboard sidecar. The dashboard is in Grafana's v2 dashboard format, which the cluster's Grafana must read, as the Grafana 13 of `make observe` does. Pick the cluster's Prometheus in the dashboard's data source variable.

## Checking the chart

`make chart-check` installs the pinned helm and kubeconform into `local/bin`, renders the chart for a release with a fleet, for the same release with the cluster's Prometheus and Grafana, and for one with direct engines. It validates the manifests against the Kubernetes API schemas and the Prometheus Operator v0.95.2 CRDs that `tools/maintenance/operator_schemas.py` pins, and runs `tests/deployment/test_helm_chart.py`.

`python -m tests.deployment.chart_live --kubeconfig <file> --namespace <ns> --release <name>` checks an installed release against an API server: what the controller's Role and ClusterRole grant and refuse, the objects the controller builds and the pods the admission policy must refuse, and its model volume rules against the controller's. With `--lifecycle <chart> <values> --upgrade-set <key>=<value>`, it first uninstalls the release, then installs, upgrades, uninstalls and installs it again with the admission policy in place, and checks each time that every workload the chart applies makes its pods.

`make chart-e2e` installs the pinned kind and kubectl as well, and runs both charts and the fleet controller on a kind cluster with a control plane and two workers. It builds the Narwhal image from the repository's `Dockerfile` and downloads the default `narwhal dev` template's model into `local/cache/chart-e2e/`. The workers advertise fake GPU and RDMA resources, and the test applies a `NarwhalFleet` with `kubectl apply --server-side` and nothing else. Without real GPUs the controller cannot pass `discover`, so the test checks the stages it reaches: the fleet becomes Ready up to `access`, neither the fleet nor the chart drifts, an edit reruns only the stages its input affects, a restarted controller adopts the running discovery Jobs without duplicating them, `spec.stopped` deletes the workloads and keeps the volumes, the admission policy refuses a namespace editor's forged engine pod, and deleting the fleet leaves its objects to garbage collection. It also checks that the API server stores the Prometheus Operator objects.

The serving checks run on two llama.cpp direct engines. The router pair and HAProxy come from the controller's own builders, sent as its ServiceAccount, because the controller builds them only at `router-start`. The routers must run on separate workers and answer 401 to `POST /narwhal/engines` without the control token. Under a steady stream of completions through the Service `haproxy`, the test deletes the router pod that holds the Lease. It fails unless the other router takes the Lease, every request succeeds and no request waits longer than the 10 seconds HAProxy holds one. Each check prints one JSON line. The test needs Docker and about 3 GB of free memory, since its three kind nodes peak at about 2.6 GiB, takes about six minutes, and deletes its cluster and image at the end.
