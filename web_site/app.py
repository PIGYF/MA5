from __future__ import annotations

import html
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

try:
    from .auth import create_session, destroy_session, is_session_valid, verify_password
    from .constants import (
        HEALTH_PATH,
        HOME_PATH,
        LOGIN_PAGE_HEAD_HTML,
        LOGIN_PAGE_STYLES,
        LOGIN_PATH,
        LOGIN_TEMPLATE_PATH,
        LOGOUT_PATH,
        SESSION_COOKIE_NAME,
        SESSION_TTL_SECONDS,
        WEB_SITE_HOST,
        WEB_SITE_PORT,
    )
    from .proxy import UpstreamUnavailableError, proxy_request
except ImportError:
    from auth import create_session, destroy_session, is_session_valid, verify_password
    from constants import (
        HEALTH_PATH,
        HOME_PATH,
        LOGIN_PAGE_HEAD_HTML,
        LOGIN_PAGE_STYLES,
        LOGIN_PATH,
        LOGIN_TEMPLATE_PATH,
        LOGOUT_PATH,
        SESSION_COOKIE_NAME,
        SESSION_TTL_SECONDS,
        WEB_SITE_HOST,
        WEB_SITE_PORT,
    )
    from proxy import UpstreamUnavailableError, proxy_request


def sanitize_next_path(next_path: str | None) -> str:
    if not next_path:
        return HOME_PATH
    if not next_path.startswith("/") or next_path.startswith("//"):
        return HOME_PATH
    if next_path.startswith(LOGIN_PATH) or next_path.startswith(LOGOUT_PATH):
        return HOME_PATH
    return next_path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def handle_request(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path or HOME_PATH

        if self.command == "GET" and path == HEALTH_PATH:
            self.send_plain_text("ok")
            return

        if path == LOGIN_PATH:
            if self.command == "GET":
                self.handle_login_page(parsed)
                return
            if self.command == "POST":
                self.handle_login_submit()
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return

        if path == LOGOUT_PATH:
            self.handle_logout()
            return

        session_id = self.get_session_id()
        if not is_session_valid(session_id):
            self.redirect_to_login(self.path)
            return

        self.proxy_current_request()

    def handle_login_page(self, parsed) -> None:
        if is_session_valid(self.get_session_id()):
            self.redirect(sanitize_next_path(parse_qs(parsed.query).get("next", [HOME_PATH])[0]))
            return
        next_path = sanitize_next_path(parse_qs(parsed.query).get("next", [HOME_PATH])[0])
        self.send_html(render_login_page(next_path=next_path))

    def handle_login_submit(self) -> None:
        form = self.parse_form_body()
        next_path = sanitize_next_path(form.get("next", [HOME_PATH])[0])
        password = form.get("password", [""])[0]
        if not verify_password(password):
            self.send_html(render_login_page(next_path=next_path, error_message="访问验证失败，请重新输入密码。"), status=HTTPStatus.UNAUTHORIZED)
            return

        session_id = create_session()
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", next_path)
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={session_id}; Max-Age={SESSION_TTL_SECONDS}; Path=/; HttpOnly; SameSite=Lax",
        )
        self.end_headers()

    def handle_logout(self) -> None:
        destroy_session(self.get_session_id())
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", LOGIN_PATH)
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
        )
        self.end_headers()

    def proxy_current_request(self) -> None:
        body = self.read_request_body() if self.command in {"POST", "PUT", "PATCH"} else b""
        try:
            proxy_response = proxy_request(
                method=self.command,
                raw_path=self.path,
                body=body,
                request_headers=list(self.headers.items()),
            )
        except UpstreamUnavailableError:
            self.send_html(render_error_page("服务暂时不可用", "当前入口服务已就绪，但业务服务暂时未响应。请稍后刷新重试；如果问题持续存在，再联系维护者检查服务状态。"), status=HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self.send_response(proxy_response.status, proxy_response.reason)
        for name, value in proxy_response.headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(proxy_response.body)

    def get_session_id(self) -> str | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def parse_form_body(self) -> dict[str, list[str]]:
        body = self.read_request_body().decode("utf-8")
        return parse_qs(body, keep_blank_values=True)

    def read_request_body(self) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            return b""
        return self.rfile.read(content_length)

    def redirect_to_login(self, next_path: str) -> None:
        safe_next = sanitize_next_path(next_path)
        self.redirect(f"{LOGIN_PATH}?next={quote(safe_next, safe='/?:=&')}")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def send_plain_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def render_login_page(next_path: str, error_message: str = "") -> str:
    template = LOGIN_TEMPLATE_PATH.read_text(encoding="utf-8")
    message_block = (
        f'<div class="alert soft-alert mb-4" role="alert"><i class="bi bi-exclamation-circle me-2"></i>{html.escape(error_message)}</div>'
        if error_message
        else ""
    )
    return (
        template.replace("%%HEAD_ASSETS%%", LOGIN_PAGE_HEAD_HTML)
        .replace("%%INLINE_STYLES%%", LOGIN_PAGE_STYLES)
        .replace("%%MESSAGE_BLOCK%%", message_block)
        .replace("%%NEXT_PATH%%", html.escape(next_path, quote=True))
    )


def render_error_page(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>{html.escape(title)}</title>
{LOGIN_PAGE_HEAD_HTML}
<style>{LOGIN_PAGE_STYLES}</style>
</head>
<body>
<main class=\"auth-shell\">
    <div class=\"container py-4 py-lg-5\">
        <div class=\"row align-items-center justify-content-center auth-grid\">
            <div class=\"col-12 col-lg-7 col-xl-6\">
                <div class=\"card login-card error-card mx-auto\">
                    <div class=\"card-body p-4 p-lg-5 text-center\">
                        <div class=\"error-icon\"><i class=\"bi bi-shield-exclamation\"></i></div>
                        <p class=\"eyebrow text-center\">MA5 Strategy Hub</p>
                        <h1 class=\"login-title mb-3\">{html.escape(title)}</h1>
                        <p class=\"login-subtitle mb-4\">{html.escape(message)}</p>
                        <div class=\"d-grid gap-2 d-sm-flex justify-content-sm-center\">
                            <a class=\"btn login-btn btn-lg px-4\" href=\"{LOGIN_PATH}\">返回登录</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</main>
</body>
</html>"""


def main() -> None:
    server = ThreadingHTTPServer((WEB_SITE_HOST, WEB_SITE_PORT), Handler)
    print(f"Open http://{WEB_SITE_HOST}:{WEB_SITE_PORT}{LOGIN_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
