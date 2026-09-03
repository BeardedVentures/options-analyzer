"""edge_score must be computed for rejects, not only for spreads that passed every gate.

WHY. Until 2026-09-02 edge_score was written only inside `if all(g.values())`. Measured on the
counterfactual ledger: of 2,726 rows, ZERO carried an edge_score while failing a gate --
perfect confounding, not partial range restriction. Any test of "does edge score predict
outcomes" would have run inside the survivors, where the entire range below the gates' own cut
is missing by construction, and could not speak to where MIN_EDGE_SCORE belongs.

edge_score is a SCORING function, not a gate. Scoring the rejects is what makes it falsifiable.
"""
import inspect

import vega_candidates as vc


def test_edge_score_is_written_outside_the_gate_branch():
    src = inspect.getsource(vc)
    i_assign = src.index('best["edge_score"] = _asmt.get("edge_score")')
    i_else = src.index('best["narrative"] = ("Blocked by "')
    assert i_assign > i_else, (
        "edge_score must be assigned AFTER the blocked branch closes, i.e. for both paths -- "
        "if it moves back inside `if all(g.values())` the sample becomes confounded again")


def test_the_expensive_reads_stay_conditional():
    """The cost argument that originally put scoring inside the branch is still valid for the
    parts that are actually expensive. Scoring a reject must not start pulling a DTE 5-120
    surface for every rejected spread on the board."""
    src = inspect.getsource(vc)
    assert "if not _blocked:\n                    _A2.enrich_surface(assess_ctx)" in src, (
        "enrich_surface must remain gated on the candidate having passed")


def test_the_scoring_basis_is_recorded():
    """A passing spread is scored after enrich_surface and a blocked one before it. Both term
    structure and skew scoring are OFF today so the two are identical, but a later re-enable
    would make them different measurements -- and they must not pool silently."""
    src = inspect.getsource(vc)
    assert 'best["edge_score_basis"]' in src
    assert "pre_surface" in src and "post_surface" in src


def test_enrich_surface_is_a_noop_while_term_structure_is_off():
    """Establishes the claim the cost argument rests on, rather than asserting it in a comment:
    with TERM_STRUCTURE_ENABLED False, enrich_surface returns before any network read."""
    from analysis import assessment as A
    import config
    assert getattr(config, "TERM_STRUCTURE_ENABLED") is False
    ctx = {"ticker": "TEST", "spot": 100.0}
    out = A.enrich_surface(ctx)
    assert out is ctx and not ctx.get("_surface_loaded")
