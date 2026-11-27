# Narwhal Helm chart

The chart runs one Narwhal fleet in one namespace:

- the engines, one pod each in the StatefulSet `engine`, with the attestation sidecar in each pod
- the headless Service the engine URLs name
- the state PersistentVolumeClaim and the router pair's lease claim
- the router pair, the StatefulSet `router`, each started as a standby of the other
- HAProxy in front of the routers, configured from [`deploy/ha/haproxy.cfg`](../../ha/haproxy.cfg)
- the gate Jobs of the deployment stage that is running

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
| `credentials.engineApiKey` | `{"env", "secretName", "key"}`: the Secret key the routers and gate Jobs read into the variable the fleet file's `engine.engine_api_key_env` names |
| `gates` | The gate Jobs of the running stage |

## Volumes

| Workload | State volume | Other volumes |
| --- | --- | --- |
| Engine pod | Its own directory, `engines/<role>/<pod UID>/`, at `/narwhal-state`. The engine container mounts it read-only. The sidecar also mounts the launch directory that checked the pod, `engines/<role>/launches/<pod UID>/`, read-only | The model volume, read-only |
| Router | The whole volume, read-only, at `/narwhal-state` | The lease at `/narwhal-lease/router.lease`, its own claim at `/narwhal-router` |
| Gate Job | The whole volume, writable, at `/narwhal-state` | A discovery Job mounts the model volume read-only at `/model` |

Only gate Jobs write outside an engine pod's own directory. The fleet file's `recovery.state_path` belongs under `/narwhal-router`, which holds each router's state alone.

## Routers and HAProxy

The routers are `router-0` and `router-1` in the headless Service `routers`. Each starts with `--router-id` set to its pod name and `--standby-of` set to the other, listening on port 8000 at its pod address with its journal at `/narwhal-router/journal.jsonl`. The first router to find the lease free takes it.

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

## Checking the chart

`make chart-check` installs the pinned helm and kubeconform into `local/bin`, renders the chart for a synthetic two-engine fleet, validates the manifests against the Kubernetes API schemas and runs `tests/deployment/test_helm_chart.py`.
