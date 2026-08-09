"""The daily BTC directional claim, and the two ledger defects it exposed (step 3, 2026-08-09).

The ATLAS spec proposed a new forecast table, a new outcome logger, and a 60-cycle dry run
before any BTC signal could inform a trade. None of it needed building: predictions.DIRECTION
was already defined, scored, tested — and had never been recorded once. Fully built, dormant.

Wiring a 24/7 asset into that ledger surfaced two defects that had never mattered for equities:

  1. The "flat" band was hard-coded at ±1%. BTC at 34 vol moves ±6.7% over 14 days, so ±1% is
     0.15 sigma — "flat" is unreachable and the claim degrades into a coin flip on noise while
     still reporting a hit rate that looks like skill.
  2. A failed price lookup marked a claim `unresolvable` FOREVER. One blip on the day a claim
     came due deleted it permanently, and because the sample only shrinks the loss is invisible.

Both are fixed here, and both fixes benefit the equity claims that were already being recorded.
"""
from datetime import date, timedelta

import math
import pytest

import config
from analysis import btc_forecast as bf
from analysis import predictions as P


def _candles(closes, start_day=1):
    return [{"date": f"2026-0{1 + (start_day + i) // 28}-{((start_day + i) % 28) + 1:02d}",
             "close": c, "high": c * 1.01, "low": c * 0.99, "open": c, "volume": 100.0}
            for i, c in enumerate(closes)]


def _rising(n=120, start=50000.0, step=0.004):
    return [start * (1 + step) ** i for i in range(n)]


def _falling(n=120, start=90000.0, step=0.004):
    return [start * (1 - step) ** i for i in range(n)]


BTC = {"dvol": 34.26, "btc_spot": None, "btc_rv_30d": 27.99, "btc_vrp_pp": 6.27}


def _btc(**over):
    d = dict(BTC)
    d.update(over)
    return d


# ── The flat band comes from the asset, not from a constant ───────────────────────────────────

def test_the_flat_band_is_derived_from_btcs_own_volatility():
    """Half a 14-day sigma at DVOL 34.26 is ~3.4%. The ledger's equity default of ±1% would be
    0.15 sigma for this asset — a band so narrow that 'flat' can essentially never occur, which
    turns a three-way claim into a two-way coin flip while still reporting a hit rate."""
    c = _rising()
    fc = bf.forecast(_candles(c), _btc(btc_spot=c[-1]))
    expected = 34.26 * math.sqrt(14 / 365) * 0.5
    assert fc["flat_band_pct"] == pytest.approx(expected, abs=0.05)
    assert fc["flat_band_pct"] > 3.0


def test_the_band_travels_with_the_claim_into_the_ledger(tmp_path, monkeypatch):
    """The scorer reads it from context. If it did not travel, the equity default would silently
    apply to a BTC claim and every 'flat' call would be graded against the wrong question."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    c = _rising()
    fc = bf.forecast(_candles(c), _btc(btc_spot=c[-1]))
    bf.record_daily(fc, today=date(2026, 8, 9))
    row = P.load()[0]
    assert row["context"]["flat_band_pct"] == fc["flat_band_pct"]


def test_the_scorer_honours_a_supplied_band():
    ctx = {"price_at_claim": 100.0, "expected": "flat", "flat_band_pct": 5.0}
    bars = [(date(2026, 8, 1), 104, 96, 103.0)]        # +3%: flat under a 5% band
    correct, note = P._score({"claim_type": P.DIRECTION, "context": ctx}, bars)
    assert correct is True
    assert "±5.0% flat band" in note


def test_the_scorer_still_defaults_to_one_percent_for_equities():
    """Existing equity claims carry no band and must grade exactly as before."""
    ctx = {"price_at_claim": 100.0, "expected": "up"}
    bars = [(date(2026, 8, 1), 104, 96, 103.0)]
    assert P._score({"claim_type": P.DIRECTION, "context": ctx}, bars)[0] is True


@pytest.mark.parametrize("bad", ["x", None, ""])
def test_a_junk_band_falls_back_rather_than_crashing_the_resolver(bad):
    ctx = {"price_at_claim": 100.0, "expected": "up", "flat_band_pct": bad}
    bars = [(date(2026, 8, 1), 104, 96, 103.0)]
    assert P._score({"claim_type": P.DIRECTION, "context": ctx}, bars)[0] is True


# ── A transient failure must not delete a claim ───────────────────────────────────────────────

def _open_claim(tmp_path, monkeypatch, due):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    P.record("btcfc-x", "BTC-USD", P.DIRECTION, "c", 0.55, due.isoformat(),
             context={"expected": "up", "price_at_claim": 100.0, "flat_band_pct": 3.4})


def test_a_network_blip_defers_the_claim_instead_of_burning_it(tmp_path, monkeypatch):
    """This is the one that quietly eats a validation sample. The hit rate still computes —
    over fewer claims than were made — so the loss never shows up anywhere."""
    due = date(2026, 8, 9)
    _open_claim(tmp_path, monkeypatch, due)

    def broken(*a):
        raise ConnectionError("coinbase down")

    stats = P.resolve(broken, today=due)
    assert stats["deferred"] == 1 and stats["unresolvable"] == 0
    row = P.load()[0]
    assert row["status"] == "open", "a claim must survive a bad day"
    assert row["resolve_attempts"] == 1


def test_a_deferred_claim_resolves_normally_once_data_returns(tmp_path, monkeypatch):
    due = date(2026, 8, 9)
    _open_claim(tmp_path, monkeypatch, due)
    P.resolve(lambda *a: (_ for _ in ()).throw(ConnectionError("down")), today=due)
    P.resolve(lambda *a: [(due, 110, 100, 108.0)], today=due)
    row = P.load()[0]
    assert row["status"] == "resolved" and row["correct"] is True


def test_a_claim_that_stays_unreadable_is_eventually_written_off(tmp_path, monkeypatch):
    """Retrying forever would be its own dishonesty — an open claim that can never resolve
    inflates the 'awaiting horizon' count and hides the fact that data is missing."""
    due = date(2026, 8, 9)
    _open_claim(tmp_path, monkeypatch, due)
    grace = config.PREDICTION_RESOLVE_GRACE_DAYS
    for d in range(grace + 1):
        stats = P.resolve(lambda *a: [], today=due + timedelta(days=d))
    assert P.load()[0]["status"] == "open"
    stats = P.resolve(lambda *a: [], today=due + timedelta(days=grace + 1))
    assert stats["unresolvable"] == 1
    row = P.load()[0]
    assert row["status"] == "unresolvable" and "gave up" in row["resolution_note"]


def test_an_unparseable_date_is_still_written_off_immediately(tmp_path, monkeypatch):
    """A malformed date is not transient and must not be retried forever."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    P.record("x", "BTC-USD", P.DIRECTION, "c", 0.5, "2026-08-09",
             context={"expected": "up", "price_at_claim": 100.0})
    rows = P.load()
    rows[0]["resolves_on"] = "not-a-date"
    P._write(rows)
    P.resolve(lambda *a: [], today=date(2026, 8, 9))
    assert P.load()[0]["status"] == "unresolvable"


# ── The forecast itself ───────────────────────────────────────────────────────────────────────

def test_an_uptrend_calls_up_and_a_downtrend_calls_down():
    up = _rising()
    dn = _falling()
    assert bf.forecast(_candles(up), _btc(btc_spot=up[-1]))["expected"] == "up"
    assert bf.forecast(_candles(dn), _btc(btc_spot=dn[-1]))["expected"] == "down"


def test_a_directionless_tape_calls_flat_rather_than_guessing():
    flat = [65000.0 + (i % 2) * 5 for i in range(120)]
    assert bf.forecast(_candles(flat), _btc(btc_spot=flat[-1]))["expected"] == "flat"


def test_it_abstains_rather_than_guessing_on_thin_history():
    """'none' is a first-class outcome. A forecaster that cannot decline must guess, and a guess
    logged as a claim poisons the record being built to grade it."""
    fc = bf.forecast(_candles([65000.0] * 10), _btc(btc_spot=65000.0))
    assert fc["expected"] == "none" and fc["probability"] is None
    assert "No claim made" in fc["reason"]


def test_an_abstention_writes_nothing_to_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    assert bf.record_daily(bf.forecast(_candles([65000.0] * 5), _btc()), today=date(2026, 8, 9)) is None
    assert P.load() == []


def test_confidence_is_deliberately_timid():
    """A 20/50 crossover has no business claiming 80%. The Brier score punishes overconfidence
    twice, so this model starts humble and lets the grader tell it to claim more."""
    up = _rising()
    fc = bf.forecast(_candles(up), _btc(btc_spot=up[-1]))
    assert 0.50 <= fc["probability"] <= config.BTC_FORECAST_MAX_PROB
    assert config.BTC_FORECAST_MAX_PROB <= 0.65


def test_a_high_variance_premium_trims_conviction_but_never_flips_direction():
    """Crowded protection is a CONFIDENCE signal. Treating it as directional is how a
    volatility reading gets laundered into a price call."""
    up = _rising()
    calm = bf.forecast(_candles(up), _btc(btc_spot=up[-1], btc_vrp_pp=1.0))
    crowded = bf.forecast(_candles(up), _btc(btc_spot=up[-1], btc_vrp_pp=15.0))
    assert crowded["expected"] == calm["expected"] == "up"
    assert crowded["probability"] < calm["probability"]


def test_it_still_forecasts_when_the_vol_read_is_missing():
    up = _rising()
    fc = bf.forecast(_candles(up), {"btc_spot": up[-1], "dvol": None, "btc_rv_30d": None})
    assert fc["expected"] == "up" and fc["flat_band_pct"] == 3.0


# ── It lands in the shared ledger, not a private table ────────────────────────────────────────

def test_the_claim_goes_into_vegas_own_prediction_ledger(tmp_path, monkeypatch):
    """The spec called for atlas_forecast_outcomes. A separate table would make ATLAS
    incomparable to VEGA by construction — the failure close_cohort() exists to prevent."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    up = _rising()
    pid = bf.record_daily(bf.forecast(_candles(up), _btc(btc_spot=up[-1])), today=date(2026, 8, 9))
    row = P.load()[0]
    assert pid == "btcfc-2026-08-09::direction"
    assert row["claim_type"] == P.DIRECTION
    assert row["ticker"] == "BTC-USD"
    assert row["resolves_on"] == "2026-08-23"          # 14-day horizon
    assert row["probability"] is not None, "an unpriced claim can be counted but never calibrated"


def test_the_cohort_tag_keeps_it_separate_from_trade_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    up = _rising()
    bf.record_daily(bf.forecast(_candles(up), _btc(btc_spot=up[-1])), today=date(2026, 8, 9))
    assert P.grade(cohort=bf.COHORT)["total_claims"] == 1
    assert P.grade(cohort="ravens_v1")["total_claims"] == 0


def test_one_claim_per_day_and_reruns_are_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    up = _rising()
    fc = bf.forecast(_candles(up), _btc(btc_spot=up[-1]))
    for _ in range(4):
        bf.record_daily(fc, today=date(2026, 8, 9))
    assert len(P.load()) == 1


def test_a_full_claim_can_be_graded_end_to_end(tmp_path, monkeypatch):
    """Record, resolve, grade — the whole loop the spec's 60-cycle dry run needs, using
    machinery that already existed."""
    monkeypatch.setattr(P, "PREDICTIONS_FILE", tmp_path / "p.jsonl")
    made, due = date(2026, 8, 9), date(2026, 8, 23)
    up = _rising()
    bf.record_daily(bf.forecast(_candles(up), _btc(btc_spot=up[-1])), today=made)
    px = P.load()[0]["context"]["price_at_claim"]
    P.resolve(lambda t, s, e: [(due, px * 1.2, px, px * 1.15)], today=due)   # +15%, clearly up
    g = P.grade(cohort=bf.COHORT)["by_type"]["direction"]
    assert g["n"] == 1 and g["hit_rate"] == 100.0
    assert g["brier"] is not None
    assert "not gradeable yet" in g["verdict"]        # one sample is not a record


def test_the_cycle_records_the_forecast_on_both_paths():
    """Including mark-only: the forecast is about the asset, not the book, so a day with no new
    positions is still a day the claim belongs on record."""
    import inspect
    import auto_paper_cycle as apc
    assert inspect.getsource(apc).count("_record_btc_forecast()") >= 3   # def + 2 call sites


def test_a_dead_crypto_endpoint_costs_one_sample_not_the_cycle(monkeypatch):
    import auto_paper_cycle as apc
    monkeypatch.setattr(bf, "forecast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert apc._record_btc_forecast() is None      # logged, swallowed, cycle continues
