# Contributing

## Set up the checkout

Fork [Narwhal](https://github.com/athrael-soju/Narwhal), then clone your fork.

```bash
git clone git@github.com:YOUR-USERNAME/Narwhal.git
cd Narwhal
git remote add upstream https://github.com/athrael-soju/Narwhal.git
git fetch upstream
git switch -c describe-your-change upstream/main
```

Install the development tools and local checks.

```bash
make setup
.venv/bin/pre-commit install
make check
```

`constraints-dev.txt` pins the development toolchain. The equivalent manual install is:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' -c constraints-dev.txt
```

`make check` runs the full local validation pass: publication and version metadata checks, Ruff lint and formatting, mypy, unit tests, three CPU process drills and the documentation link checker.

CI is started manually and runs `make check` on GitHub-hosted runners. It also runs the unit suite and installed-wheel checks on Python 3.11, 3.12 and 3.13. Python 3.12 collects subprocess coverage and runs the unit suite and integration drills from an extracted source distribution. The wheel checks exercise console commands, package data and HTTP routes outside the checkout.

The CI jobs use the repository's CPU fixtures and a standard read-only GitHub token. For a narrower test pass, `make test` runs the unit tests and three CPU integration drills.

### Coverage and test scope

Run `make coverage` to measure lines and branches across the unit suite and CPU drills. Reports, subprocess data and logs go under a new `runs/coverage/run-*` directory. Select a new output directory with `COVERAGE_ARGS='--out runs/coverage/review'`. The HTML report annotates each source file; JSON records the suite contexts. CI retains these artifacts for 14 days.

Coverage measures package and tool code, so every counted line ships with the distribution; the HTML report renders executable source branches inline for reviewers. Each new test should assert a named failure or invariant. Shared CPU profiles and fleets live in `tools/tests/fixtures.py`.

CPU checks use local stubs, temporary files and synthetic credentials. Release validation requires live engines for kernel execution, memory capacity, NIXL transfers and measured profiles. Validate each production engine shape with two engines at its intended TP size and context. Check attestation, the ring, cross-engine generation and token accounting, holder-wave recovery, profiles, exact-output canaries and deployment load. Prefix-cache-enabled configurations also require exact-replay and continuation checks.

## Behaviour changes

Use the CPU integration drills to validate serving and recovery changes, adding a focused scenario when a change exercises behaviour beyond the existing drill paths.

Add operator settings to `FleetConfig` and document them in the annotated example, keeping secret values in environment variables. `NARWHAL_FLEET` selects the fleet configuration.

Keep the engine contract portable. Deployment automation owns hardware, model, image and launch details; the fleet config records the compatibility fields that Narwhal verifies.

## Repository layout

| Path           | Contents                                                       |
| -------------- | -------------------------------------------------------------- |
| `src/narwhal/` | Python package and scheduling implementation                   |
| `tools/`       | Operator commands, CPU integration drills, and support scripts |
| `config/`      | Shipped example and stub fleet configs                         |
| `docs/`        | Repository documentation and wiki source                       |
| `deploy/`      | Optional deployment infrastructure                             |
| `assets/`      | Images used by documentation                                   |

### Source responsibilities

| Area                                                                               | Owner                                                 |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------- |
| HTTP routes, router construction, admission, request execution and responses       | `serving/`                                            |
| Placement, role control, demand, cost scoring and engine availability              | `scheduling/`                                         |
| Engine HTTP client, API dialects, KV connectors and attestation                    | `engines/`                                            |
| Engine lifecycle, monitoring loop, persisted state, leases and failover            | `runtime/`                                            |
| Calibration probes, cost-model fitting and profile storage                         | `profiling/`                                          |
| Fleet preflight and exact-output canaries                                         | `diagnostics/`                                        |
| Prometheus metrics and request journals                                            | `observability/`                                      |
| Config models, JSON loading, validation and serialization                          | `config/`                                             |
| Serving entry point, versioned document contracts, build identity and shared types | `cli.py`, `contracts.py`, `provenance.py`, `types.py` |

Paths in this table are relative to `src/narwhal/`. Put changes in the package that owns the operation or state: `serving/lifecycle.py` manages individual requests, while `runtime/lifecycle.py` manages engine drains and replacement.

The scheduler estimates load from request activity, while the runtime monitoring loop checks whether the engines are healthy.

Keep package initializers light and cross-package imports explicit. Use `TYPE_CHECKING` for type-only imports across the serving/runtime boundary.

Because config models already import scheduling definitions and serving policy, those modules must stay independent of router construction to avoid circular imports.

Within those packages:

- `profiling/fitting.py` owns numerical fitting and cross-validation; `model.py` owns profile validation and capacity calculations; `store.py` owns persistence and fleet queries.
- `engines/stream.py` decodes SSE events and validates token identity for serving, profiling and canaries. HTTP deadlines belong to `engines/client.py`; each consumer owns its timing and completion requirements.
- `engines/validation.py` selects role-permitted KV pairs for preflight and lifecycle readmission.
- `config/serialization.py` builds fleet documents for file output and CLI printing.
- `observability/metrics.py` composes section renderers in a fixed order. Metric names, labels, histogram buckets and conditional emission are part of the metrics contract.

Runtime helpers take `NarwhalRouter` explicitly. Type injected HTTP transports as `httpx.AsyncBaseTransport` and resolve their type errors. Lease renewal requires a configured `FileLease`.

Use the installed `narwhal-*` commands in deployment scripts. `python -m narwhal.cli` also starts the router. Internal Python module paths may change between releases. Document schema identifiers such as `narwhal.state` name wire contracts.

Keep evaluation builders, generators, deployment-specific datasets, experiment configurations, generated results, research ledgers and paper working files outside the tracked source tree. Profiling, preflight and canary commands produce Narwhal-owned deployment evidence; the deployment load toolchain owns workload acceptance.

Store local profiles, journals and run outputs under `runs/`, and use `config/fleet.json` or the ignored `config/fleet.*.json` pattern for working fleet configs. Keep live engine addresses and site paths in those ignored files or reference node URLs from the ignored `.env` through the documented endpoint syntax.

Site automation owns host credentials, source distribution, network configuration and engine process launch. [Deploy Narwhal](docs/Deploy.md#4-prepare-the-transfer-fabric) defines the engine-facing fabric contract that automation must establish.

`make publication` scans the Git index for private files and private key material, so include new files in the index when checking them for publication.

`make links` checks links and HTML targets in unfenced Markdown, heading anchors and canonical Narwhal URLs against the checkout. It also renders the selected wiki pages and assets into a temporary directory and checks links against that flattened layout.

## Pull requests

Review the diff and commit messages and run the local checks before pushing.

Push your branch to your fork and open a pull request against `athrael-soju/Narwhal:main`. Maintainers use short-lived branches in the same repository. Keep the branch limited to one coherent change, and open a draft while implementation or evidence gathering continues.

Describe the problem, the resulting behaviour, and how you checked it. Link the relevant issue. Include reproduction steps for a bug fix and identify any checks that require hardware. Sanitize logs and configuration before attaching them.

Use a Conventional Commit prefix in the PR title, such as `fix: preserve queued requests` or `feat: add an engine dialect`. The prefix determines the release impact.

Bring the branch up to date with `main` and run `make check` locally before merge. Documentation changes also require `make wiki-check`.

A maintainer reviews the PR and local check results, then squash-merges it using the PR title. Branch protection requires a PR and blocks force pushes; GitHub deletes each branch at squash-merge.

Address review comments on the same branch and rerun the relevant checks after editing.

Update the docs when a config field, route, journal field, metric, CLI flag or operator procedure changes.

## Wiki publishing

Edit the Markdown under `docs/` through a pull request. `docs/wiki-pages.txt` selects the wiki pages. The `wiki` workflow publishes canonical `main` after a documentation or asset change.

Run `tools/publish_wiki.sh --check` to validate wiki links locally. To compare the rendered pages with the current wiki, run `make wiki-check`; it validates the layout, clones the wiki and reports the diff. The wiki commands use the checkout's `origin` by default; set `WIKI_URL` to target another wiki or a local test repository.
