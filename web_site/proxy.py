from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

try:
    from .constants import PROXY_TIMEOUT_SECONDS, SESSION_COOKIE_NAME, UPSTREAM_ORIGIN, WEB_APP_HOST, WEB_APP_PORT
except ImportError:
    from constants import PROXY_TIMEOUT_SECONDS, SESSION_COOKIE_NAME, UPSTREAM_ORIGIN, WEB_APP_HOST, WEB_APP_PORT

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class UpstreamUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class ProxyResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes


def _strip_session_cookie(cookie_header: str) -> str:
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    cookie.pop(SESSION_COOKIE_NAME, None)
    parts = [f"{morsel.key}={morsel.value}" for morsel in cookie.values()]
    return "; ".join(parts)


def build_upstream_headers(request_headers: list[tuple[str, str]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request_headers:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS or lower == "host":
            continue
        if lower == "cookie":
            cleaned = _strip_session_cookie(value)
            if cleaned:
                headers[name] = cleaned
            continue
        headers[name] = value
    headers["Host"] = f"{WEB_APP_HOST}:{WEB_APP_PORT}"
    headers["X-Forwarded-Host"] = headers.get("X-Forwarded-Host", f"{WEB_APP_HOST}:{WEB_APP_PORT}")
    headers["X-Forwarded-Proto"] = headers.get("X-Forwarded-Proto", "http")
    return headers


def _rewrite_location(location: str) -> str:
    if location.startswith(UPSTREAM_ORIGIN):
        rewritten = location[len(UPSTREAM_ORIGIN):]
        return rewritten or "/"
    return location


def _filter_response_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    filtered: list[tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "location":
            filtered.append((name, _rewrite_location(value)))
            continue
        filtered.append((name, value))
    return filtered


def proxy_request(method: str, raw_path: str, body: bytes, request_headers: list[tuple[str, str]]) -> ProxyResponse:
    split = urlsplit(raw_path)
    upstream_path = split.path or "/"
    if split.query:
        upstream_path = f"{upstream_path}?{split.query}"

    connection = HTTPConnection(WEB_APP_HOST, WEB_APP_PORT, timeout=PROXY_TIMEOUT_SECONDS)
    try:
        connection.request(method, upstream_path, body=body or None, headers=build_upstream_headers(request_headers))
        response = connection.getresponse()
        response_body = response.read()
        response_headers = _filter_response_headers(response.getheaders())
        return ProxyResponse(
            status=response.status,
            reason=response.reason,
            headers=response_headers,
            body=response_body,
        )
    except OSError as exc:
        raise UpstreamUnavailableError(str(exc)) from exc
    finally:
        connection.close()
