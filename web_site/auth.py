from __future__ import annotations

import hmac
import secrets
import threading
import time

try:
    from .constants import ADMIN_PASSWORD, SESSION_TTL_SECONDS
except ImportError:
    from constants import ADMIN_PASSWORD, SESSION_TTL_SECONDS

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _purge_expired(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired_ids = [session_id for session_id, expires_at in _sessions.items() if expires_at <= current]
    for session_id in expired_ids:
        _sessions.pop(session_id, None)


def verify_password(password: str) -> bool:
    return hmac.compare_digest(password, ADMIN_PASSWORD)


def create_session() -> str:
    now = time.time()
    session_id = secrets.token_urlsafe(32)
    with _sessions_lock:
        _purge_expired(now)
        _sessions[session_id] = now + SESSION_TTL_SECONDS
    return session_id


def is_session_valid(session_id: str | None) -> bool:
    if not session_id:
        return False
    now = time.time()
    with _sessions_lock:
        _purge_expired(now)
        expires_at = _sessions.get(session_id)
        return bool(expires_at and expires_at > now)


def destroy_session(session_id: str | None) -> None:
    if not session_id:
        return
    with _sessions_lock:
        _sessions.pop(session_id, None)
