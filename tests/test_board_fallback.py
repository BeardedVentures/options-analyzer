"""What the cockpit still knows when the engine qualifies nothing.

A session where no spread clears the contract is an ordinary outcome, not a data outage. The
legacy fallback in load_board discarded market_context and regime unconditionally, so on
exactly that session Market Snapshot rendered blank and the regime banner disappeared — the
page looked like it had lost its feed rather than found no trade. Whether a SPREAD qualified
and what VIX did are different questions and the artifact holds both.
"""
import json
from datetime import datetime, timedelta

import pytest

import config
import vega_app


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    def _write(d):
        p = tmp_path / "scan_latest.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        monkeypatch.setattr(vega_app, "SCAN_LATEST", p)
        return p
    return _write


def _payload(qualified, timestamp=None):
    """A scan artifact for TODAY.

    THE TIMESTAMP MUST NOT BE HARDCODED. `_engine_artifact` ages the artifact off this field
    while the market is open, so a pinned date makes every test in this file pass on the day it
    is written and fail from the next session onward -- for a reason that has nothing to do with
    what any of them assert. The literal "2026-08-11T15:09:12" that used to sit here did exactly
    that: on 2026-09-09 it read as 41,778 minutes old, tripped the staleness branch, and
    test_the_market_read_survives_a_board_with_nothing_on_it failed with source="legacy" while
    looking like a regression in the fallback rule it was written to protect.

    Tests that ARE about staleness pass an explicit `timestamp` and own that dimension
    themselves; everything else gets a fresh one and is isolated from the clock.
    """
    ts = timestamp or datetime.now().astimezone().isoformat()
    return {"timestamp": ts, "qualified_trades": qualified,
            "market_context": {"vix": {"current": 15.4, "trend": "falling"},
                               "spy": {"day_change_pct": -0.2}, "bias": "NEUTRAL"},
            "regime": {"regime_flag": "LOW_VOL", "regime_note": "n", "trade_suppressed": False},
            "scan_summary": {"total_scanned": 1842, "total_qualified": 0,
                             "high_edge_count": 0, "exceptional_count": 0}}


def test_the_market_read_survives_a_board_with_nothing_on_it(artifact, monkeypatch):
    """CONTRACT CHANGED 2026-09-08, DELIBERATELY. This asserted `source == "legacy"`, because
    load_board used to fall back to the fast scan whenever `qualified_trades` was empty -- for
    any reason at all. That rule is what let a crashed engine (54/54 NameErrors) be served as
    162 ungated fast-scan rows under a routine amber strip, with nothing on screen separating
    "found nothing" from "died". A clean empty scan is now TRUSTED and stays on the engine path.

    The intent of this test is unchanged and is what is still asserted: a board with nothing on
    it must not lose the market read. Only the path it arrives by is different, and it now
    arrives by the better one -- with the engine's own scan_summary rather than a fallback's.
    """
    artifact(_payload([]))
    monkeypatch.setattr(vega_app, "_latest_candidates", lambda: (None, None))
    b = vega_app.load_board()
    assert b["source"] == "engine" and b["engine_state"] == "trust"
    assert b["trades"] == [], "a trusted zero shows zero rows, not a fallback's rows"
    assert b["context"].get("vix"), "market context was thrown away with the trades"
    assert b["regime"].get("regime_flag") == "LOW_VOL"
    assert b["note"], "an empty table with no explanation reads as a broken feed"


def test_the_market_read_survives_the_REAL_fallback_too(artifact, monkeypatch):
    """The coverage the test above used to provide, kept where it still applies.

    When the artifact is genuinely absent the legacy path really does run, and that path is
    where market_context and regime were once discarded. Nothing above exercises it any more,
    so it is exercised here or the original regression is uncovered.
    """
    # Stale while the market is open is the ordinary way to reach the fallback with an artifact
    # still on disk -- exactly the case where its market read must be carried across. The age is
    # stated as an explicit offset from NOW, so this test measures staleness rather than
    # measuring how long ago it was written.
    stale = (datetime.now().astimezone()
             - timedelta(minutes=10 * float(getattr(config, "ENGINE_ARTIFACT_MAX_QUOTE_AGE_MIN", 45))))
    artifact(_payload([], timestamp=stale.isoformat()))
    monkeypatch.setattr(vega_app, "_latest_candidates",
                        lambda: ({"meta": {"stamp": "s"}, "rows": []}, "x"))
    monkeypatch.setattr(vega_app, "market_status", lambda: (True, ""))
    b = vega_app.load_board()
    assert b["source"] == "legacy" and b["engine_state"] == "absent"
    assert b["context"].get("vix"), "the fallback dropped the market read again"
    assert b["regime"].get("regime_flag") == "LOW_VOL"
    assert "ENGINE FAILED" not in b["note"], "a stale artifact is not an engine failure"


def test_the_density_funnel_survives_it_too(artifact, monkeypatch):
    """0 qualified out of 1,842 scanned is the single most informative thing the page can say
    on a no-trade day, and it lived in the artifact the fallback was discarding."""
    artifact(_payload([]))
    monkeypatch.setattr(vega_app, "_latest_candidates", lambda: (None, None))
    assert vega_app.load_board()["scan_summary"].get("total_scanned") == 1842


def test_a_missing_artifact_still_degrades_quietly(monkeypatch, tmp_path):
    monkeypatch.setattr(vega_app, "SCAN_LATEST", tmp_path / "gone.json")
    monkeypatch.setattr(vega_app, "_latest_candidates", lambda: (None, None))
    b = vega_app.load_board()
    assert b["context"] == {} and b["regime"] == {}


# ── Position impact ───────────────────────────────────────────────────────────────────────────

def test_impact_says_why_a_cell_is_empty_rather_than_showing_a_dash():
    """Both of these depend on true_pop, which the fast rescan does not produce. A bare blank
    reads as "we computed this and got nothing"."""
    import re
    h = vega_app._copilot_impact({"ticker": "AAA", "max_loss_usd": 349.0, "credit_usd": 151.0})
    t = re.sub(r"<[^>]+>", " ", h)
    assert "needs the full engine" in t
    assert "no true-POP on this board" in t


# ── The tradeable table ───────────────────────────────────────────────────────────────────────

def _cand(gap, gates=11):
    return {"ticker": "IBIT", "short_strike": 33, "long_strike": 32, "dte": 38,
            "expiration": "2026-09-18", "natural_credit_usd": 16, "short_delta": -0.21,
            "true_pop": 0.66, "pop_implied": 0.66 - gap, "max_loss_usd": 83,
            "gates_passed": gates, "gates_total": 11, "gates": {}, "score": 10}


def test_passing_every_gate_does_not_hide_a_negative_edge(monkeypatch):
    """None of the eleven gates tests the edge: `pop` checks the ABSOLUTE probability against a
    floor, never true_pop minus what the market implies. So "passes every gate" and "VEGA
    rates this worse than the market" are both true at once, and the table said only the
    flattering half."""
    import re
    monkeypatch.setattr(vega_app, "_latest_candidates",
                        lambda: ({"rows": [{"ticker": "IBIT", "candidates": [_cand(-0.126)]}],
                                  "meta": {"stamp": "x"}}, None))
    t = re.sub(r"<[^>]+>", " ", vega_app._tradeable_block("IBIT"))
    assert "BELOW the market" in t
    assert "no gate tests this" in t


def test_a_clean_positive_edge_still_reads_simply(monkeypatch):
    import re
    monkeypatch.setattr(vega_app, "_latest_candidates",
                        lambda: ({"rows": [{"ticker": "IBIT", "candidates": [_cand(0.05)]}],
                                  "meta": {"stamp": "x"}}, None))
    t = re.sub(r"<[^>]+>", " ", vega_app._tradeable_block("IBIT"))
    assert "passes every gate" in t and "BELOW the market" not in t


def test_a_negative_edge_row_does_not_outrank_a_positive_one(monkeypatch):
    """A spread VEGA rates worse than the market must not sit at the top of a table headed
    "Tradeable now" purely because it clears the same number of gates."""
    import re
    bad, good = _cand(-0.126), _cand(0.04)
    good["short_strike"] = 30
    monkeypatch.setattr(vega_app, "_latest_candidates",
                        lambda: ({"rows": [{"ticker": "IBIT", "candidates": [bad, good]}],
                                  "meta": {"stamp": "x"}}, None))
    t = re.sub(r"<[^>]+>", " ", vega_app._tradeable_block("IBIT"))
    assert t.index("30/32") < t.index("33/32"), "the negative-edge row sorted first"
