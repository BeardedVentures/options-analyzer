"""The cockpit updating itself, and the two ways that goes wrong quietly.

Before this, the app was a snapshot: it rendered whatever was on disk when the window opened,
the scheduler that would have refreshed the board behind it was switched off, and nothing told
an open page that the numbers on it had been superseded. The operator's workaround was to close
the engine and relaunch it, which is a reliable way to see a fresh board and a terrible way to
find out that yours was stale.

Two things had to be true at once, and each has a comfortable-looking wrong answer:

  * The SCHEDULER had to come back on without re-creating the collision that got it disabled.
    The comfortable wrong answer is one master switch: it also restarts the cockpit's paper
    cycle, which lands inside the Windows task's run every time and is turned away by the lock,
    so three of four task fires do no work and the log says only "skipping this run".
  * The PAGE had to reload on a changed artifact rather than on a timer. The comfortable wrong
    answer is a meta refresh, which reloads a weekend board that cannot have moved and takes
    the page out from under a half-typed contract count.
"""
import inspect
import json
import time

import pytest

import config
import vega_app as app


# ── The page knows what it is showing ───────────────────────────────────────
def test_freshness_is_cheap_and_never_scans():
    """The page polls this every few seconds. If it ever grew a scan, a screen left open would
    quietly run the engine hundreds of times a day."""
    src = inspect.getsource(app.freshness) + inspect.getsource(app.data_stamp)
    for forbidden in ("run_scan_now", "subprocess", "get_price_data", "yfinance"):
        assert forbidden not in src, f"freshness must not reach {forbidden}"


def test_freshness_reports_the_fields_the_page_depends_on():
    f = app.freshness()
    for k in ("stamp", "scan_age_min", "market_open", "running", "refresh_min", "server_time"):
        assert k in f, k
    json.dumps(f)          # it is served as JSON; a stray datetime would 500 the poller


def test_data_stamp_moves_when_the_artifact_is_rewritten(tmp_path, monkeypatch):
    """This single number is the whole reload rule. If it did not move on a re-scan the page
    would sit stale forever, which is exactly the state this work exists to end."""
    art = tmp_path / "scan_latest.json"
    art.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "SCAN_LATEST", art)
    monkeypatch.setattr(app, "LOTTERY_LATEST", tmp_path / "missing.json")
    monkeypatch.setattr(app, "CAND_DIR", tmp_path / "none")
    first = app.data_stamp()
    assert first > 0
    import os
    os.utime(art, (time.time() + 60, time.time() + 60))
    assert app.data_stamp() > first


def test_missing_artifacts_do_not_raise():
    """A first run has no scan on disk. The nav still has to render."""
    assert isinstance(app.data_stamp(), float)


# ── The reload rule ─────────────────────────────────────────────────────────
def test_the_page_reloads_on_a_changed_stamp_and_not_on_a_timer():
    js = app.AUTOREFRESH_JS
    assert "f.stamp > stamp" in js, "the reload must be driven by the artifact, not the clock"
    assert "meta http-equiv" not in js and "http-equiv=\"refresh\"" not in js


def test_the_reload_refuses_to_interrupt_the_operator():
    """Losing a half-typed contract count is a worse failure than being one scan behind."""
    js = app.AUTOREFRESH_JS
    assert "INPUT|TEXTAREA|SELECT" in js
    assert "vdetail" in js, "an open candidate drawer must also hold the reload"
    assert "rlstop" in js, "there must be a way to keep the current view"


def test_the_render_ships_the_stamp_it_rendered_from():
    """The page compares the server's CURRENT stamp against the one it was built from. Shipping
    a stamp taken at poll time instead would make every page permanently up to date."""
    src = inspect.getsource(app.render)
    assert "__VEGA_REFRESH__" in src and "data_stamp()" in src
    assert "AUTOREFRESH_JS" in src


# ── The scheduler came back on WITHOUT the collision ────────────────────────
def test_the_board_refresh_is_on_and_the_paper_cycle_is_not():
    """The half that collided with VEGA_AutoPaper_2Weeks stays off. Paper execution and the
    re-mark loop must not be conditional on a dashboard window being open."""
    assert getattr(config, "INTRADAY_SCHEDULER_ENABLED") is True
    assert getattr(config, "INTRADAY_BOARD_REFRESH_ENABLED") is True
    assert getattr(config, "INTRADAY_PAPER_CYCLE_ENABLED") is False


def test_the_two_jobs_are_gated_separately():
    """One switch for both is how the collision comes back."""
    src = inspect.getsource(app._scheduler_loop)
    assert "INTRADAY_BOARD_REFRESH_ENABLED" in src
    assert "INTRADAY_PAPER_CYCLE_ENABLED" in src


def test_a_board_scan_defers_to_a_running_paper_cycle(tmp_path, monkeypatch):
    """auto_paper_cycle spawns main.py itself. Two of those on one artifact means the loser
    writes a scan assembled from the winner's half-written state — a board that looks fine and
    reconciles against nothing."""
    lock = tmp_path / "auto_paper_cycle.lock"
    monkeypatch.setattr(app, "PAPER_LOCK", lock)
    assert app._paper_cycle_running() is False
    lock.write_text("1234", encoding="utf-8")
    assert app._paper_cycle_running() is True


def test_a_stale_lock_stops_holding_the_board_hostage(tmp_path, monkeypatch):
    """A crashed cycle's abandoned lock must not freeze the board until someone notices."""
    import os
    lock = tmp_path / "auto_paper_cycle.lock"
    lock.write_text("1234", encoding="utf-8")
    old = time.time() - (31 * 60)
    os.utime(lock, (old, old))
    monkeypatch.setattr(app, "PAPER_LOCK", lock)
    assert app._paper_cycle_running() is False


def test_the_deferred_board_scan_is_not_stamped():
    """Stamping a deferred slot would cost the whole fifteen minutes to a check that costs
    nothing to repeat thirty seconds later."""
    src = inspect.getsource(app._scheduler_loop)
    i = src.index("_paper_cycle_running()")
    window = src[i:i + 420]
    deferred = window.split("else:")[0]
    assert '_sched_state["board_at"] = now' not in deferred


def test_the_regime_forecast_is_not_gated_on_the_equity_clock():
    """Crypto trades on weekends. Recording the daily claim only while US options are open would
    silently drop two claims in seven for BTC and ETH and bias their record toward weekdays."""
    src = inspect.getsource(app._scheduler_loop)
    fc = src[src.index("if do_fc and now.hour"):]
    assert "is_open" not in fc.split("_spawn_job")[0]


def test_the_forecast_day_is_stamped_before_the_job_runs():
    """Stamping on completion lets a slow job be launched again on the very next tick."""
    src = inspect.getsource(app._scheduler_loop)
    fc = src[src.index("if do_fc and now.hour"):]
    assert fc.index('_sched_state["forecast_on"] = now.date()') < fc.index("_spawn_job")


# ── The view is wired ───────────────────────────────────────────────────────
def test_the_forecast_view_is_reachable_and_named():
    assert "forecast" in app.VIEWS
    assert 'href="/?view=forecast"' in app.nav("today")
    assert ">Forecast<" in app.nav("forecast")


def test_the_forecast_view_degrades_rather_than_500s(monkeypatch):
    """A dead vendor must narrow this page, not take the cockpit down with it."""
    from analysis import market_forecast as mf

    def boom(*_a, **_k):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(mf, "live_board", boom)
    html = app.view_forecast()
    assert "vendor down" in html and "<h1>" in html


# ── The script itself, executed rather than grepped ─────────────────────────
def test_the_autorefresh_script_actually_behaves(tmp_path):
    """Run AUTOREFRESH_JS against a stub DOM and a virtual clock.

    The other tests in this file assert that the SOURCE mentions `f.stamp` and `activeElement`.
    That is the same coverage that let `test_price_cache_expires` pass while exercising nothing:
    it proves a line was written, never that it runs. The properties that matter here are
    behavioural and invisible in the source text — an unchanged stamp must never reload, an
    advanced one must reload exactly once after the grace period, and the countdown must HOLD
    while a field is focused or a drawer is open.

    It found a real defect on first run: after "Keep this view" the next poll's `setAge`
    overwrote the paused message, so within one poll the pill was indistinguishable from the
    armed state while auto-refresh was permanently off — a page that had stopped updating and
    no longer said so, which is the failure this whole feature exists to remove.

    NOT A BROWSER. No rendering, layout or styling, so a CSS or event-wiring bug still gets
    through. One real page open across a scan boundary is still worth doing once.
    """
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; the JS behaviour harness cannot run here")

    harness = Path(__file__).resolve().parent / "js" / "autorefresh_harness.js"
    app_py = Path(__file__).resolve().parent.parent / "vega_app.py"
    proc = subprocess.run([node, str(harness), str(app_py)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode != 2, f"harness could not read the script: {proc.stderr}"
    results = json.loads(proc.stdout)
    failed = {k: v for k, v in results.items() if not v["pass"]}
    assert not failed, "auto-refresh behaviour regressions: " + json.dumps(failed, indent=1)
    # Guard the harness itself: a stub that silently stopped exercising anything would pass.
    assert len(results) >= 9, f"expected the full scenario set, got {sorted(results)}"
