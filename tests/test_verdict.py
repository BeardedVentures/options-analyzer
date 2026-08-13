"""The trade in words a person without a finance background can act on.

Nothing here dumbs down what premium sellers actually use — VRP, the probability gap against
the market's own pricing, defended levels, directional exposure. What changes is that it gets
SAID. "VRP +8.6pp, VEGA POP 82%, edge 74, delta 0.20" is every number correct and none of them
answering the question the reader has, which is "is this good, and why".

THE RULE: every sentence is generated from a number the engine already computed. If the words
and the scores ever disagree it is because the inputs disagree — a bug worth seeing rather
than smoothing over.
"""
import pytest

from analysis import verdict as V


def _good(**over):
    c = {"ticker": "WMT", "edge_score": 74, "vrp": 8.6, "edge_pp": 7.0, "true_pop": 0.82,
         "implied_pop": 0.75, "roi": 0.43, "credit_usd": 151, "max_loss_usd": 349,
         "short": 104.0, "breakeven": 102.49, "exp": "2026-09-18", "dte": 44,
         "strat_type": "bull_put"}
    c.update(over)
    return c


# ── The grade ─────────────────────────────────────────────────────────────────────────────────

def test_the_grade_uses_the_same_0_to_10_scale_the_kpi_legend_teaches():
    """A second scale would mean a second thing to learn, and the cards already teach 0-10."""
    assert V.grade(74)["score"] == pytest.approx(7.4)
    assert 0 <= V.grade(0)["score"] <= 10 and 0 <= V.grade(200)["score"] <= 10


def test_a_negative_vrp_caps_the_grade_however_good_the_composite_looks():
    """The reader must never see an encouraging word over a trade the engine's own
    disqualifiers reject. Negative VRP means the options are cheap for how much the stock
    moves — the wrong side to sell."""
    g = V.grade(90, vrp=-5.0)
    assert g["score"] <= 3.5 and g["word"] in (V.WEAK, V.AVOID)
    assert g["capped_by"] == "negative_vrp"


def test_a_negative_pop_gap_caps_it_too():
    g = V.grade(90, pop_gap_pp=-12.0)
    assert g["score"] <= 4.5 and g["capped_by"] == "negative_pop_gap"


def test_a_clean_trade_is_not_capped():
    assert V.grade(85, vrp=8.0, pop_gap_pp=7.0)["capped_by"] is None


@pytest.mark.parametrize("score,word", [(95, V.STRONG), (72, V.GOOD), (55, V.FAIR),
                                        (40, V.WEAK), (20, V.AVOID)])
def test_the_word_matches_the_number(score, word):
    assert V.grade(score)["word"] == word


# ── The bet ───────────────────────────────────────────────────────────────────────────────────

def test_the_bet_is_stated_in_dollars_and_a_date():
    """"Short the 104 put, long the 103" is precise and useless to most readers."""
    t = V.the_bet(_good())
    assert "$151" in t and "WMT" in t and "$104.00" in t and "2026-09-18" in t
    assert "ABOVE" in t


def test_the_bet_states_the_loss_beside_the_gain():
    assert "most you can lose is $349" in V.the_bet(_good())


def test_a_bear_call_bets_the_other_way():
    t = V.the_bet(_good(strat_type="bear_call", call_short=130.0, short=None))
    assert "BELOW" in t and "$130.00" in t


def test_a_condor_is_described_as_a_range():
    t = V.the_bet(_good(strat_type="iron_condor", put_short=100.0, call_short=130.0))
    assert "between" in t and "$100.00" in t and "$130.00" in t


def test_no_jargon_survives_into_the_bet():
    t = V.the_bet(_good()).lower()
    for word in ("delta", "theta", "vrp", "pop", "spread", "strike", "premium"):
        assert word not in t, f"'{word}' is jargon the target reader does not have"


def test_an_incomplete_card_produces_no_sentence_rather_than_a_wrong_one():
    assert V.the_bet({"ticker": "X"}) is None


# ── Why, and what could go wrong ──────────────────────────────────────────────────────────────

def test_the_reasons_explain_the_edge_in_plain_terms():
    r = " ".join(V.reasons(_good()))
    assert "overpaid" in r
    assert "82% odds" in r and "market is pricing 75%" in r


def test_a_defended_level_is_named_as_one():
    r = " ".join(V.reasons(_good(_shelter="105.00")))
    assert "defended before" in r


def test_the_risk_is_stated_as_plainly_as_the_reward():
    """A page that explains the upside in plain English and leaves the downside in jargon is
    not a neutral page."""
    w = " ".join(V.watch_outs(_good()))
    assert "risk $349 to make $151" in w
    assert "2 to 1 against you" in w
    assert "Below $102.49 you start losing money" in w


def test_a_bad_trade_leads_with_the_reason_to_pass():
    w = V.watch_outs(_good(vrp=-23.9, edge_pp=-19.3))
    assert "main reason to pass" in w[0]


def test_the_pop_gap_warning_says_no_gate_blocks_it():
    """Eleven gates and none tests the edge — the reader has to know that is on them."""
    w = " ".join(V.watch_outs(_good(edge_pp=-19.3)))
    assert "No gate blocks that" in w


def test_watch_outs_are_never_empty_on_a_good_trade():
    """Whatever effort went into making the upside legible has to go into the downside."""
    assert V.watch_outs(_good())


def test_concentration_is_flagged_in_plain_words():
    w = " ".join(V.watch_outs(_good(already_in_position=True)))
    assert "doubles down" in w


# ── Wiring ────────────────────────────────────────────────────────────────────────────────────

def test_the_verdict_leads_the_drawer():
    """It answers the question the reader arrived with; everything below is the evidence."""
    import re
    import vega_app
    h = vega_app._copilot(_good(), 0, "PROVISIONAL", "", "")
    t = re.sub(r"<[^>]+>", " ", h)
    assert t.index("You are paid") < t.index("VEGA recommendation")


def test_the_rendered_verdict_carries_grade_bet_reasons_and_risks():
    import re
    import vega_app
    t = re.sub(r"<[^>]+>", " ", vega_app._verdict_html(_good()))
    assert "Good" in t and "7.4" in t
    assert "You are paid" in t
    assert "Why this one" in t and "What could go wrong" in t


def test_a_capped_grade_says_why_on_the_card():
    import re
    import vega_app
    t = re.sub(r"<[^>]+>", " ", vega_app._verdict_html(_good(vrp=-23.9, edge_pp=-19.3)))
    assert "Capped" in t and "cheap for how much this moves" in t
