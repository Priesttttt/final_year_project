# DIMF prototype — convenience commands.
# Windows users: run the equivalent `python -m ...` commands in a venv
# (see README); the Python code itself is cross-platform.

PYTHON ?= python3
VENV ?= .venv
ifeq ($(OS),Windows_NT)
  VENV_PY := $(VENV)/Scripts/python.exe
else
  VENV_PY := $(VENV)/bin/python
endif
PY := $(VENV_PY)
ARGS := $(filter-out bench demo --,$(MAKECMDGOALS))

.PHONY: install test demo bench lint clean help
.DEFAULT_GOAL := help

install:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e . --no-deps

test:
	$(PY) -m pytest

demo:
	$(PY) scripts/run_demo.py $(ARGS)

bench:
	$(PY) scripts/run_bench.py $(ARGS)

lint:
	$(PY) -m ruff check src tests scripts

clean:
	$(PY) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['results/tmp','.pytest_cache','.ruff_cache','build','src/dimf_prototype.egg-info']]; [p.unlink(missing_ok=True) for p in []]; [p.unlink(missing_ok=True) for p in pathlib.Path('.').glob('**/__pycache__/**') if p.is_file()]; [pathlib.Path(p).unlink(missing_ok=True) for p in []]"
	@echo "cleaned build/test artefacts (data/ and results/ CSV/PNG are kept)"

# Allow `make bench -- --quick` and `make bench -- --full` (arguments after --).
--quick:
	@:
--full:
	@:
--reset:
	@:

help:
	@echo "make install   create .venv and install pinned dependencies"
	@echo "make test      run pytest"
	@echo "make demo      seed synthetic data and serve the dashboard at http://127.0.0.1:8000"
	@echo "make bench     full benchmark suite -> results/ and docs/RESULTS.md"
	@echo "make bench -- --quick   fast benchmark (< 2 minutes)"
	@echo "make lint      ruff check"
	@echo "make clean     remove temporary artefacts"
