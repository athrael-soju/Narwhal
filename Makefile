PYTHON ?= python3

# Package targets use the project virtual environment.
VENV_PYTHON ?= .venv/bin/python

# A VENV_PYTHON override disables automatic `.venv` creation.
BOOTSTRAP := $(filter .venv/bin/python,$(VENV_PYTHON))


.PHONY: setup test unit lint format types links publication versions check stub-fleet wiki wiki-check observe integration
.PHONY: coverage

.venv/bin/python:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -e '.[dev]' -c constraints-dev.txt

setup: .venv/bin/python

test: unit integration

coverage: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/run_coverage.py $(COVERAGE_ARGS)

unit: $(BOOTSTRAP)
	$(VENV_PYTHON) -m unittest discover -s tools/tests

SOURCES := src tools

lint: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff check $(SOURCES)

format: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff format --check $(SOURCES)

types: $(BOOTSTRAP)
	$(VENV_PYTHON) -m mypy

links: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/check_links.py

publication:
	$(PYTHON) tools/check_publication.py

versions:
	$(PYTHON) tools/release.py check

# Keep this order aligned with `.github/workflows/ci.yml`.
check: publication versions lint format types unit integration links

wiki:
	tools/publish_wiki.sh

wiki-check:
	tools/publish_wiki.sh --dry-run

# Start the provisioned Prometheus and Grafana services.
observe: $(BOOTSTRAP)
	$(VENV_PYTHON) -m tools.observability.start

# Run six engine-protocol stubs in the foreground, with a matching local fleet.
STUB_BASE_PORT ?= 8101
STUB_FLEET ?= runs/stub/$(STUB_BASE_PORT)/fleet.json
stub-fleet: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/stub_fleet.py --base-port $(STUB_BASE_PORT) --instances 6 --model stub --write-fleet "$(STUB_FLEET)"

# Real router processes with CPU engine stubs; no fleet access.
integration: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/drills/ha_failover.py
	$(VENV_PYTHON) tools/drills/lifecycle_restart.py
	$(VENV_PYTHON) tools/drills/lifecycle_restart.py --restart-policy whole_wave
