"""What the cockpit still knows when the engine qualifies nothing.

A session where no spread clears the contract is an ordinary outcome, not a data outage. The
legacy fallback in load_board discarded market_context and regime unconditionally, so on
exactly that session Market Snapshot rendered blank and the regime banner disappeared — the
page looked like it had lost its feed rather than found no trade. Whether a SPREAD qualified
and what VIX did are different questions and the artifact holds both.
"""
import json

import pytest

import vega_app


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    def _write(d):
        p = tmp_path / "scan_latest.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        monkeypatch.setattr(vega_app, "SCAN_LATEST", p)
        return p
    return _write


def _payload(qualified):
    return {"timestamp": "2026-08-11T15:09:12", "qualified_trades": qualified,
            "market_context": {"vix": {"current": 15.4, "trend": "falling"},
                               "spy": {"day_change_pct": -0.2}, "bias": "NEUTRAL"},
            "regime": {"regime_flag": "LOW_VOL", "regime_note": "n", "trade_suppressed": False},
            "scan_summary": {"total_scanned": 1842, "total_qualified": 0,
                             "high_edge_count": 0, "exceptional_count": 0}}


def test_the_market_read_survives_a_board_with_nothing_on_it(artifact, monkeypatch):
    artifact(_payload([]))
    monkeypatch.setattr(vega_app, "_latest_candidates", lambda: (None, None))
    b = vega_app.load_board()
    assert b["source"] == "legacy"
    assert b["context"].get("vix"), "market context was thrown away with the trades"
    assert b["regime"].get("regime_flag") == "LOW_VOL"


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
