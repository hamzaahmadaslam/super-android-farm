"""Who may call the API: token, session cookie + CSRF, rate limit, Host and Origin checks,
security headers, WebSocket authentication."""
from __future__ import annotations

import json
import stat

import pytest
from api_fakes import BASE, WS, Api, api, closes
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from superfarm.api import auth as auth_mod
from superfarm.api.auth import RateLimiter, Sessions, TokenStore, ensure_token, host_allowed, read_token, rotate_token
from superfarm.config import Config


@pytest.fixture
def a(tmp_path):
    with api(tmp_path) as made:
        yield made


def fresh(a: Api, **kw) -> TestClient:
    return TestClient(a.app, base_url=kw.pop("base_url", BASE), raise_server_exceptions=False, **kw)


def login(a: Api, client: TestClient | None = None) -> tuple[TestClient, str]:
    client = client or fresh(a)
    r = client.post("/api/auth/login", json={"token": a.token})
    assert r.status_code == 200, r.text
    return client, r.json()["csrf"]


# ------------------------------------------------------------------------- the token

def test_token_is_created_on_first_run_owner_only(tmp_path):
    with api(tmp_path) as a:
        path = a.farm.paths.secrets / "api-token"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        token = path.read_text().strip()
        assert len(token) >= 43 and token == a.token            # 32 random bytes, urlsafe base64
        assert ensure_token(a.farm.paths) == token               # stable: not regenerated


def test_tokens_differ_between_farms(tmp_path):
    with api(tmp_path / "a") as one, api(tmp_path / "b") as two:
        assert one.token != two.token


def test_health_needs_no_token(a):
    r = a.anonymous.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"]


@pytest.mark.parametrize("path", ["/api/instances", "/api/system", "/api/config", "/api/tasks", "/api/auth/me",
                                  "/api/openapi.json", "/api/automation/flows", "/api/backups", "/api/net/credentials"])
def test_no_token_is_401(a, path):
    r = a.anonymous.get(path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer wrong", "Bearer ", "Basic abc", "wrong", "Bearer " + "x" * 5000])
def test_bad_token_is_401(a, header):
    r = a.anonymous.get("/api/instances", headers={"Authorization": header})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_good_token_is_accepted_whatever_the_scheme_case(a):
    assert a.client.get("/api/instances").status_code == 200
    assert a.anonymous.get("/api/instances", headers={"Authorization": f"bearer {a.token}"}).status_code == 200


def test_token_in_the_query_string_is_not_a_credential(a):
    assert a.anonymous.get("/api/instances", params={"token": a.token}).status_code == 401


def test_rotating_the_token_takes_effect_without_a_restart(a):
    old = a.token
    new = rotate_token(a.farm.paths)
    assert new != old
    assert a.anonymous.get("/api/instances", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert a.anonymous.get("/api/instances", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_token_store_recreates_a_deleted_file(tmp_path):
    with api(tmp_path) as a:
        store = TokenStore(a.farm.paths)
        first = store.current()
        (a.farm.paths.secrets / "api-token").unlink()
        second = store.current()
        assert second and second != first and read_token(a.farm.paths) == second
        assert not store.matches(first) and store.matches(second) and not store.matches("")


def test_comparison_goes_through_a_constant_time_primitive(monkeypatch):
    seen = []
    real = auth_mod.hmac.compare_digest
    monkeypatch.setattr(auth_mod.hmac, "compare_digest", lambda x, y: seen.append((len(x), len(y))) or real(x, y))
    assert auth_mod.same("abc", "abc") and not auth_mod.same("abc", "abcd")
    assert seen == [(32, 32), (32, 32)]          # digests: the length of the secret is not exposed either


# ------------------------------------------------------------------- login and cookies

def test_login_sets_an_httponly_strict_session_cookie(a):
    client = fresh(a)
    r = client.post("/api/auth/login", json={"token": a.token})
    assert r.status_code == 200
    body = r.json()
    cookies = {c.split("=", 1)[0]: c.lower() for c in r.headers.get_list("set-cookie")}
    session = cookies["sf_session"]
    assert "httponly" in session and "samesite=strict" in session and "path=/" in session
    assert "secure" not in session                       # plain HTTP on loopback
    assert "httponly" not in cookies["sf_csrf"] and "samesite=strict" in cookies["sf_csrf"]
    assert body["csrf"] and body["csrf"] == client.cookies["sf_csrf"]
    assert a.token not in r.text and a.token not in str(r.headers)
    assert client.cookies["sf_session"] != a.token


def test_login_cookie_is_secure_over_tls(a):
    client = fresh(a, base_url="https://127.0.0.1")
    r = client.post("/api/auth/login", json={"token": a.token})
    assert all("secure" in c.lower() for c in r.headers.get_list("set-cookie"))


def test_wrong_login_is_401_and_sets_no_cookie(a):
    r = fresh(a).post("/api/auth/login", json={"token": "nope"})
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_login_body_is_strict(a):
    r = fresh(a).post("/api/auth/login", json={"token": a.token, "remember": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert a.token not in r.text                          # a rejected body is never echoed


def test_session_cookie_reads_without_csrf_but_cannot_change_anything_without_it(a):
    client, csrf = login(a)
    assert client.get("/api/instances").status_code == 200
    r = client.post("/api/instances", json={"count": 1})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    assert client.post("/api/instances", json={"count": 1}, headers={"X-SF-CSRF": "wrong"}).status_code == 403
    assert a.farm.store.ids() == []
    r = client.post("/api/instances", json={"count": 1}, headers={"X-SF-CSRF": csrf})
    assert r.status_code == 201 and a.farm.store.ids() == [1]
    assert client.delete("/api/instances/1").status_code == 403
    assert client.patch("/api/config", json={"patch": {"android": {"fps": 30}}}).status_code == 403


def test_csrf_value_of_another_session_is_refused(a):
    one, _ = login(a)
    _, other_csrf = login(a)
    assert one.post("/api/instances", json={"count": 1}, headers={"X-SF-CSRF": other_csrf}).status_code == 403


def test_bearer_token_needs_no_csrf(a):
    assert a.client.post("/api/instances", json={"count": 1}).status_code == 201


def test_me_tells_how_the_caller_is_authenticated(a):
    assert a.client.get("/api/auth/me").json() == {"authenticated": True, "method": "token", "auth_required": True}
    client, csrf = login(a)
    me = client.get("/api/auth/me").json()
    assert me["method"] == "session" and me["csrf"] == csrf and me["expires"] > 0


def test_logout_ends_the_session(a):
    client, _ = login(a)
    session = client.cookies["sf_session"]
    assert client.post("/api/auth/logout").status_code == 200
    assert "sf_session" not in client.cookies
    replay = fresh(a, cookies={"sf_session": session})
    assert replay.get("/api/instances").status_code == 401


def test_a_made_up_session_cookie_is_refused(a):
    assert fresh(a, cookies={"sf_session": "x" * 43}).get("/api/instances").status_code == 401


def test_sessions_expire_and_are_bounded(monkeypatch):
    sessions = Sessions()
    gone = sessions.create(-1)
    live = sessions.create(60)
    assert sessions.get(gone.id) is None and sessions.get(live.id) is live
    assert sessions.get(None) is None and sessions.get("") is None
    sessions.drop(live.id)
    assert sessions.get(live.id) is None
    monkeypatch.setattr(auth_mod, "MAX_SESSIONS", 3)
    made = [sessions.create(60 + n) for n in range(5)]
    assert [sessions.get(s.id) is not None for s in made] == [False, False, True, True, True]


# -------------------------------------------------------------------------- rate limit

def test_rate_limiter_counts_failures_in_a_sliding_minute():
    limiter = RateLimiter(window=60)
    for n in range(3):
        assert limiter.retry_after("a", 3, now=100 + n) == 0
        limiter.fail("a", now=100 + n)
    assert limiter.retry_after("a", 3, now=103) == 57
    assert limiter.retry_after("b", 3, now=103) == 0             # per client address
    assert limiter.retry_after("a", 3, now=160.5) == 0           # the first failure aged out
    limiter.fail("a", now=160.6)                                 # 101, 102 and this one: full again
    assert limiter.retry_after("a", 3, now=160.7) == 1
    limiter.reset("a")
    assert limiter.retry_after("a", 3, now=160.8) == 0


def test_rate_limiter_does_not_grow_without_bound():
    limiter = RateLimiter(max_clients=3)
    for n in range(10):
        limiter.fail(f"client{n}", now=n)
    assert len(limiter._failures) == 3 and "client9" in limiter._failures


def test_failed_logins_lock_the_client_out_for_a_while(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 3}}) as a:
        client = fresh(a)
        for _ in range(3):
            assert client.post("/api/auth/login", json={"token": "guess"}).status_code == 401
        r = client.post("/api/auth/login", json={"token": "guess"})
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(r.headers["retry-after"]) <= 60
        # Only wrong tokens are refused: the right one is compared first and always let in,
        # or ten wrong ones from an address would lock its real owner out.
        assert a.client.get("/api/instances").status_code == 200
        assert client.post("/api/auth/login", json={"token": "guess"}).status_code == 429
        assert client.post("/api/auth/login", json={"token": a.token}).status_code == 200


def test_bad_bearer_tokens_count_towards_the_same_limit(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 2}}) as a:
        for _ in range(2):
            assert a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"}).status_code == 401
        assert a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"}).status_code == 429
        assert fresh(a).post("/api/auth/login", json={"token": "guess"}).status_code == 429
        assert fresh(a).post("/api/auth/login", json={"token": a.token}).status_code == 200


def test_a_session_is_not_affected_by_the_lockout(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 1}}) as a:
        client, _ = login(a)
        assert fresh(a).post("/api/auth/login", json={"token": "guess"}).status_code == 401
        assert fresh(a).post("/api/auth/login", json={"token": "guess"}).status_code == 429
        assert client.get("/api/instances").status_code == 200


def test_a_successful_login_clears_the_count(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 3}}) as a:
        for _ in range(2):
            fresh(a).post("/api/auth/login", json={"token": "guess"})
        login(a)
        for _ in range(2):
            assert fresh(a).post("/api/auth/login", json={"token": "guess"}).status_code == 401


# ---------------------------------------------------------------------- Host and Origin

@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:7700", True), ("localhost:7700", True), ("LOCALHOST", True), ("[::1]:7700", True), ("127.0.0.1", True),
    ("evil.example", False), ("evil.example:7700", False), ("127.0.0.1.evil.example", False), ("", False),
    ("192.168.1.20:7700", False), ("localhost.evil.example", False),
])
def test_host_header_allow_list_on_a_loopback_daemon(host, ok):
    assert host_allowed(host, Config()) is ok


def test_host_allow_list_follows_the_configuration():
    named = Config.model_validate({"server": {"host": "farm.lan"}})
    assert host_allowed("farm.lan:7700", named) and not host_allowed("other.lan", named)
    assert not host_allowed("192.168.1.20", named)
    everywhere = Config.model_validate({"server": {"host": "0.0.0.0"}})
    # Listening on every interface: its addresses cannot be listed, and an address cannot be rebound.
    assert host_allowed("192.168.1.20:7700", everywhere) and host_allowed("[fe80::1]:7700", everywhere)
    assert not host_allowed("evil.example", everywhere)
    proxied = Config.model_validate({"server": {"allowed_origins": ["https://farm.example.org"]}})
    assert host_allowed("farm.example.org", proxied) and not host_allowed("example.org", proxied)


def test_requests_for_an_unknown_host_are_refused_before_anything_else(a):
    for path in ("/api/health", "/api/instances", "/"):
        r = a.client.get(path, headers={"Host": "evil.example"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert a.client.get("/api/health", headers={"Host": "localhost:7700"}).status_code == 200


def test_cross_origin_changes_are_refused_even_with_a_token(a):
    r = a.client.post("/api/instances", json={"count": 1}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert a.client.post("/api/instances", json={"count": 1}, headers={"Origin": "null"}).status_code == 403
    assert a.farm.store.ids() == []
    assert a.client.post("/api/instances", json={"count": 1}, headers={"Origin": "http://127.0.0.1"}).status_code == 201


def test_configured_origins_may_call_and_get_cors_headers(tmp_path):
    with api(tmp_path, {"server": {"allowed_origins": ["https://farm.example.org"]}}) as a:
        r = a.client.post("/api/instances", json={"count": 1}, headers={"Origin": "https://farm.example.org"})
        assert r.status_code == 201 and r.headers["access-control-allow-origin"] == "https://farm.example.org"
        r = a.client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in r.headers


def test_without_configured_origins_there_are_no_cors_headers(a):
    r = a.client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    r = a.anonymous.options("/api/instances", headers={"Origin": "https://evil.example",
                                                       "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers


# --------------------------------------------------------------------- security headers

@pytest.mark.parametrize("path", ["/api/health", "/api/instances", "/api/nope", "/"])
def test_every_response_carries_the_security_headers(a, path):
    r = a.anonymous.get(path)
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    if path.startswith("/api/"):
        assert r.headers["cache-control"] == "no-store"


def test_an_unexpected_error_is_a_500_that_says_nothing(a, monkeypatch, caplog):
    def boom():
        raise ZeroDivisionError("secret internal detail")
    monkeypatch.setattr(a.core.mgr, "list_status", boom)
    r = a.client.get("/api/instances")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal", "message": "internal error; the daemon log has the details"}}
    assert "secret internal detail" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "secret internal detail" in caplog.text              # the operator can find it in the log


# ------------------------------------------------------------------------ auth disabled

def test_with_auth_off_everything_is_open_but_cross_origin_changes_are_still_refused(tmp_path):
    with api(tmp_path, {"server": {"auth_required": False}}) as a:
        assert a.anonymous.get("/api/instances").status_code == 200
        assert a.anonymous.get("/api/auth/me").json()["method"] == "none"
        assert a.anonymous.post("/api/auth/login", json={"token": "anything"}).json()["auth_required"] is False
        assert a.anonymous.post("/api/instances", json={"count": 1}).status_code == 201
        # The one thing a hostile web page could do to an open loopback daemon: make the
        # browser send a request. The Origin it carries gives it away.
        r = a.anonymous.post("/api/instances/1/start", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        assert a.anonymous.get("/api/health", headers={"Host": "evil.example"}).status_code == 403


# ----------------------------------------------------------------------------- sockets

def test_websocket_accepts_the_session_cookie(a):
    client, _ = login(a)
    with client.websocket_connect(WS + "/api/events") as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello"}


def test_websocket_accepts_a_bearer_header(a):
    with a.client.websocket_connect(WS + "/api/events") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"


def test_websocket_accepts_the_token_as_first_message(a):
    with fresh(a).websocket_connect(WS + "/api/events") as ws:
        ws.send_text(json.dumps({"auth": a.token}))
        assert json.loads(ws.receive_text())["type"] == "hello"


@pytest.mark.parametrize("first", ['{"auth": "wrong"}', '{"hello": 1}', "not json", '["auth"]', '{"auth": 5}'])
def test_websocket_closes_on_a_bad_first_message(a, first):
    with fresh(a).websocket_connect(WS + "/api/events") as ws:
        ws.send_text(first)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1008


def test_websocket_refuses_credentials_in_the_url(a):
    # Even the right token: accepting it would teach clients to put it where it gets logged.
    with pytest.raises(WebSocketDisconnect) as closed:
        with fresh(a).websocket_connect(f"{WS}/api/events?token={a.token}"):
            pass
    assert closed.value.code == 1008


def test_websocket_refuses_a_wrong_bearer_and_other_origins_and_hosts(a):
    for headers in ({"Authorization": "Bearer wrong"}, {"Origin": "http://evil.example", "Authorization": f"Bearer {a.token}"},
                    {"Host": "evil.example", "Authorization": f"Bearer {a.token}"}):
        with pytest.raises(WebSocketDisconnect):
            with fresh(a).websocket_connect(WS + "/api/events", headers=headers):
                pass


def test_websocket_first_message_guesses_are_rate_limited(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 2}}) as a:
        for _ in range(2):
            with fresh(a).websocket_connect(WS + "/api/events") as ws:
                ws.send_text('{"auth": "guess"}')
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_text()
        with fresh(a).websocket_connect(WS + "/api/events") as ws:
            ws.send_text('{"auth": "guess"}')
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.reason == "rate_limited"
        with fresh(a).websocket_connect(WS + "/api/events") as ws:
            ws.send_text(json.dumps({"auth": a.token}))    # the right token is let in all the same
            assert json.loads(ws.receive_text()) == {"type": "hello"}


def test_websocket_without_authentication_times_out(a, monkeypatch):
    from superfarm.api import ws as ws_mod
    monkeypatch.setattr(ws_mod, "AUTH_TIMEOUT", 0.05)
    with fresh(a).websocket_connect(WS + "/api/events") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
        assert closed.value.code == 1008


# ------------------------------------------- authentication follows the bound address

def assert_locked(a: Api) -> None:
    """Nothing is served without credentials, whatever the config says by now."""
    assert a.farm.config.server.auth_required is False            # the setting itself was saved
    assert a.app.state.auth.required is True
    assert a.anonymous.get("/api/instances").status_code == 401
    assert a.anonymous.post("/api/instances", json={"count": 1}).status_code == 401
    assert a.anonymous.post("/api/shell", json={"selector": "all", "argv": ["id"], "root": True}).status_code == 401
    assert a.anonymous.post("/api/auth/login", json={"token": "anything"}).status_code == 401
    assert a.anonymous.get("/api/auth/me").status_code == 401
    with fresh(a).websocket_connect(WS + "/api/events") as ws:
        ws.send_text('{"hello": 1}')
        assert closes(ws) == 1008
    me = a.client.get("/api/auth/me").json()
    assert me["method"] == "token" and me["auth_required"] is True      # the panel is told the truth
    client, csrf = login(a)
    assert csrf and client.get("/api/instances").status_code == 200


def test_one_save_cannot_leave_a_listener_that_is_not_loopback_open(tmp_path):
    # server.host only moves at a restart; auth_required applies at once. Saved together
    # ("back to local-only, no login") the socket is still on the old address.
    with api(tmp_path, {"server": {"host": "192.0.2.10"}}) as a:
        assert a.anonymous.get("/api/instances").status_code == 401
        r = a.client.patch("/api/config", json={"patch": {"server": {"host": "127.0.0.1", "auth_required": False}}})
        assert r.status_code == 200 and r.json()["changed"] == ["server.auth_required", "server.host"]
        assert r.json()["restart_daemon"] == ["server.auth_required", "server.host"]
        assert_locked(a)


def test_a_host_given_on_the_command_line_keeps_authentication_on(tmp_path):
    with api(tmp_path, listen_host="192.0.2.10") as a:           # `sf serve --host`, config still says 127.0.0.1
        assert a.app.state.auth.listen_host == "192.0.2.10"
        r = a.client.patch("/api/config", json={"patch": {"server": {"auth_required": False}}})
        assert r.status_code == 200 and r.json()["restart_daemon"] == ["server.auth_required"]
        assert_locked(a)
        # ... and the address it was told to listen on is one it answers to.
        assert a.client.get("/api/instances", headers={"Host": "192.0.2.10:7700"}).status_code == 200
        assert a.client.get("/api/instances", headers={"Host": "evil.example"}).status_code == 403


def test_a_restored_config_cannot_open_a_listener_that_is_not_loopback(tmp_path):
    with api(tmp_path, {"server": {"host": "192.0.2.10"}}) as a:
        a.farm.paths.config.write_text("server:\n  auth_required: false\n")       # what a farm restore may put back
        a.farm.reload_config()
        assert_locked(a)


def test_on_loopback_turning_auth_off_still_takes_effect_at_once(a):
    r = a.client.patch("/api/config", json={"patch": {"server": {"auth_required": False}}})
    assert r.status_code == 200 and r.json()["restart_daemon"] == []
    assert a.app.state.auth.required is False
    assert a.anonymous.get("/api/instances").status_code == 200
    assert a.anonymous.get("/api/auth/me").json() == {"authenticated": True, "method": "none", "auth_required": False}


# --------------------------------------------------- rotating the token signs everyone out

def test_rotating_the_token_ends_browser_sessions(a):
    client, csrf = login(a)
    assert client.get("/api/instances").status_code == 200
    new = rotate_token(a.farm.paths)
    # No bearer request in between: the cookie itself has to notice.
    assert client.get("/api/instances").status_code == 401
    assert client.post("/api/instances", json={"count": 1}, headers={"X-SF-CSRF": csrf}).status_code == 401
    assert a.core.store.ids() == []
    a.token = new
    again, _ = login(a)
    assert again.get("/api/instances").status_code == 200           # a login made with the new token holds


@pytest.mark.parametrize("how", ["session", "bearer", "first-message"])
def test_rotating_the_token_closes_open_sockets(a, monkeypatch, how):
    from superfarm.api import ws as ws_mod
    monkeypatch.setattr(ws_mod, "AUTH_RECHECK", 0.05)
    client = login(a)[0] if how == "session" else a.client if how == "bearer" else fresh(a)
    with client.websocket_connect(WS + "/api/events") as ws:
        if how == "first-message":
            ws.send_text(json.dumps({"auth": a.token}))
        assert json.loads(ws.receive_text()) == {"type": "hello"}
        new = rotate_token(a.farm.paths)
        assert closes(ws) == 1008
    with fresh(a).websocket_connect(WS + "/api/events", headers={"Authorization": f"Bearer {new}"}) as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello"}
        assert closes(ws, 0.5) is None                              # authenticated after the rotation: stays


def test_logging_out_closes_that_sessions_sockets_and_no_others(a, monkeypatch):
    from superfarm.api import ws as ws_mod
    monkeypatch.setattr(ws_mod, "AUTH_RECHECK", 0.05)
    leaving, _ = login(a)
    staying, _ = login(a)
    with leaving.websocket_connect(WS + "/api/events") as gone, staying.websocket_connect(WS + "/api/events") as kept:
        assert json.loads(gone.receive_text()) == json.loads(kept.receive_text()) == {"type": "hello"}
        fresh(a, cookies=dict(leaving.cookies)).post("/api/auth/logout")
        assert closes(gone) == 1008
        assert closes(kept, 0.5) is None


def test_turning_auth_on_closes_the_sockets_opened_without_it(tmp_path, monkeypatch):
    from superfarm.api import ws as ws_mod
    monkeypatch.setattr(ws_mod, "AUTH_RECHECK", 0.05)
    with api(tmp_path, {"server": {"auth_required": False}}) as a:
        with a.anonymous.websocket_connect(WS + "/api/events") as ws:
            assert json.loads(ws.receive_text()) == {"type": "hello"}
            assert a.anonymous.patch("/api/config", json={"patch": {"server": {"auth_required": True}}}).status_code == 200
            assert closes(ws) == 1008


# ------------------------------------------------ the right token is never locked out

def test_the_right_token_is_accepted_while_wrong_ones_are_locked_out(tmp_path):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 3}}) as a:
        for _ in range(3):
            assert a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"}).status_code == 401
        refused = a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"})
        assert refused.status_code == 429 and 1 <= int(refused.headers["retry-after"]) <= 60
        assert fresh(a).post("/api/auth/login", json={"token": "guess"}).status_code == 429
        # Same address, right token: in at once, by header, by login and on a socket.
        assert a.client.get("/api/instances").status_code == 200
        assert fresh(a).post("/api/auth/login", json={"token": a.token}).status_code == 200
        with fresh(a).websocket_connect(WS + "/api/events") as ws:
            ws.send_text(json.dumps({"auth": a.token}))
            assert json.loads(ws.receive_text()) == {"type": "hello"}
        # ... which does not lift the lockout for whoever is guessing (only a login does that).
        assert a.app.state.auth.limiter.retry_after("testclient", 3) == 0       # the login above reset it
        for _ in range(3):
            a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"})
        assert a.client.get("/api/instances").status_code == 200
        assert a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess"}).status_code == 429


def test_wrong_tokens_on_read_requests_leave_a_bounded_trace_in_the_daemon_log(tmp_path, caplog):
    with api(tmp_path, {"server": {"login_attempts_per_minute": 3}}) as a:
        with caplog.at_level("WARNING", logger="superfarm.api"):
            for _ in range(40):
                a.anonymous.get("/api/instances", headers={"Authorization": "Bearer guess-on-a-read"})
        lines = [r.getMessage() for r in caplog.records if "token" in r.getMessage()]
        assert 1 <= len(lines) <= 3                                 # one per counted failure, none per refusal
        assert all("testclient" in line for line in lines)          # who, whatever the method
        assert "refused" in lines[-1]                               # the last one says the address is now locked out
        assert "guess-on-a-read" not in caplog.text and a.token not in caplog.text
        assert a.audit_lines() == []                                # reads stay out of the audit log
        # Many addresses cannot fill the log either: there is one budget for all of them.
        caplog.clear()
        with caplog.at_level("WARNING", logger="superfarm.api"):
            for n in range(500):
                with pytest.raises(auth_mod.ApiError):
                    a.app.state.auth.check_token(f"2001:db8::{n:x}", "guess")
        lines = [r.getMessage() for r in caplog.records if "token" in r.getMessage()]
        assert len(lines) == auth_mod.WRONG_TOKEN_LOG_LINES - 3 and "not logged for a minute" in lines[-1]


# ------------------------------------------------------------ frames nobody should send

def test_a_binary_first_frame_is_closed_cleanly(a, caplog):
    with caplog.at_level("WARNING"):
        with fresh(a).websocket_connect(WS + "/api/events") as ws:
            ws.send_bytes(b"\x00")
            assert closes(ws) == 1008
    assert [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")] == []
    assert "Traceback" not in caplog.text
