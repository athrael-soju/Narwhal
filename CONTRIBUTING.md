# Contributing

```
make setup    # venv + editable install with dev extras
make check    # pytest and ruff
```

`constraints-dev.txt` freezes the dev toolchain, and `pip install -e '.[dev]' -c constraints-dev.txt` gives you the same suite byte for byte. `pre-commit install` arms the cheap gates on every commit (ruff, format, link check, mypy).

The gates are local and complete. `make check` runs `pytest`, `ruff check`, `ruff format --check`, and the link check, the same four gates CI runs. `mypy` is the only pre-commit-only gate. A green `make check` with clean hooks is the merge bar.

What gates a change:

- Tests name the Arrow paper clause or the measurement they hold the code to; a behavior change without a test that would have caught it is not done.
- Operator-tunable values live in `FleetConfig` and the annotated example config, never in module constants or environment variables. Secrets stay in the environment; the bootstrap pointers are `NARWHAL_FLEET` and the fleet tool's `NARWHAL_FLEET_PREFIX`.
- Docs live in this repository (`docs/`), published to the wiki.
- Trunk-based: short-lived branches off `main`, squash merges, and one PR may bundle several related stories.

Where things go, one rule per directory:

- `src/narwhal/`: the package, flat; every module cites the Arrow paper section it implements. No new top-level directory without a rule in this list.
- `tests/`: the suite. `tools/`: operator-facing executables. `config/`: fleet configs, example and stub tracked, `*.local.json` yours.
- `demo/`: the front door only (`make demo`). `examples/`: runnable API examples. `docs/`: operator prose. `assets/`: images only.
- `presets/`: (hardware, model) bundles. The README, `_template/`, and the shipped presets (`mi355x-kimi-k3/`, `b200-kimi-k3/`) are tracked.
- The research record (studies, ledger, eval protocol) lives in the papers repository; `docs/Benchmarking.md` documents measuring a fleet.
- root: README, LICENSE, NOTICE, CHANGELOG, SECURITY, CITATION.cff, this file, build and config files; nothing else.

Directories a fresh clone will see untracked: `runs/` (profiles and journals), `config/fleet.*.json` beyond the two shipped examples (your fleet), `local/bin/` and `build/` (scratch and artifacts). All are gitignored on purpose.

Results claims follow the experiments ledger: each run keeps its README, sanitized config and numbers with the study's artifact (*The Price of Order in Disaggregated Inference*).

## Releasing

A release is a pull request plus a tag. The release PR bumps the version in three places - `pyproject.toml`, a new CHANGELOG section, and `CITATION.cff` - and the CHANGELOG section it adds becomes the published release notes. Once the PR merges, pushing the matching `v*` tag triggers the release workflow: build, `twine check`, and a GitHub Release carrying the CHANGELOG section with the generated commit list as an appendix. The same workflow publishes to PyPI via trusted publishing, skipping files already on PyPI so a tag re-push stays green.

Versioning is `0.MINOR.PATCH`: minor when behavior or the configuration surface moves, patch for fixes and docs. Releases cut from a green `main`, at milestones rather than on a calendar.
