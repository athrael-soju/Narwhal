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

Run `make help` from the repository root to list contributor commands and their
overrides.

Create `.venv` with the development dependencies, install the pre-commit hooks,
and run all checks:

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

After pulling changes to `pyproject.toml` or `constraints-dev.txt`, refresh the
dependencies. `make sync` installs the editable package and development
dependencies using `constraints-dev.txt`, and creates `.venv` if needed.

```bash
make sync
```

To use an existing virtualenv for `make sync`, tests, linting, formatting or
documentation, set `VENV_PYTHON` to its Python executable:

```bash
make sync VENV_PYTHON=/path/to/venv/bin/python
```

## Local checks

| Command             | Purpose                                                                                   |
| ------------------- | ----------------------------------------------------------------------------------------- |
| `make check`        | Publication and version metadata checks, Ruff lint and formatting, mypy, unit tests, links |
| `make lint`         | Report lint errors without editing files                                                  |
| `make format`       | Report formatting errors without editing files                                            |
| `make publication`  | Scan the Git index for private files and private key material                             |
| `make links`        | Check links, HTML targets, heading anchors and canonical Narwhal URLs in unfenced Markdown |
| `make fix`          | Apply safe Ruff lint fixes, then format `src/`, `tools/` and `tests/`                     |

`make publication` reads the Git index, so add new files to the index before running it.

If `make fix` finds lint errors it cannot fix, it stops before formatting. Fix those
errors and rerun it. Review the diff before committing.

```bash
make fix
```

## Tests

`make test` runs the unit suite. Pass unittest discovery options through
`TEST_ARGS` to select tests or change verbosity:

```bash
make test TEST_ARGS='-k failover -v'
```

This runs tests whose fully qualified names contain `failover`, verbosely.
`make unit` and `make check` ignore `TEST_ARGS`, even when it is set in the
environment.

### Coverage and test scope

`make coverage` runs the unit suite with package and tool coverage. It writes HTML
branch annotations, JSON and XML reports, and the test log to a new
`runs/coverage/run-*` directory. Set `COVERAGE_ARGS='--out runs/coverage/review'`
to choose the output path.

Testing conventions:

- Place tests under `tests/` by component.
- Assert a specific failure or invariant.
- Reuse the synthetic profiles and fleets in `tests/fixtures.py`.

Fleet acceptance follows [Deploy a fleet](docs/Deploy.md) on GPU hosts: inspect devices
and artifacts, start each checked engine, qualify directed links against the live cache,
attest and profile those processes, then run preflight and routed load through the
private path.

## Continuous integration

Run CI manually from the Actions tab or with `gh workflow run ci.yml --ref <branch>`.
Release automation also runs it on release PRs.

CI runs `make check` on GitHub-hosted runners, plus the unit suite and installed-wheel
checks on Python 3.11, 3.12 and 3.13. The installed-wheel checks run console commands,
package data and HTTP routes outside the checkout. Python 3.12 also builds the
documentation and runs the unit suite from an extracted source distribution. Jobs use
synthetic test inputs and a read-only GitHub token.

CodeQL scans the Python source and GitHub Actions workflows on pushes and pull
requests to `main`, and weekly. It skips changes that only touch documentation.

## Documentation

Edit the Markdown under `docs/` through a pull request and add each public page to
`nav` in `mkdocs.yml`. The docs workflow builds every pull request in strict mode and
publishes `main` to GitHub Pages.

Update the docs when a config field, route, journal field, metric, CLI flag or
operator procedure changes.

### Documentation preview

Install the documentation tools:

```bash
make docs-setup
```

Start the local preview:

```bash
make docs-serve
```

Open `http://127.0.0.1:8000/Narwhal/`. MkDocs runs in the foreground and reloads the
preview after documentation edits. Press Ctrl+C to stop it.

If port 8000 is occupied, run `make docs-serve DOCS_PORT=8001` and open
`http://127.0.0.1:8001/Narwhal/`.

Before submitting documentation changes, run the strict build:

```bash
make docs-build
```

MkDocs clears and rebuilds `site/`. Warnings fail the build, including navigation,
asset and rendering errors. `make docs-build` produces the same site that the docs
workflow publishes.

## Making behavior changes

Add focused tests for serving and recovery changes. Validate process replacement and
router failover on a deployed fleet using the
[upgrade, rollback, and release drills](docs/operate/04-Upgrade-and-Validate.md).

Add operator settings to `FleetConfig` and document them in
`config/fleet.example.json`. Keep secret values in environment variables.
`NARWHAL_FLEET` selects the fleet configuration.

Keep the engine contract portable. Deployment automation owns hardware, model, image
and launch details. The fleet config records the compatibility fields that
Narwhal verifies.

## Repository layout

| Path           | Contents                                         |
| -------------- | ------------------------------------------------ |
| `src/narwhal/` | Python package and scheduling implementation     |
| `tools/`       | Operator commands and support scripts            |
| `tests/`       | Unit tests and synthetic fixtures                |
| `config/`      | Shipped configuration examples                   |
| `docs/`        | Repository documentation and GitHub Pages source |
| `docs/assets/` | Images used by documentation                     |
| `deploy/`      | Optional deployment infrastructure               |

### Source responsibilities

Paths in this table are relative to `src/narwhal/`. Put changes in the package that
owns the operation or state: `serving/lifecycle.py` manages individual requests,
while `runtime/lifecycle.py` manages engine drains and replacement.

| Package or module                                                                  | Responsibility                                                          |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `serving/`                                                                         | HTTP routes, router construction, admission, request execution and responses |
| `scheduling/`                                                                      | Placement, role control, demand, cost scoring and engine availability   |
| `engines/`                                                                         | Engine HTTP client, API dialects, KV connectors and attestation         |
| `runtime/`                                                                         | Engine lifecycle, monitoring loop, persisted state, leases and failover |
| `profiling/`                                                                       | Calibration probes, cost-model fitting and profile storage              |
| `diagnostics/`                                                                     | Fleet preflight and KV transfer checks                                  |
| `observability/`                                                                   | Prometheus metrics and request journals                                 |
| `config/`                                                                          | Config models, JSON loading, validation and serialization               |
| `deployment/`                                                                      | Deployment discovery, launch, budget and attestation helpers            |
| `dev/`                                                                             | Single-GPU workstation workflow                                         |
| `cli.py`, `contracts.py`, `provenance.py`, `types.py`                              | Serving entry point, versioned document contracts, build identity and shared types |
| `cli_errors.py`, `cli_support.py`, `command_results.py`                            | Command failure rendering, shared command options and command outcome documents |

Import rules:

- Keep package initializers light and cross-package imports explicit.
- Use `TYPE_CHECKING` for type-only imports across the serving/runtime boundary.
- Scheduling definitions and serving policy must not import router construction,
  because config models import them and the import would be circular.

Module notes:

- `profiling/fitting.py` owns numerical fitting and cross-validation.
- `profiling/model.py` owns profile validation and capacity calculations.
- `profiling/store.py` owns persistence and fleet queries.
- `engines/stream.py` decodes SSE events and validates token identity for serving and
  profiling. HTTP deadlines belong to `engines/client.py`; each consumer owns its
  timing and completion requirements.
- `engines/validation.py` selects role-permitted KV pairs for preflight and lifecycle
  readmission.
- `config/serialization.py` builds fleet documents for file output and CLI printing.
- `observability/metrics.py` composes section renderers in a fixed order. Metric names,
  labels, histogram buckets and conditional emission are stable interfaces.

Typing and runtime rules:

- Runtime helpers take `NarwhalRouter` explicitly.
- Type injected HTTP transports as `httpx.AsyncBaseTransport` and fix the resulting
  type errors.
- Lease renewal requires a configured `FileLease`.

Use the installed `narwhal-*` commands in deployment scripts. `python -m narwhal.cli`
also starts the router. Internal Python module paths may change between releases.
Schema identifiers such as `narwhal.state` name wire contracts.

### Working files and deployment artifacts

Keep evaluation and research material outside the tracked source tree. This covers
evaluation builders and generators, deployment-specific datasets, experiment
configurations, generated results, research ledgers and paper working files.
Profiling and preflight commands produce Narwhal-owned deployment evidence; the
deployment load toolchain owns workload acceptance.

Store local profiles, journals and run outputs under `runs/`. Keep working fleet
configs in `config/fleet.json` or `config/fleet.*.json`, which are ignored by Git.
Keep live engine addresses and site paths in those files, or reference node URLs from
the ignored `.env` using the endpoint syntax in the [fleet schema](docs/configuration/01-Fleet-Schema.md).

Site automation owns host credentials, source distribution, network configuration and
engine process launch. [Gate D: Prove the transfer fabric against the serving
cache](docs/deploy/04-Qualify-Fabric.md) defines the engine-facing fabric contract
that automation must establish.

## Issues

Use the [issue chooser](https://github.com/athrael-soju/Narwhal/issues/new/choose)
to report a failure, request a feature or suggest a documentation correction.

Labels:

| Label           | Use                                                                    |
| --------------- | ---------------------------------------------------------------------- |
| `bug`           | Confirmed failure or regression                                        |
| `enhancement`   | New capability or behavior change                                      |
| `documentation` | Change to operator or contributor guidance                             |
| `triage`        | Set on blank issues; maintainers replace it with the applicable label  |

The bug, feature and documentation forms select `bug`, `enhancement` and
`documentation`. Use more than one label when several apply.

Set a milestone when the issue is part of a planned deliverable, and link prerequisite
or related issues in its description.

### Milestone delivery

Use this workflow for each newly scoped milestone:

1. Create one parent issue in the milestone. Record its outcome, scope, exclusions,
   dependencies and acceptance gates.
2. Create its integration branch, `milestone/<number>-<slug>`, from current `main`.
   Use the milestone number and record the branch and starting commit in the parent
   issue.
3. Divide the work into native GitHub sub-issues under that parent, assigned to the
   same milestone. Give each sub-issue its package ownership, prerequisites,
   acceptance criteria and required checks. Choose the number of sub-issues to fit the
   work.
4. When a sub-issue starts, create `issue/<number>-<slug>` from the integration branch
   after its prerequisites have merged. Its PR must target the integration branch.
   Update dependent work from that branch before review.
5. Close each sub-issue explicitly after its PR merges and its acceptance criteria pass.
6. Keep live qualification gates open until the required measurements pass. A failed
   feasibility gate blocks dependent implementation.
7. Open the parent PR from the integration branch to `main`. Merge it after all
   sub-issues and milestone acceptance gates pass, then close the parent issue and
   milestone.

Link cross-milestone prerequisites. Deliver their shared contracts through
the owning milestone's merge to `main`, then update dependent integration branches
from `main`.

### Hardware and model selection

Keep Narwhal's scope and shared contracts hardware/GPU agnostic. Milestones, issues and
sub-issues describe required capabilities, topology, behavior and acceptance gates
without selecting hardware or models.

Select hardware and model inputs when the corresponding work starts. Keep those
selections, pinned execution inputs and raw evidence in the private locations
described under [Working files and deployment artifacts](#working-files-and-deployment-artifacts).
Keep hardware and model identities out of public milestone and issue text, comments
and attachments, including copied configuration and logs. Public qualification
records state the tested capabilities, outcomes, limits and a sanitized reference to
the private evidence. A passing run qualifies only its measured scope.

## Pull requests

Push your branch to your fork and open a pull request against the target branch in
`athrael-soju/Narwhal`. Sub-issue PRs target their
[milestone integration branch](#milestone-delivery); parent milestone PRs and
standalone changes target `main`. Maintainers use branches in the same repository.

- Keep the branch limited to one coherent change.
- Open a draft while implementation or evidence gathering continues.
- Review the diff and commit messages and run the local checks before pushing.
- Bring the branch up to date with its PR target and run `make check` locally before
  merge.
- For documentation changes, also run the strict build described under
  [Documentation preview](#documentation-preview).
- Before the parent milestone PR merges, bring its integration branch up to date with
  `main` and repeat the required checks on the combined changes.

Describe the problem, the resulting behavior, and how you checked it. Link the
relevant issue and, for a sub-issue, its parent. Include reproduction steps for a bug
fix and identify any checks that require hardware. Keep deployment selections in
private execution records and sanitize logs and configuration before attaching them.

Use a Conventional Commit prefix in the PR title, such as `fix: preserve queued
requests` or `feat: add an engine dialect`. The required PR title check validates the
prefix before squash merge, and Release Please uses it to decide the release bump.
Use `docs:` for documentation changes. Release Please lists each `docs:` squash commit
in the Documentation changelog section and proposes a patch release when documentation
is the only change since the previous release.

A maintainer reviews the PR and any manually requested CI results, then squash-merges
it using the PR title. The `main`
ruleset requires a PR and the PR title check, and blocks force pushes. GitHub deletes
the branch at merge. Address review comments on the same branch and rerun the
relevant checks.

## Releases (maintainers)

One-time PyPI setup: configure the `narwhal-inference` project with a GitHub trusted
publisher using owner `athrael-soju`, repository `Narwhal`, workflow `release.yml`,
and environment `pypi`.

The release job requests an OIDC token after the GitHub release artifacts pass the
package and installed-wheel checks. Before retrying a publish, it compares the hashes
of both artifacts with PyPI. If the version already exists with different content, the
release stops.

To prepare a release:

1. Check the latest PyPI version.
2. If the target differs from the version implied by the conventional commits, add a
   single `Release-As` footer to the message of the squash commit.
3. Before merging the generated release PR, confirm that the package, citation,
   manifest and changelog versions agree.
4. After the release PR merges, the release workflow publishes the reviewed wheel and
   source archive to GitHub and PyPI.
