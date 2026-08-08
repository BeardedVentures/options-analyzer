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
        "side": "put",
        # evaluate_gates reads liquidity and quote_spread off the LEG, not off the flattened
        # short_bid/short_ask that vega_candidates._quote_spread_ok uses. Without a leg here the
        # fixture could only ever be handed to helpers that take the flat form, and any test
        # driving the real contract end-to-end failed `liquidity` and `quote_spread` on a
        # candidate that is otherwise perfectly good — a fixture artefact indistinguishable
        # from a genuine rejection. Keep the leg consistent with the flat quotes above.
        "short_leg": {"bid": 2.00, "ask": 2.20, "mid": 2.10, "iv": 0.28,
                      "volume": 1500, "open_interest": 4200},
        "gates": make_gates(),
    }
    c.update(overrides)
    # build_candidates always emits these alongside `gates`; keep the fixture faithful to it.
    c.setdefault("gates_passed", sum(1 for v in c["gates"].values() if v))
    c.setdefault("gates_total", len(c["gates"]))
    # Derived from whatever natural credit the test asked for, so a test that overrides the
    # per-share credit cannot leave the usd/ratio forms contradicting it.
    # build_candidates assigns `pop` from pop_implied before gating; the gate reads `pop`.
    c.setdefault("pop", c.get("pop_implied"))
    _nat = float(c.get("natural_credit_per_share") or 0)
    _w = float(c.get("width") or 0)
    c.setdefault("natural_credit_usd", round(_nat * 100, 2))
    c.setdefault("natural_credit_to_width", round(_nat / _w, 3) if _w else 0)
    return c


def make_ctx(**overrides):
    """A context that clears every ctx-dependent gate in assessment.evaluate_gates.

    Pairs with make_candidate() so a test can exercise the REAL contract rather than a
    pre-built gates dict, and break exactly one thing at a time.
    """
    ctx = {
        "ticker": "TEST",
        "spot": 110.0,          # 9.1% above the 100 short strike — clears MIN_STRIKE_BUFFER_STOCK
        "earnings_days": None,  # unknown → the earnings gate defers
        "levels": {},           # empty → the shelter gate fails open by design
    }
    ctx.update(overrides)
    return ctx
