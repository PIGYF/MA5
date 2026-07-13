from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from backend_storage import atomic_write_json
from ma5_config import SCAN_DIR


SCAN_JOBS: dict[str, dict[str, object]] = {}
SCAN_JOBS_LOCK = threading.Lock()
TASK_HISTORY_PATH = SCAN_DIR / "task_history.json"
TASK_HISTORY_LIMIT = 80
TASK_HISTORY_LOCK = threading.Lock()
ACTIVE_SCAN_STATUSES = {"queued", "running", "pausing", "paused", "stopping"}
FINISHED_SCAN_STATUSES = {"done", "stopped", "error"}
JOB_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "pausing": "正在暂停",
    "paused": "已暂停",
    "stopping": "正在终止",
    "stopped": "已终止",
    "done": "已完成",
    "error": "失败",
}


def set_job(job_id: str, **updates: object) -> None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.setdefault(job_id, {})
        if "created_at" not in job:
            job["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job.update(updates)


def get_job(job_id: str) -> dict[str, object] | None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        return dict(job) if job else None


def job_market(job_id: str, job: dict[str, object] | None = None) -> str:
    if job and isinstance(job.get("market"), str):
        return str(job["market"])
    if str(job_id).startswith("profile-"):
        return "us_profile"
    return "cn" if str(job_id).startswith("ashare-") else "us"


def active_scan_job(market: str | None = None) -> tuple[str, dict[str, object]] | None:
    with SCAN_JOBS_LOCK:
        for job_id, job in SCAN_JOBS.items():
            if job.get("status") not in ACTIVE_SCAN_STATUSES:
                continue
            if market and job_market(job_id, job) != market:
                continue
            return job_id, dict(job)
    return None


def normalize_job_payload(job_id: str, job: dict[str, object]) -> dict[str, object]:
    payload = dict(job)
    status = str(payload.get("status", ""))
    total = int(payload.get("total") or 0)
    scanned = int(payload.get("scanned") or 0)
    if status in FINISHED_SCAN_STATUSES:
        progress_pct = 100
    elif total > 0:
        progress_pct = max(1, min(99, round(scanned / total * 100)))
    elif status in ACTIVE_SCAN_STATUSES:
        progress_pct = 8
    else:
        progress_pct = 0
    market = job_market(job_id, payload)
    market_label = "A股" if market == "cn" else "美股资料" if market == "us_profile" else "美股"
    payload.update(
        {
            "job_id": job_id,
            "market": market,
            "market_label": market_label,
            "status_label": JOB_STATUS_LABELS.get(status, status or "-"),
            "is_active": status in ACTIVE_SCAN_STATUSES,
            "is_finished": status in FINISHED_SCAN_STATUSES,
            "can_stop": status in ACTIVE_SCAN_STATUSES,
            "progress_pct": progress_pct,
        }
    )
    return payload


def latest_job_for_market(market: str, include_finished: bool = True) -> tuple[str, dict[str, object]] | None:
    latest_job_id = ""
    latest_job: dict[str, object] | None = None
    with SCAN_JOBS_LOCK:
        for job_id, job in SCAN_JOBS.items():
            if job_market(job_id, job) != market:
                continue
            if job.get("status") in ACTIVE_SCAN_STATUSES:
                return job_id, dict(job)
            if include_finished and job.get("status") in FINISHED_SCAN_STATUSES:
                latest_job_id = job_id
                latest_job = dict(job)
    return (latest_job_id, latest_job) if latest_job else None


def clear_jobs_for_market(market: str) -> int:
    deleted = 0
    with SCAN_JOBS_LOCK:
        for job_id, job in list(SCAN_JOBS.items()):
            if job_market(job_id, job) == market:
                SCAN_JOBS.pop(job_id, None)
                deleted += 1
    return deleted


def load_task_history(limit: int = 12) -> list[dict[str, object]]:
    if not TASK_HISTORY_PATH.exists():
        return []
    try:
        payload = json.loads(TASK_HISTORY_PATH.read_text(encoding="utf-8"))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)][: max(1, int(limit))]
    except Exception:
        return []


def append_task_history(
    job_id: str,
    market: str,
    status: str,
    scanned: int = 0,
    candidates: int = 0,
    errors: int = 0,
    source: str = "",
    params: dict[str, list[str]] | None = None,
    message: str = "",
) -> None:
    market_label = "A股" if market == "cn" else "美股资料" if market == "us_profile" else "美股"
    with TASK_HISTORY_LOCK:
        existing = load_task_history(TASK_HISTORY_LIMIT)
        entry = {
            "job_id": job_id,
            "market": market,
            "market_label": market_label,
            "status": status,
            "status_label": JOB_STATUS_LABELS.get(status, status),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scanned": int(scanned or 0),
            "candidates": int(candidates or 0),
            "errors": int(errors or 0),
            "source": source,
            "message": message,
            "params": {key: values[-1] if values else "" for key, values in (params or {}).items()},
        }
        deduped = [item for item in existing if item.get("job_id") != job_id]
        atomic_write_json(
            TASK_HISTORY_PATH,
            {"updated_at": entry["created_at"], "items": [entry] + deduped[: TASK_HISTORY_LIMIT - 1]},
            indent=2,
        )


def job_pause_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("pause_requested"))


def job_stop_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("stop_requested"))


def classify_scan_error(reason: str) -> str:
    text = str(reason or "").lower()
    if any(token in text for token in ("recent split", "split adjustment", "拆股", "复权", "volume regime")):
        return "数据质量"
    if any(token in text for token in ("缺少", "no module", "importerror", "install yfinance", "pip install")):
        return "依赖缺失"
    if any(token in text for token in ("timeout", "timed out", "connection", "network", "http", "urlopen", "远程", "网络")):
        return "网络/接口"
    if any(token in text for token in ("no data", "empty", "没有可用", "日线", "数据源", "possibly delisted")):
        return "无数据"
    if any(token in text for token in ("symbol", "代码", "6 位", "invalid", "not found")):
        return "代码/标的"
    return "其他"


def summarize_error_categories(errors: list[tuple[str, str]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for _, reason in errors:
        category = classify_scan_error(reason)
        summary[category] = summary.get(category, 0) + 1
    return summary
