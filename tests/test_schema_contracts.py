"""Producer/consumer field-name contracts for the dicts that cross module boundaries.

Five bugs on 2026-08-10 had one shape: a consumer read a string-literal key that its producer
never wrote. Each read None forever and nothing failed.

    assess()          read tech["vrp"]        vol_context emitted "vrp_pp"
    auto_paper_cycle  read ctx["vrp"]         same producer, same miss
    auto_paper_cycle  read ctx["vix"]         vol_context never emitted vix at all
    the review plan   read ctx["earnings_date"]   the field is "earnings_days"
    the earnings gate got 999 from days_until_earnings where it expected None

The reachability test in test_candidate_gates.py catches an ORPHANED IMPLEMENTATION — a
function nothing calls. It cannot catch a live call reading a key that does not exist, because
`.get()` on a missing key is a successful call returning None. That is what this module covers.

`row["ctx"]` is the dangerous case and the reason this is worth the machinery: vol_context's
output is SERIALISED into the candidates snapshot and read back by a different module on a
later run. It is a schema crossing a process boundary with nothing checking it, which is
exactly the situation a type system would cover and this codebase does not have.
"""
import ast
import inspect
import textwrap

import pytest

import auto_paper_cycle as apc
import vega_candidates as vc
from analysis import assessment as A


# ── Producer conformance ──────────────────────────────────────────────────────────────────────

def test_vol_context_emits_exactly_its_declared_keys():
    """Including the early-return paths. A key that appears only when the happy path completes
    is indistinguishable, to a consumer, from a key that was renamed."""
    assert set(vc.vol_context("TEST", [], 100.0)) == vc.VOL_CONTEXT_KEYS
    assert set(vc.vol_context("TEST", None, None)) == vc.VOL_CONTEXT_KEYS


def test_vrp_carries_the_same_name_as_the_other_producer():
    """assessment.load_context accepts `tech` from EITHER vol_context or
    technicals.calculate_all, and a consumer cannot know which one filled the dict it holds.
    Where both describe the same quantity in the same unit — volatility points — they must
    agree on the name, or every consumer needs a fallback chain and one of them will forget."""
    from data import technicals

    assert "vrp" in vc.VOL_CONTEXT_KEYS
    empty = technicals.calculate_all(None, "TEST")
    assert "vrp" in empty, "technicals.calculate_all no longer emits 'vrp'"


def test_legacy_keys_are_not_re_emitted():
    """vrp_pp may be READ (snapshots on disk still carry it) but must never be written again."""
    assert not (vc.VOL_CONTEXT_KEYS & vc.VOL_CONTEXT_LEGACY_KEYS)


# ── Consumer conformance ──────────────────────────────────────────────────────────────────────

def _literal_key_reads(fn_or_src, aliases):
    """Every string-literal key read off a dict identified as one of `aliases`.

    Accepts a function or a source string, so the scanner can be exercised against a known-bad
    snippet without that snippet having to exist as a real function somewhere.

    Resolves the two shapes that actually appear in this codebase:

        _ctx.get("vrp")                     a bare name
        (row.get("ctx") or {}).get("vrp")   the or-{} idiom used for nested reads

    Anything it cannot resolve is skipped rather than guessed at, so this reports no false
    positives — it is a net with known holes, not a proof.
    """
    def _is_target(node):
        # _ctx.get(...) / ctx["..."] on a bare name
        if isinstance(node, ast.Name):
            return node.id in aliases
        # (row.get("ctx") or {}).get(...)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            head = node.values[0]
            if (isinstance(head, ast.Call)
                    and isinstance(head.func, ast.Attribute) and head.func.attr == "get"
                    and head.args and isinstance(head.args[0], ast.Constant)):
                return head.args[0].value in aliases
        return False

    found = set()
    src = fn_or_src if isinstance(fn_or_src, str) else inspect.getsource(fn_or_src)
    # dedent, NOT cleandoc: cleandoc re-indents every line after the first and mangles a
    # function body. Methods arrive indented; module-level functions do not.
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and _is_target(node.func.value)
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and _is_target(node.value)
              and isinstance(node.slice, ast.Constant)
              and isinstance(node.slice.value, str)):
            found.add(node.slice.value)
    return found


# (function, variable names that hold a vol_context dict)
VOL_CONTEXT_CONSUMERS = [
    (apc._auto_open_from_candidates, {"_ctx", "ctx"}),
    (apc._pick_new_trades, {"ctx"}),
    (apc._entry_state, {"ctx"}),
]


@pytest.mark.parametrize("fn, aliases", VOL_CONTEXT_CONSUMERS,
                         ids=lambda x: getattr(x, "__name__", ""))
def test_vol_context_consumers_read_only_keys_the_producer_writes(fn, aliases):
    read = _literal_key_reads(fn, aliases)
    allowed = vc.VOL_CONTEXT_KEYS | vc.VOL_CONTEXT_LEGACY_KEYS
    unknown = read - allowed
    assert not unknown, (
        f"{fn.__name__} reads {sorted(unknown)} off the vol_context dict, which "
        f"vega_candidates.vol_context never writes. It will be None on every trade and "
        f"nothing will fail. Emit it from the producer, or read the name the producer uses "
        f"(declared: {sorted(vc.VOL_CONTEXT_KEYS)}).")


def test_the_scanner_actually_catches_the_bug_it_was_written_for():
    """A guard that cannot fail is not a guard.

    Reproduces the exact defect: reading "vix" off the row context. vol_context has never
    emitted a VIX — it lives in the snapshot's meta block — so vix_at_entry was null on all 79
    real trades, which in turn silently defeated muninn's documented fallback to the entry VIX.
    """
    src = (
        "def consumer(row):\n"
        "    _ctx = row.get('ctx') or {}\n"
        "    return _ctx.get('vix'), (row.get('ctx') or {}).get('iv_rank')\n"
    )
    read = _literal_key_reads(src, {"_ctx", "ctx"})
    assert read == {"vix", "iv_rank"}, "the scanner must resolve BOTH access shapes"
    assert read - (vc.VOL_CONTEXT_KEYS | vc.VOL_CONTEXT_LEGACY_KEYS) == {"vix"}


# ── The assessment context ────────────────────────────────────────────────────────────────────

# Keys load_context puts on the ctx that evaluate_gates and assess read back. Declared here
# rather than in assessment.py because they are asserted, not iterated — the point is that a
# reader inventing "earnings_date" (as the 2026-08-10 improvement plan did) fails a test
# instead of silently gating on None.
ASSESS_CONTEXT_KEYS = frozenset({
    "ticker", "errors", "price_data", "spot", "highs", "lows", "closes", "volumes",
    "puts", "calls", "tech", "sentiment", "earnings_days", "has_earnings", "earnings_source",
    "levels", "structure", "term_structure", "skew", "atm_iv", "rv", "vix",
    "_surface_loaded",
})


def test_load_context_emits_the_keys_its_readers_expect():
    ctx = A.load_context("TEST", price_data=None, puts=[], tech={})
    unknown = set(ctx) - ASSESS_CONTEXT_KEYS
    assert not unknown, f"load_context grew undeclared keys: {sorted(unknown)}"


@pytest.mark.parametrize("fn", [A._earnings_clear, A._shelter_ok, A.evaluate_gates],
                         ids=lambda f: f.__name__)
def test_gate_helpers_read_only_context_keys_that_exist(fn):
    read = _literal_key_reads(fn, {"ctx"})
    unknown = read - ASSESS_CONTEXT_KEYS
    assert not unknown, (
        f"{fn.__name__} reads {sorted(unknown)} off the assessment context, which "
        f"load_context never sets. A gate reading a key that does not exist sees None on "
        f"every candidate — which is how the improvement plan's proposed "
        f"`ctx.get('earnings_date')` would have blocked every trade forever.")


def test_the_earnings_field_name_that_broke_the_plan():
    """The plan's fix patched `earnings_date`. The field is `earnings_days`. Applying it would
    have read a key that is never set, seen None on every candidate, and — with the
    fail-closed change also in that patch — blocked every trade on every cycle."""
    assert "earnings_days" in ASSESS_CONTEXT_KEYS
    assert "earnings_date" not in ASSESS_CONTEXT_KEYS
    assert "earnings_days" in _literal_key_reads(A._earnings_clear, {"ctx"})
