"""confidence_tier() — honest uncertainty display for edge scores."""
import pytest

from analysis.edge_calculator import confidence_tier


@pytest.mark.parametrize("n,stars,label", [
    (0,    1, "speculative"),
    (10,   1, "speculative"),
    (11,   2, "emerging"),
    (30,   2, "emerging"),
    (31,   3, "developing"),
    (60,   3, "developing"),
    (61,   4, "established"),
    (100,  4, "established"),
    (101,  5, "validated"),
    (5000, 5, "validated"),
])
def test_tier_boundaries(n, stars, label):
    t = confidence_tier(n)
    assert t["stars"] == stars
    assert t["label"] == label
    assert t["n"] == n


def test_current_sample_is_speculative():
    """The live ledger sits at ~10 closed trades — it must not read as validated."""
    assert confidence_tier(10)["stars"] == 1


def test_stars_are_monotonic():
    prev = 0
    for n in (0, 11, 31, 61, 101):
        s = confidence_tier(n)["stars"]
        assert s > prev
        prev = s


def test_bad_input_degrades_to_lowest_tier():
    for bad in (None, "abc", -5):
        t = confidence_tier(bad)
        assert t["stars"] == 1
        assert t["n"] >= 0


def test_every_tier_carries_a_note():
    for n in (0, 11, 31, 61, 101):
        assert confidence_tier(n)["note"]
