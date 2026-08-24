"""The test suite must not write to the ledgers the system is measured by.

It did. Running the suite on 2026-08-20 and again on 2026-08-24 appended fourteen synthetic
prediction rows to logs/vega_predictions.jsonl — a quarter of that ledger — for tickers A, B, C
and NEW at strikes 100/95, straight out of the entry-diversification fixtures. Thirteen of them
carry valid dates and are indistinguishable from real claims; they would have been graded, and
the Brier score and calibration read the ledger exists to produce would have been computed
partly from trades that never existed.

The fourteenth is the one that got noticed: expiration "2026-13-18", produced by f"2026-1{i}-18"
at i=3, which is not a real date. Three separate audits treated it as an upstream data glitch on
Citigroup — "ticker C" — and looked for a validation guard in data/fetcher.py. C was the third
letter of "ABCDEF".

These tests fail if the isolation regresses, and the second one fails by observing the real file
rather than by asserting the fixture is installed — an assertion about the fixture would pass
just as happily if the fixture stopped working.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from conftest import _PRODUCTION_LEDGERS

REPO = Path(__file__).resolve().parent.parent
REAL_PREDICTIONS = REPO / "logs" / "vega_predictions.jsonl"


def _fingerprint(p: Path):
    """Content, not size or mtime — see the ledger-dedup incident: line counts can match while
    the contents differ."""
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_every_production_ledger_is_redirected_during_tests():
    """No ledger constant may point inside the repo's own logs/ directory while tests run."""
    import importlib
    checked = 0
    for mod_name, attr, _ in _PRODUCTION_LEDGERS:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if not hasattr(mod, attr):
            continue
        checked += 1
        target = Path(str(getattr(mod, attr))).resolve()
        assert REPO / "logs" not in target.parents, (
            f"{mod_name}.{attr} points at {target} — a test writing through it would hit the "
            f"real ledger.")
    assert checked >= 4, f"only {checked} ledgers checked; the isolation list has gone stale"


def test_opening_trades_does_not_touch_the_real_predictions_ledger(tmp_path, monkeypatch, temp_ledger):
    """THE REGRESSION, observed from outside.

    Drives the same auto-open path that leaked — including the f"2026-1{i}-18" expirations whose
    i=3 case produced the malformed row — and checks the real file on disk is byte-identical
    afterwards. Fails if _no_production_ledgers is removed, narrowed, or stops being autouse.

    Deliberately does NOT assert "the fixture is installed": that would pass just as happily if
    the fixture had stopped working. It reads the actual file.
    """
    import config
    import auto_paper_cycle as apc

    before = _fingerprint(REAL_PREDICTIONS)

    monkeypatch.setattr(apc, "_log", lambda m: None)
    board_file = tmp_path / "scan_latest.json"
    monkeypatch.setattr(apc, "BOARD_FILE", board_file)
    for var in ("VEGA_MAX_NEW_PER_RUN", "VEGA_MAX_NEW_PER_DAY", "VEGA_MAX_OPEN_TOTAL"):
        monkeypatch.delenv(var, raising=False)

    board_file.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "market_context": {"vix": {"current": 15.2}},
        "qualified_trades": [
            {
                "ticker": t, "strategy": "bull_put_spread",
                "short_strike": 100.0, "long_strike": 95.0,
                "expiration": f"2026-1{i}-18",   # i=3 → "2026-13-18", the row that was noticed
                "dte": 45, "delta": -0.20, "edge_score": 50,
                "natural_credit_per_share": 1.00, "credit_per_share": 1.10,
                "fill_basis": "live", "iv_rank": 60,
                "implied_pop": 0.80, "true_pop": 0.80,
                "assessment_gates": {k: True for k in config.REQUIRED_GATES},
            }
            for i, t in enumerate("ABCDEF", start=1)
        ],
    }), encoding="utf-8")

    apc._auto_open_from_board()

    after = _fingerprint(REAL_PREDICTIONS)
    assert after == before, (
        "the auto-open path wrote to logs/vega_predictions.jsonl during a test — "
        "the exact leak that put 14 synthetic rows in the live ledger")


def test_the_guard_can_actually_fail(tmp_path):
    """The fingerprint check must be capable of returning False, or the test above is a tautology.

    Cheap, and it earns its place: the prior audit's '0.000 error, tests pass' finding was a
    metric that could not have come out any other way.
    """
    p = tmp_path / "ledger.jsonl"
    p.write_text('{"a": 1}\n', encoding="utf-8")
    before = _fingerprint(p)
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"a": 2}\n')
    assert _fingerprint(p) != before
