"""HTTP client for the daemon, on the standard library only.

The CLI must work from a bare install and in rescue situations, so it speaks to the daemon
with urllib and nothing else. One class, three kinds of call:

    request()    JSON in, JSON out
    upload()     one file streamed as multipart/form-data (an app can be several GB;
                 it is never read into memory)
    download()   a response body streamed into a file

The token is read from <home>/secrets/api-token and sent as a bearer header. It is never
put in a URL, never printed and never part of an error message.
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..config import Config
from ..paths import Paths

TOKEN_FILE = "api-token"                    # noqa: S105 - a file name, not a token
NOT_RUNNING = "the daemon is not running (start it with: sf serve  or  sf service start)"
CHUNK = 256 * 1024


class CliError(Exception):
    """Anything the user should see as one line, with the exit code to use."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class DaemonDown(CliError):
    def __init__(self, url: str):
        super().__init__(f"{NOT_RUNNING}\n    tried {url}")
        self.url = url


class ApiError(CliError):
    """An error response from the daemon: `{"error": {"code", "message", "detail"?}}`."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None, payload: Any = None):
        super().__init__(message or f"HTTP {status}")
        self.status, self.api_code, self.message, self.detail, self.payload = status, code, message, detail, payload


def token_path(paths: Paths) -> Path:
    return paths.secrets / TOKEN_FILE


def read_token(paths: Paths) -> str:
    """The API token, or "" when there is none yet (the daemon creates it at first start)."""
    try:
        return token_path(paths).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except PermissionError:
        raise CliError(f"cannot read {token_path(paths)}: it belongs to another user. "
                       "Run sf as the user the daemon runs as.") from None


def connect_host(host: str) -> str:
    """Address to dial for a listen address: a wildcard is reached through loopback."""
    if host in ("0.0.0.0", ""):   # noqa: S104 - comparing against the wildcard, not binding to it
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def base_url(config: Config) -> str:
    server = config.server
    host = connect_host(server.host)
    if ":" in host:
        host = f"[{host}]"
    scheme = "https" if server.tls_cert and server.tls_key else "http"
    return f"{scheme}://{host}:{server.port}"


def tls_context(config: Config) -> ssl.SSLContext | None:
    """Verify the daemon's certificate against the system store *and* the configured
    certificate itself, so a self-signed panel certificate works without switching
    verification off."""
    cert = config.server.tls_cert
    if not (cert and config.server.tls_key):
        return None
    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=cert)
    except (OSError, ssl.SSLError) as e:
        raise CliError(f"cannot read server.tls_cert {cert}: {e}") from None
    # The configured file is usually the server's own (leaf) certificate, not a CA.
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the bearer token to wherever the response points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - signature fixed by urllib
        return None


class Api:
    def __init__(self, url: str, token: str = "", *, context: ssl.SSLContext | None = None, timeout: float = 60):
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect(), urllib.request.ProxyHandler({})]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        # ProxyHandler({}): the daemon is local; http_proxy from the environment must not
        # send the token through somebody's proxy.
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_farm(cls, config: Config, paths: Paths, *, timeout: float = 60) -> Api:
        return cls(base_url(config), read_token(paths), context=tls_context(config), timeout=timeout)

    def __repr__(self) -> str:      # the default repr would be harmless; this one makes it certain
        return f"Api({self.url!r})"

    # ------------------------------------------------------------------ plumbing
    def _target(self, path: str, query: Mapping[str, Any] | None) -> str:
        if not path.startswith("/"):
            raise ValueError(f"API path must start with /: {path!r}")
        target = self.url + urllib.parse.quote(path, safe="/-._~")
        pairs = [(k, _query_value(v)) for k, v in (query or {}).items() if v is not None]
        return f"{target}?{urllib.parse.urlencode(pairs)}" if pairs else target

    def _open(self, method: str, path: str, *, query: Mapping[str, Any] | None = None, data: Any = None,
              headers: Mapping[str, str] | None = None, timeout: float | None = None):
        target = self._target(path, query)
        request = urllib.request.Request(target, data=data, method=method)  # noqa: S310 - scheme fixed by base_url()
        request.add_header("Accept", "application/json")
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            return self._opener.open(request, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            with e:
                raise _api_error(e.code, e.read(), self) from None
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLError):
                raise CliError(f"TLS error talking to {self.url}: {reason}. Check server.tls_cert, and that "
                               "server.host is a name the certificate was issued for.") from None
            if isinstance(reason, TimeoutError):
                raise CliError(f"no answer from the daemon at {self.url} within "
                               f"{timeout or self.timeout:.0f} s") from None
            # Refused, unreachable, unknown host: nothing is listening where the config points.
            raise DaemonDown(self.url) from None
        except TimeoutError:
            raise CliError(f"no answer from the daemon at {self.url} within {timeout or self.timeout:.0f} s") from None
        except ConnectionError as e:
            if isinstance(e, ConnectionRefusedError):
                raise DaemonDown(self.url) from None
            raise CliError(f"connection to the daemon at {self.url} was lost ({type(e).__name__})") from None

    # ------------------------------------------------------------------ calls
    def request(self, method: str, path: str, *, body: Any = None, query: Mapping[str, Any] | None = None,
                timeout: float | None = None) -> Any:
        """One JSON call. Returns the decoded body (None for an empty one)."""
        data, headers = None, {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        with self._open(method, path, query=query, data=data, headers=headers, timeout=timeout) as response:
            raw = _read_all(response, self)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise CliError(f"{self.url}{path} did not answer with JSON. Is something else listening on that "
                           "port? (see server.port)") from None

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: Any = None, **query: Any) -> Any:
        return self.request("POST", path, body={} if body is None else body, query=query)

    def patch(self, path: str, body: Any, **query: Any) -> Any:
        return self.request("PATCH", path, body=body, query=query)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body=body)

    def delete(self, path: str, body: Any = None, **query: Any) -> Any:
        return self.request("DELETE", path, body=body, query=query)

    def upload(self, path: str, file: Path, *, fields: Mapping[str, Any] | None = None, field: str = "file",
               timeout: float = 3600, progress: Callable[[int, int], None] | None = None) -> Any:
        """POST one file as multipart/form-data, streamed from disk. `progress(sent, total)`
        is called as the file goes out."""
        file = Path(file)
        try:
            length = file.stat().st_size
            handle = open(file, "rb")           # noqa: SIM115 - closed in the generator below
        except OSError as e:
            raise CliError(f"cannot read {file}: {e.strerror or e}") from None
        boundary = "sf-" + secrets.token_hex(16)
        head = b"".join(_form_field(boundary, k, v) for k, v in (fields or {}).items() if v is not None)
        # The file name is quoted into a header: reduce it to characters that cannot end the
        # quoted string or start a new header line. The daemon sanitises it again.
        name = "".join(ch if ch.isalnum() or ch in "._-+ ()" else "_" for ch in file.name) or "file"
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        def chunks() -> Iterator[bytes]:
            sent = 0
            with handle:
                yield head
                while True:
                    block = handle.read(CHUNK)
                    if not block:
                        break
                    yield block
                    sent += len(block)
                    if progress is not None:
                        progress(sent, length)
                yield tail

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(head) + length + len(tail))}
        try:
            with self._open("POST", path, data=chunks(), headers=headers, timeout=timeout) as response:
                raw = _read_all(response, self)
        finally:
            handle.close()
        return json.loads(raw) if raw.strip() else None

    def download(self, path: str, dest: Path, *, query: Mapping[str, Any] | None = None, timeout: float = 3600) -> int:
        """Stream a response body into `dest` (written next to it first, renamed when
        complete, so an interrupted transfer never leaves a plausible-looking half file)."""
        dest = Path(dest)
        partial = dest.with_name(dest.name + ".part")
        total = 0
        try:
            with self._open("GET", path, query=query, timeout=timeout) as response, open(partial, "wb") as out:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    total += len(block)
            os.replace(partial, dest)
        except OSError as e:
            _unlink(partial)
            if isinstance(e, (ConnectionError, TimeoutError)):
                raise CliError(f"connection to the daemon at {self.url} was lost during the download") from None
            raise CliError(f"cannot write {dest}: {e.strerror or e}") from None
        except BaseException:
            _unlink(partial)
            raise
        return total

    def read_bytes(self, path: str, *, query: Mapping[str, Any] | None = None, timeout: float | None = None) -> bytes:
        with self._open("GET", path, query=query, timeout=timeout) as response:
            return _read_all(response, self)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _read_all(response: Any, api: Api) -> bytes:
    try:
        return response.read()
    except OSError as e:
        raise CliError(f"connection to the daemon at {api.url} was lost ({type(e).__name__})") from None


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _form_field(boundary: str, name: str, value: Any) -> bytes:
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"bad form field name {name!r}")
    return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + _query_value(value).encode() + b"\r\n")


def _api_error(status: int, raw: bytes, api: Api) -> ApiError:
    """Turn an error response into one readable exception."""
    payload: Any = None
    try:
        payload = json.loads(raw) if raw.strip() else None
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
        detail = error.get("detail")
    else:
        code, message, detail = "", "", None
    if status == 401:
        message = ("the daemon rejected the API token" if api._token else "the daemon wants an API token and none "
                   "was found") + ". The token is read from <home>/secrets/api-token of the farm home in use; " \
                  "pass --home if the daemon runs with another one (`sf token` prints the token of a home)."
    elif not message:
        message = f"the daemon answered HTTP {status}" + (f" ({code})" if code else "")
    return ApiError(status, code, message, detail, payload)
