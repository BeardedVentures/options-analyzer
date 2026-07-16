# VEGA Paper Desk — UX Redesign Spec (2026-07-15)

**What this is:** a design specification for the live cockpit (`vega_app.py`), grounded in how the human
visual system actually reads numbers, color, and dense tables. Every recommendation names the perceptual or
cognitive principle it rests on, gives the concrete rule, and points at the exact code to change. Paired with a
visual mockup (`vega_dashboard_redesign_mockup.html`) so you can see the target before touching the code.

**Design goal (your words):** intuitive, smart, and fun. Translated into measurable terms:
- **Intuitive** = the eye lands on the right number without effort (preattentive processing, visual hierarchy).
- **Smart** = the interface shows its own confidence and never implies more precision than the data has.
- **Fun** = tasteful motion, a hero moment for the day's best trade, and a board that rewards a quick glance.

---

## 1. The core problem: everything is shouting, so nothing is heard

The current table is 15 columns wide, and almost every cell is either **bold** or colored. When everything is
emphasized, emphasis carries no information — the eye has no anchor and has to read the whole row
left-to-right, every row. Three principles are being violated at once:

- **Preattentive processing** (Treisman): the visual system can locate *one* differing element (a bold number,
  a single color) in <200ms — but only if it's the *only* thing that differs. Bolding six of fifteen cells
  destroys the effect.
- **Miller's Law / working memory**: ~4–7 chunks at once. 15 ungrouped columns exceeds that, forcing the user
  to re-scan rather than hold a row in mind.
- **Von Restorff (isolation) effect**: the item that looks different is the one remembered and acted on. Right
  now the actionable elements (the day's best trade, the Log button) look the same as everything else.

**Fix in one sentence:** emphasize exactly one number per row (Priority), group the other fourteen columns into
four labeled clusters, and let color mean only one thing.

---

## 2. Number formatting — how humans actually read digits

### 2.1 Right-align and use tabular figures  *(biggest single win)*
Numbers are compared by **magnitude**, and the brain reads magnitude from digit *position*. Left-aligned
numbers (the current default — `td` has no alignment) make `$16` and `$125` start at the same x-position, so a
10× difference looks like a 2-character difference. **Right-align every numeric column** and set
`font-variant-numeric: tabular-nums` so each digit occupies identical width and columns line up into visual
bar charts you can scan vertically.

> Rule: all numeric `<td>` → `text-align:right; font-variant-numeric:tabular-nums;`. Text columns
> (ticker, signals) stay left-aligned. This one change does more for scannability than any color work.
> (Basis: Tufte, *Visual Display*; Few, *Show Me the Numbers* — tabular alignment turns a column of numbers
> into a pre-attentive comparison.)

### 2.2 De-emphasize the decimals, emphasize the magnitude
The integer part carries the signal; cents are noise you only need on demand. Render the primary figure large
and the fractional/secondary part smaller and dimmer (`$16` big, `.16/sh` small-grey beneath — the current
sub-line pattern is good, keep it but make the size gap bigger).

### 2.3 Signed numbers for anything directional
P/L, calibration, and theta have a *direction*, and a leading `+`/`−` is read preattentively as
"good/bad" before the digits register. Keep the `$+14` / `+24pp` convention on P/L and calibration (already
present in `stat_cards`) — it's correct. Do **not** sign quantities that have no direction (credit, ROI, POP):
a `+` on POP implies a comparison that isn't there.

### 2.4 Consistent precision within a column
Pick decimals per column and never vary them: POP as whole % (80%), ROI as whole % (19%), credit as whole $
with cents in the sub-line, delta as 2dp (−0.21). Ragged precision (80% next to 78.4%) makes columns read as
noisier than the data is.

### 2.5 Thousands separators once numbers get big
Capital-at-risk and credit-collected will cross $1,000 — use `1,240` not `1240`. The grouping comma is a
parsing aid the eye relies on.

---

## 3. Color — make it mean one thing, and make it colorblind-safe

### 3.1 The current palette overloads green
Right now green marks the *price* (`.px`), *positive P/L* (`.pos`), *passing gates* (`.g`), and *credit*.
When one color means four things, it means nothing — the user can't use color as a channel. **Reserve green/red
exclusively for realized outcome and live P/L** (win/loss rows, unrealized P/L, calibration sign). Everything
else — price, credit, POP — becomes neutral ink. (Basis: color is your scarcest perceptual channel; spend it
on the one distinction that drives decisions.)

### 3.2 Red/green is the worst possible pair for ~8% of men
Deuteranopia/protanopia (red-green colorblindness) affects ~8% of men. A green-vs-red gate chip is invisible to
them. **Double-encode**: never rely on hue alone. Pairing rules:
- Gate status: color **and** a filled/empty shape (see §4.1), so it reads with zero color.
- Win/loss rows: color **and** a text result (`WIN`/`LOSS`) **and** the signed P/L — three redundant channels.
- Prefer a **blue↔amber** accent pair over red↔green for non-P/L emphasis (blue and orange are distinguishable
  across all common color-vision types). Keep true red/green only for money, where the cultural mapping is
  worth it and the redundancy above covers the deficit.

### 3.3 Use a calm, low-saturation base so accents can pop
Dense financial tables read best on near-neutral backgrounds with one saturated accent reserved for the call to
action. The current near-white is fine; the change is *restraint* elsewhere so the accent has contrast to work
with. (Basis: Weber–Fechner — a signal is perceived by its *contrast* against its surroundings, not its
absolute intensity. A green Log button pops only if the rest of the row is quiet.)

---

## 4. Turn parsing into glancing

### 4.1 Replace the gate text with an 8-dot matrix  *(preattentive gate reading)*
Today gates are a `6/8` chip plus a comma-joined failure list (`IV-Rank<45, min_credit`). Reading which gates
failed is a *sequential* text-parsing task. Replace it with a fixed row of **8 dots** in a stable order
(IV·Δ·buffer·cr/w·credit·liq·POP·DTE), filled = pass, hollow = fail. Now "which gates failed" is a
*preattentive* pattern — you see the shape of the failures without reading. Keep the `6/8` count as a label and
put the full names in a tooltip (progressive disclosure). A row of dots is also visually calmer than a wall of
red words. (Basis: preattentive shape/fill detection; Gestalt similarity.)

### 4.2 Group the 15 columns into 4 labeled clusters
Apply **Gestalt proximity and common region**: columns that answer the same question should sit together under
a group header with subtle vertical separators between groups:

| Group | Columns | The question it answers |
|---|---|---|
| **Trade** | Ticker · Payoff · Short/Long · Exp | *What is it?* |
| **Edge** | Priority · POP · ROI · Cr/W | *Is it worth taking?* |
| **Greeks/Risk** | Δ · θ/day · IV-Rank · Credit | *What am I holding?* |
| **Quality** | Gate dots · Signals | *Did it pass the rules?* |
| **Action** | Log | *Take it* |

Four chunks fit working memory where fifteen columns don't. The group headers also tell a first-time viewer
what to look at first (Edge), which is the "smart" part of intuitive.

### 4.3 Give Priority the hero treatment and mark it as the sort key
Priority is the one number the whole board is sorted by, so it should be the one number that's visually
loudest: largest type, a small horizontal **micro-bar** behind the value (0–100 scale) so rank is readable as
*length* — a preattentive quantitative channel far faster than reading digits. Add a ▼ glyph in the Priority
header so the user knows *why* the rows are in this order (right now the sort is invisible).

### 4.4 Hero card for the day's single best trade
**Von Restorff:** lift the top-ranked candidate out of the table into a bordered "Top setup today" card above
the grid — bigger payoff diagram, plain-English one-liner ("SPY 722/721, 38 DTE — 80% POP, 19% ROI, passes 6/8
gates"). It gives the eye a starting point and makes the board feel like it has a point of view. This is where
"fun" and "smart" meet.

---

## 5. Confidence — the "smart" requirement

A dashboard that shows `Win rate 100%` and `Profit factor ∞` off one trade looks naïve, not smart. The
interface should visibly know how much data stands behind each number (ties to code finding S2).

- **Progress, not just a number.** "Closed 1 **of 30**" should be a thin progress bar, not a subtitle. The
  user instantly sees the sample is 3% mature.
- **Grey out or annotate premature stats.** Until `n_closed ≥ ~10`, render Calibration / Profit factor /
  Expectancy in muted grey with a "n=1 — not yet meaningful" tooltip. Showing an unreliable statistic at full
  confidence is the opposite of smart. (Basis: honest signalling / avoiding false precision — Tufte's
  "graphical integrity.")
- **POP is a probability, so show it as one.** An "80%" reads as a hard fact. A tiny confidence pip
  (Est vs Hist for IV-Rank already does this — extend the idea) or a shaded bar communicates "estimate" and
  discourages over-trust.

---

## 6. Micro-interactions — the "fun" without the cost

Fun in a numbers tool comes from responsiveness and small rewards, not decoration:

- **Row hover lift**: subtle background + 1px raise on hover so the row you're reading detaches from the grid
  (aids horizontal tracking on a wide table — also a readability win, not just delight).
- **Log confirmation**: on Log, a brief green pulse on the row + a toast ("Logged SPY 722/721 ×1") instead of a
  full page flash. Immediate feedback closes the action loop (Doherty threshold — sub-400ms feedback keeps the
  user in flow).
- **Win celebration, tastefully**: when a closed trade lands a win, a one-shot subtle confetti or a green
  sweep on that row. Variable, earned reward is what makes a tracking tool sticky — but keep it to *closed
  wins* so it stays meaningful.
- **Rescan as a live state**: the button should show a spinner + "Scanning live chains…" inline (the status is
  already computed in `_scan_status`) rather than a silent reload, so the wait feels handled.
- **Payoff diagram polish**: on hover, show the P/L value at the cursor's price. The sparkline is already the
  best "small multiple" on the board (Tufte) — a little interactivity makes it a teaching tool.

None of these need a framework — the app is stdlib + inline SVG/CSS today, and all of the above are plain
CSS transitions + a few lines of vanilla JS.

---

## 7. Typography & layout hygiene

- **Establish a type scale** instead of near-uniform 13px. Suggested: hero/Priority 20px, primary cell 14px,
  sub-line 11px, group headers 10px uppercase tracked. Hierarchy in *size* does much of the work color is
  currently overdoing.
- **One accent font weight for emphasis** (600), reserved for the single hero number per row. Everything else
  regular (400). Bold currently appears ~6× per row; target 1×.
- **Row rhythm**: keep the light 1px rules but add a hover highlight instead of full zebra striping — zebra on a
  15-column table adds visual noise; hover gives on-demand row tracking with none of the static clutter.
- **Sticky header + group header** so the column meaning stays visible as the board scrolls (the watchlist is
  50 names — the board is long).

---

## 8. Accessibility checklist (fast wins)

- Contrast ≥ 4.5:1 for all text (the current `#999` dim on white is ~2.8:1 — bump to `#6b7280`).
- Never encode meaning by color alone (§3.2) — gate dots, win/loss text, signed P/L all double-encode.
- Hit targets ≥ 32px for Log/Close buttons and the contracts input.
- Respect `prefers-reduced-motion` — gate the confetti/pulse behind it so motion-sensitive users opt out.

---

## 9. Priority order for implementation

1. **Right-align + tabular-nums on all numeric columns** (§2.1) — one CSS block, largest readability gain.
2. **Restrict color to P/L only; double-encode gates as dots** (§3, §4.1) — fixes both clarity and colorblind
   safety in one pass.
3. **Group columns into 4 clusters + Priority hero treatment with micro-bar and sort glyph** (§4.2–4.3).
4. **Confidence: progress bar + grey premature stats** (§5) — also closes code finding S2.
5. **Hero card for the top setup** (§4.4).
6. **Micro-interactions**: hover lift, Log toast, rescan spinner, win pulse (§6).
7. Type scale, sticky headers, contrast bumps (§7, §8).

Items 1–2 are ~30 minutes of CSS and change the *feel* immediately. 3–5 are the "smart" layer. 6 is the "fun"
layer. The mockup shows all of it assembled so you can lift the exact styles.

---

*Companion files: `VEGA_Code_Review_2026-07-15.md` (engine audit — S1/S2 there map to §5–6 here) and
`vega_dashboard_redesign_mockup.html` (the visual target).*
