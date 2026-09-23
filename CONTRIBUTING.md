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

CI is started manually and runs `make check` on GitHub-hosted runners. It also runs the unit suite and installed-wheel checks on Python 3.11, 3.12 and 3.13. Python 3.12 builds the documentation and runs the unit suite and integration drills from an extracted source distribution. The wheel checks exercise console commands, package data and HTTP routes outside the checkout.

The CI jobs use the repository's CPU fixtures and a standard read-only GitHub token. For a narrower test pass, `make test` runs the unit tests and three CPU integration drills.

### Coverage and test scope

`make coverage` runs the unit suite and CPU drills with package and tool coverage, writing HTML branch annotations, JSON suite contexts, subprocess data, and logs to a new `runs/coverage/run-*` directory. Set `COVERAGE_ARGS='--out runs/coverage/review'` to choose the output path.

Place tests under `tests/` by component, assert a named failure or invariant, and reuse the CPU profiles and fleets in `tests/fixtures.py`.

CPU checks use local stubs, temporary files and synthetic credentials. Fleet acceptance follows [Deploy a fleet](docs/Deploy.md) on GPU hosts: inspect devices and artifacts, launch a representative per runtime group, qualify directed links, attest the live engines, profile and preflight the fleet, then run routed load through the private path with observability.

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
| `docs/`        | Repository documentation and GitHub Pages source               |
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

Keep evaluation builders, generators, deployment-specific datasets, experiment configurations, generated results, research ledgers and paper working files outside the tracked source tree. Profiling and preflight commands produce Narwhal-owned deployment evidence; the deployment load toolchain owns workload acceptance.

Store local profiles, journals and run outputs under `runs/`, and use `config/fleet.json` or the ignored `config/fleet.*.json` pattern for working fleet configs. Keep live engine addresses and site paths in those ignored files or reference node URLs from the ignored `.env` through the documented endpoint syntax.

Site automation owns host credentials, source distribution, network configuration and engine process launch. [Deploy Narwhal](docs/deploy/04-Qualify-Fabric.md) defines the engine-facing fabric contract that automation must establish.

`make publication` scans the Git index for private files and private key material, so include new files in the index when checking them for publication.

`make links` checks links and HTML targets in unfenced Markdown, heading anchors and canonical Narwhal URLs against the checkout. `make docs-build` builds the public site in strict mode and reports navigation, asset and rendering errors.

## Pull requests

Review the diff and commit messages and run the local checks before pushing.

Push your branch to your fork and open a pull request against `athrael-soju/Narwhal:main`. Maintainers use short-lived branches in the same repository. Keep the branch limited to one coherent change, and open a draft while implementation or evidence gathering continues.

Describe the problem, the resulting behaviour, and how you checked it. Link the relevant issue. Include reproduction steps for a bug fix and identify any checks that require hardware. Sanitize logs and configuration before attaching them.

Use a Conventional Commit prefix in the PR title, such as `fix: preserve queued requests` or `feat: add an engine dialect`. The prefix determines the release impact.

Bring the branch up to date with `main` and run `make check` locally before merge. For documentation changes, install the documentation extra with `.venv/bin/pip install -e '.[docs]'` and run `make docs-build`.

A maintainer reviews the PR and local check results, then squash-merges it using the PR title. Branch protection requires a PR and blocks force pushes; GitHub deletes each branch at squash-merge.

Address review comments on the same branch and rerun the relevant checks after editing.

Update the docs when a config field, route, journal field, metric, CLI flag or operator procedure changes.

## PyPI publishing

Configure the `narwhal-inference` project on PyPI with a GitHub trusted publisher: owner `athrael-soju`, repository `Narwhal`, workflow `release.yml`, and environment `pypi`. The release job requests an OIDC token after the GitHub release artifacts pass the package and installed-wheel checks. Its PyPI check compares both artifact hashes before a retry; an existing version with different bytes stops the release.

Check the latest PyPI version before preparing a release. When PyPI already has the version release-please would choose, set a higher version with a single `Release-As` footer in the preceding squash commit. Check that the generated release PR agrees on the package, citation, manifest and changelog versions before merge. The release workflow publishes the reviewed wheel and source archive to GitHub and PyPI after that release PR is merged.

## Documentation publishing

Edit the Markdown under `docs/` through a pull request and add each public page to `nav` in `mkdocs.yml`. The docs workflow builds every pull request in strict mode and publishes `main` to GitHub Pages. Run `make docs-build` locally to produce the same site under `site/`.
