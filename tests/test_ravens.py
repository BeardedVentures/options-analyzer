"""The Ravens framework — Huginn (thought), Muninn (memory), Odin (synthesis).

Replaces a credit-multiplier stop that was not reading the market. Measured 2026-08-06 across
45 stop-outs: median hold before the stop fired was 0 days, median DTE remaining was 44, and
44 of 45 underlyings are now back above the strike. The cause was not a tight multiplier — it
was the mark. Entry natural is short_bid-long_ask and exit natural is short_ask-long_bid, so
a position pays the full bid-ask on both legs twice before it can show profit. Live GDX 76/75
quoted entry +$0.03 against exit +$0.49: a 16.3x apparent loss at t=0 with no price movement.

The load-bearing property throughout is that Muninn must never invent a probability. It is
the sober counterweight to Huginn's present-moment alarm, and a fabricated base rate would
launder a guess into something that looks like evidence.
"""
import pytest

import config
from analysis import huginn as H
from analysis import muninn as M
from analysis import odin as O


def _series(n=220, start=100.0, drift=0.15, vol=100000):
    closes = [start + drift * i for i in range(n)]
    return {"closes": closes,
            "highs": [c + 1.0 for c in closes],
            "lows": [c - 1.0 for c in closes],
            "volumes": [vol] * n,
            "current_price": closes[-1]}


def _trade(**over):
    t = {"id": "T1", "ticker": "AAA", "short_strike": 100.0, "long_strike": 95.0,
         "actual_fill_credit": 1.00, "support_level_at_entry": 110.0, "dte": 30}
    t.update(over)
    return t


def _data(**over):
    d = _series()
    d.update({"current_delta": -0.20, "mark": 1.0, "dte_remaining": 30,
              "as_of": "2026-08-06T16:00:00", "news_sentiment": None, "earnings_check": {}})
    d.update(over)
    return d


# ── The wolves ────────────────────────────────────────────────────────────────────────────────

def test_wolf_fires_on_delta_breach():
    w = H.check_wolves(_trade(), _data(current_delta=-0.62))
    assert w["delta_breach"] and w["any_wolf"]


def test_delta_below_threshold_is_not_a_wolf():
    assert not H.check_wolves(_trade(), _data(current_delta=-0.30))["any_wolf"]


def test_wolf_fires_on_gap_through_the_strike():
    d = _data()
    d["closes"] = d["closes"][:-1] + [80.0]      # gap far below the 100 strike
    d["current_price"] = 80.0
    assert H.check_wolves(_trade(), d)["gap_event"]


def test_wolf_fires_on_blocking_news_only():
    assert H.check_wolves(_trade(), _data(news_sentiment="BLOCKING"))["blocking_news"]
    assert not H.check_wolves(_trade(), _data(news_sentiment="NEGATIVE"))["any_wolf"]


def test_wolf_fires_on_new_earnings_in_window():
    d = _data(earnings_check={"in_window": True})
    assert H.check_wolves(_trade(), d)["earnings_in_window"]
    # Already known at entry is not a structural change.
    assert not H.check_wolves(_trade(earnings_in_window_at_entry=True), d)["earnings_in_window"]


def test_hard_floor_is_three_times_credit_not_one_and_a_half():
    """1.5x was the trigger that fired on spread alone. It survives only as a wide backstop."""
    assert not H.check_wolves(_trade(), _data(mark=2.0))["hard_floor_loss"]
    assert H.check_wolves(_trade(), _data(mark=3.5))["hard_floor_loss"]


def test_wolf_short_circuits_the_signal_stack():
    out = H.evaluate(_trade(), _data(current_delta=-0.70))
    assert out["thesis_status"] == "WOLF"
    assert out["signal_readings"] == {}


# ── Support ───────────────────────────────────────────────────────────────────────────────────

def test_support_intact_when_price_is_clear_above():
    r = H.read_support(_trade(support_level_at_entry=80.0), _data())
    assert r["status"] == "INTACT"


def test_one_close_below_support_is_a_breach_not_a_violation():
    """Levels get wicked constantly. Confirmation is what separates a break from noise."""
    d = _data()
    d["closes"] = d["closes"][:-1] + [95.0]
    d["current_price"] = 95.0
    assert H.read_support(_trade(support_level_at_entry=100.0), d)["status"] == "BREACH"


def test_two_closes_below_on_volume_is_a_violation():
    d = _data()
    d["closes"] = d["closes"][:-2] + [95.0, 94.0]
    d["current_price"] = 94.0
    d["volumes"] = [100000] * (len(d["closes"]) - 2) + [300000, 300000]
    assert H.read_support(_trade(support_level_at_entry=100.0), d)["status"] == "VIOLATED"


def test_missing_support_is_reported_as_unknown_not_assumed_intact():
    """Positions opened before the field existed have no recorded level; claiming INTACT
    would assert structure that was never observed."""
    r = H.read_support(_trade(support_level_at_entry=None), _data())
    assert r["status"] == "UNKNOWN" and "blind" in r["reason"]


# ── Thesis synthesis ──────────────────────────────────────────────────────────────────────────

def test_healthy_uptrend_is_intact_or_exceeded():
    assert H.evaluate(_trade(short_strike=80.0), _data())["thesis_status"] in ("INTACT", "EXCEEDED")


def test_exceeded_requires_a_strong_trend_not_just_distance():
    """Live QCOM sat 2.1 ATR above its strike while below its own 50-day. Reading that as
    'exceeded' would claim a weakening trend as a win."""
    closes = [100.0 + 0.5 * i for i in range(180)] + [190.0 - 1.2 * i for i in range(40)]
    d = _data(closes=closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
              volumes=[100000] * len(closes), current_price=closes[-1])
    out = H.evaluate(_trade(short_strike=closes[-1] - 20, support_level_at_entry=None), d)
    assert out["thesis_status"] != "EXCEEDED"


def test_price_below_the_strike_drives_pressure_or_violation():
    closes = [200.0 - 0.6 * i for i in range(220)]
    d = _data(closes=closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
              volumes=[100000] * len(closes), current_price=closes[-1])
    out = H.evaluate(_trade(short_strike=closes[-1] + 15, support_level_at_entry=closes[-1] + 20), d)
    assert out["thesis_status"] in ("UNDER_PRESSURE", "VIOLATED")


def test_reason_never_contradicts_itself():
    """The first cut emitted "Structure is holding: below the 50-day"."""
    out = H.evaluate(_trade(short_strike=80.0), _data())
    r = out["reason"].lower()
    assert not (r.startswith("structure is holding") and "below the" in r)


def test_same_chart_reads_differently_early_and_late():
    """The clock is part of the fact. A strike breached with 40 days left is a problem the
    position has time to solve; the identical chart with 5 days left is nearly settled.
    Reading structure without the clock treats those as the same thing."""
    closes = [200.0 - 0.6 * i for i in range(220)]
    base = dict(closes=closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
                volumes=[100000] * len(closes), current_price=closes[-1], current_iv=0.30)
    # Short strike ABOVE spot: for a bull put that means price has fallen through it.
    t = _trade(short_strike=closes[-1] + 12, support_level_at_entry=closes[-1] + 18,
               strategy="bull_put_spread")
    early = H.evaluate(t, _data(dte_remaining=45, **base))
    late = H.evaluate(t, _data(dte_remaining=4, **base))
    assert late["pressure_score"] > early["pressure_score"]
    assert "little time to recover" in " ".join(
        [late["reason"]] + [n for n in late.get("signal_readings", {}).get("horizon", {}).values()
                            if isinstance(n, str)]) or late["pressure_score"] > early["pressure_score"]


def test_safety_buffer_is_signed_by_which_side_was_sold():
    """(strike - price)/em is not safety — it is safety for a short CALL and danger for a
    short PUT. Reading it raw told a deeply ITM short put that time was on its side."""
    closes = [100.0] * 220
    base = dict(closes=closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
                volumes=[100000] * len(closes), current_price=100.0, current_iv=0.30)
    put = H.evaluate(_trade(short_strike=90.0, strategy="bull_put_spread",
                            support_level_at_entry=None), _data(dte_remaining=30, **base))
    call = H.evaluate(_trade(short_strike=90.0, strategy="bear_call_spread",
                             support_level_at_entry=None), _data(dte_remaining=30, **base))
    # Same strike, same chart: safe for the put seller, breached for the call seller.
    assert put["signal_readings"]["horizon"]["em_buffer"] > 0
    assert call["signal_readings"]["horizon"]["em_buffer"] < 0


def test_late_dte_with_the_strike_still_clear_eases_the_read():
    closes = [100.0 + 0.1 * i for i in range(220)]
    base = dict(closes=closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes],
                volumes=[100000] * len(closes), current_price=closes[-1], current_iv=0.25)
    out = H.evaluate(_trade(short_strike=closes[-1] - 25, support_level_at_entry=None),
                     _data(dte_remaining=3, **base))
    assert out["thesis_status"] in ("INTACT", "EXCEEDED")


def test_horizon_readings_are_exposed_for_audit():
    out = H.evaluate(_trade(short_strike=80.0), _data(dte_remaining=30, current_iv=0.3))
    h = out["signal_readings"]["horizon"]
    assert h["dte_remaining"] == 30
    assert h.get("strike_reach") in ("in_play", "likely_tested", "reachable", "out_of_reach")


def test_missing_iv_degrades_without_raising():
    out = H.evaluate(_trade(), _data(dte_remaining=30, current_iv=None))
    assert out["signal_readings"]["horizon"]["dte_remaining"] == 30
    assert out["thesis_status"] in ("INTACT", "EXCEEDED", "UNDER_PRESSURE", "VIOLATED")


def test_huginn_never_reads_pnl():
    """A position is not wrong because it is down. Conflating those is what the old stop did."""
    import inspect
    src = inspect.getsource(H.evaluate) + inspect.getsource(H.read_support)
    for banned in ("unrealized", "realized_", "credit_usd", "profit"):
        assert banned not in src


# ── Muninn ────────────────────────────────────────────────────────────────────────────────────

def _closed(outcome="win", **snap):
    s = {"dte_remaining": 30, "price_to_strike_bucket": "below",
         "support_status_at_stress": "BREACH", "vix_at_stress": 15.0}
    s.update(snap)
    # opened_at is what outcome_logger.gate_basis reads to tell a trade selected on the natural
    # credit from one selected on the mid — a record without it has an unknowable basis and is
    # excluded from the pool, so a comparable fixture has to carry a date like a real trade.
    return {"status": "closed", "outcome": outcome, "fill_model": "natural",
            "opened_at": "2026-08-09T10:00:00", "stress_snapshots": [s]}


def test_memory_is_blind_with_no_stress_snapshots():
    """The honest starting state. The brief's similarity function reads fields that have
    never been written and cannot be reconstructed after the fact."""
    r = M.compute_recovery_probability(_trade(), {"signal_readings": {}}, [], _data())
    assert r["sufficient"] is False and r["recovery_probability"] is None
    assert r["confidence"] == "insufficient_data"


def test_memory_stays_blind_below_the_minimum():
    hist = [_closed() for _ in range(3)]
    r = M.compute_recovery_probability(_trade(), {"signal_readings": {}}, hist,
                                       _data(dte_remaining=30))
    assert r["sufficient"] is False
    assert r["comparable_count"] < config.MUNINN_MIN_COMPARABLE


def test_mid_fill_history_never_contaminates_the_base_rate():
    """Mid-fill trades were entered at a price that could not be achieved; pooling them
    imports a systematic bias."""
    hist = [dict(_closed(), fill_model="mid") for _ in range(20)]
    assert M.compute_recovery_probability(_trade(), {"signal_readings": {}}, hist,
                                          _data())["sufficient"] is False


def test_mid_gate_basis_history_never_contaminates_the_base_rate():
    """The subtler contamination: filled at natural, but SELECTED on the mid credit.

    Those trades cleared a scaled credit floor on a price the desk could not get and opened
    for a fraction of it — the 5 ravens_v1 wolf-stops in the real ledger are exactly this.
    Their outcomes measure the gating leak, not the thesis, and Muninn must not learn from
    them any more than it learns from mid fills.
    """
    hist = [dict(_closed(), opened_at="2026-08-06T10:00:00") for _ in range(20)]
    assert M.compute_recovery_probability(_trade(), {"signal_readings": {}}, hist,
                                          _data())["sufficient"] is False


def test_history_with_no_open_date_is_excluded():
    """An unknowable gate basis is not a passing one — the same fail-closed logic the earnings
    gate uses. A record that cannot say how it was selected cannot be pooled with ones that can."""
    hist = [{k: v for k, v in _closed().items() if k != "opened_at"} for _ in range(20)]
    assert M.compute_recovery_probability(_trade(), {"signal_readings": {}}, hist,
                                          _data())["sufficient"] is False


def test_the_qqq_scenario_produces_a_holdable_probability():
    """Six comparable stressed positions, five of which recovered."""
    hist = [_closed("win") for _ in range(5)] + [_closed("loss")]
    hug = {"signal_readings": {"support": {"status": "BREACH"}, "strike_buffer": {"atr": 2.0}}}
    r = M.compute_recovery_probability(_trade(), hug, hist,
                                       _data(dte_remaining=30, current_price=98.0, vix=15.0))
    assert r["sufficient"] is True
    assert r["recovery_probability"] > config.ODIN_RECOVERY_THRESHOLD


def test_similarity_rewards_matching_dte_and_support():
    hug = {"signal_readings": {"support": {"status": "BREACH"}, "strike_buffer": {"atr": 2.0}}}
    d = _data(dte_remaining=30, current_price=98.0, vix=15.0)
    near = M.similarity(_trade(), _closed(), hug, d)
    far = M.similarity(_trade(), _closed(dte_remaining=90,
                                         support_status_at_stress="INTACT",
                                         price_to_strike_bucket="clear",
                                         vix_at_stress=35.0), hug, d)
    assert near > far


def test_stress_snapshot_captures_what_cannot_be_reconstructed():
    hug = {"signal_readings": {"support": {"status": "BREACH"},
                               "strike_buffer": {"atr": 2.0, "atr_buffer": -0.5}},
           "thesis_status": "UNDER_PRESSURE"}
    snap = M.record_stress_snapshot(_trade(), hug, _data(dte_remaining=21, current_price=98.0))
    assert snap["support_status_at_stress"] == "BREACH"
    assert snap["dte_remaining"] == 21
    assert snap["price_to_strike_bucket"] is not None


# ── Odin ──────────────────────────────────────────────────────────────────────────────────────

def _m(prob=None, suff=False, n=0):
    return {"sufficient": suff, "recovery_probability": prob,
            "confidence": "high" if suff else "insufficient_data", "comparable_count": n,
            "reason": "r"}


def test_wolf_always_closes_regardless_of_memory():
    o = O.synthesize({"thesis_status": "WOLF", "reason": "gap"}, _m(0.99, True, 40), _trade())
    assert o["recommendation"] == "WOLF_CLOSE"


def test_violated_with_low_recovery_closes():
    o = O.synthesize({"thesis_status": "VIOLATED", "reason": "broke"}, _m(0.20, True, 12), _trade())
    assert o["recommendation"] == "CLOSE" and o["raven_agreement"] is True


def test_violated_with_high_recovery_holds_in_tension():
    """The QQQ moment: Thought says broken, Memory says these recover. The system must say so
    rather than quietly closing."""
    o = O.synthesize({"thesis_status": "VIOLATED", "reason": "broke"}, _m(0.62, True, 14), _trade())
    assert o["recommendation"] == "HOLD_TENSION"
    assert o["raven_agreement"] is False
    assert "disagree" in o["plain_english"]


def test_violated_with_blind_memory_asks_a_human():
    o = O.synthesize({"thesis_status": "VIOLATED", "reason": "broke"}, _m(), _trade())
    assert o["recommendation"] == "MUNINN_BLIND"
    assert "human" in o["plain_english"].lower()


def test_under_pressure_does_not_close():
    for mun in (_m(), _m(0.55, True, 9)):
        o = O.synthesize({"thesis_status": "UNDER_PRESSURE", "reason": "weak"}, mun, _trade())
        assert o["recommendation"] in ("HOLD", "MUNINN_BLIND")


def test_intact_holds():
    o = O.synthesize({"thesis_status": "INTACT", "reason": "fine"}, _m(), _trade())
    assert o["recommendation"] == "HOLD"


def test_only_wolves_and_agreement_ever_close():
    """Nothing else may auto-close. Every other path surfaces for a human."""
    closing = set()
    for h in ("WOLF", "VIOLATED", "UNDER_PRESSURE", "INTACT", "EXCEEDED", "UNKNOWN"):
        for mun in (_m(), _m(0.10, True, 12), _m(0.80, True, 12)):
            rec = O.synthesize({"thesis_status": h, "reason": "r"}, mun, _trade())["recommendation"]
            if rec in ("CLOSE", "WOLF_CLOSE"):
                closing.add((h, mun["recovery_probability"]))
    assert all(h == "WOLF" or (h == "VIOLATED" and p is not None and p < 0.35)
               for h, p in closing), closing


# ── The mark-basis fix ────────────────────────────────────────────────────────────────────────

def test_close_decisions_use_the_mid_basis():
    """The finding this framework exists for: natural-in/natural-out charges the bid-ask
    twice, so the stop fired at t=0 on spread alone."""
    assert config.CLOSE_DECISION_MARK_BASIS == "mid"
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc._reprice_and_close_open)
    assert "decision_mark" in src
    assert "CLOSE_DECISION_MARK_BASIS" in src


def test_realised_pnl_still_books_the_natural_price():
    """Only the trigger changes. The record must stay honest about slippage."""
    import inspect
    import auto_paper_cycle as apc
    src = inspect.getsource(apc._ravens_or_legacy_close)
    assert "ol.set_close(r.get(\"id\"), float(mark)" in src


# ── Stratification of memory (P1-5, 2026-08-08) ───────────────────────────────────────────────

@pytest.mark.parametrize("dte, expected", [
    (0, "0-14"), (7, "0-14"), (14, "0-14"),
    (15, "15-25"), (25, "15-25"),
    (26, "26-45"), (45, "26-45"), (60, "26-45"),
    (None, None), (-1, None), ("x", None),
])
def test_dte_buckets(dte, expected):
    assert M.dte_bucket(dte) == expected


@pytest.mark.parametrize("delta, expected", [
    (-0.05, "0.00-0.10"), (-0.15, "0.10-0.20"), (0.15, "0.10-0.20"),
    (-0.22, "0.20-0.30"), (-0.30, "0.30+"), (-0.45, "0.30+"),
    (None, None), ("x", None),
])
def test_delta_buckets_ignore_sign(delta, expected):
    """A put's short delta is negative and a call's is positive; the risk is the same size.
    Bucketing on the signed value would split one stratum into two half-empty ones."""
    assert M.delta_bucket(delta) == expected


def test_snapshot_carries_its_stratum():
    hug = {"signal_readings": {"support": {"status": "BREACH"},
                               "strike_buffer": {"atr": 2.0, "atr_buffer": -0.5}},
           "thesis_status": "UNDER_PRESSURE"}
    snap = M.record_stress_snapshot(_trade(delta=-0.22), hug,
                                    _data(dte_remaining=21, current_price=98.0, vix=28.0))
    assert M.stratum(snap) == ("15-25", "0.20-0.30", "high")


def test_vol_regime_reuses_the_existing_vix_bucket():
    """One definition of high/mid/low. A second one here would let the regime label on a
    snapshot drift away from the regime term in similarity(), and the drift would be invisible
    — both would keep returning plausible strings."""
    hug = {"signal_readings": {}, "thesis_status": "UNDER_PRESSURE"}
    for vix, expected in [(30.0, "high"), (20.0, "mid"), (12.0, "low")]:
        snap = M.record_stress_snapshot(_trade(), hug, _data(vix=vix))
        assert snap["vol_regime"] == expected == M.vix_bucket(vix)


def test_vol_regime_falls_back_to_the_regime_the_trade_was_opened_in():
    """A stale regime beats no regime: it is wrong only by the holding period."""
    hug = {"signal_readings": {}, "thesis_status": "UNDER_PRESSURE"}
    snap = M.record_stress_snapshot(_trade(vix_at_entry=30.0), hug, _data(vix=None))
    assert snap["vol_regime"] == "high"


def test_an_unknowable_stratum_is_none_rather_than_a_guess():
    """Old snapshots are NOT backfilled. A cell inferred after the fact from the close record
    would be a guess about the moment of stress — and the absence of any record of that moment
    is the reason this file exists."""
    hug = {"signal_readings": {}, "thesis_status": "UNDER_PRESSURE"}
    snap = M.record_stress_snapshot({"short_strike": 100.0}, hug,
                                    _data(dte_remaining=None, vix=None))
    assert M.stratum(snap) == (None, None, None)


def test_a_minimum_cell_size_is_stated_rather_than_left_to_judgement():
    """Whoever writes the stratified read next needs a number to check against, not a feeling.
    Two of three positions recovering is not a 67% base rate."""
    assert M.MIN_STRATUM_SAMPLE >= 15


def test_stratification_does_not_disturb_what_the_snapshot_already_recorded():
    hug = {"signal_readings": {"support": {"status": "BREACH"},
                               "strike_buffer": {"atr": 2.0, "atr_buffer": -0.5}},
           "thesis_status": "UNDER_PRESSURE"}
    snap = M.record_stress_snapshot(_trade(), hug, _data(dte_remaining=21, current_price=98.0))
    assert snap["support_status_at_stress"] == "BREACH"
    assert snap["dte_remaining"] == 21
    assert snap["price_to_strike_bucket"] is not None


# ── Signals that were silently dead (2026-08-10) ──────────────────────────────────────────────

def test_support_level_at_entry_is_actually_written_at_open():
    """huginn.read_support calls itself the PRIMARY signal and opens by reading this field.

    Nothing ever wrote it, so it returned UNKNOWN — "blind to structure" — on every position
    the system has ever managed, while the value sat in the candidate's own analysis block,
    computed and discarded on the same cycle. A quarter of muninn.similarity's weight
    (support_status_at_stress) was unfillable for the same reason.
    """
    import inspect
    import auto_paper_cycle as apc
    from analysis import outcome_logger as ol

    assert "support_level_at_entry" in inspect.signature(ol.open_paper_trade).parameters
    src = inspect.getsource(apc._auto_open_from_candidates)
    assert "support_level_at_entry=" in src, "the open path must pass the level it just computed"
    assert '"shelter"' in src, "and it must come from the assessment's shelter block"


def test_huginn_reads_structure_once_the_level_is_recorded():
    trade = dict(_trade(), support_level_at_entry=99.0)
    out = H.read_support(trade, _data(current_price=101.0))
    assert out["status"] != "UNKNOWN"


def test_earnings_wolf_can_actually_fire():
    """check_wolves tests earnings_check['in_window'], and _ravens_close_check passed a
    hardcoded {} — so this wolf was unreachable and earnings risk was unmanaged at BOTH ends
    (the entry gate was simultaneously passing unknown dates open)."""
    import inspect
    import auto_paper_cycle as apc

    w = H.check_wolves(_trade(), dict(_data(), earnings_check={"in_window": True}))
    assert w["earnings_in_window"] is True and w["any_wolf"] is True

    src = inspect.getsource(apc._ravens_close_check)
    assert '"earnings_check": _earnings_check(' in src, "the wolf must get a real read"


def test_earnings_check_degrades_to_silence_not_to_a_close():
    """A calendar outage must not close open positions. Unknown at ENTRY fails closed; unknown
    while ALREADY OPEN must not realise a loss on no evidence."""
    import auto_paper_cycle as apc
    assert apc._earnings_check({"ticker": "___NOT_A_TICKER___"}, "2026-09-18") == {}


def test_the_gap_wolf_is_signed_by_which_side_was_sold():
    """A short PUT is threatened by price gapping down through the strike; a short CALL by
    price gapping up. Unsigned, this wolf sat silent through exactly the event it exists to
    catch on every call-side position — and the board is now mostly bear calls.

    Flat series so the gap is unambiguous: _series() trends upward, and mutating its last bar
    to 120 is a gap DOWN from a ~132 prior close, not the up-gap this means to test.
    """
    def _flat(last):
        closes = [100.0] * 219 + [last]
        return {"closes": closes, "highs": [c + 1 for c in closes],
                "lows": [c - 1 for c in closes], "volumes": [100000] * 220,
                "current_price": last, "current_delta": -0.20, "mark": 1.0,
                "dte_remaining": 30, "as_of": "2026-08-06T16:00:00",
                "news_sentiment": None, "earnings_check": {}}

    up, down = _flat(120.0), _flat(80.0)      # short strike is 100 in _trade()
    put = _trade(strategy="bull_put_spread")
    call = _trade(strategy="Bear Call Spread")

    assert H.check_wolves(put, down)["gap_event"] is True
    assert H.check_wolves(put, up)["gap_event"] is False
    assert H.check_wolves(call, up)["gap_event"] is True
    assert H.check_wolves(call, down)["gap_event"] is False
