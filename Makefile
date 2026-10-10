PYTHON ?= python3

# Package targets use the project virtual environment.
VENV_PYTHON ?= .venv/bin/python

# A VENV_PYTHON override disables automatic `.venv` creation.
BOOTSTRAP := $(filter .venv/bin/python,$(VENV_PYTHON))

TEST_ARGS ?=
DOCS_PORT ?= 8000

.PHONY: setup sync test unit lint format types links publication versions boundary check docs-build observe
.PHONY: coverage
.PHONY: docs-setup docs-serve
.PHONY: help fix

.venv/bin/python:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -e '.[dev]' -c constraints-dev.txt

setup: .venv/bin/python

sync: $(BOOTSTRAP)
	$(VENV_PYTHON) -m pip install -e '.[dev]' -c constraints-dev.txt

help:
	@printf '%s\n' \
	  'Usage: make <target> [VARIABLE=value]' \
	  '' \
	  '  setup        Create the default development environment when missing' \
	  '  sync         Refresh constrained development dependencies' \
	  '  test         Run unit tests, with optional TEST_ARGS' \
	  '  unit         Run the full unit suite, ignoring TEST_ARGS' \
	  '  check        Run all local CI gates, including the full unit suite' \
	  '  lint         Check Python lint rules' \
	  '  format       Check Python formatting' \
	  '  fix          Apply safe Ruff lint fixes, then format Python files' \
	  '  types        Check package types with mypy' \
	  '  links        Check documentation paths and heading anchors' \
	  '  publication  Check tracked files for private data' \
	  '  boundary     Check that engine-specific code stays in backend packages' \
	  '  versions     Check publication version metadata' \
	  '  coverage     Run unit tests and write coverage reports under runs/' \
	  '  docs-setup   Install constrained documentation dependencies' \
	  '  docs-serve   Preview docs at http://127.0.0.1:8000/Narwhal/' \
	  '  docs-build   Build documentation in strict mode' \
	  '  observe      Start provisioned Prometheus and Grafana services' \
	  '  help         Show this command list without setting up an environment' \
	  '' \
	  'Overrides:' \
	  '  PYTHON         Interpreter for environment creation and metadata checks (python3)' \
	  '  VENV_PYTHON    Existing Python for package targets (.venv/bin/python)' \
	  '  TEST_ARGS      unittest discovery options for test only (empty)' \
	  '  DOCS_PORT      Loopback preview port for docs-serve (8000)' \
	  '  COVERAGE_ARGS  Arguments for the coverage runner (empty)'

test: $(BOOTSTRAP)
	$(VENV_PYTHON) -m unittest discover -s tests -t . $(TEST_ARGS)

coverage: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/maintenance/run_coverage.py $(COVERAGE_ARGS)

unit: $(BOOTSTRAP)
	$(VENV_PYTHON) -m unittest discover -s tests -t .

SOURCES := src tools tests

lint: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff check $(SOURCES)

format: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff format --check $(SOURCES)

fix: $(BOOTSTRAP)
	$(VENV_PYTHON) -m ruff check --fix $(SOURCES)
	$(VENV_PYTHON) -m ruff format $(SOURCES)

types: $(BOOTSTRAP)
	$(VENV_PYTHON) -m mypy

links: $(BOOTSTRAP)
	$(VENV_PYTHON) tools/maintenance/check_links.py

publication:
	$(PYTHON) tools/maintenance/check_publication.py

versions:
	$(PYTHON) tools/maintenance/release.py check

boundary:
	$(PYTHON) tools/maintenance/check_backend_boundary.py

# Keep this order aligned with `.github/workflows/ci.yml`.
check: publication versions boundary lint format types unit links

docs-setup: $(BOOTSTRAP)
	$(VENV_PYTHON) -m pip install -e '.[docs]' -c constraints-dev.txt

docs-serve: $(BOOTSTRAP)
	$(VENV_PYTHON) -m mkdocs serve --dev-addr 127.0.0.1:$(DOCS_PORT)

docs-build: $(BOOTSTRAP)
	$(VENV_PYTHON) -m mkdocs build --strict --clean

# Start the provisioned Prometheus and Grafana services.
observe: $(BOOTSTRAP)
	$(VENV_PYTHON) -m tools.observability.start.cli
