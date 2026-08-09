"""Per-ticker character, and the ATM-IV integrity bug it exposed (2026-08-09).

Every gate in REQUIRED_GATES is a textbook rule applied identically to 56 names. None answers
"is this a good setup FOR THIS ASSET". That gap has already produced two real defects:

  · MIN_CREDIT_USD $25 was 0.03% of spot on SPY and 0.68% on IBIT — twenty times stricter for
    no reason but share price. It kept IBIT out of the book for its entire life.
  · estimate_atm_iv's near-ATM window was a flat 3% of spot: 138 contracts on SPY, 10 on IBIT.

And the IV history those richness signals are built from was being written by TWO different
definitions of "ATM IV" — a near-ATM median, and the single contract nearest spot. The
single-contract one ran during market hours, so SPY's stored history reads 34-68% on weekdays
and 12-14% on weekends. 10% of all stored observations exceed 3x the ticker's realised vol.
"""
import json

import pandas as pd
import pytest

import config
from analysis import ticker_profile as tp
from data import technicals


def _close(vals):
    return pd.Series(vals)


def _steady(n=120, start=100.0, daily=0.01):
    """A series with a stable ~16% annualised realised vol."""
    out, px = [], start
    for i in range(n):
        px *= (1 + daily * (1 if i % 2 == 0 else -1))
        out.append(px)
    return _close(out)


# ── One definition of ATM IV ──────────────────────────────────────────────────────────────────

def test_both_engines_share_one_atm_iv_definition():
    """vega_candidates used the IV of the single contract nearest spot; technicals used a
    near-ATM median. Both wrote to the same history file, so iv_rank compared a number produced
    one way against a distribution produced two ways."""
    import inspect
    import vega_candidates as vc
    src = inspect.getsource(vc.vol_context)
    assert "estimate_atm_iv" in src
    assert "min(puts, key=" not in src, "the single-contract definition must be gone"


def test_a_single_bad_quote_cannot_decide_the_number():
    """The whole point of a median. One garbage contract at the money used to BE the reading."""
    chain = [{"strike": 100 + i, "iv": 0.20} for i in (-2, -1, 1, 2)]
    chain.append({"strike": 100.0, "iv": 9.99})        # one absurd live quote, exactly ATM
    assert technicals.estimate_atm_iv(chain, 100.0) == pytest.approx(0.20)


def test_the_window_widens_for_a_thin_chain_instead_of_giving_up():
    """At IBIT $36.80 a 3% window spans ±$1.10. A chain quoting only $2-wide strikes has nothing
    inside it, and the old code fell through to the whole-chain median."""
    chain = [{"strike": 34.0, "iv": 0.32}, {"strike": 36.0, "iv": 0.33},
             {"strike": 38.0, "iv": 0.34}, {"strike": 50.0, "iv": 0.90}]
    iv = technicals.estimate_atm_iv(chain, 36.80)
    assert iv == pytest.approx(0.33, abs=0.02), "should widen to the near strikes, not reach $50"


def test_it_never_falls_back_to_the_whole_chain_median():
    """That fallback is a smile-weighted number, not ATM IV — it ran 7 vol points high on SPY.
    A wrong number is worse than no number, because callers treat 0.0 as unknown."""
    chain = [{"strike": 500.0, "iv": 0.80}, {"strike": 600.0, "iv": 0.90}]
    assert technicals.estimate_atm_iv(chain, 100.0) == 0.0


@pytest.mark.parametrize("chain, spot", [
    ([], 100.0), (None, 100.0),
    ([{"strike": 100.0, "iv": 0.2}], 0), ([{"strike": 100.0, "iv": 0.2}], None),
    ([{"strike": 100.0}], 100.0),
])
def test_no_honest_estimate_returns_zero_not_a_guess(chain, spot):
    assert technicals.estimate_atm_iv(chain, spot) == 0.0


def test_a_sample_below_the_minimum_widens_rather_than_reporting_it():
    """Two contracts is not a median. The window must widen before it will speak."""
    chain = [{"strike": 100.0, "iv": 0.20}, {"strike": 101.0, "iv": 0.21},
             {"strike": 110.0, "iv": 0.30}]
    assert technicals.estimate_atm_iv(chain, 100.0) > 0


# ── Implausible stored observations do not vote ───────────────────────────────────────────────

def test_implausible_stored_ivs_are_dropped_against_the_tickers_own_realised_vol():
    """90% IV is normal for a meme name and impossible for TLT, so the ceiling has to be
    relative. AMD had a stored 507% against 83% realised; IWM 188% against 14%."""
    close = _steady()
    rv = float(technicals._historical_vol(close, 30))
    good = [rv * 0.9, rv * 1.1, rv * 1.5]
    bad = [rv * 5, rv * 12]
    clean, dropped = technicals._plausible_iv_samples(good + bad, close)
    assert dropped == 2 and sorted(clean) == sorted(good)


def test_the_filter_fails_open_when_realised_vol_is_unknowable():
    """A filter that cannot see must not censor."""
    clean, dropped = technicals._plausible_iv_samples([0.2, 9.9], _close([100.0]))
    assert dropped == 0 and len(clean) == 2


def test_history_files_are_never_rewritten_by_the_filter(tmp_path, monkeypatch):
    """Filtered at READ time on purpose. A dedup script that rewrote a ledger in place once
    reverted a day of closes here while the line count still looked right — the audit trail is
    worth more than the tidiness."""
    import inspect
    src = inspect.getsource(technicals._plausible_iv_samples)
    assert "write" not in src and "unlink" not in src and "remove" not in src


def test_iv_rank_refuses_to_rank_against_a_mostly_bad_history(tmp_path, monkeypatch):
    """If dropping bad quotes leaves too few points, fall back to the approximation rather than
    reporting a confident percentile computed from three survivors."""
    monkeypatch.setattr(config, "IV_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(config, "IV_HISTORY_MIN_SAMPLES", 10)
    close = _steady()
    rv = float(technicals._historical_vol(close, 30))
    hist = [{"date": f"2026-01-{i+1:02d}", "iv": rv * 8} for i in range(20)]
    (tmp_path / "TEST.json").write_text(json.dumps(hist), encoding="utf-8")
    out = technicals.calculate_iv_rank("TEST", rv, close)
    assert out["iv_rank_method"] == "APPROX"
    assert out.get("iv_history_dropped", 0) >= 20


# ── Declared knowledge ────────────────────────────────────────────────────────────────────────

def test_ibit_is_declared_as_a_bitcoin_tracker_without_earnings():
    d = tp.declared("IBIT")
    assert d["tracks"] == "BTC" and d["has_earnings"] is False
    assert d["reference_vol"] == "deribit_dvol"


def test_coin_is_declared_an_operating_company_not_a_tracker():
    """The 31-vol-point gap to BTC measures the company. Treating COIN as a BTC proxy is the
    single easiest way to misread it."""
    d = tp.declared("COIN")
    assert d["tracks"] == "crypto_beta" and d["has_earnings"] is True
    assert d["reference_vol"] is None


def test_an_unknown_ticker_says_unknown_rather_than_assuming():
    d = tp.declared("ZZZZ")
    assert d["kind"] == "unknown"
    assert d["has_earnings"] is None, "absence of a declaration is not a claim that it has none"


# ── Learned knowledge, and honesty about it ───────────────────────────────────────────────────

def test_a_ticker_with_no_history_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IV_HISTORY_DIR", str(tmp_path))
    p = tp.profile("ZZZZ", _steady())
    assert p["learned"]["confidence"] == "none"
    assert p["learned"]["sufficient"] is False
    assert "No IV history" in p["headline"]


def test_a_thin_sample_is_flagged_rather_than_reported_as_a_range(tmp_path, monkeypatch):
    """IBIT has 3 observations, all from this week. A profile that presented that as an IV range
    would launder a guess into a number — the same discipline muninn applies to recovery rates."""
    monkeypatch.setattr(config, "IV_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROFILE_MIN_OBSERVATIONS", 20)
    (tmp_path / "IBIT.json").write_text(
        json.dumps([{"date": f"2026-08-0{i+7}", "iv": 0.33} for i in range(3)]), encoding="utf-8")
    p = tp.profile("IBIT", _steady())
    assert p["learned"]["iv_observations"] == 3
    assert p["learned"]["sufficient"] is False
    assert any("IV rank is unreliable" in c for c in p["cautions"])


def test_a_deep_sample_reports_a_real_range(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IV_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROFILE_MIN_OBSERVATIONS", 20)
    close = _steady()
    rv = float(technicals._historical_vol(close, 30))
    (tmp_path / "AAA.json").write_text(
        json.dumps([{"date": f"2026-01-{i+1:02d}", "iv": rv * (0.9 + i * 0.01)}
                    for i in range(25)]), encoding="utf-8")
    p = tp.profile("AAA", close)
    assert p["learned"]["sufficient"] is True
    assert p["learned"]["confidence"] == "usable"
    assert p["learned"]["iv_median_pct"] is not None


# ── Cautions are where the generic rule and the asset disagree ────────────────────────────────

def test_no_earnings_is_reported_as_an_inapplicable_rule_not_as_safety():
    """The earnings gate passing on IBIT means nothing — it can never bind. Reading that as
    evidence of safety is exactly the textbook-over-asset error this module exists to catch."""
    p = tp.profile("IBIT", _steady())
    assert any("can never bind" in c for c in p["cautions"])


def test_ibit_is_told_to_borrow_btcs_volatility_reference():
    p = tp.profile("IBIT", _steady())
    assert any("DVOL" in c for c in p["cautions"])


def test_a_poorly_quoted_chain_is_called_out(monkeypatch):
    monkeypatch.setattr(tp, "_chain_quality", lambda t: 0.42)
    p = tp.profile("AMD", _steady())
    assert any("42% quotable" in c for c in p["cautions"])


def test_a_healthy_chain_raises_no_caution(monkeypatch):
    monkeypatch.setattr(tp, "_chain_quality", lambda t: 0.95)
    p = tp.profile("SPY", _steady())
    assert not any("quotable" in c for c in p["cautions"])


# ── It is advisory, and it reaches the operator ───────────────────────────────────────────────

def test_the_profile_never_enters_the_gates_dict():
    """Same guarantee as the BTC signal: not being in the dict means it cannot block a trade,
    whatever it says. Turning any of this into a rule is a separate, deliberate decision."""
    import inspect
    from analysis import assessment as A
    assert "profile" not in inspect.getsource(A.evaluate_gates)
    assert '"profile"' in inspect.getsource(A.assess)


def test_the_cautions_reach_the_narrative():
    """A caution nobody reads is not expertise. The narrative is the sentence the cockpit shows
    and the auto-trader logs."""
    import inspect
    from analysis import assessment as A
    assert "cautions" in inspect.getsource(A._narrate)


def test_a_broken_profile_cannot_fail_an_assessment(monkeypatch):
    from analysis import assessment as A
    monkeypatch.setattr(tp, "profile", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert A._ticker_profile({"ticker": "SPY"}) == {}
