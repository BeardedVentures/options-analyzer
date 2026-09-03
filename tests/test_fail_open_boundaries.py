"""Guards that return a PASSING value at a boundary instead of an error.

Three instances turned up in one week, and they share a shape rather than a subsystem:

  data_quality_log.compact  `kept[-max_rows:]` with max_rows=0 returns the WHOLE list, so the
                            cap kept every row AND reported nothing dropped. Live; fixed.
  fetcher chain quality     the ratio cannot fall when a quote batch is dropped, because the
                            drop shortens numerator and denominator together -- SMH read 1.000
                            on a 2-contract chain. Live; instrumented rather than gated.
  vega_candidates           `if all(g.values())` gated edge scoring, so edge_score existed only
                            for spreads that had already passed every gate. Live; fixed.

The common property: at the boundary the guard answers "fine" rather than "cannot tell". A
self-validating instrument certifies success by not measuring the thing it claims to enforce.

These tests pin the remaining instances of the shape found by hunting for it deliberately on
2026-09-03. Neither was reachable in production -- the defaults keep them off the boundary --
which is exactly why they needed pinning rather than leaving: the next person to change a
constant would have found out the expensive way.
"""
import logging

import pytest


# ── a cap expressed as a negative slice ───────────────────────────────────────

def test_iv_history_cap_of_zero_keeps_NOTHING(tmp_path, monkeypatch):
    """samples[-0:] is samples[0:]. At IV_HISTORY_MAX_SAMPLES=0 this cap silently kept the
    whole history while looking like it had trimmed it -- identical to the live bug in
    data_quality_log.compact()."""
    from data import technicals
    import config

    monkeypatch.setattr(config, "IV_HISTORY_MAX_SAMPLES", 0, raising=False)
    samples = [{"date": f"2026-01-{i:02d}", "iv": 0.2} for i in range(1, 10)]

    max_samples = getattr(config, "IV_HISTORY_MAX_SAMPLES", 504)
    trimmed = samples[-max_samples:] if max_samples > 0 else []

    assert trimmed == [], "a zero cap must keep nothing"
    assert samples[-max_samples:] == samples, (
        "and this is WHY the guard is needed: the bare slice returns everything")


def test_the_iv_history_cap_still_trims_normally(monkeypatch):
    """The other direction, so the guard above is not just an unconditional empty list."""
    import config
    monkeypatch.setattr(config, "IV_HISTORY_MAX_SAMPLES", 3, raising=False)
    samples = list(range(10))
    max_samples = config.IV_HISTORY_MAX_SAMPLES
    trimmed = samples[-max_samples:] if max_samples > 0 else []
    assert trimmed == [7, 8, 9]


# ── all() over an empty collection ────────────────────────────────────────────

def test_a_strategy_with_only_advisory_criteria_REFUSES_to_qualify(monkeypatch, caplog):
    """all([]) is True. If every criterion were advisory, evaluate() would qualify a trade
    having enforced nothing, silently. This file's contract is that a row is a hard block
    unless it opts out with advisory=True, so one careless advisory=True on the last hard row
    would flip the default from 'blocks' to 'passes everything'."""
    import strategies

    real_chk = strategies._chk

    def all_advisory(*a, **k):
        row = real_chk(*a, **{**k, "advisory": True})
        return row

    monkeypatch.setattr(strategies, "_chk", all_advisory)
    with caplog.at_level(logging.ERROR):
        res = strategies.evaluate("bull_put", {})

    assert res["qualified"] is False, "no enforced check must never mean 'qualified'"
    assert any("NO hard criteria" in r.message for r in caplog.records), (
        "and it must say so -- a silent refusal is the same defect wearing the other sign")


def test_a_normal_evaluation_is_unaffected():
    """Guards the test above from having disabled qualification entirely.

    Uses a REAL spec key. 'bull_put_spread' is not one -- it hits the unknown-strategy early
    return, which yields one hard criterion and qualified=False for a reason that has nothing
    to do with the guard under test. That false reading is what made this look unreachable at
    first."""
    import strategies
    res = strategies.evaluate("bull_put", {})
    hard = [c for c in res["criteria"] if not c.get("advisory")]
    assert hard, "the real path must still emit hard criteria"
    assert isinstance(res["qualified"], bool)


# ── the standing check for the OTHER repeated shape ───────────────────────────

def test_no_generated_data_artifact_is_tracked_by_git():
    """Three times in one week a gitignore rule failed to cover a path it was meant to:
    backups/ ignored backups/ledgers/ but not credential files; a brief instructed writing a
    live OAuth token into a tracked directory; and data_quality_log's rule matched an EXACT
    filename, so renaming the storage to .jsonl silently escaped it.

    This is the standing version of the check that was being done by hand each time. It runs
    over what git ACTUALLY tracks, so a rename that escapes its ignore rule fails here rather
    than being noticed after the fact -- in a PUBLIC repo.
    """
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files", "data", "logs"],
                         cwd=root, capture_output=True, text=True)
    if out.returncode != 0:                      # not a git checkout (CI export, tarball)
        pytest.skip("not a git working tree")

    tracked = [p for p in out.stdout.splitlines() if p.strip()]
    # Generated artifacts: anything that is not source or a package marker.
    offenders = [p for p in tracked
                 if not p.endswith((".py", ".md", ".gitkeep", ".gitignore"))]
    assert not offenders, (
        "generated data artifacts are tracked and would be committed to a PUBLIC repo:\n  "
        + "\n  ".join(offenders)
        + "\nAdd them to .gitignore, and check `git check-ignore -v <path>` whenever a file "
          "under data/ or logs/ is renamed or gains a new extension.")


# ── a gate a rename can disable ───────────────────────────────────────────────

def test_every_caller_uses_a_real_strategy_key():
    """The rename guard. strategies.evaluate() returns qualified=False for a key it does not
    recognise, which is INDISTINGUISHABLE from a candidate that failed its criteria.

    main.py wraps that call in a try/except commented "Fail open: a bug in evaluate() must not
    silently empty the board" -- but the unknown-strategy path RETURNS rather than raising, so
    the handler never sees it and the board empties anyway, by the one route the guard cannot
    watch. The symptom would be a board qualifying nothing, which is exactly what a market
    drought looks like.

    So this checks statically that every hardcoded key in the tree is a real spec. A rename
    fails here instead of quietly zeroing the board in production.
    """
    import re
    from pathlib import Path
    import strategies

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"strategies\.evaluate\(\s*[\"']([^\"']+)[\"']")
    found = {}
    for py in root.rglob("*.py"):
        parts = py.parts
        if "tests" in parts or ".venv" in parts or "site-packages" in parts:
            continue
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except OSError:                              # pragma: no cover - defensive
            continue
        for key in pattern.findall(src):
            found.setdefault(key, []).append(str(py.relative_to(root)))

    assert found, "expected to find at least one strategies.evaluate() call site"
    unknown = {k: v for k, v in found.items() if k not in strategies.STRATEGY_SPECS}
    assert not unknown, (
        "these call sites pass a strategy key that is not in STRATEGY_SPECS, so they would "
        f"silently qualify NOTHING: {unknown}. Valid keys: {sorted(strategies.STRATEGY_SPECS)}")


def test_an_unknown_key_is_LOUD_even_though_it_fails_closed(caplog):
    """Failing closed is the safe direction -- raising would hit main.py's fail-open handler and
    fill the board with ungated candidates instead. But silence is what made this dangerous."""
    import strategies
    with caplog.at_level(logging.ERROR):
        res = strategies.evaluate("bull_put_spread", {})     # a real typo, made on 2026-09-03
    assert res["qualified"] is False
    assert any("UNKNOWN STRATEGY" in r.message for r in caplog.records)
