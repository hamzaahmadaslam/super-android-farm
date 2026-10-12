"""Who may call the API.

Two ways in, both proving knowledge of the same secret (the API token):

    Authorization: Bearer <token>      scripts and the CLI
    session cookie                     the panel, after POST /api/auth/login

The token is 32 random bytes, created on first run and kept in `secrets/api-token` (0600).
It is never put in a URL, a log line or an error message. Comparisons are constant-time.

A browser session is a cookie the page cannot read (HttpOnly, SameSite=Strict). Because the
browser attaches it by itself, every state-changing request made with it must also carry the
session's CSRF value in `X-SF-CSRF`: another site can make the browser send the cookie, but
it cannot read the value to put in the header.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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

TOKEN_FILE = "api-token"       # noqa: S105 - a file name
SESSION_COOKIE = "sf_session"
CSRF_COOKIE = "sf_csrf"
CSRF_HEADER = "x-sf-csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})      # noqa: S104 - compared against, never bound here
MAX_SESSIONS = 1000            # a login needs the token, so this only bounds a scripted client that never logs out


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
    """Replace the token. Every client holding the old one is locked out at once."""
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

    def fail(self, client: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if client not in self._failures and len(self._failures) >= self.max_clients:
                # Full of addresses: forget the one that failed longest ago rather than grow.
                del self._failures[min(self._failures, key=lambda k: self._failures[k][-1])]
            self._failures.setdefault(client, deque()).append(now)

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


def allowed_hosts(config: Config) -> set[str]:
    hosts = set(LOOPBACK_NAMES)
    if config.server.host not in WILDCARD_HOSTS:
        hosts.add(_host_only(config.server.host))
    for origin in config.server.allowed_origins:
        host = urlsplit(origin).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def host_allowed(host_header: str, config: Config) -> bool:
    """Is this Host header one the daemon answers to?

    The check exists because of DNS rebinding: a page on evil.example can have its own name
    re-resolved to 127.0.0.1 and would then talk to this API as "same origin". The browser
    still sends `Host: evil.example`, so refusing names nobody configured closes that door.
    An IP address cannot be rebound; when the daemon listens on every interface its own
    addresses cannot be listed in advance, so any literal address is accepted then.
    """
    host = _host_only(host_header)
    if not host:
        return False
    if host in allowed_hosts(config):
        return True
    return config.server.host in WILDCARD_HOSTS and _is_ip(host)


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


def client_of(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "unknown"


def bearer_of(conn: HTTPConnection) -> str | None:
    header = conn.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


class Auth:
    """Everything the app needs to decide about one request. One per app."""

    def __init__(self, farm) -> None:
        self.farm = farm
        self.tokens = TokenStore(farm.paths)
        self.sessions = Sessions()
        self.limiter = RateLimiter()

    @property
    def config(self) -> Config:
        return self.farm.config

    def _limit(self) -> int:
        return self.config.server.login_attempts_per_minute

    def check_token(self, client: str, candidate: str) -> None:
        """Accept a presented token or raise. A client that failed too often in the last
        minute is refused before the comparison is made, right or wrong."""
        wait = self.limiter.retry_after(client, self._limit())
        if wait:
            raise ApiError(429, "rate_limited", "too many failed attempts; try again shortly",
                           headers={"Retry-After": str(wait)})
        if not self.tokens.matches(candidate):
            self.limiter.fail(client)
            raise ApiError(401, "unauthorized", "the token is not valid")

    def identify(self, conn: HTTPConnection) -> Identity:
        """Who is calling (HTTP request or WebSocket handshake). Raises ApiError 401/429."""
        client = client_of(conn)
        if not self.config.server.auth_required:
            return Identity("none", client)
        token = bearer_of(conn)
        if token is not None:
            self.check_token(client, token)
            return Identity("token", client)
        session = self.sessions.get(conn.cookies.get(SESSION_COOKIE))
        if session is not None:
            return Identity("session", client, session)
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


def require(request: Request) -> Identity:
    """FastAPI dependency guarding every route except health and login."""
    return request.app.state.auth.authorize(request)
