"""Vendor basis describes ENTRY, and the trades that cross the boundary say so.

WHY THIS EXISTS. vendor_basis was added 2026-08-31 as the cohort key's fifth dimension and held
'unrecorded' on every row ever written, because the candidates path dropped chain_source. A
constant fifth component neither pools nor splits anything, so the key was effectively
four-dimensional while believed to be five.

Fixing that field is a cohort split, and the split has to land where it is meant to. Two
separate problems had to be solved for that:

  1. WHERE the boundary falls. All 79 executed rows opened 2026-07-09..08-10, every one before
     Robinhood was a source. Leaving them 'unrecorded' would put the boundary at "the day the
     field started being written" rather than at "the day the vendor changed".

  2. That vendor basis IS NOT CONSTANT across a trade's life. META entered pre-Robinhood and
     exited on Robinhood; its realized P&L mixes the two, in the direction that matters, since
     yfinance's wide quotes collapse natural credit toward zero. One value cannot describe that
     trade honestly, so the field now names the side it means.
"""
import pytest

from analysis import outcome_logger as ol


# ── where the boundary falls ──────────────────────────────────────────────────

def test_a_pre_robinhood_trade_is_labelled_pre_robinhood_not_unrecorded():
    """Before 2026-08-27 Robinhood was not in the fetch chain, so this is not a guess about
    which vendor served -- it is a fact about which vendor could not have."""
    assert ol.entry_vendor_basis({"opened_at": "2026-08-06T09:32:18"}) == "pre_robinhood"


def test_the_label_does_not_claim_which_vendor_served():
    """Polygon was also in the chain then. Naming yfinance would assert something the record
    cannot support -- the same 'inventing a measurement' objection that keeps gate_basis
    derived rather than stored."""
    assert ol.entry_vendor_basis({"opened_at": "2026-08-06"}) != "yfinance"


def test_a_recorded_source_always_wins_over_the_derivation():
    """The derivation is a fallback for rows that cannot answer, never an override."""
    assert ol.entry_vendor_basis(
        {"opened_at": "2026-08-06", "chain_source": "polygon"}) == "polygon"


def test_after_the_cutover_an_unstamped_row_stays_unrecorded():
    """Both vendors are live from 2026-08-27, so absence of a stamp is genuinely unknown and
    must not be derived. This is the half of the original docstring's argument that still
    holds."""
    assert ol.entry_vendor_basis({"opened_at": "2026-09-01"}) == "unrecorded"


# ── entry vs exit ─────────────────────────────────────────────────────────────

def test_exit_vendor_is_None_while_the_trade_is_open():
    assert ol.exit_vendor_basis({"status": "open", "opened_at": "2026-08-06"}) is None


def test_the_META_case_crosses_the_boundary():
    """Opened 2026-08-06 on a pre-Robinhood credit, closed 2026-09-03 on a Robinhood debit at
    $0.29. Realized P&L is entry credit minus exit debit, so that number is attributable to
    neither vendor's pricing."""
    meta = {"status": "closed", "opened_at": "2026-08-06T09:32:18",
            "closed_at": "2026-09-03T13:50:52"}
    assert ol.entry_vendor_basis(meta) == "pre_robinhood"
    assert ol.exit_vendor_basis(meta) == "unrecorded"
    assert ol.vendor_boundary_crossed(meta) is True


def test_a_trade_that_opened_AND_closed_pre_robinhood_does_not_cross():
    """Guards the crossing flag from being permanently on. Eleven of the twelve closed
    ravens_v1 rows are this shape."""
    t = {"status": "closed", "opened_at": "2026-08-06", "closed_at": "2026-08-12"}
    assert ol.vendor_boundary_crossed(t) is False


def test_an_open_trade_never_reports_as_crossed():
    """It has no exit yet. The four open positions WILL cross when they close -- that is a
    future fact, not a current one, and asserting it early would overstate the count."""
    assert ol.vendor_boundary_crossed({"status": "open", "opened_at": "2026-08-06"}) is False


def test_the_boundary_DATE_is_treated_as_a_date_not_an_instant():
    """The cutover time within 2026-08-27 is not recorded. A same-day close resolves to the
    Robinhood side, so a boundary trade is flagged as crossing when it may not have been --
    harmless in that direction, and the constant must not be reused where precision matters.
    MSFT (closed exactly on 2026-08-27) is the live instance."""
    msft = {"status": "closed", "opened_at": "2026-08-06", "closed_at": "2026-08-27"}
    assert ol.vendor_boundary_crossed(msft) is True


# ── the cohort key ────────────────────────────────────────────────────────────

def test_the_cohort_key_carries_ENTRY_vendor_only():
    """A comparability key needs one value per trade, and entry is the side that determines
    which trades exist at all. The exit side lives in vendor_boundary_crossed."""
    meta = {"status": "closed", "fill_model": "natural",
            "opened_at": "2026-08-06T09:32:18", "closed_at": "2026-09-03T13:50:52"}
    key = ol.cohort(meta)
    assert key.split("|")[-1] == "pre_robinhood"
    assert "unrecorded" not in key


def test_pre_robinhood_and_robinhood_are_different_cohorts():
    """The whole point of the dimension: yfinance's wide quotes collapse natural credit toward
    zero, which is why the move to Robinhood happened and why the prior 64-trade cohort was
    invalidated. Two trades identical in every other dimension must not pool across it."""
    base = {"fill_model": "natural", "status": "closed"}
    old = ol.cohort({**base, "opened_at": "2026-08-06"})
    new = ol.cohort({**base, "opened_at": "2026-09-01", "chain_source": "robinhood"})
    assert old != new


def test_existing_rows_move_TOGETHER_so_nothing_previously_pooled_is_split():
    """All 79 executed rows predate the cutover, so they all shift unrecorded -> pre_robinhood
    at once. If they moved independently the rename would silently re-partition history.

    Asserted on the VENDOR COMPONENT, not the whole key: 2026-07-09 and 2026-08-04 are
    gate_basis 'mid' while 2026-08-10 is 'natural' (GATE_BASIS_FIX_DATE is 2026-08-08), so
    these rows legitimately differ on a DIFFERENT dimension. Comparing whole keys here would
    fail for a reason that has nothing to do with the change under test -- which is what the
    first version of this test did.
    """
    base = {"fill_model": "natural", "status": "closed"}
    vendors = {ol.cohort({**base, "opened_at": d}).split("|")[-1]
               for d in ("2026-07-09", "2026-08-04", "2026-08-10")}
    assert vendors == {"pre_robinhood"}, "every pre-cutover row must move to the same label"
