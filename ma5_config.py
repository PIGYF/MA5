from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MA5_DATA_DIR", ROOT / "data")).expanduser().resolve()
REPORT_DIR = Path(os.environ.get("MA5_REPORT_DIR", ROOT / "reports")).expanduser().resolve()
SCAN_DIR = DATA_DIR / "scans"
LATEST_SCAN_PATH = SCAN_DIR / "latest.json"
WATCHLIST_PATH = DATA_DIR / "watchlist.json"
NASDAQ_CACHE_PATH = DATA_DIR / "cache" / "nasdaq_screener.json"
LEGACY_NASDAQ_CACHE_PATH = ROOT / "nasdaq_screener_cache.json"
EARNINGS_CACHE_PATH = DATA_DIR / "cache" / "earnings_dates.json"

DEFAULT_BENCHMARK = "^IXIC"
DEFAULT_SCAN_LOOKBACK_DAYS = 70
DEFAULT_MIN_MARKET_CAP_100M_USD = 200
DEFAULT_MAX_SCAN_SYMBOLS = 500
DEFAULT_HIDE_WEAK_CANDIDATES = True
NASDAQ_CACHE_SECONDS = 60 * 60 * 12
EARNINGS_CACHE_SECONDS = 60 * 60 * 24
REPORT_RETENTION_DAYS = 30
REPORT_RETENTION_MAX_FILES = 120


def field(params: dict[str, list[str]], name: str, default: str) -> str:
    return params.get(name, [default])[0].strip()


def number_field(params: dict[str, list[str]], name: str, default: float) -> float:
    return float(field(params, name, str(default)))


def min_backtest_days(vol_length: int) -> int:
    return max(30, vol_length + 10)


def validate_backtest_range(start: str, end: str, vol_length: int) -> None:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    minimum_days = min_backtest_days(vol_length)
    if end_date <= start_date:
        raise ValueError("结束日期必须晚于开始日期。")
    actual_days = (end_date - start_date).days
    if actual_days < minimum_days:
        raise ValueError(f"回测区间过短。当前均量周期为 {vol_length}，至少需要 {minimum_days} 天。")


def validate_scan_range(start: str, end: str) -> None:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if end_date <= start_date:
        raise ValueError("选股结束日期必须晚于开始日期。")
    if (end_date - start_date).days < 30:
        raise ValueError("选股区间过短。开始日期和结束日期至少需要间隔 1 个月。")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def start_for_preset(preset: str, end: date) -> date:
    if preset == "6m":
        return end - timedelta(days=183)
    if preset == "1y":
        return end - timedelta(days=365)
    if preset == "3y":
        return end - timedelta(days=365 * 3)
    if preset == "5y":
        return end - timedelta(days=365 * 5)
    return end - timedelta(days=365)


def default_scan_end_date() -> date:
    end = date.today() - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return end


def default_scan_start_date(end: date) -> date:
    return end - timedelta(days=DEFAULT_SCAN_LOOKBACK_DAYS)


def current_signal_date() -> str:
    return default_scan_end_date().isoformat()


def next_market_weekday(day: date) -> date:
    next_day = day + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day


def chart_start_for_preset(preset: str, end: date) -> date:
    return {
        "1m": end - timedelta(days=30),
        "3m": end - timedelta(days=90),
        "6m": end - timedelta(days=183),
        "1y": end - timedelta(days=365),
        "3y": end - timedelta(days=365 * 3),
        "5y": end - timedelta(days=365 * 5),
    }.get(preset, end - timedelta(days=365))
