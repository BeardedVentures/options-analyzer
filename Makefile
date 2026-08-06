# VEGA — developer tasks.
# Windows: run under Git Bash, or invoke the python commands directly.

PY ?= python

.PHONY: test test-fast lint scan mark cycle help

help:
	@echo "make test      - run the money-path test suite"
	@echo "make scan      - candidate scan, no files written"
	@echo "make mark      - reprice open positions, no new opens"
	@echo "make cycle     - full auto paper cycle (opens trades)"

# tests/ on PYTHONPATH so conftest helpers import without packaging the repo
test:
	PYTHONUTF8=1 PYTHONPATH=".:./tests" $(PY) -m pytest tests/ -v

test-fast:
	PYTHONUTF8=1 PYTHONPATH=".:./tests" $(PY) -m pytest tests/ -q

scan:
	PYTHONUTF8=1 $(PY) vega_candidates.py --no-save --no-open

mark:
	PYTHONUTF8=1 $(PY) auto_paper_cycle.py --mark-only

cycle:
	PYTHONUTF8=1 $(PY) auto_paper_cycle.py
