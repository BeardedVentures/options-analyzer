/* Executes the cockpit's REAL auto-refresh script against a stub DOM and a virtual clock.
 *
 * WHY THIS EXISTS. The reload behaviour was covered only by substring assertions over
 * AUTOREFRESH_JS's source -- "the file mentions f.stamp", "the file mentions activeElement".
 * That is the same shape of coverage that let test_price_cache_expires pass while exercising
 * nothing: it asserts a line was WRITTEN, never that it RUNS. The three properties that
 * actually matter are behavioural, and none of them are visible in the source text:
 *
 *   1. an unchanged stamp must never reload  (a timer-driven page reloads a weekend board)
 *   2. an advanced stamp must reload, once, after the grace countdown
 *   3. the countdown must HOLD while the operator is typing or has a drawer open
 *
 * THIS IS NOT A BROWSER. It does not render, lay out or style anything, and it cannot catch a
 * CSS, paint or event-wiring bug. It executes the script's control flow, which is the part
 * that was untested. A real page open across a scan boundary is still worth doing once.
 *
 * Usage: node autorefresh_harness.js <path to vega_app.py>   -> prints JSON, exits 0/1
 */
const fs = require('fs');

const src = fs.readFileSync(process.argv[2], 'utf8');
const m = src.match(/AUTOREFRESH_JS = """([\s\S]*?)"""/);
if (!m) { console.error('could not extract AUTOREFRESH_JS from vega_app.py'); process.exit(2); }
const code = m[1].replace(/<\/?script>/g, '');

function makeEnv(cfg, freshnessSeq) {
  const state = {
    reloads: 0, appended: [], ids: {}, activeElement: null, querySelectorHit: null,
    pillText: null, pillClass: null, timers: [], nextId: 1, fetches: 0,
  };
  const pill = {
    get textContent() { return state.pillText; },
    set textContent(v) { state.pillText = v; },
    get className() { return state.pillClass; },
    set className(v) { state.pillClass = v; },
  };

  function mkEl() {
    const el = {
      tagName: 'DIV', className: '', _html: '', onclick: null, textContent: null,
      remove() { state.appended = state.appended.filter((x) => x !== el); },
    };
    Object.defineProperty(el, 'innerHTML', {
      get() { return el._html; },
      set(v) {
        el._html = v;
        // Register the ids the script reaches for by id straight after setting innerHTML.
        const found = v.match(/id="([^"]+)"/g) || [];
        for (const raw of found) {
          const id = raw.slice(4, -1);
          state.ids[id] = { id, textContent: null, onclick: null };
        }
      },
    });
    return el;
  }

  const document = {
    get activeElement() { return state.activeElement; },
    getElementById(id) { return id === 'freshpill' ? pill : (state.ids[id] || null); },
    querySelector(sel) {
      if (state.querySelectorHit && sel.indexOf(state.querySelectorHit) !== -1) return {};
      return null;
    },
    createElement() { return mkEl(); },
    body: { appendChild(el) { state.appended.push(el); } },
  };

  const setIntervalStub = (fn, ms) => {
    const id = state.nextId++;
    state.timers.push({ id, fn, ms, acc: 0 });
    return id;
  };
  const clearIntervalStub = (id) => { state.timers = state.timers.filter((t) => t.id !== id); };
  const location = { reload() { state.reloads += 1; } };

  let seqIdx = 0;
  const fetchStub = () => {
    state.fetches += 1;
    const f = freshnessSeq[Math.min(seqIdx, freshnessSeq.length - 1)];
    seqIdx += 1;
    return Promise.resolve({ json: () => Promise.resolve(f) });
  };

  const window = { __VEGA_REFRESH__: cfg };

  // Virtual clock. Advance in whole milliseconds; fire each timer whose period has elapsed.
  const advance = async (ms) => {
    for (let t = 0; t < ms; t += 1) {
      for (const timer of [...state.timers]) {
        timer.acc += 1;
        if (timer.acc >= timer.ms) { timer.acc = 0; timer.fn(); }
      }
      if (t % 50 === 0) await new Promise((r) => setImmediate(r));  // let fetch promises settle
    }
    await new Promise((r) => setImmediate(r));
  };

  new Function('window', 'document', 'location', 'fetch', 'setInterval', 'clearInterval', code)(
    window, document, location, fetchStub, setIntervalStub, clearIntervalStub);
  return { state, advance };
}

const CFG = { enabled: true, stamp: 1000, poll: 20, grace: 8 };
const SAME = { stamp: 1000, scan_at: '14:30', scan_age_min: 3, market_open: true, refresh_min: 15, running: [] };
const NEWER = { stamp: 2000, scan_at: '14:45', scan_age_min: 0.2, market_open: true, refresh_min: 15, running: [] };

const results = {};
const check = (name, cond, detail) => { results[name] = { pass: !!cond, detail: detail || '' }; };

(async () => {
  // 1. An unchanged artifact must never reload, however long the page sits there.
  {
    const e = makeEnv(CFG, [SAME]);
    await e.advance(20000 * 6);
    check('unchanged_stamp_never_reloads',
          e.state.reloads === 0 && e.state.appended.length === 0,
          `reloads=${e.state.reloads} banners=${e.state.appended.length} fetches=${e.state.fetches}`);
    check('age_pill_updates_without_reloading',
          typeof e.state.pillText === 'string' && e.state.pillText.indexOf('14:30') !== -1,
          `pill=${JSON.stringify(e.state.pillText)}`);
  }

  // 2. A superseded board banners, counts down, and reloads exactly once.
  {
    const e = makeEnv(CFG, [SAME, NEWER]);
    await e.advance(20001);                      // second poll returns the newer stamp
    const bannered = e.state.appended.length === 1;
    await e.advance(8100);
    check('advanced_stamp_reloads_after_grace', bannered && e.state.reloads === 1,
          `bannered=${bannered} reloads=${e.state.reloads}`);
    await e.advance(20000);
    check('reload_is_not_repeated', e.state.reloads === 1, `reloads=${e.state.reloads}`);
  }

  // 3. THE COUNTDOWN HOLDS WHILE THE OPERATOR IS TYPING. Losing a half-entered contract count
  //    is a worse failure than being one scan behind.
  {
    const e = makeEnv(CFG, [SAME, NEWER]);
    await e.advance(20001);
    e.state.activeElement = { tagName: 'INPUT' };
    await e.advance(30000);
    const heldWhileTyping = e.state.reloads === 0;
    e.state.activeElement = null;
    await e.advance(8100);
    check('typing_holds_the_countdown', heldWhileTyping && e.state.reloads === 1,
          `held=${heldWhileTyping} after_blur_reloads=${e.state.reloads}`);
  }

  // 4. An open candidate drawer holds it too.
  {
    const e = makeEnv(CFG, [SAME, NEWER]);
    await e.advance(20001);
    e.state.querySelectorHit = '.vdetail.open';
    await e.advance(30000);
    check('open_drawer_holds_the_countdown', e.state.reloads === 0, `reloads=${e.state.reloads}`);
  }

  // 5. "Keep this view" is permanent, not a snooze.
  {
    const e = makeEnv(CFG, [SAME, NEWER]);
    await e.advance(20001);
    e.state.ids['rlstop'].onclick();
    await e.advance(20000 * 5);
    check('keep_this_view_stops_reloading',
          e.state.reloads === 0 && e.state.appended.length === 0,
          `reloads=${e.state.reloads} banners=${e.state.appended.length}`);
    check('keep_this_view_says_so', String(e.state.pillText).indexOf('paused') !== -1,
          `pill=${JSON.stringify(e.state.pillText)}`);
  }

  // 6. Disabled means inert -- no polling at all.
  {
    const e = makeEnv({ enabled: false, stamp: 1000, poll: 20, grace: 8 }, [NEWER]);
    await e.advance(20000 * 3);
    check('disabled_config_does_nothing', e.state.fetches === 0 && e.state.reloads === 0,
          `fetches=${e.state.fetches}`);
  }

  console.log(JSON.stringify(results, null, 1));
  process.exit(Object.values(results).every((r) => r.pass) ? 0 : 1);
})();
