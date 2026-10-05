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

`make setup` creates the development environment when it is missing:

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

`constraints-dev.txt` pins the development toolchain. Install it by hand with:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' -c constraints-dev.txt
```

After any change to `pyproject.toml` or `constraints-dev.txt`, refresh the environment with:

```bash
make sync
```

`make sync` installs the editable package and its development dependencies against `constraints-dev.txt`, creating `.venv` when needed. You can point it at an existing virtual environment by setting `VENV_PYTHON`, which applies to `make sync`, tests, linting, formatting, and documentation:

```bash
make sync VENV_PYTHON=/path/to/venv/bin/python
```

These targets run the checks:

| Command                    | Action                                                                                                                       |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `make check`               | Runs publication and version metadata checks, Ruff lint and formatting, mypy, unit tests, and the documentation link checker |
| `make lint`, `make format` | Runs Ruff in check mode and reports lint and formatting errors                                                               |
| `make publication`         | Scans the Git index for private files and private key material                                                               |
| `make links`               | Checks links and HTML targets in unfenced Markdown, heading anchors, and canonical Narwhal URLs                              |
| `make docs-build`          | Builds the public site in strict mode and reports navigation, asset, and rendering errors                                    |
| `make test`                | Runs the unit suite                                                                                                          |

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

You can start the remote CI either from GitHub Actions or with `gh workflow run ci.yml --ref <branch>`, replacing `<branch>` with the branch name. Release automation dispatches the same suite for release PRs.

On the GitHub-hosted runners, the suite covers:

- `make check`
- the unit suite and installed-wheel checks on Python 3.11, 3.12, and 3.13
- the documentation build and unit suite from an extracted source distribution on Python 3.12

The wheel checks exercise console commands, package data, and HTTP routes outside the checkout.

CodeQL scans the Python source and GitHub Actions workflows weekly and on pushes and pull requests to `main` that change files outside `docs/`, Markdown, and `mkdocs.yml`.

### Coverage and test scope

`make coverage` runs the unit suite with package and tool coverage, writing HTML branch annotations, JSON and XML reports, and the test log to a fresh `runs/coverage/run-*` directory. Set the output path with `COVERAGE_ARGS='--out runs/coverage/review'`.

Place tests under `tests/` by component, assert a named failure or invariant, and reuse the synthetic profiles and fleets in `tests/fixtures.py`.

Fleet acceptance on GPU hosts follows [Deploying a fleet](docs/Deploy.md):

1. Inspect devices and artifacts.
2. Start each checked engine.
3. Qualify directed links against the live cache.
4. Attest and profile those processes.
5. Run preflight and routed load through the private path.

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
| Engine launch plans, runtime checks, stage supervision, and attestation capture     | `deployment/`                                         |
| Command results, CLI error rendering, and shared command options                    | `command_results.py`, `cli_errors.py`, `cli_support.py` |
| Serving entry point, versioned document contracts, build identity, and shared types | `cli.py`, `contracts.py`, `provenance.py`, `types.py` |

Paths are relative to `src/narwhal/`. Put each change in the package that implements the operation or holds the state.

`serving/lifecycle.py` manages individual requests, while `runtime/lifecycle/` handles engine drains and replacement.

Keep package initializers light and cross-package imports explicit, using `TYPE_CHECKING` for type-only imports across the serving/runtime boundary. Config models import scheduling definitions from `scheduling/control.py` and serving policy from `serving/policy.py`, and those two modules must stay independent of router construction.

Module responsibilities break down as follows:

- `profiling/fitting.py` implements numerical fitting and cross-validation, `model.py` implements profile validation and capacity calculations, and `store.py` implements persistence and fleet queries.
- `engines/stream.py` decodes server-sent events (SSE) and validates token identity for serving and profiling.
- `engines/client.py` holds HTTP deadlines.
- `engines/validation.py` selects role-permitted KV pairs for preflight and lifecycle readmission.
- `config/serialization.py` serializes the versioned fleet configuration for file output and CLI printing.
- `observability/metrics/render.py` composes section renderers in a fixed order, with metric names, labels, histogram buckets, and conditional emission all part of the metrics contract.

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
| `profiling/probe/offline.py`                                                 | TTFT refit and profile merge                                             |
| `profiling/probe/cli.py`                                                     | `narwhal-profile`                                                        |
| `runtime/lifecycle/records.py`                                               | Lifecycle errors, drain records and readmission outcomes                 |
| `runtime/lifecycle/manager.py`                                               | `LifecycleManager` drain and hold state                                  |
| `runtime/lifecycle/identity.py`                                              | Process identity binding, capture and profile recovery checks            |
| `runtime/lifecycle/readmission.py`                                           | Readmission validation                                                   |
| `diagnostics/check/report.py`                                                | Gate outcomes                                                            |
| `diagnostics/check/engines.py`                                               | Reach, calibration, contract, model, pace and tokenize gates             |
| `diagnostics/check/profiles.py`                                              | Profile, profile generation and SLO gates                                |
| `diagnostics/check/transfer.py`                                              | KV handoff production and directed transfer gates                        |
| `diagnostics/check/evidence.py`                                              | Directed KV evidence snapshots, writing and verification                 |
| `diagnostics/check/preflight.py`                                             | Gate order for one preflight run                                         |
| `diagnostics/check/cli.py`                                                   | `narwhal-check`                                                          |
| `serving/router/routing.py`                                                  | `NarwhalRouter` construction, request admission and live state           |
| `serving/router/sizing.py`                                                   | `RequestSizer` input counts and prefix-cache evidence                    |
| `serving/router/verification.py`                                             | `SuspectVerifier` leg failure classes and suspect verification           |
| `scheduling/scheduler/placement.py`                                          | `GlobalScheduler` placement, pricing, availability and engine health     |
| `scheduling/scheduler/occupancy.py`                                          | Decode occupancy projection and decode admission                         |
| `scheduling/scheduler/roles.py`                                              | `RoleChanges` role changes and controller decisions                      |
| `observability/metrics/exposition.py`                                        | Prometheus text lines and SLO-scaled histograms                          |
| `observability/metrics/traffic.py`, `runtime.py`, `engines.py`, `control.py` | Metric section renderers                                                 |
| `observability/metrics/render.py`                                            | Section order of the metrics document                                    |
| `deployment/launch_engine/runtime.py`                                        | Standard-library code for the engine image and serving hook              |
| `deployment/launch_engine/plan.py`                                           | Launch plan building and loading                                         |
| `deployment/launch_engine/docker.py`                                         | Docker commands and interrupted-command reconciliation                   |
| `deployment/launch_engine/check.py`                                          | Runtime check                                                            |
| `deployment/launch_engine/captures.py`                                       | Model dimension, cache layout, cache registration and handshake captures |
| `deployment/launch_engine/start.py`                                          | Container start and shared-GPU start                                     |
| `deployment/launch_engine/cli.py`                                            | `narwhal-engine`                                                         |
| `deployment/attestation_contract/evidence.py`                                | Checked plan and live engine binding                                     |
| `deployment/attestation_contract/capture.py`                                 | Live NIXL, model, transfer and HTTP captures                             |
| `deployment/attestation_contract/document.py`                                | Engine attestation document assembly                                     |
| `deployment/attestation_contract/sidecar.py`                                 | Attestation sidecar serving                                              |
| `deployment/attestation_contract/fleet.py`                                   | Router engine contract finalization                                      |
| `deployment/attestation_contract/cli.py`                                     | Contract tool command                                                    |

`make observe` runs `tools/observability/start/cli.py`:

| Module                                      | Contents                                         |
| ------------------------------------------- | ------------------------------------------------ |
| `tools/observability/start/services.py`     | Listener and service contracts                   |
| `tools/observability/start/stack.py`        | Docker Compose control                           |
| `tools/observability/start/listeners.py`    | Listener occupancy and owner diagnostics         |
| `tools/observability/start/readiness.py`    | Service, dashboard and target readiness          |
| `tools/observability/start/cli.py`          | Startup sequence and command                     |

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

### Working files and deployment artifacts

Keep evaluation builders, generators, deployment-specific datasets, experiment configurations, generated results, research ledgers, and paper working files outside the tracked source tree.

| Data                                      | Location                                                                                         |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Local profiles, journals, and run outputs | `runs/`                                                                                          |
| Working fleet configurations              | `config/fleet.json` or the ignored `config/fleet.*.json` pattern                                 |
| Live engine addresses and site paths      | Those ignored files, or node URLs from the ignored `.env` through the documented endpoint syntax |

Site automation provides host credentials, the source distribution, network configuration, and engine process launch. [Gate D: Proving the transfer fabric against the serving cache](docs/deploy/04-Qualify-Fabric.md) defines the engine-facing fabric contract that automation must establish.

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
9. Merge the milestone PR after every task is checked off and the milestone criteria pass.
10. Close the issue and the milestone.

The issue records the outcome, scope, exclusions, dependencies, tasks, acceptance criteria, integration branch, and starting commit. Each task records its prerequisites and acceptance criteria as nested checkboxes, plus its package and required checks when they differ from the issue's defaults.

Link cross-milestone prerequisites explicitly. Deliver shared contracts through the defining milestone's merge to `main`, then update dependent integration branches from `main` after it lands.

### Hardware and model selection

Keep Narwhal's scope and shared contracts hardware and GPU agnostic. Milestones, issues, and tasks describe the required capabilities, topology, behaviour, and acceptance criteria.

Select hardware and model inputs when the corresponding work starts, keeping those selections, pinned execution inputs, and raw evidence in the private locations under [Working files and deployment artifacts](#working-files-and-deployment-artifacts).

Keep hardware and model identities out of public milestone and issue text, comments, and attachments, including any copied configuration and logs.

## Pull requests

Review the diff and commit messages and run the local checks before pushing. Push your branch to your fork, or to `athrael-soju/Narwhal` as a maintainer, and open a pull request in `athrael-soju/Narwhal`.

A task PR targets its [milestone integration branch](#milestone-delivery). A milestone PR or a standalone change targets `main`.

Keep each branch to one coherent change, and open a draft while implementation or evidence gathering continues.

The PR description should cover the problem, the resulting behaviour, and how you checked it, along with the relevant issue and, for a task, its task number. Include reproduction steps for a bug fix and note any checks that require hardware.

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

Prepare a release by checking the latest PyPI version, comparing the target version with the conventional commit bump, and, if they differ, putting one `Release-As` footer in the preceding squash commit. Confirm that the release PR's package, citation, manifest, and changelog versions all agree.

After the release PR merges, the release workflow publishes the reviewed wheel and source archive to GitHub and PyPI.

## Documentation publishing

Edit the Markdown under `docs/` through a pull request, adding each public page to `nav` in `mkdocs.yml`. The docs workflow builds every pull request in strict mode and publishes `main` to GitHub Pages, with `make docs-build` producing the same site under `site/`.