from __future__ import annotations

import csv
import html
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from backtest import (
    Bar,
    PRICE_CACHE_DIR,
    PRICE_CACHE_MAX_BARS,
    backtest,
    build_signal_detail_rows,
    fetch_bars,
    make_report,
    open_position_snapshot,
    price_cache_path,
    read_price_cache,
    build_ratchet_inputs,
    rolling_sma,
    summarize,
    write_equity,
    write_trades,
)
from ma5_config import (
    DATA_DIR,
    DEFAULT_BENCHMARK,
    DEFAULT_HIDE_WEAK_CANDIDATES,
    DEFAULT_MAX_SCAN_SYMBOLS,
    DEFAULT_MIN_MARKET_CAP_100M_USD,
    EARNINGS_CACHE_PATH,
    EARNINGS_CACHE_SECONDS,
    LATEST_SCAN_PATH,
    LEGACY_NASDAQ_CACHE_PATH,
    NASDAQ_CACHE_PATH,
    NASDAQ_CACHE_SECONDS,
    REPORT_DIR,
    REPORT_RETENTION_DAYS,
    REPORT_RETENTION_MAX_FILES,
    SCAN_DIR,
    WATCHLIST_PATH,
    chart_start_for_preset,
    current_signal_date,
    default_scan_end_date,
    default_scan_start_date,
    field,
    next_market_weekday,
    number_field,
    safe_name,
    start_for_preset,
    validate_backtest_range,
    validate_scan_range,
)
from ashare_lab import (
    ASHARE_ROUTE,
    AShareSignalSnapshot,
    ashare_chart_payload,
    fetch_ashare_profile,
    latest_ashare_signal,
    load_ashare_universe_for_scan,
    scan_ashare_candidates,
)
from scan_next_b import SignalResult, latest_b_signal, load_symbols, unique_symbols, write_html


SCAN_JOBS: dict[str, dict[str, object]] = {}
SCAN_JOBS_LOCK = threading.Lock()
ACTIVE_SCAN_STATUSES = {"queued", "running", "pausing", "paused", "stopping"}


def cleanup_old_reports() -> None:
    if not REPORT_DIR.exists():
        return
    files = [path for path in REPORT_DIR.iterdir() if path.is_file()]
    now = time.time()
    cutoff = now - REPORT_RETENTION_DAYS * 24 * 60 * 60
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    files = sorted(
        [path for path in REPORT_DIR.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files[REPORT_RETENTION_MAX_FILES:]:
        try:
            path.unlink()
        except OSError:
            pass


def set_job(job_id: str, **updates: object) -> None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.setdefault(job_id, {})
        job.update(updates)


def get_job(job_id: str) -> dict[str, object] | None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        return dict(job) if job else None


def active_scan_job() -> tuple[str, dict[str, object]] | None:
    with SCAN_JOBS_LOCK:
        for job_id, job in SCAN_JOBS.items():
            if job.get("status") in ACTIVE_SCAN_STATUSES:
                return job_id, dict(job)
    return None


def job_pause_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("pause_requested"))


def job_stop_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("stop_requested"))


def build_benchmark(symbol: str, start: str, end: str, initial_cash: float) -> dict[str, object]:
    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
    first_close = bars[0].close
    return {
        "symbol": symbol,
        "return_pct": (bars[-1].close / first_close - 1) * 100,
        "curve": [(bar.date, initial_cash * (bar.close / first_close)) for bar in bars],
    }


def market_environment(symbol: str = "QQQ") -> dict[str, object]:
    end = default_scan_end_date()
    start = end - timedelta(days=120)
    try:
        bars = fetch_bars("yfinance", symbol, start.isoformat(), end.isoformat(), "qfq", None)
        vix_value = 0.0
        vix_label = "Unavailable"
        try:
            vix_bars = fetch_bars("yfinance", "^VIX", start.isoformat(), end.isoformat(), "qfq", None)
            if vix_bars:
                vix_value = vix_bars[-1].close
                if vix_value >= 30:
                    vix_label = "高恐慌"
                elif vix_value >= 20:
                    vix_label = "风险升温"
                elif vix_value >= 15:
                    vix_label = "正常偏谨慎"
                else:
                    vix_label = "低波动"
        except Exception:
            pass
        closes = [bar.close for bar in bars]
        ma20 = rolling_sma(closes, 20)
        ma50 = rolling_sma(closes, 50)
        latest = bars[-1]
        ma20_now = ma20[-1] or 0.0
        ma50_now = ma50[-1] or 0.0
        ma20_prev = next((value for value in reversed(ma20[:-1]) if value is not None), 0.0)
        dist20 = (latest.close / ma20_now - 1) * 100 if ma20_now else 0.0
        dist50 = (latest.close / ma50_now - 1) * 100 if ma50_now else 0.0
        ma20_rising = bool(ma20_now and ma20_prev and ma20_now >= ma20_prev)
        if vix_value >= 30:
            state = "Risk-Off"
            tone = "bad"
            message = "VIX 高位，候选保留，但建议显著降低仓位和追高优先级"
        elif dist20 >= 0 and ma20_rising and dist50 >= -1 and vix_value < 20:
            state = "Risk-On"
            tone = "good"
            message = "环境支持正常选股"
        elif dist20 < -1.5 or not ma20_rising or vix_value >= 20:
            state = "Risk-Off"
            tone = "bad" if vix_value >= 20 else "warn"
            message = "候选保留，但建议降低仓位和追高优先级"
        else:
            state = "Neutral"
            tone = "warn"
            message = "环境一般，优先看强催化和低乖离"
        return {
            "symbol": symbol,
            "date": latest.date,
            "state": state,
            "tone": tone,
            "dist20": dist20,
            "dist50": dist50,
            "ma20_direction": "上行" if ma20_rising else "下行",
            "vix": vix_value,
            "vix_label": vix_label,
            "message": message,
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "date": "-",
            "state": "Unavailable",
            "tone": "neutral",
            "dist20": 0.0,
            "dist50": 0.0,
            "ma20_direction": "-",
            "vix": 0.0,
            "vix_label": "Unavailable",
            "message": f"大盘环境暂不可用：{exc}",
        }


def render_market_environment_bar(env: dict[str, object] | None = None) -> str:
    env = env or market_environment()
    tone = html.escape(str(env.get("tone", "neutral")))
    return f"""
<section class="market-bar market-{tone}">
  <div>
    <strong>{html.escape(str(env.get("state", "Unavailable")))}</strong>
    <span>{html.escape(str(env.get("symbol", "QQQ")))} {html.escape(str(env.get("date", "-")))}</span>
  </div>
  <p>{html.escape(str(env.get("symbol", "QQQ")))} 距20MA {float(env.get("dist20", 0.0)):.2f}% / 距50MA {float(env.get("dist50", 0.0)):.2f}% / 20MA {html.escape(str(env.get("ma20_direction", "-")))} / VIX {float(env.get("vix", 0.0)):.1f} {html.escape(str(env.get("vix_label", "")))}。{html.escape(str(env.get("message", "")))}</p>
</section>
"""


def build_buy_hold(symbol: str, bars: list[Bar], initial_cash: float) -> dict[str, object]:
    first_close = bars[0].close
    curve = [(bar.date, initial_cash * (bar.close / first_close)) for bar in bars]
    peak = initial_cash
    max_drawdown = 0.0
    for _, value in curve:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    return {
        "symbol": symbol,
        "return_pct": (bars[-1].close / first_close - 1) * 100,
        "max_drawdown_pct": max_drawdown,
        "curve": curve,
    }


def watchlist_default_payload() -> dict[str, object]:
    return {"items": []}


def load_watchlist_items() -> list[dict[str, str]]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        payload = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = [{"symbol": symbol} for symbol in payload.get("symbols", [])]
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_items:
        if isinstance(raw, str):
            raw = {"symbol": raw}
        if not isinstance(raw, dict):
            continue
        symbol = normalize_yahoo_symbol(str(raw.get("symbol", "")))
        if not symbol:
            continue
        symbol = symbol.upper()
        if symbol in seen:
            continue
        seen.add(symbol)
        items.append(
            {
                "symbol": symbol,
                "group": str(raw.get("group", "") or "观察"),
                "note": str(raw.get("note", "") or ""),
                "added_at": str(raw.get("added_at", "") or ""),
            }
        )
    return items


def load_watchlist() -> list[str]:
    return [item["symbol"] for item in load_watchlist_items()]


def save_watchlist_items(items: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = watchlist_default_payload()
    clean_items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        symbol = normalize_yahoo_symbol(str(item.get("symbol", "")))
        if not symbol:
            continue
        symbol = symbol.upper()
        if symbol in seen:
            continue
        seen.add(symbol)
        clean_items.append(
            {
                "symbol": symbol,
                "group": str(item.get("group", "") or "观察").strip()[:40],
                "note": str(item.get("note", "") or "").strip()[:240],
                "added_at": str(item.get("added_at", "") or time.strftime("%Y-%m-%d %H:%M:%S")),
            }
        )
    payload["items"] = clean_items
    payload["symbols"] = [item["symbol"] for item in clean_items]
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    WATCHLIST_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def save_watchlist(symbols: list[str]) -> None:
    save_watchlist_items([{"symbol": symbol} for symbol in symbols])


def add_watchlist_symbol(symbol: str, group: str = "观察", note: str = "") -> list[str]:
    clean = normalize_yahoo_symbol(symbol)
    if not clean:
        raise ValueError("请输入股票代码。")
    clean = clean.upper()
    items = load_watchlist_items()
    for item in items:
        if item["symbol"] == clean:
            if group:
                item["group"] = group.strip()[:40]
            if note:
                item["note"] = note.strip()[:240]
            save_watchlist_items(items)
            return [row["symbol"] for row in items]
    items.append({"symbol": clean, "group": group or "观察", "note": note, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    save_watchlist_items(items)
    return [row["symbol"] for row in items]


def delete_watchlist_symbol(symbol: str) -> list[str]:
    clean = normalize_yahoo_symbol(symbol)
    if not clean:
        return load_watchlist()
    clean = clean.upper()
    items = [item for item in load_watchlist_items() if item["symbol"].upper() != clean]
    save_watchlist_items(items)
    return [item["symbol"] for item in items]


def update_watchlist_symbol(symbol: str, group: str, note: str) -> list[str]:
    clean = normalize_yahoo_symbol(symbol)
    if not clean:
        raise ValueError("缺少股票代码。")
    clean = clean.upper()
    items = load_watchlist_items()
    for item in items:
        if item["symbol"] == clean:
            item["group"] = group.strip()[:40] or "观察"
            item["note"] = note.strip()[:240]
            save_watchlist_items(items)
            return [row["symbol"] for row in items]
    raise ValueError(f"{clean} 不在自选池中。")


def ashare_watchlist_path() -> Path:
    return DATA_DIR / "ashare" / "watchlist.json"


def normalize_ashare_code_for_storage(symbol: str) -> str:
    clean = "".join(ch for ch in symbol.strip().upper() if ch.isalnum())
    if clean.startswith(("SH", "SS", "SZ", "BJ")):
        clean = clean[2:]
    if clean.endswith(("SH", "SS", "SZ", "BJ")):
        clean = clean[:6]
    if len(clean) != 6 or not clean.isdigit():
        raise ValueError("请输入 6 位 A 股代码，例如 600487。")
    return clean


def load_ashare_watchlist_items() -> list[dict[str, str]]:
    path = ashare_watchlist_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in payload.get("items", []):
        if not isinstance(raw, dict):
            continue
        try:
            symbol = normalize_ashare_code_for_storage(str(raw.get("symbol", "")))
        except Exception:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        items.append(
            {
                "symbol": symbol,
                "name": str(raw.get("name", "") or ""),
                "sector": str(raw.get("sector", "") or ""),
                "group": str(raw.get("group", "") or "观察"),
                "note": str(raw.get("note", "") or ""),
                "added_at": str(raw.get("added_at", "") or ""),
            }
        )
    return items


def save_ashare_watchlist_items(items: list[dict[str, str]]) -> None:
    path = ashare_watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        try:
            symbol = normalize_ashare_code_for_storage(str(item.get("symbol", "")))
        except Exception:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        clean_items.append(
            {
                "symbol": symbol,
                "name": str(item.get("name", "") or "").strip()[:80],
                "sector": str(item.get("sector", "") or "").strip()[:80],
                "group": str(item.get("group", "") or "观察").strip()[:40],
                "note": str(item.get("note", "") or "").strip()[:240],
                "added_at": str(item.get("added_at", "") or time.strftime("%Y-%m-%d %H:%M:%S")),
            }
        )
    path.write_text(json.dumps({"items": clean_items, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2), encoding="utf-8")


def add_ashare_watchlist_symbol(symbol: str, group: str = "观察", note: str = "", name: str = "", sector: str = "") -> list[dict[str, str]]:
    clean = normalize_ashare_code_for_storage(symbol)
    if not name or not sector:
        try:
            fetched_name, fetched_sector = fetch_ashare_profile(clean)
            name = name or fetched_name
            sector = sector or fetched_sector
        except Exception:
            pass
    items = load_ashare_watchlist_items()
    for item in items:
        if item["symbol"] == clean:
            item["name"] = name or item.get("name", "")
            item["sector"] = sector or item.get("sector", "")
            item["group"] = group.strip()[:40] or item.get("group", "观察")
            if note:
                item["note"] = note.strip()[:240]
            save_ashare_watchlist_items(items)
            return items
    items.append({"symbol": clean, "name": name, "sector": sector, "group": group or "观察", "note": note, "added_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    save_ashare_watchlist_items(items)
    return items


def delete_ashare_watchlist_symbol(symbol: str) -> list[dict[str, str]]:
    try:
        clean = normalize_ashare_code_for_storage(symbol)
    except Exception:
        return load_ashare_watchlist_items()
    items = [item for item in load_ashare_watchlist_items() if item["symbol"] != clean]
    save_ashare_watchlist_items(items)
    return items


def price_cache_summary(symbols: list[str]) -> dict[str, object]:
    files = list(PRICE_CACHE_DIR.glob("*.csv")) if PRICE_CACHE_DIR.exists() else []
    total_size = sum(path.stat().st_size for path in files if path.exists())
    cached_symbols = 0
    latest_dates: list[str] = []
    for symbol in symbols:
        bars = read_price_cache(symbol)
        if bars:
            cached_symbols += 1
            latest_dates.append(max(bar.date for bar in bars))
    return {
        "files": len(files),
        "size_mb": total_size / 1_000_000,
        "cached_symbols": cached_symbols,
        "latest": max(latest_dates) if latest_dates else "-",
        "max_bars": PRICE_CACHE_MAX_BARS,
    }


def page_shell(content: str, active: str = "backtest", market: str = "us") -> bytes:
    prefix = "/cn" if market == "cn" else "/us"
    home_active = " active" if active == "home" else ""
    backtest_active = " active" if active == "backtest" else ""
    scanner_active = " active" if active == "scanner" else ""
    batch_active = " active" if active == "batch" else ""
    watchlist_active = " active" if active == "watchlist" else ""
    us_market_active = " active" if market == "us" else ""
    cn_market_active = " active" if market == "cn" else ""
    text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>MA5 Strategy Lab</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f0f3f7; color: #131722; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", Arial, sans-serif; font-size: 14px; }}
main {{ width: 100%; max-width: 1680px; margin: 0 auto; padding: 0 16px 24px; }}
.app-topbar {{ position: sticky; top: 0; z-index: 20; display: flex; justify-content: space-between; align-items: center; gap: 16px; height: 54px; margin: 0 -16px 16px; padding: 0 18px; background: #131722; border-bottom: 1px solid #2a2e39; box-shadow: 0 1px 3px rgba(19, 23, 34, .18); }}
.brand {{ display: flex; flex-direction: column; line-height: 1.1; color: #f8fafc; font-weight: 800; letter-spacing: 0; }}
.brand span {{ color: #9ca3af; font-size: 11px; font-weight: 600; margin-top: 3px; }}
.topbar-actions {{ display: flex; align-items: center; gap: 14px; min-width: 0; }}
.market-switch {{ display: flex; gap: 2px; padding: 2px; border: 1px solid #2a2e39; border-radius: 6px; background: #0f131d; }}
.market-switch a {{ padding: 6px 10px; border-radius: 4px; color: #d1d4dc; text-decoration: none; font-size: 12px; font-weight: 900; white-space: nowrap; }}
.market-switch a:hover {{ background: #1f2430; color: #fff; }}
.market-switch a.active {{ background: #2962ff; color: #fff; }}
.tabs {{ display: flex; gap: 2px; margin: 0; }}
.tabs a {{ padding: 8px 12px; border: 1px solid transparent; border-radius: 4px; color: #d1d4dc; text-decoration: none; font-size: 13px; font-weight: 700; }}
.tabs a:hover {{ background: #1f2430; color: #fff; }}
.tabs a.active {{ background: #2962ff; color: #fff; border-color: #2962ff; }}
h1 {{ margin: 0 0 6px; font-size: 22px; line-height: 1.25; letter-spacing: 0; }}
h2 {{ margin: 18px 0 10px; font-size: 16px; }}
.hint {{ color: #5d6675; font-size: 13px; margin: 0 0 14px; line-height: 1.55; }}
.form {{ display: grid; grid-template-columns: repeat(8, minmax(116px, 1fr)); gap: 10px; align-items: end; background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
label {{ display: block; font-size: 12px; color: #5d6675; font-weight: 700; }}
.checkbox-label {{ display: flex; align-items: center; gap: 8px; min-height: 38px; color: #334155; }}
.checkbox-label input {{ width: auto; margin: 0; }}
input, select, textarea {{ width: 100%; margin-top: 6px; padding: 8px 9px; border: 1px solid #c7ccd5; border-radius: 4px; background: #fff; color: #131722; font-family: inherit; font-size: 13px; outline: none; }}
input:focus, select:focus, textarea:focus {{ border-color: #2962ff; box-shadow: 0 0 0 2px rgba(41, 98, 255, .12); }}
textarea {{ min-height: 78px; resize: vertical; line-height: 1.45; }}
button, .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; padding: 8px 13px; border: 1px solid #2962ff; border-radius: 4px; background: #2962ff; color: #fff; font: inherit; font-size: 13px; font-weight: 800; line-height: 1.2; text-decoration: none; cursor: pointer; transition: background-color .12s ease, border-color .12s ease, color .12s ease, box-shadow .12s ease, transform .06s ease; }}
button:hover, .btn:hover {{ filter: none; background: #1e53e5; border-color: #1e53e5; color: #fff; text-decoration: none; }}
button:active, .btn:active {{ transform: translateY(1px); }}
button:focus-visible, .btn:focus-visible {{ outline: none; box-shadow: 0 0 0 2px rgba(41, 98, 255, .18); }}
button:disabled, .btn.disabled {{ cursor: not-allowed; opacity: .62; transform: none; }}
button.secondary, .btn-secondary {{ background: #fff; color: #334155; border-color: #c7ccd5; }}
button.secondary:hover, .btn-secondary:hover {{ background: #f8fafc; border-color: #aeb7c5; color: #131722; }}
button.success, .btn-success {{ background: #089981; border-color: #089981; }}
button.success:hover, .btn-success:hover {{ background: #067a6b; border-color: #067a6b; }}
button.danger, .btn-danger {{ background: #fff; border-color: #f3a6ad; color: #d12030; }}
button.danger:hover, .btn-danger:hover {{ background: #fff5f6; border-color: #f23645; color: #b42332; }}
.btn-small {{ min-height: 26px; padding: 4px 8px; font-size: 12px; }}
.btn-loading::before {{ content: ""; width: 12px; height: 12px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .75s linear infinite; }}
.symbol-button {{ border: 1px solid transparent; background: transparent; color: #2962ff; padding: 2px 4px; min-height: 24px; font: inherit; font-weight: 800; cursor: pointer; border-radius: 4px; }}
.symbol-button:hover {{ background: rgba(41, 98, 255, .08); border-color: rgba(41, 98, 255, .16); color: #1e53e5; text-decoration: none; }}
.wide {{ grid-column: span 3; }}
.page-head {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 12px; }}
.mode-pill {{ background: #131722; color: #f8fafc; border-radius: 999px; padding: 6px 10px; font-size: 12px; white-space: nowrap; }}
.status-strip {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 8px; margin: 0 0 14px; }}
.stat-card {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 10px 12px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.stat-label {{ color: #6b7280; font-size: 11px; font-weight: 800; text-transform: uppercase; margin-bottom: 6px; }}
.stat-value {{ color: #131722; font-size: 18px; font-weight: 800; }}
.market-bar {{ display: flex; align-items: center; justify-content: space-between; gap: 14px; border: 1px solid #d6dbe3; border-left-width: 4px; border-radius: 6px; background: #fff; padding: 10px 12px; margin: 0 0 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.market-bar div {{ display: flex; align-items: baseline; gap: 8px; white-space: nowrap; }}
.market-bar strong {{ font-size: 15px; }}
.market-bar span, .market-bar p {{ color: #5d6675; font-size: 13px; margin: 0; line-height: 1.45; }}
.market-good {{ border-left-color: #089981; }}
.market-warn {{ border-left-color: #f59e0b; }}
.market-bad {{ border-left-color: #f23645; }}
.market-neutral {{ border-left-color: #94a3b8; }}
.toolbar {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }}
.toolbar .links {{ margin: 0; }}
.latest-scan-card {{ margin: 0 0 20px; }}
.latest-scan-card .toolbar {{ margin-bottom: 0; align-items: flex-start; }}
.scan-facts {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 9px; }}
.scan-fact {{ display: inline-flex; gap: 6px; align-items: center; border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 999px; padding: 5px 9px; font-size: 12px; color: #334155; }}
.scan-fact span {{ color: #64748b; font-weight: 700; }}
.inline-actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
.delete-form {{ display: inline; margin: 0; }}
.delete-link {{ display: inline-flex; align-items: center; justify-content: center; min-height: 26px; padding: 4px 8px; border: 1px solid #f3a6ad; border-radius: 4px; background: #fff; color: #d12030; font-size: 12px; font-weight: 800; cursor: pointer; text-decoration: none; }}
.delete-link:hover {{ background: #fff5f6; border-color: #f23645; color: #b42332; text-decoration: none; filter: none; }}
.mini-action {{ min-height: 26px; padding: 4px 8px; font-size: 12px; border-color: #c7ccd5; background: #fff; color: #2962ff; }}
.mini-action.added {{ color: #089981; border-color: #9fd8cc; background: rgba(8,153,129,.08); }}
.rating {{ display: inline-block; min-width: 64px; text-align: center; border-radius: 4px; padding: 3px 8px; font-weight: 800; font-size: 12px; }}
.rating-Strong {{ color: #067a6b; background: rgba(8, 153, 129, .12); }}
.rating-Medium {{ color: #b26b00; background: rgba(245, 158, 11, .16); }}
.rating-Weak {{ color: #c22736; background: rgba(242, 54, 69, .13); }}
.score-badge {{ display: inline-flex; align-items: center; justify-content: center; min-width: 46px; border-radius: 4px; padding: 3px 7px; font-weight: 900; font-size: 12px; }}
.score-Strong {{ color: #067a6b; background: rgba(8, 153, 129, .12); }}
.score-Medium {{ color: #b26b00; background: rgba(245, 158, 11, .16); }}
.score-Weak {{ color: #c22736; background: rgba(242, 54, 69, .13); }}
.badge {{ display: inline-flex; align-items: center; justify-content: center; border-radius: 4px; padding: 3px 7px; font-weight: 800; font-size: 12px; white-space: nowrap; }}
.earnings-safe {{ color: #334155; background: #eef2f7; }}
.earnings-watch {{ color: #915d00; background: rgba(245, 158, 11, .16); }}
.earnings-danger {{ color: #b42332; background: rgba(242, 54, 69, .13); }}
.earnings-unknown {{ color: #64748b; background: #f1f5f9; }}
.error {{ background: #fff5f6; border: 1px solid #ffc9cf; color: #b42332; padding: 12px; border-radius: 6px; white-space: pre-wrap; }}
.result {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; margin-top: 14px; margin-bottom: 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.candidate-detail {{ margin-top: 14px; }}
.candidate-detail iframe {{ height: 980px; }}
.watchlist-grid {{ display: grid; grid-template-columns: 340px minmax(0, 1fr); gap: 14px; align-items: start; }}
.watchlist-panel {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.watchlist-chart-shell {{ position: relative; height: 680px; min-width: 520px; }}
.watchlist-chart {{ width: 100%; height: 100%; }}
.watchlist-list-wrap {{ max-height: 760px; overflow: auto; }}
.watch-row-button {{ width: 100%; justify-content: flex-start; border: 0; background: transparent; color: #131722; padding: 0; min-height: 0; text-align: left; }}
.watch-row-button:hover {{ background: transparent; color: #2962ff; }}
.watch-row-cell {{ min-width: 220px; }}
.watch-row-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }}
.watch-row-actions {{ display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto; }}
.watch-row-actions .delete-link {{ min-height: 22px; padding: 2px 6px; font-size: 11px; }}
.watch-symbol-line {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 900; }}
.watch-meta-line {{ margin-top: 4px; color: #64748b; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }}
.watch-detail-grid {{ display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 8px; margin: 10px 0 12px; }}
.watch-detail-item {{ border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 6px; padding: 8px 9px; }}
.watch-detail-item span {{ display: block; color: #64748b; font-size: 11px; font-weight: 800; margin-bottom: 4px; }}
.watch-detail-item strong {{ font-size: 13px; }}
.loading-overlay {{ position: absolute; inset: 0; z-index: 5; display: none; align-items: center; justify-content: center; flex-direction: column; gap: 10px; background: rgba(255,255,255,.82); color: #334155; font-weight: 800; }}
.loading-overlay.active {{ display: flex; }}
.spinner {{ width: 28px; height: 28px; border: 3px solid #d6dbe3; border-top-color: #2962ff; border-radius: 50%; animation: spin .8s linear infinite; }}
.toast-stack {{ position: fixed; right: 18px; top: 68px; z-index: 80; display: grid; gap: 8px; width: min(340px, calc(100vw - 32px)); pointer-events: none; }}
.toast {{ background: #131722; color: #fff; border: 1px solid #2a2e39; border-radius: 6px; box-shadow: 0 12px 28px rgba(15,23,42,.2); padding: 10px 12px; font-size: 13px; font-weight: 700; opacity: 0; transform: translateY(-6px); animation: toastIn .16s ease forwards; }}
.toast.success {{ background: #087f6f; border-color: #087f6f; }}
.toast.error {{ background: #b42332; border-color: #b42332; }}
.table-input {{ min-width: 90px; margin: 0; padding: 5px 7px; font-size: 12px; }}
.table-note {{ min-width: 220px; height: 34px; min-height: 34px; margin: 0; padding: 5px 7px; font-size: 12px; resize: horizontal; }}
.watchlist-panel td form {{ display: inline-flex; gap: 6px; margin-right: 8px; vertical-align: middle; }}
.watchlist-panel td button.secondary {{ min-height: 28px; padding: 4px 8px; font-size: 12px; }}
.period-tabs {{ display: flex; gap: 4px; margin: 0 0 10px; flex-wrap: wrap; }}
.period-tabs button {{ min-height: 28px; padding: 4px 9px; border-color: #c7ccd5; background: #fff; color: #334155; font-size: 12px; }}
.period-tabs button.active {{ border-color: #2962ff; background: #2962ff; color: #fff; }}
.watchlist-empty {{ color: #607080; padding: 18px 0; text-align: center; }}
.chart-tooltip {{ position: absolute; z-index: 6; display: none; min-width: 220px; pointer-events: none; border: 1px solid #d6dbe3; background: rgba(255,255,255,0.96); border-radius: 6px; box-shadow: 0 8px 22px rgba(15,23,42,0.12); padding: 8px 10px; font-size: 12px; line-height: 1.6; }}
.chart-tooltip strong {{ display: block; margin-bottom: 4px; font-size: 13px; }}
.chart-tooltip .up {{ color: #089981; }}
.chart-tooltip .down {{ color: #f23645; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
@keyframes toastIn {{ to {{ opacity: 1; transform: translateY(0); }} }}
.progress-box {{ display: none; background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; margin-top: 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.progress-track {{ height: 8px; background: #e6eaf0; border-radius: 999px; overflow: hidden; margin: 8px 0; }}
.progress-bar {{ height: 100%; width: 0%; background: #2962ff; transition: width .2s ease; }}
.progress-meta {{ color: #475569; font-size: 13px; }}
.progress-actions {{ display: flex; gap: 8px; margin-top: 10px; }}
.progress-actions button[hidden] {{ display: none; }}
.dashboard-grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; }}
.dashboard-panel {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.dashboard-panel h2 {{ margin-top: 0; }}
.quick-actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.links {{ margin: 0 0 12px; font-size: 13px; }}
.links a {{ color: #2962ff; text-decoration: none; margin-right: 12px; font-weight: 700; }}
.links a:hover {{ text-decoration: underline; }}
iframe {{ width: 100%; height: 1320px; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; }}
table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #fff; }}
.table-wrap {{ width: 100%; overflow: auto; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; max-height: 680px; }}
.table-wrap table {{ width: max-content; min-width: 100%; table-layout: auto; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eef1f5; text-align: right; font-size: 12px; white-space: nowrap; }}
tbody tr:hover td {{ background: #f8fafc; }}
th {{ background: #f5f7fa; color: #5d6675; position: sticky; top: 0; z-index: 8; font-size: 11px; font-weight: 800; text-transform: uppercase; border-bottom: 1px solid #d6dbe3; }}
th:first-child, td:first-child {{ position: sticky; left: 0; background: #fff; z-index: 3; box-shadow: 1px 0 0 #eef1f5; }}
th:first-child {{ background: #f5f7fa; z-index: 10; }}
th.resizable {{ position: sticky; user-select: none; }}
.col-resizer {{ position: absolute; top: 0; right: -3px; width: 6px; height: 100%; cursor: col-resize; z-index: 2; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6), th:nth-child(7), td:nth-child(7) {{ text-align: left; }}
.empty {{ text-align: center; color: #607080; }}
@media (max-width: 1200px) {{ .form {{ grid-template-columns: repeat(4, 1fr); }} .watchlist-grid {{ grid-template-columns: 1fr; }} .watchlist-chart-shell {{ min-width: 0; }} }}
@media (max-width: 760px) {{ main {{ padding: 0 10px 18px; }} .app-topbar {{ margin: 0 -10px 12px; height: auto; padding: 10px; align-items: flex-start; flex-direction: column; }} .topbar-actions {{ width: 100%; flex-direction: column; align-items: stretch; gap: 8px; }} .market-switch, .tabs {{ width: 100%; overflow-x: auto; }} .form, .status-strip {{ grid-template-columns: repeat(2, 1fr); }} .wide {{ grid-column: span 2; }} .page-head {{ display: block; }} }}
</style>
</head>
<body><main>
<header class="app-topbar">
  <div class="brand">MA5 Strategy Lab<span>选股 | 自选 | 回测</span></div>
  <div class="topbar-actions">
    <nav class="market-switch" aria-label="市场切换">
      <a class="{us_market_active}" href="/us">美股</a>
      <a class="{cn_market_active}" href="/cn">A股</a>
    </nav>
    <nav class="tabs">
      <a class="{home_active}" href="{prefix}">首页</a>
      <a class="{scanner_active}" href="{prefix}/scanner">选股器</a>
      <a class="{watchlist_active}" href="{prefix}/watchlist">自选池</a>
      <a class="{backtest_active}" href="{prefix}/backtest">回测</a>
      <a class="{batch_active}" href="{prefix}/batch">批量回测</a>
    </nav>
  </div>
</header>
{content}
<div class="toast-stack" id="toast-stack"></div>
<script>
window.showToast = function(message, type = "success") {{
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast ${{type}}`;
  toast.textContent = message;
  stack.appendChild(toast);
  window.setTimeout(() => {{
    toast.style.opacity = "0";
    toast.style.transform = "translateY(-6px)";
    window.setTimeout(() => toast.remove(), 180);
  }}, 2600);
}};

document.addEventListener("submit", event => {{
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.dataset.asyncSubmit === "true") return;
  const button = form.querySelector("button[type='submit']");
  if (!button || button.dataset.loadingReady === "true") return;
  button.dataset.loadingReady = "true";
  button.dataset.originalText = button.textContent || "";
  button.classList.add("btn-loading");
  button.disabled = true;
  button.textContent = button.dataset.loadingText || "处理中";
}});
</script>
</main></body>
</html>"""
    return text.encode("utf-8")


def render_backtest_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    today = date.today()
    preset = field(params, "preset", "1y")
    start_default = start_for_preset(preset, today).isoformat()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""

    return f"""
<section class="page-head">
  <div>
    <h1>本地 Strategy Tester</h1>
    <p class="hint">输入股票代码，选择回测周期后运行。数据会从 yfinance 拉取最新可用日线，默认对比纳斯达克综合指数 ^IXIC。</p>
  </div>
  <div class="mode-pill">Backtest | Daily</div>
</section>
<form class="form" action="/run" method="get" id="backtest-form">
  <label>股票代码<input name="symbol" value="{value("symbol", "AAPL").upper()}" placeholder="AAPL"></label>
  <input type="hidden" name="strategy_name" value="ratchet">
  <label>回测周期
    <select name="preset" id="preset">
      <option value="6m"{selected(preset, "6m")}>近 6 个月</option>
      <option value="1y"{selected(preset, "1y")}>近 1 年</option>
      <option value="3y"{selected(preset, "3y")}>近 3 年</option>
      <option value="5y"{selected(preset, "5y")}>近 5 年</option>
      <option value="custom"{selected(preset, "custom")}>自定义</option>
    </select>
  </label>
  <label>开始日期<input type="date" name="start" id="start" value="{value("start", start_default)}"></label>
  <label>结束日期<input type="date" name="end" id="end" value="{value("end", today.isoformat())}"></label>
  <label>对比基准<input name="benchmark" value="{value("benchmark", DEFAULT_BENCHMARK).upper()}" placeholder="^IXIC"></label>
  <label>初始资金<input name="initial_cash" value="{value("initial_cash", "100000")}"></label>
  <label>手续费 %<input name="commission_pct" value="{value("commission_pct", "0.1")}"></label>
  <label>滑点 %<input name="slippage_pct" value="{value("slippage_pct", "0")}"></label>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" id="vol_length" value="{value("vol_length", "20")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>跌破均线止损 %<input name="stop_5ma_pct" value="{value("stop_5ma_pct", "7.5")}"></label>
  <label>B点追踪止损 %<input name="hard_stop_pct" value="{value("hard_stop_pct", "20")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <button type="submit">运行回测</button>
</form>
<script>
const preset = document.getElementById("preset");
const start = document.getElementById("start");
const end = document.getElementById("end");
const volLength = document.getElementById("vol_length");
const backtestForm = document.getElementById("backtest-form");
function isoDate(d) {{ return d.toISOString().slice(0, 10); }}
function applyPreset() {{
  const value = preset.value;
  if (value === "custom") return;
  const endDate = new Date(end.value || new Date());
  const startDate = new Date(endDate);
  if (value === "6m") startDate.setMonth(startDate.getMonth() - 6);
  if (value === "1y") startDate.setFullYear(startDate.getFullYear() - 1);
  if (value === "3y") startDate.setFullYear(startDate.getFullYear() - 3);
  if (value === "5y") startDate.setFullYear(startDate.getFullYear() - 5);
  start.value = isoDate(startDate);
}}
preset.addEventListener("change", applyPreset);
end.addEventListener("change", applyPreset);
backtestForm.addEventListener("submit", (event) => {{
  const startDate = new Date(start.value);
  const endDate = new Date(end.value);
  const volDays = Number.parseInt(volLength.value || "20", 10);
  const minDays = Math.max(30, volDays + 10);
  const diffDays = Math.round((endDate - startDate) / 86400000);
  if (!Number.isFinite(diffDays) || diffDays < minDays) {{
    event.preventDefault();
    alert(`回测区间至少需要 ${{minDays}} 天。当前均量周期为 ${{volDays}}，请扩大日期范围。`);
  }}
}});
</script>
"""


def render_dashboard() -> str:
    latest = load_latest_scan()
    latest_summary = latest.get("summary", {}) if latest else {}
    latest_signal = str(latest.get("signal_date", "-")) if latest else "-"
    latest_candidates = int(latest_summary.get("visible_candidates", 0)) if latest else 0
    latest_strong = int(latest_summary.get("strong", 0)) if latest else 0
    latest_medium = int(latest_summary.get("medium", 0)) if latest else 0
    watch_items = load_watchlist_items()
    symbols = [item["symbol"] for item in watch_items]
    cache = price_cache_summary(symbols)
    earnings_soon = 0
    if symbols:
        temp_rows = [
            SignalResult(
                symbol=symbol,
                signal_date="",
                close=0,
                ma=0,
                dist_to_ma_pct=0,
                volume=0,
                vol_ma=0,
                volume_ratio=0,
                massive_count_7d=0,
                signal_type="",
                avg_dollar_volume_20d=0,
            )
            for symbol in symbols[:50]
        ]
        enrich_earnings_dates(temp_rows)
        earnings_soon = sum(1 for row in temp_rows if row.next_earnings_date and row.earnings_days <= 7)

    return f"""
<section class="page-head">
  <div>
    <h1>今日复盘面板</h1>
    <p class="hint">盘后先看大盘环境，再看今日扫描和自选池风险；需要验证时再进入回测。</p>
  </div>
  <div class="mode-pill">Daily Review</div>
</section>
{render_market_environment_bar()}
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <h2>今日扫描</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(latest_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{latest_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">Strong</div><div class="stat-value">{latest_strong}</div></div>
      <div class="stat-card"><div class="stat-label">Medium</div><div class="stat-value">{latest_medium}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/us/scanner">开始选股</a>
      <a class="btn btn-secondary" href="/us/scan/latest">查看今日结果</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>自选池</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">自选数量</div><div class="stat-value">{len(symbols)}</div></div>
      <div class="stat-card"><div class="stat-label">7天内财报</div><div class="stat-value">{earnings_soon}</div></div>
      <div class="stat-card"><div class="stat-label">缓存覆盖</div><div class="stat-value">{cache["cached_symbols"]}/{len(symbols)}</div></div>
      <div class="stat-card"><div class="stat-label">数据最新</div><div class="stat-value">{html.escape(str(cache["latest"]))}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/us/watchlist">打开自选池</a>
      <a class="btn btn-secondary" href="/us/backtest">单票回测</a>
      <a class="btn btn-secondary" href="/us/batch">批量回测</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>快捷入口</h2>
    <div class="quick-actions">
      <a class="btn" href="/us/scanner">选股器</a>
      <a class="btn btn-secondary" href="/us/watchlist">自选池</a>
      <a class="btn btn-secondary" href="/us/backtest">回测</a>
      <a class="btn btn-secondary" href="/us/batch">批量回测</a>
    </div>
  </div>
</section>
"""


def render_market_placeholder(active: str = "home") -> str:
    if active == "home":
        return """
<section class="page-head">
  <div>
    <h1>A股复盘面板</h1>
    <p class="hint">当前已开放 A 股选股器：支持单票验证和盘后批量选股。A 股自选池、回测和批量回测后续再独立实现。</p>
  </div>
  <div class="mode-pill">A Share | Scanner Ready</div>
</section>
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <h2>A股选股器</h2>
    <p class="hint">硬条件为趋势通过 + J值冰点，红长绿短量能用于二次看图确认强弱。</p>
    <div class="quick-actions">
      <a class="btn" href="/cn/scanner">打开 A 股选股器</a>
      <a class="btn btn-secondary" href="/cn/scanner?mode=single&symbol=600487&j_threshold=14">验证 600487</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>A股自选池</h2>
    <p class="hint">A 股自选池已独立保存，可从选股结果加入，也可以手动添加代码后看策略图。</p>
    <div class="quick-actions">
      <a class="btn btn-secondary" href="/cn/watchlist">打开 A 股自选池</a>
      <a class="btn btn-secondary" href="/cn/backtest">A 股回测占位</a>
    </div>
  </div>
</section>
"""
    labels = {
        "home": ("A股复盘面板", "后续这里会显示 A 股市场环境、A 股扫描摘要和 A 股自选池状态。"),
        "scanner": ("A股选股器", "后续这里会接入 A 股股票池、A 股策略和 A 股盘后选股。"),
        "watchlist": ("A股自选池", "后续这里会维护 A 股自选列表，并和美股自选池分开保存。"),
        "backtest": ("A股回测", "后续这里会使用 A 股独立交易规则、手续费、印花税、涨跌停和复权设置。"),
        "batch": ("A股批量回测", "后续这里会批量验证 A 股策略参数和股票池表现。"),
    }
    title, description = labels.get(active, labels["home"])
    return f"""
<section class="page-head">
  <div>
    <h1>{html.escape(title)}</h1>
    <p class="hint">{html.escape(description)}</p>
  </div>
  <div class="mode-pill">A Share | Reserved</div>
</section>
<section class="result">
  <h2>暂未开放</h2>
  <p class="hint">A 股数据源、策略、选股池、回测规则会从独立模块开始实现，不和当前美股 MA5 逻辑混在一起。</p>
</section>
"""


def render_ashare_scan_result(
    candidates: list[AShareSignalSnapshot],
    errors: list[tuple[str, str]],
    scanned: int,
    universe_source: str,
    market_cap_filter_applied: bool,
) -> str:
    rows = []
    rating_class = {"Strong": "score-Strong", "Medium": "score-Medium", "Watch": "score-Medium"}
    for row in candidates:
        cls = rating_class.get(row.candidate_rating, "score-Weak")
        rows.append(
            "<tr>"
            f"<td>{html.escape(row.symbol)}</td>"
            f"<td>{html.escape(row.name or '-')}</td>"
            f"<td>{html.escape(row.sector or '-')}</td>"
            f"<td><span class=\"score-badge {cls}\">{html.escape(row.candidate_rating)}</span></td>"
            f"<td>{row.volume_score:.1f}/5</td>"
            f"<td>{row.j_value:.2f}</td>"
            f"<td>{row.close:.2f}</td>"
            f"<td>{html.escape(row.latest_date)}</td>"
            f"<td>{row.recent_peak_to_base:.2f}</td>"
            f"<td>{row.recent_avg10_to_base:.2f}</td>"
            f"<td>{row.red_avg_to_green_avg:.2f}</td>"
            f"<td>{row.top5_red_count}/5</td>"
            f"<td>{html.escape(row.data_source)}</td>"
            f'<td><a class="btn btn-secondary btn-small" href="/cn/watchlist/add?symbol={quote(row.symbol)}&name={quote(row.name or "")}&sector={quote(row.sector or "")}">加入自选</a></td>'
            "</tr>"
        )
    table_html = (
        """
  <div class="table-wrap">
    <table class="sortable resizable-table">
      <thead><tr><th>代码</th><th>名称</th><th>板块</th><th>评级</th><th>量能分</th><th>J值</th><th>收盘</th><th>交易日</th><th>峰值/基准</th><th>10日均/基准</th><th>红均/绿均</th><th>Top5红柱</th><th>数据源</th><th>操作</th></tr></thead>
      <tbody>
"""
        + "\n".join(rows)
        + """
      </tbody>
    </table>
  </div>
"""
        if rows
        else '<p class="hint">本次没有筛出候选。可以适当放宽 J 值阈值，或降低市值/扫描数量限制后再试。</p>'
    )
    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {reason[:80]}" for symbol, reason in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 只股票扫描失败：{html.escape(sample)}</p>'
    cap_note = "已按总市值过滤" if market_cap_filter_applied else "总市值接口不可用，本次按行情接口取前 N 只，市值过滤未生效"
    return f"""
<section class="result">
  <div class="toolbar">
    <div>
      <h2>A股选股结果</h2>
      <p class="hint">入选硬条件为趋势通过 + J值冰点；量能分用于二次看图确认。股票池来源：{html.escape(universe_source)}。</p>
    </div>
  </div>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">扫描数量</div><div class="stat-value">{scanned}</div></div>
    <div class="stat-card"><div class="stat-label">候选数量</div><div class="stat-value">{len(candidates)}</div></div>
    <div class="stat-card"><div class="stat-label">市值过滤</div><div class="stat-value">{html.escape(cap_note)}</div></div>
    <div class="stat-card"><div class="stat-label">失败数量</div><div class="stat-value">{len(errors)}</div></div>
  </section>
  {table_html}
  {error_note}
</section>
"""


def render_ashare_scanner(params: dict[str, list[str]]) -> str:
    mode = field(params, "mode", "")
    symbol = field(params, "symbol", "600487")
    j_threshold = number_field(params, "j_threshold", 14.0)
    min_market_cap = number_field(params, "min_market_cap", 50.0)
    max_symbols = int(number_field(params, "max_symbols", 300))
    result_html = ""
    if mode == "market":
        try:
            scan = scan_ashare_candidates(min_market_cap, max_symbols, j_threshold)
            result_html = render_ashare_scan_result(
                scan.candidates,
                scan.errors,
                scan.scanned,
                scan.universe_source,
                scan.market_cap_filter_applied,
            )
        except Exception as exc:
            result_html = f'<div class="error">{html.escape(str(exc))}</div>'
    elif mode == "single" and symbol.strip():
        try:
            snapshot = latest_ashare_signal(symbol, j_threshold)
            chart_payload = ashare_chart_payload(symbol, j_threshold)

            def status_badge(value: bool) -> str:
                cls = "score-Strong" if value else "score-Weak"
                text = "通过" if value else "未通过"
                return f'<span class="score-badge {cls}">{text}</span>'

            rating_cls = "score-Strong" if snapshot.candidate_rating == "Strong" else "score-Medium" if snapshot.candidate_rating in ("Medium", "Watch") else "score-Weak"
            signal_text = {
                "Strong": "强候选",
                "Medium": "中等候选",
                "Watch": "待看图",
                "None": "未入选",
            }.get(snapshot.candidate_rating, snapshot.candidate_rating)
            name = f" / {snapshot.name}" if snapshot.name else ""
            sector = snapshot.sector or "-"
            result_html = f"""
<section class="result">
  <div class="toolbar">
    <div>
      <h2>{html.escape(snapshot.symbol)}{html.escape(name)} 策略验证</h2>
      <p class="hint">当前只做单票验证，用于确认 A 股策略指标和量能结构；全市场扫描会在这个模块稳定后接入。</p>
    </div>
    <span class="score-badge {rating_cls}">{signal_text}</span>
  </div>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">最新交易日</div><div class="stat-value">{html.escape(snapshot.latest_date)}</div></div>
    <div class="stat-card"><div class="stat-label">收盘价</div><div class="stat-value">{snapshot.close:.2f}</div></div>
    <div class="stat-card"><div class="stat-label">量能评分</div><div class="stat-value">{snapshot.volume_score:.1f}/5</div></div>
    <div class="stat-card"><div class="stat-label">板块</div><div class="stat-value">{html.escape(sector)}</div></div>
  </section>
  <p class="hint">数据源 / 日K：{html.escape(snapshot.data_source)} / {snapshot.bars_count}</p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>条件</th><th>结果</th><th>关键数值</th><th>说明</th></tr></thead>
      <tbody>
        <tr>
          <td>趋势条件</td>
          <td>{status_badge(snapshot.trend_ok)}</td>
          <td>短期趋势 {snapshot.zx_short_trend:.2f} / 多空线 {snapshot.zx_multi_trend:.2f} / 斜率 {snapshot.zx_multi_slope:.2f}</td>
          <td>短期趋势线在多空线之上，且多空线向上。</td>
        </tr>
        <tr>
          <td>KDJ J 超卖</td>
          <td>{status_badge(snapshot.j_oversold)}</td>
          <td>J={snapshot.j_value:.2f} / 阈值 {j_threshold:.2f}</td>
          <td>用于寻找强趋势中的短线冰点。</td>
        </tr>
        <tr>
          <td>红长绿短量能</td>
          <td><span class="score-badge {rating_cls}">{snapshot.volume_score:.1f}/5</span></td>
          <td>峰值/基准 {snapshot.recent_peak_to_base:.2f}，10日均量/基准 {snapshot.recent_avg10_to_base:.2f}，红均/绿均 {snapshot.red_avg_to_green_avg:.2f}</td>
          <td>该项用于二次看图确认强弱，不再作为入选硬条件。</td>
        </tr>
      </tbody>
    </table>
  </div>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">基准均量</div><div class="stat-value">{snapshot.base_volume / 10000:.1f}万</div></div>
    <div class="stat-card"><div class="stat-label">近20日峰值量</div><div class="stat-value">{snapshot.recent_peak_volume / 10000:.1f}万</div></div>
    <div class="stat-card"><div class="stat-label">红 / 绿天数</div><div class="stat-value">{snapshot.red_days} / {snapshot.green_days}</div></div>
    <div class="stat-card"><div class="stat-label">Top5 红柱数</div><div class="stat-value">{snapshot.top5_red_count} / 5</div></div>
  </section>
  <section class="result">
    <div class="toolbar">
      <div>
        <h2>策略图表</h2>
        <p class="hint">主图显示 K 线、知行短期趋势、知行多空线；下方显示成交量与 KDJ。</p>
      </div>
    </div>
    <div id="ashare-chart" style="height:780px;width:100%;"></div>
  </section>
  <script type="application/json" id="ashare-chart-data">{json.dumps(chart_payload, ensure_ascii=False)}</script>
  <script>
  (function() {{
    const payload = JSON.parse(document.getElementById("ashare-chart-data").textContent);
    const ohlc = payload.ohlc || [];
    const volume = payload.volume || [];
    const traces = [
      {{
        type: "candlestick",
        x: ohlc.map(row => row.x),
        open: ohlc.map(row => row.open),
        high: ohlc.map(row => row.high),
        low: ohlc.map(row => row.low),
        close: ohlc.map(row => row.close),
        name: "K线",
        increasing: {{ line: {{ color: "#ef4444" }}, fillcolor: "rgba(239,68,68,.28)" }},
        decreasing: {{ line: {{ color: "#06b6d4" }}, fillcolor: "rgba(6,182,212,.28)" }},
        xaxis: "x",
        yaxis: "y"
      }},
      {{
        type: "scatter",
        mode: "lines",
        x: payload.zx_short_trend.map(row => row.x),
        y: payload.zx_short_trend.map(row => row.y),
        name: "知行短期趋势",
        line: {{ color: "#2563eb", width: 2 }},
        xaxis: "x",
        yaxis: "y"
      }},
      {{
        type: "scatter",
        mode: "lines",
        x: payload.zx_multi_trend.map(row => row.x),
        y: payload.zx_multi_trend.map(row => row.y),
        name: "知行多空线",
        line: {{ color: "#dc2626", width: 2 }},
        xaxis: "x",
        yaxis: "y"
      }},
      {{
        type: "scatter",
        mode: "markers+text",
        x: payload.signals.map(row => row.x),
        y: payload.signals.map(row => row.y),
        text: payload.signals.map(row => row.text),
        textposition: "bottom center",
        marker: {{ color: "#16a34a", size: 10, symbol: "triangle-up" }},
        name: "B信号",
        xaxis: "x",
        yaxis: "y"
      }},
      {{
        type: "bar",
        x: volume.map(row => row.x),
        y: volume.map(row => row.y),
        marker: {{ color: volume.map(row => row.color) }},
        name: "成交量",
        xaxis: "x2",
        yaxis: "y2"
      }},
      {{
        type: "scatter",
        mode: "lines",
        x: payload.k.map(row => row.x),
        y: payload.k.map(row => row.y),
        name: "K",
        line: {{ color: "#2563eb", width: 1.5 }},
        xaxis: "x3",
        yaxis: "y3"
      }},
      {{
        type: "scatter",
        mode: "lines",
        x: payload.d.map(row => row.x),
        y: payload.d.map(row => row.y),
        name: "D",
        line: {{ color: "#f59e0b", width: 1.5 }},
        xaxis: "x3",
        yaxis: "y3"
      }},
      {{
        type: "scatter",
        mode: "lines",
        x: payload.j.map(row => row.x),
        y: payload.j.map(row => row.y),
        name: "J",
        line: {{ color: "#7c3aed", width: 1.8 }},
        xaxis: "x3",
        yaxis: "y3"
      }}
    ];
    const layout = {{
      margin: {{ l: 54, r: 24, t: 18, b: 32 }},
      paper_bgcolor: "#fff",
      plot_bgcolor: "#fff",
      hovermode: "x unified",
      dragmode: "pan",
      showlegend: true,
      legend: {{ orientation: "h", x: 0, y: 1.05 }},
      font: {{ family: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif", size: 12, color: "#131722" }},
      grid: {{ rows: 3, columns: 1, pattern: "independent", roworder: "top to bottom" }},
      xaxis: {{ rangeslider: {{ visible: false }}, showgrid: true, gridcolor: "#eef1f5" }},
      yaxis: {{ domain: [0.42, 1], showgrid: true, gridcolor: "#eef1f5" }},
      xaxis2: {{ matches: "x", showticklabels: false, showgrid: true, gridcolor: "#eef1f5" }},
      yaxis2: {{ domain: [0.24, 0.38], showgrid: true, gridcolor: "#eef1f5" }},
      xaxis3: {{ matches: "x", showgrid: true, gridcolor: "#eef1f5" }},
      yaxis3: {{ domain: [0, 0.2], showgrid: true, gridcolor: "#eef1f5", range: [-20, 120] }},
      shapes: [
        {{ type: "line", xref: "paper", x0: 0, x1: 1, yref: "y3", y0: {j_threshold}, y1: {j_threshold}, line: {{ color: "#ef4444", width: 1, dash: "dot" }} }}
      ]
    }};
    Plotly.newPlot("ashare-chart", traces, layout, {{ responsive: true, displaylogo: false, scrollZoom: true }});
  }})();
  </script>
</section>
"""
        except Exception as exc:
            result_html = f'<div class="error">{html.escape(str(exc))}</div>'

    return f"""
<section class="page-head">
  <div>
    <h1>A股选股器</h1>
    <p class="hint">趋势条件 + J值冰点作为候选硬条件，红长绿短量能用于二次看图确认强弱。首次进入不拉行情，点击按钮后才开始请求数据。</p>
  </div>
  <div class="mode-pill">A Share | Scanner</div>
</section>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<form class="form" action="/cn/scanner" method="get">
  <input type="hidden" name="mode" value="single">
  <label>股票代码<input name="symbol" value="{html.escape(symbol)}" placeholder="600487"></label>
  <label>J值阈值<input type="number" step="0.1" name="j_threshold" value="{j_threshold:g}"></label>
  <button type="submit">单票验证</button>
</form>
<form class="form" id="ashare-scanner-form" action="/cn/scanner" method="get">
  <input type="hidden" name="mode" value="market">
  <label>最低市值（亿元）<input type="number" step="1" name="min_market_cap" value="{min_market_cap:g}"></label>
  <label>最多扫描<input type="number" step="1" name="max_symbols" value="{max_symbols}"></label>
  <label>并发数<input type="number" step="1" name="max_workers" value="{int(number_field(params, "max_workers", 6))}"></label>
  <label>J值阈值<input type="number" step="0.1" name="j_threshold" value="{j_threshold:g}"></label>
  <button type="submit">开始选股</button>
</form>
<section class="progress-box" id="ashare-scan-progress">
  <div class="progress-meta" id="ashare-scan-status">准备开始</div>
  <div class="progress-track"><div class="progress-bar" id="ashare-scan-bar"></div></div>
  <div class="progress-meta" id="ashare-scan-detail"></div>
</section>
<section id="ashare-scan-result"></section>
<script>
(function() {{
  const form = document.getElementById("ashare-scanner-form");
  const progressBox = document.getElementById("ashare-scan-progress");
  const progressBar = document.getElementById("ashare-scan-bar");
  const status = document.getElementById("ashare-scan-status");
  const detail = document.getElementById("ashare-scan-detail");
  const result = document.getElementById("ashare-scan-result");
  let jobId = "";
  function update(job) {{
    const total = Number(job.total || 0);
    const scanned = Number(job.scanned || 0);
    const percent = total > 0 ? Math.round(scanned / total * 100) : (job.status === "running" ? 8 : 0);
    progressBar.style.width = percent + "%";
    const stage = job.stage || "处理中";
    const source = job.data_source ? `｜数据源：${{job.data_source}}` : "";
    const current = job.current ? `｜当前：${{job.current}}` : "";
    const extra = job.detail ? `｜${{job.detail}}` : "";
    status.textContent = `${{stage}}：${{job.message || "正在处理"}}`;
    detail.textContent = `进度 ${{scanned}} / ${{total || "-"}}｜候选 ${{job.candidates || 0}}｜失败 ${{job.errors || 0}}${{current}}${{source}}${{extra}}`;
  }}
  async function poll() {{
    if (!jobId) return;
    const res = await fetch(`/cn/scan/status?job_id=${{encodeURIComponent(jobId)}}`);
    const job = await res.json();
    update(job);
    if (job.result_html) {{
      result.innerHTML = job.result_html;
      if (window.initializeResizableTables) initializeResizableTables(result);
      if (window.initializeSortableTables) initializeSortableTables(result);
    }}
    if (["done", "error", "stopped"].includes(job.status)) {{
      if (job.status === "error") result.innerHTML = `<div class="error">${{job.error || "扫描失败"}}</div>`;
      jobId = "";
      return;
    }}
    setTimeout(poll, 800);
  }}
  form.addEventListener("submit", async event => {{
    event.preventDefault();
    result.innerHTML = "";
    progressBox.classList.add("active");
    progressBar.style.width = "0%";
    status.textContent = "正在启动 A 股扫描";
    detail.textContent = "";
    const params = new URLSearchParams(new FormData(form));
    const res = await fetch(`/cn/scan/start?${{params.toString()}}`);
    const data = await res.json();
    if (data.status === "error") {{
      result.innerHTML = `<div class="error">${{data.error || "无法启动扫描"}}</div>`;
      return;
    }}
    jobId = data.job_id;
    poll();
  }});
}})();
</script>
{result_html}
"""


def render_ashare_watchlist_page(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    items = load_ashare_watchlist_items()
    rows = []
    for item in items:
        symbol = item["symbol"]
        name = item.get("name", "") or "-"
        sector = item.get("sector", "") or "-"
        group = item.get("group", "") or "观察"
        note = item.get("note", "") or "-"
        added_at = item.get("added_at", "") or "-"
        chart_url = f"/cn/scanner?mode=single&symbol={quote(symbol)}&j_threshold=14"
        rows.append(
            "<tr>"
            f"<td><a class=\"symbol-button\" href=\"{chart_url}\" target=\"ashare-watch-chart\">{html.escape(symbol)}</a></td>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(sector)}</td>"
            f"<td>{html.escape(group)}</td>"
            f"<td>{html.escape(note)}</td>"
            f"<td>{html.escape(added_at)}</td>"
            f"<td><a class=\"btn btn-secondary btn-small\" href=\"{chart_url}\" target=\"ashare-watch-chart\">看图</a> "
            f"<a class=\"delete-link\" href=\"/cn/watchlist/delete?symbol={quote(symbol)}\" onclick=\"return confirm('确认删除 {html.escape(symbol)}？');\">删除</a></td>"
            "</tr>"
        )
    table_rows = "\n".join(rows) if rows else '<tr><td colspan="7" class="empty">暂无 A 股自选。可以先添加代码，或从 A 股选股结果加入。</td></tr>'
    default_symbol = items[0]["symbol"] if items else ""
    default_src = f"/cn/scanner?mode=single&symbol={quote(default_symbol)}&j_threshold=14" if default_symbol else "about:blank"
    return f"""
<section class="page-head">
  <div>
    <h1>A股自选池</h1>
    <p class="hint">A 股自选池和美股自选池分开保存。点击代码或“看图”后，右侧显示 A 股策略图表。</p>
  </div>
  <div class="mode-pill">A Share | Watchlist</div>
</section>
<form class="form" action="/cn/watchlist/add" method="get">
  <label>股票代码<input name="symbol" value="{html.escape(field(params, "symbol", ""))}" placeholder="600487"></label>
  <label>分组<input name="group" value="{html.escape(field(params, "group", "观察"))}" placeholder="观察 / 候选 / 持仓"></label>
  <label class="wide">备注<input name="note" value="{html.escape(field(params, "note", ""))}" placeholder="关注原因、板块、阻力位等"></label>
  <button type="submit">添加到自选</button>
</form>
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">自选数量</div><div class="stat-value">{len(items)}</div></div>
  <div class="stat-card"><div class="stat-label">存储位置</div><div class="stat-value">data/ashare</div></div>
  <div class="stat-card"><div class="stat-label">图表</div><div class="stat-value">A股策略</div></div>
  <div class="stat-card"><div class="stat-label">市场</div><div class="stat-value">A股</div></div>
</section>
<section class="watchlist-grid">
  <div class="watchlist-panel">
    <div class="table-wrap watchlist-list-wrap">
      <table class="sortable resizable-table">
        <thead><tr><th>代码</th><th>名称</th><th>板块</th><th>分组</th><th>备注</th><th>加入日期</th><th>操作</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  <div class="watchlist-panel">
    <iframe name="ashare-watch-chart" src="{html.escape(default_src)}" title="A股策略图表" style="width:100%;height:980px;border:1px solid #d6dbe3;border-radius:6px;background:#fff;"></iframe>
  </div>
</section>
"""


def render_backtest_trade_table(trades, equity_curve) -> str:
    rows = []
    for i, trade in enumerate(trades, 1):
        pnl_class = "pos" if trade.pnl >= 0 else "neg"
        pct_class = "pos" if trade.pnl_pct >= 0 else "neg"
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(trade.entry_signal_date)}</td>"
            f"<td>{html.escape(trade.entry_date)}</td>"
            f"<td>{html.escape(trade.exit_signal_date)}</td>"
            f"<td>{html.escape(trade.exit_date)}</td>"
            f"<td>{trade.entry_price:.2f}</td>"
            f"<td>{trade.exit_price:.2f}</td>"
            f"<td>{trade.shares}</td>"
            f"<td>{trade.bars_held}</td>"
            f"<td class=\"{pnl_class}\">{trade.pnl:.2f}</td>"
            f"<td class=\"{pct_class}\">{trade.pnl_pct:.2f}%</td>"
            f"<td>{html.escape(trade.exit_reason)}</td>"
            "</tr>"
        )
    open_position = open_position_snapshot(equity_curve)
    if open_position:
        pnl_class = "pos" if float(open_position["pnl"]) >= 0 else "neg"
        rows.append(
            "<tr>"
            f"<td>{len(rows) + 1}</td>"
            f"<td>{html.escape(str(open_position['entry_signal_date']))}</td>"
            f"<td>{html.escape(str(open_position['entry_date']))}</td>"
            "<td>未触发</td>"
            "<td>未平仓</td>"
            f"<td>{float(open_position['entry_price']):.2f}</td>"
            f"<td>{float(open_position['mark_price']):.2f}</td>"
            f"<td>{int(open_position['shares'])}</td>"
            f"<td>{int(open_position['bars_held'])}</td>"
            f"<td class=\"{pnl_class}\">{float(open_position['pnl']):.2f}</td>"
            f"<td class=\"{pnl_class}\">{float(open_position['pnl_pct']):.2f}%</td>"
            f"<td>未平仓，按 {html.escape(str(open_position['mark_date']))} 收盘价估值</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="12" class="empty">这个区间没有完成交易。</td></tr>')
    return f"""
  <h2>交易明细</h2>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>#</th><th>买入信号日</th><th>买入操作日</th><th>卖出信号日</th><th>卖出操作日</th><th>买入价</th><th>卖出价</th><th>股数</th><th>持仓K线</th><th>收益金额</th><th>收益率</th><th>卖出原因</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
"""


def render_backtest_signal_table(bars, trades, equity_curve) -> str:
    def fmt_pct(value) -> str:
        if value == "" or value is None:
            return "-"
        number = float(value)
        cls = "pos" if number >= 0 else "neg"
        return f'<span class="{cls}">{number:.2f}%</span>'

    def fmt_num(value) -> str:
        if value == "" or value is None:
            return "-"
        return f"{float(value):.2f}"

    def score_badge(row) -> str:
        rating = html.escape(str(row["signal_rating"]))
        score = fmt_num(row["signal_score"])
        return f'<span class="score-badge score-{rating}">{score}</span>'

    def rating_badge(row) -> str:
        rating = html.escape(str(row["signal_rating"]))
        return f'<span class="rating rating-{rating}">{rating}</span>'

    rows = []
    for i, row in enumerate(build_signal_detail_rows(bars, trades, equity_curve), 1):
        rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(str(row['date']))}</td>"
            f"<td>{html.escape(str(row['signal_type']))}</td>"
            f"<td>{html.escape(str(row['status']))}</td>"
            f"<td>{'是' if int(row['executed']) else '否'}</td>"
            f"<td>{score_badge(row)}</td>"
            f"<td>{rating_badge(row)}</td>"
            f"<td>{fmt_num(row['volume_score'])}</td>"
            f"<td>{fmt_num(row['trend_score'])}</td>"
            f"<td>{fmt_num(row['candle_score'])}</td>"
            f"<td>{fmt_num(row['space_score'])}</td>"
            f"<td>{fmt_num(row['risk_score'])}</td>"
            f"<td>{html.escape(str(row['score_notes']))}</td>"
            f"<td>{fmt_num(row['close'])}</td>"
            f"<td>{fmt_num(row['ma'])}</td>"
            f"<td>{fmt_pct(row['dist_ma_pct'])}</td>"
            f"<td>{fmt_num(row['volume_ratio'])}x</td>"
            f"<td>{'是' if int(row['in_position']) else '否'}</td>"
            f"<td>{fmt_pct(row['ret_1d'])}</td>"
            f"<td>{fmt_pct(row['ret_3d'])}</td>"
            f"<td>{fmt_pct(row['ret_5d'])}</td>"
            f"<td>{fmt_pct(row['ret_10d'])}</td>"
            f"<td>{fmt_pct(row['ret_20d'])}</td>"
            f"<td>{fmt_pct(row['max_up_20d'])}</td>"
            f"<td>{fmt_pct(row['max_down_20d'])}</td>"
            f"<td>{html.escape(str(row['next_sell_signal'] or '-'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="26" class="empty">这个区间没有信号。</td></tr>')
    return f"""
  <h2>信号明细</h2>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>#</th><th>信号日</th><th>类型</th><th>状态</th><th>是否交易</th><th>技术分</th><th>评级</th><th>量能</th><th>趋势</th><th>K线</th><th>空间</th><th>风险</th><th>说明</th><th>收盘</th><th>MA</th><th>距MA</th><th>量比</th><th>持仓中</th><th>后1日</th><th>后3日</th><th>后5日</th><th>后10日</th><th>后20日</th><th>20日最大涨幅</th><th>20日最大回撤</th><th>20日内S点</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
"""


def render_signal_rating_summary(bars, trades, equity_curve) -> str:
    signal_rows = [
        row for row in build_signal_detail_rows(bars, trades, equity_curve)
        if row.get("signal_type") == "B"
    ]
    groups: dict[str, list[dict[str, float | int | str]]] = {"Strong": [], "Medium": [], "Weak": []}
    for row in signal_rows:
        groups.setdefault(str(row.get("signal_rating", "Weak")), []).append(row)

    def numeric(row: dict[str, float | int | str], key: str) -> float | None:
        value = row.get(key, "")
        if value == "" or value is None:
            return None
        return float(value)

    def fmt_pct(value: float | None) -> str:
        if value is None:
            return "-"
        cls = "pos" if value >= 0 else "neg"
        return f'<span class="{cls}">{value:.2f}%</span>'

    rows_html = []
    for rating in ("Strong", "Medium", "Weak"):
        rows = groups.get(rating, [])
        if not rows:
            rows_html.append(
                f'<tr><td><span class="rating rating-{rating}">{rating}</span></td><td>0</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>'
            )
            continue
        ret5 = [value for row in rows if (value := numeric(row, "ret_5d")) is not None]
        ret10 = [value for row in rows if (value := numeric(row, "ret_10d")) is not None]
        ret20 = [value for row in rows if (value := numeric(row, "ret_20d")) is not None]
        max_up = [value for row in rows if (value := numeric(row, "max_up_20d")) is not None]
        max_down = [value for row in rows if (value := numeric(row, "max_down_20d")) is not None]

        def avg(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        win5 = sum(1 for value in ret5 if value > 0) / len(ret5) * 100 if ret5 else None
        win10 = sum(1 for value in ret10 if value > 0) / len(ret10) * 100 if ret10 else None
        win20 = sum(1 for value in ret20 if value > 0) / len(ret20) * 100 if ret20 else None
        rows_html.append(
            "<tr>"
            f'<td><span class="rating rating-{rating}">{rating}</span></td>'
            f"<td>{len(rows)}</td>"
            f"<td>{fmt_pct(avg(ret5))}</td>"
            f"<td>{fmt_pct(avg(ret10))}</td>"
            f"<td>{fmt_pct(avg(ret20))}</td>"
            f"<td>{fmt_pct(win5)}</td>"
            f"<td>{fmt_pct(win10)}</td>"
            f"<td>{fmt_pct(win20)}</td>"
            f"<td>{fmt_pct(avg(max_up))} / {fmt_pct(avg(max_down))}</td>"
            "</tr>"
        )

    return f"""
  <h2>技术分后续表现</h2>
  <div class="table-wrap compact-table">
    <table class="resizable-table">
      <thead><tr><th>评级</th><th>B点数</th><th>平均后5日</th><th>平均后10日</th><th>平均后20日</th><th>5日胜率</th><th>10日胜率</th><th>20日胜率</th><th>20日平均最大涨幅 / 回撤</th></tr></thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
  </div>
"""


def render_strategy_compare(summary: dict[str, float | int], buy_hold: dict[str, object], benchmark: dict[str, object]) -> str:
    strategy_return = float(summary["return_pct"])
    strategy_dd = float(summary["max_drawdown_pct"])
    buy_hold_return = float(buy_hold.get("return_pct", 0.0))
    buy_hold_dd = float(buy_hold.get("max_drawdown_pct", 0.0))
    benchmark_return = float(benchmark.get("return_pct", 0.0))
    out_stock = strategy_return - buy_hold_return
    out_benchmark = strategy_return - benchmark_return

    def pct_cell(value: float) -> str:
        cls = "pos" if value >= 0 else "neg"
        return f'<span class="{cls}">{value:.2f}%</span>'

    return f"""
  <h2>策略对比</h2>
  <div class="table-wrap compact-table">
    <table class="resizable-table">
      <thead><tr><th>项目</th><th>收益率</th><th>最大回撤</th><th>相对策略</th></tr></thead>
      <tbody>
        <tr><td>策略</td><td>{pct_cell(strategy_return)}</td><td>{strategy_dd:.2f}%</td><td>-</td></tr>
        <tr><td>{html.escape(str(buy_hold.get("symbol", "Buy & Hold")))} 买入持有</td><td>{pct_cell(buy_hold_return)}</td><td>{buy_hold_dd:.2f}%</td><td>{pct_cell(out_stock)}</td></tr>
        <tr><td>{html.escape(str(benchmark.get("symbol", "Benchmark")))}</td><td>{pct_cell(benchmark_return)}</td><td>-</td><td>{pct_cell(out_benchmark)}</td></tr>
      </tbody>
    </table>
  </div>
"""


def run_strategy(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    symbol = field(params, "symbol", "AAPL").upper()
    strategy_name = "ratchet"
    start = field(params, "start", start_for_preset("1y", date.today()).isoformat())
    end = field(params, "end", date.today().isoformat())
    benchmark_symbol = field(params, "benchmark", DEFAULT_BENCHMARK).upper()
    initial_cash = number_field(params, "initial_cash", 100000)
    ma_length = int(number_field(params, "ma_length", 5))
    vol_length = int(number_field(params, "vol_length", 20))
    validate_backtest_range(start, end, vol_length)

    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
    trades, equity_curve = backtest(
        bars=bars,
        ma_length=ma_length,
        vol_length=vol_length,
        vol_multiplier=number_field(params, "vol_multiplier", 1.45),
        initial_cash=initial_cash,
        commission_pct=number_field(params, "commission_pct", 0.1),
        slippage_pct=number_field(params, "slippage_pct", 0),
        strategy_name=strategy_name,
        stop_5ma_pct=number_field(params, "stop_5ma_pct", 7.5),
        hard_stop_pct=number_field(params, "hard_stop_pct", 20),
        reentry_pct=number_field(params, "reentry_pct", 4.5),
    )
    summary = summarize(trades, equity_curve, initial_cash)
    benchmark = build_benchmark(benchmark_symbol, start, end, initial_cash)
    buy_hold = build_buy_hold(symbol, bars, initial_cash)
    benchmark["buy_hold_symbol"] = symbol
    benchmark["buy_hold_return_pct"] = buy_hold["return_pct"]
    benchmark["buy_hold_curve"] = buy_hold["curve"]

    stem = safe_name(f"{symbol}_{strategy_name}_{start}_{end}_{benchmark_symbol}")
    report_path = REPORT_DIR / f"{stem}_report.html"
    trades_path = REPORT_DIR / f"{stem}_trades.csv"
    equity_path = REPORT_DIR / f"{stem}_equity.csv"
    make_report(report_path, f"{symbol} {strategy_name} backtest {start} to {end}", bars, trades, equity_curve, summary, benchmark=benchmark)
    write_trades(trades_path, trades)
    write_equity(equity_path, equity_curve)

    report_url = f"/reports/{quote(report_path.name)}"
    trades_url = f"/reports/{quote(trades_path.name)}"
    equity_url = f"/reports/{quote(equity_path.name)}"
    return f"""
{render_backtest_form(params)}
<section class="result">
  <p class="links">
    <a href="{report_url}" target="_blank">打开完整图表</a>
    <a href="{trades_url}" target="_blank">交易明细 CSV</a>
    <a href="{equity_url}" target="_blank">权益曲线 CSV</a>
  </p>
  {render_strategy_compare(summary, buy_hold, benchmark)}
  {render_backtest_trade_table(trades, equity_curve)}
  {render_signal_rating_summary(bars, trades, equity_curve)}
  {render_backtest_signal_table(bars, trades, equity_curve)}
  <iframe src="{report_url}" title="Backtest report"></iframe>
</section>
"""


def render_batch_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    today = date.today()
    start_default = start_for_preset("1y", today).isoformat()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    return f"""
<section class="page-head">
  <div>
    <h1>批量回测</h1>
    <p class="hint">用同一组策略参数批量验证多个股票，避免只看单票结果造成过拟合。</p>
  </div>
  <div class="mode-pill">Batch Backtest</div>
</section>
<form class="form" action="/batch/run" method="get">
  <label class="wide">股票代码，逗号或换行分隔
    <textarea name="symbols" placeholder="AAPL,MSFT,NVDA,TSM">{value("symbols", "AAPL,MSFT,NVDA,TSM")}</textarea>
  </label>
  <label>开始日期<input type="date" name="start" value="{value("start", start_default)}"></label>
  <label>结束日期<input type="date" name="end" value="{value("end", today.isoformat())}"></label>
  <label>初始资金<input name="initial_cash" value="{value("initial_cash", "100000")}"></label>
  <label>手续费 %<input name="commission_pct" value="{value("commission_pct", "0.1")}"></label>
  <label>滑点 %<input name="slippage_pct" value="{value("slippage_pct", "0")}"></label>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" value="{value("vol_length", "20")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>跌破均线止损 %<input name="stop_5ma_pct" value="{value("stop_5ma_pct", "7.5")}"></label>
  <label>B点追踪止损 %<input name="hard_stop_pct" value="{value("hard_stop_pct", "20")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <button type="submit">运行批量回测</button>
</form>
"""

def run_batch_backtest(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    symbols = parse_symbols_text(field(params, "symbols", "AAPL,MSFT,NVDA,TSM"))
    start = field(params, "start", start_for_preset("1y", date.today()).isoformat())
    end = field(params, "end", date.today().isoformat())
    initial_cash = number_field(params, "initial_cash", 100000)
    ma_length = int(number_field(params, "ma_length", 5))
    vol_length = int(number_field(params, "vol_length", 20))
    validate_backtest_range(start, end, vol_length)
    rows = []
    errors = []
    for symbol in symbols:
        try:
            bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
            trades, equity_curve = backtest(
                bars=bars,
                ma_length=ma_length,
                vol_length=vol_length,
                vol_multiplier=number_field(params, "vol_multiplier", 1.45),
                initial_cash=initial_cash,
                commission_pct=number_field(params, "commission_pct", 0.1),
                slippage_pct=number_field(params, "slippage_pct", 0),
                strategy_name="ratchet",
                stop_5ma_pct=number_field(params, "stop_5ma_pct", 7.5),
                hard_stop_pct=number_field(params, "hard_stop_pct", 20),
                reentry_pct=number_field(params, "reentry_pct", 4.5),
            )
            rows.append((symbol, summarize(trades, equity_curve, initial_cash)))
        except Exception as exc:
            errors.append((symbol, str(exc)))
    rows.sort(key=lambda item: item[1]["return_pct"], reverse=True)
    body_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(symbol)}</td>"
        f"<td>{summary['return_pct']:.2f}%</td>"
        f"<td>{summary['net_profit']:.2f}</td>"
        f"<td>{summary['max_drawdown_pct']:.2f}%</td>"
        f"<td>{summary['trades']}</td>"
        f"<td>{summary['win_rate_pct']:.2f}%</td>"
        f"<td>{summary['profit_factor']:.2f}</td>"
        f"<td>{summary['avg_trade_drawdown_pct']:.2f}%</td>"
        f"<td>{summary['avg_max_favorable_pct']:.2f}%</td>"
        "</tr>"
        for symbol, summary in rows
    )
    if not body_rows:
        body_rows = '<tr><td colspan="9" class="empty">No successful backtests.</td></tr>'
    return f"""
{render_batch_form(params)}
<section class="result">
  <p class="hint">已回测 {len(symbols)} 个代码，成功 {len(rows)} 个，失败 {len(errors)} 个。区间：{html.escape(start)} 到 {html.escape(end)}。</p>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>Symbol</th><th>Return</th><th>Net Profit</th><th>Max DD</th><th>Trades</th><th>Win Rate</th><th>Profit Factor</th><th>Avg Trade DD</th><th>Avg MFE</th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>
  {render_failure_table(errors)}
</section>
"""


def parse_symbols_text(text: str) -> list[str]:
    raw = text.replace("\n", ",").replace(" ", ",").split(",")
    return unique_symbols(raw)


def money_to_float(value: str) -> float:
    clean = str(value or "").replace("$", "").replace(",", "").strip()
    if clean in ("", "N/A"):
        return 0.0
    try:
        return float(clean)
    except ValueError:
        return 0.0


def normalize_yahoo_symbol(symbol: str) -> str | None:
    symbol = symbol.strip().upper().replace("/", "-")
    if not symbol or not re.fullmatch(r"[A-Z0-9.-]+", symbol):
        return None
    if any(bad in symbol for bad in ("^", "$", " ")):
        return None
    return symbol


def is_etf_like_nasdaq_row(row: dict[str, object]) -> bool:
    text = " ".join(
        str(row.get(key, ""))
        for key in ("symbol", "name", "industry", "sector")
    ).lower()
    return any(
        term in text
        for term in (
            "etf",
            "exchange traded fund",
            "fund",
            "index fund",
            "trust",
            "ishares",
            "spdr",
            "invesco",
            "vanguard",
            "proshares",
            "direxion",
            "wisdomtree",
            "global x",
        )
    )


def is_stock_like_nasdaq_row(row: dict[str, object]) -> bool:
    text = " ".join(
        str(row.get(key, ""))
        for key in ("symbol", "name", "industry", "sector")
    ).lower()
    excluded_terms = (
        "etf",
        "exchange traded fund",
        "fund",
        "trust",
        "preferred",
        "preference",
        "warrant",
        "right",
        "unit",
        "notes",
        "bond",
        "debenture",
        "depositary shares",
        "closed end",
        "closed-end",
        "acquisition corp",
        "acquisition corporation",
        "spac",
    )
    return not any(term in text for term in excluded_terms)


def nasdaq_asset_type(row: dict[str, object]) -> str:
    if is_etf_like_nasdaq_row(row):
        return "ETF"
    return "Stock"


def nasdaq_row_metadata(row: dict[str, object]) -> dict[str, object]:
    return {
        "company_name": str(row.get("name", "") or ""),
        "market_cap": money_to_float(str(row.get("marketCap", ""))),
        "country": str(row.get("country", "") or ""),
        "sector": str(row.get("sector", "") or ""),
        "industry": str(row.get("industry", "") or ""),
        "asset_type": nasdaq_asset_type(row),
    }


def enrich_signal_result(result: SignalResult, metadata: dict[str, object] | None) -> SignalResult:
    if not metadata:
        return result
    for key in ("company_name", "market_cap", "country", "sector", "industry", "asset_type"):
        setattr(result, key, metadata.get(key, getattr(result, key)))
    return result


def yahoo_news_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{quote(symbol)}/news/"


def google_news_url(symbol: str, company_name: str) -> str:
    query = f"{symbol} {company_name} stock news earnings contract catalyst".strip()
    return f"https://www.google.com/search?q={quote(query)}&tbm=nws"


def xueqiu_news_url(symbol: str, company_name: str) -> str:
    return f"https://xueqiu.com/k?q={quote(symbol.upper())}"


def load_earnings_cache() -> dict[str, dict[str, object]]:
    if not EARNINGS_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(EARNINGS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key).upper(): value for key, value in payload.items() if isinstance(value, dict)}


def save_earnings_cache(cache: dict[str, dict[str, object]]) -> None:
    EARNINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EARNINGS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_earnings_date(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if not text or text.lower() in ("nat", "none", "nan"):
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else ""


def fetch_next_earnings_date(symbol: str) -> tuple[str, str]:
    try:
        import yfinance as yf
    except Exception:
        return "", "Unavailable"

    today = date.today()
    ticker = yf.Ticker(symbol)

    try:
        earnings_dates = ticker.get_earnings_dates(limit=12)
        if earnings_dates is not None and not earnings_dates.empty:
            future_dates: list[date] = []
            for index_value in earnings_dates.index:
                normalized = normalize_earnings_date(index_value)
                if not normalized:
                    continue
                parsed = date.fromisoformat(normalized)
                if parsed >= today:
                    future_dates.append(parsed)
            if future_dates:
                return min(future_dates).isoformat(), "Estimated"
    except Exception:
        pass

    try:
        calendar = ticker.calendar
        if hasattr(calendar, "empty") and not calendar.empty:
            if "Earnings Date" in calendar.index:
                values = calendar.loc["Earnings Date"].tolist()
            elif "Earnings Date" in calendar.columns:
                values = calendar["Earnings Date"].tolist()
            else:
                values = []
            dates = []
            for value in values:
                normalized = normalize_earnings_date(value)
                if normalized:
                    parsed = date.fromisoformat(normalized)
                    if parsed >= today:
                        dates.append(parsed)
            if dates:
                return min(dates).isoformat(), "Estimated"
        elif isinstance(calendar, dict):
            values = calendar.get("Earnings Date") or calendar.get("EarningsDate") or []
            if not isinstance(values, (list, tuple)):
                values = [values]
            dates = []
            for value in values:
                normalized = normalize_earnings_date(value)
                if normalized:
                    parsed = date.fromisoformat(normalized)
                    if parsed >= today:
                        dates.append(parsed)
            if dates:
                return min(dates).isoformat(), "Estimated"
    except Exception:
        pass

    return "", "Unknown"


def enrich_earnings_dates(rows: list[SignalResult]) -> None:
    if not rows:
        return
    cache = load_earnings_cache()
    changed = False
    now = time.time()
    today = date.today()
    missing: list[str] = []
    row_by_symbol = {row.symbol.upper(): row for row in rows}
    for row in rows:
        symbol = row.symbol.upper()
        cached = cache.get(symbol)
        if cached and now - float(cached.get("fetched_at", 0)) < EARNINGS_CACHE_SECONDS:
            earnings_date = str(cached.get("date", ""))
            status = str(cached.get("status", "Unknown"))
            row.next_earnings_date = earnings_date
            row.earnings_status = status
            row.earnings_days = (date.fromisoformat(earnings_date) - today).days if earnings_date else 9999
        else:
            missing.append(symbol)

    if missing:
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as executor:
            future_by_symbol = {executor.submit(fetch_next_earnings_date, symbol): symbol for symbol in missing}
            for future in as_completed(future_by_symbol):
                symbol = future_by_symbol[future]
                try:
                    earnings_date, status = future.result()
                except Exception:
                    earnings_date, status = "", "Unknown"
                row = row_by_symbol.get(symbol)
                if row:
                    row.next_earnings_date = earnings_date
                    row.earnings_status = status
                    row.earnings_days = (date.fromisoformat(earnings_date) - today).days if earnings_date else 9999
                cache[symbol] = {"date": earnings_date, "status": status, "fetched_at": now}
                changed = True
    if changed:
        save_earnings_cache(cache)


def format_earnings(row: SignalResult) -> str:
    if not row.next_earnings_date:
        return "未知"
    if row.earnings_days <= 3:
        risk = "3天内"
    elif row.earnings_days <= 7:
        risk = "7天内"
    elif row.earnings_days <= 30:
        risk = "30天内"
    else:
        risk = ""
    suffix = f" / {risk}" if risk else ""
    return f"{row.next_earnings_date} / {row.earnings_days}天 / {row.earnings_status or 'Estimated'}{suffix}"


def earnings_badge(row: SignalResult) -> str:
    if not row.next_earnings_date:
        return '<span class="badge earnings-unknown" title="财报日期暂不可用">Unknown</span>'
    status = html.escape(row.earnings_status or "Estimated")
    title = html.escape(f"{row.next_earnings_date} / {row.earnings_days}天 / {status}")
    if row.earnings_days <= 3:
        cls = "earnings-danger"
        label = f"Earnings {row.earnings_days}D"
    elif row.earnings_days <= 7:
        cls = "earnings-watch"
        label = f"Earnings {row.earnings_days}D"
    elif row.earnings_days <= 30:
        cls = "earnings-safe"
        label = f"After {row.earnings_days}D"
    else:
        cls = "earnings-safe"
        label = f"After {row.earnings_days}D"
    return f'<span class="badge {cls}" title="{title}">{html.escape(label)}</span>'


def format_metric(value: float, suffix: str = "%") -> str:
    if value == 999.0:
        return "N/A"
    return f"{value:.1f}{suffix}"


def space_score(label: str) -> int:
    return {
        "52W high": 5,
        "Near high": 4,
        "Enough room": 3,
        "Nearby resistance": 2,
        "Below 200MA": 1,
    }.get(label, 3)


def candle_score(label: str) -> int:
    return {
        "Strong bullish": 5,
        "Bullish": 3,
        "Rejected": 2,
        "Bearish": 1,
    }.get(label, 3)


def sector_score(label: str) -> int:
    return {
        "Industry cluster": 5,
        "Sector cluster": 4,
        "Some support": 3,
        "Isolated": 2,
    }.get(label, 1)


def update_total_score(row: SignalResult) -> None:
    row.second_stage_score_total = (
        int(row.catalyst_score or 0)
        + int(row.sector_score or 0)
        + int(row.space_score or 0)
        + int(row.candle_score or 0)
    )
    if row.second_stage_score_total >= 15:
        row.second_stage_rating = "Strong"
    elif row.second_stage_score_total >= 9:
        row.second_stage_rating = "Medium"
    else:
        row.second_stage_rating = "Weak"


def add_space_and_candle_quality(result: SignalResult, bars: list[Bar]) -> SignalResult:
    if not bars:
        result.second_stage_rating = "Pending"
        result.catalyst_score = 3
        update_total_score(result)
        return result

    bar_by_date = {bar.date: bar for bar in bars}
    signal_bar = bar_by_date.get(result.signal_date, bars[-1])
    close = signal_bar.close
    high_52w = max(bar.high for bar in bars)
    result.distance_52w_high_pct = (close / high_52w - 1) * 100 if high_52w else 0.0

    ma200_values = rolling_sma([bar.close for bar in bars], 200)
    ma200 = ma200_values[-1] if ma200_values else None
    if ma200:
        result.above_200ma = "Yes" if close > ma200 else "No"
        result.distance_200ma_pct = (close / ma200 - 1) * 100
    else:
        result.above_200ma = "Insufficient data"
        result.distance_200ma_pct = 0.0

    prior_resistances = [
        bar.high for bar in bars[:-1]
        if close < bar.high <= close * 1.10
    ]
    nearest = min(prior_resistances) if prior_resistances else None
    result.nearest_resistance_pct = ((nearest / close - 1) * 100) if nearest else 999.0

    near_52w = result.distance_52w_high_pct >= -5
    if close >= high_52w * 0.995:
        result.space_label = "52W high"
    elif ma200 and close < ma200:
        result.space_label = "Below 200MA"
    elif nearest and result.nearest_resistance_pct <= 10:
        result.space_label = "Nearby resistance"
    elif near_52w:
        result.space_label = "Near high"
    else:
        result.space_label = "Enough room"

    body = abs(signal_bar.close - signal_bar.open)
    upper_shadow = signal_bar.high - max(signal_bar.open, signal_bar.close)
    full_range = signal_bar.high - signal_bar.low
    result.day_change_pct = (signal_bar.close / signal_bar.open - 1) * 100 if signal_bar.open else 0.0
    result.close_position_pct = ((signal_bar.close - signal_bar.low) / full_range * 100) if full_range else 50.0
    result.upper_shadow_body_ratio = upper_shadow / body if body > 0 else 999.0

    if signal_bar.close <= signal_bar.open:
        result.candle_label = "Bearish"
    elif result.close_position_pct >= 80 and result.upper_shadow_body_ratio <= 0.5:
        result.candle_label = "Strong bullish"
    elif result.upper_shadow_body_ratio > 0.5:
        result.candle_label = "Rejected"
    else:
        result.candle_label = "Bullish"

    result.catalyst_label = "Manual review"
    result.catalyst_score = 3
    result.space_score = space_score(result.space_label)
    result.candle_score = candle_score(result.candle_label)
    result.catalyst_yahoo_url = yahoo_news_url(result.symbol)
    result.catalyst_google_url = xueqiu_news_url(result.symbol, result.company_name)
    update_total_score(result)
    return result


def add_sector_and_rating(rows: list[SignalResult]) -> None:
    sector_counts: dict[str, int] = {}
    industry_counts: dict[str, int] = {}
    for row in rows:
        if row.sector:
            sector_counts[row.sector] = sector_counts.get(row.sector, 0) + 1
        if row.industry:
            industry_counts[row.industry] = industry_counts.get(row.industry, 0) + 1

    for row in rows:
        row.sector_peer_count = sector_counts.get(row.sector, 0) if row.sector else 0
        row.industry_peer_count = industry_counts.get(row.industry, 0) if row.industry else 0
        if row.industry_peer_count >= 2:
            row.sector_label = "Industry cluster"
        elif row.sector_peer_count >= 3:
            row.sector_label = "Sector cluster"
        elif row.sector_peer_count >= 2:
            row.sector_label = "Some support"
        else:
            row.sector_label = "Isolated"

        row.catalyst_score = row.catalyst_score or 3
        row.sector_score = sector_score(row.sector_label)
        row.space_score = row.space_score or space_score(row.space_label)
        row.candle_score = row.candle_score or candle_score(row.candle_label)
        update_total_score(row)


def hide_weak_candidates(params: dict[str, list[str]]) -> bool:
    return field(params, "hide_weak", "1" if DEFAULT_HIDE_WEAK_CANDIDATES else "0") == "1"


def visible_candidate_rows(params: dict[str, list[str]], rows: list[SignalResult]) -> list[SignalResult]:
    visible = rows
    if hide_weak_candidates(params):
        visible = [row for row in visible if row.second_stage_rating != "Weak"]
    earnings_filter = field(params, "earnings_filter", "show")
    if earnings_filter == "hide_3d":
        visible = [row for row in visible if not row.next_earnings_date or row.earnings_days > 3]
    elif earnings_filter == "hide_7d":
        visible = [row for row in visible if not row.next_earnings_date or row.earnings_days > 7]
    elif earnings_filter == "hide_unknown":
        visible = [row for row in visible if row.next_earnings_date]
    return visible


def cleanup_stale_latest_scan() -> None:
    if not LATEST_SCAN_PATH.exists():
        return
    try:
        payload = json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
    except Exception:
        LATEST_SCAN_PATH.unlink(missing_ok=True)
        return
    if payload.get("signal_date") != current_signal_date():
        LATEST_SCAN_PATH.unlink(missing_ok=True)


def report_url_to_path(url: str) -> Path | None:
    if not url.startswith("/reports/"):
        return None
    name = Path(unquote(url.removeprefix("/reports/"))).name
    if not name:
        return None
    path = (REPORT_DIR / name).resolve()
    if path.parent != REPORT_DIR.resolve():
        return None
    return path


def delete_latest_scan() -> int:
    deleted = 0
    latest = load_latest_scan()
    if latest:
        for key in ("report", "csv"):
            path = report_url_to_path(str(latest.get(key, "")))
            if path and path.exists():
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
    if LATEST_SCAN_PATH.exists():
        LATEST_SCAN_PATH.unlink(missing_ok=True)
        deleted += 1
    return deleted


def load_latest_scan() -> dict[str, object] | None:
    cleanup_stale_latest_scan()
    if not LATEST_SCAN_PATH.exists():
        return None
    try:
        return json.loads(LATEST_SCAN_PATH.read_text(encoding="utf-8"))
    except Exception:
        LATEST_SCAN_PATH.unlink(missing_ok=True)
        return None


def save_latest_scan(
    params: dict[str, list[str]],
    source: str,
    symbols: list[str],
    rows: list[SignalResult],
    display_rows: list[SignalResult],
    errors: list[tuple[str, str]],
    end: str,
    html_path: Path,
    csv_path: Path,
) -> None:
    if end != current_signal_date():
        return
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    signal_date = date.fromisoformat(end)
    payload = {
        "signal_date": end,
        "planned_trade_date": next_market_weekday(signal_date).isoformat(),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "params": {key: values[0] if values else "" for key, values in params.items()},
        "summary": {
            "scanned": len(symbols),
            "technical_candidates": len(rows),
            "visible_candidates": len(display_rows),
            "strong": sum(1 for row in display_rows if row.second_stage_rating == "Strong"),
            "medium": sum(1 for row in display_rows if row.second_stage_rating == "Medium"),
            "failed": len(errors),
        },
        "report": f"/reports/{html_path.name}",
        "csv": f"/reports/{csv_path.name}",
        "candidates": [row.__dict__ for row in display_rows],
        "errors": [{"symbol": symbol, "reason": reason} for symbol, reason in errors],
    }
    LATEST_SCAN_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def quality_bars_for_symbol(symbol: str, current_bars: list[Bar], end: str) -> list[Bar]:
    if len(current_bars) >= 220:
        return current_bars
    quality_start = (date.fromisoformat(end) - timedelta(days=420)).isoformat()
    return fetch_bars("yfinance", symbol, quality_start, end, "qfq", None)


def scanner_worker_count(params: dict[str, list[str]]) -> int:
    return max(1, min(12, int(number_field(params, "max_workers", 6))))


def scan_symbol_candidate(
    symbol: str,
    start: str,
    end: str,
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
    reentry_pct: float,
    min_price: float,
    min_avg_dollar_volume: float,
    metadata: dict[str, object] | None,
) -> tuple[str, SignalResult | None, str | None]:
    try:
        bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
        result = latest_b_signal(
            symbol,
            bars,
            ma_length,
            vol_length,
            vol_multiplier,
            reentry_pct,
            min_price,
            min_avg_dollar_volume,
        )
        if not result:
            return symbol, None, None

        result = enrich_signal_result(result, metadata)
        try:
            result = add_space_and_candle_quality(result, quality_bars_for_symbol(symbol, bars, end))
        except Exception:
            result.second_stage_rating = "Pending"
            result.catalyst_label = "Manual review"
            result.catalyst_score = 3
            result.space_score = result.space_score or 3
            result.candle_score = result.candle_score or 3
            update_total_score(result)
            result.catalyst_yahoo_url = yahoo_news_url(symbol)
            result.catalyst_google_url = xueqiu_news_url(symbol, result.company_name)
        return symbol, result, None
    except Exception as exc:
        return symbol, None, str(exc)


def fetch_nasdaq_screener_rows() -> list[dict[str, object]]:
    cache_path = NASDAQ_CACHE_PATH if NASDAQ_CACHE_PATH.exists() else LEGACY_NASDAQ_CACHE_PATH
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < NASDAQ_CACHE_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

    request = Request(
        "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        if NASDAQ_CACHE_PATH.exists():
            return json.loads(NASDAQ_CACHE_PATH.read_text(encoding="utf-8"))
        if LEGACY_NASDAQ_CACHE_PATH.exists():
            return json.loads(LEGACY_NASDAQ_CACHE_PATH.read_text(encoding="utf-8"))
        raise RuntimeError(f"无法拉取 Nasdaq 股票池，且本地没有可用缓存：{exc}") from exc
    rows = payload.get("data", {}).get("rows", [])
    NASDAQ_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NASDAQ_CACHE_PATH.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def build_auto_universe(
    min_market_cap: float,
    max_market_cap: float,
    min_price: float,
    min_volume: float,
    max_symbols: int,
    asset_type: str,
) -> list[str]:
    symbols, _ = build_auto_universe_with_metadata(
        min_market_cap=min_market_cap,
        max_market_cap=max_market_cap,
        min_price=min_price,
        min_volume=min_volume,
        max_symbols=max_symbols,
        asset_type=asset_type,
    )
    return symbols


def build_auto_universe_with_metadata(
    min_market_cap: float,
    max_market_cap: float,
    min_price: float,
    min_volume: float,
    max_symbols: int,
    asset_type: str,
) -> tuple[list[str], dict[str, dict[str, object]]]:
    rows = fetch_nasdaq_screener_rows()
    candidates: list[tuple[str, float, dict[str, object]]] = []
    for row in rows:
        if asset_type == "stocks" and not is_stock_like_nasdaq_row(row):
            continue
        if asset_type == "etf" and not is_etf_like_nasdaq_row(row):
            continue
        symbol = normalize_yahoo_symbol(str(row.get("symbol", "")))
        if not symbol:
            continue
        market_cap = money_to_float(str(row.get("marketCap", "")))
        price = money_to_float(str(row.get("lastsale", "")))
        volume = money_to_float(str(row.get("volume", "")))
        if market_cap < min_market_cap:
            continue
        if max_market_cap > 0 and market_cap > max_market_cap:
            continue
        if price < min_price or volume < min_volume:
            continue
        candidates.append((symbol, market_cap, nasdaq_row_metadata(row)))

    candidates.sort(key=lambda item: item[1], reverse=True)
    symbols = unique_symbols([symbol for symbol, _, _ in candidates[:max_symbols]])
    metadata_by_symbol = {symbol: metadata for symbol, _, metadata in candidates[:max_symbols]}
    return symbols, metadata_by_symbol


def render_scanner_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    cleanup_stale_latest_scan()
    scan_end = default_scan_end_date()
    latest_scan = load_latest_scan()
    latest_html = ""
    if latest_scan:
        summary = latest_scan.get("summary", {})
        latest_html = f"""
<section class="result latest-scan-card">
  <div class="toolbar">
    <div>
      <strong>当前信号日期已有扫描结果</strong>
      <div class="scan-facts">
        <span class="scan-fact"><span>信号日</span>{html.escape(str(latest_scan.get("signal_date", "")))}</span>
        <span class="scan-fact"><span>买入日</span>{html.escape(str(latest_scan.get("planned_trade_date", "")))}</span>
        <span class="scan-fact"><span>候选</span>{summary.get("visible_candidates", 0)}</span>
        <span class="scan-fact"><span>Strong</span>{summary.get("strong", 0)}</span>
        <span class="scan-fact"><span>Medium</span>{summary.get("medium", 0)}</span>
        <span class="scan-fact"><span>扫描时间</span>{html.escape(str(latest_scan.get("created_at", "")))}</span>
      </div>
    </div>
    <div class="inline-actions links">
      <a href="/scan/latest">查看当前结果</a>
      <a href="{html.escape(str(latest_scan.get("csv", "#")))}" target="_blank">下载 CSV</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
</section>
"""

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    source = field(params, "universe_source", "auto")

    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""

    asset_type = field(params, "asset_type", "stocks")
    hide_weak_checked = " checked" if field(params, "hide_weak", "1" if DEFAULT_HIDE_WEAK_CANDIDATES else "0") == "1" else ""
    earnings_filter = field(params, "earnings_filter", "show")
    default_symbols = "ASTS,NVDA,TSLA,AAPL,MSFT,QQQ"
    return f"""
<section class="page-head">
  <div>
    <h1>下一交易日 B 点选股器</h1>
    <p class="hint">扫描最后一根已完成日 K 是否出现 B 信号。符合条件的股票按策略在下一交易日开盘执行；不使用盘后或夜盘价格。</p>
  </div>
  <div class="mode-pill">盘后复盘 | Daily Close</div>
</section>
{latest_html}
{render_market_environment_bar()}
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">模式</div><div class="stat-value">盘后复盘</div></div>
  <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{scan_end.isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{next_market_weekday(scan_end).isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">默认过滤</div><div class="stat-value">{DEFAULT_MIN_MARKET_CAP_100M_USD} 亿美元+</div></div>
</section>
<form class="form" id="scanner-form" action="/scan" method="get" data-async-submit="true">
  <label>股票池来源
    <select name="universe_source">
      <option value="auto"{selected(source, "auto")}>按市值自动筛选美股</option>
      <option value="manual"{selected(source, "manual")}>手动输入股票池</option>
    </select>
  </label>
  <label>最低市值，亿美元<input name="min_market_cap_billion" value="{value("min_market_cap_billion", str(DEFAULT_MIN_MARKET_CAP_100M_USD))}"></label>
  <label>最高市值，亿美元<input name="max_market_cap_billion" value="{value("max_market_cap_billion", "0")}"></label>
  <label>最低当日成交量<input name="min_screener_volume" value="{value("min_screener_volume", "500000")}"></label>
  <label>最多扫描数量<input name="max_symbols" value="{value("max_symbols", str(DEFAULT_MAX_SCAN_SYMBOLS))}"></label>
  <label>并发数<input name="max_workers" value="{value("max_workers", "6")}"></label>
  <label>资产类型
    <select name="asset_type">
      <option value="stocks"{selected(asset_type, "stocks")}>只扫 Stocks</option>
      <option value="etf"{selected(asset_type, "etf")}>只扫 ETF</option>
      <option value="all"{selected(asset_type, "all")}>Stocks + ETF</option>
    </select>
  </label>
  <label class="wide">手动股票池，逗号或换行分隔
    <textarea name="symbols" placeholder="ASTS,NVDA,TSLA">{value("symbols", default_symbols)}</textarea>
  </label>
  <label>开始日期<input type="date" name="start" value="{value("start", default_scan_start_date(scan_end).isoformat())}"></label>
  <label>结束日期<input type="date" name="end" value="{value("end", scan_end.isoformat())}"></label>
  <label>最低价格<input name="min_price" value="{value("min_price", "5")}"></label>
  <label>20日最低成交额<input name="min_avg_dollar_volume" value="{value("min_avg_dollar_volume", "20000000")}"></label>
  <label class="checkbox-label"><input type="checkbox" name="hide_weak" value="1"{hide_weak_checked}> 隐藏 Weak 候选</label>
  <label>财报风险
    <select name="earnings_filter">
      <option value="show"{selected(earnings_filter, "show")}>显示全部</option>
      <option value="hide_3d"{selected(earnings_filter, "hide_3d")}>隐藏3天内财报</option>
      <option value="hide_7d"{selected(earnings_filter, "hide_7d")}>隐藏7天内财报</option>
      <option value="hide_unknown"{selected(earnings_filter, "hide_unknown")}>隐藏未知财报</option>
    </select>
  </label>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" value="{value("vol_length", "20")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <button type="submit">开始选股</button>
</form>
<section class="progress-box" id="scan-progress">
  <div class="progress-meta" id="scan-status">准备开始</div>
  <div class="progress-track"><div class="progress-bar" id="scan-bar"></div></div>
  <div class="progress-meta" id="scan-detail"></div>
  <div class="progress-actions">
    <button type="button" class="secondary" id="pause-scan" hidden>暂停</button>
    <button type="button" class="success" id="resume-scan" hidden>继续</button>
    <button type="button" class="danger" id="stop-scan" hidden>终止</button>
  </div>
</section>
<section id="scan-result"></section>
<script>
const scannerForm = document.getElementById("scanner-form");
const progressBox = document.getElementById("scan-progress");
const progressBar = document.getElementById("scan-bar");
const scanStatus = document.getElementById("scan-status");
const scanDetail = document.getElementById("scan-detail");
const scanResult = document.getElementById("scan-result");
const pauseScan = document.getElementById("pause-scan");
const resumeScan = document.getElementById("resume-scan");
const stopScan = document.getElementById("stop-scan");
let activeScanJobId = "";
let lastResultHtml = "";

function updateProgress(job) {{
  const total = Number(job.total || 0);
  const scanned = Number(job.scanned || 0);
  const percent = total > 0 ? Math.round(scanned / total * 100) : 0;
  progressBar.style.width = percent + "%";
  scanStatus.textContent = job.message || "扫描中";
  scanDetail.textContent = `已扫描 ${{scanned}} / ${{total}}，候选 ${{job.candidates || 0}}，失败 ${{job.errors || 0}}，当前 ${{job.current || "-"}}`;
  pauseScan.hidden = !activeScanJobId || !["running", "pausing"].includes(job.status);
  resumeScan.hidden = !activeScanJobId || job.status !== "paused";
  stopScan.hidden = !activeScanJobId || !["running", "pausing", "paused", "stopping"].includes(job.status);
}}

function initializeResizableTables(root = document) {{
  root.querySelectorAll("table.resizable-table").forEach(table => {{
    if (table.dataset.resizableReady) return;
    table.dataset.resizableReady = "1";
    table.querySelectorAll("th").forEach((th, index) => {{
      th.classList.add("resizable");
      const handle = document.createElement("span");
      handle.className = "col-resizer";
      th.appendChild(handle);
      handle.addEventListener("mousedown", event => {{
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = th.offsetWidth;
        const cells = table.querySelectorAll(`tr > *:nth-child(${{index + 1}})`);
        function onMove(moveEvent) {{
          const width = Math.max(56, startWidth + moveEvent.clientX - startX);
          cells.forEach(cell => {{
            cell.style.width = width + "px";
            cell.style.minWidth = width + "px";
            cell.style.maxWidth = width + "px";
          }});
        }}
        function onUp() {{
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
        }}
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      }});
    }});
  }});
}}

function initializeSortableTables(root = document) {{
  root.querySelectorAll("table.resizable-table").forEach(table => {{
    if (table.dataset.sortableReady) return;
    table.dataset.sortableReady = "1";
    table.querySelectorAll("th").forEach((th, index) => {{
      th.style.cursor = "pointer";
      th.addEventListener("click", event => {{
        if (event.target.classList.contains("col-resizer")) return;
        const tbody = table.querySelector("tbody");
        const rows = Array.from(tbody.querySelectorAll("tr"));
        if (rows.length <= 1 || rows[0].querySelector(".empty")) return;
        const direction = th.dataset.sortDirection === "asc" ? "desc" : "asc";
        table.querySelectorAll("th").forEach(header => delete header.dataset.sortDirection);
        th.dataset.sortDirection = direction;
        rows.sort((a, b) => {{
          const av = a.children[index]?.innerText.trim() || "";
          const bv = b.children[index]?.innerText.trim() || "";
          const an = Number(av.replace(/[^0-9.-]/g, ""));
          const bn = Number(bv.replace(/[^0-9.-]/g, ""));
          const numeric = Number.isFinite(an) && Number.isFinite(bn) && (av.match(/[0-9]/) || bv.match(/[0-9]/));
          const result = numeric ? an - bn : av.localeCompare(bv);
          return direction === "asc" ? result : -result;
        }});
        rows.forEach(row => tbody.appendChild(row));
      }});
    }});
  }});
}}

async function pollScan(jobId) {{
  while (true) {{
    const res = await fetch(`/scan/status?id=${{encodeURIComponent(jobId)}}`);
    const job = await res.json();
    updateProgress(job);
    if (job.result_html && job.result_html !== lastResultHtml) {{
      lastResultHtml = job.result_html;
      scanResult.innerHTML = job.result_html;
      initializeResizableTables(scanResult);
      initializeSortableTables(scanResult);
    }}
    if (job.status === "done" || job.status === "stopped") {{
      progressBar.style.width = "100%";
      pauseScan.hidden = true;
      resumeScan.hidden = true;
      stopScan.hidden = true;
      break;
    }}
    if (job.status === "error") {{
      pauseScan.hidden = true;
      resumeScan.hidden = true;
      stopScan.hidden = true;
      scanResult.innerHTML = `<div class="error">${{job.error || "扫描失败"}}</div>`;
      break;
    }}
    await new Promise(resolve => setTimeout(resolve, 1000));
  }}
}}

pauseScan.addEventListener("click", async () => {{
  if (!activeScanJobId) return;
  pauseScan.hidden = true;
  scanStatus.textContent = "正在暂停，当前股票处理完后会显示当前结果...";
  await fetch(`/scan/pause?id=${{encodeURIComponent(activeScanJobId)}}`);
}});

resumeScan.addEventListener("click", async () => {{
  if (!activeScanJobId) return;
  resumeScan.hidden = true;
  scanStatus.textContent = "正在继续扫描...";
  await fetch(`/scan/resume?id=${{encodeURIComponent(activeScanJobId)}}`);
}});

stopScan.addEventListener("click", async () => {{
  if (!activeScanJobId) return;
  pauseScan.hidden = true;
  resumeScan.hidden = true;
  stopScan.hidden = true;
  scanStatus.textContent = "正在终止，当前股票处理完后会保留当前结果...";
  await fetch(`/scan/stop?id=${{encodeURIComponent(activeScanJobId)}}`);
}});

document.addEventListener("click", async event => {{
  const addButton = event.target.closest("[data-add-watchlist]");
  if (addButton) {{
    event.preventDefault();
    const symbol = addButton.dataset.addWatchlist;
    const note = addButton.dataset.watchNote || "";
    addButton.disabled = true;
    addButton.textContent = "添加中";
    const params = new URLSearchParams({{ symbol, group: "候选", note }});
    const res = await fetch(`/watchlist/add.json?${{params.toString()}}`);
    const data = await res.json();
    if (data.ok) {{
      addButton.classList.add("added");
      addButton.textContent = "已加入";
      window.showToast?.(`${{symbol}} 已加入自选池`, "success");
    }} else {{
      addButton.disabled = false;
      addButton.textContent = data.error || "失败";
      window.showToast?.(data.error || `${{symbol}} 加入失败`, "error");
    }}
    return;
  }}
  const button = event.target.closest("[data-candidate-symbol]");
  if (!button) return;
  event.preventDefault();
  const symbol = button.dataset.candidateSymbol;
  const host = button.closest(".result") || scanResult || document.body;
  let detail = host.querySelector("#candidate-detail");
  if (!detail) {{
    detail = document.createElement("section");
    detail.id = "candidate-detail";
    detail.className = "candidate-detail";
    host.appendChild(detail);
  }}
  detail.innerHTML = `<section class="result"><div class="loading-overlay active" style="position:relative; min-height:140px;"><div class="spinner"></div><div>正在生成 ${{symbol}} 的日 K 线和策略交易点</div></div></section>`;
  const params = new URLSearchParams(new FormData(scannerForm));
  params.set("symbol", symbol);
  const res = await fetch(`/candidate?${{params.toString()}}`);
  const html = await res.text();
  detail.innerHTML = html;
  detail.scrollIntoView({{ behavior: "smooth", block: "start" }});
}});

scannerForm.addEventListener("submit", async event => {{
  event.preventDefault();
  const formData = new FormData(scannerForm);
  const scanStart = new Date(formData.get("start"));
  const scanEnd = new Date(formData.get("end"));
  const scanDays = (scanEnd - scanStart) / 86400000;
  if (!Number.isFinite(scanDays) || scanDays < 30) {{
    const message = "选股区间至少需要 1 个月，请扩大开始日期和结束日期。";
    scanResult.innerHTML = `<div class="error">${{message}}</div>`;
    window.showToast?.(message, "error");
    return;
  }}
  const submitButton = scannerForm.querySelector("button[type='submit']");
  if (submitButton) {{
    submitButton.disabled = true;
    submitButton.classList.add("btn-loading");
    submitButton.dataset.originalText = submitButton.dataset.originalText || submitButton.textContent || "";
    submitButton.textContent = "准备中";
  }}
  progressBox.style.display = "block";
  scanResult.innerHTML = "";
  lastResultHtml = "";
  progressBar.style.width = "0%";
  scanStatus.textContent = "正在准备股票池";
  scanDetail.textContent = "";
  pauseScan.hidden = true;
  resumeScan.hidden = true;
  stopScan.hidden = true;
  activeScanJobId = "";
  const params = new URLSearchParams(formData);
  let data = {{}};
  try {{
    const res = await fetch(`/scan/start?${{params.toString()}}`);
    data = await res.json();
  }} catch (error) {{
    data = {{ error: error?.message || "无法启动扫描" }};
  }}
  if (submitButton) {{
    submitButton.disabled = false;
    submitButton.classList.remove("btn-loading");
    submitButton.textContent = submitButton.dataset.originalText || "开始选股";
  }}
  if (!data.job_id) {{
    scanResult.innerHTML = `<div class="error">${{data.error || "无法启动扫描"}}</div>`;
    window.showToast?.(data.error || "无法启动扫描", "error");
    return;
  }}
  activeScanJobId = data.job_id;
  pollScan(data.job_id);
}});
initializeResizableTables(document);
initializeSortableTables(document);
</script>
"""

def render_candidate_table(rows: list[SignalResult]) -> str:
    def catalyst_secondary_url(row: SignalResult) -> str:
        stored = row.catalyst_google_url or ""
        if stored and "google." not in stored:
            return stored
        return xueqiu_news_url(row.symbol, row.company_name)

    def technical_badge(row: SignalResult) -> str:
        rating = html.escape(row.technical_rating or "Pending")
        score = row.technical_score or 0.0
        return f'<span class="score-badge score-{rating}" title="{html.escape(row.technical_notes or "")}">{score:.1f}</span>'

    def candidate_summary(row: SignalResult) -> str:
        pieces = [
            f"Tech {row.technical_score:.0f}",
            f"量比 {row.volume_ratio:.2f}x",
            f"距5MA {row.dist_to_ma_pct:.1f}%",
        ]
        if row.distance_52w_high_pct:
            pieces.append(f"距52W高 {row.distance_52w_high_pct:.1f}%")
        if row.second_stage_rating:
            pieces.append(row.second_stage_rating)
        if row.earnings_days != 9999:
            pieces.append(f"财报 {row.earnings_days}天")
        return " / ".join(pieces)

    table_rows = "\n".join(
        f'<tr><td><button type="button" class="symbol-button" data-candidate-symbol="{html.escape(r.symbol)}">{html.escape(r.symbol)}</button></td><td>{html.escape(r.company_name or "-")}</td>'
        f"<td>{r.market_cap / 1_000_000_000:.2f}</td>"
        f"<td>{html.escape(candidate_summary(r))}</td>"
        f"<td>{technical_badge(r)}</td>"
        f"<td>{r.second_stage_score_total}/20</td>"
        f'<td><span class="rating rating-{html.escape(r.second_stage_rating or "Pending")}">{html.escape(r.second_stage_rating or "Pending")}</span></td>'
        f'<td><button type="button" class="mini-action" data-add-watchlist="{html.escape(r.symbol)}" data-watch-note="{html.escape((r.second_stage_rating or "") + " " + (r.catalyst_label or ""))}">加入自选</button></td>'
        f"<td>{earnings_badge(r)}</td>"
        f'<td>{r.catalyst_score}/5 {html.escape(r.catalyst_label or "Manual review")} <a href="{html.escape(r.catalyst_yahoo_url or yahoo_news_url(r.symbol))}" target="_blank">Yahoo</a> <a href="{html.escape(catalyst_secondary_url(r))}" target="_blank">雪球</a></td>'
        f"<td>{r.sector_score}/5 {html.escape(r.sector_label or '-')} ({r.sector_peer_count}/{r.industry_peer_count})</td>"
        f"<td>{r.space_score}/5 {html.escape(r.space_label or '-')} / 52W {r.distance_52w_high_pct:.1f}% / 200MA {html.escape(r.above_200ma or '-')}</td>"
        f"<td>{r.candle_score}/5 {html.escape(r.candle_label or '-')} / close pos {r.close_position_pct:.0f}% / upper shadow {format_metric(r.upper_shadow_body_ratio, 'x')}</td>"
        f"<td>{html.escape(r.sector or '-')}</td><td>{html.escape(r.industry or '-')}</td>"
        f"<td>{html.escape(r.signal_date)}</td><td>{html.escape(r.signal_type)}</td>"
        f"<td>{r.close:.2f}</td><td>{r.ma:.2f}</td><td>{r.dist_to_ma_pct:.2f}%</td>"
        f"<td>{r.volume_ratio:.2f}x</td><td>{r.massive_count_7d}</td><td>{r.avg_dollar_volume_20d / 1_000_000:.1f}M</td></tr>"
        for r in rows
    )
    if not table_rows:
        table_rows = '<tr><td colspan="23" class="empty">No visible candidates.</td></tr>'
    return f"""
<div class="table-wrap">
<table class="resizable-table">
  <thead><tr><th>Symbol</th><th>Company</th><th>Mkt Cap $B</th><th>Summary</th><th>Tech</th><th>Total</th><th>Rating</th><th>Watch</th><th>Next Earnings</th><th>Catalyst</th><th>Sector Score</th><th>Space</th><th>Candle</th><th>Sector</th><th>Industry</th><th>Signal Date</th><th>Signal</th><th>Close</th><th>MA</th><th>Dist</th><th>Vol Ratio</th><th>Massive 7D</th><th>20D $Vol</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
</div>
"""


def render_failure_table(failures: list[tuple[str, str]]) -> str:
    if not failures:
        return ""
    table_rows = "\n".join(
        f"<tr><td>{html.escape(symbol)}</td><td>{html.escape(reason)}</td></tr>"
        for symbol, reason in failures
    )
    return f"""
<h2>失败原因</h2>
<div class="table-wrap">
<table class="resizable-table">
  <thead><tr><th>Symbol</th><th>Reason</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
</div>
"""

def render_scan_summary(
    source: str,
    symbols_count: int,
    technical_count: int,
    visible_count: int,
    errors_count: int,
    end: str,
) -> str:
    signal_date = date.fromisoformat(end)
    plan_date = next_market_weekday(signal_date)
    return f"""
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{html.escape(end)}</div></div>
  <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{plan_date.isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">扫描 / 技术候选</div><div class="stat-value">{symbols_count} / {technical_count}</div></div>
  <div class="stat-card"><div class="stat-label">显示 / 失败</div><div class="stat-value">{visible_count} / {errors_count}</div></div>
</section>
<p class="hint">股票池：{html.escape(source)}。这里只使用已完成的日 K 线；信号日期出现 B 点，代表策略可在下一交易日开盘执行。</p>
"""


def latest_scan_to_html() -> str:
    latest = load_latest_scan()
    if not latest:
        return f"""
{render_scanner_form({})}
<section class="result">
  <p class="hint">当前信号日期还没有保存的扫描结果。</p>
</section>
"""
    candidates = [SignalResult(**row) for row in latest.get("candidates", [])]
    errors = [(item.get("symbol", ""), item.get("reason", "")) for item in latest.get("errors", [])]
    summary = latest.get("summary", {})
    source = str(latest.get("source", "saved"))
    end = str(latest.get("signal_date", current_signal_date()))
    report = html.escape(str(latest.get("report", "#")))
    csv_url = html.escape(str(latest.get("csv", "#")))
    saved_params = {
        str(key): [str(value)]
        for key, value in (latest.get("params", {}) or {}).items()
    }
    return f"""
{render_scanner_form(saved_params)}
<section class="result">
  <div class="toolbar">
    <div class="inline-actions links">
      <a href="{report}" target="_blank">打开扫描报告</a>
      <a href="{csv_url}" target="_blank">下载 CSV</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
  {render_scan_summary(source, int(summary.get("scanned", 0)), int(summary.get("technical_candidates", 0)), int(summary.get("visible_candidates", len(candidates))), int(summary.get("failed", len(errors))), end)}
  {render_candidate_table(candidates)}
  {render_failure_table(errors)}
</section>
<script>
if (window.initializeResizableTables) initializeResizableTables(document);
if (window.initializeSortableTables) initializeSortableTables(document);
</script>
"""

def run_scanner(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    source = field(params, "universe_source", "auto")
    symbols_text = field(params, "symbols", "")
    scan_end = default_scan_end_date()
    start = field(params, "start", default_scan_start_date(scan_end).isoformat())
    end = field(params, "end", scan_end.isoformat())
    validate_scan_range(start, end)
    ma_length = int(number_field(params, "ma_length", 5))
    vol_length = int(number_field(params, "vol_length", 20))
    vol_multiplier = number_field(params, "vol_multiplier", 1.45)
    reentry_pct = number_field(params, "reentry_pct", 4.5)
    min_price = number_field(params, "min_price", 5)
    min_avg_dollar_volume = number_field(params, "min_avg_dollar_volume", 20_000_000)
    if source == "auto":
        min_market_cap = number_field(
            params,
            "min_market_cap_billion",
            number_field(params, "min_market_cap", DEFAULT_MIN_MARKET_CAP_100M_USD * 100_000_000) / 100_000_000,
        ) * 100_000_000
        max_market_cap_billion = number_field(
            params,
            "max_market_cap_billion",
            number_field(params, "max_market_cap", 0) / 100_000_000,
        )
        max_market_cap = max_market_cap_billion * 100_000_000 if max_market_cap_billion > 0 else 0
        symbols, metadata_by_symbol = build_auto_universe_with_metadata(
            min_market_cap=min_market_cap,
            max_market_cap=max_market_cap,
            min_price=min_price,
            min_volume=number_field(params, "min_screener_volume", 500_000),
            max_symbols=int(number_field(params, "max_symbols", DEFAULT_MAX_SCAN_SYMBOLS)),
            asset_type=field(params, "asset_type", "stocks"),
        )
    else:
        symbols = parse_symbols_text(symbols_text) if symbols_text else load_symbols(None)
        metadata_by_symbol = {}

    rows: list[SignalResult] = []
    errors: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
            result = latest_b_signal(symbol, bars, ma_length, vol_length, vol_multiplier, reentry_pct, min_price, min_avg_dollar_volume)
            if result:
                result = enrich_signal_result(result, metadata_by_symbol.get(symbol))
                try:
                    result = add_space_and_candle_quality(result, quality_bars_for_symbol(symbol, bars, end))
                except Exception:
                    result.second_stage_rating = "Pending"
                    result.catalyst_label = "Manual review"
                    result.catalyst_yahoo_url = yahoo_news_url(symbol)
                    result.catalyst_google_url = xueqiu_news_url(symbol, result.company_name)
                rows.append(result)
        except Exception as exc:
            errors.append((symbol, str(exc)))

    add_sector_and_rating(rows)
    rows.sort(key=lambda row: (row.second_stage_score_total, row.avg_dollar_volume_20d), reverse=True)
    enrich_earnings_dates(rows)
    display_rows = visible_candidate_rows(params, rows)
    stem = safe_name(f"next_b_{end}_{len(symbols)}")
    csv_path = REPORT_DIR / f"{stem}.csv"
    html_path = REPORT_DIR / f"{stem}.html"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SignalResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in display_rows:
            writer.writerow(row.__dict__)
    write_html(html_path, display_rows, end)
    save_latest_scan(params, source, symbols, rows, display_rows, errors, end, html_path, csv_path)

    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {message[:80]}" for symbol, message in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 个代码扫描失败：{html.escape(sample)}</p>'

    return f"""
{render_scanner_form(params)}
<section class="result">
  <div class="toolbar">
    <div class="inline-actions links">
      <a href="/reports/{quote(html_path.name)}" target="_blank">打开扫描报告</a>
      <a href="/reports/{quote(csv_path.name)}" target="_blank">下载 CSV</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
  {render_scan_summary(source, len(symbols), len(rows), len(display_rows), len(errors), end)}
  {error_note}
  {render_candidate_table(display_rows)}
  {render_failure_table(errors)}
</section>
"""


def render_candidate_detail(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        raise ValueError("Missing symbol")

    scan_end = default_scan_end_date()
    start = field(params, "start", default_scan_start_date(scan_end).isoformat())
    end = field(params, "end", scan_end.isoformat())
    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
    signal_result = latest_b_signal(
        symbol,
        bars,
        int(number_field(params, "ma_length", 5)),
        int(number_field(params, "vol_length", 20)),
        number_field(params, "vol_multiplier", 1.45),
        number_field(params, "reentry_pct", 4.5),
        number_field(params, "min_price", 5),
        number_field(params, "min_avg_dollar_volume", 20_000_000),
    )
    detail_panel = ""
    if signal_result:
        signal_result = add_space_and_candle_quality(signal_result, quality_bars_for_symbol(symbol, bars, end))
        add_sector_and_rating([signal_result])
        plan_date = next_market_weekday(date.fromisoformat(signal_result.signal_date)).isoformat()
        detail_panel = f"""
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{html.escape(signal_result.signal_date)}</div></div>
    <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{plan_date}</div></div>
    <div class="stat-card"><div class="stat-label">总分 / 评级</div><div class="stat-value">{signal_result.second_stage_score_total}/20 {html.escape(signal_result.second_stage_rating)}</div></div>
    <div class="stat-card"><div class="stat-label">20日均成交额</div><div class="stat-value">{signal_result.avg_dollar_volume_20d / 1_000_000:.1f}M</div></div>
  </section>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Catalyst</th><th>Sector</th><th>Space</th><th>Candle</th><th>52W Distance</th><th>200MA</th><th>News</th></tr></thead>
      <tbody><tr>
        <td>{signal_result.catalyst_score}/5 {html.escape(signal_result.catalyst_label)}</td>
        <td>{signal_result.sector_score}/5 {html.escape(signal_result.sector_label)}</td>
        <td>{signal_result.space_score}/5 {html.escape(signal_result.space_label)}</td>
        <td>{signal_result.candle_score}/5 {html.escape(signal_result.candle_label)}</td>
        <td>{signal_result.distance_52w_high_pct:.1f}%</td>
        <td>{html.escape(signal_result.above_200ma)}</td>
        <td><a href="{html.escape(yahoo_news_url(symbol))}" target="_blank">Yahoo</a> <a href="{html.escape(xueqiu_news_url(symbol, signal_result.company_name))}" target="_blank">雪球</a></td>
      </tr></tbody>
    </table>
  </div>
"""
    trades, equity_curve = backtest(
        bars=bars,
        ma_length=int(number_field(params, "ma_length", 5)),
        vol_length=int(number_field(params, "vol_length", 20)),
        vol_multiplier=number_field(params, "vol_multiplier", 1.45),
        initial_cash=100000,
        commission_pct=0.1,
        slippage_pct=0,
        strategy_name="ratchet",
        stop_5ma_pct=7.5,
        hard_stop_pct=20,
        reentry_pct=number_field(params, "reentry_pct", 4.5),
    )
    summary = summarize(trades, equity_curve, 100000)

    stem = safe_name(f"candidate_{symbol}_{start}_{end}_{int(time.time())}")
    report_path = REPORT_DIR / f"{stem}.html"
    make_report(
        report_path,
        f"{symbol} daily chart and strategy points {start} to {end}",
        bars,
        trades,
        equity_curve,
        summary,
        benchmark=None,
    )
    report_url = f"/reports/{quote(report_path.name)}"
    return f"""
<section class="result">
  <p class="links">
    <strong>{html.escape(symbol)}</strong>
    <a href="{report_url}" target="_blank">打开完整图表</a>
  </p>
  <p class="hint">下方图表使用当前选股器参数重新回测该候选股，买入和卖出按信号后下一个交易日开盘成交。</p>
  {detail_panel}
  <iframe src="{report_url}" title="{html.escape(symbol)} candidate detail"></iframe>
</section>
"""


def watchlist_metadata_by_symbol(symbols: list[str]) -> dict[str, dict[str, object]]:
    if not symbols:
        return {}
    wanted = {symbol.upper() for symbol in symbols}
    metadata: dict[str, dict[str, object]] = {}
    try:
        for row in fetch_nasdaq_screener_rows():
            symbol = normalize_yahoo_symbol(str(row.get("symbol", "")))
            if symbol and symbol.upper() in wanted:
                metadata[symbol.upper()] = nasdaq_row_metadata(row)
                if len(metadata) == len(wanted):
                    break
    except Exception:
        pass
    return metadata


def watchlist_row(item: dict[str, str], metadata: dict[str, object] | None) -> str:
    symbol = item["symbol"]
    group = item.get("group", "观察")
    note = item.get("note", "")
    company = str((metadata or {}).get("company_name", "") or "-")
    sector = str((metadata or {}).get("sector", "") or "-")
    industry = str((metadata or {}).get("industry", "") or "-")
    market_cap = float((metadata or {}).get("market_cap", 0) or 0)
    latest_close = "-"
    change_pct = "-"
    dist_ma = "-"
    vol_ratio = "-"
    b_status = "-"
    ma_status = "-"
    s_status = "-"
    earnings = "未知"
    try:
        end = default_scan_end_date()
        start = end - timedelta(days=90)
        bars = fetch_bars("yfinance", symbol, start.isoformat(), end.isoformat(), "qfq", None)
        if bars:
            latest_close = f"{bars[-1].close:.2f}"
            if len(bars) >= 2 and bars[-2].close:
                change_pct = f"{(bars[-1].close / bars[-2].close - 1) * 100:.2f}%"
            ma = rolling_sma([bar.close for bar in bars], 5)
            vol_ma = rolling_sma([bar.volume for bar in bars], 20)
            if ma[-1]:
                dist_ma = f"{(bars[-1].close / ma[-1] - 1) * 100:.2f}%"
                ma_status = "5MA上方" if bars[-1].close > ma[-1] else "5MA下方"
            if vol_ma[-1]:
                vol_ratio = f"{bars[-1].volume / vol_ma[-1]:.2f}x"
            result = latest_b_signal(symbol, bars, 5, 20, 1.45, 4.5, 0, 0)
            b_status = "B点" if result else "-"
            buy_signal, _, _, _ = build_ratchet_inputs(bars, 5, 20, 1.45, 4.5 / 100)
            recent_b = next((bars[idx].date for idx in range(len(buy_signal) - 1, -1, -1) if buy_signal[idx]), "")
            if recent_b and not result:
                b_status = f"最近B {recent_b}"
            if ma[-1] and bars[-1].close < ma[-1] * (1 - 7.5 / 100):
                s_status = "跌破防守"
            elif ma[-1] and len(bars) >= 2 and ma[-2] and bars[-2].close >= ma[-2] and bars[-1].close < ma[-1]:
                s_status = "跌破5MA"
            else:
                s_status = "未触发"
            temp = SignalResult(
                symbol=symbol,
                signal_date=bars[-1].date,
                close=bars[-1].close,
                ma=ma[-1] or 0,
                dist_to_ma_pct=0,
                volume=bars[-1].volume,
                vol_ma=vol_ma[-1] or 0,
                volume_ratio=bars[-1].volume / vol_ma[-1] if vol_ma[-1] else 0,
                massive_count_7d=0,
                signal_type="",
                avg_dollar_volume_20d=0,
                company_name=company if company != "-" else "",
            )
            enrich_earnings_dates([temp])
            earnings = format_earnings(temp)
    except Exception:
        pass
    cap_text = f"{market_cap / 1_000_000_000:.2f}" if market_cap else "-"
    cache_bars = read_price_cache(symbol)
    cache_text = f"数据至 {max((bar.date for bar in cache_bars), default='-')}" if cache_bars else "待拉取"
    form_id = f"watch-edit-{html.escape(symbol)}"
    tags = []
    if b_status != "-":
        tags.append("B")
    if s_status != "未触发" and s_status != "-":
        tags.append("Risk")
    if "3天内" in earnings or "7天内" in earnings:
        tags.append("Earnings")
    tag_text = " ".join(tags) if tags else "-"
    detail = {
        "symbol": symbol,
        "company": company,
        "sector": sector,
        "industry": industry,
        "marketCap": cap_text,
        "close": latest_close,
        "change": change_pct,
        "maStatus": ma_status,
        "distMa": dist_ma,
        "volRatio": vol_ratio,
        "sStatus": s_status,
        "earnings": earnings,
        "bStatus": b_status,
        "group": group,
        "note": note,
        "cache": cache_text,
        "addedAt": item.get("added_at", "-"),
    }
    data_attrs = " ".join(
        f'data-{key}="{html.escape(str(value), quote=True)}"'
        for key, value in detail.items()
    )
    return (
        "<tr>"
        '<td class="watch-row-cell">'
        '<div class="watch-row-head">'
        f'<button type="button" class="watch-row-button" data-watch-symbol="{html.escape(symbol)}" {data_attrs}><span class="watch-symbol-line"><span>{html.escape(symbol)}</span><span>{html.escape(change_pct)}</span></span><span class="watch-meta-line">{html.escape(company)} · {html.escape(group)}</span></button>'
        f'<span class="watch-row-actions"><a class="delete-link" href="/watchlist/delete?symbol={quote(symbol)}" onclick="return confirm(\'确认从自选池删除 {html.escape(symbol)}？\');">删除</a></span>'
        "</div>"
        "</td>"
        f"<td>{latest_close}</td>"
        f"<td>{html.escape(tag_text)}</td>"
        "</tr>"
    )


def render_watchlist_page(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    items = load_watchlist_items()
    symbols = [item["symbol"] for item in items]
    metadata = watchlist_metadata_by_symbol(symbols)
    rows = "\n".join(watchlist_row(item, metadata.get(item["symbol"].upper())) for item in items)
    if not rows:
        rows = '<tr><td colspan="3" class="empty">暂无自选股。先在上方添加股票代码。</td></tr>'
    default_symbol = symbols[0] if symbols else ""
    cache = price_cache_summary(symbols)
    return f"""
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<section class="page-head">
  <div>
    <h1>自选池</h1>
    <p class="hint">维护你关注的股票。点击 Symbol 后在右侧查看可缩放、可拖动的日 K 图；图表数据通过 JSON 返回，不生成报告文件。</p>
  </div>
  <div class="mode-pill">Watchlist | Daily</div>
</section>
<form class="form" action="/watchlist/add" method="get">
  <label>添加股票代码<input name="symbol" value="{html.escape(field(params, "symbol", ""))}" placeholder="NVDA"></label>
  <label>分组<input name="group" value="{html.escape(field(params, "group", "观察"))}" placeholder="AI / 半导体 / 观察"></label>
  <label class="wide">备注<input name="note" value="{html.escape(field(params, "note", ""))}" placeholder="关注原因、财报催化、阻力位等"></label>
  <button type="submit">添加到自选</button>
</form>
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">自选数量</div><div class="stat-value">{len(symbols)}</div></div>
  <div class="stat-card"><div class="stat-label">行情可加速</div><div class="stat-value">{cache["cached_symbols"]}/{len(symbols)}</div></div>
  <div class="stat-card"><div class="stat-label">数据最新至</div><div class="stat-value">{html.escape(str(cache["latest"]))}</div></div>
  <div class="stat-card"><div class="stat-label">缓存容量</div><div class="stat-value">{float(cache["size_mb"]):.1f} MB</div></div>
</section>
<p class="hint">缓存用于减少重复拉取行情：打开图表或扫描时会自动补最新日 K；每只股票最多保留约 {cache["max_bars"]} 根，避免长期膨胀。</p>
<section class="watchlist-grid">
  <div class="watchlist-panel">
    <div class="table-wrap watchlist-list-wrap">
      <table id="watchlist-table">
        <thead><tr><th>自选</th><th>Close</th><th>Tags</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
  <div class="watchlist-panel">
    <div class="toolbar">
      <div>
        <strong id="watch-chart-title">{html.escape(default_symbol) if default_symbol else "选择一个股票"}</strong>
        <p class="hint" id="watch-chart-subtitle">周期可切换，数据为已完成日 K。</p>
      </div>
    </div>
    <div class="watch-detail-grid" id="watch-detail-grid">
      <div class="watch-detail-item"><span>Company</span><strong id="watch-detail-company">-</strong></div>
      <div class="watch-detail-item"><span>Market Cap</span><strong id="watch-detail-market">-</strong></div>
      <div class="watch-detail-item"><span>Industry</span><strong id="watch-detail-industry">-</strong></div>
      <div class="watch-detail-item"><span>加入日期</span><strong id="watch-detail-added">-</strong></div>
      <div class="watch-detail-item"><span>技术状态</span><strong id="watch-detail-tech">-</strong></div>
      <div class="watch-detail-item"><span>B / S</span><strong id="watch-detail-signal">-</strong></div>
      <div class="watch-detail-item"><span>财报</span><strong id="watch-detail-earnings">-</strong></div>
      <div class="watch-detail-item"><span>备注</span><strong id="watch-detail-note">-</strong></div>
    </div>
    <div class="period-tabs" id="watch-periods">
      <button type="button" data-preset="1m">1M</button>
      <button type="button" data-preset="3m">3M</button>
      <button type="button" data-preset="6m">6M</button>
      <button type="button" data-preset="1y" class="active">1Y</button>
      <button type="button" data-preset="3y">3Y</button>
      <button type="button" data-preset="5y">5Y</button>
    </div>
    <div class="watchlist-chart-shell">
      <div id="watchlist-chart" class="watchlist-chart"></div>
      <div id="watchlist-tooltip" class="chart-tooltip"></div>
      <div id="watch-chart-loading" class="loading-overlay"><div class="spinner"></div><div>正在拉取日 K 数据</div></div>
    </div>
  </div>
</section>
<script>
const watchInitialSymbol = {json.dumps(default_symbol)};
let watchCurrentSymbol = watchInitialSymbol;
let watchCurrentPreset = "1y";
let watchChart = null;
let watchSeries = {{}};
const watchChartEl = document.getElementById("watchlist-chart");
const watchTooltip = document.getElementById("watchlist-tooltip");
const watchTitle = document.getElementById("watch-chart-title");
const watchSubtitle = document.getElementById("watch-chart-subtitle");
const watchLoading = document.getElementById("watch-chart-loading");
function attr(node, name, fallback = "-") {{
  return node?.getAttribute(name) || fallback;
}}
function updateWatchDetail(button) {{
  if (!button) return;
  document.getElementById("watch-detail-company").textContent = attr(button, "data-company");
  document.getElementById("watch-detail-market").textContent = attr(button, "data-marketCap");
  document.getElementById("watch-detail-industry").textContent = `${{attr(button, "data-sector")}} / ${{attr(button, "data-industry")}}`;
  document.getElementById("watch-detail-added").textContent = attr(button, "data-addedAt");
  document.getElementById("watch-detail-tech").textContent = `${{attr(button, "data-maStatus")}} / 距5MA ${{attr(button, "data-distMa")}} / 量比 ${{attr(button, "data-volRatio")}}`;
  document.getElementById("watch-detail-signal").textContent = `${{attr(button, "data-bStatus")}} / ${{attr(button, "data-sStatus")}}`;
  document.getElementById("watch-detail-earnings").textContent = attr(button, "data-earnings");
  document.getElementById("watch-detail-note").textContent = attr(button, "data-note");
}}

function destroyWatchChart() {{
  if (watchChart) {{
    watchChart.remove();
    watchChart = null;
    watchSeries = {{}};
  }}
}}

function makeWatchChart(payload) {{
  destroyWatchChart();
  watchChart = LightweightCharts.createChart(watchChartEl, {{
    layout: {{ background: {{ type: "solid", color: "#ffffff" }}, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif" }},
    width: watchChartEl.clientWidth,
    height: watchChartEl.clientHeight,
    rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.08, bottom: 0.28 }} }},
    timeScale: {{ borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }},
    grid: {{ vertLines: {{ color: "#f1f3f6" }}, horzLines: {{ color: "#f1f3f6" }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
    handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
  }});
  const candle = watchChart.addCandlestickSeries({{
    upColor: "#089981", downColor: "#f23645", borderUpColor: "#089981", borderDownColor: "#f23645", wickUpColor: "#089981", wickDownColor: "#f23645", priceLineVisible: false,
  }});
  candle.setData(payload.ohlc);
  candle.priceScale().applyOptions({{ scaleMargins: {{ top: 0.08, bottom: 0.28 }} }});
  const ma = watchChart.addLineSeries({{ color: "#f5a623", lineWidth: 2, title: "5MA", priceLineVisible: false }});
  ma.setData(payload.ma);
  const stop = watchChart.addLineSeries({{ color: "#f97316", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "动态止损", priceLineVisible: false }});
  stop.setData(payload.dynamicStop);
  const volume = watchChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
  volume.setData(payload.volume);
  watchChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
  const volMa = watchChart.addLineSeries({{ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false, lastValueVisible: false }});
  volMa.setData(payload.volMa);
  candle.setMarkers(payload.markers || []);
  const rowByTime = new Map(payload.rows.map(row => [row.time, row]));
  watchChart.subscribeCrosshairMove(param => {{
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0 || param.point.x > watchChartEl.clientWidth || param.point.y > watchChartEl.clientHeight) {{ watchTooltip.style.display = "none"; return; }}
    const row = rowByTime.get(param.time);
    if (!row) {{ watchTooltip.style.display = "none"; return; }}
    const up = row.close >= row.open;
    const f = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
    const fv = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
    watchTooltip.innerHTML = `<strong>${{row.time}}</strong><div><span class="${{up ? "up" : "down"}}">开 ${{f(row.open)}} 高 ${{f(row.high)}} 低 ${{f(row.low)}} 收 ${{f(row.close)}}</span></div><div>成交量 ${{fv(row.volume)}} &nbsp; 5MA ${{f(row.ma)}} &nbsp; 动态止损 ${{f(row.dynamicStop)}}</div>`;
    watchTooltip.style.display = "block";
    watchTooltip.style.left = Math.min(param.point.x + 16, watchChartEl.clientWidth - 250) + "px";
    watchTooltip.style.top = Math.max(44, param.point.y - 72) + "px";
  }});
  new ResizeObserver(entries => {{
    if (!watchChart) return;
    const rect = entries[0].contentRect;
    watchChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(watchChartEl);
  watchChart.timeScale().fitContent();
}}

async function loadWatchChart(symbol, preset = watchCurrentPreset) {{
  if (!symbol) return;
  watchCurrentSymbol = symbol;
  watchCurrentPreset = preset;
  watchTitle.textContent = symbol;
  watchSubtitle.textContent = "正在加载...";
  watchLoading?.classList.add("active");
  try {{
    const res = await fetch(`/watchlist/chart?symbol=${{encodeURIComponent(symbol)}}&preset=${{encodeURIComponent(preset)}}`);
    const payload = await res.json();
    if (payload.error) {{
      watchSubtitle.textContent = payload.error;
      destroyWatchChart();
      window.showToast?.(payload.error, "error");
      return;
    }}
    watchSubtitle.textContent = `${{payload.start}} 到 ${{payload.end}}，日 K`;
    makeWatchChart(payload);
  }} catch (error) {{
    const message = error?.message || "图表加载失败";
    watchSubtitle.textContent = message;
    destroyWatchChart();
    window.showToast?.(message, "error");
  }} finally {{
    watchLoading?.classList.remove("active");
  }}
}}

document.addEventListener("click", event => {{
  const button = event.target.closest("[data-watch-symbol]");
  if (!button) return;
  event.preventDefault();
  updateWatchDetail(button);
  loadWatchChart(button.dataset.watchSymbol, watchCurrentPreset);
}});
document.getElementById("watch-periods").addEventListener("click", event => {{
  const button = event.target.closest("[data-preset]");
  if (!button) return;
  document.querySelectorAll("#watch-periods button").forEach(item => item.classList.remove("active"));
  button.classList.add("active");
  loadWatchChart(watchCurrentSymbol, button.dataset.preset);
}});
if (window.initializeResizableTables) initializeResizableTables(document);
if (window.initializeSortableTables) initializeSortableTables(document);
const initialWatchButton = document.querySelector("[data-watch-symbol]");
if (initialWatchButton) updateWatchDetail(initialWatchButton);
if (watchInitialSymbol) loadWatchChart(watchInitialSymbol, "1y");
</script>
"""


def watchlist_chart_payload(params: dict[str, list[str]]) -> dict[str, object]:
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        return {"error": "缺少股票代码。"}
    preset = field(params, "preset", "1y").lower()
    end_day = default_scan_end_date()
    start_day = chart_start_for_preset(preset, end_day)
    try:
        bars = fetch_bars("yfinance", symbol, start_day.isoformat(), end_day.isoformat(), "qfq", None)
    except Exception as exc:
        return {"error": str(exc)}
    if not bars:
        return {"error": f"{symbol} 没有可用日线数据。"}
    try:
        trades, equity_curve = backtest(
            bars=bars,
            ma_length=5,
            vol_length=20,
            vol_multiplier=1.45,
            initial_cash=100000,
            commission_pct=0.1,
            slippage_pct=0,
            strategy_name="ratchet",
            stop_5ma_pct=7.5,
            hard_stop_pct=20,
            reentry_pct=4.5,
        )
    except Exception as exc:
        return {"error": str(exc)}
    markers = [
        {"time": trade.entry_date, "position": "belowBar", "color": "#089981", "shape": "arrowUp", "text": "买入"}
        for trade in trades
    ] + [
        {"time": trade.exit_date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": "卖出"}
        for trade in trades
    ]
    trade_entry_dates = {trade.entry_date for trade in trades}
    trade_exit_dates = {trade.exit_date for trade in trades}
    rows = []
    ma_points = []
    vol_ma_points = []
    dynamic_points = []
    volume_points = []
    for i, bar in enumerate(bars):
        row = equity_curve[i]
        ma = None if row["ma"] == "" else float(row["ma"])
        vol_ma = None if row["vol_ma"] == "" else float(row["vol_ma"])
        dynamic_stop = None if row.get("dynamic_stop", "") in ("", None) else float(row["dynamic_stop"])
        rows.append(
            {
                "time": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "ma": ma,
                "volMa": vol_ma,
                "dynamicStop": dynamic_stop,
            }
        )
        if ma is not None:
            ma_points.append({"time": bar.date, "value": ma})
        if vol_ma is not None:
            vol_ma_points.append({"time": bar.date, "value": vol_ma})
        if dynamic_stop is not None:
            dynamic_points.append({"time": bar.date, "value": dynamic_stop})
        volume_points.append({"time": bar.date, "value": bar.volume, "color": "rgba(8,153,129,0.42)" if bar.close >= bar.open else "rgba(242,54,69,0.42)"})
        position = float(row.get("position_shares", 0) or 0)
        if position > 0 and int(row.get("buy_signal", 0)) and bar.date not in trade_entry_dates:
            markers.append({"time": bar.date, "position": "belowBar", "color": "#84cc16", "shape": "circle", "text": "B"})
        if position > 0 and int(row.get("sell_signal", 0)) and bar.date not in trade_exit_dates:
            markers.append({"time": bar.date, "position": "aboveBar", "color": "#f97316", "shape": "circle", "text": "S"})
    return {
        "symbol": symbol,
        "preset": preset,
        "start": bars[0].date,
        "end": bars[-1].date,
        "ohlc": [{"time": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": volume_points,
        "ma": ma_points,
        "volMa": vol_ma_points,
        "dynamicStop": dynamic_points,
        "markers": sorted(markers, key=lambda item: str(item["time"])),
        "rows": rows,
    }


def resolve_scan_symbols(params: dict[str, list[str]]) -> tuple[str, list[str], dict[str, dict[str, object]]]:
    source = field(params, "universe_source", "auto")
    symbols_text = field(params, "symbols", "")
    min_price = number_field(params, "min_price", 5)
    if source == "auto":
        min_market_cap = number_field(
            params,
            "min_market_cap_billion",
            number_field(params, "min_market_cap", DEFAULT_MIN_MARKET_CAP_100M_USD * 100_000_000) / 100_000_000,
        ) * 100_000_000
        max_market_cap_billion = number_field(
            params,
            "max_market_cap_billion",
            number_field(params, "max_market_cap", 0) / 100_000_000,
        )
        max_market_cap = max_market_cap_billion * 100_000_000 if max_market_cap_billion > 0 else 0
        symbols, metadata_by_symbol = build_auto_universe_with_metadata(
            min_market_cap=min_market_cap,
            max_market_cap=max_market_cap,
            min_price=min_price,
            min_volume=number_field(params, "min_screener_volume", 500_000),
            max_symbols=int(number_field(params, "max_symbols", DEFAULT_MAX_SCAN_SYMBOLS)),
            asset_type=field(params, "asset_type", "stocks"),
        )
    else:
        symbols = parse_symbols_text(symbols_text) if symbols_text else load_symbols(None)
        metadata_by_symbol = {}
    return source, symbols, metadata_by_symbol


def finish_scan_result(
    params: dict[str, list[str]],
    source: str,
    symbols: list[str],
    rows: list[SignalResult],
    errors: list[tuple[str, str]],
) -> str:
    end = field(params, "end", default_scan_end_date().isoformat())
    add_sector_and_rating(rows)
    rows.sort(key=lambda row: (row.second_stage_score_total, row.avg_dollar_volume_20d), reverse=True)
    enrich_earnings_dates(rows)
    display_rows = visible_candidate_rows(params, rows)
    stem = safe_name(f"next_b_{end}_{len(symbols)}_{int(time.time())}")
    csv_path = REPORT_DIR / f"{stem}.csv"
    html_path = REPORT_DIR / f"{stem}.html"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SignalResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in display_rows:
            writer.writerow(row.__dict__)
    write_html(html_path, display_rows, end)
    save_latest_scan(params, source, symbols, rows, display_rows, errors, end, html_path, csv_path)

    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {message[:80]}" for symbol, message in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 个代码扫描失败：{html.escape(sample)}</p>'

    return f"""
<section class="result">
  <div class="toolbar">
    <div class="inline-actions links">
      <a href="/reports/{quote(html_path.name)}" target="_blank">打开扫描报告</a>
      <a href="/reports/{quote(csv_path.name)}" target="_blank">下载 CSV</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
  {render_scan_summary(source, len(symbols), len(rows), len(display_rows), len(errors), end)}
  {error_note}
  {render_candidate_table(display_rows)}
  {render_failure_table(errors)}
</section>
"""


def execute_scan_job(job_id: str, params: dict[str, list[str]]) -> None:
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        cleanup_old_reports()
        set_job(job_id, status="running", message="正在准备股票池", total=0, scanned=0, candidates=0, errors=0, current="")
        source, symbols, metadata_by_symbol = resolve_scan_symbols(params)
        scan_end = default_scan_end_date()
        start = field(params, "start", default_scan_start_date(scan_end).isoformat())
        end = field(params, "end", scan_end.isoformat())
        validate_scan_range(start, end)
        ma_length = int(number_field(params, "ma_length", 5))
        vol_length = int(number_field(params, "vol_length", 20))
        vol_multiplier = number_field(params, "vol_multiplier", 1.45)
        reentry_pct = number_field(params, "reentry_pct", 4.5)
        min_price = number_field(params, "min_price", 5)
        min_avg_dollar_volume = number_field(params, "min_avg_dollar_volume", 20_000_000)

        rows: list[SignalResult] = []
        errors: list[tuple[str, str]] = []
        set_job(job_id, message="正在扫描 B 点信号", total=len(symbols), symbols_count=len(symbols))
        max_workers = scanner_worker_count(params)
        next_index = 0
        scanned = 0
        pending = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while pending or next_index < len(symbols):
                if job_stop_requested(job_id):
                    for future in pending:
                        future.cancel()
                    result_html = finish_scan_result(params, source, symbols, rows, errors)
                    set_job(
                        job_id,
                        status="stopped",
                        message="已终止，保留当前结果",
                        current="",
                        scanned=scanned,
                        candidates=len(rows),
                        errors=len(errors),
                        result_html=result_html,
                    )
                    return

                if job_pause_requested(job_id) and not pending:
                    partial_html = finish_scan_result(params, source, symbols, rows, errors)
                    set_job(
                        job_id,
                        status="paused",
                        message="已暂停，可查看当前结果，点击继续后接着扫描",
                        current="",
                        scanned=scanned,
                        candidates=len(rows),
                        errors=len(errors),
                        result_html=partial_html,
                    )
                    while job_pause_requested(job_id) and not job_stop_requested(job_id):
                        time.sleep(0.5)
                    if job_stop_requested(job_id):
                        continue
                    set_job(job_id, status="running", message="继续扫描 B 点信号", result_html="")

                while (
                    next_index < len(symbols)
                    and len(pending) < max_workers
                    and not job_pause_requested(job_id)
                    and not job_stop_requested(job_id)
                ):
                    symbol = symbols[next_index]
                    future = executor.submit(
                        scan_symbol_candidate,
                        symbol,
                        start,
                        end,
                        ma_length,
                        vol_length,
                        vol_multiplier,
                        reentry_pct,
                        min_price,
                        min_avg_dollar_volume,
                        metadata_by_symbol.get(symbol),
                    )
                    pending[future] = symbol
                    next_index += 1

                if not pending:
                    time.sleep(0.2)
                    continue

                running_symbols = ",".join(list(pending.values())[:3])
                if len(pending) > 3:
                    running_symbols += f"...(+{len(pending) - 3})"
                set_job(job_id, current=running_symbols, scanned=scanned, candidates=len(rows), errors=len(errors))
                done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    symbol = pending.pop(future)
                    try:
                        _, result, error = future.result()
                    except Exception as exc:
                        result = None
                        error = str(exc)
                    scanned += 1
                    if result:
                        rows.append(result)
                        add_sector_and_rating(rows)
                    if error:
                        errors.append((symbol, error))
                set_job(job_id, scanned=scanned, candidates=len(rows), errors=len(errors))

        result_html = finish_scan_result(params, source, symbols, rows, errors)
        set_job(
            job_id,
            status="done",
            message="扫描完成",
            current="",
            scanned=len(symbols),
            candidates=len(rows),
            errors=len(errors),
            result_html=result_html,
        )
    except Exception as exc:
        set_job(job_id, status="error", message="扫描失败", error=str(exc))


def execute_ashare_scan_job(job_id: str, params: dict[str, list[str]]) -> None:
    try:
        set_job(
            job_id,
            status="running",
            stage="准备股票池",
            message="正在拉取 A 股股票池",
            detail="优先使用东方财富总市值行情，失败后自动切换备用数据源。",
            data_source="",
            total=0,
            scanned=0,
            candidates=0,
            errors=0,
            current="",
        )
        min_market_cap = number_field(params, "min_market_cap", 50.0)
        max_symbols = int(number_field(params, "max_symbols", 300))
        max_workers = max(1, min(12, int(number_field(params, "max_workers", 6))))
        j_threshold = number_field(params, "j_threshold", 14.0)

        def universe_progress(message: str) -> None:
            set_job(job_id, stage="拉取股票池", message=message, detail=f"最低市值 {min_market_cap:g} 亿元，最多扫描 {max_symbols} 只。")

        universe, universe_source, market_cap_filter_applied = load_ashare_universe_for_scan(min_market_cap, max_symbols, universe_progress)
        filter_text = "市值过滤已生效" if market_cap_filter_applied else "当前数据源没有总市值字段，市值过滤未生效"
        set_job(
            job_id,
            stage="股票池完成",
            message=f"A 股股票池准备完成：{len(universe)} 只",
            detail=filter_text,
            data_source=universe_source,
            total=len(universe),
            scanned=0,
            candidates=0,
            errors=0,
            current="",
        )

        candidates: list[AShareSignalSnapshot] = []
        errors: list[tuple[str, str]] = []
        scanned = 0
        set_job(job_id, stage="扫描日线", message="正在扫描 A 股日线信号", total=len(universe), scanned=0, candidates=0, errors=0)

        def run_one(item) -> AShareSignalSnapshot:
            snapshot = latest_ashare_signal(item.symbol, j_threshold, fetch_name_value=False)
            snapshot.name = item.name
            snapshot.sector = item.sector
            return snapshot

        with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(universe)))) as executor:
            future_map = {executor.submit(run_one, item): item for item in universe}
            for future in as_completed(future_map):
                item = future_map[future]
                set_job(job_id, stage="扫描日线", message="正在扫描 A 股日线信号", current=f"{item.symbol} {item.name}")
                try:
                    snapshot = future.result()
                    if snapshot.signal:
                        candidates.append(snapshot)
                except Exception as exc:
                    errors.append((f"{item.symbol} {item.name}", str(exc)))
                scanned += 1
                set_job(job_id, scanned=scanned, candidates=len(candidates), errors=len(errors))

        rating_order = {"Strong": 0, "Medium": 1, "Watch": 2, "None": 3}
        candidates.sort(key=lambda row: (rating_order.get(row.candidate_rating, 9), -row.volume_score, row.j_value))
        result_html = render_ashare_scan_result(candidates, errors, scanned, universe_source, market_cap_filter_applied)
        set_job(
            job_id,
            status="done",
            stage="完成",
            message="A 股扫描完成",
            detail=f"数据源：{universe_source}",
            data_source=universe_source,
            current="",
            scanned=scanned,
            candidates=len(candidates),
            errors=len(errors),
            result_html=result_html,
        )
    except Exception as exc:
        set_job(job_id, status="error", stage="失败", message="A 股扫描失败", detail="请查看错误信息；通常是外部数据源返回异常或网络超时。", error=str(exc))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        raw_path = parsed.path
        market = "us"
        route_path = raw_path
        if raw_path == ASHARE_ROUTE:
            market = "cn"
            route_path = "/scanner"
        elif raw_path == "/us" or raw_path.startswith("/us/"):
            market = "us"
            route_path = raw_path[3:] or "/"
        elif raw_path == "/cn" or raw_path.startswith("/cn/"):
            market = "cn"
            route_path = raw_path[3:] or "/"
        try:
            if market == "cn":
                if route_path in ("/", ""):
                    self.send_bytes(page_shell(render_market_placeholder("home"), "home", "cn"))
                elif route_path == "/scanner":
                    self.send_bytes(page_shell(render_ashare_scanner(params), "scanner", "cn"))
                elif route_path == "/scan/start":
                    self.start_ashare_scan_job(params)
                elif route_path == "/scan/status":
                    self.ashare_scan_job_status(params)
                elif route_path == "/watchlist":
                    self.send_bytes(page_shell(render_ashare_watchlist_page(params), "watchlist", "cn"))
                elif route_path == "/watchlist/add":
                    self.add_ashare_watchlist_item(params)
                elif route_path == "/watchlist/delete":
                    self.delete_ashare_watchlist_item(params)
                elif route_path == "/backtest":
                    self.send_bytes(page_shell(render_market_placeholder("backtest"), "backtest", "cn"))
                elif route_path == "/batch":
                    self.send_bytes(page_shell(render_market_placeholder("batch"), "batch", "cn"))
                else:
                    self.send_error(404)
            elif route_path == "/":
                self.send_bytes(page_shell(render_dashboard(), "home", "us"))
            elif route_path == "/backtest":
                self.send_bytes(page_shell(render_backtest_form(params), "backtest", "us"))
            elif route_path == "/run":
                self.send_bytes(page_shell(run_strategy(params), "backtest", "us"))
            elif route_path == "/batch":
                self.send_bytes(page_shell(render_batch_form(params), "batch", "us"))
            elif route_path == "/batch/run":
                self.send_bytes(page_shell(run_batch_backtest(params), "batch", "us"))
            elif route_path == "/watchlist":
                self.send_bytes(page_shell(render_watchlist_page(params), "watchlist", "us"))
            elif route_path == "/watchlist/add":
                self.add_watchlist_item(params)
            elif route_path == "/watchlist/add.json":
                self.add_watchlist_item_json(params)
            elif route_path == "/watchlist/update":
                self.update_watchlist_item(params)
            elif route_path == "/watchlist/delete":
                self.delete_watchlist_item(params)
            elif route_path == "/watchlist/chart":
                self.send_json(watchlist_chart_payload(params))
            elif route_path == "/scanner":
                self.send_bytes(page_shell(render_scanner_form(params), "scanner", "us"))
            elif route_path == "/scan":
                self.send_bytes(page_shell(run_scanner(params), "scanner", "us"))
            elif route_path == "/scan/latest":
                self.send_bytes(page_shell(latest_scan_to_html(), "scanner", "us"))
            elif route_path == "/scan/delete":
                self.delete_scan_result()
            elif route_path == "/scan/start":
                self.start_scan_job(params)
            elif route_path == "/scan/pause":
                self.pause_scan_job(params)
            elif route_path == "/scan/resume":
                self.resume_scan_job(params)
            elif route_path == "/scan/stop":
                self.stop_scan_job(params)
            elif route_path == "/scan/status":
                self.scan_job_status(params)
            elif route_path == "/candidate":
                self.send_bytes(render_candidate_detail(params).encode("utf-8"))
            elif route_path.startswith("/reports/"):
                self.send_report(route_path)
            else:
                self.send_error(404)
        except Exception as exc:
            active = "scanner" if route_path in ("/scanner", "/scan") or route_path.startswith("/scan/") else "watchlist" if route_path.startswith("/watchlist") else "batch" if route_path.startswith("/batch") else "home" if route_path in ("/", "") else "backtest"
            form = render_ashare_scanner(params) if market == "cn" and active == "scanner" else render_ashare_watchlist_page(params) if market == "cn" and active == "watchlist" else render_market_placeholder(active) if market == "cn" else render_scanner_form(params) if active == "scanner" else render_watchlist_page(params) if active == "watchlist" else render_batch_form(params) if active == "batch" else render_dashboard() if active == "home" else render_backtest_form(params)
            self.send_bytes(page_shell(form + f'<div class="error">{html.escape(str(exc))}</div>', active, market), 500)

    def add_watchlist_item(self, params: dict[str, list[str]]) -> None:
        symbol = field(params, "symbol", "")
        group = field(params, "group", "观察")
        note = field(params, "note", "")
        try:
            add_watchlist_symbol(symbol, group, note)
            content = render_watchlist_page({})
        except ValueError as exc:
            content = render_watchlist_page(params) + f'<div class="error">{html.escape(str(exc))}</div>'
        self.send_bytes(page_shell(content, "watchlist"))

    def add_watchlist_item_json(self, params: dict[str, list[str]]) -> None:
        try:
            symbol = field(params, "symbol", "")
            group = field(params, "group", "候选")
            note = field(params, "note", "")
            symbols = add_watchlist_symbol(symbol, group, note)
            self.send_json({"ok": True, "symbols": symbols})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def update_watchlist_item(self, params: dict[str, list[str]]) -> None:
        try:
            update_watchlist_symbol(field(params, "symbol", ""), field(params, "group", "观察"), field(params, "note", ""))
            content = render_watchlist_page({})
        except ValueError as exc:
            content = render_watchlist_page(params) + f'<div class="error">{html.escape(str(exc))}</div>'
        self.send_bytes(page_shell(content, "watchlist"))

    def delete_watchlist_item(self, params: dict[str, list[str]]) -> None:
        symbol = field(params, "symbol", "")
        delete_watchlist_symbol(symbol)
        self.send_bytes(page_shell(render_watchlist_page({}), "watchlist"))

    def start_ashare_scan_job(self, params: dict[str, list[str]]) -> None:
        active = active_scan_job()
        if active:
            job_id, _ = active
            self.send_json(
                {
                    "status": "error",
                    "error": f"已有扫描任务正在运行：{job_id}。请等待完成后再启动 A 股扫描。",
                },
                status=409,
            )
            return
        job_id = f"ashare-{uuid.uuid4().hex[:10]}"
        set_job(job_id, status="queued", message="排队中", total=0, scanned=0, candidates=0, errors=0, current="")
        worker = threading.Thread(target=execute_ashare_scan_job, args=(job_id, params), daemon=True)
        worker.start()
        self.send_json({"status": "queued", "job_id": job_id})

    def ashare_scan_job_status(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "job_id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到 A 股扫描任务"}, status=404)
            return
        self.send_json(job)

    def add_ashare_watchlist_item(self, params: dict[str, list[str]]) -> None:
        try:
            add_ashare_watchlist_symbol(
                field(params, "symbol", ""),
                field(params, "group", "观察"),
                field(params, "note", ""),
                field(params, "name", ""),
                field(params, "sector", ""),
            )
            content = render_ashare_watchlist_page({})
        except Exception as exc:
            content = render_ashare_watchlist_page(params) + f'<div class="error">{html.escape(str(exc))}</div>'
        self.send_bytes(page_shell(content, "watchlist", "cn"))

    def delete_ashare_watchlist_item(self, params: dict[str, list[str]]) -> None:
        delete_ashare_watchlist_symbol(field(params, "symbol", ""))
        self.send_bytes(page_shell(render_ashare_watchlist_page({}), "watchlist", "cn"))

    def start_scan_job(self, params: dict[str, list[str]]) -> None:
        scan_end = default_scan_end_date()
        start = field(params, "start", default_scan_start_date(scan_end).isoformat())
        end = field(params, "end", scan_end.isoformat())
        try:
            validate_scan_range(start, end)
        except ValueError as exc:
            self.send_json({"status": "error", "error": str(exc)}, status=400)
            return
        active = active_scan_job()
        if active:
            active_id, active_job = active
            self.send_json(
                {
                    "status": "busy",
                    "error": "已有扫描任务正在运行，请等待完成，或先暂停/终止当前任务。",
                    "active_job_id": active_id,
                    "active_status": active_job.get("status", ""),
                    "active_message": active_job.get("message", ""),
                },
                status=409,
            )
            return
        job_id = uuid.uuid4().hex
        set_job(job_id, status="queued", message="排队中", total=0, scanned=0, candidates=0, errors=0, current="", pause_requested=False, stop_requested=False)
        worker = threading.Thread(target=execute_scan_job, args=(job_id, params), daemon=True)
        worker.start()
        self.send_json({"job_id": job_id})

    def delete_scan_result(self) -> None:
        deleted = delete_latest_scan()
        message = f"已删除当前扫描结果（清理 {deleted} 个文件）。" if deleted else "当前没有可删除的扫描结果。"
        content = render_scanner_form({}) + f'<section class="result"><p class="hint">{html.escape(message)}</p></section>'
        self.send_bytes(page_shell(content, "scanner"))

    def pause_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error"):
            self.send_json(job)
            return
        set_job(job_id, pause_requested=True, status="pausing", message="正在暂停，当前股票处理完后显示结果")
        self.send_json(get_job(job_id) or {"status": "pausing"})

    def resume_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error"):
            self.send_json(job)
            return
        set_job(job_id, pause_requested=False, status="running", message="继续扫描 B 点信号")
        self.send_json(get_job(job_id) or {"status": "running"})

    def stop_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error", "stopped"):
            self.send_json(job)
            return
        set_job(job_id, stop_requested=True, pause_requested=False, status="stopping", message="正在终止，当前股票处理完后保留结果")
        self.send_json(get_job(job_id) or {"status": "stopping"})

    def scan_job_status(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        self.send_json(job)

    def send_report(self, request_path: str) -> None:
        name = unquote(request_path.removeprefix("/reports/"))
        path = (REPORT_DIR / name).resolve()
        if not str(path).startswith(str(REPORT_DIR.resolve())) or not path.exists():
            self.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if path.suffix.lower() == ".html" else "text/csv; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if path.suffix.lower() == ".csv":
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def send_bytes(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    host = os.environ.get("MA5_HOST", "127.0.0.1")
    port = int(os.environ.get("MA5_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Open http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
