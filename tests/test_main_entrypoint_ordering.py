"""Nothing a scan needs may be defined BELOW the `__main__` guard.

THE DEFECT THIS EXISTS TO PREVENT, WITH THE NUMBERS.

`_exit_cross_proj`, `_exit_cross_pct`, `_spread_ratio` and `_quantiles` were appended to the
bottom of main.py on 2026-09-04, below `if __name__ == "__main__": ... run_scan(session)`.
Running `python main.py` executes the module body top to bottom, so the guard fired and ran the
ENTIRE SCAN before those four defs were ever reached. Every ticker raised NameError inside
select_bull_put_pair, and the per-ticker `except Exception` at main.py's screening loop turned
each crash into an ordinary rejection row:

    2026-09-08 scan_latest.json
        qualified            0
        rejected            54   ALL of category ERROR
        reasons             45x "name '_spread_ratio' is not defined"
                             9x "name '_quantiles' is not defined"
        degraded         False
        scan_coverage    healthy: True, ratio 1.0, 54/54

The board read "0 qualified" for two full sessions and every health field agreed with it. It
looked exactly like a quiet market. Nothing had been evaluated at all.

WHY THE REST OF THE SUITE CANNOT CATCH THIS, AND WHY THIS TEST IS STATIC.

`import main` runs the module body with __name__ != "__main__". The guard is skipped, the body
runs to the last line, and all four helpers exist. tests/test_reason_margins.py calls
`main._spread_ratio(...)` directly and passes — it passed on every one of the 1,899 logged
NameErrors. Every import-based test in this repo is blind to script-execution order BY
CONSTRUCTION, so no amount of additional behavioural coverage would have found it. The only
observation that distinguishes the two worlds is the ORDER OF THE SOURCE, so that is what is
asserted here.

This is a whole-file structural rule, not a list of four names: the next helper appended to the
bottom of main.py fails this test on the day it is written.
"""
import ast
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _tree():
    # utf-8-sig: main.py carries a BOM, and a plain utf-8 read makes ast choke on U+FEFF.
    return ast.parse(MAIN.read_text(encoding="utf-8-sig"))


def _guard(tree):
    for n in tree.body:
        if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test):
            return n
    pytest.fail("main.py has no `if __name__ == \"__main__\"` guard")


def test_no_definition_sits_below_the_main_guard():
    """The rule, stated once and for the whole file.

    A def below the guard is invisible to `import main` and absent during `python main.py` —
    the two ways this codebase runs, disagreeing silently.
    """
    tree = _tree()
    guard = _guard(tree)
    stragglers = [
        (n.lineno, n.name) for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.lineno > guard.lineno
    ]
    assert not stragglers, (
        f"defined below the __main__ guard (line {guard.lineno}) and therefore missing while "
        f"the scan runs: {stragglers}. Move them above the guard."
    )


def test_the_guard_is_the_last_statement_in_the_file():
    """Stronger and simpler than listing what may follow: nothing may.

    Assignments and module-level calls below the guard are the same bug wearing different
    syntax — a constant defined after run_scan() is as absent as a function.
    """
    tree = _tree()
    guard = _guard(tree)
    trailing = [(n.lineno, type(n).__name__) for n in tree.body if n.lineno > guard.lineno]
    assert not trailing, f"statements follow the __main__ guard: {trailing}"


@pytest.mark.parametrize("name", ["_exit_cross_proj", "_exit_cross_pct",
                                  "_spread_ratio", "_quantiles"])
def test_the_four_that_actually_broke_are_above_the_guard(name):
    """Named explicitly as well as covered by the general rule.

    The general test would go green if someone deleted these instead of moving them. These four
    are called from inside the screening loop, so their absence is what a dead board looks like.
    """
    tree = _tree()
    guard = _guard(tree)
    hits = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == name]
    assert hits, f"{name} is gone from main.py — the screening loop still calls it"
    assert hits[0].lineno < guard.lineno, f"{name} is below the __main__ guard"


def test_every_scan_helper_is_defined_before_its_first_call():
    """Generalises past the four: any top-level name main.py CALLS before the guard must be
    DEFINED before the guard. Catches the next instance without needing to know its name."""
    tree = _tree()
    guard = _guard(tree)
    defined_after = {
        n.name: n.lineno for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.lineno > guard.lineno
    }
    called_before = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.lineno < guard.lineno:
                called_before.add(node.func.id)
    broken = sorted(called_before & set(defined_after))
    assert not broken, (
        f"called during the scan but defined after the entry point: "
        f"{ {n: defined_after[n] for n in broken} }"
    )
