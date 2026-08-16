# The simulator imports only the standard library, so it needs no install.
PYTHON ?= python3

# Anything importing the package runs through the venv, created on first use.
VENV_PYTHON ?= .venv/bin/python

# Empty when VENV_PYTHON is overridden, so an override never bootstraps.
BOOTSTRAP := $(filter .venv/bin/python,$(VENV_PYTHON))


.PHONY: setup test lint format links check demo stub-fleet wiki observe

.venv/bin/python:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -e '.[dev]'

setup: .venv/bin/python

test: $(BOOTSTRAP)
	$(VENV_PYTHON) -m pytest -q

SOURCES := src tests demo tools examples

lint: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff check $(SOURCES)

format: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff format --check $(SOURCES)

links: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/check_links.py

# The four gates `.github/workflows/ci.yml` runs, in its order. Keep the two
# in step: a `make check` that passes while CI fails is worse than no target,
# because it is trusted. `make format` is the one that drifts silently, since
# `ruff check` says nothing about formatting.
check: lint format test links

demo:
	$(PYTHON) demo/run_demo.py

wiki:
	tools/publish_wiki.sh

# The standing Prometheus + Grafana stack, provisioned from tools/observability/.
observe:
	cd tools/observability && docker compose up -d

# Six engine-protocol stubs on ports 8101-8106: the no-GPU path through the
# real router. Foreground; ^C stops the fleet.
stub-fleet: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/stub_fleet.py --base-port 8101 --instances 6 --model stub
