"""A crashed engine must never be servable as a quiet market, at either layer.

WHAT HAPPENED ON 2026-09-08, IN TWO PARTS.

main.py raised NameError on all 54 tickers (helpers defined below the `__main__` guard --
tests/test_main_entrypoint_ordering.py holds that shut). The per-ticker `except Exception`
filed each crash as an ordinary rejection, and then:

  1. THE ARTIFACT called itself healthy. `degraded` was derived from `scan_coverage`, which
     measures whether a two-sided CHAIN ARRIVED -- and the chains arrived perfectly, 54/54,
     ratio 1.0, no band holes. Coverage was 100% correct and entirely beside the point.

  2. THE COCKPIT quietly swapped boards. `load_board` fell back to the legacy fast scan
     whenever `qualified_trades` was empty, for any reason, so the operator got 162 ungated
     rows -- no true POP, no edge score, blocked spreads included -- under the same amber
     "provisional" strip shown when a scan merely has not run yet. Nothing on screen separated
     "the engine found nothing" from "the engine died".

Both halves are needed. Fixing only the artifact leaves the cockpit swapping boards silently;
fixing only the cockpit leaves every other consumer of `degraded` believing a dead scan.

The fixture below is the REAL artifact from that session, kept at
backups/scan_latest.BROKEN-2026-09-08.json, so these tests are anchored to what actually
happened rather than to a reconstruction of it.
"""
import json
from pathlib import Path

import pytest

import config
import vega_app as app

ROOT = Path(__file__).resolve().parent.parent
BROKEN = ROOT / "backups" / "scan_latest.BROKEN-2026-09-08.json"


def _broken():
    if not BROKEN.exists():
        pytest.skip(f"real broken artifact not kept at {BROKEN}")
    return json.loads(BROKEN.read_text(encoding="utf-8"))


def _write(tmp_path, monkeypatch, payload, market_open=False):
    """Write an artifact and PIN THE CLOCK.

    These tests are about the ERROR-share contract, not about staleness, and the two are
    entangled by `_engine_artifact`: a fixture carrying a hardcoded timestamp ages past
    ENGINE_ARTIFACT_MAX_QUOTE_AGE_MIN the day after it is written, and every one of them starts
    returning "absent" for a reason that has nothing to do with what it is testing. Three of
    these passed on 2026-09-08 and failed on 2026-09-09 for exactly that. Pinning the market
    CLOSED disables the staleness branch entirely, which is the honest way to isolate the
    dimension under test -- the staleness tests pin the clock the other way and own that
    dimension themselves.
    """
    f = tmp_path / "scan_latest.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(app, "SCAN_LATEST", f)
    monkeypatch.setattr(app, "market_status", lambda: (market_open, ""))
    return f


# ── The artifact from that day, judged by the new rule ──────────────────────
def test_the_real_broken_artifact_is_recognised_as_a_failure(tmp_path, monkeypatch):
    """The exact file that shipped `degraded: false` on 54/54 NameErrors."""
    d = _broken()
    assert d["degraded"] is False, "fixture must be the ORIGINAL, pre-fix artifact"
    assert d["scan_coverage"]["healthy"] is True, "and coverage must be the green that fooled us"

    _write(tmp_path, monkeypatch, d)
    _, state, _why = app._engine_artifact()
    assert state == "failed"


def test_the_error_share_is_derived_when_the_artifact_predates_scan_errors(tmp_path, monkeypatch):
    """Artifacts written before main.py grew `scan_errors` must still be judged, not trusted by
    default -- otherwise every board on disk from before the fix reads as healthy forever."""
    d = _broken()
    d.pop("scan_errors", None)
    _write(tmp_path, monkeypatch, d)
    got, state, _ = app._engine_artifact()
    assert state == "failed"
    se = got["scan_errors"]
    assert se["errored"] == 54 and se["considered"] == 54
    assert se["share"] == 1.0
    # One repeated defect, reported as the exception's own words rather than 54 separate rows.
    reasons = {r["reason"]: r["n"] for r in se["reasons"]}
    assert reasons == {"name '_spread_ratio' is not defined": 45,
                       "name '_quantiles' is not defined": 9}


# ── A genuine zero must be allowed to look like a zero ──────────────────────
def test_a_clean_empty_scan_is_trusted_and_does_not_fall_back(tmp_path, monkeypatch):
    """THE OTHER HALF OF THE BUG. Falling back on any empty board means a real drought can never
    be displayed -- the operator always sees ungated rows instead of the truth."""
    _write(tmp_path, monkeypatch, {
        "timestamp": "2026-09-08T15:47:30.156782-04:00",
        "qualified_trades": [], "rejected_trades": [
            {"ticker": "SPY", "category": "GATE", "reason": "credit below floor"}],
        "scan_coverage": {"healthy": True}, "degraded": False,
        "market_context": {"vix": {"current": 15.6}}, "regime": {"regime_flag": "LOW_VOL"},
    })
    _, state, _ = app._engine_artifact()
    assert state == "trust"

    b = app.load_board()
    assert b["source"] == "engine", "a trusted zero must NOT become a legacy board"
    assert b["trades"] == []
    assert "real zero" in b["note"], "and it must say so, or an empty table reads as a dead feed"


def test_a_couple_of_bad_chains_do_not_degrade_the_whole_scan(tmp_path, monkeypatch):
    """The floor exists so ordinary vendor noise is not dressed up as an engine failure."""
    rej = ([{"ticker": f"T{i}", "category": "GATE", "reason": "delta"} for i in range(50)]
           + [{"ticker": "X", "category": "ERROR", "reason": "bad chain"}])
    _write(tmp_path, monkeypatch, {
        "timestamp": "2026-09-08T15:47:30-04:00", "qualified_trades": [],
        "rejected_trades": rej, "scan_coverage": {"healthy": True}, "degraded": False})
    _, state, _ = app._engine_artifact()
    assert state == "trust", "1 error in 51 is noise, not a broken engine"


def test_the_floor_is_configurable_and_actually_read(tmp_path, monkeypatch):
    rej = ([{"ticker": f"T{i}", "category": "GATE", "reason": "delta"} for i in range(9)]
           + [{"ticker": "X", "category": "ERROR", "reason": "boom"}])
    _write(tmp_path, monkeypatch, {
        "timestamp": "2026-09-08T15:47:30-04:00", "qualified_trades": [],
        "rejected_trades": rej, "scan_coverage": {"healthy": True}, "degraded": False})
    monkeypatch.setattr(config, "SCAN_ERROR_DEGRADE_SHARE", 0.50, raising=False)
    assert app._engine_artifact()[1] == "trust"
    monkeypatch.setattr(config, "SCAN_ERROR_DEGRADE_SHARE", 0.05, raising=False)
    assert app._engine_artifact()[1] == "failed"


# ── The fallback still happens on failure, but it is BANNERED ───────────────
def test_a_failed_engine_still_shows_the_fast_scan_but_names_the_failure(tmp_path, monkeypatch):
    """The rows are the only usable data, so they stay. What changes is that the board stops
    presenting them as its own opinion."""
    _write(tmp_path, monkeypatch, _broken())
    monkeypatch.setattr(app, "_latest_candidates", lambda: ({"meta": {"stamp": "2026-09-08 14:45"},
                                                             "rows": []}, "x"))
    b = app.load_board()
    assert b["source"] == "legacy"
    assert b["engine_state"] == "failed"
    assert "ENGINE FAILED" in b["note"]
    assert "_spread_ratio" in b["note"], "the exception's own words, not a generic apology"
    assert "BLOCKED" in b["note"], "the rows include spreads the gates would have refused"


def test_the_failure_banner_is_red_and_not_the_routine_provisional_strip():
    """An amber strip the eye has learned to skip was the ONLY signal on 2026-09-08."""
    src = __import__("inspect").getsource(app.view_today)
    i = src.index('engine_state") == "failed"')
    block = src[i:i + 1200]
    assert "ENGINE FAILED" in block
    assert "240,69,90" in block, "must use the red band, not the amber provisional colour"
    assert block.index("ENGINE FAILED") < src.index("Fast scan (provisional)") - i + 1200


def test_an_absent_artifact_still_falls_back_quietly(tmp_path, monkeypatch):
    """No artifact is ordinary -- a first launch, a cleared logs dir. It must not scream."""
    monkeypatch.setattr(app, "SCAN_LATEST", tmp_path / "nope.json")
    monkeypatch.setattr(app, "_latest_candidates", lambda: ({"meta": {"stamp": "s"}, "rows": []}, "x"))
    b = app.load_board()
    assert b["engine_state"] == "absent"
    assert "ENGINE FAILED" not in b["note"] and "provisional" in b["note"]


# ── Staleness ───────────────────────────────────────────────────────────────
def test_a_stale_artifact_falls_back_only_while_the_market_is_open(tmp_path, monkeypatch):
    """After the close the session's last scan IS the board. Expiring it would make the cockpit
    drop to the fast scan every single evening."""
    _write(tmp_path, monkeypatch, {
        "timestamp": "2020-01-02T10:00:00-05:00", "qualified_trades": [],
        "rejected_trades": [], "scan_coverage": {"healthy": True}, "degraded": False})
    monkeypatch.setattr(app, "market_status", lambda: (False, ""))
    assert app._engine_artifact()[1] == "trust", "closed: an old artifact is the correct one"
    monkeypatch.setattr(app, "market_status", lambda: (True, ""))
    assert app._engine_artifact()[1] == "absent", "open: years old is not the current board"


def test_an_unreadable_artifact_does_not_take_the_cockpit_down(tmp_path, monkeypatch):
    f = tmp_path / "scan_latest.json"
    f.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(app, "SCAN_LATEST", f)
    got, state, why = app._engine_artifact()
    assert got is None and state == "absent" and "unreadable" in why
