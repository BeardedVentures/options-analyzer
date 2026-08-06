"""Shared fixtures. Every test here is offline: no network, no real ledger, no Supabase."""
import json
import sys
from pathlib import Path

import pytest

# tests/ lives at the repo root; make the package importable without installing it.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def temp_ledger(tmp_path, monkeypatch):
    """Point outcome_logger at a throwaway ledger so tests can never touch logs/vega_outcomes.jsonl."""
    from analysis import outcome_logger as ol
    ledger = tmp_path / "vega_outcomes.jsonl"
    monkeypatch.setattr(ol, "OUTCOMES_FILE", ledger)
    return ledger


@pytest.fixture
def read_ledger(temp_ledger):
    def _read():
        if not temp_ledger.exists():
            return []
        return [json.loads(l) for l in temp_ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    return _read


def make_gates(**overrides):
    """A gates dict with every REQUIRED_GATES key passing, minus whatever the test overrides."""
    import config
    gates = {k: True for k in config.REQUIRED_GATES}
    gates.update(overrides)
    return gates


def make_candidate(**overrides):
    """A candidate that passes _candidate_passes_minimum unless a test breaks it."""
    c = {
        "ticker": "TEST",
        "short_strike": 100.0,
        "long_strike": 95.0,
        "expiration": "2026-09-18",
        "dte": 35,
        "width": 5.0,
        "credit_per_share": 1.20,
        "natural_credit_per_share": 0.90,
        "credit_usd": 120.0,
        "short_bid": 2.00, "short_ask": 2.20,
        "long_bid": 1.05, "long_ask": 1.15,
        "short_delta": -0.22,
        "pop_implied": 0.78,
        "true_pop": 0.81,
        "gates": make_gates(),
    }
    c.update(overrides)
    # build_candidates always emits these alongside `gates`; keep the fixture faithful to it.
    c.setdefault("gates_passed", sum(1 for v in c["gates"].values() if v))
    c.setdefault("gates_total", len(c["gates"]))
    return c
