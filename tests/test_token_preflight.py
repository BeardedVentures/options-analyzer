"""Token pre-flight: the thing that makes unattended operation actually unattended.

WHY THESE TESTS EXIST AT ALL. The MCP SDK already had a refresh path, it had passing tests
upstream, and it could never run: OAuthContext.token_expiry_time is None after loading a
cached token, so is_token_valid() returns True and the refresh branch is dead code. The
lesson is not "the SDK is buggy" -- it is that a credential-renewal path nothing exercises is
indistinguishable from one that works, right up until the morning it doesn't.

So each test here forces the branch it is checking to actually execute, and asserts on an
observable effect (file contents, a raised exception, a journal line), never on "it returned
without error".
"""
import json
import time

import pytest

from data import robinhood_mcp as R


# ── Endpoint discovery ────────────────────────────────────────────────────────

def test_endpoint_is_discovered_from_metadata_not_hardcoded(monkeypatch):
    """The SDK's bug was a hardcoded {origin}/token. Reproducing it with a different
    constant would be the same defect wearing a new hat, so the endpoint must come from the
    server's own metadata document."""
    seen = []

    class _Resp:
        def __init__(self, body):
            self._body = body
        def read(self):
            return json.dumps(self._body).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        return _Resp({"token_endpoint": "https://elsewhere.example/oauth2/token/"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ep = R._discover_token_endpoint("https://agent.example.com/mcp/trading")

    assert ep == "https://elsewhere.example/oauth2/token/"
    # RFC 8414 inserts the path INTO the well-known segment; Robinhood scopes per path, so the
    # path-aware form must be tried FIRST or a per-path server answers with the wrong document.
    assert seen[0] == "https://agent.example.com/.well-known/oauth-authorization-server/mcp/trading"


def test_discovery_falls_back_to_the_root_wellknown(monkeypatch):
    """A server that does not scope metadata per path must still resolve."""
    class _Resp:
        def read(self):
            return json.dumps({"token_endpoint": "https://t.example/tok"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        if url.endswith("/mcp/trading"):
            raise OSError("404")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert R._discover_token_endpoint("https://a.example/mcp/trading") == "https://t.example/tok"


def test_discovery_returns_none_rather_than_guessing(monkeypatch):
    """No metadata means NO endpoint. Falling back to a guessed URL is how the SDK got a 404."""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=None: (_ for _ in ()).throw(OSError("down")))
    assert R._discover_token_endpoint("https://a.example/mcp/trading") is None


# ── Expiry accounting ─────────────────────────────────────────────────────────

def _write_token_file(tmp_path, monkeypatch, *, expires_in=3600, obtained_at=None,
                      refresh="rt-old", client_id="cid"):
    p = tmp_path / ".robinhood_mcp_tokens.json"
    raw = {"tokens": {"access_token": "at-old", "token_type": "Bearer",
                      "expires_in": expires_in, "scope": "internal", "refresh_token": refresh},
           "client_info": {"client_id": client_id}}
    if obtained_at is not None:
        raw["obtained_at"] = obtained_at
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(R, "_TOKEN_PATH", p)
    return p


def test_expiry_prefers_obtained_at_over_file_mtime(tmp_path, monkeypatch):
    """mtime is the FALLBACK, not the design: a copied file carries a wrong mtime and a right
    token, which would silently mis-date the credential."""
    stamped = time.time() - 100
    _write_token_file(tmp_path, monkeypatch, expires_in=1000, obtained_at=stamped)
    assert R.token_expiry_epoch() == pytest.approx(stamped + 1000, abs=1)


def test_expiry_falls_back_to_mtime_for_files_written_before_this_landed(tmp_path, monkeypatch):
    p = _write_token_file(tmp_path, monkeypatch, expires_in=1000, obtained_at=None)
    assert R.token_expiry_epoch() == pytest.approx(p.stat().st_mtime + 1000, abs=2)


# ── The renewal decision ──────────────────────────────────────────────────────

def test_a_fresh_token_is_left_alone(tmp_path, monkeypatch):
    """The pre-flight runs every cycle. If it refreshed every time it would burn a rotation
    per run and turn a working credential into churn."""
    _write_token_file(tmp_path, monkeypatch, expires_in=86400 * 10, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint",
                        lambda *a, **k: pytest.fail("must not even look up the endpoint"))
    res = R.ensure_fresh_token("https://a.example/mcp/trading")
    assert res["status"] == "ok"
    assert res["seconds_remaining"] > 0


def test_a_near_expiry_token_is_refreshed_and_persisted(tmp_path, monkeypatch):
    p = _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: "https://t.example/tok")
    _fake_token_response(monkeypatch, {"access_token": "at-new", "expires_in": 700,
                                       "refresh_token": "rt-new"})

    res = R.ensure_fresh_token("https://a.example/mcp/trading")

    assert res["status"] == "refreshed" and res["rotated"] is True
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk["tokens"]["access_token"] == "at-new"
    assert on_disk["tokens"]["refresh_token"] == "rt-new"
    assert isinstance(on_disk["obtained_at"], (int, float))


def test_an_omitted_refresh_token_keeps_the_old_one(tmp_path, monkeypatch):
    """RFC 6749 6: the response MAY omit refresh_token, meaning 'keep using the one you have'.
    Blanking it there would destroy the credential on a SUCCESSFUL refresh."""
    p = _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: "https://t.example/tok")
    _fake_token_response(monkeypatch, {"access_token": "at-new", "expires_in": 700})

    res = R.ensure_fresh_token("https://a.example/mcp/trading")

    assert res["rotated"] is False
    assert json.loads(p.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "rt-old"


# ── Failure has to be loud ────────────────────────────────────────────────────

def test_a_rejected_refresh_is_journalled_and_not_raised(tmp_path, monkeypatch):
    """A dead credential degrades this source and nothing else -- same contract as every other
    data-source failure in the cycle. But it MUST leave a durable record."""
    _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: "https://t.example/tok")
    _fake_token_response(monkeypatch, {"error": "invalid_grant"}, status=400)

    events = []
    monkeypatch.setattr(R, "_record_auth_event", events.append)

    res = R.ensure_fresh_token("https://a.example/mcp/trading")

    assert res["status"] == "refresh_failed" and res["http"] == 400
    assert events and events[0]["severity"] == "critical"


def test_a_refresh_that_cannot_be_saved_RAISES(tmp_path, monkeypatch):
    """THE important one. Robinhood rotates: the moment the server answers 200 the old refresh
    token is dead. If the write then fails and we returned quietly, disk holds a dead
    credential, the only live one dies with the process, and the symptom appears days later
    looking exactly like an ordinary expiry. That must be impossible to miss."""
    p = _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: "https://t.example/tok")
    _fake_token_response(monkeypatch, {"access_token": "at-new", "expires_in": 700,
                                       "refresh_token": "rt-new"})

    # A write that silently does not land -- a full disk, a lock, an antivirus hold.
    monkeypatch.setattr(R, "_record_auth_event", lambda e: None)
    import durable_write
    monkeypatch.setattr(durable_write, "atomic_write_text", lambda *a, **k: None)
    monkeypatch.setattr(type(p), "write_text", lambda self, *a, **k: None)

    with pytest.raises(R.TokenRefreshError) as exc:
        R.ensure_fresh_token("https://a.example/mcp/trading")
    assert "FAILED TO PERSIST" in str(exc.value)


def test_the_persist_check_can_actually_fail(tmp_path, monkeypatch):
    """Guards the test above from being a tautology: prove the same call SUCCEEDS when the
    write is allowed to land, so the assertion is measuring the write and not the mock."""
    p = _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: "https://t.example/tok")
    _fake_token_response(monkeypatch, {"access_token": "at-new", "expires_in": 700,
                                       "refresh_token": "rt-new"})
    monkeypatch.setattr(R, "_record_auth_event", lambda e: None)

    res = R.ensure_fresh_token("https://a.example/mcp/trading")
    assert res["status"] == "refreshed"
    assert json.loads(p.read_text(encoding="utf-8"))["tokens"]["access_token"] == "at-new"


def test_no_endpoint_is_reported_rather_than_guessed(tmp_path, monkeypatch):
    _write_token_file(tmp_path, monkeypatch, expires_in=60, obtained_at=time.time())
    monkeypatch.setattr(R, "_discover_token_endpoint", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(R, "_record_auth_event", events.append)

    res = R.ensure_fresh_token("https://a.example/mcp/trading")

    assert res["status"] == "no_endpoint"
    assert events and events[0]["severity"] == "critical"


# ── helper ────────────────────────────────────────────────────────────────────

def _fake_token_response(monkeypatch, body, status=200):
    class _Resp:
        def __init__(self):
            self.status = status
        def read(self):
            return json.dumps(body).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
