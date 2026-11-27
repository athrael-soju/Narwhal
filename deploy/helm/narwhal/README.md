# Narwhal Helm chart

The chart runs one Narwhal fleet in one namespace:

- the engines, one pod each in the StatefulSet `engine`, with the attestation sidecar in each pod
- the headless Service the engine URLs name
- the state PersistentVolumeClaim and the router pair's lease claim
- the router pair, the StatefulSet `router`, each started as a standby of the other
- HAProxy in front of the routers, configured from [`deploy/ha/haproxy.cfg`](../../ha/haproxy.cfg)
- the gate Jobs of the deployment stage that is running
- Prometheus and Grafana, when `monitoring.enabled` is set

`narwhal deploy --target kubernetes` installs the chart and sets the `engines` and `gates` values. The cluster needs Kubernetes 1.27 or later, the GPU and RDMA device plugins the engine plans name, and a ReadWriteMany storage class for the state and lease claims. The lease volume must support `flock`, coherent reads and atomic rename across nodes.

## Engines

`engines` holds what `narwhal.deployment.launch_engine.engine_set.chart_values` derives from the launch directories that `narwhal-engine prepare --backend kubernetes` wrote, one per engine role. Do not write it by hand.

Each plan renders its engine pod into `kubernetes.json`. The StatefulSet's ordinals start at 1, so its pods are `engine-1` onwards, the names the plans' engine URLs use, `http://engine-<n>.<service>.<namespace>.svc:<port>`. Each pod reads its role from its name. Its containers mount that role's hook files and pod steps, and each step container replaces itself with the command its plan rendered, after checking the role's spec file against the SHA-256 the pod template pins. The engine process keeps the container's first PID and the environment its plan gives it.

`chart_values` refuses plans that differ in more than the role: the engine image, GPU count, resource names, model volume, state claim, service account, RuntimeClass, security settings and namespace must be equal. Engines on one node listen on different ports, so the pod template lists no container ports. The StatefulSet uses `updateStrategy: OnDelete`: only a drain and readmission replaces an engine pod.

## Values

Keep a filled values file in the ignored `config/helm/` directory. `values.schema.json` refuses a release that leaves out any value without a default.

| Value | Meaning |
| --- | --- |
| `images.narwhal` | The Narwhal image by registry digest, `<repository>@sha256:<hex>`. The routers, the gate Jobs and the sidecars run it, so it must equal the plans' `NARWHAL_KUBERNETES_SIDECAR_IMAGE` |
| `images.haproxy` | The HAProxy image by registry digest |
| `images.fabric` | The image the fabric Jobs run `ib_write_bw` from, by registry digest. Required when a fabric gate runs |
| `engines` | The engines, from `chart_values` |
| `state.storageClassName`, `state.size` | The state claim's ReadWriteMany storage class and size. The claim takes the name the plans give `NARWHAL_KUBERNETES_STATE_CLAIM` |
| `lease.storageClassName`, `lease.size` | The lease claim `narwhal-lease` |
| `routers.fleet` | The fleet file's path on the state volume |
| `routers.storageClassName`, `routers.size` | Each router's own claim, for its journal and state handoff |
| `routers.args` | More `narwhal-serve` options, such as `["--lease-ttl", "6"]` |
| `haproxy.replicas` | HAProxy pods, default 1 |
| `haproxy.clusterDomain` | The cluster's DNS domain, default `cluster.local` |
| `credentials.engineApiKey` | `{"env", "secretName", "key"}`: the Secret key the routers and gate Jobs read into the variable the fleet file's `engine.engine_api_key_env` names. Required, with the same Secret and key, when the engines read their API key from a Secret |
| `gates` | The gate Jobs of the running stage |
| `monitoring` | Prometheus and Grafana, under [Monitoring](#monitoring) |

## Volumes

| Workload | State volume | Other volumes |
| --- | --- | --- |
| Engine pod | Its own directory, `engines/<role>/<pod UID>/`, at `/narwhal-state`. The engine container mounts it read-only. The sidecar also mounts the launch directory that checked the pod, `engines/<role>/launches/<pod UID>/`, read-only | The model volume, read-only |
| Router | The whole volume, read-only, at `/narwhal-state` | The lease at `/narwhal-lease/router.lease`, its own claim at `/narwhal-router` |
| Gate Job | The whole volume, writable, at `/narwhal-state` | A discovery Job mounts the model volume read-only at `/model` |

Only gate Jobs write outside an engine pod's own directory. The fleet file's `recovery.state_path` belongs under `/narwhal-router`, which holds each router's state alone; a relative path lands there too.

## Routers and HAProxy

The routers are `router-0` and `router-1` in the headless Service `routers`. Each runs from `/narwhal-router` with `--router-id` set to its pod name, listening on port 8000 at its pod address with its journal at `/narwhal-router/journal.jsonl`. [`files/router-start.py`](files/router-start.py) decides how each one starts, as [Starting a router pair](../../../docs/operate/01-Start-Routers.md#starting-a-router-pair) does:

- On the first install, `router-0` finds no handoff at its `recovery.state_path` and no lease file. It starts without `--standby-of` and claims the free lease.
- `router-1`, and `router-0` once either file exists, start with `--standby-of` set to the other router, as a supervisor restarts one. A primary start while the other router held the lease would leave `router-0` a standby that never takes over.

When both routers stop and start again, each is a standby of the other, and the first to find the lease free resumes from the handoff at its state path. That handoff must be at most `--standby-max-handoff-age` seconds old, as [Lease behavior](../../../docs/operate/01-Start-Routers.md#lease-behavior) states, so when both routers stay down longer than that, neither takes over on its own.

HAProxy runs the shipped configuration with `router-a` at `router-0.routers.<namespace>.svc.<cluster domain>:8000` and `router-b` at `router-1`. A `resolvers` section reads the pod's DNS servers and holds an answer for one second, so HAProxy finds a replaced router pod at its new address. The Service `haproxy` serves port 8000.

## Gate Jobs

Each `gates` entry becomes one Job that runs once, within `deadlineSeconds`, with `backoffLimit: 0`:

| Field | Meaning |
| --- | --- |
| `name` | The Job name |
| `kind` | `discovery`, `engine-check`, `fabric-server`, `fabric-client`, `finalize-fleet`, `profile`, `calibrate` or `preflight` |
| `command` | The command the Job runs |
| `deadlineSeconds` | The stage's time limit |
| `node` | The node a `discovery` or fabric Job runs on, required for them |
| `env` | Plain variables |
| `resources` | Resource limits, such as GPUs for a discovery Job |

A fabric Job runs on the host network with one HCA from the RDMA device plugin and `IPC_LOCK`, in `images.fabric`. Every other Job runs in `images.narwhal`.

## Monitoring

With `monitoring.enabled: true`, the chart runs Prometheus and Grafana from the files `make observe` stages: [`prometheus.yml`](../../../tools/observability/prometheus.yml), [`prometheus-alerts.yml`](../../../tools/observability/prometheus-alerts.yml) and the Grafana provisioning under [`tools/observability/grafana`](../../../tools/observability/grafana). The chart links them from `files/observability/`, so there is one copy, and a chart test fails when the mounted files differ from what `make observe` stages for the same routers.

Prometheus scrapes `router-0` and `router-1` by their pod DNS names and reads the engine targets from `/narwhal/targets` through the Service `haproxy`, which reaches the active router. Each engine is scraped once, as `make observe` does for a router pair, and the alert rules and dashboard read the pair as they do there. The Services `prometheus` (port 9090) and `grafana` (port 3000) serve them in the namespace. Grafana lets anonymous users view dashboards, as under `make observe`, and runs without the image renderer.

The dashboard needs rendering first, because its queries come from the installed engine backends:

```bash
python -m tools.observability.chart_dashboard config/helm/dashboard.json
helm install fleet deploy/helm/narwhal -f config/helm/values.yaml \
  --set-file monitoring.grafana.dashboard=config/helm/dashboard.json
```

`--console-url` adds the fleet control link and markers that `make observe` adds.

| Value | Meaning |
| --- | --- |
| `monitoring.enabled` | Run Prometheus and Grafana, default `false` |
| `monitoring.prometheus.image`, `monitoring.grafana.image` | The images by registry digest. Required with monitoring |
| `monitoring.grafana.dashboard` | The rendered dashboard. Required with monitoring |
| `monitoring.grafana.admin` | `{"secretName", "userKey", "passwordKey"}`: the Secret that holds the Grafana admin login. Required with monitoring |
| `monitoring.prometheus.storage`, `monitoring.grafana.storage` | `{"storageClassName", "size"}` for a ReadWriteOnce data claim. Without one, the data lasts only as long as the pod |
| `monitoring.prometheus.resources`, `monitoring.grafana.resources` | Container `requests` and `limits` |

Prometheus keeps 30 days of samples, as under `make observe`. A changed configuration or dashboard restarts the pod that reads it.

## Checking the chart

`make chart-check` installs the pinned helm and kubeconform into `local/bin`, renders the chart for a synthetic two-engine fleet, validates the manifests against the Kubernetes API schemas and runs `tests/deployment/test_helm_chart.py`.
