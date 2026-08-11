"""The CLV no-edge baseline.

CLV grades a spread against what it should be worth from the clock alone. The baseline used
to be `entry - |short_theta| * days`, and that made the metric unwinnable: short_theta is the
SHORT LEG's decay, while a credit spread's net decay is the short leg's minus the long leg's,
several times smaller. Charging the full short-leg theta overstated decay 3-10x, then clamped
at zero — and once the baseline is zero, CLV = -mark, negative for every positive mark.

Measured on the live ledger before the fix: 16% of records carried a baseline of exactly 0.00
and the beat rate was 9.3% over 75 records. That was measuring the formula, not the trades.

These tests pin the property that makes the metric worth keeping: the baseline must be able to
come out on either side.
"""
import pytest

import clv_tracker as C


def _rec(**over):
    r = {"id": "t1", "ticker": "AAA", "strategy": "bull_put_spread", "status": "closed",
         "modeled_credit_per_share": 1.71, "actual_fill_credit": 1.71,
         "exit_price": 0.40, "dte": 38, "short_theta": -0.4931,
         "opened_at": "2026-07-13T12:00:00", "closed_at": "2026-07-29T12:00:00",
         "marked_at": "2026-07-29T12:00:00"}
    r.update(over)
    return r


def test_the_baseline_scales_with_time_remaining_not_short_leg_theta():
    """16 of 38 days elapsed leaves 22/38 of the term, so the zero-edge mark is 22/38 of the
    credit. The old formula charged 0.4931 x 16 = 7.89 against a 1.71 credit and floored."""
    out = C.clv_for_record(_rec())
    assert out["theta_expected"] == pytest.approx(1.71 * (22 / 38), abs=0.01)


def test_the_baseline_is_not_floored_to_zero_by_a_long_hold():
    """A 16-day hold on a 38-day spread is ordinary, not expiry."""
    assert C.clv_for_record(_rec())["theta_expected"] > 0


def test_clv_can_come_out_positive():
    """The property the old formula destroyed. A mark well below the time-decay baseline is a
    win against the clock and has to be able to read as one."""
    out = C.clv_for_record(_rec(exit_price=0.10))
    assert out["clv"] > 0 and out["beat"] is True


def test_clv_can_come_out_negative():
    out = C.clv_for_record(_rec(exit_price=4.25))
    assert out["clv"] < 0 and out["beat"] is False


def test_a_short_leg_with_enormous_theta_no_longer_decides_the_grade():
    """META's short-leg theta alone would have consumed its whole credit in 3.5 days. The
    grade must not move when that number changes, because it is not the spread's decay."""
    a = C.clv_for_record(_rec(short_theta=-0.05))
    b = C.clv_for_record(_rec(short_theta=-9.99))
    assert a["theta_expected"] == b["theta_expected"]


def test_a_record_with_no_term_is_ungradeable_not_zero_graded():
    """An ungradeable trade must not become a graded one because the grader had a fallback."""
    assert C.clv_for_record(_rec(dte=None)) is None
    assert C.clv_for_record(_rec(dte=0)) is None


def test_an_explicitly_logged_baseline_still_wins():
    out = C.clv_for_record(_rec(theta_expected_mark=1.00))
    assert out["theta_expected"] == 1.00


def test_a_hold_past_expiry_floors_at_zero_but_only_then():
    out = C.clv_for_record(_rec(closed_at="2026-09-30T12:00:00",
                                marked_at="2026-09-30T12:00:00"))
    assert out["theta_expected"] == 0.0


def test_a_spread_that_expired_worthless_counts_as_a_beat():
    """The maximum win. At expiry the baseline is 0 by construction, so `clv > 0` scored the
    best available outcome as a miss — nothing beats capturing the entire credit."""
    out = C.clv_for_record(_rec(exit_price=0.0, closed_at="2026-09-30T12:00:00",
                                marked_at="2026-09-30T12:00:00"))
    assert out["clv"] == 0.0
    assert out["beat"] is True


def test_a_worthless_mark_before_expiry_is_also_a_beat():
    out = C.clv_for_record(_rec(exit_price=0.0))
    assert out["beat"] is True
