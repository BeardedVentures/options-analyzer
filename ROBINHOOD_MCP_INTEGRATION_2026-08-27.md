# Robinhood Agentic Trading MCP — Integration (2026-08-27)

## What changed

Added Robinhood's official Agentic Trading MCP server (`agent.robinhood.com/mcp/trading`)
as VEGA's **primary** options data source, ahead of Polygon and yfinance. This replaces
the Tradier/new-vendor path discussed earlier — it uses the Robinhood account you already
trade and bank through, no second brokerage account, and (per Robinhood's own support docs)
gives real-time quotes with full Greeks through a sanctioned, ToS-safe channel — not the
unofficial `robin_stocks`-style private-API wrappers.

**Files touched:**
- `data/robinhood_mcp.py` (new) — the MCP client: OAuth (browser approval once, token
  cached to `data/.robinhood_mcp_tokens.json`, gitignored), session handling, and thin
  wrappers around the `get_option_chains` / `get_option_quotes` tools.
- `data/fetcher.py` — added `validate_robinhood_connection()` and
  `_parse_robinhood_options()`, and made Robinhood Tier 1 in `get_options_chain()`
  (Polygon becomes Tier 2 fallback, yfinance Tier 3).
- `config.py` — `ROBINHOOD_MCP_ENABLED` (default true) and `ROBINHOOD_MCP_URL`.
- `requirements.txt` — added `mcp` and `httpx`.
- `.gitignore` — added the token cache file.
- `test_robinhood_mcp_connection.py` (new) — standalone one-time connection test.

## What is NOT yet confirmed — read this before trusting a scan

I built this from Robinhood's own public support-article description of the MCP tools
(`get_option_chains`, `get_option_quotes` returning "full Greeks") and the standard MCP
Python SDK's OAuth client pattern. **I have not run this against the live server** —
that requires your actual Robinhood login in a real browser, which I can't do. Two things
are genuinely unverified:

1. **The exact field names in Robinhood's response.** `_parse_robinhood_options()` in
   `data/fetcher.py` tries several plausible key spellings for each field (e.g.
   `bid_price` or `bid`) via a small `_first_present()` helper, but if the real response
   is shaped differently than guessed, it will log a WARNING with the actual keys it saw
   and return zero records rather than silently returning wrong numbers — same
   fail-safe philosophy as the rest of this file.
2. **Whether the cached OAuth token survives unattended reuse**, or needs periodic
   re-approval (a real risk flagged in `VEGA_DataVendor_Comparison_2026-08-27.md` in the
   Claude project — one third-party writeup on running this headlessly said re-auth is
   "manual by design"). Since you're already manually relaunching VEGA to refresh the
   screen, occasional re-approval likely isn't a new burden — but worth knowing it's
   there.

## Before running a real scan

1. `pip install -r requirements.txt` (installs `mcp`).
2. `python test_robinhood_mcp_connection.py` — approve the browser prompt once, then
   check the printed output:
   - The tool list should show `get_option_chains` / `get_option_quotes` (or whatever
     they're actually named — if different, tell me and I'll fix the calls in
     `robinhood_mcp.py`).
   - The raw SPY chain/quote dump should show real numbers, not nulls.
3. If that looks right, run `python -c "from data import fetcher; print(fetcher.validate_robinhood_connection())"`
   from the project root — should report `healthy: true`.
4. Then a normal VEGA run will use Robinhood data automatically (Tier 1). Check the
   `chain_source` field VEGA already logs per ticker — it should say `robinhood` instead
   of `polygon`/`yfinance`.

If step 2 or 3 turns up a mismatch, send me the printed output and I'll correct the
field-mapping in one pass rather than guessing again.

---

## Validation & hardening addendum — 2026-08-27, tower session

Reviewed on the machine that runs the engine, with network egress and the live venv.

### 1. The server is real — verified by direct probe, not by search

```
DNS  agent.robinhood.com                    -> 18.238.171.22
GET  /mcp/trading                           -> 405 method not allowed   (route exists)
GET  /                                      -> 404                      (not a catch-all)
POST /mcp/trading  {"method":"initialize"}  -> 401
     WWW-Authenticate: Bearer resource_metadata="https://agent.robinhood.com/
                              .well-known/oauth-protected-resource/mcp/trading"
GET  /.well-known/oauth-authorization-server -> 200
     authorization_endpoint  https://robinhood.com/oauth
     token_endpoint          https://api.robinhood.com/oauth2/token/
     registration_endpoint   https://agent.robinhood.com/oauth/trading/register
     grant_types_supported   ["authorization_code", "refresh_token"]
     code_challenge_methods  ["S256"]
     token_endpoint_auth     ["none"]        (public client + PKCE)
```

Textbook RFC 9728 remote-MCP OAuth. The integration is built against something that exists.

### 2. The "manual re-auth forever" worry is probably wrong

`refresh_token` is in `grant_types_supported`, and the client already declares it
(`grant_types=["authorization_code","refresh_token"]`) with the MCP SDK handling renewal
against a persisted token store. So the expected shape is **one browser approval, then silent
refresh** — not periodic manual re-approval. Unverified only in the sense that refresh-token
lifetime is not published; the one thing worth actually measuring is whether a cached token
still works after a few days.

### 3. What was fixed before this could reach a cycle

The integration shipped **enabled by default at Tier 1**, with `mcp>=1.2.0` added to
`requirements.txt` and an instruction to `pip install -r requirements.txt`. That combination
was one command away from this failure:

- no cached token, so the SDK enters browser OAuth;
- `_redirect_handler` calls `webbrowser.open()` — on a task that runs `-WindowStyle Hidden`,
  non-interactive, with nobody watching;
- `wait_for_callback` then blocks for **300 s**, and nothing latched the failure, so this
  repeated **per ticker** across a 56-ticker scan;
- against a 30-minute `ExecutionTimeLimit` on a cycle that already runs 11–16 minutes.

That is the 2026-08-25 close-scan death rebuilt from new parts. Changes:

| Change | Where | Why |
|---|---|---|
| `ROBINHOOD_MCP_ENABLED` default `true` → **`false`** | `config.py` | An unverified parser must not sit at Tier 1 of a live engine until someone opts in |
| `ROBINHOOD_MCP_ALLOW_BROWSER` (new, default false) | `config.py` | Browser OAuth is opt-in and only the standalone test sets it |
| `ROBINHOOD_MCP_CALLBACK_TIMEOUT` (new, 120 s) | `config.py` | 300 s could blow the cycle budget by itself |
| `InteractiveAuthRequired` raised instead of opening a browser | `robinhood_mcp.py` | Fails in milliseconds instead of blocking; needs **both** the flag and a real TTY |
| `_robinhood_unavailable_this_run` latch | `fetcher.py` | A source that cannot serve ticker 1 will not serve tickers 2–56 |
| `from data import robinhood_mcp` moved inside `try` | `fetcher.py` | A missing optional dependency degrades the source, not the scan |
| `getattr(config, "ROBINHOOD_MCP_ENABLED", True)` → `False` | `fetcher.py` | Defence in depth on the default |
| `tests/test_robinhood_mcp.py` — 15 tests | new | There were none |

Guard proven load-bearing: with `_interactive_auth_allowed` stubbed back to permissive,
`webbrowser.open` fires on this headless check; with the guard in place it raises and opens
nothing. Full suite **1,203 passing**.

### 4. Still true, and still the gating risk

The field mapping in `_parse_robinhood_options` has **still never seen a real response**. That
is not fixable from here — it needs the browser approval only the account owner can give.
Nothing in this addendum changes that; it only ensures the untested parser cannot damage the
engine while it waits.

### 5. Operator steps

```bash
pip install -r requirements.txt
python test_robinhood_mcp_connection.py     # opens ONE browser tab; approve once
```

The script now sets `ROBINHOOD_MCP_ALLOW_BROWSER=true` and `ROBINHOOD_MCP_ENABLED=true` for its
own process only, so it can authorize without changing what the engine does.

If the printed chain looks right, enable the engine path explicitly by adding
`ROBINHOOD_MCP_ENABLED=true` to `.env`, then confirm `chain_source=robinhood` appears in
`data/data_quality_log.json` on the next cycle. If it does not look right, leave it off and
send the tool list plus raw response for remapping — the parser logs the real keys it saw
rather than failing silently.
