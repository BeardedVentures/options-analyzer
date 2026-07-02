# BeardedVentures Content Formatter

`bv_content_formatter.py` — VEGA scan → social media posts

Converts each VEGA scan result into 3 ready-to-review social posts for the BeardedVentures trading channel. Run after every scan, manually or via n8n.

---

## What It Generates

**Post 1 — Market Context** (every scan)
Market bias, VIX, SPY move, scan count. Short enough for Twitter, expands for LinkedIn.

**Post 2 — Setup Highlight** (only when qualified trades exist)
Highlights the top qualifying setup by edge score. Uses full trade detail (strikes, credit, IV rank, VRP, POP) when VEGA payload is available. Falls back to ticker + edge score from scan_log.

**Post 3 — Educational** (every scan)
Derives a lesson from what the scanner rejected today. Automatically picks the most relevant angle: IV Rank, VRP, liquidity, probability, news. Rotates based on rejection data — no manual curation needed.

---

## Quick Start

```bash
# Run from inside options_intelligence/
cd options_intelligence

# Use latest scan from scan_log.json (quick mode):
python bv_content_formatter.py

# Print to screen only (no files written):
python bv_content_formatter.py --print-only

# Use a specific scan entry:
python bv_content_formatter.py --scan-log logs/scan_log.json --index -2

# Use full VEGA payload (richer content — includes VIX, SPY, strikes, credit):
python bv_content_formatter.py --vega-file path/to/vega_payload.json

# Fetch latest from JARVIS tower:
python bv_content_formatter.py --from-jarvis --jarvis-host http://100.83.195.50:8000
```

---

## Output

Files are saved to `output/social_content/`:

```
bv_content_YYYY-MM-DD_SESSION.json   ← machine-readable, for n8n automation
bv_content_YYYY-MM-DD_SESSION.txt    ← human-readable, for manual review/copy-paste
```

The JSON structure per post:
```json
{
  "post_type": "market_context | setup_highlight | educational",
  "platforms": {
    "twitter": "...",
    "linkedin": "..."
  },
  "char_count_twitter": 240
}
```

---

## Two Input Modes

| Mode | Data Available | Content Quality |
|------|---------------|-----------------|
| **Scan log** (`scan_log.json`) | Ticker, edge score, rejection reasons | Good — market context + ticker names |
| **VEGA payload** (full JSON) | All of the above + strikes, credit, IV rank, VRP, POP, trend, RSI | Rich — specific prices and metrics in posts |

**Recommendation:** Wire n8n to capture the VEGA payload at ingest time and pass it to this formatter. The scan already POSTs the full payload to `/vega/ingest` — same data, just needs to be routed to this formatter in parallel.

---

## Wire to n8n (Manual Trigger, Today)

Until the JARVIS action bridge is built, you can trigger this from n8n manually:

1. Add an **Execute Command** node after the VEGA ingest node
2. Command: `cd /path/to/options_intelligence && python bv_content_formatter.py`
3. Outputs land in `output/social_content/`

For richer content, pipe the VEGA payload:
```bash
echo '{{ $json.body }}' | python bv_content_formatter.py --stdin
```

---

## Wire to n8n (Automated — After JARVIS Action Bridge)

Once the `/build-queue` action bridge exists:

1. VEGA scan completes → POSTs to `/vega/ingest`
2. n8n VEGA ingest node fires → also calls formatter
3. Formatter writes JSON to `output/social_content/`
4. Review node (optional): route JSON to JARVIS `/chat` for approval
5. Post node: hit social APIs (Buffer, Twitter API, LinkedIn API)

---

## Connecting to Social APIs (Future)

The JSON output is structured for easy integration:

**Buffer**: POST to `https://api.bufferapp.com/1/updates/create.json`
```json
{ "text": "<twitter content>", "profile_ids": ["<id>"] }
```

**Twitter API v2**: POST to `/2/tweets`
```json
{ "text": "<twitter content>" }
```

**LinkedIn**: POST to `/v2/ugcPosts`
```json
{ "specificContent": { "shareCommentary": { "text": "<linkedin content>" } } }
```

The formatter outputs both platform versions per post — just route to the right API.

---

## Notes

- **Disclaimer is non-removable.** All posts include the standard educational disclaimer. Do not post without it.
- **Review before posting.** The formatter generates drafts. Read each post before it goes live.
- **VEGA mode is richer.** Scan-log mode is functional but won't have VIX/SPY data or specific strikes. The VEGA payload has everything.
- **GitHub Actions**: The scan runs at 9:50 AM ET and 3:10 PM ET weekdays via cron. Add a workflow step to run the formatter after the scan and commit the output.

---

## File Location

```
options_intelligence/
  bv_content_formatter.py          ← the formatter (this file's source)
  BV_CONTENT_FORMATTER_README.md   ← this doc
  output/
    social_content/                ← generated content packages land here
      bv_content_YYYY-MM-DD_SESSION.json
      bv_content_YYYY-MM-DD_SESSION.txt
```
