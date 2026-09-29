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

From the repository root, install the development tools and local checks:

1. Create the development environment when it is missing:

```bash
make setup
    ```

2. Install the pre-commit hooks:

    ```bash
.venv/bin/pre-commit install
    ```

3. Confirm the checkout passes the local checks:

    ```bash
make check
```

`constraints-dev.txt` pins the development toolchain. The equivalent manual install is:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' -c constraints-dev.txt
```

After pulling changes to `pyproject.toml` or `constraints-dev.txt`, refresh the
development dependencies from the repository root:

```bash
make sync
```

`make sync` installs the editable package and development dependencies using
`constraints-dev.txt`. It creates `.venv` if needed.

To use an existing virtual environment for `make sync`, tests, linting,
formatting, or documentation, pass its Python executable as `VENV_PYTHON`.
Replace `/path/to/venv/bin/python` with that executable in this example:

```bash
make sync VENV_PYTHON=/path/to/venv/bin/python
```

`make check` runs publication and version metadata checks, Ruff lint and formatting, mypy, unit tests, and the documentation link checker.

`make lint` and `make format` run Ruff in check mode and report lint and
formatting errors. To apply safe Ruff lint fixes and then format `src/`, `tools/`, and `tests/`,
run:

```bash
make fix
```

Review the diff before committing. If lint errors remain, the command stops
before formatting; address those errors and rerun it.

`make publication` scans the Git index for private files and private key material. To include a new file in the scan, stage it with `git add` first.

`make links` checks links and HTML targets in unfenced Markdown, heading anchors, and canonical Narwhal URLs against the checkout. `make docs-build` builds the public site in strict mode and reports navigation, asset, and rendering errors.

`make test` runs the unit suite. To select tests or change verbosity, pass unittest discovery options through `TEST_ARGS`:

```bash
make test TEST_ARGS='-k failover -v'
```

This command runs tests whose fully qualified names contain `failover`, with
verbose output. `make unit` and `make check` run the full unit suite and ignore
`TEST_ARGS`, including values supplied through the environment.

When a branch needs a remote check, start the CI suite from GitHub Actions or run `gh workflow run ci.yml --ref <branch>`, replacing `<branch>` with the branch name. Release automation also dispatches the suite for release PRs.

The suite runs `make check` on GitHub-hosted runners. On Python 3.11, 3.12, and 3.13, it runs the unit suite and installed-wheel checks. Under Python 3.12, it also builds the documentation and runs the unit suite from an extracted source distribution. The wheel checks exercise console commands, package data, and HTTP routes outside the checkout.

### Coverage and test scope

`make coverage` runs the unit suite with package and tool coverage. It writes HTML branch annotations, JSON and XML reports, and the test log to a new `runs/coverage/run-*` directory. Set `COVERAGE_ARGS='--out runs/coverage/review'` to choose the output path.

Place tests under `tests/` by component, assert a named failure or invariant, and reuse the synthetic profiles and fleets in `tests/fixtures.py`.

Fleet acceptance follows [Deploy a fleet](docs/Deploy.md) on GPU hosts, in this order:

1. Inspect devices and artifacts.
2. Start each checked engine.
3. Qualify directed links against the live cache.
4. Attest and profile those processes.
5. Run preflight and routed load through the private path.

### Documentation preview

From the repository root, install the documentation tools:

```bash
make docs-setup
```

Start the local preview:

```bash
make docs-serve
```

Open `http://127.0.0.1:8000/Narwhal/`. MkDocs runs in the foreground and reloads the
preview after documentation edits. Press Ctrl+C to stop it.

If port 8000 is in use, run `make docs-serve DOCS_PORT=8001` and open
`http://127.0.0.1:8001/Narwhal/`.

Before submitting documentation changes, run the strict build:

```bash
make docs-build
```

MkDocs clears and rebuilds `site/`. Documentation warnings fail the build.

## Behaviour changes

Add focused tests for serving and recovery changes. Validate process replacement and router failover on a deployed fleet using the [release drills](docs/operate/04-Upgrade-and-Validate.md).

Add operator settings to `FleetConfig` and document them in the annotated example, keeping secret values in environment variables. `NARWHAL_FLEET` selects the fleet configuration.

Update the docs when a config field, route, journal field, metric, CLI flag, or operator procedure changes.

## Repository layout

| Path           | Contents                                         |
| -------------- | ------------------------------------------------ |
| `src/narwhal/` | Python package and scheduling implementation     |
| `tools/`       | Operator commands and support scripts            |
| `config/`      | Shipped configuration examples                   |
| `docs/`        | Repository documentation and GitHub Pages source |
| `deploy/`      | Optional deployment infrastructure               |
| `assets/`      | Images used by documentation                     |

### Source responsibilities

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
| Serving entry point, versioned document contracts, build identity, and shared types | `cli.py`, `contracts.py`, `provenance.py`, `types.py` |

Paths in this table are relative to `src/narwhal/`. Put each change in the package that implements the operation or holds the state. For example, `serving/lifecycle.py` manages individual requests, and `runtime/lifecycle.py` manages engine drains and replacement.

Keep package initializers light and cross-package imports explicit. Use `TYPE_CHECKING` for type-only imports across the serving/runtime boundary.

Config models import scheduling definitions from `scheduling/control.py` and serving policy from `serving/policy.py`. To avoid circular imports, those two modules must stay independent of router construction.

Within those packages:

- `profiling/fitting.py` implements numerical fitting and cross-validation, `model.py` implements profile validation and capacity calculations, and `store.py` implements persistence and fleet queries.
- `engines/stream.py` decodes server-sent events (SSE) and validates token identity for serving and profiling. HTTP deadlines belong to `engines/client.py`
- `engines/validation.py` selects role-permitted KV pairs for preflight and lifecycle readmission.
- `config/serialization.py` serializes the versioned fleet configuration for file output and CLI printing.
- `observability/metrics.py` composes section renderers in a fixed order. Metric names, labels, histogram buckets, and conditional emission are part of the metrics contract.

Runtime helpers take `NarwhalRouter` explicitly. Type injected HTTP transports as `httpx.AsyncBaseTransport` and resolve their type errors. Lease renewal requires a configured `FileLease`.

Use the installed `narwhal-*` commands in deployment scripts. `python -m narwhal.cli` also starts the router. Internal Python module paths may change between releases. Each wire contract has a document schema identifier, such as `narwhal.state`.

### Working files and deployment artifacts

Keep evaluation builders, generators, deployment-specific datasets, experiment configurations, generated results, research ledgers, and paper working files outside the tracked source tree.

Store local profiles, journals, and run outputs under `runs/`, and use `config/fleet.json` or the ignored `config/fleet.*.json` pattern for working fleet configurations. Keep live engine addresses and site paths in those ignored files or reference node URLs from the ignored `.env` through the documented endpoint syntax.

Site automation owns host credentials, source distribution, network configuration and engine process launch. [Deploy Narwhal](docs/deploy/04-Qualify-Fabric.md) defines the engine-facing fabric contract that automation must establish.

## Issues

Use the [issue chooser](https://github.com/athrael-soju/Narwhal/issues/new/choose)
to report a failure, request a feature, or suggest a documentation correction.

Apply the labels that match the work:

| Label           | Use for                                      |
| --------------- | -------------------------------------------- |
| `bug`           | A confirmed failure or regression            |
| `enhancement`   | A new capability or behaviour change         |
| `documentation` | A change to operator or contributor guidance |

Combine labels when more than one applies, such as `enhancement` and `documentation` for a feature with operator guidance.

New issues receive a label from their source:

| Source                        | Label applied   |
| ----------------------------- | --------------- |
| Bug report form               | `bug`           |
| Feature request form          | `enhancement`   |
| Documentation correction form | `documentation` |
| Blank issue                   | `triage`        |

Maintainers replace `triage` with the applicable label.

Set a milestone when the issue contributes to a planned deliverable, and link prerequisite or related issues in its description.

### Milestone delivery

Use this workflow for each newly scoped milestone:

1. Create one parent issue in the milestone. Record its outcome, scope, exclusions, dependencies, and acceptance criteria.
2. Create the integration branch `milestone/<number>-<slug>` from current `main`, where `<number>` is the milestone number. Record the branch and its starting commit in the parent issue.
3. Divide the work into native GitHub sub-issues under that parent, assigned to the same milestone. Give each sub-issue its package ownership, prerequisites, acceptance criteria, and required checks. Choose the number of sub-issues to fit the work.
4. When work on a sub-issue starts and its prerequisites have merged, create `issue/<number>-<slug>` from the integration branch.
5. Open the sub-issue PR against the integration branch.
6. Before review, update dependent work from the integration branch.
7. After a sub-issue's PR merges and its acceptance criteria pass, close the sub-issue explicitly.
8. Open the parent PR from the integration branch to `main`.
9. After all sub-issues close and the milestone's acceptance criteria pass, merge the parent PR, then close the parent issue and the milestone.

Link cross-milestone prerequisites explicitly. Deliver their shared contracts through the merge to `main` of the milestone that defines them, then update dependent integration branches from `main`.

### Hardware and model selection

Keep Narwhal's scope and shared contracts hardware/GPU agnostic. Milestones, issues, and sub-issues describe required capabilities, topology, behaviour, and acceptance criteria.

Select hardware and model inputs when the corresponding work starts. Retain those selections, pinned execution inputs, and raw evidence in the private locations described under [Working files and deployment artifacts](#working-files-and-deployment-artifacts). Keep hardware and model identities out of public milestone and issue text, comments, and attachments, including copied configuration and logs.

## Pull requests

Review the diff and commit messages and run the local checks before pushing.

Push your branch to your fork and open a pull request against the target branch in `athrael-soju/Narwhal`. Sub-issue PRs target their [milestone integration branch](#milestone-delivery); parent milestone PRs and standalone changes target `main`. Maintainers use branches in the same repository. Keep the branch limited to one coherent change, and open a draft while implementation or evidence gathering continues.

Describe the problem, the resulting behaviour, and how you checked it. Link the relevant issue and, for a sub-issue, its parent. Include reproduction steps for a bug fix and identify any checks that require hardware. Keep deployment selections in private execution records and sanitize logs and configuration before attaching them.

Use a Conventional Commit prefix in the PR title, such as `fix: preserve queued requests` or `feat: add an engine dialect`. The required PR title check validates the prefix before squash merge, and the prefix determines the release impact.

Use `docs:` for documentation changes. Release Please includes each `docs:` squash commit in the Documentation changelog section. When documentation is the only change since the previous release, it proposes a patch release.

Bring the branch up to date with its PR target and run `make check` locally before merge. Before the parent milestone PR merges, bring its integration branch up to date with `main` and repeat the required checks on the combined changes. For documentation changes, also run the strict build described in [Documentation preview](#documentation-preview).

A maintainer reviews the PR and any manually requested CI results, then squash-merges it using the PR title. The `main` ruleset requires a PR and the automatic PR title check and blocks force pushes. GitHub deletes each branch at squash-merge.

Address review comments on the same branch and rerun the relevant checks after editing.

## PyPI publishing

Configure the `narwhal-inference` project on PyPI with a GitHub trusted publisher for repository `athrael-soju/Narwhal`, workflow `release.yml`, and environment `pypi`. The release job requests an OpenID Connect (OIDC) token after the GitHub release artifacts pass the package and installed-wheel checks.

Check the latest PyPI version before preparing a release. When the target version differs from the conventional commit bump, set it with a single `Release-As` footer in the preceding squash commit. Check that the generated release PR agrees on the package, citation, manifest, and changelog versions before merge. The release workflow publishes the reviewed wheel and source archive to GitHub and PyPI after that release PR merges.

## Documentation publishing

Edit the Markdown under `docs/` through a pull request and add each public page to `nav` in `mkdocs.yml`. The docs workflow builds every pull request in strict mode and publishes `main` to GitHub Pages. Run `make docs-build` locally to produce the same site under `site/`.
