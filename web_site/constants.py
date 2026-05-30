from __future__ import annotations

from pathlib import Path

WEB_SITE_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_SITE_DIR / "static"
LOGIN_TEMPLATE_PATH = STATIC_DIR / "login.html"

WEB_SITE_HOST = "127.0.0.1"
WEB_SITE_PORT = 8764
WEB_APP_HOST = "127.0.0.1"
WEB_APP_PORT = 8765
UPSTREAM_ORIGIN = f"http://{WEB_APP_HOST}:{WEB_APP_PORT}"

LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
HEALTH_PATH = "/health"
HOME_PATH = "/"

SESSION_COOKIE_NAME = "ma5_session"
SESSION_TTL_SECONDS = 24 * 60 * 60
ADMIN_PASSWORD = "admin"
PROXY_TIMEOUT_SECONDS = 600

LOGIN_PAGE_HEAD_HTML = """
<link rel="preconnect" href="https://cdn.jsdelivr.net" />
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" />
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" />
"""

LOGIN_PAGE_STYLES = """
:root {
	--ma5-primary: #2563eb;
	--ma5-primary-dark: #1d4ed8;
	--ma5-ink: #0f172a;
	--ma5-muted: #64748b;
}

body {
	min-height: 100vh;
	margin: 0;
	color: var(--ma5-ink);
	font-family: Inter, Arial, "Microsoft YaHei", sans-serif;
	background:
		radial-gradient(circle at top left, rgba(37, 99, 235, 0.22), transparent 34%),
		radial-gradient(circle at bottom right, rgba(14, 165, 233, 0.16), transparent 28%),
		linear-gradient(180deg, #f8fbff 0%, #eef4ff 52%, #f7f9fc 100%);
}

main,
.auth-shell {
	min-height: 100vh;
}

.auth-shell {
	position: relative;
	overflow: hidden;
}

.auth-grid {
	min-height: 100vh;
}

.hero-panel {
	position: relative;
	overflow: hidden;
	padding: 2.25rem;
	border-radius: 28px;
	color: #fff;
	border: 1px solid rgba(255, 255, 255, 0.14);
	background: linear-gradient(145deg, rgba(37, 99, 235, 0.96), rgba(15, 23, 42, 0.92));
	box-shadow: 0 32px 80px rgba(15, 23, 42, 0.22);
}

.hero-panel::after {
	content: "";
	position: absolute;
	inset: auto -30% -35% auto;
	width: 260px;
	height: 260px;
	border-radius: 999px;
	background: rgba(255, 255, 255, 0.08);
	filter: blur(6px);
}

.hero-badge {
	display: inline-flex;
	align-items: center;
	gap: 0.45rem;
	padding: 0.45rem 0.8rem;
	border-radius: 999px;
	background: rgba(255, 255, 255, 0.12);
	color: rgba(255, 255, 255, 0.95);
	font-size: 0.78rem;
	letter-spacing: 0.08em;
	text-transform: uppercase;
}

.hero-copy {
	max-width: 34rem;
	color: rgba(255, 255, 255, 0.78);
	line-height: 1.7;
}

.feature-list {
	display: grid;
	gap: 0.9rem;
	margin-top: 1.75rem;
}

.feature-item {
	position: relative;
	display: flex;
	align-items: flex-start;
	gap: 0.9rem;
	padding: 1rem 1rem 1rem 1.05rem;
	border-radius: 18px;
	background: rgba(255, 255, 255, 0.08);
	backdrop-filter: blur(10px);
}

.feature-icon {
	width: 2.5rem;
	height: 2.5rem;
	border-radius: 14px;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	font-size: 1.1rem;
	background: rgba(255, 255, 255, 0.16);
}

.feature-title {
	font-size: 0.98rem;
	font-weight: 600;
	margin-bottom: 0.2rem;
}

.feature-text {
	color: rgba(255, 255, 255, 0.7);
	font-size: 0.9rem;
	margin: 0;
}

.login-card {
	border: 0;
	border-radius: 28px;
	background: rgba(255, 255, 255, 0.92);
	backdrop-filter: blur(18px);
	box-shadow: 0 28px 72px rgba(15, 23, 42, 0.14);
}

.brand-mark {
	width: 3.3rem;
	height: 3.3rem;
	border-radius: 1.15rem;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	font-weight: 800;
	font-size: 1.05rem;
	color: #fff;
	background: linear-gradient(145deg, var(--ma5-primary), #60a5fa);
	box-shadow: 0 16px 30px rgba(37, 99, 235, 0.25);
}

.eyebrow {
	margin: 1rem 0 0.35rem;
	color: var(--ma5-muted);
	letter-spacing: 0.24em;
	text-transform: uppercase;
	font-size: 0.72rem;
	font-weight: 700;
}

.login-title {
	font-size: clamp(1.8rem, 2vw, 2.2rem);
	font-weight: 700;
	letter-spacing: -0.02em;
}

.login-subtitle {
	color: var(--ma5-muted);
	line-height: 1.7;
}

.form-label {
	color: #475569;
	font-size: 0.85rem;
	font-weight: 600;
}

.password-group {
	border: 1px solid #d9e2ef;
	border-radius: 18px;
	padding: 0.25rem;
	background: #fff;
	box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9);
	transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.password-group:focus-within {
	border-color: rgba(37, 99, 235, 0.45);
	box-shadow: 0 0 0 0.28rem rgba(37, 99, 235, 0.12);
}

.password-group .input-group-text,
.password-group .form-control,
.password-group .btn {
	border: 0;
	background: transparent;
	box-shadow: none !important;
}

.password-group .input-group-text {
	color: var(--ma5-primary);
	padding-left: 0.9rem;
}

.password-group .form-control {
	padding: 0.92rem 0.35rem;
	color: var(--ma5-ink);
	font-size: 1rem;
}

.password-group .form-control::placeholder {
	color: #94a3b8;
}

.password-group .btn {
	color: #64748b;
	padding-right: 0.9rem;
}

.login-btn {
	border: 0;
	border-radius: 18px;
	padding: 0.95rem 1.1rem;
	font-weight: 700;
	letter-spacing: 0.01em;
	background: linear-gradient(145deg, var(--ma5-primary), var(--ma5-primary-dark));
	box-shadow: 0 18px 30px rgba(37, 99, 235, 0.22);
	transition: transform 0.2s ease, box-shadow 0.2s ease, filter 0.2s ease;
}

.login-btn:hover {
	filter: brightness(1.03);
	transform: translateY(-1px);
	box-shadow: 0 20px 34px rgba(37, 99, 235, 0.24);
}

.trust-note {
	display: inline-flex;
	align-items: center;
	gap: 0.55rem;
	color: var(--ma5-muted);
	font-size: 0.86rem;
}

.trust-dot {
	width: 0.6rem;
	height: 0.6rem;
	border-radius: 999px;
	background: linear-gradient(145deg, #10b981, #34d399);
	box-shadow: 0 0 0 0.2rem rgba(16, 185, 129, 0.12);
}

.soft-alert {
	border: 0;
	border-radius: 16px;
	background: linear-gradient(145deg, rgba(248, 113, 113, 0.14), rgba(251, 146, 60, 0.08));
	color: #9f1239;
}

.error-icon {
	width: 4rem;
	height: 4rem;
	margin: 0 auto 1.25rem;
	border-radius: 1.5rem;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	color: var(--ma5-primary);
	font-size: 1.65rem;
	background: rgba(37, 99, 235, 0.08);
}

.error-card {
	max-width: 36rem;
}

@media (max-width: 991.98px) {
	.auth-grid {
		padding: 2rem 0;
	}

	.login-card {
		background: rgba(255, 255, 255, 0.96);
	}
}
"""
