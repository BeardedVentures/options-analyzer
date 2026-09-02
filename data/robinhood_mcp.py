"""
data/robinhood_mcp.py -- Robinhood Agentic Trading MCP client.

Talks to Robinhood's OFFICIAL Trading MCP server (agent.robinhood.com/mcp/trading),
launched 2026, NOT the unofficial robin_stocks-style private-API wrappers. This is a
sanctioned, ToS-safe way to pull options chains/quotes/Greeks from the same Robinhood
account VEGA already trades and banks through -- no second brokerage account needed.

Auth: standard remote-MCP OAuth (browser approval, once). The resulting token is cached
to `data/.robinhood_mcp_tokens.json` (gitignored) and reused on every subsequent run, so
this should only prompt a browser approval again if that token is later revoked/expires
server-side -- see ROBINHOOD_MCP_NOTES.md for what to do if that happens more often than
expected.

IMPORTANT -- first-run status: the exact MCP tool names/parameter shapes below
("get_option_chains", "get_option_quotes") are Robinhood's own stated names from their
public support docs, not yet confirmed against a live call. Run
`test_robinhood_mcp_connection.py` once, standalone, before trusting this in a real scan --
it prints the server's real tool list/schemas and a sample response so the field-mapping
in fetcher.py's `_parse_robinhood_options()` can be corrected against real data rather than
guesses. Treat this module as "should work" not "confirmed working" until that test passes.

Requires: pip install mcp   (added to requirements.txt)
"""

import asyncio
from datetime import date, timedelta
import contextlib
import http.server
import sys
import json
import logging
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TOKEN_PATH = Path(__file__).parent / ".robinhood_mcp_tokens.json"
_CALLBACK_PORT = 3030
_CALLBACK_PATH = "/callback"

# Cached across calls within a process so repeated fetches in one scan don't re-import
# or re-probe tool schemas every time.
_tools_cache: Optional[List[str]] = None


# ─────────────────────────────────────────────
# Token pre-flight (added 2026-09-02)
# ─────────────────────────────────────────────
#
# WHY THIS EXISTS, and why it is here rather than as a patch to the MCP SDK.
#
# The SDK's refresh path is unreachable, and then broken. Both faults verified live on
# 2026-09-02 against this machine's real cached token:
#
#   1. UNREACHABLE. OAuthContext.token_expiry_time defaults to None and _initialize() loads
#      tokens from storage WITHOUT setting it (mcp/client/auth/oauth2.py:550). is_token_valid()
#      reads `not self.token_expiry_time or time.time() <= ...`, so an unknown expiry counts as
#      VALID. In a fresh process -- which every scheduled cycle is -- the refresh branch at
#      oauth2.py:589 (`if not is_token_valid() and can_refresh_token()`) therefore never fires.
#      A 401 then goes to the FULL OAuth flow, not a refresh; there is no refresh attempt
#      anywhere inside that handler.
#
#   2. BROKEN WHEN FORCED. Patching is_token_valid() to return False once made _refresh_token()
#      run: it POSTed to https://agent.robinhood.com/token and got HTTP 404. The SDK builds the
#      token URL from the server ORIGIN before metadata discovery has happened. Robinhood's real
#      token endpoint is on a DIFFERENT HOST -- https://api.robinhood.com/oauth2/token/ -- named
#      in the authorization-server metadata.
#
# So the promise in _redirect_handler's message ("the cached refresh token then serves
# unattended runs") was false: every expiry, forever, required a browser click. That is not a
# stale-token problem, it is an architecturally unreachable path.
#
# NOT AN SDK PATCH, deliberately. Monkeypatching OAuthContext would put this project's
# unattended operation at the mercy of an upstream refactor, and the failure would be silent.
# This runs BEFORE the SDK is handed the token, so the SDK only ever sees a fresh one and its
# broken path is never entered.
#
# The endpoint is DISCOVERED, never hardcoded. Hardcoding api.robinhood.com would reproduce the
# exact bug above with a different constant.
_AUTH_EVENT_LOG = Path(__file__).resolve().parent.parent / "logs" / "vega_auth_events.jsonl"

# Refresh when less than this remains. One trading day of margin: a cycle that starts inside
# the margin renews rather than gambling that the token outlives the run.
TOKEN_REFRESH_MARGIN_S = 24 * 3600


class TokenRefreshError(RuntimeError):
    """A refresh that must not be swallowed -- see _persist_tokens on the write-failure case."""


def _record_auth_event(event: Dict) -> None:
    """Durable, operator-visible record of every pre-flight outcome.

    Mirrors logs/cycle_deaths.jsonl in intent. A WARNING in run.log is technically a record and
    practically unread -- run.log is 15MB. This file holds only auth events, so a non-empty
    tail is itself the signal. Never raises: a failure to journal must not break the scan.
    """
    try:
        from datetime import datetime as _dt
        event = {"at": _dt.now().isoformat(timespec="seconds"), **event}
        _AUTH_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUTH_EVENT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception as exc:               # pragma: no cover - journalling is best-effort
        logger.warning("[robinhood_mcp] could not journal auth event: %s", exc)


def _discover_token_endpoint(server_url: str, timeout: float = 12.0) -> Optional[str]:
    """The authorization server's token endpoint, read from its own metadata.

    RFC 8414 inserts the path INTO the well-known segment rather than appending it, and
    Robinhood scopes metadata per path, so the path-aware URL is tried first. Both forms were
    confirmed to answer on 2026-09-02; the root form is kept as a fallback for servers that
    do not scope per path.
    """
    import urllib.request

    parsed = urllib.parse.urlparse(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = (parsed.path or "").rstrip("/")
    candidates = []
    if path:
        candidates.append(f"{origin}/.well-known/oauth-authorization-server{path}")
    candidates.append(f"{origin}/.well-known/oauth-authorization-server")

    for url in candidates:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
            endpoint = meta.get("token_endpoint")
            if endpoint:
                logger.debug("[robinhood_mcp] token_endpoint %s discovered at %s", endpoint, url)
                return str(endpoint)
        except Exception as exc:
            logger.debug("[robinhood_mcp] metadata probe failed at %s: %s", url, exc)
    return None


def token_expiry_epoch() -> Optional[float]:
    """When the cached access token stops working, as a unix timestamp.

    The token file records `expires_in` (a DURATION) and, since this pre-flight landed,
    `obtained_at` (the instant it was issued). Files written before that fall back to the
    file's mtime, which is sound because _FileTokenStorage._save() rewrites the file on every
    token update -- but it is a fallback, not the design, because a file COPY would carry a
    wrong mtime and a right token.
    """
    try:
        raw = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = raw.get("tokens") or {}
    expires_in = tokens.get("expires_in")
    if not isinstance(expires_in, (int, float)):
        return None
    obtained = raw.get("obtained_at")
    if not isinstance(obtained, (int, float)):
        try:
            obtained = _TOKEN_PATH.stat().st_mtime
        except OSError:
            return None
    return float(obtained) + float(expires_in)


def _persist_tokens(raw: Dict, new_tokens: Dict) -> None:
    """Write refreshed tokens, then PROVE the write landed.

    LOUD ON FAILURE, BY DESIGN. Robinhood ROTATES the refresh token: the 2026-09-02 refresh
    returned a different one, which means the previous refresh token is dead the instant the
    server answers 200. A silent write failure here would leave the old, now-invalid credential
    on disk and the only working one in a process about to exit -- an unrecoverable state that
    would look exactly like an ordinary expiry days later. So the write is verified by reading
    it back, and a mismatch raises rather than returns.
    """
    import time as _time

    raw = dict(raw)
    raw["tokens"] = new_tokens
    raw["obtained_at"] = _time.time()
    payload = json.dumps(raw, indent=2)

    try:
        import durable_write
        durable_write.atomic_write_text(_TOKEN_PATH, payload)
    except Exception:
        _TOKEN_PATH.write_text(payload, encoding="utf-8")

    check = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
    if (check.get("tokens") or {}).get("access_token") != new_tokens.get("access_token"):
        _record_auth_event({"event": "persist_failed", "severity": "critical"})
        raise TokenRefreshError(
            "Refreshed the Robinhood token but FAILED TO PERSIST it. The refresh token "
            "rotates, so the copy on disk is now dead and the working one is only in this "
            "process. Re-authorize immediately: python test_robinhood_mcp_connection.py "
            f"(token file: {_TOKEN_PATH})")


def ensure_fresh_token(server_url: str, margin_s: float = TOKEN_REFRESH_MARGIN_S) -> Dict:
    """Renew the cached access token before the SDK is asked to use it.

    Returns a status dict; never raises for an ordinary failure, because a data source that
    cannot authenticate must cost its own records and nothing else. The ONE exception is
    TokenRefreshError from _persist_tokens, which is unrecoverable state and must not be
    swallowed.

    status: ok | refreshed | no_token | no_endpoint | refresh_failed
    """
    import time as _time
    import urllib.request

    if not _TOKEN_PATH.exists():
        return {"status": "no_token",
                "reason": "no cached token; authorize once with test_robinhood_mcp_connection.py"}

    expiry = token_expiry_epoch()
    remaining = None if expiry is None else expiry - _time.time()
    if remaining is not None and remaining > margin_s:
        return {"status": "ok", "seconds_remaining": int(remaining)}

    raw = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
    tokens = raw.get("tokens") or {}
    client_info = raw.get("client_info") or {}
    refresh_token = tokens.get("refresh_token")
    client_id = client_info.get("client_id")
    if not refresh_token or not client_id:
        out = {"status": "no_token", "reason": "no refresh_token/client_id cached"}
        _record_auth_event({"event": "preflight", "severity": "critical", **out})
        logger.error("[robinhood_mcp] token pre-flight: %s", out["reason"])
        return out

    endpoint = _discover_token_endpoint(server_url)
    if not endpoint:
        out = {"status": "no_endpoint",
               "reason": "authorization-server metadata did not name a token_endpoint"}
        _record_auth_event({"event": "preflight", "severity": "critical", **out})
        logger.error("[robinhood_mcp] token pre-flight: %s", out["reason"])
        return out

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status_code, payload = resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        out = {"status": "refresh_failed", "reason": f"{type(exc).__name__}: {exc}",
               "endpoint": endpoint}
        _record_auth_event({"event": "preflight", "severity": "critical", **out})
        logger.error("[robinhood_mcp] TOKEN REFRESH FAILED (%s). Robinhood access will fall "
                     "back to yfinance until someone runs "
                     "test_robinhood_mcp_connection.py from a terminal.", out["reason"])
        return out

    if status_code != 200 or not payload.get("access_token"):
        out = {"status": "refresh_failed", "http": status_code, "endpoint": endpoint}
        _record_auth_event({"event": "preflight", "severity": "critical", **out})
        logger.error("[robinhood_mcp] TOKEN REFRESH REJECTED (HTTP %s). Re-authorize with "
                     "test_robinhood_mcp_connection.py.", status_code)
        return out

    # RFC 6749 6: a refresh response MAY omit the refresh token, meaning "keep using the old
    # one". Robinhood does rotate, but do not assume it -- an omitted value must not blank it.
    new_tokens = {
        "access_token": payload["access_token"],
        "token_type": payload.get("token_type", tokens.get("token_type", "Bearer")),
        "expires_in": payload.get("expires_in", tokens.get("expires_in")),
        "scope": payload.get("scope", tokens.get("scope")),
        "refresh_token": payload.get("refresh_token") or refresh_token,
    }
    rotated = new_tokens["refresh_token"] != refresh_token
    _persist_tokens(raw, new_tokens)          # raises on an unpersisted rotation, by design

    out = {"status": "refreshed", "rotated": rotated,
           "expires_in": new_tokens.get("expires_in"), "endpoint": endpoint}
    _record_auth_event({"event": "preflight", **out})
    logger.info("[robinhood_mcp] token refreshed via %s (rotated=%s, expires_in=%s)",
                endpoint, rotated, new_tokens.get("expires_in"))
    return out


def _require_mcp_sdk():
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "The 'mcp' package is not installed. Run: pip install mcp"
        ) from exc


# ─────────────────────────────────────────────
# Token storage -- persists across process runs so OAuth only happens once
# ─────────────────────────────────────────────

class _FileTokenStorage:
    """Implements the mcp SDK's TokenStorage protocol, backed by a local JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self._tokens = None
        self._client_info = None
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
            raw = json.loads(self.path.read_text())
            if raw.get("tokens"):
                self._tokens = OAuthToken.model_validate(raw["tokens"])
            if raw.get("client_info"):
                self._client_info = OAuthClientInformationFull.model_validate(raw["client_info"])
        except Exception as exc:
            logger.warning(f"[robinhood_mcp] Could not load cached tokens ({exc}); will re-authorize.")

    def _save(self):
        data = {}
        if self._tokens is not None:
            data["tokens"] = self._tokens.model_dump(mode="json", exclude_none=True)
        if self._client_info is not None:
            data["client_info"] = self._client_info.model_dump(mode="json", exclude_none=True)
        try:
            self.path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning(f"[robinhood_mcp] Could not persist tokens: {exc}")

    async def get_tokens(self):
        return self._tokens

    async def set_tokens(self, tokens):
        self._tokens = tokens
        self._save()

    async def get_client_info(self):
        return self._client_info

    async def set_client_info(self, client_info):
        self._client_info = client_info
        self._save()


# ─────────────────────────────────────────────
# Local loopback callback server -- catches the OAuth redirect after browser approval
# ─────────────────────────────────────────────

class _OAuthCallbackServer:
    def __init__(self, port: int = _CALLBACK_PORT):
        self.port = port
        self._code = None
        self._state = None
        self._error = None
        self._event = threading.Event()
        self._server = None
        self._thread = None

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                outer._code = qs.get("code", [None])[0]
                outer._state = qs.get("state", [None])[0]
                outer._error = qs.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                if outer._code:
                    msg = "Robinhood connected -- you can close this tab and return to VEGA."
                else:
                    msg = f"Authorization failed: {outer._error or 'unknown error'}"
                self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
                outer._event.set()

            def log_message(self, *args):
                pass  # silence default request logging

        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    async def wait_for_callback(self, timeout: float = None):
        if timeout is None:
            try:
                import config
                timeout = float(getattr(config, "ROBINHOOD_MCP_CALLBACK_TIMEOUT", 120))
            except Exception:
                timeout = 120.0
        loop = asyncio.get_event_loop()
        got_it = await loop.run_in_executor(None, self._event.wait, timeout)
        if not got_it:
            raise TimeoutError(
                "Timed out waiting for Robinhood authorization in the browser "
                f"(waited {timeout:.0f}s). Re-run and approve the browser prompt promptly."
            )
        if self._error:
            raise RuntimeError(f"Robinhood OAuth error: {self._error}")
        if not self._code:
            raise RuntimeError("Robinhood OAuth callback returned no authorization code.")
        # mcp 2.x reads result.code / result.state off an AuthorizationCodeResult. The 1.x
        # contract was a bare (code, state) tuple, which reached the SDK as:
        #   AttributeError: 'tuple' object has no attribute 'state'
        # after the browser approval had already succeeded -- the very last step of the
        # flow, and the third place the 1.x/2.x API break surfaced.
        from mcp.shared.auth import AuthorizationCodeResult
        return AuthorizationCodeResult(code=self._code, state=self._state)

    def stop(self):
        if self._server:
            self._server.shutdown()


def _interactive_auth_allowed() -> bool:
    """Is a human present to approve a browser prompt?

    Two independent conditions, because either alone is too weak. The env flag is the
    explicit opt-in (only the standalone connection test sets it). The TTY check catches
    the case where the flag is left set in a profile that the scheduled task inherits.
    """
    try:
        import config
        if not getattr(config, "ROBINHOOD_MCP_ALLOW_BROWSER", False):
            return False
    except Exception:
        return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


class InteractiveAuthRequired(RuntimeError):
    """Raised instead of opening a browser in a non-interactive process."""


async def _redirect_handler(auth_url: str) -> None:
    if not _interactive_auth_allowed():
        # Fail immediately rather than opening a tab nobody will see and then blocking
        # for the callback timeout. The hourly cycle runs hidden and non-interactive; a
        # blocking prompt there costs the timeout PER TICKER and would push a 11-16 min
        # cycle past the 30 min ExecutionTimeLimit -- re-creating, from a new direction,
        # exactly the daily close-scan death that was fixed on 2026-08-25.
        raise InteractiveAuthRequired(
            "Robinhood MCP needs a one-time browser approval and this process is not "
            "interactive. Run test_robinhood_mcp_connection.py from a terminal to "
            "authorize once; the cached refresh token then serves unattended runs."
        )
    print("\n" + "=" * 70)
    print("ROBINHOOD AUTHORIZATION REQUIRED")
    print("Open this URL and approve VEGA's read access to options data:")
    print(auth_url)
    print("=" * 70 + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # printed URL is enough if auto-open fails (e.g. headless)


# ─────────────────────────────────────────────
# Session management
# ─────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _robinhood_session(server_url: str):
    """One authenticated MCP session, opened and closed inside a single task.

    This was a class that stored `streamable_http_client(...)` and `ClientSession(...)` on
    self and drove their __aenter__/__aexit__ from its own __aenter__/__aexit__. That looks
    equivalent to nesting `async with` blocks and is not:

        RuntimeError: Attempted to exit cancel scope in a different task than it was entered in

    anyio binds a cancel scope to the task that entered it, and the MCP transport runs its
    reader and writer in a task group. Entering the scope in one task and leaving it in
    another is unsupported, so the session blew up during teardown -- after the OAuth URL had
    already been issued, which made it look like an auth problem rather than a structural one.

    Nesting the context managers keeps every enter and exit in this coroutine, where anyio
    expects them. The auth flow itself was already correct: dynamic client registration
    succeeded against Robinhood on 2026-08-27 and the resulting client_info is cached.
    """
    _require_mcp_sdk()
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    callback_server = _OAuthCallbackServer(_CALLBACK_PORT)
    callback_server.start()

    async def callback_handler():
        return await callback_server.wait_for_callback()

    oauth_provider = OAuthClientProvider(
        # The FULL MCP url, not the origin. Robinhood scopes its protected-resource metadata
        # per path: the 401 names /.well-known/oauth-protected-resource/mcp/trading and the
        # issuer is https://agent.robinhood.com/mcp/trading.
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="VEGA Options Scanner",
            redirect_uris=[f"http://127.0.0.1:{_CALLBACK_PORT}{_CALLBACK_PATH}"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=_FileTokenStorage(_TOKEN_PATH),
        redirect_handler=_redirect_handler,
        callback_handler=callback_handler,
    )

    try:
        async with httpx2.AsyncClient(auth=oauth_provider, timeout=30.0) as http_client:
            # terminate_on_close=False: on close the SDK otherwise sends
            #   DELETE /mcp/trading  ->  400 Bad Request  ("Session termination failed")
            # because Robinhood's server does not implement the optional MCP session-
            # termination verb. That 400 raised inside the transport's task group and
            # surfaced as "unhandled errors in a TaskGroup" -- AFTER a fully successful
            # authenticated session, so a working call looked like a failed one. The
            # session is short-lived and server-expired anyway; not sending the DELETE
            # costs nothing.
            async with streamable_http_client(server_url, http_client=http_client,
                                              terminate_on_close=False) as streams:
                # 1.x yielded (read, write, get_session_id); 2.x yields a TransportStreams
                # tuple. Take the first two positionally so a third element -- or its absence
                # -- does not matter here.
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
    finally:
        # Always release the callback listener, including when the handshake fails. Leaving it
        # bound means the next attempt cannot start its listener on the same port.
        callback_server.stop()


def _extract_tool_json(result) -> Any:
    """CallToolResult -> parsed JSON, trying structuredContent first, then text content."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


# ─────────────────────────────────────────────
# Public async API
# ─────────────────────────────────────────────

async def alist_tools(server_url: str) -> List[Dict[str, Any]]:
    """List every tool the server exposes, with its input schema. Diagnostic use."""
    async with _robinhood_session(server_url) as session:
        result = await session.list_tools()
        return [
            # mcp 2.x renamed inputSchema -> input_schema. getattr covers both so this
            # diagnostic keeps working across SDK versions.
            {"name": t.name, "description": t.description,
             "input_schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)}
            for t in result.tools
        ]


# Quotes are requested by instrument UUID, and the tool's own schema warns that past 20 ids
# the response degrades. Batch conservatively.
_QUOTE_BATCH = 20

# Retries for one quote batch that came back as something other than JSON. The server answers
# errors -- rate limits among them -- as plain text, and the original code logged that and
# `continue`d, dropping all 20 contracts on the floor.
#
# Be precise about what this fixes, because the obvious story is wrong. A dropped batch does
# NOT show up as a low quotable ratio: fetcher._parse_robinhood_options builds records only
# from quotes that came back, so 20 lost contracts are 20 records that never exist, and the
# ratio is computed over the survivors. The harm is quieter than a bad ratio -- it is a hole
# in the strike grid that no downstream number reports, and the strikes lost are whichever
# ones happened to land in that batch.
_QUOTE_RETRIES = 3
_QUOTE_BACKOFF_BASE_S = 1.0

# The instrument walk gets its own retry count because a failure there is STRICTLY WORSE than a
# failed quote batch. A dropped quote batch loses 20 contracts out of a known population; a
# rate-limited instrument page ends the walk, and the caller keeps whatever pages arrived.
# 85 of the non-JSON responses in run.log came from this path, every one RATE_LIMITED, and
# roughly 35 of them truncated a walk rather than emptying it -- silently, because the
# "no instruments" warning only fires when the walk returns nothing at all.
_INSTRUMENT_RETRIES = 4


# Substrings that mark a TRANSIENT server error -- one worth waiting out. Everything else is
# permanent for this request and retrying it burns the request budget it is supposedly
# protecting, plus its own backoff, to arrive at the same answer.
#
# Added after the first version of this retry spent 4 attempts and 15s of backoff on
# "Request entity too large" -- a 413 caused by the REQUEST SHAPE (too many expiration dates in
# one filter, which is what _MAX_EXPIRATION_DATES chunking exists to avoid). No amount of
# waiting makes an oversized request smaller.
_RETRYABLE_ERROR_MARKERS = (
    "rate_limited",
    "too many requests",
    "429",
    "timeout",
    "timed out",
    "503",
    "service unavailable",
    "temporarily unavailable",
)


def _is_retryable_error(payload) -> bool:
    """Is this non-JSON error body worth another attempt?

    Conservative by design: retry only what is known-transient. An unrecognised error is
    treated as permanent, so a new server-side failure mode costs one request rather than
    _INSTRUMENT_RETRIES of them across every ticker in the scan.
    """
    text = str(payload or "").lower()
    return any(marker in text for marker in _RETRYABLE_ERROR_MARKERS)


def _error_reason(payload) -> str:
    """Short machine-readable tag for why a walk ended, for the truncation record."""
    return ("rate_limited_instrument_page" if _is_retryable_error(payload)
            else "server_error_instrument_page")


# Error bodies _RETRYABLE_ERROR_MARKERS did not recognise, counted per scan.
# {signature: count} -- a signature, not a raw body, so one recurring failure is one line
# rather than 56 near-identical ones.
#
# WHY THIS EXISTS. _is_retryable_error defaults the unknown to PERMANENT, which is the right
# default -- it fails fast instead of burning four attempts and 15s of backoff on a request
# that can never succeed. The cost of that default is a silent mode: if Robinhood reworded its
# rate-limit response, every marker would stop matching, retries would quietly collapse to one
# attempt, and the symptom would be a coverage regression with nothing in the log naming the
# cause. That is the same shape as the Polygon probe that sat at healthy=False for 29
# consecutive runs -- a component that had stopped working, reporting nothing about it.
#
# The 2026-09-01 scans make this concrete rather than hypothetical: SEVEN full scans produced
# ZERO rate-limited responses, so the matcher never ran. It is now load-bearing code that
# nothing has exercised in production, and a matcher that is never exercised is a matcher
# nobody can tell has broken.
_unrecognized_errors: Dict[str, int] = {}


def _error_signature(payload) -> str:
    """Collapse an error body to something stable enough to count.

    Keeps the leading words, which is where these servers put the error CODE, and drops the
    tail, which is where they put the ids and timestamps that make every instance unique.
    """
    text = " ".join(str(payload or "").split())[:80]
    return text or "<empty response>"


def _note_unrecognized(payload) -> None:
    sig = _error_signature(payload)
    _unrecognized_errors[sig] = _unrecognized_errors.get(sig, 0) + 1


def unrecognized_errors() -> Dict[str, int]:
    """Unrecognised error bodies seen since the last reset. Read by fetcher.chain_coverage."""
    return dict(_unrecognized_errors)


def reset_unrecognized_errors() -> None:
    """Called from fetcher.clear_cache() at the start of each scan."""
    _unrecognized_errors.clear()

# Strikes outside this band cost quote requests and are never selected. Measured across SPY,
# NKE, XLE and AMD on 2026-08-28, every strike inside VEGA's usable delta range (.10-.35) fell
# between 82% and 99% of spot; the widest was AMD at 82%. 75%-102% keeps a 7-point margin below
# the widest observed case and covers the long leg sitting beneath the short, while dropping the
# deep-OTM tail and the ITM strikes above spot that a put seller never touches.
#
# This is the only lever available on fetch cost: get_option_quotes SILENTLY TRUNCATES above 20
# instrument ids -- 50 ids returned 43 rows and 100 returned 81, with no error -- so the batch
# size cannot be raised to reduce the number of calls. Fetching fewer strikes is the alternative.
_STRIKE_LO_PCT = 0.75
_STRIKE_HI_PCT = 1.02


# get_option_instruments returns 100 per page. A full SPY put chain across a 20-day window is
# several hundred contracts, so cap the walk rather than trusting the server to terminate it.
_MAX_INSTRUMENT_PAGES = 12

# Above this many dates the expiration_dates parameter gets long enough that the server stops
# returning JSON. 201 dates (2,210 chars) reproducibly failed on 2026-08-27; 21 dates (230
# chars) is fine. Keep a wide margin -- this is a guard, not a measured ceiling.
_MAX_EXPIRATION_DATES = 60


def _next_cursor(next_url):
    """Pull the `cursor` query parameter out of the tool's `next` link.

    `next` is a fully-qualified internal Robinhood URL
    (http://edge-internal.brokeback-shard-router...), which is NOT reachable from here and must
    not be fetched directly -- only the cursor value it carries is usable, fed back into the
    tool's own `cursor` argument.
    """
    if not next_url:
        return None
    try:
        return urllib.parse.parse_qs(
            urllib.parse.urlparse(str(next_url)).query).get("cursor", [None])[0]
    except Exception:                                # pragma: no cover - defensive
        return None


async def afetch_chain(server_url: str, ticker: str, option_type: str = "put",
                       spot: float = None,
                       min_dte: int = None, max_dte: int = None,
                       expirations=None, strikes=None) -> Dict[str, Any]:
    """Fetch contracts of `option_type` and their quotes for `ticker`.

    THREE calls, not two, because that is what the server implements. The original code called
    get_option_chains(symbol=...) then get_option_quotes(symbol=..., option_type=...), which was
    a reasonable guess from the published tool names and wrong in every particular. Read off the
    live schemas on 2026-08-27:

      get_option_chains       takes `underlying_symbol` / `ids`, and returns CHAIN metadata --
                              tick sizes and multipliers, not contracts.
      get_option_instruments  takes `chain_symbol`, `type`, `expiration_dates`, `state` and
                              returns the contracts, each with a UUID `id`, `strike_price`
                              (string), `expiration_date`, and a pagination `next`.
      get_option_quotes       REQUIRES `instrument_ids` -- an array of those UUIDs. There is no
                              symbol form.

    So the contracts have to be listed first and their ids fed to the quote call. Returns the
    raw pieces; fetcher._parse_robinhood_options does the normalisation, same division of
    labour as the Polygon and yfinance parsers.
    """
    import config as _cfg
    min_dte = _cfg.MIN_DTE if min_dte is None else min_dte
    max_dte = _cfg.MAX_DTE if max_dte is None else max_dte

    today = date.today()
    # The tool filters server-side on exact dates, so for a normal scan window hand it every
    # date and let it return the ones that exist -- more robust than predicting which Fridays a
    # given underlying lists, since SPY has near-daily expiries and some names are monthly only.
    #
    # But ONLY for a normal window. auto_paper_cycle calls get_options_chain(ticker, 0, 200) to
    # mark open positions, which produced 201 dates in a 2,210-character parameter; the server
    # answered with a plain-text error instead of JSON, and the whole source latched off for the
    # rest of the run on the first ticker. Past the cap, omit the filter and let pagination plus
    # the strike window bound the walk, with the DTE filter applied client-side in the parser.
    span = [(today + timedelta(days=d)).isoformat()
            for d in range(max(min_dte, 0), max_dte + 1)]
    # CHUNK the window rather than dropping the filter past the cap. Omitting it looks
    # equivalent -- the parser filters DTE client-side anyway -- and is not:
    # get_option_instruments paginates by ASCENDING STRIKE across every expiration it can see,
    # so with no date filter the first pages are the lowest strikes of the entire chain.
    # Against the narrowed strike window (75-102% of spot) none of them qualify, the early-stop
    # never fires because those strikes are BELOW the window rather than above it, and the walk
    # exhausts its page budget having kept nothing. SPY DTE 5-120 returned 0 instruments in the
    # 09:08 scan on 2026-08-28 for exactly this reason, which then sent the run to Polygon and
    # yfinance. Chunking keeps the server-side filter -- which is what makes pagination
    # tractable -- for any width of DTE range.
    # An explicit expiration list beats the computed span. Marking an open position knows the
    # exact expiry, so asking for a 200-day window to price a single known contract fetched
    # 1,541 instruments and ~40 s per ticker to use two of them.
    if expirations:
        span = sorted({str(e)[:10] for e in expirations if e})
    date_chunks = [span[i:i + _MAX_EXPIRATION_DATES]
                   for i in range(0, len(span), _MAX_EXPIRATION_DATES)] or [None]

    # The useful strike band is on opposite sides for the two option types. A put seller works
    # below spot; a call seller works above it. Using the put window for calls would fetch
    # deep-ITM calls and miss every strike a bear-call or condor could actually use.
    # Explicit strikes bypass the percentage band ENTIRELY. The band is a selection
    # optimisation -- it exists so a scan does not quote strikes it would never sell -- and it
    # must never decide whether an already-open position can be priced. A put spread that is
    # winning has seen the underlying rally AWAY from it, so its strikes drift toward the
    # bottom of the band and eventually out of it: the better the position, the more likely the
    # window would stop being able to see it. Marking asks a different question from selection
    # and gets a different filter.
    want_strikes = None
    if strikes:
        try:
            want_strikes = {round(float(k), 2) for k in strikes}
        except (TypeError, ValueError):
            want_strikes = None
    if want_strikes:
        lo = hi = None
    elif spot:
        if option_type == "call":
            lo, hi = spot * (2 - _STRIKE_HI_PCT), spot * (2 - _STRIKE_LO_PCT)
        else:
            lo, hi = spot * _STRIKE_LO_PCT, spot * _STRIKE_HI_PCT
    else:
        lo = hi = None

    async with _robinhood_session(server_url) as session:
        # PAGINATED, 100 per page, ordered by STRIKE ASCENDING. This matters more than it
        # sounds: page one for SPY on 2026-08-27 was strikes 375-605 against a spot of 771 --
        # every contract on it hundreds of points out of the money, delta ~0.00. Reading only
        # the first page produced 82 perfectly-quoted records and ZERO inside VEGA's 0.12-0.30
        # delta band, which looks exactly like "this underlying has nothing to sell" rather
        # than like a truncated fetch.
        instruments = []
        truncated = False
        truncated_reason = None
        for chunk in date_chunks:
            cursor = None
            for _page in range(_MAX_INSTRUMENT_PAGES):
                args = {
                    "chain_symbol": ticker.upper(),
                    "type": option_type,
                    "state": "active",
                    "tradability": "tradable",
                }
                if chunk:
                    args["expiration_dates"] = ",".join(chunk)
                if cursor:
                    args["cursor"] = cursor
                raw = None
                last_error = None
                for attempt in range(_INSTRUMENT_RETRIES):
                    inst_result = await _call_read_tool(session, "get_option_instruments", args)
                    candidate = _extract_tool_json(inst_result)
                    if isinstance(candidate, dict):
                        raw = candidate
                        break
                    # The server answers errors as text, not JSON. Treating that as a dict raised
                    # "AttributeError: 'str' object has no attribute 'go'" from inside the anyio
                    # task group, which surfaced as an opaque ExceptionGroup. Every one of the 85
                    # such responses in run.log is "RATE_LIMITED: too many requests".
                    last_error = candidate
                    if not _is_retryable_error(candidate):
                        _note_unrecognized(candidate)
                        logger.warning("[robinhood_mcp] %s: non-retryable error from "
                                       "get_option_instruments, not retrying: %s",
                                       ticker, str(candidate)[:200])
                        break
                    delay = _QUOTE_BACKOFF_BASE_S * (2 ** attempt)
                    logger.warning("[robinhood_mcp] %s: non-JSON response from "
                                   "get_option_instruments (attempt %d/%d, retrying in %.0fs): %s",
                                   ticker, attempt + 1, _INSTRUMENT_RETRIES, delay,
                                   str(candidate)[:200])
                    if attempt < _INSTRUMENT_RETRIES - 1:
                        await asyncio.sleep(delay)
                if raw is None:
                    # TRUNCATION, not a skip. This `break` leaves the walk part-done and the
                    # caller previously proceeded as though the pages already collected were the
                    # whole chain. Pagination is ASCENDING BY STRIKE, so what survives is
                    # systematically the LOW strikes -- for puts, the deep-OTM tail below the
                    # 0.12-0.30 delta band. A truncated walk therefore yields a plausible
                    # partial chain that is missing precisely the region the engine sells from,
                    # and it is invisible to every ratio computed downstream: those are measured
                    # over the instrument list, and this shrinks the instrument list itself.
                    truncated = True
                    truncated_reason = _error_reason(last_error)
                    break
                payload = (raw.get("data") or {})
                page = payload.get("instruments") or []
                if not page:
                    break

                page_strikes = []
                for i in page:
                    try:
                        page_strikes.append(float(i.get("strike_price")))
                    except (TypeError, ValueError):
                        continue
                    if want_strikes is not None:
                        if round(page_strikes[-1], 2) in want_strikes:
                            instruments.append(i)
                    elif lo is None or lo <= page_strikes[-1] <= hi:
                        instruments.append(i)

                # Ascending order lets us stop as soon as a page opens above the useful range,
                # instead of walking every LEAPS strike to the top of the chain.
                if hi is not None and page_strikes and min(page_strikes) > hi:
                    break
                cursor = _next_cursor(payload.get("next"))
                if not cursor:
                    break
            else:
                # The page loop ran to _MAX_INSTRUMENT_PAGES with a cursor still outstanding:
                # the walk stopped because it hit its budget, not because the chain ended or
                # the ascending-strike early-stop fired. Same silent partial chain as a
                # rate-limited page, from a different cause, so it is reported the same way.
                if cursor:
                    truncated = True
                    truncated_reason = "page_budget_exhausted"

        if truncated:
            logger.warning("[robinhood_mcp] TRUNCATED_WALK %s %ss DTE %s-%s (%s): kept %d "
                           "instruments from an incomplete page walk. Pagination is ascending "
                           "by strike, so the missing strikes are the HIGH ones -- for puts "
                           "that is the sellable band, not the tail.",
                           ticker, option_type, min_dte, max_dte, truncated_reason,
                           len(instruments))

        if not instruments:
            logger.warning("[robinhood_mcp] no %s instruments for %s in DTE %s-%s",
                           option_type, ticker, min_dte, max_dte)
            return {"instruments": [], "quotes": [],
                    "truncated": truncated, "truncated_reason": truncated_reason}

        by_id = {i["id"]: i for i in instruments if i.get("id")}
        ids = list(by_id.keys())

        quotes = []
        dropped = 0
        dropped_strikes: List[float] = []
        for n in range(0, len(ids), _QUOTE_BATCH):
            batch = ids[n:n + _QUOTE_BATCH]
            q_payload = None
            for attempt in range(_QUOTE_RETRIES):
                q_result = await _call_read_tool(session, "get_option_quotes",
                                                 {"instrument_ids": batch})
                candidate = _extract_tool_json(q_result)
                if isinstance(candidate, dict):
                    q_payload = candidate
                    break
                if not _is_retryable_error(candidate):
                    _note_unrecognized(candidate)
                    logger.warning("[robinhood_mcp] %s: non-retryable error from "
                                   "get_option_quotes, not retrying: %s",
                                   ticker, str(candidate)[:200])
                    break
                # Exponential backoff. The one thing that must NOT happen on a rate-limited
                # response is an immediate retry, which spends the budget it is waiting on.
                delay = _QUOTE_BACKOFF_BASE_S * (2 ** attempt)
                logger.warning("[robinhood_mcp] %s: non-JSON response from get_option_quotes "
                               "(attempt %d/%d, retrying in %.0fs): %s",
                               ticker, attempt + 1, _QUOTE_RETRIES, delay,
                               str(candidate)[:200])
                if attempt < _QUOTE_RETRIES - 1:
                    await asyncio.sleep(delay)
            if q_payload is None:
                # Still a drop, but a counted one -- and counted by STRIKE, not just by
                # quantity. The strikes matter because the caller gates on the delta band, and
                # a hole there is a contract the engine could have sold and cannot see. A bare
                # count cannot distinguish "we lost 20 far-OTM strikes nobody would sell" from
                # "we lost the three strikes this spread would have been built from".
                dropped += len(batch)
                for _id in batch:
                    try:
                        dropped_strikes.append(float(by_id[_id].get("strike_price")))
                    except (TypeError, ValueError, KeyError):
                        pass
                continue
            for row in ((q_payload.get("data") or {}) or {}).get("results") or []:
                quote = row.get("quote") if isinstance(row, dict) else None
                if quote:
                    quotes.append(quote)

        if dropped:
            logger.warning("[robinhood_mcp] %s: QUOTE_BATCH_DROPPED %d of %d instruments have "
                           "no quote after %d attempts -- this chain has holes in its strike "
                           "grid, not a low quotable ratio.",
                           ticker, dropped, len(ids), _QUOTE_RETRIES)
        logger.debug("[robinhood_mcp] %s: %d instruments, %d quotes",
                     ticker, len(instruments), len(quotes))
        return {"instruments": list(by_id.values()), "quotes": quotes,
                "dropped_instruments": dropped,
                "dropped_strikes": sorted(dropped_strikes),
                "truncated": truncated,
                "truncated_reason": truncated_reason}


# ─────────────────────────────────────────────
# READ-ONLY ENFORCEMENT
# ─────────────────────────────────────────────
#
# This connects to Robinhood's Agentic TRADING MCP server, against a real brokerage account,
# and the OAuth grant is a single broad scope ("internal") -- there is no read-only scope to
# ask for. The server therefore also exposes order-placement tools, and the token minted here
# would very likely be accepted for them.
#
# VEGA's contract is explicit and non-negotiable: this integration is a DATA SOURCE. Every
# order is initiated and executed by a human, in Robinhood's own interface. Nothing here may
# place, modify, or cancel an order.
#
# "Our code only happens to call read tools" is not that guarantee -- it is a property of the
# current call sites, which a later edit, a copied snippet, or a mistaken tool name could
# quietly change. An allowlist is the guarantee: a tool that is not named here cannot be
# invoked through this module at all, and adding one is a deliberate, reviewable act.
READ_ONLY_TOOLS = frozenset({
    "get_option_chains",
    "get_option_quotes",
    "get_option_historicals",
    "get_option_instruments",
    # Added deliberately 2026-08-28. The server grew from 47 to 55 tools overnight and this one
    # appeared with it -- blocked by default until reviewed, which is the point of the list.
    # Read-only by contract: "Get recent news articles for a stock, resolved by ticker symbol."
    # Replaces a NewsAPI free tier that answers 429 to nearly every call.
    "get_equity_news",
})


class WriteToolBlocked(RuntimeError):
    """Raised when anything tries to invoke a non-read-only tool through this module."""


async def _call_read_tool(session, name: str, args: dict):
    """The ONLY path from this module to the MCP server. Refuses anything not allowlisted."""
    if name not in READ_ONLY_TOOLS:
        raise WriteToolBlocked(
            f"Refusing to call Robinhood MCP tool {name!r}: not in READ_ONLY_TOOLS. "
            f"VEGA is a data source; orders are placed by a human in Robinhood. If this tool "
            f"is genuinely read-only, add it to READ_ONLY_TOOLS deliberately."
        )
    return await session.call_tool(name, args)


# ─────────────────────────────────────────────
# Sync wrapper -- fetcher.py is a synchronous module (uses `requests`, not asyncio)
# ─────────────────────────────────────────────

def fetch_chain(ticker: str, server_url: str, option_type: str = "put",
                spot: float = None, min_dte: int = None, max_dte: int = None,
                expirations=None, strikes=None) -> Optional[Dict[str, Any]]:
    """Sync entry point for fetcher.py. Returns None on any failure -- never raises,
    matching the graceful-degradation contract of every other parser in fetcher.py.

    Catches BaseException, not Exception, and that is the whole point of this docstring.
    The MCP client runs its transport inside an anyio task group, and a failure raised in
    there -- including the InteractiveAuthRequired this module raises on purpose -- comes back
    out as `asyncio.CancelledError`. CancelledError derives from BaseException, NOT Exception,
    so the obvious `except Exception` silently did not apply: verified 2026-08-27, an auth
    refusal escaped this function and then escaped fetcher._parse_robinhood_options too,
    straight into the scan. A data source that cannot reach its server must cost its own
    records and nothing else.

    KeyboardInterrupt and SystemExit are re-raised: those are the operator, not the source.
    """
    try:
        return asyncio.run(afetch_chain(server_url, ticker, option_type, spot=spot,
                                        min_dte=min_dte, max_dte=max_dte,
                                        expirations=expirations, strikes=strikes))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        logger.warning(f"[robinhood_mcp] {option_type} fetch failed for {ticker} "
                       f"({type(exc).__name__}: {exc})", exc_info=True)
        return None



def fetch_put_chain(ticker: str, server_url: str, spot: float = None,
                    min_dte: int = None, max_dte: int = None) -> Optional[Dict[str, Any]]:
    """Puts. Thin wrapper over fetch_chain, kept because callers and tests already use it."""
    return fetch_chain(ticker, server_url, "put", spot=spot,
                       min_dte=min_dte, max_dte=max_dte)


def fetch_call_chain(ticker: str, server_url: str, spot: float = None,
                     min_dte: int = None, max_dte: int = None) -> Optional[Dict[str, Any]]:
    """Calls. The bear-call, iron-condor and lottery paths were still on yfinance at 34-48%
    quotable while the put side ran on Robinhood at ~91%; this is the other half."""
    return fetch_chain(ticker, server_url, "call", spot=spot,
                       min_dte=min_dte, max_dte=max_dte)

async def afetch_news(server_url: str, ticker: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Recent articles for one symbol, via the official MCP server.

    Discovered 2026-08-28: the server exposes get_equity_news, and it returns rather more than
    NewsAPI's free tier ever did -- title, publisher, published_at, a preview AND the full
    article body. It arrived in an overnight expansion from 47 tools to 55, blocked by
    READ_ONLY_TOOLS until reviewed, which is what that list is for.

    This matters because NewsAPI's free tier answers 429 to nearly every call (3,688 of them in
    one log), so per-ticker sentiment had been silently running on keyword fallback.
    """
    async with _robinhood_session(server_url) as session:
        result = await _call_read_tool(session, "get_equity_news",
                                       {"symbol": ticker.upper(), "limit": int(limit)})
        payload = _extract_tool_json(result)
        if not isinstance(payload, dict):
            logger.warning("[robinhood_mcp] %s: non-JSON news response: %s",
                           ticker, str(payload)[:160])
            return []
        return ((payload.get("data") or {}).get("articles") or [])


def fetch_news(ticker: str, server_url: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Sync wrapper. Returns [] on any failure -- news is advisory and must never fail a scan."""
    try:
        return asyncio.run(afetch_news(server_url, ticker, limit))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        logger.warning("[robinhood_mcp] news fetch failed for %s (%s: %s)",
                       ticker, type(exc).__name__, exc)
        return []


def list_tools(server_url: str) -> List[Dict[str, Any]]:
    """Sync wrapper around alist_tools -- used by the standalone connection test.

    Unwraps the anyio cancellation so the diagnostic prints the real cause rather than
    "Cancelled via cancel scope 0x...". Deliberately still RAISES: this one is the
    interactive test, where a failure should be loud, not swallowed.
    """
    try:
        return asyncio.run(alist_tools(server_url))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        cause = exc
        seen = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if isinstance(cause, InteractiveAuthRequired):
                raise cause
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
        raise
