"""Who may call the API.

Two ways in, both proving knowledge of the same secret (the API token):

    Authorization: Bearer <token>      scripts and the CLI
    session cookie                     the panel, after POST /api/auth/login

The token is 32 random bytes, created on first run and kept in `secrets/api-token` (0600).
It is never put in a URL, a log line or an error message. Comparisons are constant-time.
Whoever holds it has root on this machine (the daemon controls Docker and Androids are
privileged containers): it is to be treated like a root password.

Whether a request needs credentials at all is decided from the address the daemon is bound
to, captured once when the app is built (see Auth.required). The config file can change
while the socket stays where it is; an open socket on a non-loopback address never serves
anybody anonymous.

Replacing the token (`sf token --rotate`) ends every browser session and closes every
WebSocket that was let in before it: see Auth.still_valid.

A browser session is a cookie the page cannot read (HttpOnly, SameSite=Strict). Because the
browser attaches it by itself, every state-changing request made with it must also carry the
session's CSRF value in `X-SF-CSRF`: another site can make the browser send the cookie, but
it cannot read the value to put in the header.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection, Request

from ..config import Config
from ..paths import Paths
from ..util.files import write_secret
from .errors import ApiError

log = logging.getLogger("superfarm.api")

TOKEN_FILE = "api-token"       # noqa: S105 - a file name
SESSION_COOKIE = "sf_session"
CSRF_COOKIE = "sf_csrf"
CSRF_HEADER = "x-sf-csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})      # noqa: S104 - compared against, never bound here
MAX_SESSIONS = 1000            # a login needs the token, so this only bounds a scripted client that never logs out
WRONG_TOKEN_LOG_LINES = 30     # per minute, all addresses together: guessing must not be able to fill the daemon log


# ------------------------------------------------------------------- the token

def token_path(paths: Paths):
    return paths.secrets / TOKEN_FILE


def read_token(paths: Paths) -> str | None:
    try:
        text = token_path(paths).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def rotate_token(paths: Paths) -> str:
    """Replace the token. The old one stops working at once, and a running daemon signs
    out every browser session and closes every socket opened before the change (it notices
    on the next request, and within a few seconds on an idle socket)."""
    token = secrets.token_urlsafe(32)
    write_secret(token_path(paths), token + "\n")
    return token


def ensure_token(paths: Paths) -> str:
    """The farm's API token, created on first use."""
    return read_token(paths) or rotate_token(paths)


def same(a: str, b: str) -> bool:
    """Constant-time equality. Both sides are hashed first so that neither the length of
    the secret nor how much of a guess was right shows in the timing."""
    return hmac.compare_digest(hashlib.sha256(a.encode()).digest(), hashlib.sha256(b.encode()).digest())


class TokenStore:
    """The token as the daemon sees it. Re-read when the file changes, so `sf token --rotate`
    takes effect without a restart."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._token = ""
        ensure_token(paths)

    def current(self) -> str:
        path = token_path(self.paths)
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            if stamp is None or stamp != self._stamp:
                self._token = ensure_token(self.paths) if stamp is None else (read_token(self.paths) or "")
                try:
                    st = path.stat()
                    self._stamp = (st.st_mtime_ns, st.st_size)
                except OSError:
                    self._stamp = None
            return self._token

    def matches(self, candidate: str) -> bool:
        token = self.current()
        return bool(token) and bool(candidate) and same(candidate, token)


# -------------------------------------------------------------------- sessions

@dataclass
class Session:
    id: str
    csrf: str
    expires: float


class Sessions:
    """Browser logins. Kept in memory: a daemon restart signs every browser out, which is the
    safe side of the trade against writing session secrets to disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def create(self, lifetime: float) -> Session:
        session = Session(id=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32), expires=time.time() + lifetime)
        with self._lock:
            now = time.time()
            for key in [k for k, s in self._sessions.items() if s.expires <= now]:
                del self._sessions[key]
            while len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda k: self._sessions[k].expires)]
            self._sessions[_key(session.id)] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(_key(session_id))
            if session and session.expires <= time.time():
                del self._sessions[_key(session_id)]
                return None
            return session

    def drop(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(_key(session_id), None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def _key(session_id: str) -> str:
    # Looked up by digest: a dict lookup on the raw value would compare it byte by byte.
    return hashlib.sha256(session_id.encode()).hexdigest()


# ------------------------------------------------------------------ rate limit

class RateLimiter:
    """Failed token presentations per client address in the last minute."""

    def __init__(self, window: float = 60.0, max_clients: int = 4096):
        self.window, self.max_clients = window, max_clients
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, client: str, now: float) -> deque[float]:
        q = self._failures.get(client)
        if q is None:
            return deque()
        while q and now - q[0] >= self.window:
            q.popleft()
        if not q:
            del self._failures[client]
        return q

    def retry_after(self, client: str, limit: int, now: float | None = None) -> int:
        """0 when the client may try; otherwise the seconds until it may."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._recent(client, now)
            if len(q) < limit:
                return 0
            return max(1, int(self.window - (now - q[0]) + 0.999))

    def fail(self, client: str, now: float | None = None) -> int:
        """Count one failure. Returns how many this client has in the window now."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if client not in self._failures and len(self._failures) >= self.max_clients:
                # Full of addresses: forget the one that failed longest ago rather than grow.
                del self._failures[min(self._failures, key=lambda k: self._failures[k][-1])]
            q = self._failures.setdefault(client, deque())
            q.append(now)
            return len(self._recent(client, now))

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


# ---------------------------------------------------------------- host / origin

def _host_only(value: str) -> str:
    """`example.org:7700` -> `example.org`; `[::1]:7700` -> `::1`. Lower-case."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def allowed_hosts(config: Config, listen_host: str | None = None) -> set[str]:
    hosts = set(LOOPBACK_NAMES)
    for bound in (config.server.host, listen_host):
        if bound and bound not in WILDCARD_HOSTS:
            hosts.add(_host_only(bound))
    for origin in config.server.allowed_origins:
        host = urlsplit(origin).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def host_allowed(host_header: str, config: Config, listen_host: str | None = None) -> bool:
    """Is this Host header one the daemon answers to? `listen_host` is the address it is
    bound to when that is not server.host (`sf serve --host`).

    The check exists because of DNS rebinding: a page on evil.example can have its own name
    re-resolved to 127.0.0.1 and would then talk to this API as "same origin". The browser
    still sends `Host: evil.example`, so refusing names nobody configured closes that door.
    An IP address cannot be rebound; when the daemon listens on every interface its own
    addresses cannot be listed in advance, so any literal address is accepted then.
    """
    host = _host_only(host_header)
    if not host:
        return False
    if host in allowed_hosts(config, listen_host):
        return True
    return (config.server.host in WILDCARD_HOSTS or listen_host in WILDCARD_HOSTS) and _is_ip(host)


def origin_allowed(conn: HTTPConnection, config: Config) -> bool:
    """True unless the request names an Origin that is neither this server nor configured.

    Browsers attach Origin to cross-site requests that change something and to every
    WebSocket handshake. Clients that are not browsers send none, and have nothing to forge."""
    origin = conn.headers.get("origin")
    if not origin:
        return True
    if origin in config.server.allowed_origins:
        return True
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc.lower() == conn.headers.get("host", "").lower()


# ---------------------------------------------------------------- the decision

@dataclass
class Identity:
    method: str                    # none (auth is off) | token | session
    client: str
    session: Session | None = None
    epoch: int = 0                 # which token it was checked against (see Auth.still_valid)


def client_of(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "unknown"


def bearer_of(conn: HTTPConnection) -> str | None:
    header = conn.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


class Auth:
    """Everything the app needs to decide about one request. One per app."""

    def __init__(self, farm, listen_host: str | None = None) -> None:
        self.farm = farm
        # The address the daemon is bound to. Fixed for the life of the app: server.host in
        # the config can be changed (or restored) while the socket stays where it is.
        self.listen_host = listen_host or farm.config.server.host
        self.tokens = TokenStore(farm.paths)
        self.sessions = Sessions()
        self.limiter = RateLimiter()
        self._logged = RateLimiter()                # the same sliding minute, counting log lines instead of failures
        self.epoch = 0                              # goes up each time the token is replaced
        self._seen = self.tokens.current()
        self._sync_lock = threading.Lock()

    @property
    def config(self) -> Config:
        return self.farm.config

    @property
    def required(self) -> bool:
        """Must a caller authenticate? Yes unless server.auth_required is off AND the daemon
        is bound to a loopback address. The one place this is decided: a settings save, a
        restored config and `sf serve --host` all end up here."""
        return self.config.server.auth_required or self.listen_host not in LOOPBACK_NAMES

    def _limit(self) -> int:
        return self.config.server.login_attempts_per_minute

    def _sync(self) -> None:
        """Notice a replaced token (one stat() of the token file). Everything let in with the
        old one is signed out: sessions are dropped here, sockets ask still_valid()."""
        token = self.tokens.current()
        with self._sync_lock:
            if token != self._seen:
                self._seen = token
                self.epoch += 1
                self.sessions.clear()
                log.warning("the API token was replaced: every browser session and open socket is signed out")

    def check_token(self, client: str, candidate: str) -> None:
        """Accept a presented token or raise.

        The comparison comes first, so the right token is always accepted: wrong ones from
        the same address (another local process, other users behind one proxy) cannot lock
        its holder out. Only failures are counted and limited. That throttles guessing and
        the noise it makes; it is not what protects the token, which is 256 random bits."""
        self._sync()
        if self.tokens.matches(candidate):
            return
        limit = self._limit()
        wait = self.limiter.retry_after(client, limit)
        if wait:
            raise ApiError(429, "rate_limited", "too many failed attempts; try again shortly",
                           headers={"Retry-After": str(wait)})
        count = self.limiter.fail(client)
        # The only trace a wrong token on a read request leaves (the audit log skips reads).
        # Bounded twice: `limit` lines per address and minute, since the refusals above are
        # not logged, and WRONG_TOKEN_LOG_LINES a minute over all addresses.
        if not self._logged.retry_after("", WRONG_TOKEN_LOG_LINES):
            last = self._logged.fail("") >= WRONG_TOKEN_LOG_LINES
            log.warning("wrong API token from %s (%d in the last minute)%s%s", client, count,
                        "; further wrong tokens from this address are refused for a minute" if count >= limit else "",
                        "; more of these are not logged for a minute" if last else "")
        raise ApiError(401, "unauthorized", "the token is not valid")

    def identify(self, conn: HTTPConnection) -> Identity:
        """Who is calling (HTTP request or WebSocket handshake). Raises ApiError 401/429."""
        client = client_of(conn)
        if not self.required:
            return Identity("none", client)
        self._sync()                               # before the cookie is looked up: a replaced token ends sessions
        token = bearer_of(conn)
        if token is not None:
            self.check_token(client, token)
            return Identity("token", client, epoch=self.epoch)
        session = self.sessions.get(conn.cookies.get(SESSION_COOKIE))
        if session is not None:
            return Identity("session", client, session, epoch=self.epoch)
        raise ApiError(401, "unauthorized", "authentication required: send `Authorization: Bearer <token>` "
                                            "or log in (the token is shown by `sf token`)",
                       headers={"WWW-Authenticate": "Bearer"})

    def authorize(self, request: Request) -> Identity:
        """identify() plus the cross-site checks for requests that change something."""
        identity = self.identify(request)
        request.state.identity = identity          # before the checks below: the audit line of a refusal says who
        if request.method not in SAFE_METHODS:
            if not origin_allowed(request, self.config):
                raise ApiError(403, "forbidden", "cross-origin request refused")
            if identity.method == "session":
                sent = request.headers.get(CSRF_HEADER, "")
                if not sent or not same(sent, identity.session.csrf):       # type: ignore[union-attr]
                    raise ApiError(403, "csrf", "missing or wrong X-SF-CSRF header for a cookie-authenticated request")
        return identity

    def login(self, client: str, token: str) -> Session:
        self.check_token(client, token)
        self.limiter.reset(client)
        return self.sessions.create(self.config.server.session_hours * 3600)

    def still_valid(self, identity: Identity) -> bool:
        """Is what let a long-lived connection in still good? A WebSocket is checked once
        when it opens; it asks this every few seconds so that it ends when its credential
        does: the token was replaced, the session was logged out or expired, or
        authentication was switched on after an anonymous socket was opened."""
        if identity.method == "none":
            return not self.required
        self._sync()
        if identity.epoch != self.epoch:
            return False
        if identity.session is not None:
            return self.sessions.get(identity.session.id) is identity.session
        return True


def require(request: Request) -> Identity:
    """FastAPI dependency guarding every route except health and login."""
    return request.app.state.auth.authorize(request)
