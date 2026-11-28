# Contributing

## Setting up the checkout

Fork [Narwhal](https://github.com/athrael-soju/Narwhal) and clone your fork:

```bash
git clone git@github.com:YOUR-USERNAME/Narwhal.git
cd Narwhal
git remote add upstream https://github.com/athrael-soju/Narwhal.git
git fetch upstream
git switch -c describe-your-change upstream/main
```

Run all `make` commands from the repository root. `make help` lists the contributor commands and their overrides.

A codespace or the repository's dev container runs `make setup` when it is created and puts `.venv/bin` on the terminal's `PATH`.

`make setup` creates `.venv` when it is missing and installs the package with its development dependencies, pinned by `constraints-dev.txt`:

```bash
make setup
```

Install the pre-commit hooks:

```bash
.venv/bin/pre-commit install
```

Run the local checks:

```bash
make check
```

After any change to `pyproject.toml` or `constraints-dev.txt`, reinstall against the pins with:

```bash
make sync
```

To use an existing virtual environment, set `VENV_PYTHON`. It applies to `make sync`, tests, linting, formatting, and documentation:

```bash
make sync VENV_PYTHON=/path/to/venv/bin/python
```

These targets run the checks:

| Command                    | Action                                                                                                                                         |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `make check`               | Runs publication, version metadata and backend boundary checks, Ruff lint and formatting, mypy, unit tests, and the documentation link checker |
| `make lint`, `make format` | Runs Ruff in check mode and reports lint and formatting errors                                                                                 |
| `make types`               | Runs mypy on the `narwhal` package in `src/` with the settings in `pyproject.toml`, and fails on untyped function definitions                  |
| `make publication`         | Scans the Git index for private files and private key material                                                                                 |
| `make versions`            | Checks that the `pyproject.toml` version has the form `MAJOR.MINOR.PATCH` and equals the `CITATION.cff` version                                |
| `make boundary`            | Fails when a module in `src/narwhal/` outside `backends/` imports a backend package or names an engine-specific term                           |
| `make links`               | Checks links and HTML targets in unfenced Markdown, heading anchors, and canonical Narwhal URLs                                                |
| `make docs-build`          | Builds the public site in strict mode and reports navigation, asset, and rendering errors                                                      |
| `make test`                | Runs the unit suite                                                                                                                            |
| `make chart-check`         | Installs the pinned helm and kubeconform into `local/bin`, then lints the Helm chart, validates its manifests and runs the chart tests          |
| `make chart-e2e`           | Installs the pinned kind and kubectl too, then runs both charts and the fleet controller on kind and fails over its router pair                 |

Stage new files with `git add` before running `make publication`.

To apply safe Ruff fixes and formatting, use:

```bash
make fix
```

`make fix` formats `src/`, `tools/`, and `tests/`, but stops before formatting if any lint errors remain. Review the resulting diff before committing.

Pass unittest discovery options through `TEST_ARGS`:

```bash
make test TEST_ARGS='-k failover -v'
```

`make unit` and `make check` always run the full unit suite and ignore `TEST_ARGS`, including any value set in the environment.

To run the remote CI on a pull request, tick **Run CI** in its description. The suite starts when you tick the box and runs again on each push while the box stays ticked. You can also start it from GitHub Actions or with `gh workflow run ci.yml --ref <branch>`, replacing `<branch>` with the branch name. Release automation dispatches the same suite for release PRs.

On the GitHub-hosted runners, the suite covers:

- `make check`
- `make chart-check`
- the unit suite and installed-wheel checks on Python 3.11, 3.12, and 3.13
- the documentation build and unit suite from an extracted source distribution on Python 3.12

The wheel checks exercise console commands, package data, and HTTP routes outside the checkout.

CodeQL scans the Python source and GitHub Actions workflows weekly and on pushes and pull requests to `main` that change files outside `docs/`, Markdown, and `mkdocs.yml`.

### Coverage and test scope

`make coverage` runs the unit suite with package and tool coverage, writing HTML branch annotations, JSON and XML reports, and the test log to a fresh `runs/coverage/run-*` directory. Set the output path with `COVERAGE_ARGS='--out runs/coverage/review'`.

Place tests under `tests/` by component, assert a named failure or invariant, and reuse the synthetic profiles and fleets in `tests/fixtures.py`.

Run `make chart-check` after a change to either chart under `deploy/helm`, to `deployment/fleet_resource.py`, to `deploy/ha/haproxy.cfg`, to the monitoring files in `src/narwhal/observability/stack` or to the Kubernetes launch backend. Run `make chart-e2e` by hand after a change to the fleet controller, to the chart's controller, RBAC, admission policy or direct engines, to the router or HAProxy objects, or to the router lease. It needs Docker and about 6 GB of memory, so CI does not run it. The chart tests in `tests/deployment/test_helm_chart.py` also run in `make test` when helm is installed, and skip without it.

`tests/observability/test_engine_state_promql.py` runs promtool from the Prometheus image pinned in `src/narwhal/observability/stack/compose.yml`, or from a `promtool` on `PATH` with the same major version, and skips when neither is available. To run it locally, pull the image with `docker pull`.

To measure the router's per-request bookkeeping cost before and after a change, run `python tools/measurement/bookkeeping_bench.py`. It prints one JSON line per measurement with the median microseconds per call. `--calls` sets the calls per round, default 20000, and `--rounds` sets the rounds, default 7.

To count client errors through `deploy/ha/haproxy.cfg` across router takeovers, pull `haproxy:3.2-alpine` and run `python tools/measurement/takeover_check.py --out runs/takeover/<name>`. It starts two simulated engines, a router pair and HAProxy in Docker, offers 2 requests per second of open-loop load through HAProxy, and stops the active router with SIGTERM twice, restarting it as a standby as soon as it exits. `--signal KILL` sends SIGKILL, `--restart-after` restarts the stopped router that many seconds after the new holder is ready, and `--takeover-after` sets the standby's failed probes before it takes over, default 4, 0.25 seconds apart, to delay the new holder. `--both` also stops the standby with SIGTERM and restarts both routers at once, each a standby of the other. The check also exits 1 when both routers report ready at once. The check writes `report.json`, the per-request rows and the HAProxy log to the `--out` directory, and exits 1 when a client request fails, apart from a request in flight on a killed router. When the image is present, `tests/measurement/test_takeover_check.py` runs three cases. The first is a SIGTERM takeover with an immediate restart and a 3-second gap. The second is a SIGKILL takeover by the standby. The third is a `--both` restart after a SIGKILL. Without the image, the test skips.

Fleet acceptance on GPU hosts follows [Deploying a fleet](docs/Deploy.md):

1. Inspect devices and artifacts.
2. Start each checked engine.
3. Qualify directed links against the live cache.
4. Attest and profile those processes.
5. Run preflight.
6. Start the router, then [start the monitoring stack](docs/deploy/07-Serve-and-Measure.md#starting-the-monitoring-stack) with `make observe`. Keep it running until the fleet stops.
7. Run routed load through the private path.
8. Check the behaviour under test in the **Narwhal Orchestrator** dashboard, and keep screenshots of those panels with the private records.

### Documentation preview

Install the documentation tools:

```bash
make docs-setup
```

Start the local preview:

```bash
make docs-serve
```

Open `http://127.0.0.1:8000/Narwhal/` in a browser.

MkDocs runs in the foreground and reloads the preview as you edit the documentation. Press Ctrl+C to stop it. If port 8000 is in use, run `make docs-serve DOCS_PORT=8001` and open `http://127.0.0.1:8001/Narwhal/`.

Run the strict build before submitting documentation changes:

```bash
make docs-build
```

MkDocs clears and rebuilds `site/`, and any documentation warning fails the build.

## Behaviour changes

Add focused tests for serving and recovery changes, and validate process replacement and router failover on a deployed fleet with the [release drills](docs/operate/05-Release-Drills.md).

Put operator settings in `FleetConfig` and document them in `config/fleet.example.json`, keeping secret values in environment variables. `NARWHAL_FLEET` selects the fleet configuration.

Keep the engine contract portable, leaving hardware, model, image, and launch details to the deployment automation. The fleet configuration records the compatibility fields that Narwhal verifies.

Update the docs whenever a config field, route, journal field, metric, CLI flag, or operator procedure changes.

## Contributing a GPU recipe

To contribute a Narwhal dev template for a new small CUDA GPU, follow the checks and pull request workflow above and submit the working template with qualification evidence. The evidence records the GPU, driver, model and runtime versions; the free-memory reserve and peak startup memory; both directed KV transfers; and the routed completion. A contribution for a GPU from another vendor includes a measured template and, optionally, discovery, memory accounting, engine launch and a compatible transfer runtime.

## Repository layout

Each top-level directory holds one part of the project:

| Path           | Contents                                         |
| -------------- | ------------------------------------------------ |
| `src/narwhal/` | Python package and scheduling implementation     |
| `tools/`       | Operator commands and support scripts            |
| `config/`      | Shipped configuration examples                   |
| `docs/`        | Repository documentation and GitHub Pages source |
| `deploy/`      | Optional deployment infrastructure               |
| `tests/`       | Unit tests and shared fixtures                   |
| `overrides/`   | MkDocs theme template and build hooks            |

### Source responsibilities

Each area of the Python package lives in one package or module:

| Area                                                                                | Package                                               |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------- |
| HTTP routes, router construction, admission, request execution, and responses       | `serving/`                                            |
| Placement, role control, demand, cost scoring, and engine availability              | `scheduling/`                                         |
| Engine HTTP client, API dialects, KV connectors, and attestation                    | `engines/`                                            |
| Engine lifecycle, engine monitoring, persisted state, leases, and failover          | `runtime/`                                            |
| Calibration probes, cost-model fitting, and profile storage                         | `profiling/`                                          |
| Fleet preflight and KV transfer checks                                              | `diagnostics/`                                        |
| Prometheus metrics and request journals                                             | `observability/`                                      |
| Config models, JSON loading, validation, and serialization                          | `config/`                                             |
| Narwhal dev templates and the local instance lifecycle                              | `dev/`                                                |
| Engine launch plans and checks, stage runs, process supervision, attestation capture, and `narwhal deploy` | `deployment/` |
| Engine backend registry and backend implementations                                 | `backends/`, `backends/<name>/`                       |
| Command results, CLI error rendering, and shared command options                    | `command_results.py`, `cli_errors.py`, `cli_support.py` |
| Serving entry point, versioned document contracts, build identity, and shared types | `cli.py`, `contracts.py`, `provenance.py`, `types.py` |

Paths are relative to `src/narwhal/`. Put each change in the package that implements the operation or holds the state.

Each package defines the interfaces for the operations it owns. `backends/<name>/` implements them for one engine. Other code loads a backend with `narwhal.backends.load`.

`serving/lifecycle.py` manages individual requests, while `runtime/lifecycle/` handles engine drains and replacement.

Keep package initializers light and cross-package imports explicit, using `TYPE_CHECKING` for type-only imports across the serving/runtime boundary. Config models import scheduling definitions from `scheduling/control.py` and serving policy from `serving/policy.py`, and those two modules must stay independent of router construction.

Module responsibilities break down as follows:

- `profiling/fitting.py` implements numerical fitting and cross-validation, `model.py` implements profile validation and capacity calculations, and `store.py` implements persistence and fleet queries.
- `engines/stream.py` decodes server-sent events (SSE) and validates token identity for serving and profiling.
- `engines/client.py` holds HTTP deadlines.
- `engines/handoff.py` sends a request to its prefill and decode engines, one after the other or both at once, as the KV connector specifies.
- `engines/validation.py` selects role-permitted KV pairs for preflight and lifecycle readmission.
- `config/serialization.py` serializes the versioned fleet configuration for file output and CLI printing.
- `observability/metrics/render.py` renders the metric sections in a fixed order. Metric names, labels, histogram buckets, and conditional emission are part of the metrics contract.

Runtime helpers take `NarwhalRouter` explicitly, and injected HTTP transports should be typed as `httpx.AsyncBaseTransport` with their type errors resolved. Lease renewal requires a configured `FileLease`.

Use the installed `narwhal-*` commands in deployment scripts. Each wire contract carries a document schema identifier, such as `narwhal.state`.

### Subpackages

Import each name from the subpackage module that defines it.

| Module                                                                       | Contents                                                                 |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `profiling/probe/engine.py`                                                  | Engine metric scrapes, tokenization, prompt sizing and completion bodies |
| `profiling/probe/sweep.py`                                                   | Sweep grids and their bounds from live engine limits                     |
| `profiling/probe/prefill.py`, `decode.py`, `warm.py`                         | Cold prefill, decode token-gap and warm prefill probes                   |
| `profiling/probe/instance.py`                                                | One engine's profile from its sweeps                                     |
| `profiling/probe/neighbours.py`                                              | Paced load on engines that share a GPU group                             |
| `profiling/probe/fleet.py`                                                   | Fleet profiling run                                                      |
| `profiling/probe/drain.py`                                                   | Router drain check of the engines a role-switching sweep uses            |
| `profiling/probe/offline.py`                                                 | TTFT refit and profile merge                                             |
| `profiling/probe/cli.py`                                                     | `narwhal-profile`                                                        |
| `profiling/membership.py`                                                    | Profile and calibration checks of an engine set                          |
| `runtime/lifecycle/records.py`                                               | Lifecycle errors, drain records and readmission outcomes                 |
| `runtime/lifecycle/manager.py`                                               | `LifecycleManager` drain and hold state                                  |
| `runtime/lifecycle/identity.py`                                              | Process identity binding, capture and profile recovery checks            |
| `runtime/lifecycle/readmission.py`                                           | Readmission validation                                                   |
| `runtime/lifecycle/join.py`                                                  | Join validation and the generation that adds an engine                   |
| `runtime/lifecycle/leave.py`                                                 | Leave drains and the generation that removes an engine                   |
| `diagnostics/check/report.py`                                                | Gate outcomes                                                            |
| `diagnostics/check/engines.py`                                               | Reach, calibration, contract, model, pace and tokenize gates             |
| `diagnostics/check/profiles.py`                                              | Profile, profile generation and SLO gates                                |
| `diagnostics/check/transfer.py`                                              | KV handoff production and directed transfer gates                        |
| `diagnostics/check/evidence.py`                                              | Directed KV evidence snapshots, writing and verification                 |
| `diagnostics/check/preflight.py`                                             | Gate order for one preflight run                                         |
| `diagnostics/check/cli.py`                                                   | `narwhal-check`                                                          |
| `serving/router/routing.py`                                                  | `NarwhalRouter` construction, request admission and live state           |
| `serving/router/membership.py`                                               | `Membership` engine membership snapshot and its generation               |
| `serving/router/sizing.py`                                                   | `RequestSizer` input counts and prefix-cache evidence                    |
| `serving/router/verification.py`                                             | `SuspectVerifier` leg failure classes and suspect verification           |
| `scheduling/scheduler/placement.py`                                          | `GlobalScheduler` placement, pricing, availability and engine health     |
| `scheduling/scheduler/occupancy.py`                                          | Decode occupancy projection and decode admission                         |
| `scheduling/scheduler/roles.py`                                              | `RoleChanges` role changes and controller decisions                      |
| `observability/metrics/exposition.py`                                        | Prometheus text lines and SLO-scaled histograms                          |
| `observability/metrics/traffic.py`, `runtime.py`, `engines.py`, `control.py` | Metric section renderers                                                 |
| `observability/metrics/render.py`                                            | Section order of the metrics document                                    |
| `deployment/graph.py`                                                        | Stage graphs: stages, facts and rerun rules                              |
| `deployment/deploy_graph.py`                                                 | The fleet deployment stage graph                                         |
| `deployment/dev_graph.py`                                                    | The `narwhal dev up` stage graphs                                        |
| `deployment/run_record.py`                                                   | Run record contract and the run directory lock                           |
| `deployment/runner.py`                                                       | Stage runner: skips, reruns, attempts and cancellation                   |
| `deployment/hosts.py`                                                        | Host inventory, SSH with pinned host keys, and remote command time limits |
| `deployment/discovery.py`                                                    | Host discovery and the generated deployment configuration                |
| `deployment/role_env.py`                                                     | Role environments and the helper snapshots of each backend               |
| `deployment/install.py`                                                      | Deployment packages and their installation on SSH hosts                  |
| `deployment/cache_groups.py`                                                 | Cache-equivalence groups of the engine roles                             |
| `deployment/fabric_budget.py`                                                | Fabric workload budgets, link records and edge evidence                  |
| `deployment/fabric.py`                                                       | Fabric qualification: transfer measurement, budgets and directed edges   |
| `deployment/cli.py`                                                          | `narwhal deploy`                                                         |
| `deployment/trial.py`                                                        | The capacity trial's definition, prerequisites and Prometheus series checks |
| `deployment/load_trial.py`                                                   | Synthetic load client of the capacity trial and the measurement tools    |
| `deployment/reconcile.py`                                                    | Join of client offers to router journals and counters                    |
| `deployment/launch_engine/runtime.py`                                        | File digests and private file writes                                     |
| `deployment/launch_engine/checkpoint.py`                                     | Checkpoint file digests and status, also run by discovery on engine hosts |
| `deployment/launch_engine/accelerators.py`                                   | GPU inventory, also run by discovery on engine hosts                     |
| `deployment/launch_engine/plan.py`                                           | Launch plan building and loading                                         |
| `deployment/launch_engine/settings.py`                                       | Runtime fields that become engine launch flags                           |
| `deployment/launch_engine/docker.py`                                         | Docker commands and interrupted-command reconciliation                   |
| `deployment/launch_engine/check.py`                                          | Runtime check                                                            |
| `deployment/launch_engine/verdict.py`                                        | Verdict on runtime probe output, also run in a Kubernetes engine pod     |
| `deployment/launch_engine/inspection.py`                                     | Engine host inspection before launch                                     |
| `deployment/launch_engine/probe.py`                                          | Live HTTP probe of a started engine                                      |
| `deployment/launch_engine/evidence.py`                                       | Evidence and failed checks of `inspect-host` and `probe`                 |
| `deployment/launch_engine/validation.py`                                     | `narwhal-engine inspect-host` and `probe`                                |
| `deployment/launch_engine/backend.py`                                        | Launcher, live probe, cache capture, transfer measurement and model dimension interfaces for backends |
| `deployment/launch_engine/start.py`                                          | Container start and shared-GPU start                                     |
| `deployment/launch_engine/kubernetes.py`                                     | Kubernetes engine pod manifests and the check of the pod's records         |
| `deployment/launch_engine/pod.py`                                            | Engine pod steps, run with the engine image's Python                       |
| `deployment/launch_engine/engine_set.py`                                     | The engine StatefulSet's values, from every engine's Kubernetes launch plan |
| `deployment/direct_engine_set.py`                                            | The Helm chart's direct engine values, from a backend's direct start         |
| `deployment/launch_engine/cli.py`                                            | `narwhal-engine`                                                         |
| `deployment/fleet_resource.py`                                               | The `NarwhalFleet` API group, CRD schema and printer columns, and the variables a fleet's spec gives |
| `deployment/kubernetes_graph.py`                                             | The Kubernetes stage graph                                               |
| `deployment/kubernetes_target.py`                                            | The Kubernetes stages' actions, live checks, Job adoption and stop       |
| `deployment/kubernetes_objects.py`                                           | The engine, gate Job, router and HAProxy objects the controller derives, and their pruning |
| `deployment/kubernetes_gate.py`                                              | What a gate Job runs around its command, and the files its log returns   |
| `deployment/kubernetes_controller.py`                                        | The fleet controller: its loop, Lease, status and stop                   |
| `runtime/kubernetes_api.py`                                                  | The in-cluster Kubernetes API client, informer and workqueue             |
| `runtime/kubernetes_lease.py`                                                | The router and controller election on a `coordination.k8s.io` Lease      |
| `deployment/attestation_contract/evidence.py`                                | Matches the live engine to its checked plan, and reads model dimensions  |
| `deployment/attestation_contract/document.py`                                | Engine attestation document assembly                                     |
| `deployment/attestation_contract/capture.py`                                 | One engine's attestation captures, in attempts, and the published one    |
| `deployment/attestation_contract/sidecar.py`                                 | Attestation sidecar serving and verification                             |
| `deployment/attestation_contract/fleet.py`                                   | Router engine contract finalization                                      |
| `deployment/attestation_contract/cli.py`                                     | Contract tool command                                                    |

`make observe` runs `src/narwhal/observability/stack/start/cli.py`:

| Module                                      | Contents                                         |
| ------------------------------------------- | ------------------------------------------------ |
| `src/narwhal/observability/stack/make_targets.py`       | Router origin validation and the Prometheus router targets |
| `src/narwhal/observability/stack/start/services.py`     | Listener and service contracts                   |
| `src/narwhal/observability/stack/start/stack.py`        | Docker Compose control                           |
| `src/narwhal/observability/stack/start/listeners.py`    | Listener occupancy and owner diagnostics         |
| `src/narwhal/observability/stack/start/readiness.py`    | Service, dashboard and target readiness          |
| `src/narwhal/observability/stack/start/cli.py`          | Startup sequence and command                     |

`src/narwhal/observability/stack/chart_dashboard.py` renders the same dashboard for the Helm chart's Grafana.

The [router benchmark](docs/measure/09-Router-Benchmark.md) runs `tools/measurement/router_benchmark/cli.py`:

| Module                                             | Contents                                                      |
| -------------------------------------------------- | ------------------------------------------------------------- |
| `tools/measurement/router_benchmark/cpus.py`       | CPU lists, SMT siblings, CPU allocation, pinning and CPU time |
| `tools/measurement/router_benchmark/fleet.py`      | Simulated fleet, engine roles and profiles                    |
| `tools/measurement/router_benchmark/processes.py`  | Child environment, readiness, process records and shutdown    |
| `tools/measurement/router_benchmark/files.py`      | Private output files                                          |
| `tools/measurement/router_benchmark/client.py`     | Load client process                                           |
| `tools/measurement/router_benchmark/sampling.py`   | Router, client, engine and core samples                       |
| `tools/measurement/router_benchmark/report.py`     | Rate rows, stop conditions and the benchmark point            |
| `tools/measurement/router_benchmark/comparison.py` | Alternating runs and the comparison document                  |
| `tools/measurement/router_benchmark/render.py`     | Report and comparison text                                    |
| `tools/measurement/router_benchmark/run.py`        | One run: engines, router, rate sweep and report               |
| `tools/measurement/router_benchmark/cli.py`        | Options and command                                           |

The [fleet control service](docs/operate/06-Controlling-the-Fleet.md) runs `src/narwhal/fleet_control/cli.py`:

| Module                               | Contents                                                                |
| ------------------------------------ | ----------------------------------------------------------------------- |
| `src/narwhal/fleet_control/config.py`      | Private configuration, hooks, router, console settings and the token    |
| `src/narwhal/fleet_control/app.py`         | Bearer-token middleware, session, health and load-job routes            |
| `src/narwhal/fleet_control/service.py`     | Sessions, recorded actions, exclusive actions and baseline restore      |
| `src/narwhal/fleet_control/records.py`     | Action log and per-session run records                                  |
| `src/narwhal/fleet_control/journal.py`     | Router journal extracts for sessions and load jobs                      |
| `src/narwhal/fleet_control/hooks.py`       | Hook commands, their logs and timeouts                                  |
| `src/narwhal/fleet_control/engines.py`     | Engine hook actions, lifecycle drain and readmit, and engine state      |
| `src/narwhal/fleet_control/engine_hook.py` | Reference engine hook for container launches                            |
| `src/narwhal/fleet_control/overlays.py`    | Configuration overlays, cold restarts, restores and router readiness    |
| `src/narwhal/fleet_control/jobs.py`        | The single load-job slot and the job runner interface                   |
| `src/narwhal/fleet_control/workloads.py`   | Load settings, the workload library and job parameters                  |
| `src/narwhal/fleet_control/aiperf.py`      | AIPerf commands, client results and the workload route                  |
| `src/narwhal/fleet_control/live.py`        | Live load-job results from AIPerf's per-request records                 |
| `src/narwhal/fleet_control/signals.py`     | Router readiness, firing alerts and the service's Prometheus metrics    |
| `src/narwhal/fleet_control/console.py`     | Console page routes, its content security policy and Grafana panel URLs |
| `src/narwhal/fleet_control/console.html`   | Console page                                                            |
| `src/narwhal/fleet_control/cli.py`         | Options and command                                                     |

### Working files and deployment artifacts

Keep evaluation builders, generators, deployment-specific datasets, experiment configurations, generated results, research ledgers, and paper working files outside the tracked source tree.

| Data                                      | Location                                                                                         |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Local profiles, journals, and run outputs | `runs/`                                                                                          |
| Working fleet configurations              | `config/fleet.json` or the ignored `config/fleet.*.json` pattern                                 |
| Live engine addresses and site paths      | Those ignored files, or node URLs from the ignored `.env` through the documented endpoint syntax |
| Fleet control configurations and hooks    | The ignored `config/fleet-control.*.json` pattern                                                |
| Fleet control action log and run records  | `runs/fleet-control/`                                                                            |
| Benchmark plans and evidence output       | `runs/`                                                            |
| Helm values files of a deployment         | The ignored `config/helm/` directory                                                             |

The plan's `evidence.identity` holds the engine image reference and the host and GPU map (`gpu_allocation`), and the collector copies it into the point's `evidence.json`. A benchmark point's `summary.shareable.json` is the one evidence file meant for publication. The collector builds it from a fixed list of fields, so it holds no URLs, credentials, private file paths or request failure text. It names engines `engine-1`, `engine-2` and onward in order of first appearance, replaces the engine image reference and GPU allocation with SHA-256 digests, and keeps `narwhal_revision`, `model_id`, `benchmark_client_version`, `engine_version`, `checkpoint_revision` and `gpu_shape` exactly as supplied in the plan. Review those labels for sensitive data before you publish the file.

Site automation provides the hosts, their credentials and packages, the network configuration, and the staged checkpoint and engine image, as the [production boundary](docs/operate/01-Start-Routers.md#production-boundary) lists. The transfer fabric it configures must pass [Qualify the transfer fabric](docs/deploy/04-Qualify-Fabric.md).

## Issues

Use the [issue chooser](https://github.com/athrael-soju/Narwhal/issues/new/choose) to report a failure, request a feature, or suggest a documentation correction. New issues receive a label from their source:

| Label           | Use for                                      | Set by                        |
| --------------- | -------------------------------------------- | ----------------------------- |
| `bug`           | A confirmed failure or regression            | Bug report form               |
| `enhancement`   | A new capability or behaviour change         | Feature request form          |
| `documentation` | A change to operator or contributor guidance | Documentation correction form |
| `triage`        | An issue awaiting its label                  | Blank issue                   |

Combine labels when several apply. Maintainers replace `triage` with the applicable label. Set a milestone when the issue contributes to a planned deliverable, and link prerequisite or related issues in its description.

### Milestone delivery

For each newly scoped milestone:

1. Create one issue in the milestone.
2. Create the integration branch `milestone/<number>-<slug>` from current `main`, using the milestone number.
3. List the milestone's tasks as a numbered task list in the issue.
4. Branch `issue/<number>-<task>-<slug>` from the integration branch after the task's prerequisites merge, using the issue number and task number.
5. Open the task PR against the integration branch.
6. Update dependent work from the integration branch before review.
7. Check off each task after its PR merges and its criteria pass.
8. Open the milestone PR from the integration branch to `main`.
9. Merge the milestone PR after you check off every task and the milestone criteria pass.
10. Close the issue and the milestone.

The issue records the outcome, scope, exclusions, dependencies, tasks, acceptance criteria, integration branch, and starting commit. Each task records its prerequisites and acceptance criteria as nested checkboxes, plus its package and required checks when they differ from the issue's defaults.

Link cross-milestone prerequisites explicitly. Deliver shared contracts through the defining milestone's merge to `main`, then update dependent integration branches from `main` after it lands.

### Hardware and model selection

Keep Narwhal's scope and shared contracts hardware and GPU agnostic. Milestones, issues, and tasks describe the required capabilities, topology, behaviour, and acceptance criteria.

Select hardware and model inputs when the corresponding work starts. Keep those selections, pinned execution inputs, and raw evidence in the private locations under [Working files and deployment artifacts](#working-files-and-deployment-artifacts).

Keep hardware and model identities out of public milestone and issue text, comments, and attachments, including any copied configuration and logs.

## Pull requests

Review the diff and commit messages and run the local checks before pushing. Push your branch to your fork, or to `athrael-soju/Narwhal` as a maintainer, and open a pull request in `athrael-soju/Narwhal`.

A task PR targets its [milestone integration branch](#milestone-delivery). A milestone PR or a standalone change targets `main`.

Keep each branch to one coherent change, and open a draft while implementation or evidence gathering continues.

The PR description should cover the problem, the resulting behaviour, and how you checked it. It should also name the relevant issue and, for a task, its task number. Include reproduction steps for a bug fix and note any checks that require hardware.

Keep deployment selections in private execution records, and sanitize logs and configuration before attaching them.

Use a Conventional Commit prefix in the PR title, such as `fix: preserve queued requests` or `feat: add an engine dialect`. The required PR title check validates the prefix before squash merge. The prefix sets the release impact, with `docs:` reserved for documentation changes.

Release Please includes each `docs:` squash commit in the Documentation changelog section and proposes a patch release when a release contains only documentation changes.

Before merge:

- Bring the branch up to date with its PR target and run `make check`.
- For a milestone PR, update the integration branch from `main` and repeat the required checks.
- For documentation changes, run the strict build in [Documentation preview](#documentation-preview).

A maintainer reviews the PR and any manually requested CI results, then squash-merges it with the PR title. GitHub deletes each branch at squash-merge.

The `main` ruleset requires a PR and the automatic PR title check, and blocks force pushes. Address review comments on the same branch and rerun the relevant checks.

## PyPI publishing

Configure a GitHub trusted publisher on PyPI:

| Setting      | Value                  |
| ------------ | ---------------------- |
| PyPI project | `narwhal-inference`    |
| Repository   | `athrael-soju/Narwhal` |
| Workflow     | `release.yml`          |
| Environment  | `pypi`                 |

Prepare a release by checking the latest PyPI version and comparing the target version with the conventional commit bump. If they differ, put one `Release-As` footer in the preceding squash commit.

Check the release PR's versions:

```bash
python3 tools/maintenance/release.py check --release
```

The check fails unless the package, citation, manifest and changelog versions agree and the changelog's first section holds the release notes.

After the release PR merges, the release workflow publishes the reviewed wheel and source archive to GitHub and PyPI. It publishes only from a public repository whose `NARWHAL_RELEASES_ENABLED` variable is `true`.

The PyPI project description is `README.md` followed by `CHANGELOG.md`, so each version's page on PyPI shows its own release notes at the top of the changelog.

## Documentation publishing

Edit the Markdown under `docs/` through a pull request, adding each public page to `nav` in `mkdocs.yml`. The stage tables in `docs/Deploy.md` come from `src/narwhal/deployment/deploy_graph.py`, those in `docs/deploy/Kubernetes.md` from `kubernetes_graph.py`, and those in `docs/cli/Dev.md` from `dev_graph.py`: after changing a graph, run `.venv/bin/python tools/maintenance/stage_graph_doc.py`. The field tables in `docs/deploy/NarwhalFleet.md` come from `SCHEMA` in `src/narwhal/deployment/fleet_resource.py`: after changing it, run `.venv/bin/python tools/maintenance/crd_schema.py` for the charts and `.venv/bin/python tools/maintenance/fleet_reference_doc.py` for the page. The unit suite fails while a page or chart differs from its source. The docs workflow builds every pull request in strict mode and publishes `main` to GitHub Pages, with `make docs-build` producing the same site under `site/`.