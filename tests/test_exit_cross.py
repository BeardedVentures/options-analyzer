"""Net P/L stops assuming the exit is free.

`estimated_round_trip_cost_per_contract` is commissions only -- $2.16 -- and `set_close`
subtracted only that, so every net figure this ledger has reported assumed closing cost nothing.
Measured 2026-09-04 on the real books: $2.00 (NKE), $4.00 (NEE), $28.50 (SMH). The SMH case is
the one that matters -- both legs clear MAX_QUOTE_SPREAD_PCT comfortably (9.9%, 7.7%) and it
still costs 30% of the entry credit, because the gate is a RATIO and the cost is DOLLARS.
"""
import pytest

from analysis import outcome_logger as ol


def _book(sb, sa, lb, la):
    return {"short_bid": sb, "short_ask": sa, "long_bid": lb, "long_ask": la}


# ── the cross itself ─────────────────────────────────────────────────────────

def test_cross_is_the_half_spread_on_each_leg():
    """Closing buys back the short at its ask and sells the long at its bid, against the mark."""
    # short 1.00/2.00 (mid 1.50), long 0.10/0.30 (mid 0.20)
    # cross = (2.00-1.50) + (0.20-0.10) = 0.60 -> $60
    assert ol.exit_cross_per_contract(_book(1.00, 2.00, 0.10, 0.30)) == pytest.approx(60.0)


def test_the_real_smh_book_reproduces_the_measured_number():
    """SMH 535/530 on 2026-09-04: 3.35/3.70 short, 2.76/2.98 long -> $28.50."""
    assert ol.exit_cross_per_contract(_book(3.35, 3.70, 2.76, 2.98)) == pytest.approx(28.50)


def test_a_tight_book_costs_almost_nothing():
    """NKE 37.5/35 on the same day: 0.53/0.55 and 0.06/0.08 -> $2.00."""
    assert ol.exit_cross_per_contract(_book(0.53, 0.55, 0.06, 0.08)) == pytest.approx(2.00)


def test_a_ratio_gate_cannot_see_this():
    """The structural point, pinned: relative spread and absolute cross disagree.

    SMH clears a 35% cap on both legs and costs 14x what NKE costs. A 10% spread on a $3.50 leg
    crosses for more than a 35% spread on a $0.50 leg.
    """
    import config
    smh_rel = (3.70 - 3.35) / ((3.35 + 3.70) / 2)
    nke_rel = (0.55 - 0.53) / ((0.53 + 0.55) / 2)
    assert smh_rel < config.MAX_QUOTE_SPREAD_PCT and nke_rel < config.MAX_QUOTE_SPREAD_PCT
    assert (ol.exit_cross_per_contract(_book(3.35, 3.70, 2.76, 2.98))
            > 10 * ol.exit_cross_per_contract(_book(0.53, 0.55, 0.06, 0.08)))


@pytest.mark.parametrize("book", [
    None, {}, _book(0, 1.0, 0.1, 0.2), _book(1.0, 0.5, 0.1, 0.2),   # missing, zero, crossed
    {"short_bid": 1.0, "short_ask": 2.0},                            # incomplete
])
def test_an_unreadable_book_records_absence_not_zero(book):
    """A zero would read as "the exit was free", which is the defect being fixed."""
    assert ol.exit_cross_per_contract(book) is None


# ── what set_close does with it ──────────────────────────────────────────────

def _open_and_close(book):
    tid = ol.open_paper_trade("TEST", 100.0, 95.0, "2026-10-16",
                              entry_credit_per_share=1.00, dte=35, delta=-0.22, source="manual")
    ol.set_close(tid, 0.50, "win", "target", exit_legs=book)
    return next(r for r in ol.load_records() if r.get("id") == tid)


def test_net_subtracts_the_measured_cross_and_says_so():
    r = _open_and_close(_book(0.45, 0.55, 0.03, 0.07))     # cross = (0.05 + 0.02)*100 = $7
    assert r["realized_gross_pl_per_contract"] == pytest.approx(50.0)
    assert r["realized_exit_cross_per_contract"] == pytest.approx(7.0)
    assert r["net_basis"] == "commissions_plus_exit_cross"
    est = r["estimated_round_trip_cost_per_contract"]
    assert r["realized_net_pl_per_contract"] == pytest.approx(50.0 - est - 7.0)


def test_a_close_with_no_book_is_labelled_commissions_only():
    """Manual closes pass no book. That must be visible, not silently the old behaviour."""
    r = _open_and_close(None)
    assert r["realized_exit_cross_per_contract"] is None
    assert r["net_basis"] == "commissions_only"
    est = r["estimated_round_trip_cost_per_contract"]
    assert r["realized_net_pl_per_contract"] == pytest.approx(50.0 - est)


def test_the_raw_book_is_stored_so_the_number_can_be_rechecked():
    b = _book(0.45, 0.55, 0.03, 0.07)
    r = _open_and_close(b)
    assert r["exit_leg_quotes"] == b


def test_net_basis_makes_the_two_populations_unpoolable():
    """The whole reason for the field: a net that includes the exit cross and one that does not
    are different measurements, exactly as mid-fill and natural-fill credits are."""
    a = _open_and_close(_book(0.45, 0.55, 0.03, 0.07))
    b = _open_and_close(None)
    assert a["net_basis"] != b["net_basis"]
    assert a["realized_net_pl_per_contract"] < b["realized_net_pl_per_contract"]
