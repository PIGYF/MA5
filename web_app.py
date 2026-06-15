from __future__ import annotations

import csv
import html
import importlib.util
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
    calculate_kdj,
    fetch_bars,
    make_report,
    open_position_snapshot,
    price_cache_path,
    read_price_cache,
    build_ratchet_inputs,
    rolling_sma,
    summarize,
    trade_structure_label,
    write_equity,
    write_trades,
)
from ma5_config import (
    DATA_DIR,
    DEFAULT_BENCHMARK,
    DEFAULT_HIDE_WEAK_CANDIDATES,
    DEFAULT_MAX_SCAN_SYMBOLS,
    DEFAULT_MIN_MARKET_CAP_100M_USD,
    DIVERGENCE_EVENTS_PATH,
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
from macro_calendar import macro_risk_state
from ashare_lab import (
    ASHARE_ROUTE,
    ASHARE_BOARD_LABELS,
    AShareSignalSnapshot,
    ashare_board_filter_label,
    ashare_chart_payload,
    ashare_limit_pct,
    ashare_to_backtest_bars,
    fetch_ashare_profile,
    fetch_ashare_bars,
    filter_ashare_universe_by_board,
    latest_ashare_signal,
    load_ashare_universe_for_scan,
    normalize_ashare_boards,
    resolve_ashare_symbol_query,
    scan_ashare_candidates,
    suggest_ashare_symbols,
)
from scan_next_b import SignalResult, latest_b_signal, load_symbols, unique_symbols, write_html


SCAN_JOBS: dict[str, dict[str, object]] = {}
SCAN_JOBS_LOCK = threading.Lock()
ACTIVE_SCAN_STATUSES = {"queued", "running", "pausing", "paused", "stopping"}
FINISHED_SCAN_STATUSES = {"done", "stopped", "error"}
ASHARE_DEFAULT_MAX_SCAN_SYMBOLS = 6000
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
        if "created_at" not in job:
            job["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        job.update(updates)


def get_job(job_id: str) -> dict[str, object] | None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        return dict(job) if job else None


def active_scan_job(market: str | None = None) -> tuple[str, dict[str, object]] | None:
    with SCAN_JOBS_LOCK:
        for job_id, job in SCAN_JOBS.items():
            if job.get("status") not in ACTIVE_SCAN_STATUSES:
                continue
            if market and job_market(job_id, job) != market:
                continue
            if job.get("status") in ACTIVE_SCAN_STATUSES:
                return job_id, dict(job)
    return None


def job_market(job_id: str, job: dict[str, object] | None = None) -> str:
    if job and isinstance(job.get("market"), str):
        return str(job["market"])
    return "cn" if str(job_id).startswith("ashare-") else "us"


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
    payload.update(
        {
            "job_id": job_id,
            "market": market,
            "market_label": "A股" if market == "cn" else "美股",
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
    if latest_job:
        return latest_job_id, latest_job
    return None


def job_pause_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("pause_requested"))


def job_stop_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("stop_requested"))


def classify_scan_error(reason: str) -> str:
    text = str(reason or "").lower()
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


def render_error_category_chips(errors: list[tuple[str, str]]) -> str:
    summary = summarize_error_categories(errors)
    if not summary:
        return ""
    chips = "".join(
        f'<span class="scan-fact"><span>{html.escape(category)}</span>{count}</span>'
        for category, count in sorted(summary.items(), key=lambda item: (-item[1], item[0]))
    )
    return f'<div class="scan-facts">{chips}</div>'


def checkbox_field(params: dict[str, list[str]], name: str, default: bool = False) -> bool:
    values = params.get(name)
    if not values:
        return default
    return any(str(value).strip().lower() in {"1", "true", "on", "yes"} for value in values)


def display_preset_for_dates(params: dict[str, list[str]], preset: str, default_end: date) -> str:
    if preset == "custom":
        return "custom"
    if "start" not in params and "end" not in params:
        return preset
    start_value = field(params, "start", "")
    end_value = field(params, "end", default_end.isoformat())
    try:
        end_date = date.fromisoformat(end_value)
    except ValueError:
        return "custom"
    expected_start = start_for_preset(preset, end_date).isoformat()
    return preset if start_value == expected_start else "custom"


def package_health(name: str) -> tuple[str, str]:
    return ("正常", "condition-on") if importlib.util.find_spec(name) else ("缺失", "condition-off")


def file_health(path: Path, max_age_seconds: int | None = None) -> tuple[str, str]:
    if not path.exists():
        return "无缓存", "condition-off"
    if max_age_seconds is None:
        return "已缓存", "condition-on"
    age = time.time() - path.stat().st_mtime
    if age <= max_age_seconds:
        return "新鲜", "condition-on"
    days = max(1, int(age // 86400))
    return f"{days}天前", "condition-off"


def render_health_tag(label: str, value: str, cls: str = "condition-primary") -> str:
    return f'<span class="condition-tag {cls}">{html.escape(label)} {html.escape(value)}</span>'


def render_data_health_panel(market: str) -> str:
    if market == "cn":
        packages = [("efinance", "efinance"), ("pytdx", "pytdx"), ("yfinance备用", "yfinance")]
        latest = load_latest_ashare_scan()
        latest_errors = [
            (str(item.get("symbol", "")), str(item.get("reason", "")))
            for item in (latest or {}).get("errors", [])
            if isinstance(item, dict)
        ]
        universe_status, universe_cls = file_health(DATA_DIR / "ashare" / "universe_cache.json", 18 * 60 * 60)
        sector_status, sector_cls = file_health(DATA_DIR / "ashare" / "sector_map.json", 7 * 24 * 60 * 60)
        job = latest_job_for_market("cn", include_finished=True)
        source_tags = [
            render_health_tag(label, *package_health(package))
            for label, package in packages
        ]
        cache_tags = [
            render_health_tag("股票池缓存", universe_status, universe_cls),
            render_health_tag("行业缓存", sector_status, sector_cls),
        ]
        title = "A股数据源状态"
        note = "A股优先使用直连/efinance/通达信等多数据源；这里显示依赖和缓存是否可用。"
    else:
        latest = load_latest_scan()
        latest_errors = [
            (str(item.get("symbol", "")), str(item.get("reason", "")))
            for item in (latest or {}).get("errors", [])
            if isinstance(item, dict)
        ]
        nasdaq_status, nasdaq_cls = file_health(NASDAQ_CACHE_PATH if NASDAQ_CACHE_PATH.exists() else LEGACY_NASDAQ_CACHE_PATH, NASDAQ_CACHE_SECONDS)
        earnings_status, earnings_cls = file_health(EARNINGS_CACHE_PATH, EARNINGS_CACHE_SECONDS)
        price_files = len(list(PRICE_CACHE_DIR.glob("*.csv"))) if PRICE_CACHE_DIR.exists() else 0
        job = latest_job_for_market("us", include_finished=True)
        source_tags = [render_health_tag("yfinance", *package_health("yfinance"))]
        cache_tags = [
            render_health_tag("股票池缓存", nasdaq_status, nasdaq_cls),
            render_health_tag("财报缓存", earnings_status, earnings_cls),
            render_health_tag("价格缓存", f"{price_files}个", "condition-on" if price_files else "condition-off"),
        ]
        title = "美股数据源状态"
        note = "美股行情和财报主要依赖 yfinance，股票池使用 Nasdaq 缓存和接口。"

    job_tags = []
    if job:
        job_id, job_payload = job
        normalized = normalize_job_payload(job_id, job_payload)
        job_tags.append(render_health_tag("最近任务", str(normalized["status_label"]), "condition-on" if normalized["is_active"] else "condition-primary"))
        job_tags.append(render_health_tag("进度", f'{normalized["progress_pct"]}%', "condition-primary"))
    else:
        job_tags.append(render_health_tag("最近任务", "无", "condition-off"))

    error_html = ""
    if latest_errors:
        error_html = f"""
    <div class="condition-note">最近扫描失败分类</div>
    {render_error_category_chips(latest_errors)}
"""

    return f"""
<section class="result strategy-condition-panel" data-condition-panel data-panel-key="{html.escape(market)}-data-health">
  <div class="strategy-condition-head">
    <div>
      <strong>{title}</strong>
      <p class="hint">{note}</p>
    </div>
    <div class="condition-head-actions">
      <span class="condition-tag condition-primary">Health</span>
      <button type="button" class="condition-panel-toggle" data-condition-collapse aria-label="折叠或展开{html.escape(title)}">折叠</button>
      <button type="button" class="condition-panel-toggle" data-condition-pin aria-label="固定或取消固定{html.escape(title)}">固定</button>
    </div>
  </div>
  <div class="condition-panel-body">
    <div class="scan-facts">
      {''.join(source_tags)}
      {''.join(cache_tags)}
      {''.join(job_tags)}
    </div>
    {error_html}
  </div>
</section>
"""


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
    macro = macro_risk_state(date.today())
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
            "macro": macro,
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
            "macro": macro,
        }


def macro_day_label(event_date: date, today: date) -> str:
    delta = (event_date - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == -1:
        return "昨天"
    if delta > 1:
        return f"{delta}天后"
    return f"{abs(delta)}天前"


def render_market_environment_bar(env: dict[str, object] | None = None) -> str:
    env = env or market_environment()
    tone = html.escape(str(env.get("tone", "neutral")))
    macro = env.get("macro") if isinstance(env.get("macro"), dict) else {}
    macro_tone = html.escape(str(macro.get("tone", "neutral")))
    macro_events = macro.get("events", [])
    today = date.today()
    event_chips = []
    if isinstance(macro_events, list):
        for event in macro_events[:5]:
            event_date = getattr(event, "date", None)
            title = getattr(event, "title", "")
            time_et = getattr(event, "time_et", "")
            impact = getattr(event, "impact", "medium")
            if not isinstance(event_date, date):
                continue
            event_chips.append(
                f"""<span class="macro-event macro-{html.escape(str(impact))}">
                  <b>{html.escape(macro_day_label(event_date, today))}</b>
                  {html.escape(event_date.isoformat())} {html.escape(str(time_et))} ET · {html.escape(str(title))}
                </span>"""
            )
    if not event_chips:
        event_chips.append('<span class="macro-event macro-medium">未来21天暂无已录入大事</span>')
    return f"""
<section class="market-bar market-{tone}">
  <div class="market-main">
    <div>
      <strong>{html.escape(str(env.get("state", "Unavailable")))}</strong>
      <span>{html.escape(str(env.get("symbol", "QQQ")))} {html.escape(str(env.get("date", "-")))}</span>
    </div>
    <p>{html.escape(str(env.get("symbol", "QQQ")))} 距20MA {float(env.get("dist20", 0.0)):.2f}% / 距50MA {float(env.get("dist50", 0.0)):.2f}% / 20MA {html.escape(str(env.get("ma20_direction", "-")))} / VIX {float(env.get("vix", 0.0)):.1f} {html.escape(str(env.get("vix_label", "")))}。{html.escape(str(env.get("message", "")))}</p>
  </div>
  <div class="macro-box macro-tone-{macro_tone}">
    <div>
      <strong>{html.escape(str(macro.get("label", "宏观日历")))}</strong>
      <span>{html.escape(str(macro.get("message", "")))}</span>
    </div>
    <div class="macro-events">{"".join(event_chips)}</div>
    <p class="macro-links">来源：
      <a href="https://www.bls.gov/schedule/2026/home.htm" target="_blank">BLS</a>
      <a href="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm" target="_blank">Fed</a>
      <a href="https://www.bea.gov/news/schedule/full" target="_blank">BEA</a>
    </p>
  </div>
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


EVENT_TYPE_LABELS = {
    "bullish": "利好",
    "bearish": "利空",
}
DIVERGENCE_TYPE_LABELS = {
    "good_news_ignored": "利好未涨",
    "bad_news_resilient": "利空不跌",
}
IMPORTANCE_LABELS = {
    "major": "重大",
    "medium": "中等",
    "minor": "一般",
}


def load_divergence_events() -> list[dict[str, str]]:
    if not DIVERGENCE_EVENTS_PATH.exists():
        return []
    try:
        payload = json.loads(DIVERGENCE_EVENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_events = payload.get("events", []) if isinstance(payload, dict) else []
    events: list[dict[str, str]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        symbol = normalize_yahoo_symbol(str(raw.get("symbol", "")))
        event_date = str(raw.get("event_date", "")).strip()
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue
        if not symbol:
            continue
        events.append(
            {
                "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
                "symbol": symbol.upper(),
                "event_date": event_date,
                "event_type": str(raw.get("event_type") or "bullish"),
                "divergence_type": str(raw.get("divergence_type") or "good_news_ignored"),
                "importance": str(raw.get("importance") or "medium"),
                "note": str(raw.get("note") or "").strip()[:240],
                "created_at": str(raw.get("created_at") or ""),
            }
        )
    return sorted(events, key=lambda item: (item["symbol"], item["event_date"]), reverse=True)


def save_divergence_events(events: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "events": events,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    DIVERGENCE_EVENTS_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def add_divergence_event(params: dict[str, list[str]]) -> dict[str, str]:
    symbol = normalize_yahoo_symbol(field(params, "symbol", ""))
    if not symbol:
        raise ValueError("缺少股票代码。")
    event_date = field(params, "event_date", "")
    try:
        date.fromisoformat(event_date)
    except ValueError as exc:
        raise ValueError("请输入有效的事件日期。") from exc
    event_type = field(params, "event_type", "bullish")
    divergence_type = field(params, "divergence_type", "good_news_ignored")
    importance = field(params, "importance", "medium")
    if event_type not in EVENT_TYPE_LABELS:
        event_type = "bullish"
    if divergence_type not in DIVERGENCE_TYPE_LABELS:
        divergence_type = "good_news_ignored"
    if importance not in IMPORTANCE_LABELS:
        importance = "medium"
    event = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol.upper(),
        "event_date": event_date,
        "event_type": event_type,
        "divergence_type": divergence_type,
        "importance": importance,
        "note": field(params, "note", "")[:240],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    events = load_divergence_events()
    events.append(event)
    save_divergence_events(events)
    return event


def delete_divergence_event(event_id: str) -> None:
    event_id = event_id.strip()
    events = [event for event in load_divergence_events() if event.get("id") != event_id]
    save_divergence_events(events)


def business_days_between(start: date, end: date) -> int:
    if end < start:
        return -business_days_between(end, start)
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def divergence_window_status(event_date: date, as_of: date) -> tuple[str, str, int]:
    day_count = business_days_between(event_date, as_of)
    if day_count < 0:
        return "future", "未发生", day_count
    if 13 <= day_count <= 27:
        return "window", "分歧窗口", day_count
    if day_count < 13:
        return "building", "观察期", day_count
    return "expired", "已过期", day_count


def divergence_score(event: dict[str, str], status: str) -> int:
    if status == "window":
        score = 3
    elif status == "building":
        score = 2
    else:
        score = 0
    if event.get("importance") == "major":
        score += 1
    if event.get("divergence_type") == "bad_news_resilient":
        score += 1
    return min(score, 5)


def enriched_divergence_event(event: dict[str, str], as_of: date) -> dict[str, object]:
    event_day = date.fromisoformat(event["event_date"])
    status_key, status_label, day_count = divergence_window_status(event_day, as_of)
    return {
        **event,
        "event_type_label": EVENT_TYPE_LABELS.get(event.get("event_type", ""), "事件"),
        "divergence_type_label": DIVERGENCE_TYPE_LABELS.get(event.get("divergence_type", ""), "分歧"),
        "importance_label": IMPORTANCE_LABELS.get(event.get("importance", ""), "中等"),
        "status_key": status_key,
        "status_label": status_label,
        "day_count": day_count,
        "score": divergence_score(event, status_key),
    }


def divergence_events_for_symbol(symbol: str, as_of: date | None = None) -> list[dict[str, object]]:
    clean = normalize_yahoo_symbol(symbol)
    if not clean:
        return []
    as_of = as_of or default_scan_end_date()
    events = [event for event in load_divergence_events() if event["symbol"] == clean.upper()]
    return [enriched_divergence_event(event, as_of) for event in sorted(events, key=lambda item: item["event_date"], reverse=True)]


def best_divergence_for_symbol(symbol: str, as_of_text: str) -> dict[str, object] | None:
    try:
        as_of = date.fromisoformat(as_of_text)
    except ValueError:
        as_of = default_scan_end_date()
    events = divergence_events_for_symbol(symbol, as_of)
    if not events:
        return None
    rank = {"window": 0, "building": 1, "future": 2, "expired": 3}
    return sorted(events, key=lambda item: (rank.get(str(item.get("status_key")), 9), -int(item.get("score", 0)), str(item.get("event_date", ""))), reverse=False)[0]


def render_divergence_chip(event: dict[str, object] | None) -> str:
    if not event:
        return '<span class="divergence-chip divergence-none">无记录</span>'
    status = html.escape(str(event.get("status_key", "none")))
    label = html.escape(str(event.get("status_label", "-")))
    event_date = html.escape(str(event.get("event_date", "-")))
    div_label = html.escape(str(event.get("divergence_type_label", "-")))
    importance = html.escape(str(event.get("importance_label", "-")))
    day_count = event.get("day_count", "-")
    return f'<span class="divergence-chip divergence-{status}" title="{event_date} · {importance} · {div_label}">D+{day_count} {label}</span>'


def render_divergence_event_list(symbol: str, as_of: date | None = None) -> str:
    events = divergence_events_for_symbol(symbol, as_of)
    if not events:
        return '<div class="divergence-empty">当前股票还没有记录分歧事件。</div>'
    items = []
    for event in events[:8]:
        event_id = quote(str(event.get("id", "")))
        status = html.escape(str(event.get("status_key", "none")))
        note = html.escape(str(event.get("note", "")) or "-")
        items.append(
            f"""<div class="divergence-item divergence-{status}">
  <div><strong>{html.escape(str(event.get("event_date", "-")))} · {html.escape(str(event.get("importance_label", "-")))} {html.escape(str(event.get("divergence_type_label", "-")))}</strong>
  <span>D+{html.escape(str(event.get("day_count", "-")))} · {html.escape(str(event.get("status_label", "-")))} · {html.escape(str(event.get("score", 0)))}/5</span></div>
  <p>{note}</p>
  <a class="delete-link" href="/watchlist/divergence/delete?id={event_id}&symbol={quote(symbol)}" onclick="return confirm('确认删除这条分歧事件？');">删除</a>
</div>"""
        )
    return "".join(items)


def ashare_watchlist_path() -> Path:
    return DATA_DIR / "ashare" / "watchlist.json"


def ashare_latest_scan_path() -> Path:
    return DATA_DIR / "ashare" / "latest_scan.json"


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


def ashare_snapshot_to_dict(row: AShareSignalSnapshot) -> dict[str, object]:
    return dict(row.__dict__)


def load_latest_ashare_scan() -> dict[str, object] | None:
    path = ashare_latest_scan_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        path.unlink(missing_ok=True)
        return None


def save_latest_ashare_scan(
    candidates: list[AShareSignalSnapshot],
    errors: list[tuple[str, str]],
    scanned: int,
    universe_source: str,
    market_cap_filter_applied: bool,
    params: dict[str, list[str]],
) -> None:
    path = ashare_latest_scan_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    signal_dates = [row.latest_date for row in candidates if row.latest_date]
    signal_date = max(signal_dates) if signal_dates else date.today().isoformat()
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": signal_date,
        "source": universe_source,
        "market_cap_filter_applied": market_cap_filter_applied,
        "summary": {"scanned": scanned, "candidates": len(candidates), "failed": len(errors)},
        "params": {key: values[-1] if len(values) == 1 else values for key, values in params.items()},
        "candidates": [ashare_snapshot_to_dict(row) for row in candidates],
        "errors": [{"symbol": symbol, "reason": reason} for symbol, reason in errors],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_latest_ashare_scan() -> bool:
    path = ashare_latest_scan_path()
    if path.exists():
        path.unlink(missing_ok=True)
        return True
    return False


def add_ashare_watchlist_symbol(symbol: str, group: str = "观察", note: str = "", name: str = "", sector: str = "") -> list[dict[str, str]]:
    clean = normalize_ashare_code_for_storage(resolve_ashare_symbol_query(symbol))
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


def file_group_summary(paths: list[Path]) -> dict[str, object]:
    files = [path for path in paths if path.exists() and path.is_file()]
    total_size = sum(path.stat().st_size for path in files)
    latest = max((path.stat().st_mtime for path in files), default=0)
    return {
        "files": len(files),
        "size_mb": total_size / 1_000_000,
        "latest": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest)) if latest else "-",
    }


def directory_file_summary(path: Path) -> dict[str, object]:
    files = [item for item in path.iterdir() if item.is_file()] if path.exists() else []
    return file_group_summary(files)


def cache_dashboard_summary() -> dict[str, dict[str, object]]:
    us_market_paths = [NASDAQ_CACHE_PATH, EARNINGS_CACHE_PATH]
    if LEGACY_NASDAQ_CACHE_PATH != NASDAQ_CACHE_PATH:
        us_market_paths.append(LEGACY_NASDAQ_CACHE_PATH)
    ashare_cache_paths = [DATA_DIR / "ashare" / "universe_cache.json", DATA_DIR / "ashare" / "sector_map.json"]
    latest_scan_paths = [LATEST_SCAN_PATH, ashare_latest_scan_path()]
    return {
        "reports": directory_file_summary(REPORT_DIR),
        "prices": directory_file_summary(PRICE_CACHE_DIR),
        "us_market": file_group_summary(us_market_paths),
        "ashare": file_group_summary(ashare_cache_paths),
        "latest": file_group_summary(latest_scan_paths),
    }


def delete_files(paths: list[Path]) -> int:
    deleted = 0
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


def delete_directory_files(path: Path) -> int:
    if not path.exists():
        return 0
    return delete_files([item for item in path.iterdir() if item.is_file()])


def clear_cache_area(area: str) -> str:
    if area == "reports":
        deleted = delete_directory_files(REPORT_DIR)
        return f"已清理报告文件 {deleted} 个。"
    if area == "prices":
        deleted = delete_directory_files(PRICE_CACHE_DIR)
        return f"已清理行情缓存 {deleted} 个。"
    if area == "us_market":
        deleted = delete_files([NASDAQ_CACHE_PATH, LEGACY_NASDAQ_CACHE_PATH, EARNINGS_CACHE_PATH])
        return f"已清理美股市场/财报缓存 {deleted} 个。"
    if area == "ashare":
        deleted = delete_files([DATA_DIR / "ashare" / "universe_cache.json", DATA_DIR / "ashare" / "sector_map.json"])
        return f"已清理 A 股股票池/板块缓存 {deleted} 个。"
    if area == "latest":
        us_deleted = delete_latest_scan()
        cn_deleted = 1 if delete_latest_ashare_scan() else 0
        return f"已清理最新扫描结果 {us_deleted + cn_deleted} 个相关文件。"
    return "未知缓存类型，未执行清理。"


def page_shell(content: str, active: str = "backtest", market: str = "us") -> bytes:
    prefix = "/cn" if market == "cn" else "/us"
    home_active = " active" if active == "home" else ""
    backtest_active = " active" if active == "backtest" else ""
    scanner_active = " active" if active == "scanner" else ""
    batch_active = " active" if active == "batch" else ""
    watchlist_active = " active" if active == "watchlist" else ""
    action_active = " active" if market == "global" else ""
    us_market_active = " active" if market == "us" else ""
    cn_market_active = " active" if market == "cn" else ""
    subnav = ""
    if market in ("us", "cn"):
        batch_link = f'<a class="{batch_active}" href="{prefix}/batch">批量回测</a>' if market == "us" else ""
        subnav = f"""
    <nav class="tabs" aria-label="市场功能">
      <a class="{scanner_active}" href="{prefix}/scanner">选股器</a>
      <a class="{watchlist_active}" href="{prefix}/watchlist">自选池</a>
      <a class="{backtest_active}" href="{prefix}/backtest">回测</a>
      {batch_link}
    </nav>"""
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
.form-options {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 8px; margin-top: 2px; border-top: 1px solid #e3e7ee; }}
.form-options > span {{ color: #64748b; font-size: 12px; font-weight: 900; margin-right: 2px; }}
.form-options .checkbox-label {{ min-height: 30px; padding: 3px 8px; border: 1px solid #e3e7ee; border-radius: 4px; background: #f8fafc; }}
.form-section-title {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; margin-top: 4px; padding-top: 8px; border-top: 1px solid #e3e7ee; color: #131722; font-size: 12px; font-weight: 900; }}
.form-section-title:first-child {{ margin-top: 0; padding-top: 0; border-top: 0; }}
.form-section-title span {{ color: #64748b; font-weight: 700; }}
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
.market-bar {{ display: grid; grid-template-columns: minmax(280px, .9fr) minmax(360px, 1.3fr); align-items: start; gap: 14px; border: 1px solid #d6dbe3; border-left-width: 4px; border-radius: 6px; background: #fff; padding: 10px 12px; margin: 0 0 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.market-bar .market-main > div, .macro-box > div:first-child {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
.market-bar strong {{ font-size: 15px; }}
.market-bar span, .market-bar p {{ color: #5d6675; font-size: 13px; margin: 0; line-height: 1.45; }}
.market-good {{ border-left-color: #089981; }}
.market-warn {{ border-left-color: #f59e0b; }}
.market-bad {{ border-left-color: #f23645; }}
.market-neutral {{ border-left-color: #94a3b8; }}
.macro-box {{ border-left: 1px solid #e3e7ee; padding-left: 14px; }}
.macro-box strong {{ color: #131722; }}
.macro-events {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.macro-event {{ display: inline-flex; align-items: center; gap: 5px; border: 1px solid #e3e7ee; border-radius: 999px; padding: 4px 8px; background: #f8fafc; color: #334155; font-size: 12px; white-space: nowrap; }}
.macro-event b {{ color: #131722; }}
.macro-high {{ border-color: #ffc9cf; background: #fff5f6; color: #b42332; }}
.macro-medium {{ border-color: #fed7aa; background: #fff7ed; color: #9a4f00; }}
.macro-tone-bad strong {{ color: #d12030; }}
.macro-tone-warn strong {{ color: #b26b00; }}
.macro-links {{ margin-top: 6px !important; font-size: 12px !important; }}
.macro-links a {{ margin-left: 6px; font-weight: 800; }}
.toolbar {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }}
.toolbar .links {{ margin: 0; }}
.latest-scan-card {{ margin: 0 0 20px; }}
.latest-scan-card .toolbar {{ margin-bottom: 0; align-items: flex-start; }}
.scan-facts {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 9px; }}
.scan-fact {{ display: inline-flex; gap: 6px; align-items: center; border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 999px; padding: 5px 9px; font-size: 12px; color: #334155; }}
.scan-fact span {{ color: #64748b; font-weight: 700; }}
.cache-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
.cache-actions form {{ margin: 0; }}
.cache-actions button {{ min-height: 30px; padding: 6px 10px; font-size: 12px; }}
.notice {{ background: #eefbf7; border: 1px solid #9fd8cc; color: #067a6b; padding: 10px 12px; border-radius: 6px; margin: 0 0 12px; font-weight: 800; }}
.scan-result-alert {{ border-left: 4px solid #2962ff; background: #f8fbff; box-shadow: 0 8px 20px rgba(41,98,255,.08); }}
.scan-result-alert .toolbar {{ margin-bottom: 0; }}
.scan-result-alert strong, .scan-result-alert h2 {{ color: #1e53e5; }}
.strategy-condition-panel {{ padding: 0; overflow: hidden; }}
.strategy-condition-panel.is-pinned {{ position: sticky; top: 12px; z-index: 30; box-shadow: 0 10px 24px rgba(15,23,42,.14); }}
.strategy-condition-panel.is-collapsed .condition-panel-body {{ display: none; }}
.strategy-condition-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; padding: 12px 14px; border-bottom: 1px solid #e3e7ee; background: #fbfcfe; }}
.strategy-condition-head strong {{ display: block; font-size: 15px; }}
.strategy-condition-head .hint {{ margin: 3px 0 0; }}
.condition-head-actions {{ display: flex; align-items: center; justify-content: flex-end; gap: 6px; flex-wrap: wrap; }}
.condition-panel-toggle {{ min-height: 26px; padding: 4px 8px; border-color: #c7ccd5; background: #fff; color: #334155; font-size: 12px; }}
.condition-panel-toggle.is-active {{ border-color: #2962ff; background: rgba(41,98,255,.08); color: #1e53e5; }}
.condition-grid {{ display: grid; grid-template-columns: repeat(4, minmax(220px, 1fr)); gap: 0; }}
.condition-card {{ min-height: 150px; padding: 13px 14px; border-right: 1px solid #e3e7ee; }}
.condition-card:last-child {{ border-right: 0; }}
.condition-card h3 {{ margin: 0 0 9px; font-size: 13px; color: #131722; }}
.condition-list {{ display: grid; gap: 7px; margin: 0; padding: 0; list-style: none; }}
.condition-list li {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; color: #334155; font-size: 12px; line-height: 1.35; }}
.condition-list b {{ font-weight: 800; color: #131722; }}
.condition-tags {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px; }}
.condition-tag {{ flex: 0 0 auto; display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 7px; font-size: 11px; font-weight: 900; border: 1px solid #d6dbe3; background: #fff; color: #64748b; }}
.condition-on {{ border-color: #9fd8cc; background: rgba(8,153,129,.10); color: #067a6b; }}
.condition-off {{ border-color: #e3e7ee; background: #f1f5f9; color: #94a3b8; }}
.condition-primary {{ border-color: #bfdbfe; background: rgba(41,98,255,.09); color: #1e53e5; }}
.condition-note {{ margin-top: 10px; color: #64748b; font-size: 12px; line-height: 1.5; }}
.risk-note {{ display: flex; align-items: flex-start; gap: 8px; margin: 10px 14px 0; padding: 9px 10px; border: 1px solid #fed7aa; background: #fff7ed; color: #9a4f00; border-radius: 6px; font-size: 12px; line-height: 1.45; font-weight: 700; }}
.risk-note strong {{ color: #7c3f00; }}
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
.watchlist-chart-shell {{ position: relative; height: 880px; min-width: 520px; }}
.watchlist-chart {{ width: 100%; height: 100%; }}
.watchlist-price-chart {{ height: 640px; }}
.watchlist-kdj-chart {{ height: 200px; margin-top: 12px; border-top: 1px solid #eef1f5; }}
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
.divergence-panel {{ border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 6px; padding: 10px; margin: 0 0 12px; }}
.divergence-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 8px; }}
.divergence-head strong {{ font-size: 14px; }}
.divergence-form {{ display: grid; grid-template-columns: 130px 110px 140px 100px minmax(160px, 1fr) auto; gap: 8px; align-items: end; }}
.divergence-list {{ display: grid; gap: 8px; margin-top: 10px; }}
.divergence-item {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: center; border: 1px solid #e3e7ee; background: #fff; border-left-width: 4px; border-radius: 6px; padding: 8px; }}
.divergence-item strong {{ display: block; font-size: 13px; }}
.divergence-item span, .divergence-item p {{ display: block; margin: 3px 0 0; color: #64748b; font-size: 12px; }}
.divergence-window {{ border-left-color: #089981; }}
.divergence-building {{ border-left-color: #2962ff; }}
.divergence-expired {{ border-left-color: #94a3b8; }}
.divergence-future {{ border-left-color: #f59e0b; }}
.divergence-empty {{ color: #64748b; font-size: 12px; padding: 6px 0; }}
.divergence-chip {{ display: inline-flex; align-items: center; white-space: nowrap; border-radius: 999px; padding: 4px 8px; font-size: 12px; font-weight: 900; border: 1px solid #e3e7ee; background: #f8fafc; color: #334155; }}
.divergence-chip.divergence-window {{ border-color: #9fd8cc; background: rgba(8,153,129,.12); color: #067a6b; }}
.divergence-chip.divergence-building {{ border-color: #bfdbfe; background: rgba(41,98,255,.10); color: #1e53e5; }}
.divergence-chip.divergence-expired {{ color: #64748b; background: #f1f5f9; }}
.divergence-chip.divergence-none {{ color: #94a3b8; background: #fff; }}
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
.progress-box.active {{ display: block; }}
.progress-track {{ height: 8px; background: #e6eaf0; border-radius: 999px; overflow: hidden; margin: 8px 0; }}
.progress-bar {{ height: 100%; width: 0%; background: #2962ff; transition: width .2s ease; }}
.progress-meta {{ color: #475569; font-size: 13px; }}
.progress-actions {{ display: flex; gap: 8px; margin-top: 10px; }}
.progress-actions button[hidden] {{ display: none; }}
.dashboard-grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; }}
.dashboard-panel {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 12px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.dashboard-panel h2 {{ margin-top: 0; }}
.quick-actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.checkbox-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 6px; }}
.check-inline {{ display: inline-flex; align-items: center; gap: 6px; min-height: 30px; padding: 5px 9px; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; color: #131722; font-size: 13px; font-weight: 800; cursor: pointer; }}
.check-inline input {{ width: auto; margin: 0; accent-color: #2962ff; }}
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
@media (max-width: 1200px) {{ .form {{ grid-template-columns: repeat(4, 1fr); }} .watchlist-grid {{ grid-template-columns: 1fr; }} .watchlist-chart-shell {{ min-width: 0; }} .divergence-form {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }} .condition-grid {{ grid-template-columns: repeat(2, minmax(220px, 1fr)); }} .condition-card:nth-child(2) {{ border-right: 0; }} .condition-card:nth-child(-n+2) {{ border-bottom: 1px solid #e3e7ee; }} }}
@media (max-width: 760px) {{ main {{ padding: 0 10px 18px; }} .app-topbar {{ margin: 0 -10px 12px; height: auto; padding: 10px; align-items: flex-start; flex-direction: column; }} .topbar-actions {{ width: 100%; flex-direction: column; align-items: stretch; gap: 8px; }} .market-switch, .tabs {{ width: 100%; overflow-x: auto; }} .form, .status-strip {{ grid-template-columns: repeat(2, 1fr); }} .wide {{ grid-column: span 2; }} .page-head {{ display: block; }} .condition-grid {{ grid-template-columns: 1fr; }} .condition-card, .condition-card:nth-child(2) {{ border-right: 0; border-bottom: 1px solid #e3e7ee; }} .condition-card:last-child {{ border-bottom: 0; }} }}
</style>
</head>
<body><main>
<header class="app-topbar">
  <div class="brand">MA5 Strategy Lab<span>选股 | 自选 | 回测</span></div>
  <div class="topbar-actions">
    <nav class="market-switch" aria-label="一级菜单">
      <a class="{action_active}" href="/">行动台</a>
      <a class="{us_market_active}" href="/us/scanner">美股</a>
      <a class="{cn_market_active}" href="/cn/scanner">A股</a>
    </nav>
    {subnav}
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

window.initializeConditionPanels = function(root = document) {{
  root.querySelectorAll("[data-condition-panel]").forEach((panel, index) => {{
    if (panel.dataset.conditionReady === "true") return;
    panel.dataset.conditionReady = "true";
    const key = panel.dataset.panelKey || `condition-panel-${{index}}`;
    const collapseButton = panel.querySelector("[data-condition-collapse]");
    const pinButton = panel.querySelector("[data-condition-pin]");
    const collapsed = localStorage.getItem(`ma5:${{key}}:collapsed`) === "1";
    const pinned = localStorage.getItem(`ma5:${{key}}:pinned`) === "1";
    function applyState() {{
      panel.classList.toggle("is-collapsed", panel.dataset.collapsed === "1");
      panel.classList.toggle("is-pinned", panel.dataset.pinned === "1");
      if (collapseButton) {{
        const isCollapsed = panel.dataset.collapsed === "1";
        collapseButton.textContent = isCollapsed ? "展开" : "折叠";
        collapseButton.setAttribute("aria-expanded", String(!isCollapsed));
      }}
      if (pinButton) {{
        const isPinned = panel.dataset.pinned === "1";
        pinButton.textContent = isPinned ? "取消固定" : "固定";
        pinButton.classList.toggle("is-active", isPinned);
      }}
    }}
    panel.dataset.collapsed = collapsed ? "1" : "0";
    panel.dataset.pinned = pinned ? "1" : "0";
    collapseButton?.addEventListener("click", () => {{
      panel.dataset.collapsed = panel.dataset.collapsed === "1" ? "0" : "1";
      localStorage.setItem(`ma5:${{key}}:collapsed`, panel.dataset.collapsed);
      applyState();
    }});
    pinButton?.addEventListener("click", () => {{
      panel.dataset.pinned = panel.dataset.pinned === "1" ? "0" : "1";
      localStorage.setItem(`ma5:${{key}}:pinned`, panel.dataset.pinned);
      applyState();
    }});
    applyState();
  }});
}};

window.initializeConditionPanels();

window.initializeSecondaryFilters = function(root = document) {{
  root.querySelectorAll("[data-secondary-filter-table]").forEach(table => {{
    const wrap = table.closest(".table-wrap");
    const panel = wrap?.previousElementSibling;
    if (!panel || !panel.matches("[data-secondary-filter-panel]") || panel.dataset.secondaryReady === "1") return;
    panel.dataset.secondaryReady = "1";
    const rows = Array.from(table.querySelectorAll("[data-secondary-row]"));
    const filters = Array.from(panel.querySelectorAll("[data-secondary-filter]"));
    const totalEl = panel.querySelector("[data-secondary-total]");
    const visibleEl = panel.querySelector("[data-secondary-visible]");
    const countEls = Array.from(panel.querySelectorAll("[data-secondary-count]"));
    const clearButton = panel.querySelector("[data-secondary-clear]");
    function applyFilters() {{
      const activeFilters = filters.filter(input => input.checked).map(input => input.getAttribute("data-secondary-filter") || "");
      const counts = Object.fromEntries(countEls.map(el => [el.getAttribute("data-secondary-count") || "", 0]));
      let visible = 0;
      for (const row of rows) {{
        for (const key of Object.keys(counts)) {{
          if (row.getAttribute(`data-filter-${{key.replaceAll("_", "-")}}`) === "1") counts[key] += 1;
        }}
        const show = activeFilters.every(key => row.getAttribute(`data-filter-${{key.replaceAll("_", "-")}}`) === "1");
        row.hidden = !show;
        if (show) visible += 1;
      }}
      if (totalEl) totalEl.textContent = String(rows.length);
      if (visibleEl) visibleEl.textContent = String(visible);
      countEls.forEach(el => {{
        const key = el.getAttribute("data-secondary-count") || "";
        el.textContent = String(counts[key] || 0);
      }});
    }}
    filters.forEach(input => input.addEventListener("change", applyFilters));
    clearButton?.addEventListener("click", () => {{
      filters.forEach(input => {{ input.checked = false; }});
      applyFilters();
    }});
    applyFilters();
  }});
}};

window.initializeSecondaryFilters();

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


def render_us_strategy_condition_panel(params: dict[str, list[str]], context: str = "scanner") -> str:
    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    require_ma5_rising = checkbox_field(params, "require_ma5_rising", True)
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", True)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", True)
    secondary_big_red_b1 = checkbox_field(params, "secondary_big_red_b1", False)
    secondary_above_ma5_3d = checkbox_field(params, "secondary_above_ma5_3d", False)

    def toggle_tag(enabled: bool, label: str) -> str:
        cls = "condition-on" if enabled else "condition-off"
        state = "启用" if enabled else "关闭"
        return f'<span class="condition-tag {cls}">{label} {state}</span>'

    b1_tags = " ".join(
        [
            toggle_tag(require_ma5_rising, "5MA向上"),
            toggle_tag(b1_require_20ma_gt_50ma, "20MA&gt;50MA"),
            toggle_tag(require_5ma_gt_20ma, "5MA&gt;20MA"),
            toggle_tag(secondary_big_red_b1, "大阴线B1"),
            toggle_tag(secondary_above_ma5_3d, "连续三天&gt;MA5"),
        ]
    )
    b2_requirements = []
    if require_ma5_rising:
        b2_requirements.append("5MA向上")
    if require_5ma_gt_20ma:
        b2_requirements.append("5MA&gt;20MA")
    b2_filter_text = " + ".join(b2_requirements) if b2_requirements else "无额外均线过滤"
    title_map = {
        "scanner": "当前美股选股条件",
        "backtest": "当前美股回测条件",
        "batch": "当前美股批量回测条件",
    }
    note_map = {
        "scanner": "扫描最后一根已完成日 K；触发信号后按下一交易日开盘执行。",
        "backtest": "本页用同一套 B1/B2 和卖出规则回测单只股票。",
        "batch": "本页把同一套条件批量应用到多个股票，便于横向比较。",
    }
    risk_note = ""
    if not require_5ma_gt_20ma:
        risk_note = '<div class="risk-note"><strong>防误买提醒</strong><span>当前关闭 5MA&gt;20MA，策略会允许 MA5 低于 MA20 的 B 点进入，买入后更容易很快触发 20MA 趋势止损。做防守型回测时建议打开。</span></div>'

    return f"""
<section class="result strategy-condition-panel" data-condition-panel data-panel-key="us-{html.escape(context)}-conditions">
  <div class="strategy-condition-head">
    <div>
      <strong>{title_map.get(context, "当前美股策略条件")}</strong>
      <p class="hint">{note_map.get(context, note_map["scanner"])}</p>
    </div>
    <div class="condition-head-actions">
      <span class="condition-tag condition-primary">Daily Close</span>
      <button type="button" class="condition-panel-toggle" data-condition-collapse aria-label="折叠或展开美股条件">折叠</button>
      <button type="button" class="condition-panel-toggle" data-condition-pin aria-label="固定或取消固定美股条件">固定</button>
    </div>
  </div>
  <div class="condition-panel-body">
    {risk_note}
    <div class="condition-grid">
      <div class="condition-card">
        <h3>B1 起爆</h3>
        <ul class="condition-list">
          <li><b>价格</b><span>收盘站上 {value("ma_length", "5")}MA</span></li>
          <li><b>趋势过滤</b><span class="condition-tags">{b1_tags}</span></li>
          <li><b>连续放量</b><span>{value("vol_high_days", "3")} 日 &gt; 均量×{value("vol_high_multiplier", "1.0")}</span></li>
          <li><b>巨量窗口</b><span>{value("massive_window", "7")} 日内 ≥ {value("massive_min_count", "1")} 次 ×{value("vol_multiplier", "1.45")}</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>B2 回踩</h3>
        <ul class="condition-list">
          <li><b>前提</b><span>已有 B1 趋势</span></li>
          <li><b>量价</b><span>巨量阳线</span></li>
          <li><b>位置</b><span>距 {value("ma_length", "5")}MA ≤ {value("reentry_pct", "4.5")}%</span></li>
          <li><b>过滤</b><span>{b2_filter_text}</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>执行</h3>
        <ul class="condition-list">
          <li><b>B1</b><span>目标仓位 50%</span></li>
          <li><b>B2</b><span>目标仓位 100%</span></li>
          <li><b>成交</b><span>下一交易日开盘</span></li>
          <li><b>价格</b><span>不使用盘后/夜盘</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>风控</h3>
        <ul class="condition-list">
          <li><b>均线</b><span>{value("ma_length", "5")}MA 下 {value("stop_5ma_pct", "7.5")}%</span></li>
          <li><b>趋势</b><span>连续 {value("below_20ma_stop_days", "2")} 日跌破 20MA</span></li>
          <li><b>成本</b><span>跌破成本 {value("hard_stop_pct", "20")}%</span></li>
          <li><b>弱趋势卖出</b><span>{value("weak_trend_exit_mode", "hybrid")} / MA5 {value("weak_ma5_reclaim_days", "5")}日 / MA20 {value("weak_ma20_reclaim_days", "10")}日</span></li>
        </ul>
      </div>
    </div>
  </div>
</section>
"""


def render_backtest_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    today = date.today()
    preset = field(params, "preset", "1y")
    display_preset = display_preset_for_dates(params, preset, today)
    start_default = start_for_preset(preset, today).isoformat()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", True) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", True) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", True) else ""
    secondary_big_red_checked = " checked" if checkbox_field(params, "secondary_big_red_b1", False) else ""
    secondary_above_ma5_checked = " checked" if checkbox_field(params, "secondary_above_ma5_3d", False) else ""

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
{render_us_strategy_condition_panel(params, "backtest")}
<form class="form" action="/run" method="get" id="backtest-form">
  <div class="form-section-title">基础设置 <span>标的、周期、资金和成本</span></div>
  <label>股票代码<input name="symbol" value="{value("symbol", "AAPL").upper()}" placeholder="AAPL"></label>
  <input type="hidden" name="strategy_name" value="ratchet">
  <label>回测周期
    <select name="preset" id="preset">
      <option value="6m"{selected(display_preset, "6m")}>近 6 个月</option>
      <option value="1y"{selected(display_preset, "1y")}>近 1 年</option>
      <option value="3y"{selected(display_preset, "3y")}>近 3 年</option>
      <option value="5y"{selected(display_preset, "5y")}>近 5 年</option>
      <option value="custom"{selected(display_preset, "custom")}>自定义</option>
    </select>
  </label>
  <label>开始日期<input type="date" name="start" id="start" value="{value("start", start_default)}"></label>
  <label>结束日期<input type="date" name="end" id="end" value="{value("end", today.isoformat())}"></label>
  <label>对比基准<input name="benchmark" value="{value("benchmark", DEFAULT_BENCHMARK).upper()}" placeholder="^IXIC"></label>
  <label>初始资金<input name="initial_cash" value="{value("initial_cash", "100000")}"></label>
  <label>手续费 %<input name="commission_pct" value="{value("commission_pct", "0.1")}"></label>
  <label>滑点 %<input name="slippage_pct" value="{value("slippage_pct", "0")}"></label>
  <div class="form-section-title">信号参数 <span>B1/B2、放量和回踩距离</span></div>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" id="vol_length" value="{value("vol_length", "20")}"></label>
  <label>连续放量天数<input name="vol_high_days" value="{value("vol_high_days", "3")}"></label>
  <label>连续放量倍数<input name="vol_high_multiplier" value="{value("vol_high_multiplier", "1.0")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>巨量观察窗口<input name="massive_window" value="{value("massive_window", "7")}"></label>
  <label>巨量最少次数<input name="massive_min_count" value="{value("massive_min_count", "1")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <div class="form-section-title">风控参数 <span>卖出和止损</span></div>
  <label>跌破均线止损 %<input name="stop_5ma_pct" value="{value("stop_5ma_pct", "7.5")}"></label>
  <label>连续跌破20MA天数<input name="below_20ma_stop_days" value="{value("below_20ma_stop_days", "2")}"></label>
  <label>成本强制止损 %<input name="hard_stop_pct" value="{value("hard_stop_pct", "20")}"></label>
  <label>弱趋势卖出
    <select name="weak_trend_exit_mode">
      <option value="hybrid"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "hybrid")}>混合模式：仅5MA&lt;20MA买入启用</option>
      <option value="off"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "off")}>关闭：全部使用标准止损</option>
      <option value="weak"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "weak")}>弱趋势持仓使用修复止损</option>
    </select>
  </label>
  <label>站回5MA期限<input name="weak_ma5_reclaim_days" value="{value("weak_ma5_reclaim_days", "5")}"></label>
  <label>站回20MA期限<input name="weak_ma20_reclaim_days" value="{value("weak_ma20_reclaim_days", "10")}"></label>
  <label>放量下跌倍数<input name="weak_volume_down_multiplier" value="{value("weak_volume_down_multiplier", "1.5")}"></label>
  <label>事件低点窗口<input name="weak_event_low_lookback" value="{value("weak_event_low_lookback", "27")}"></label>
  <div class="form-options">
    <span>可选买入条件</span>
    <input type="hidden" name="require_ma5_rising" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{require_ma5_rising_checked}> MA5向上</label>
    <input type="hidden" name="require_5ma_gt_20ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{require_5ma_gt_20ma_checked}> MA5&gt;MA20</label>
    <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{b1_require_20ma_gt_50ma_checked}> 20MA&gt;50MA</label>
    <input type="hidden" name="secondary_big_red_b1" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_big_red_b1" value="1"{secondary_big_red_checked}> 大阴线B1</label>
    <input type="hidden" name="secondary_above_ma5_3d" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_above_ma5_3d" value="1"{secondary_above_ma5_checked}> 连续三天&gt;MA5</label>
  </div>
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
start.addEventListener("change", () => {{ preset.value = "custom"; }});
end.addEventListener("change", () => {{ preset.value = "custom"; }});
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


def render_action_dashboard(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    us_latest = load_latest_scan()
    us_summary = us_latest.get("summary", {}) if us_latest else {}
    us_signal = str(us_latest.get("signal_date", "-")) if us_latest else "-"
    us_candidates = int(us_summary.get("visible_candidates", 0) or 0) if us_latest else 0
    us_strong = int(us_summary.get("strong", 0) or 0) if us_latest else 0
    us_watch_count = len(load_watchlist_items())

    cn_latest = load_latest_ashare_scan()
    cn_summary = cn_latest.get("summary", {}) if cn_latest else {}
    cn_signal = str(cn_latest.get("signal_date", "-")) if cn_latest else "-"
    cn_candidates = int(cn_summary.get("candidates", 0) or 0) if cn_latest else 0
    cn_scanned = int(cn_summary.get("scanned", 0) or 0) if cn_latest else 0
    cn_watch_count = len(load_ashare_watchlist_items())
    cache = cache_dashboard_summary()
    notice = field(params, "cache_message", "").strip()
    notice_html = f'<section class="notice">{html.escape(notice)}</section>' if notice else ""

    def cache_fact(key: str, label: str) -> str:
        item = cache[key]
        return f'<span class="scan-fact"><span>{html.escape(label)}</span>{int(item["files"])} 个 / {float(item["size_mb"]):.1f} MB</span>'

    def clear_form(area: str, label: str, danger: bool = False) -> str:
        cls = "danger" if danger else "secondary"
        return f"""
      <form action="/cache/clear" method="get" onsubmit="return confirm('确认清理{html.escape(label)}？自选池不会被删除，但下次打开会重新拉取相关数据。');">
        <input type="hidden" name="area" value="{html.escape(area)}">
        <button class="{cls}" type="submit">{html.escape(label)}</button>
      </form>"""

    return f"""
<section class="page-head">
  <div>
    <h1>行动台</h1>
    <p class="hint">先选择市场工作区，再进入选股、自选池或回测。这里保留两边市场的最新状态和最短路径。</p>
  </div>
  <div class="mode-pill">Global | Action Desk</div>
</section>
{notice_html}
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <h2>美股</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(us_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{us_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">Strong</div><div class="stat-value">{us_strong}</div></div>
      <div class="stat-card"><div class="stat-label">自选</div><div class="stat-value">{us_watch_count}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/us/scanner">进入美股选股器</a>
      <a class="btn btn-secondary" href="/us/scanner">选股器</a>
      <a class="btn btn-secondary" href="/us/watchlist">自选池</a>
      <a class="btn btn-secondary" href="/us/backtest">回测</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>A股</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(cn_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{cn_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">扫描数量</div><div class="stat-value">{cn_scanned}</div></div>
      <div class="stat-card"><div class="stat-label">自选</div><div class="stat-value">{cn_watch_count}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/cn/scanner">进入A股选股器</a>
      <a class="btn btn-secondary" href="/cn/scanner">选股器</a>
      <a class="btn btn-secondary" href="/cn/watchlist">自选池</a>
      <a class="btn btn-secondary" href="/cn/backtest">回测</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>缓存维护</h2>
    <p class="hint">清理的是服务器本地缓存和生成报告，不会删除美股/A股自选池。</p>
    <div class="scan-facts">
      {cache_fact("reports", "报告")}
      {cache_fact("prices", "行情缓存")}
      {cache_fact("us_market", "美股缓存")}
      {cache_fact("ashare", "A股缓存")}
      {cache_fact("latest", "扫描结果")}
    </div>
    <div class="cache-actions">
      {clear_form("reports", "清理报告", True)}
      {clear_form("prices", "清理行情缓存")}
      {clear_form("us_market", "清理美股缓存")}
      {clear_form("ashare", "清理A股缓存")}
      {clear_form("latest", "清理扫描结果", True)}
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>建议流程</h2>
    <div class="scan-facts">
      <span class="scan-fact"><span>盘后</span>先跑对应市场选股器</span>
      <span class="scan-fact"><span>筛选</span>把候选加入自选池看图</span>
      <span class="scan-fact"><span>验证</span>用回测对比条件开关</span>
      <span class="scan-fact"><span>执行</span>第二天按市场规则处理买卖</span>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>市场差异</h2>
    <div class="scan-facts">
      <span class="scan-fact"><span>美股</span>关注大盘环境、财报和流动性</span>
      <span class="scan-fact"><span>A股</span>额外处理涨跌停、高开和成交量失真</span>
      <span class="scan-fact"><span>图表</span>A股统一为K线+成交量均线+KDJ</span>
      <span class="scan-fact"><span>策略</span>条件默认严格，可在回测里关闭对比</span>
    </div>
  </div>
</section>
"""


def render_dashboard() -> str:
    latest = load_latest_scan()
    latest_summary = latest.get("summary", {}) if latest else {}
    latest_signal = str(latest.get("signal_date", "-")) if latest else "-"
    latest_plan = str(latest.get("planned_trade_date", "-")) if latest else "-"
    latest_candidates = int(latest_summary.get("visible_candidates", 0)) if latest else 0
    latest_strong = int(latest_summary.get("strong", 0)) if latest else 0
    latest_medium = int(latest_summary.get("medium", 0)) if latest else 0
    latest_failed = int(latest_summary.get("failed", 0)) if latest else 0
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
    <h1>美股行动台</h1>
    <p class="hint">先判断市场环境，再处理最新扫描和自选池风险；页面只保留今天需要做的动作。</p>
  </div>
  <div class="mode-pill">US | Action Desk</div>
</section>
{render_market_environment_bar()}
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <h2>扫描结果</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(latest_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{html.escape(latest_plan)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{latest_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">Strong</div><div class="stat-value">{latest_strong}</div></div>
      <div class="stat-card"><div class="stat-label">Medium</div><div class="stat-value">{latest_medium}</div></div>
      <div class="stat-card"><div class="stat-label">失败</div><div class="stat-value">{latest_failed}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/us/scanner">重新扫描</a>
      <a class="btn btn-secondary" href="/us/scan/latest">查看候选</a>
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
      <a class="btn" href="/us/watchlist">复盘自选池</a>
      <a class="btn btn-secondary" href="/us/backtest">单票回测</a>
      <a class="btn btn-secondary" href="/us/batch">批量回测</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>下一步</h2>
    <div class="scan-facts">
      <span class="scan-fact"><span>无扫描</span>先运行选股器生成今日候选</span>
      <span class="scan-fact"><span>有候选</span>优先看 Strong，再看 Medium</span>
      <span class="scan-fact"><span>有财报</span>7天内财报先降权或跳过</span>
      <span class="scan-fact"><span>要验证</span>用单票/批量回测看策略稳定性</span>
    </div>
  </div>
</section>
"""


def render_ashare_dashboard() -> str:
    latest = load_latest_ashare_scan()
    summary = latest.get("summary", {}) if latest else {}
    signal_date = str(latest.get("signal_date", "-")) if latest else "-"
    saved_at = str(latest.get("saved_at", "-")) if latest else "-"
    candidates = int(summary.get("candidates", 0) or 0) if latest else 0
    scanned = int(summary.get("scanned", 0) or 0) if latest else 0
    failed = int(summary.get("failed", 0) or 0) if latest else 0
    source = str(latest.get("source", "-")) if latest else "-"
    market_cap_filter = "已生效" if latest and latest.get("market_cap_filter_applied") else "未生效/无结果"
    watch_items = load_ashare_watchlist_items()

    return f"""
<section class="page-head">
  <div>
    <h1>A股行动台</h1>
    <p class="hint">默认入口聚焦盘后动作：先扫 A 股候选，再把值得跟踪的票放进自选池看图确认。</p>
  </div>
  <div class="mode-pill">A Share | Action Desk</div>
</section>
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <h2>扫描结果</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(signal_date)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{candidates}</div></div>
      <div class="stat-card"><div class="stat-label">扫描数量</div><div class="stat-value">{scanned}</div></div>
      <div class="stat-card"><div class="stat-label">失败</div><div class="stat-value">{failed}</div></div>
      <div class="stat-card"><div class="stat-label">市值过滤</div><div class="stat-value">{html.escape(market_cap_filter)}</div></div>
      <div class="stat-card"><div class="stat-label">保存时间</div><div class="stat-value">{html.escape(saved_at)}</div></div>
    </section>
    <p class="hint">股票池来源：{html.escape(source)}</p>
    <div class="quick-actions">
      <a class="btn" href="/cn/scanner">开始扫描</a>
      <a class="btn btn-secondary" href="/cn/scan/latest">查看候选</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>自选池</h2>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">自选数量</div><div class="stat-value">{len(watch_items)}</div></div>
      <div class="stat-card"><div class="stat-label">图表</div><div class="stat-value">MA5/KDJ</div></div>
      <div class="stat-card"><div class="stat-label">市场规则</div><div class="stat-value">涨跌停/高开</div></div>
      <div class="stat-card"><div class="stat-label">数据</div><div class="stat-value">A股独立</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn" href="/cn/watchlist">复盘自选池</a>
      <a class="btn btn-secondary" href="/cn/backtest">单票回测</a>
    </div>
  </div>
  <div class="dashboard-panel">
    <h2>下一步</h2>
    <div class="scan-facts">
      <span class="scan-fact"><span>无结果</span>先跑全市场或板块扫描</span>
      <span class="scan-fact"><span>有候选</span>看量能分、涨跌停状态和图形位置</span>
      <span class="scan-fact"><span>加自选</span>只保留明天值得盯盘的票</span>
      <span class="scan-fact"><span>要验证</span>用 A 股回测检查涨跌停和高开过滤</span>
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
    <p class="hint">硬条件为 MA5/B 点信号 + 20日均成交额达标，量能结构用于二次看图确认强弱。</p>
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
      <a class="btn btn-secondary" href="/cn/backtest">A 股回测</a>
    </div>
  </div>
</section>
"""
    labels = {
        "home": ("A股复盘面板", "后续这里会显示 A 股市场环境、A 股扫描摘要和 A 股自选池状态。"),
        "scanner": ("A股选股器", "后续这里会接入 A 股股票池、A 股策略和 A 股盘后选股。"),
        "watchlist": ("A股自选池", "后续这里会维护 A 股自选列表，并和美股自选池分开保存。"),
        "backtest": ("A股回测", "这里使用 A 股独立交易规则、手续费、印花税、涨跌停和高开过滤。"),
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


def ashare_backtest_defaults(params: dict[str, list[str]]) -> dict[str, float | int | str]:
    today = date.today()
    preset = field(params, "preset", "1y")
    hard_stop_pct = number_field(params, "hard_stop_pct", 20.0)
    if field(params, "hard_stop_pct", "").strip() in ("8", "8.0"):
        hard_stop_pct = 20.0
    return {
        "symbol": field(params, "symbol", "600487"),
        "preset": preset,
        "start": field(params, "start", start_for_preset(preset, today).isoformat()),
        "end": field(params, "end", today.isoformat()),
        "initial_cash": number_field(params, "initial_cash", 100000),
        "commission_pct": number_field(params, "commission_pct", 0.03),
        "stamp_duty_pct": number_field(params, "stamp_duty_pct", 0.05),
        "slippage_pct": number_field(params, "slippage_pct", 0.3),
        "max_buy_gap_pct": number_field(params, "max_buy_gap_pct", 6.0),
        "hard_stop_pct": hard_stop_pct,
        "stop_5ma_pct": number_field(params, "stop_5ma_pct", 7.5),
        "below_20ma_stop_days": int(number_field(params, "below_20ma_stop_days", 2)),
        "time_stop_days": 0,
        "vol_multiplier": number_field(params, "vol_multiplier", 1.45),
        "weak_trend_exit_mode": field(params, "weak_trend_exit_mode", "hybrid"),
        "weak_ma5_reclaim_days": int(number_field(params, "weak_ma5_reclaim_days", 5)),
        "weak_ma20_reclaim_days": int(number_field(params, "weak_ma20_reclaim_days", 10)),
        "weak_volume_down_multiplier": number_field(params, "weak_volume_down_multiplier", 1.5),
        "weak_event_low_lookback": int(number_field(params, "weak_event_low_lookback", 27)),
        "b1_require_20ma_gt_50ma": checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        "require_ma5_rising": checkbox_field(params, "require_ma5_rising", True),
        "require_5ma_gt_20ma": checkbox_field(params, "require_5ma_gt_20ma", True),
    }


def render_ashare_symbol_autocomplete() -> str:
    return """
<datalist id="ashare-symbol-suggestions"></datalist>
<script>
(function() {
  const list = document.getElementById("ashare-symbol-suggestions");
  if (!list || list.dataset.ready === "true") return;
  list.dataset.ready = "true";
  let timer = 0;
  let lastQuery = "";
  async function updateSuggestions(query) {
    const q = (query || "").trim();
    if (q.length < 1 || q === lastQuery) return;
    lastQuery = q;
    try {
      const res = await fetch(`/cn/suggest?q=${encodeURIComponent(q)}`);
      const payload = await res.json();
      list.innerHTML = "";
      for (const item of payload.suggestions || []) {
        const option = document.createElement("option");
        option.value = item.value;
        option.label = [item.name, item.sector, item.exchange].filter(Boolean).join(" / ");
        list.appendChild(option);
      }
    } catch (error) {
      list.innerHTML = "";
    }
  }
  document.addEventListener("input", event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.matches("[data-ashare-symbol-input]")) return;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => updateSuggestions(input.value), 160);
  });
})();
</script>
"""


def render_ashare_backtest_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    defaults = ashare_backtest_defaults(params)
    preset = str(defaults["preset"])
    display_preset = display_preset_for_dates(params, preset, date.today())

    def value(name: str, default: object) -> str:
        return html.escape(field(params, name, str(default)))

    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""

    b1_require_20ma_gt_50ma_checked = " checked" if bool(defaults["b1_require_20ma_gt_50ma"]) else ""
    require_ma5_rising_checked = " checked" if bool(defaults["require_ma5_rising"]) else ""
    require_5ma_gt_20ma_checked = " checked" if bool(defaults["require_5ma_gt_20ma"]) else ""
    secondary_big_red_checked = " checked" if checkbox_field(params, "secondary_big_red_b1", False) else ""
    secondary_above_ma5_checked = " checked" if checkbox_field(params, "secondary_above_ma5_3d", False) else ""

    return f"""
<section class="page-head">
  <div>
    <h1>A股回测</h1>
    <p class="hint">复用 MA5/B 点核心信号，并启用 A 股执行规则：涨停不买、跌停卖不出、高开过滤、卖出印花税和止损风控。</p>
  </div>
  <div class="mode-pill">A Share | MA5/B</div>
</section>
{render_ashare_condition_panel({key: [str(value)] for key, value in defaults.items()}, "backtest")}
<form class="form" action="/cn/run" method="get" id="ashare-backtest-form">
  <label>股票代码/名称<input name="symbol" value="{value("symbol", defaults["symbol"])}" placeholder="600487 或 亨通光电" list="ashare-symbol-suggestions" data-ashare-symbol-input></label>
  <label>回测周期
    <select name="preset" id="ashare-preset">
      <option value="6m"{selected(display_preset, "6m")}>近 6 个月</option>
      <option value="1y"{selected(display_preset, "1y")}>近 1 年</option>
      <option value="3y"{selected(display_preset, "3y")}>近 3 年</option>
      <option value="5y"{selected(display_preset, "5y")}>近 5 年</option>
      <option value="custom"{selected(display_preset, "custom")}>自定义</option>
    </select>
  </label>
  <label>开始日期<input type="date" name="start" id="ashare-start" value="{value("start", defaults["start"])}"></label>
  <label>结束日期<input type="date" name="end" id="ashare-end" value="{value("end", defaults["end"])}"></label>
  <label>初始资金<input name="initial_cash" value="{value("initial_cash", defaults["initial_cash"])}"></label>
  <label>手续费 %<input name="commission_pct" value="{value("commission_pct", defaults["commission_pct"])}"></label>
  <label>卖出印花税 %<input name="stamp_duty_pct" value="{value("stamp_duty_pct", defaults["stamp_duty_pct"])}"></label>
  <label>滑点 %<input name="slippage_pct" value="{value("slippage_pct", defaults["slippage_pct"])}"></label>
  <label>最高可追高开 %<input name="max_buy_gap_pct" value="{value("max_buy_gap_pct", defaults["max_buy_gap_pct"])}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", defaults["vol_multiplier"])}"></label>
  <label>跌破MA5止损 %<input name="stop_5ma_pct" value="{value("stop_5ma_pct", defaults["stop_5ma_pct"])}"></label>
  <label>连续跌破20MA天数<input name="below_20ma_stop_days" value="{value("below_20ma_stop_days", defaults["below_20ma_stop_days"])}"></label>
  <label>硬止损 %<input name="hard_stop_pct" value="{html.escape(str(defaults["hard_stop_pct"]))}"></label>
  <label>弱趋势卖出
    <select name="weak_trend_exit_mode">
      <option value="hybrid"{selected(str(defaults["weak_trend_exit_mode"]), "hybrid")}>混合模式：仅MA5&lt;MA20买入启用</option>
      <option value="off"{selected(str(defaults["weak_trend_exit_mode"]), "off")}>关闭：全部使用标准止损</option>
      <option value="weak"{selected(str(defaults["weak_trend_exit_mode"]), "weak")}>弱趋势持仓使用修复止损</option>
    </select>
  </label>
  <label>站回MA5期限<input name="weak_ma5_reclaim_days" value="{value("weak_ma5_reclaim_days", defaults["weak_ma5_reclaim_days"])}"></label>
  <label>站回MA20期限<input name="weak_ma20_reclaim_days" value="{value("weak_ma20_reclaim_days", defaults["weak_ma20_reclaim_days"])}"></label>
  <label>放量下跌倍数<input name="weak_volume_down_multiplier" value="{value("weak_volume_down_multiplier", defaults["weak_volume_down_multiplier"])}"></label>
  <label>事件低点窗口<input name="weak_event_low_lookback" value="{value("weak_event_low_lookback", defaults["weak_event_low_lookback"])}"></label>
  <div class="form-options">
    <span>可选买入条件</span>
    <input type="hidden" name="require_ma5_rising" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{require_ma5_rising_checked}> MA5向上</label>
    <input type="hidden" name="require_5ma_gt_20ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{require_5ma_gt_20ma_checked}> MA5&gt;MA20</label>
    <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{b1_require_20ma_gt_50ma_checked}> 20MA&gt;50MA</label>
    <input type="hidden" name="secondary_big_red_b1" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_big_red_b1" value="1"{secondary_big_red_checked}> 大阴线B1</label>
    <input type="hidden" name="secondary_above_ma5_3d" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_above_ma5_3d" value="1"{secondary_above_ma5_checked}> 连续三天&gt;MA5</label>
  </div>
  <button type="submit">运行 A 股回测</button>
</form>
{render_ashare_symbol_autocomplete()}
<script>
(function() {{
  const preset = document.getElementById("ashare-preset");
  const start = document.getElementById("ashare-start");
  const end = document.getElementById("ashare-end");
  const form = document.getElementById("ashare-backtest-form");
  if (!preset || !start || !end || !form) return;
  function isoDate(d) {{ return d.toISOString().slice(0, 10); }}
  function applyPreset() {{
    if (preset.value === "custom") return;
    const endDate = new Date(end.value || new Date());
    const startDate = new Date(endDate);
    if (preset.value === "6m") startDate.setMonth(startDate.getMonth() - 6);
    if (preset.value === "1y") startDate.setFullYear(startDate.getFullYear() - 1);
    if (preset.value === "3y") startDate.setFullYear(startDate.getFullYear() - 3);
    if (preset.value === "5y") startDate.setFullYear(startDate.getFullYear() - 5);
    start.value = isoDate(startDate);
  }}
  preset.addEventListener("change", applyPreset);
  start.addEventListener("change", () => {{ preset.value = "custom"; }});
  end.addEventListener("change", () => {{ preset.value = "custom"; }});
}})();
</script>
"""


def run_ashare_strategy(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    defaults = ashare_backtest_defaults(params)
    symbol = resolve_ashare_symbol_query(str(defaults["symbol"]))
    start = str(defaults["start"])
    end = str(defaults["end"])
    validate_backtest_range(start, end, 20)
    bars_raw, data_source = fetch_ashare_bars(symbol, start, end)
    bars = ashare_to_backtest_bars(bars_raw)
    initial_cash = float(defaults["initial_cash"])
    limit_pct = ashare_limit_pct(symbol)
    trades, equity_curve = backtest(
        bars=bars,
        ma_length=5,
        vol_length=20,
        vol_multiplier=float(defaults["vol_multiplier"]),
        initial_cash=initial_cash,
        commission_pct=float(defaults["commission_pct"]),
        slippage_pct=float(defaults["slippage_pct"]),
        strategy_name="ratchet",
        stop_5ma_pct=float(defaults["stop_5ma_pct"]),
        hard_stop_pct=float(defaults["hard_stop_pct"]),
        reentry_pct=4.5,
        vol_high_days=2,
        vol_high_multiplier=1.0,
        massive_window=7,
        massive_min_count=1,
        massive_max_count=2,
        b1_require_20ma_gt_50ma=bool(defaults["b1_require_20ma_gt_50ma"]),
        require_ma5_rising=bool(defaults["require_ma5_rising"]),
        require_5ma_gt_20ma=bool(defaults["require_5ma_gt_20ma"]),
        below_20ma_stop_days=int(defaults["below_20ma_stop_days"]),
        market="cn",
        limit_pct=limit_pct,
        max_buy_gap_pct=float(defaults["max_buy_gap_pct"]),
        stamp_duty_pct=float(defaults["stamp_duty_pct"]),
        time_stop_days=0,
        weak_trend_exit_mode=str(defaults["weak_trend_exit_mode"]),
        weak_ma5_reclaim_days=int(defaults["weak_ma5_reclaim_days"]),
        weak_ma20_reclaim_days=int(defaults["weak_ma20_reclaim_days"]),
        weak_volume_down_multiplier=float(defaults["weak_volume_down_multiplier"]),
        weak_event_low_lookback=int(defaults["weak_event_low_lookback"]),
    )
    summary = summarize(trades, equity_curve, initial_cash)
    buy_hold = build_buy_hold(symbol, bars, initial_cash)
    benchmark = {
        "symbol": f"{symbol} 买入持有",
        "return_pct": buy_hold["return_pct"],
        "curve": buy_hold["curve"],
        "buy_hold_symbol": symbol,
        "buy_hold_return_pct": buy_hold["return_pct"],
        "buy_hold_curve": buy_hold["curve"],
    }
    stem = safe_name(f"CN_{symbol}_ma5_{start}_{end}")
    report_path = REPORT_DIR / f"{stem}_report.html"
    trades_path = REPORT_DIR / f"{stem}_trades.csv"
    equity_path = REPORT_DIR / f"{stem}_equity.csv"
    strategy_settings = {
        "market": "A股",
        "data_source": data_source,
        "limit_pct": limit_pct,
        "max_buy_gap_pct": defaults["max_buy_gap_pct"],
        "stamp_duty_pct": defaults["stamp_duty_pct"],
        "below_20ma_stop_days": defaults["below_20ma_stop_days"],
        "vol_high_days": 2,
        "vol_multiplier": defaults["vol_multiplier"],
        "b1_require_20ma_gt_50ma": defaults["b1_require_20ma_gt_50ma"],
        "require_ma5_rising": defaults["require_ma5_rising"],
        "require_5ma_gt_20ma": defaults["require_5ma_gt_20ma"],
        "hard_stop_pct": defaults["hard_stop_pct"],
        "weak_trend_exit_mode": defaults["weak_trend_exit_mode"],
        "weak_ma5_reclaim_days": defaults["weak_ma5_reclaim_days"],
        "weak_ma20_reclaim_days": defaults["weak_ma20_reclaim_days"],
        "weak_volume_down_multiplier": defaults["weak_volume_down_multiplier"],
        "weak_event_low_lookback": defaults["weak_event_low_lookback"],
    }
    make_report(report_path, f"{symbol} A股 MA5/B 回测 {start} to {end}", bars, trades, equity_curve, summary, benchmark=benchmark, strategy_settings=strategy_settings)
    write_trades(trades_path, trades)
    write_equity(equity_path, equity_curve)
    report_url = f"/reports/{quote(report_path.name)}"
    trades_url = f"/reports/{quote(trades_path.name)}"
    equity_url = f"/reports/{quote(equity_path.name)}"
    return f"""
{render_ashare_backtest_form(params)}
<section class="result">
  <p class="hint">数据源：{html.escape(data_source)}；涨跌停阈值：{limit_pct:.0f}%；A 股执行规则已启用。</p>
  <p class="links">
    <a href="{report_url}" target="_blank">打开完整图表</a>
    <a href="{trades_url}" target="_blank">交易明细 CSV</a>
    <a href="{equity_url}" target="_blank">权益曲线 CSV</a>
  </p>
  {render_strategy_compare(summary, buy_hold, benchmark)}
  {render_backtest_trade_table(trades, equity_curve)}
  <iframe src="{report_url}" title="A Share backtest report"></iframe>
</section>
"""


OPTIONAL_RESULT_FILTERS = [
    ("require_ma5_rising", "MA5向上", "ma5_rising"),
    ("require_5ma_gt_20ma", "MA5&gt;MA20", "ma5_gt_20"),
    ("b1_require_20ma_gt_50ma", "20MA&gt;50MA", "ma20_gt_50"),
    ("secondary_big_red_b1", "大阴线B1", "big_red_b1"),
    ("secondary_above_ma5_3d", "连续三天&gt;MA5", "above_ma5_3d"),
]


def result_filter_attr(key: str) -> str:
    return f"data-filter-{key.replace('_', '-')}"


def result_filter_value(row: object, key: str) -> bool:
    for filter_key, _, attr_name in OPTIONAL_RESULT_FILTERS:
        if filter_key == key:
            return bool(getattr(row, attr_name, False))
    return False


def render_optional_condition_tags(row: object) -> str:
    tags = []
    for key, label, _ in OPTIONAL_RESULT_FILTERS:
        if result_filter_value(row, key):
            cls = "condition-primary" if key == "secondary_big_red_b1" else "condition-on"
            tags.append(f'<span class="condition-tag {cls}">{label}</span>')
    return "".join(tags) if tags else '<span class="condition-tag condition-off">-</span>'


def render_result_filter_panel(params: dict[str, list[str]], rows_count: int) -> str:
    facts = "\n".join(
        f'<span class="scan-fact"><span>{label}</span><b data-secondary-count="{html.escape(key)}">0</b></span>'
        for key, label, _ in OPTIONAL_RESULT_FILTERS
    )
    controls = []
    for key, label, _ in OPTIONAL_RESULT_FILTERS:
        if checkbox_field(params, key, False):
            controls.append(f'<span class="condition-tag condition-primary">{label} 已用于选股</span>')
        else:
            controls.append(f'<label class="check-inline"><input type="checkbox" data-secondary-filter="{html.escape(key)}"> {label}</label>')
    return f"""
<section class="result secondary-filter-panel" data-secondary-filter-panel>
  <div class="toolbar">
    <div>
      <h2>结果内筛选</h2>
      <p class="hint">只对当前选股结果继续过滤；已在开始选股时启用的条件会标为已用于选股，不再重复勾选。</p>
    </div>
    <div class="scan-facts">
      <span class="scan-fact"><span>全部</span><b data-secondary-total>{rows_count}</b></span>
      <span class="scan-fact"><span>当前</span><b data-secondary-visible>{rows_count}</b></span>
      {facts}
    </div>
  </div>
  <div class="checkbox-row">
    {"".join(controls)}
    <button type="button" class="secondary mini-action" data-secondary-clear>清空筛选</button>
  </div>
</section>
"""


def render_ashare_scan_result(
    candidates: list[AShareSignalSnapshot],
    errors: list[tuple[str, str]],
    scanned: int,
    universe_source: str,
    market_cap_filter_applied: bool,
    params: dict[str, list[str]] | None = None,
) -> str:
    params = params or {}
    rows = []
    rating_class = {"Strong": "score-Strong", "Medium": "score-Medium", "Watch": "score-Medium"}

    for row in candidates:
        cls = rating_class.get(row.candidate_rating, "score-Weak")
        filter_attrs = " ".join(
            f'{result_filter_attr(key)}="{1 if result_filter_value(row, key) else 0}"'
            for key, _, _ in OPTIONAL_RESULT_FILTERS
        )
        rows.append(
            f'<tr data-secondary-row {filter_attrs}>'
            f'<td><button type="button" class="symbol-button" data-ashare-candidate-symbol="{html.escape(row.symbol)}">{html.escape(row.symbol)}</button></td>'
            f"<td>{html.escape(row.name or '-')}</td>"
            f"<td><span class=\"score-badge {cls}\">{html.escape(row.candidate_rating)}</span></td>"
            f"<td>{html.escape(row.signal_type or '-')}</td>"
            f'<td><span class="condition-tags">{render_optional_condition_tags(row)}</span></td>'
            f"<td>{row.volume_score:.1f}/5</td>"
            f"<td>{row.volume_ratio:.2f}</td>"
            f"<td>{row.avg_amount_20d / 100_000_000:.2f}亿</td>"
            f"<td>{html.escape(row.limit_state or '正常')}</td>"
            f"<td>{html.escape(row.volume_context or '-')}</td>"
            f"<td>{row.close:.2f}</td>"
            f"<td>{html.escape(row.latest_date)}</td>"
            f"<td>{row.recent_peak_to_base:.2f}</td>"
            f"<td>{html.escape(row.data_source)}</td>"
            f'<td><a class="btn btn-secondary btn-small" href="/cn/watchlist/add?symbol={quote(row.symbol)}&name={quote(row.name or "")}">加入自选</a></td>'
            "</tr>"
        )
    table_html = (
        """
  <div class="table-wrap">
    <table class="sortable resizable-table" data-secondary-filter-table>
      <thead><tr><th>代码</th><th>名称</th><th>评级</th><th>B点</th><th>可选条件</th><th>量能分</th><th>量比</th><th>20日均成交额</th><th>涨跌停</th><th>量能语境</th><th>收盘</th><th>交易日</th><th>峰值/基准</th><th>数据源</th><th>操作</th></tr></thead>
      <tbody>
"""
        + "\n".join(rows)
        + """
      </tbody>
    </table>
  </div>
"""
        if rows
        else '<p class="hint">本次没有筛出候选。可以降低市值/扫描数量限制，或等待 MA5/B 点信号形成后再试。</p>'
    )
    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {reason[:80]}" for symbol, reason in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 只股票扫描失败：{html.escape(sample)}</p>{render_error_category_chips(errors)}{render_failure_table(errors)}'
    cap_note = "已按总市值过滤" if market_cap_filter_applied else "总市值接口不可用，本次按行情接口取前 N 只，市值过滤未生效"
    return f"""
<section class="result">
  <div class="toolbar">
    <div>
      <h2>A股选股结果</h2>
      <p class="hint">入选硬条件为 MA5/B 点信号 + 20日均成交额达标；量能分用于二次看图确认。股票池来源：{html.escape(universe_source)}。</p>
    </div>
  </div>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">扫描数量</div><div class="stat-value">{scanned}</div></div>
    <div class="stat-card"><div class="stat-label">候选数量</div><div class="stat-value">{len(candidates)}</div></div>
    <div class="stat-card"><div class="stat-label">市值过滤</div><div class="stat-value">{html.escape(cap_note)}</div></div>
    <div class="stat-card"><div class="stat-label">失败数量</div><div class="stat-value">{len(errors)}</div></div>
  </section>
  {render_result_filter_panel(params, len(candidates))}
  {table_html}
  {error_note}
  <section id="ashare-candidate-detail" class="candidate-detail"></section>
</section>
"""


def render_ashare_latest_banner() -> str:
    latest = load_latest_ashare_scan()
    if not latest:
        return ""
    summary = latest.get("summary", {}) if isinstance(latest.get("summary"), dict) else {}
    signal_date = str(latest.get("signal_date", "-"))
    saved_at = str(latest.get("saved_at", "-"))
    candidates = int(summary.get("candidates", 0) or 0)
    scanned = int(summary.get("scanned", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    return f"""
<section class="result latest-scan-card scan-result-alert">
  <div class="toolbar">
    <div>
      <h2>当前信号日已有 A 股扫描结果</h2>
      <p class="hint">信号日期：{html.escape(signal_date)}；候选：{candidates}；扫描：{scanned}；失败：{failed}；扫描时间：{html.escape(saved_at)}</p>
    </div>
    <div class="inline-actions links">
      <a href="/cn/scan/latest">查看当前结果</a>
      <form class="delete-form" action="/cn/scan/delete" method="get" onsubmit="return confirm('确认删除当前 A 股扫描结果？');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
</section>
"""


def latest_ashare_scan_to_html() -> str:
    latest = load_latest_ashare_scan()
    saved_params = {
        str(key): [str(value)]
        for key, value in ((latest or {}).get("params", {}) or {}).items()
        if not isinstance(value, list)
    }
    if latest:
        for key, value in ((latest or {}).get("params", {}) or {}).items():
            if isinstance(value, list):
                saved_params[str(key)] = [str(item) for item in value]
    saved_params["mode"] = [""]
    saved_params["_skip_latest_banner"] = ["1"]
    saved_params["_skip_restore_scan"] = ["1"]
    if not latest:
        return f"""
{render_ashare_scanner({"_skip_latest_banner": ["1"], "_skip_restore_scan": ["1"]})}
<section class="result"><p class="hint">当前还没有保存的 A 股扫描结果。</p></section>
"""
    candidates = [AShareSignalSnapshot(**row) for row in latest.get("candidates", []) if isinstance(row, dict)]
    errors = [(str(item.get("symbol", "")), str(item.get("reason", ""))) for item in latest.get("errors", []) if isinstance(item, dict)]
    source = str(latest.get("source", "saved"))
    market_cap_filter_applied = bool(latest.get("market_cap_filter_applied", False))
    scanned = int((latest.get("summary", {}) or {}).get("scanned", len(candidates)))
    result = render_ashare_scan_result(candidates, errors, scanned, source, market_cap_filter_applied, saved_params)
    return f"""
{render_ashare_scanner(saved_params)}
<section class="result">
  <div class="toolbar">
    <div>
      <h2>A股当前扫描结果</h2>
      <p class="hint">信号日期：{html.escape(str(latest.get("signal_date", "-")))}；保存时间：{html.escape(str(latest.get("saved_at", "-")))}。</p>
    </div>
    <div class="inline-actions links">
      <form class="delete-form" action="/cn/scan/delete" method="get" onsubmit="return confirm('确认删除当前 A 股扫描结果？');">
        <button type="submit" class="delete-link">删除结果</button>
      </form>
    </div>
  </div>
</section>
{result}
"""


def render_ashare_condition_panel(params: dict[str, list[str]], context: str = "scanner") -> str:
    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    selected_boards = normalize_ashare_boards(params.get("boards", []))
    require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
    secondary_big_red_b1 = checkbox_field(params, "secondary_big_red_b1", False)
    secondary_above_ma5_3d = checkbox_field(params, "secondary_above_ma5_3d", False)

    def toggle_tag(enabled: bool, label: str) -> str:
        cls = "condition-on" if enabled else "condition-off"
        state = "启用" if enabled else "关闭"
        return f'<span class="condition-tag {cls}">{label} {state}</span>'

    trend_tags = " ".join(
        [
            toggle_tag(require_ma5_rising, "MA5向上"),
            toggle_tag(require_5ma_gt_20ma, "MA5&gt;MA20"),
            toggle_tag(b1_require_20ma_gt_50ma, "20MA&gt;50MA"),
            toggle_tag(secondary_big_red_b1, "大阴线B1"),
            toggle_tag(secondary_above_ma5_3d, "连续三天&gt;MA5"),
        ]
    )
    b2_requirements = []
    if require_ma5_rising:
        b2_requirements.append("MA5向上")
    if require_5ma_gt_20ma:
        b2_requirements.append("MA5&gt;MA20")
    b2_filter_text = " + ".join(b2_requirements) if b2_requirements else "无额外均线过滤"
    risk_note = ""
    if not require_5ma_gt_20ma:
        risk_note = '<div class="risk-note"><strong>防误买提醒</strong><span>当前关闭 MA5&gt;MA20，选股器会允许 MA5 低于 MA20 的候选进入。要避免买入后马上触发 20MA 趋势止损，把“买入要求MA5&gt;MA20”打开；回测里同名条件默认已打开，适合做对照。</span></div>'
    title = "当前 A股回测条件" if context == "backtest" else "当前 A股选股条件"
    note = "本页用同一套 MA5/B 点买入条件和 A股执行/止损规则回测单只股票。" if context == "backtest" else "扫描最后一根已完成日 K；结果用于盘后复盘和二次看图确认。"
    panel_key = f"cn-{context}-conditions"
    return f"""
<section class="result strategy-condition-panel" data-condition-panel data-panel-key="{html.escape(panel_key)}">
  <div class="strategy-condition-head">
    <div>
      <strong>{html.escape(title)}</strong>
      <p class="hint">{html.escape(note)}</p>
    </div>
    <div class="condition-head-actions">
      <span class="condition-tag condition-primary">A Share | Daily Close</span>
      <button type="button" class="condition-panel-toggle" data-condition-collapse aria-label="折叠或展开A股条件">折叠</button>
      <button type="button" class="condition-panel-toggle" data-condition-pin aria-label="固定或取消固定A股条件">固定</button>
    </div>
  </div>
  <div class="condition-panel-body">
    {risk_note}
    <div class="condition-grid">
      <div class="condition-card">
        <h3>B点趋势</h3>
        <ul class="condition-list">
          <li><b>B1</b><span>收盘站上 MA5</span></li>
          <li><b>B2</b><span>已有 B1 趋势后回踩 MA5 ≤ {value("reentry_pct", "4.5")}%</span></li>
          <li><b>趋势过滤</b><span class="condition-tags">{trend_tags}</span></li>
          <li><b>B2过滤</b><span>{b2_filter_text}</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>量能结构</h3>
        <ul class="condition-list">
          <li><b>连续放量</b><span>{value("vol_high_days", "2")} 日 &gt; 均量×{value("vol_high_multiplier", "1.0")}</span></li>
          <li><b>巨量窗口</b><span>{value("massive_window", "7")} 日内 ≥ {value("massive_min_count", "1")} 次 ×{value("vol_multiplier", "1.45")}</span></li>
          <li><b>红长绿短</b><span>峰值/基准 + 红均/绿均 + Top5红柱</span></li>
          <li><b>评级阈值</b><span>Strong ≥ {value("strong_volume_score", "4.0")} / Medium ≥ {value("medium_volume_score", "2.5")}</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>股票池过滤</h3>
        <ul class="condition-list">
          <li><b>板块范围</b><span>{html.escape(ashare_board_filter_label(selected_boards))}</span></li>
          <li><b>最低市值</b><span>{value("min_market_cap", "50")} 亿元</span></li>
          <li><b>最多扫描</b><span>{value("max_symbols", str(ASHARE_DEFAULT_MAX_SCAN_SYMBOLS))} 只</span></li>
          <li><b>基础排除</b><span>剔除 ST / 退市 / 停牌不可用标的</span></li>
        </ul>
      </div>
      <div class="condition-card">
        <h3>执行与风控</h3>
        <ul class="condition-list">
          <li><b>成交额</b><span>20日均成交额 ≥ {value("min_avg_amount_20d_100m", "1.0")} 亿元</span></li>
          <li><b>低流动性提示</b><span>&lt; {value("min_control_amount_20d_100m", "2.0")} 亿元降权看待</span></li>
          <li><b>涨跌停</b><span>一字板剔除量能；普通涨跌停量能半权重</span></li>
          <li><b>弱趋势卖出</b><span>{value("weak_trend_exit_mode", "hybrid")} / MA5 {value("weak_ma5_reclaim_days", "5")}日 / MA20 {value("weak_ma20_reclaim_days", "10")}日</span></li>
        </ul>
      </div>
    </div>
  </div>
</section>
"""


def render_ashare_scanner(params: dict[str, list[str]]) -> str:
    mode = field(params, "mode", "")
    symbol = field(params, "symbol", "")
    j_threshold = number_field(params, "j_threshold", 14.0)
    min_market_cap = number_field(params, "min_market_cap", 50.0)
    max_symbols = int(number_field(params, "max_symbols", ASHARE_DEFAULT_MAX_SCAN_SYMBOLS))
    min_avg_amount_20d_100m = number_field(params, "min_avg_amount_20d_100m", 1.0)
    min_control_amount_20d_100m = number_field(params, "min_control_amount_20d_100m", 2.0)
    vol_high_days = int(number_field(params, "vol_high_days", 2))
    vol_high_multiplier = number_field(params, "vol_high_multiplier", 1.0)
    vol_multiplier = number_field(params, "vol_multiplier", 1.45)
    massive_window = int(number_field(params, "massive_window", 7))
    massive_min_count = int(number_field(params, "massive_min_count", 1))
    reentry_pct = number_field(params, "reentry_pct", 4.5)
    strong_volume_score = number_field(params, "strong_volume_score", 4.0)
    medium_volume_score = number_field(params, "medium_volume_score", 2.5)
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
    require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
    secondary_big_red_b1 = checkbox_field(params, "secondary_big_red_b1", False)
    secondary_above_ma5_3d = checkbox_field(params, "secondary_above_ma5_3d", False)
    selected_boards = normalize_ashare_boards(params.get("boards", []))
    embed = field(params, "embed", "0") == "1"
    show_latest_banner = field(params, "_skip_latest_banner", "0") != "1"
    restore_scan = field(params, "_skip_restore_scan", "0") != "1"
    result_html = ""
    if mode == "market":
        try:
            scan = scan_ashare_candidates(
                min_market_cap,
                max_symbols,
                j_threshold,
                boards=selected_boards,
                min_avg_amount_20d=min_avg_amount_20d_100m * 100_000_000,
                min_control_amount_20d=min_control_amount_20d_100m * 100_000_000,
                vol_multiplier=vol_multiplier,
                vol_high_days=vol_high_days,
                vol_high_multiplier=vol_high_multiplier,
                massive_window=massive_window,
                massive_min_count=massive_min_count,
                reentry_pct=reentry_pct / 100,
                strong_volume_score=strong_volume_score,
                medium_volume_score=medium_volume_score,
                b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
                require_ma5_rising=require_ma5_rising,
                require_5ma_gt_20ma=require_5ma_gt_20ma,
            )
            result_html = render_ashare_scan_result(
                scan.candidates,
                scan.errors,
                scan.scanned,
                scan.universe_source,
                scan.market_cap_filter_applied,
                params,
            )
        except Exception as exc:
            result_html = f'<div class="error">{html.escape(str(exc))}</div>'
    elif mode == "single" and symbol.strip():
        try:
            resolved_symbol = resolve_ashare_symbol_query(symbol)
            snapshot = latest_ashare_signal(
                resolved_symbol,
                j_threshold,
                min_avg_amount_20d=min_avg_amount_20d_100m * 100_000_000,
                min_control_amount_20d=min_control_amount_20d_100m * 100_000_000,
                vol_multiplier=vol_multiplier,
                vol_high_days=vol_high_days,
                vol_high_multiplier=vol_high_multiplier,
                massive_window=massive_window,
                massive_min_count=massive_min_count,
                reentry_pct=reentry_pct / 100,
                strong_volume_score=strong_volume_score,
                medium_volume_score=medium_volume_score,
                b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
                require_ma5_rising=require_ma5_rising,
                require_5ma_gt_20ma=require_5ma_gt_20ma,
            )
            chart_payload = ashare_chart_payload(
                resolved_symbol,
                j_threshold,
                b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
                require_ma5_rising=require_ma5_rising,
                require_5ma_gt_20ma=require_5ma_gt_20ma,
            )

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
      <p class="hint">单票验证使用 MA5/B 点框架，并叠加 A 股成交额、涨跌停和次日执行约束。</p>
    </div>
    <div class="quick-actions">
      <span class="score-badge {rating_cls}">{signal_text}</span>
      <form action="/cn/watchlist/add" method="get" style="margin:0;display:inline-flex;gap:8px;align-items:center;">
        <input type="hidden" name="symbol" value="{html.escape(snapshot.symbol)}">
        <input type="hidden" name="name" value="{html.escape(snapshot.name)}">
        <input type="hidden" name="sector" value="{html.escape(sector)}">
        <input type="hidden" name="group" value="观察">
        <input type="hidden" name="note" value="单票验证加入">
        <button class="btn btn-secondary btn-small" type="submit">加入自选池</button>
      </form>
      <a class="btn btn-secondary btn-small" href="/cn/watchlist">查看自选池</a>
    </div>
  </div>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">最新交易日</div><div class="stat-value">{html.escape(snapshot.latest_date)}</div></div>
    <div class="stat-card"><div class="stat-label">收盘价</div><div class="stat-value">{snapshot.close:.2f}</div></div>
    <div class="stat-card"><div class="stat-label">B点 / 量能</div><div class="stat-value">{html.escape(snapshot.signal_type or '-')} / {snapshot.volume_score:.1f}</div></div>
    <div class="stat-card"><div class="stat-label">板块</div><div class="stat-value">{html.escape(sector)}</div></div>
  </section>
  <p class="hint">数据源 / 日K：{html.escape(snapshot.data_source)} / {snapshot.bars_count}</p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>条件</th><th>结果</th><th>关键数值</th><th>说明</th></tr></thead>
      <tbody>
        <tr>
          <td>B点触发</td>
          <td>{status_badge(snapshot.signal)}</td>
          <td>B点类型 {html.escape(snapshot.signal_type or '-')} / 候选评级 {html.escape(signal_text)}</td>
          <td>单票验证不再使用旧 J 值条件；是否入选首先看 MA5/B1-B2 信号是否触发。</td>
        </tr>
        <tr>
          <td>MA5/B点趋势</td>
          <td>{status_badge(snapshot.trend_ok)}</td>
          <td>MA5 {snapshot.ma5:.2f} / MA20 {snapshot.ma20:.2f} / 斜率 {snapshot.zx_multi_slope:.2f}</td>
          <td>复用美股 MA5/B 点核心：收盘在 MA5 上方，MA5 上行，MA5 高于 MA20，并处于转强结构。</td>
        </tr>
        <tr>
          <td>成交额过滤</td>
          <td>{status_badge(snapshot.amount_ok)}</td>
          <td>20日均成交额 {snapshot.avg_amount_20d / 100_000_000:.2f} 亿 / 量比 {snapshot.volume_ratio:.2f}</td>
          <td>A 股优先过滤流动性，默认要求 20 日均成交额不低于 1 亿；量比已按涨跌停语境调整。</td>
        </tr>
        <tr>
          <td>涨跌停量能</td>
          <td><span class="score-badge score-Medium">{html.escape(snapshot.limit_state or '正常')}</span></td>
          <td>{html.escape(snapshot.volume_context or '-')}</td>
          <td>一字涨跌停剔除成交量，普通涨跌停按 50% 权重计入量能评分。</td>
        </tr>
        <tr>
          <td>红长绿短量能</td>
          <td><span class="score-badge {rating_cls}">{snapshot.volume_score:.1f}/5</span></td>
          <td>峰值/基准 {snapshot.recent_peak_to_base:.2f}，10日均量/基准 {snapshot.recent_avg10_to_base:.2f}，红均/绿均 {snapshot.red_avg_to_green_avg:.2f}</td>
          <td>该项用于二次看图确认强弱，不再作为入选硬条件。</td>
        </tr>
        <tr>
          <td>Top5 红柱</td>
          <td><span class="score-badge score-Medium">{snapshot.top5_red_count}/5</span></td>
          <td>上涨日 {snapshot.red_days} 天 / 下跌日 {snapshot.green_days} 天</td>
          <td>近 20 日成交量最大的 5 天里，红柱越多，说明大成交更偏向主动买入。</td>
        </tr>
        <tr>
          <td>候选评级</td>
          <td><span class="score-badge {rating_cls}">{html.escape(signal_text)}</span></td>
          <td>Strong: 硬条件通过且量能分 >= 4；Medium: >= 2.5；Watch: 硬条件通过但量能不足。</td>
          <td>信号日接近涨停不会给 Strong，避免把买不到的板当作强买点。</td>
        </tr>
        <tr>
          <td>次日执行</td>
          <td><span class="score-badge score-Medium">A股规则</span></td>
          <td>{html.escape(snapshot.execution_note)}</td>
          <td>次日接近涨停不买，高开超过阈值跳过；卖出遇跌停则顺延。</td>
        </tr>
        <tr>
          <td>卖出风控</td>
          <td><span class="score-badge score-Medium">回测执行</span></td>
          <td>跌破MA5阈值 / 连续跌破20MA / 跌破成本</td>
          <td>这些卖出条件在 A 股回测中执行，选股器单票页主要展示买入候选和次日可成交性。</td>
        </tr>
        <tr>
          <td>图表口径</td>
          <td><span class="score-badge score-Strong">新版</span></td>
          <td>主图: K线 + MA5 + MA20 + 成交量 + B1/B2；副图: KDJ。</td>
          <td>A 股单票图表按美股图表结构展示，策略仍使用 MA5/B 点口径。</td>
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
  <p class="hint">执行提示：{html.escape(snapshot.execution_note)}</p>
  <section class="result">
    <div class="toolbar">
      <div>
        <h2>策略图表</h2>
        <p class="hint">主图显示 K 线、MA5/MA20、成交量与 B1/B2 标记；下方显示 KDJ。</p>
      </div>
    </div>
    <div class="watchlist-chart-shell" style="height:780px;">
      <div id="ashare-main-chart" class="watchlist-chart" style="height:560px;"></div>
      <div id="ashare-kdj-chart" class="watchlist-chart" style="height:200px;margin-top:12px;"></div>
      <div id="ashare-tooltip" class="chart-tooltip"></div>
    </div>
  </section>
  <script type="application/json" id="ashare-chart-data">{json.dumps(chart_payload, ensure_ascii=False)}</script>
  <script>
  (function() {{
    const payload = JSON.parse(document.getElementById("ashare-chart-data").textContent);
    const ohlc = payload.ohlc || [];
    const volume = payload.volume || [];
    const mainEl = document.getElementById("ashare-main-chart");
    const kdjEl = document.getElementById("ashare-kdj-chart");
    const tooltip = document.getElementById("ashare-tooltip");
    const toLine = rows => (rows || []).map(row => ({{ time: row.x, value: row.y }})).filter(row => row.value !== null && row.value !== undefined);
    const candleRows = ohlc.map(row => ({{ time: row.x, open: row.open, high: row.high, low: row.low, close: row.close }}));
    const volumeRows = volume.map(row => ({{ time: row.x, value: row.y, color: row.color }}));
    const rowByTime = new Map(candleRows.map((row, index) => [row.time, {{ ...row, volume: volume[index]?.y }}]));
    const commonOptions = {{
      layout: {{ background: {{ type: "solid", color: "#ffffff" }}, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif" }},
      grid: {{ vertLines: {{ color: "#f1f3f6" }}, horzLines: {{ color: "#f1f3f6" }} }},
      crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
      timeScale: {{ borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }},
      handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
      handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
    }};
    const mainChart = LightweightCharts.createChart(mainEl, {{
      ...commonOptions,
      width: mainEl.clientWidth,
      height: mainEl.clientHeight,
      rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.08, bottom: 0.28 }} }},
    }});
    const kdjChart = LightweightCharts.createChart(kdjEl, {{
      ...commonOptions,
      width: kdjEl.clientWidth,
      height: kdjEl.clientHeight,
      rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.12, bottom: 0.12 }} }},
    }});
    const candle = mainChart.addCandlestickSeries({{
      upColor: "#089981", downColor: "#f23645", borderUpColor: "#089981", borderDownColor: "#f23645", wickUpColor: "#089981", wickDownColor: "#f23645", priceLineVisible: false,
    }});
    candle.setData(candleRows);
    const shortTrend = mainChart.addLineSeries({{ color: "#2563eb", lineWidth: 2, title: "MA5", priceLineVisible: false }});
    shortTrend.setData(toLine(payload.ma5 || payload.zx_short_trend));
    const multiTrend = mainChart.addLineSeries({{ color: "#dc2626", lineWidth: 2, title: "MA20", priceLineVisible: false }});
    multiTrend.setData(toLine(payload.ma20 || payload.zx_multi_trend));
    const volSeries = mainChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
    volSeries.setData(volumeRows);
    const volMaSeries = mainChart.addLineSeries({{ color: "#475569", lineWidth: 1, priceScaleId: "", title: "Vol MA20", priceLineVisible: false, lastValueVisible: false }});
    volMaSeries.setData(toLine(payload.volume_ma20));
    mainChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
    candle.setMarkers((payload.signals || []).map(row => ({{
      time: row.x,
      position: "belowBar",
      color: "#16a34a",
      shape: "arrowUp",
      text: row.text || "B",
    }})));
    const kLine = kdjChart.addLineSeries({{ color: "#2563eb", lineWidth: 1.5, title: "K", priceLineVisible: false }});
    kLine.setData(toLine(payload.k));
    const dLine = kdjChart.addLineSeries({{ color: "#f59e0b", lineWidth: 1.5, title: "D", priceLineVisible: false }});
    dLine.setData(toLine(payload.d));
    const jLine = kdjChart.addLineSeries({{ color: "#7c3aed", lineWidth: 2, title: "J", priceLineVisible: false }});
    jLine.setData(toLine(payload.j));
    const kdjDates = ohlc.map(row => row.x);
    kdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "80", priceLineVisible: false, lastValueVisible: false }}).setData(kdjDates.map(time => ({{ time, value: 80 }})));
    kdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "20", priceLineVisible: false, lastValueVisible: false }}).setData(kdjDates.map(time => ({{ time, value: 20 }})));
    let syncing = false;
    function syncRange(source, target) {{
      source.timeScale().subscribeVisibleLogicalRangeChange(range => {{
        if (syncing || !range) return;
        syncing = true;
        target.timeScale().setVisibleLogicalRange(range);
        syncing = false;
      }});
    }}
    syncRange(mainChart, kdjChart);
    syncRange(kdjChart, mainChart);
    function formatNum(value, digits = 2) {{
      return value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ minimumFractionDigits: digits, maximumFractionDigits: digits }});
    }}
    function formatVol(value) {{
      return value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
    }}
    mainChart.subscribeCrosshairMove(param => {{
      if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0 || param.point.x > mainEl.clientWidth || param.point.y > mainEl.clientHeight) {{
        tooltip.style.display = "none";
        return;
      }}
      const row = rowByTime.get(param.time);
      if (!row) {{
        tooltip.style.display = "none";
        return;
      }}
      const up = row.close >= row.open;
      tooltip.innerHTML = `<strong>${{row.time}}</strong><div><span class="${{up ? "up" : "down"}}">开 ${{formatNum(row.open)}} 高 ${{formatNum(row.high)}} 低 ${{formatNum(row.low)}} 收 ${{formatNum(row.close)}}</span></div><div>成交量 ${{formatVol(row.volume)}} &nbsp; 量MA20 ${{formatVol(param.seriesData.get(volMaSeries)?.value)}} &nbsp; MA5 ${{formatNum(param.seriesData.get(shortTrend)?.value)}} &nbsp; MA20 ${{formatNum(param.seriesData.get(multiTrend)?.value)}}</div>`;
      tooltip.style.display = "block";
      tooltip.style.left = Math.min(param.point.x + 16, mainEl.clientWidth - 280) + "px";
      tooltip.style.top = Math.max(44, param.point.y - 72) + "px";
    }});
    new ResizeObserver(entries => {{
      const rect = entries[0].contentRect;
      mainChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
    }}).observe(mainEl);
    new ResizeObserver(entries => {{
      const rect = entries[0].contentRect;
      kdjChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
    }}).observe(kdjEl);
    mainChart.timeScale().fitContent();
    kdjChart.timeScale().fitContent();
  }})();
  </script>
</section>
"""
        except Exception as exc:
            result_html = f'<div class="error">{html.escape(str(exc))}</div>'

    if embed:
        return f"""
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
{result_html or '<div class="empty">请选择一只 A 股查看策略图表。</div>'}
"""

    board_controls = "".join(
        f'<label class="check-inline"><input type="checkbox" name="boards" value="{html.escape(board)}" {"checked" if board in selected_boards else ""}> {html.escape(label)}</label>'
        for board, label in ASHARE_BOARD_LABELS.items()
    )

    return f"""
<section class="page-head">
  <div>
    <h1>A股选股器</h1>
    <p class="hint">MA5/B 点信号 + 20日均成交额作为候选硬条件，红长绿短量能用于二次看图确认强弱。首次进入不拉行情，点击按钮后才开始请求数据。</p>
  </div>
  <div class="mode-pill">A Share | Scanner</div>
</section>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
{render_ashare_latest_banner() if show_latest_banner else ""}
{render_data_health_panel("cn")}
{render_ashare_condition_panel(params)}
<form class="form" action="/cn/scanner" method="get">
  <input type="hidden" name="mode" value="single">
  <input type="hidden" name="j_threshold" value="{j_threshold:g}">
  <label>股票代码/名称<input name="symbol" value="{html.escape(symbol)}" placeholder="600487 或 亨通光电" list="ashare-symbol-suggestions" data-ashare-symbol-input></label>
  <button type="submit">单票验证</button>
</form>
{render_ashare_symbol_autocomplete()}
<form class="form" id="ashare-scanner-form" action="/cn/scanner" method="get" data-async-submit="true">
  <input type="hidden" name="mode" value="market">
  <input type="hidden" name="j_threshold" value="{j_threshold:g}">
  <label>最低市值（亿元）<input type="number" step="1" name="min_market_cap" value="{min_market_cap:g}"></label>
  <label>最多扫描<input type="number" step="1" name="max_symbols" value="{max_symbols}"></label>
  <label>并发数<input type="number" step="1" name="max_workers" value="{int(number_field(params, "max_workers", 6))}"></label>
  <label>20日均成交额（亿元）<input type="number" step="0.1" name="min_avg_amount_20d_100m" value="{min_avg_amount_20d_100m:g}"></label>
  <label>低流动性提示（亿元）<input type="number" step="0.1" name="min_control_amount_20d_100m" value="{min_control_amount_20d_100m:g}"></label>
  <label>连续放量天数<input type="number" step="1" name="vol_high_days" value="{vol_high_days}"></label>
  <label>连续放量倍数<input type="number" step="0.1" name="vol_high_multiplier" value="{vol_high_multiplier:g}"></label>
  <label>巨量倍数<input type="number" step="0.1" name="vol_multiplier" value="{vol_multiplier:g}"></label>
  <label>巨量观察窗口<input type="number" step="1" name="massive_window" value="{massive_window}"></label>
  <label>巨量最少次数<input type="number" step="1" name="massive_min_count" value="{massive_min_count}"></label>
  <label>B2回踩距离 %<input type="number" step="0.1" name="reentry_pct" value="{reentry_pct:g}"></label>
  <label>Strong量能分<input type="number" step="0.1" name="strong_volume_score" value="{strong_volume_score:g}"></label>
  <label>Medium量能分<input type="number" step="0.1" name="medium_volume_score" value="{medium_volume_score:g}"></label>
  <input type="hidden" name="require_ma5_rising" value="0">
  <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{" checked" if require_ma5_rising else ""}> 买入要求MA5向上</label>
  <input type="hidden" name="require_5ma_gt_20ma" value="0">
  <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{" checked" if require_5ma_gt_20ma else ""}> 买入要求MA5&gt;MA20</label>
  <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
  <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{" checked" if b1_require_20ma_gt_50ma else ""}> B1要求20MA&gt;50MA</label>
  <input type="hidden" name="secondary_big_red_b1" value="0">
  <label class="checkbox-label"><input type="checkbox" name="secondary_big_red_b1" value="1"{" checked" if secondary_big_red_b1 else ""}> 大阴线B1</label>
  <input type="hidden" name="secondary_above_ma5_3d" value="0">
  <label class="checkbox-label"><input type="checkbox" name="secondary_above_ma5_3d" value="1"{" checked" if secondary_above_ma5_3d else ""}> 连续三天&gt;MA5</label>
  <label class="wide">板块范围<div class="checkbox-row">{board_controls}</div></label>
  <button type="submit">开始选股</button>
</form>
<section class="progress-box" id="ashare-scan-progress">
  <div class="progress-meta" id="ashare-scan-status">准备开始</div>
  <div class="progress-track"><div class="progress-bar" id="ashare-scan-bar"></div></div>
  <div class="progress-meta" id="ashare-scan-detail"></div>
  <div class="quick-actions" style="margin-top:10px;">
    <button type="button" class="btn btn-secondary btn-small" id="ashare-scan-stop" disabled>终止选股</button>
  </div>
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
  const stopButton = document.getElementById("ashare-scan-stop");
  let jobId = "";
  async function setHtmlAndRunScripts(container, html) {{
    container.innerHTML = html;
    const scripts = Array.from(container.querySelectorAll("script"));
    for (const oldScript of scripts) {{
      const src = oldScript.getAttribute("src") || "";
      if (src.includes("lightweight-charts") && window.LightweightCharts) {{
        oldScript.remove();
        continue;
      }}
      const script = document.createElement("script");
      for (const attr of oldScript.attributes) script.setAttribute(attr.name, attr.value);
      if (src) {{
        await new Promise((resolve, reject) => {{
          script.onload = resolve;
          script.onerror = reject;
          oldScript.replaceWith(script);
        }});
      }} else {{
        script.textContent = oldScript.textContent;
        oldScript.replaceWith(script);
      }}
    }}
  }}
  function update(job) {{
    const total = Number(job.total || 0);
    const scanned = Number(job.scanned || 0);
    const finished = ["done", "stopped"].includes(job.status);
    const percent = finished ? 100 : total > 0 ? Math.round(scanned / total * 100) : (["queued", "running", "stopping"].includes(job.status) ? 8 : 0);
    progressBar.style.width = percent + "%";
    const stage = job.stage || "处理中";
    const source = job.data_source ? `｜数据源：${{job.data_source}}` : "";
    const current = job.current ? `｜当前：${{job.current}}` : "";
    const extra = job.detail ? `｜${{job.detail}}` : "";
    status.textContent = `${{stage}}：${{job.message || "正在处理"}}`;
    detail.textContent = `进度 ${{scanned}} / ${{total || "-"}}｜候选 ${{job.candidates || 0}}｜失败 ${{job.errors || 0}}${{current}}${{source}}${{extra}}`;
    stopButton.disabled = !jobId || ["done", "error", "stopped"].includes(job.status);
    const submitButton = form.querySelector("button[type='submit']");
    if (submitButton && ["done", "error", "stopped"].includes(job.status)) {{
      submitButton.disabled = false;
      submitButton.classList.remove("btn-loading");
      submitButton.textContent = "开始选股";
    }}
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
      if (window.initializeSecondaryFilters) initializeSecondaryFilters(result);
    }}
    if (["done", "error", "stopped"].includes(job.status)) {{
      if (job.status === "error") result.innerHTML = `<div class="error">${{job.error || "扫描失败"}}</div>`;
      jobId = "";
      stopButton.disabled = true;
      return;
    }}
    setTimeout(poll, 800);
  }}
  async function restoreActiveScan() {{
    try {{
      const res = await fetch("/cn/scan/active");
      const job = await res.json();
      if (!job.job_id || job.status === "idle") return;
      jobId = job.job_id;
      progressBox.classList.add("active");
      update(job);
      if (job.result_html) {{
        result.innerHTML = job.result_html;
        if (window.initializeResizableTables) initializeResizableTables(result);
        if (window.initializeSortableTables) initializeSortableTables(result);
        if (window.initializeSecondaryFilters) initializeSecondaryFilters(result);
      }}
      if (!["done", "error", "stopped"].includes(job.status)) poll();
    }} catch (error) {{}}
  }}
  stopButton.addEventListener("click", async () => {{
    if (!jobId) return;
    stopButton.disabled = true;
    status.textContent = "正在终止 A 股扫描";
    await fetch(`/cn/scan/stop?job_id=${{encodeURIComponent(jobId)}}`);
    poll();
  }});
  form.addEventListener("submit", async event => {{
    event.preventDefault();
    result.innerHTML = "";
    progressBox.classList.add("active");
    progressBar.style.width = "0%";
    status.textContent = "正在启动 A 股扫描";
    detail.textContent = "";
    const submitButton = form.querySelector("button[type='submit']");
    if (submitButton) {{
      submitButton.disabled = true;
      submitButton.classList.add("btn-loading");
      submitButton.textContent = "选股中";
    }}
    const params = new URLSearchParams(new FormData(form));
    const res = await fetch(`/cn/scan/start?${{params.toString()}}`);
    const data = await res.json();
    if (data.status === "error") {{
      result.innerHTML = `<div class="error">${{data.error || "无法启动扫描"}}</div>`;
      stopButton.disabled = true;
      if (submitButton) {{
        submitButton.disabled = false;
        submitButton.classList.remove("btn-loading");
        submitButton.textContent = "开始选股";
      }}
      return;
    }}
    jobId = data.job_id;
    stopButton.disabled = false;
    poll();
  }});
  document.addEventListener("click", async event => {{
    const button = event.target.closest("[data-ashare-candidate-symbol]");
    if (!button) return;
    const symbol = button.dataset.ashareCandidateSymbol;
    const host = document.getElementById("ashare-candidate-detail");
    if (!symbol || !host) return;
    host.innerHTML = `<section class="result"><div class="loading-overlay active" style="position:relative; min-height:120px;"><div class="spinner"></div><div>正在加载 ${{symbol}} 策略图表</div></div></section>`;
    const res = await fetch(`/cn/scanner?mode=single&symbol=${{encodeURIComponent(symbol)}}&j_threshold=14&embed=1`);
    await setHtmlAndRunScripts(host, await res.text());
    host.scrollIntoView({{ behavior: "smooth", block: "start" }});
  }});
  {"restoreActiveScan();" if restore_scan else ""}
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
        rows.append(
            "<tr>"
            f"<td><a class=\"symbol-button\" href=\"#\" data-ashare-symbol=\"{html.escape(symbol)}\">{html.escape(symbol)}</a></td>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(sector)}</td>"
            f"<td>{html.escape(group)}</td>"
            f"<td>{html.escape(note)}</td>"
            f"<td>{html.escape(added_at)}</td>"
            f"<td><button class=\"btn btn-secondary btn-small\" type=\"button\" data-ashare-symbol=\"{html.escape(symbol)}\">看图</button> "
            f"<a class=\"delete-link\" href=\"/cn/watchlist/delete?symbol={quote(symbol)}\" onclick=\"return confirm('确认删除 {html.escape(symbol)}？');\">删除</a></td>"
            "</tr>"
        )
    table_rows = "\n".join(rows) if rows else '<tr><td colspan="7" class="empty">暂无 A 股自选。可以先添加代码，或从 A 股选股结果加入。</td></tr>'
    default_symbol = items[0]["symbol"] if items else ""
    return f"""
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<section class="page-head">
  <div>
    <h1>A股自选池</h1>
    <p class="hint">A 股自选池和美股自选池分开保存。点击代码或“看图”后，右侧显示 A 股策略图表。</p>
  </div>
  <div class="mode-pill">A Share | Watchlist</div>
</section>
<form class="form" action="/cn/watchlist/add" method="get">
  <label>股票代码/名称<input name="symbol" value="{html.escape(field(params, "symbol", ""))}" placeholder="600487 或 亨通光电" list="ashare-symbol-suggestions" data-ashare-symbol-input></label>
  <label>分组<input name="group" value="{html.escape(field(params, "group", "观察"))}" placeholder="观察 / 候选 / 持仓"></label>
  <label class="wide">备注<input name="note" value="{html.escape(field(params, "note", ""))}" placeholder="关注原因、板块、阻力位等"></label>
  <button type="submit">添加到自选</button>
</form>
{render_ashare_symbol_autocomplete()}
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
    <div class="toolbar">
      <div>
        <strong id="ashare-watch-title">{html.escape(default_symbol) if default_symbol else "选择一只 A 股"}</strong>
        <p class="hint" id="ashare-watch-subtitle">点击左侧代码或“看图”后，在这里显示策略图表。</p>
      </div>
    </div>
    <div class="watch-detail-grid">
      <div class="watch-detail-item"><span>名称</span><strong id="ashare-detail-name">-</strong></div>
      <div class="watch-detail-item"><span>板块</span><strong id="ashare-detail-sector">-</strong></div>
      <div class="watch-detail-item"><span>数据源</span><strong id="ashare-detail-source">-</strong></div>
      <div class="watch-detail-item"><span>交易日数量</span><strong id="ashare-detail-count">-</strong></div>
    </div>
    <div class="watchlist-chart-shell" style="height:780px;">
      <div id="ashare-watch-main-chart" class="watchlist-chart" style="height:560px;"></div>
      <div id="ashare-watch-kdj-chart" class="watchlist-chart" style="height:200px;margin-top:12px;"></div>
      <div id="ashare-watch-tooltip" class="chart-tooltip"></div>
      <div id="ashare-watch-loading" class="loading-overlay"><div class="spinner"></div><div>正在拉取 A 股日 K 数据</div></div>
    </div>
  </div>
</section>
<script>
const ashareInitialSymbol = {json.dumps(default_symbol)};
let ashareMainChart = null;
let ashareKdjChart = null;
const ashareMainEl = document.getElementById("ashare-watch-main-chart");
const ashareKdjEl = document.getElementById("ashare-watch-kdj-chart");
const ashareTooltip = document.getElementById("ashare-watch-tooltip");
const ashareLoading = document.getElementById("ashare-watch-loading");
const ashareTitle = document.getElementById("ashare-watch-title");
const ashareSubtitle = document.getElementById("ashare-watch-subtitle");

function clearAshareCharts() {{
  if (ashareMainChart) ashareMainChart.remove();
  if (ashareKdjChart) ashareKdjChart.remove();
  ashareMainChart = null;
  ashareKdjChart = null;
}}

function renderAshareWatchChart(payload) {{
  clearAshareCharts();
  const ohlc = payload.ohlc || [];
  const volume = payload.volume || [];
  const toLine = rows => (rows || []).map(row => ({{ time: row.x, value: row.y }})).filter(row => row.value !== null && row.value !== undefined);
  const candles = ohlc.map(row => ({{ time: row.x, open: row.open, high: row.high, low: row.low, close: row.close }}));
  const volumes = volume.map(row => ({{ time: row.x, value: row.y, color: row.color }}));
  const rowByTime = new Map(candles.map((row, index) => [row.time, {{ ...row, volume: volume[index]?.y }}]));
  const common = {{
    layout: {{ background: {{ type: "solid", color: "#ffffff" }}, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif" }},
    grid: {{ vertLines: {{ color: "#f1f3f6" }}, horzLines: {{ color: "#f1f3f6" }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    timeScale: {{ borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }},
    handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
    handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
  }};
  ashareMainChart = LightweightCharts.createChart(ashareMainEl, {{ ...common, width: ashareMainEl.clientWidth, height: ashareMainEl.clientHeight, rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.08, bottom: 0.28 }} }} }});
  ashareKdjChart = LightweightCharts.createChart(ashareKdjEl, {{ ...common, width: ashareKdjEl.clientWidth, height: ashareKdjEl.clientHeight, rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.12, bottom: 0.12 }} }} }});
  const candle = ashareMainChart.addCandlestickSeries({{ upColor: "#089981", downColor: "#f23645", borderUpColor: "#089981", borderDownColor: "#f23645", wickUpColor: "#089981", wickDownColor: "#f23645", priceLineVisible: false }});
  candle.setData(candles);
  const trend = ashareMainChart.addLineSeries({{ color: "#2563eb", lineWidth: 2, title: "MA5", priceLineVisible: false }});
  trend.setData(toLine(payload.ma5 || payload.zx_short_trend));
  const multi = ashareMainChart.addLineSeries({{ color: "#dc2626", lineWidth: 2, title: "MA20", priceLineVisible: false }});
  multi.setData(toLine(payload.ma20 || payload.zx_multi_trend));
  const volSeries = ashareMainChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
  volSeries.setData(volumes);
  const volMa = ashareMainChart.addLineSeries({{ color: "#475569", lineWidth: 1, priceScaleId: "", title: "Vol MA20", priceLineVisible: false, lastValueVisible: false }});
  volMa.setData(toLine(payload.volume_ma20));
  ashareMainChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
  candle.setMarkers((payload.signals || []).map(row => ({{ time: row.x, position: "belowBar", color: "#16a34a", shape: "arrowUp", text: row.text || "B" }})));
  ashareKdjChart.addLineSeries({{ color: "#2563eb", lineWidth: 1.5, title: "K", priceLineVisible: false }}).setData(toLine(payload.k));
  ashareKdjChart.addLineSeries({{ color: "#f59e0b", lineWidth: 1.5, title: "D", priceLineVisible: false }}).setData(toLine(payload.d));
  ashareKdjChart.addLineSeries({{ color: "#7c3aed", lineWidth: 2, title: "J", priceLineVisible: false }}).setData(toLine(payload.j));
  const kdjDates = ohlc.map(row => row.x);
  ashareKdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "80", priceLineVisible: false, lastValueVisible: false }}).setData(kdjDates.map(time => ({{ time, value: 80 }})));
  ashareKdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "20", priceLineVisible: false, lastValueVisible: false }}).setData(kdjDates.map(time => ({{ time, value: 20 }})));
  let syncing = false;
  function sync(source, target) {{
    source.timeScale().subscribeVisibleLogicalRangeChange(range => {{
      if (syncing || !range) return;
      syncing = true;
      target.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    }});
  }}
  sync(ashareMainChart, ashareKdjChart);
  sync(ashareKdjChart, ashareMainChart);
  const f = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
  const fv = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
  ashareMainChart.subscribeCrosshairMove(param => {{
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0 || param.point.x > ashareMainEl.clientWidth || param.point.y > ashareMainEl.clientHeight) {{
      ashareTooltip.style.display = "none";
      return;
    }}
    const row = rowByTime.get(param.time);
    if (!row) return;
    const up = row.close >= row.open;
    ashareTooltip.innerHTML = `<strong>${{row.time}}</strong><div><span class="${{up ? "up" : "down"}}">开 ${{f(row.open)}} 高 ${{f(row.high)}} 低 ${{f(row.low)}} 收 ${{f(row.close)}}</span></div><div>成交量 ${{fv(row.volume)}} &nbsp; 量MA20 ${{fv(param.seriesData.get(volMa)?.value)}} &nbsp; MA5 ${{f(param.seriesData.get(trend)?.value)}} &nbsp; MA20 ${{f(param.seriesData.get(multi)?.value)}}</div>`;
    ashareTooltip.style.display = "block";
    ashareTooltip.style.left = Math.min(param.point.x + 16, ashareMainEl.clientWidth - 280) + "px";
    ashareTooltip.style.top = Math.max(44, param.point.y - 72) + "px";
  }});
  new ResizeObserver(entries => {{
    const rect = entries[0].contentRect;
    if (ashareMainChart) ashareMainChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(ashareMainEl);
  new ResizeObserver(entries => {{
    const rect = entries[0].contentRect;
    if (ashareKdjChart) ashareKdjChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(ashareKdjEl);
  ashareMainChart.timeScale().fitContent();
  ashareKdjChart.timeScale().fitContent();
}}

async function loadAshareWatchChart(symbol) {{
  if (!symbol) return;
  ashareTitle.textContent = symbol;
  ashareSubtitle.textContent = "正在加载...";
  ashareLoading?.classList.add("active");
  try {{
    const res = await fetch(`/cn/watchlist/chart?symbol=${{encodeURIComponent(symbol)}}&j_threshold=14`);
    const payload = await res.json();
    if (payload.error) {{
      ashareSubtitle.textContent = payload.error;
      clearAshareCharts();
      return;
    }}
    ashareTitle.textContent = `${{payload.symbol}}${{payload.name ? " / " + payload.name : ""}}`;
    ashareSubtitle.textContent = "A 股策略图表，日 K 数据";
    document.getElementById("ashare-detail-name").textContent = payload.name || "-";
    document.getElementById("ashare-detail-sector").textContent = payload.sector || "-";
    document.getElementById("ashare-detail-source").textContent = payload.source || "-";
    document.getElementById("ashare-detail-count").textContent = String((payload.ohlc || []).length);
    renderAshareWatchChart(payload);
  }} catch (error) {{
    ashareSubtitle.textContent = error?.message || "图表加载失败";
    clearAshareCharts();
  }} finally {{
    ashareLoading?.classList.remove("active");
  }}
}}

document.addEventListener("click", event => {{
  const target = event.target.closest("[data-ashare-symbol]");
  if (!target) return;
  event.preventDefault();
  loadAshareWatchChart(target.getAttribute("data-ashare-symbol"));
}});
if (ashareInitialSymbol) loadAshareWatchChart(ashareInitialSymbol);
</script>
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
            f"<td>{html.escape(trade_structure_label(getattr(trade, 'entry_structure', '')))}</td>"
            f"<td>{html.escape(trade.exit_signal_date)}</td>"
            f"<td>{html.escape(trade.exit_date)}</td>"
            f"<td>{trade.entry_price:.2f}</td>"
            f"<td>{trade.exit_price:.2f}</td>"
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
            f"<td>{html.escape(trade_structure_label(open_position.get('entry_structure', '')))}</td>"
            "<td>未触发</td>"
            "<td>未平仓</td>"
            f"<td>{float(open_position['entry_price']):.2f}</td>"
            f"<td>{float(open_position['mark_price']):.2f}</td>"
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
      <thead><tr><th>#</th><th>买入信号日</th><th>买入操作日</th><th>买入结构</th><th>卖出信号日</th><th>卖出操作日</th><th>买入价</th><th>卖出价</th><th>持仓K线</th><th>收益金额</th><th>收益率</th><th>卖出原因</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
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
        vol_high_days=int(number_field(params, "vol_high_days", 3)),
        vol_high_multiplier=number_field(params, "vol_high_multiplier", 1.0),
        massive_window=int(number_field(params, "massive_window", 7)),
        massive_min_count=int(number_field(params, "massive_min_count", 1)),
        massive_max_count=int(number_field(params, "massive_max_count", 2)),
        b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        require_ma5_rising=checkbox_field(params, "require_ma5_rising", True),
        require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", True),
        below_20ma_stop_days=int(number_field(params, "below_20ma_stop_days", 2)),
        weak_trend_exit_mode=field(params, "weak_trend_exit_mode", "hybrid"),
        weak_ma5_reclaim_days=int(number_field(params, "weak_ma5_reclaim_days", 5)),
        weak_ma20_reclaim_days=int(number_field(params, "weak_ma20_reclaim_days", 10)),
        weak_volume_down_multiplier=number_field(params, "weak_volume_down_multiplier", 1.5),
        weak_event_low_lookback=int(number_field(params, "weak_event_low_lookback", 27)),
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
    strategy_settings = {
        "vol_high_days": int(number_field(params, "vol_high_days", 3)),
        "vol_high_multiplier": number_field(params, "vol_high_multiplier", 1.0),
        "massive_window": int(number_field(params, "massive_window", 7)),
        "massive_min_count": int(number_field(params, "massive_min_count", 1)),
        "massive_max_count": int(number_field(params, "massive_max_count", 2)),
        "b1_require_20ma_gt_50ma": checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        "require_ma5_rising": checkbox_field(params, "require_ma5_rising", True),
        "require_5ma_gt_20ma": checkbox_field(params, "require_5ma_gt_20ma", True),
        "reentry_pct": number_field(params, "reentry_pct", 4.5),
        "stop_5ma_pct": number_field(params, "stop_5ma_pct", 7.5),
        "hard_stop_pct": number_field(params, "hard_stop_pct", 20),
        "below_20ma_stop_days": int(number_field(params, "below_20ma_stop_days", 2)),
    }
    make_report(report_path, f"{symbol} {strategy_name} backtest {start} to {end}", bars, trades, equity_curve, summary, benchmark=benchmark, strategy_settings=strategy_settings)
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
  <iframe src="{report_url}" title="Backtest report"></iframe>
</section>
"""


def render_batch_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    today = date.today()
    preset_value = field(params, "preset", "1y")
    display_preset = display_preset_for_dates(params, preset_value, today)
    start_default = start_for_preset(preset_value, today).isoformat()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""

    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", True) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", True) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", True) else ""

    return f"""
<section class="page-head">
  <div>
    <h1>批量回测</h1>
    <p class="hint">组合资金池回测：一笔总资金同时交易多个股票，每次买入按固定金额下单，卖出后现金回到组合。</p>
  </div>
  <div class="mode-pill">Portfolio Backtest</div>
</section>
{render_us_strategy_condition_panel(params, "batch")}
<form class="form" action="/batch/run" method="get">
  <label class="wide">股票代码，逗号或换行分隔
    <textarea name="symbols" placeholder="AAPL,MSFT,NVDA,TSM">{value("symbols", "AAPL,MSFT,NVDA,TSM")}</textarea>
  </label>
  <label>回测周期
    <select name="preset" id="batch-preset">
      <option value="1m"{selected(display_preset, "1m")}>最近1个月</option>
      <option value="3m"{selected(display_preset, "3m")}>最近3个月</option>
      <option value="6m"{selected(display_preset, "6m")}>最近6个月</option>
      <option value="1y"{selected(display_preset, "1y")}>最近1年</option>
      <option value="3y"{selected(display_preset, "3y")}>最近3年</option>
      <option value="5y"{selected(display_preset, "5y")}>最近5年</option>
      <option value="custom"{selected(display_preset, "custom")}>自定义</option>
    </select>
  </label>
  <label>开始日期<input type="date" name="start" value="{value("start", start_default)}"></label>
  <label>结束日期<input type="date" name="end" value="{value("end", today.isoformat())}"></label>
  <label>组合总资金<input name="initial_cash" value="{value("initial_cash", "100000")}"></label>
  <label>每次买入金额<input name="position_cash" value="{value("position_cash", "10000")}"></label>
  <label>手续费 %<input name="commission_pct" value="{value("commission_pct", "0.1")}"></label>
  <label>滑点 %<input name="slippage_pct" value="{value("slippage_pct", "0")}"></label>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" value="{value("vol_length", "20")}"></label>
  <label>连续放量天数<input name="vol_high_days" value="{value("vol_high_days", "3")}"></label>
  <label>连续放量倍数<input name="vol_high_multiplier" value="{value("vol_high_multiplier", "1.0")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>巨量观察窗口<input name="massive_window" value="{value("massive_window", "7")}"></label>
  <label>巨量最少次数<input name="massive_min_count" value="{value("massive_min_count", "1")}"></label>
  <input type="hidden" name="require_ma5_rising" value="0">
  <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{require_ma5_rising_checked}> 买入要求5MA向上</label>
  <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
  <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{b1_require_20ma_gt_50ma_checked}> B1要求20MA&gt;50MA</label>
  <input type="hidden" name="require_5ma_gt_20ma" value="0">
  <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{require_5ma_gt_20ma_checked}> 买入要求5MA&gt;20MA</label>
  <label>跌破均线止损 %<input name="stop_5ma_pct" value="{value("stop_5ma_pct", "7.5")}"></label>
  <label>连续跌破20MA天数<input name="below_20ma_stop_days" value="{value("below_20ma_stop_days", "2")}"></label>
  <label>成本强制止损 %<input name="hard_stop_pct" value="{value("hard_stop_pct", "20")}"></label>
  <label>弱趋势卖出
    <select name="weak_trend_exit_mode">
      <option value="hybrid"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "hybrid")}>混合模式：仅5MA&lt;20MA买入启用</option>
      <option value="off"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "off")}>关闭：全部使用标准止损</option>
      <option value="weak"{selected(field(params, "weak_trend_exit_mode", "hybrid"), "weak")}>弱趋势持仓使用修复止损</option>
    </select>
  </label>
  <label>站回5MA期限<input name="weak_ma5_reclaim_days" value="{value("weak_ma5_reclaim_days", "5")}"></label>
  <label>站回20MA期限<input name="weak_ma20_reclaim_days" value="{value("weak_ma20_reclaim_days", "10")}"></label>
  <label>放量下跌倍数<input name="weak_volume_down_multiplier" value="{value("weak_volume_down_multiplier", "1.5")}"></label>
  <label>事件低点窗口<input name="weak_event_low_lookback" value="{value("weak_event_low_lookback", "27")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <button type="submit">运行批量回测</button>
</form>
<script>
(function() {{
  const preset = document.getElementById("batch-preset");
  if (!preset) return;
  const form = preset.closest("form");
  const start = form.querySelector('input[name="start"]');
  const end = form.querySelector('input[name="end"]');
  const pad = value => String(value).padStart(2, "0");
  const fmt = d => `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}}`;
  const offsets = {{ "1m": 31, "3m": 92, "6m": 183, "1y": 365, "3y": 365 * 3, "5y": 365 * 5 }};
  preset.addEventListener("change", () => {{
    if (preset.value === "custom") return;
    const endDate = end.value ? new Date(end.value + "T00:00:00") : new Date();
    const nextStart = new Date(endDate);
    nextStart.setDate(nextStart.getDate() - (offsets[preset.value] || 365));
    start.value = fmt(nextStart);
    end.value = fmt(endDate);
  }});
  start.addEventListener("change", () => {{ preset.value = "custom"; }});
  end.addEventListener("change", () => {{ preset.value = "custom"; }});
}})();
</script>
"""

def portfolio_pct(value: float) -> str:
    cls = "pos" if value >= 0 else "neg"
    return f'<span class="{cls}">{value:.2f}%</span>'


def portfolio_money(value: float) -> str:
    cls = "pos" if value >= 0 else "neg"
    return f'<span class="{cls}">{value:,.2f}</span>'


def run_batch_backtest(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_old_reports()
    symbols = parse_symbols_text(field(params, "symbols", "AAPL,MSFT,NVDA,TSM"))
    preset = field(params, "preset", "1y")
    start = field(params, "start", start_for_preset(preset, date.today()).isoformat())
    end = field(params, "end", date.today().isoformat())
    initial_cash = number_field(params, "initial_cash", 100000)
    position_cash = number_field(params, "position_cash", 10000)
    ma_length = int(number_field(params, "ma_length", 5))
    vol_length = int(number_field(params, "vol_length", 20))
    validate_backtest_range(start, end, vol_length)
    if initial_cash <= 0:
        raise ValueError("组合总资金必须大于 0。")
    if position_cash <= 0:
        raise ValueError("每次买入金额必须大于 0。")

    commission_pct = number_field(params, "commission_pct", 0.1)
    slippage_pct = number_field(params, "slippage_pct", 0)
    strategy_settings = {
        "vol_high_days": int(number_field(params, "vol_high_days", 3)),
        "vol_high_multiplier": number_field(params, "vol_high_multiplier", 1.0),
        "massive_window": int(number_field(params, "massive_window", 7)),
        "massive_min_count": int(number_field(params, "massive_min_count", 1)),
        "massive_max_count": int(number_field(params, "massive_max_count", 2)),
        "b1_require_20ma_gt_50ma": checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        "require_ma5_rising": checkbox_field(params, "require_ma5_rising", True),
        "require_5ma_gt_20ma": checkbox_field(params, "require_5ma_gt_20ma", True),
        "reentry_pct": number_field(params, "reentry_pct", 4.5),
        "stop_5ma_pct": number_field(params, "stop_5ma_pct", 7.5),
        "hard_stop_pct": number_field(params, "hard_stop_pct", 20),
        "below_20ma_stop_days": int(number_field(params, "below_20ma_stop_days", 2)),
        "weak_trend_exit_mode": field(params, "weak_trend_exit_mode", "hybrid"),
        "weak_ma5_reclaim_days": int(number_field(params, "weak_ma5_reclaim_days", 5)),
        "weak_ma20_reclaim_days": int(number_field(params, "weak_ma20_reclaim_days", 10)),
        "weak_volume_down_multiplier": number_field(params, "weak_volume_down_multiplier", 1.5),
        "weak_event_low_lookback": int(number_field(params, "weak_event_low_lookback", 27)),
    }
    symbol_data: dict[str, dict[str, object]] = {}
    errors: list[tuple[str, str]] = []
    for symbol in symbols:
        try:
            bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
            chart_trades, signal_curve = backtest(
                bars=bars,
                ma_length=ma_length,
                vol_length=vol_length,
                vol_multiplier=number_field(params, "vol_multiplier", 1.45),
                initial_cash=initial_cash,
                commission_pct=commission_pct,
                slippage_pct=slippage_pct,
                strategy_name="ratchet",
                stop_5ma_pct=number_field(params, "stop_5ma_pct", 7.5),
                hard_stop_pct=number_field(params, "hard_stop_pct", 20),
                reentry_pct=number_field(params, "reentry_pct", 4.5),
                vol_high_days=int(number_field(params, "vol_high_days", 3)),
                vol_high_multiplier=number_field(params, "vol_high_multiplier", 1.0),
                massive_window=int(number_field(params, "massive_window", 7)),
                massive_min_count=int(number_field(params, "massive_min_count", 1)),
                massive_max_count=int(number_field(params, "massive_max_count", 2)),
                b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", True),
                require_ma5_rising=checkbox_field(params, "require_ma5_rising", True),
                require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", True),
                below_20ma_stop_days=int(number_field(params, "below_20ma_stop_days", 2)),
                weak_trend_exit_mode=field(params, "weak_trend_exit_mode", "hybrid"),
                weak_ma5_reclaim_days=int(number_field(params, "weak_ma5_reclaim_days", 5)),
                weak_ma20_reclaim_days=int(number_field(params, "weak_ma20_reclaim_days", 10)),
                weak_volume_down_multiplier=number_field(params, "weak_volume_down_multiplier", 1.5),
                weak_event_low_lookback=int(number_field(params, "weak_event_low_lookback", 27)),
            )
            chart_summary = summarize(chart_trades, signal_curve, initial_cash)
            chart_path = REPORT_DIR / f"{safe_name(f'batch_{symbol}_{start}_{end}_{int(time.time())}')}.html"
            make_report(
                chart_path,
                f"{symbol} batch chart {start} to {end}",
                bars,
                chart_trades,
                signal_curve,
                chart_summary,
                benchmark=None,
                strategy_settings=strategy_settings,
                report_mode="batch_chart",
            )
            symbol_data[symbol] = {
                "bars": bars,
                "bars_by_date": {bar.date: bar for bar in bars},
                "rows_by_date": {str(row["date"]): row for row in signal_curve},
                "last_close": 0.0,
                "report_url": f"/reports/{quote(chart_path.name)}",
            }
        except Exception as exc:
            errors.append((symbol, str(exc)))

    all_dates = sorted({bar.date for data in symbol_data.values() for bar in data["bars"]})  # type: ignore[index]
    cash = initial_cash
    positions: dict[str, dict[str, object]] = {}
    orders: list[dict[str, object]] = []
    realized_by_symbol: dict[str, float] = {}
    equity_curve: list[dict[str, float | str]] = []
    skipped_orders = 0

    for day in all_dates:
        for symbol, data in symbol_data.items():
            bar = data["bars_by_date"].get(day)  # type: ignore[union-attr]
            if bar:
                data["last_close"] = float(bar.close)

        for symbol, position in list(positions.items()):
            data = symbol_data.get(symbol)
            if not data:
                continue
            row = data["rows_by_date"].get(day)  # type: ignore[union-attr]
            bar = data["bars_by_date"].get(day)  # type: ignore[union-attr]
            if not row or not bar or not str(row.get("sell_action", "")):
                continue
            shares = int(position["shares"])
            fill_price = float(bar.open) * (1 - slippage_pct / 100)
            gross = shares * fill_price
            fee = gross * commission_pct / 100
            net = gross - fee
            cost_basis = float(position["avg_cost"]) * shares
            pnl = net - cost_basis
            cash += net
            realized_by_symbol[symbol] = realized_by_symbol.get(symbol, 0.0) + pnl
            orders.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "action": "卖出",
                    "signal_date": "-",
                    "stage": "S",
                    "shares": shares,
                    "price": fill_price,
                    "amount": net,
                    "cash_after": cash,
                    "pnl": pnl,
                    "note": str(row.get("sell_action", "卖出")),
                }
            )
            positions.pop(symbol, None)

        for symbol, data in symbol_data.items():
            row = data["rows_by_date"].get(day)  # type: ignore[union-attr]
            bar = data["bars_by_date"].get(day)  # type: ignore[union-attr]
            if not row or not bar or not str(row.get("buy_action", "")):
                continue
            budget = min(position_cash, cash)
            fill_price = float(bar.open) * (1 + slippage_pct / 100)
            cost_per_share = fill_price * (1 + commission_pct / 100)
            shares = int(budget // cost_per_share) if cost_per_share > 0 else 0
            if shares <= 0:
                skipped_orders += 1
                orders.append(
                    {
                        "date": day,
                        "symbol": symbol,
                        "action": "跳过买入",
                        "signal_date": str(row.get("buy_action_signal_date", "-") or "-"),
                        "stage": str(row.get("buy_action_stage", "B") or "B"),
                        "shares": 0,
                        "price": fill_price,
                        "amount": 0.0,
                        "cash_after": cash,
                        "pnl": 0.0,
                        "note": "现金不足，无法按每次买入金额下单",
                    }
                )
                continue
            gross = shares * fill_price
            fee = gross * commission_pct / 100
            total_cost = gross + fee
            if total_cost > cash:
                continue
            old = positions.get(symbol)
            old_shares = int(old["shares"]) if old else 0
            old_cost = float(old["avg_cost"]) * old_shares if old else 0.0
            new_shares = old_shares + shares
            cash -= total_cost
            positions[symbol] = {
                "shares": new_shares,
                "avg_cost": (old_cost + total_cost) / new_shares,
                "entry_date": old.get("entry_date") if old else day,
                "last_buy_date": day,
                "last_stage": str(row.get("buy_action_stage", "B") or "B"),
            }
            orders.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "action": "买入" if old_shares == 0 else "加仓",
                    "signal_date": str(row.get("buy_action_signal_date", "-") or "-"),
                    "stage": str(row.get("buy_action_stage", "B") or "B"),
                    "shares": shares,
                    "price": fill_price,
                    "amount": total_cost,
                    "cash_after": cash,
                    "pnl": 0.0,
                    "note": f"目标买入金额 {position_cash:,.2f}",
                }
            )

        market_value = 0.0
        for symbol, position in positions.items():
            last_close = float(symbol_data.get(symbol, {}).get("last_close", 0.0))
            market_value += int(position["shares"]) * last_close
        equity = cash + market_value
        equity_curve.append({"date": day, "cash": cash, "market_value": market_value, "equity": equity})

    final_equity = float(equity_curve[-1]["equity"]) if equity_curve else initial_cash
    net_profit = final_equity - initial_cash
    return_pct = net_profit / initial_cash * 100
    peak = initial_cash
    max_drawdown = 0.0
    for row in equity_curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

    completed_sells = [row for row in orders if row["action"] == "卖出"]
    wins = [row for row in completed_sells if float(row["pnl"]) > 0]
    win_rate = len(wins) / len(completed_sells) * 100 if completed_sells else 0.0
    symbol_rows = sorted(
        (
            (
                symbol,
                realized_by_symbol.get(symbol, 0.0),
                int(sum(int(row["shares"]) for row in orders if row["symbol"] == symbol and row["action"] in ("买入", "加仓"))),
                sum(1 for row in orders if row["symbol"] == symbol and row["action"] in ("买入", "加仓")),
                sum(1 for row in orders if row["symbol"] == symbol and row["action"] == "卖出"),
            )
            for symbol in symbol_data
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    symbol_body = "\n".join(
        "<tr>"
        f'<td><button type="button" class="symbol-button" data-batch-chart-jump="{html.escape(symbol)}">{html.escape(symbol)}</button></td>'
        f"<td>{portfolio_money(pnl)}</td>"
        f"<td>{buy_count}</td>"
        f"<td>{sell_count}</td>"
        f"<td>{shares}</td>"
        "</tr>"
        for symbol, pnl, shares, buy_count, sell_count in symbol_rows
    )
    if not symbol_body:
        symbol_body = '<tr><td colspan="5" class="empty">没有成功回测的股票。</td></tr>'

    holding_body = "\n".join(
        "<tr>"
        f"<td>{html.escape(symbol)}</td>"
        f"<td>{int(position['shares'])}</td>"
        f"<td>{float(position['avg_cost']):.2f}</td>"
        f"<td>{float(symbol_data.get(symbol, {}).get('last_close', 0.0)):.2f}</td>"
        f"<td>{portfolio_money(int(position['shares']) * float(symbol_data.get(symbol, {}).get('last_close', 0.0)) - int(position['shares']) * float(position['avg_cost']))}</td>"
        f"<td>{html.escape(str(position.get('last_stage', '-')))}</td>"
        "</tr>"
        for symbol, position in sorted(positions.items())
    )
    if not holding_body:
        holding_body = '<tr><td colspan="6" class="empty">期末没有持仓。</td></tr>'

    order_body = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row['date']))}</td>"
        f"<td>{html.escape(str(row['symbol']))}</td>"
        f"<td>{html.escape(str(row['action']))}</td>"
        f"<td>{html.escape(str(row['stage']))}</td>"
        f"<td>{html.escape(str(row['signal_date']))}</td>"
        f"<td>{int(row['shares'])}</td>"
        f"<td>{float(row['price']):.2f}</td>"
        f"<td>{float(row['amount']):,.2f}</td>"
        f"<td>{portfolio_money(float(row['pnl']))}</td>"
        f"<td>{float(row['cash_after']):,.2f}</td>"
        f"<td>{html.escape(str(row['note']))}</td>"
        "</tr>"
        for row in orders[-300:]
    )
    if not order_body:
        order_body = '<tr><td colspan="11" class="empty">没有组合交易。</td></tr>'

    chart_items = [(symbol, str(data.get("report_url", "#"))) for symbol, data in symbol_data.items()]
    first_chart_url = chart_items[0][1] if chart_items else "#"
    chart_buttons = "\n".join(
        f'<button type="button" class="{"active" if index == 0 else ""}" '
        f'data-batch-chart-symbol="{html.escape(symbol)}" '
        f'data-batch-chart-url="{html.escape(url)}">{html.escape(symbol)}</button>'
        for index, (symbol, url) in enumerate(chart_items)
    )
    chart_viewer = (
        f"""
<section class="result" id="batch-chart-viewer">
  <div class="toolbar">
    <div>
      <h2>个股交易图</h2>
      <p class="hint">点击股票切换图表；批量页图表只显示组合实际执行的买入、卖出点，不显示未执行信号。</p>
    </div>
    <div class="links"><a id="batch-chart-open" href="{html.escape(first_chart_url)}" target="_blank">打开完整图表</a></div>
  </div>
  <div class="period-tabs" id="batch-chart-tabs">{chart_buttons}</div>
  <iframe id="batch-chart-frame" src="{html.escape(first_chart_url)}" title="batch chart"></iframe>
</section>
<script>
(function() {{
  const tabs = document.getElementById("batch-chart-tabs");
  const frame = document.getElementById("batch-chart-frame");
  const openLink = document.getElementById("batch-chart-open");
  const viewer = document.getElementById("batch-chart-viewer");
  if (!tabs || !frame) return;
  function selectChart(symbol, shouldScroll) {{
    const buttons = Array.from(tabs.querySelectorAll("[data-batch-chart-symbol]"));
    const button = buttons.find((item) => item.dataset.batchChartSymbol === symbol);
    if (!button) return;
    buttons.forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    const url = button.dataset.batchChartUrl || "#";
    frame.src = url;
    frame.title = `${{symbol}} batch chart`;
    if (openLink) openLink.href = url;
    if (shouldScroll && viewer) viewer.scrollIntoView({{ behavior: "smooth", block: "start" }});
  }}
  tabs.addEventListener("click", (event) => {{
    const button = event.target.closest("[data-batch-chart-symbol]");
    if (!button) return;
    selectChart(button.dataset.batchChartSymbol || "", false);
  }});
  document.addEventListener("click", (event) => {{
    const button = event.target.closest("[data-batch-chart-jump]");
    if (!button) return;
    selectChart(button.dataset.batchChartJump || "", true);
  }});
}})();
</script>
"""
        if chart_items
        else """
<section class="result">
  <h2>个股交易图</h2>
  <p class="empty">没有可显示的股票图表。</p>
</section>
"""
    )
    return f"""
{render_batch_form(params)}
<section class="result">
  <p class="hint">组合资金池回测：已处理 {len(symbols)} 个代码，成功 {len(symbol_data)} 个，失败 {len(errors)} 个。区间：{html.escape(start)} 到 {html.escape(end)}。每次买入金额：{position_cash:,.2f}。</p>
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">组合总资金</div><div class="stat-value">{initial_cash:,.2f}</div></div>
    <div class="stat-card"><div class="stat-label">期末权益</div><div class="stat-value">{final_equity:,.2f}</div></div>
    <div class="stat-card"><div class="stat-label">组合收益率</div><div class="stat-value">{return_pct:.2f}%</div></div>
    <div class="stat-card"><div class="stat-label">最大回撤</div><div class="stat-value">{max_drawdown:.2f}%</div></div>
    <div class="stat-card"><div class="stat-label">期末现金</div><div class="stat-value">{cash:,.2f}</div></div>
    <div class="stat-card"><div class="stat-label">卖出胜率</div><div class="stat-value">{win_rate:.2f}%</div></div>
  </section>
  <h2>股票贡献</h2>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>Symbol</th><th>已实现收益</th><th>买入次数</th><th>卖出次数</th><th>累计买入股数</th></tr></thead>
      <tbody>{symbol_body}</tbody>
    </table>
  </div>
  <h2>期末持仓</h2>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>Symbol</th><th>股数</th><th>持仓成本</th><th>最新收盘</th><th>浮动盈亏</th><th>最近阶段</th></tr></thead>
      <tbody>{holding_body}</tbody>
    </table>
  </div>
  <h2>组合交易流水</h2>
  <div class="table-wrap">
    <table class="resizable-table">
      <thead><tr><th>日期</th><th>Symbol</th><th>动作</th><th>阶段</th><th>信号日</th><th>股数</th><th>成交价</th><th>成交金额</th><th>已实现盈亏</th><th>交易后现金</th><th>说明</th></tr></thead>
      <tbody>{order_body}</tbody>
    </table>
  </div>
  <p class="hint">说明：买入按固定金额执行；同一股票 B1/B2 可分别触发买入或加仓；卖出信号出现时该股票组合持仓全部卖出。若现金不足，会记录为跳过买入。</p>
  {render_failure_table(errors)}
</section>
{chart_viewer}
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


def format_us_money(value: object) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    if number >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.2f} 万亿美元"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f} 十亿美元"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f} 百万美元"
    return f"{number:,.0f} 美元"


def format_us_number(value: object, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not number or number != number:
        return "-"
    return f"{number:.{digits}f}"


def us_sector_zh(value: str) -> str:
    mapping = {
        "Technology": "科技",
        "Health Care": "医疗保健",
        "Healthcare": "医疗保健",
        "Consumer Discretionary": "非必需消费",
        "Consumer Staples": "必需消费",
        "Industrials": "工业",
        "Financials": "金融",
        "Energy": "能源",
        "Utilities": "公用事业",
        "Real Estate": "房地产",
        "Materials": "材料",
        "Communication Services": "通信服务",
        "Telecommunications": "通信",
        "Basic Materials": "基础材料",
    }
    clean = str(value or "").strip()
    if not clean:
        return "-"
    return mapping.get(clean, "其他")


def us_industry_zh(value: str) -> str:
    mapping = {
        "Consumer Electronics": "消费电子",
        "Semiconductors": "半导体",
        "Semiconductor Equipment & Materials": "半导体设备与材料",
        "Software - Infrastructure": "基础软件",
        "Software - Application": "应用软件",
        "Information Technology Services": "信息技术服务",
        "Internet Content & Information": "互联网内容与信息",
        "Communication Equipment": "通信设备",
        "Computer Hardware": "计算机硬件",
        "Electronic Components": "电子元件",
        "Scientific & Technical Instruments": "科学与技术仪器",
        "Auto Manufacturers": "汽车制造",
        "Auto Parts": "汽车零部件",
        "Specialty Retail": "专业零售",
        "Internet Retail": "互联网零售",
        "Restaurants": "餐饮",
        "Banks - Diversified": "综合银行",
        "Banks - Regional": "区域银行",
        "Capital Markets": "资本市场",
        "Asset Management": "资产管理",
        "Credit Services": "信贷服务",
        "Insurance - Diversified": "综合保险",
        "Drug Manufacturers - General": "综合制药",
        "Drug Manufacturers - Specialty & Generic": "特色与仿制药",
        "Biotechnology": "生物科技",
        "Medical Devices": "医疗器械",
        "Medical Instruments & Supplies": "医疗仪器与耗材",
        "Diagnostics & Research": "诊断与研究",
        "Oil & Gas E&P": "油气勘探与生产",
        "Oil & Gas Integrated": "综合油气",
        "Oil & Gas Equipment & Services": "油气设备与服务",
        "Utilities - Regulated Electric": "受监管电力公用事业",
        "Utilities - Renewable": "可再生能源公用事业",
        "Aerospace & Defense": "航空航天与国防",
        "Specialty Industrial Machinery": "专用工业机械",
        "Farm & Heavy Construction Machinery": "农业与重型工程机械",
        "Building Products & Equipment": "建筑产品与设备",
        "Railroads": "铁路运输",
        "Airlines": "航空公司",
        "REIT - Specialty": "特色REIT",
        "REIT - Industrial": "工业REIT",
        "Gold": "黄金",
        "Copper": "铜",
        "Steel": "钢铁",
        "Other Industrial Metals & Mining": "其他工业金属与矿业",
    }
    clean = str(value or "").strip()
    if not clean:
        return "-"
    return mapping.get(clean, "未分类行业")


def us_asset_type_zh(value: str) -> str:
    clean = str(value or "").strip()
    if clean.upper() == "ETF":
        return "ETF"
    return "股票"


def earnings_status_zh(value: str) -> str:
    mapping = {
        "Estimated": "预估",
        "Confirmed": "已确认",
        "Unknown": "未知",
        "Unavailable": "不可用",
    }
    clean = str(value or "").strip()
    return mapping.get(clean, clean or "未知")


def match_us_company_points(raw_text: str, sector: str, industry: str, raw_sector: str = "", raw_industry: str = "") -> tuple[list[str], list[str], list[str]]:
    text = f"{raw_text} {sector} {industry} {raw_sector} {raw_industry}".lower()
    rules = [
        ("AI/数据中心", ("artificial intelligence", " ai ", "gpu", "accelerator", "data center", "datacenter", "server", "cloud infrastructure"), "重点核对AI订单、数据中心资本开支、云厂商需求和业绩指引。"),
        ("半导体/芯片", ("semiconductor", "semiconductors", "chip", "processor", "memory", "dram", "nand", "wafer", "foundry", "半导体"), "重点核对芯片周期、库存变化、毛利率、客户拉货和行业价格趋势。"),
        ("软件/SaaS", ("software", "saas", "subscription", "platform", "cloud", "enterprise software"), "重点核对收入增速、净留存率、订阅收入、利润率和大客户扩张。"),
        ("网络安全", ("cybersecurity", "security", "endpoint", "threat", "identity"), "重点核对大客户签约、ARR增长、竞争格局和预算恢复。"),
        ("消费电子/硬件", ("smartphone", "iphone", "ipad", "mac", "wearables", "consumer electronics", "消费电子"), "重点核对新品周期、出货量、渠道库存和供应链订单。"),
        ("电动车/新能源车", ("electric vehicle", " ev ", "battery", "automotive", "新能源车"), "重点核对交付量、价格战、毛利率、产能利用率和政策补贴。"),
        ("医药/生物科技", ("biotechnology", "clinical", "drug", "therapy", "pharmaceutical", "fda", "trial"), "重点核对临床数据、FDA节点、药品销售和研发管线风险。"),
        ("医疗器械/诊断", ("medical device", "diagnostic", "surgical", "patient", "healthcare"), "重点核对装机量、耗材收入、医院采购和医保支付变化。"),
        ("金融/银行", ("bank", "loan", "deposit", "credit", "asset management", "capital markets", "insurance"), "重点核对利率环境、净息差、坏账率、资本充足率和交易/投行业务。"),
        ("能源/油气", ("oil", "gas", "lng", "drilling", "exploration", "pipeline", "energy"), "重点核对油气价格、产量、现金流、资本开支和分红回购。"),
        ("矿业/金属", ("gold", "copper", "steel", "mining", "metal", "lithium", "uranium"), "重点核对商品价格、矿山产量、成本曲线和供需缺口。"),
        ("工业/设备", ("machinery", "aerospace", "defense", "industrial automation", "factory automation", "工业"), "重点核对订单积压、交付节奏、政府/企业资本开支和利润率。"),
        ("零售/消费", ("retail", "store", "restaurant", "consumer", "apparel", "e-commerce"), "重点核对同店销售、客单价、库存、促销压力和消费景气。"),
        ("通信/互联网内容", ("advertising", "streaming", "social", "search", "content", "communication services"), "重点核对广告需求、用户增长、订阅变化和内容/算力投入。"),
        ("房地产/REIT", ("reit", "real estate", "property", "lease", "tenant"), "重点核对出租率、租金增速、融资成本和资产估值。"),
        ("公用事业", ("utility", "electric", "renewable", "regulated", "power"), "重点核对电价机制、利率变化、负债成本和新能源项目进度。"),
    ]
    themes: list[str] = []
    focus: list[str] = []
    for label, keywords, note in rules:
        if any(keyword in text for keyword in keywords):
            themes.append(label)
            focus.append(note)
    if not themes:
        themes.append(industry if industry != "-" else sector if sector != "-" else "未识别题材")
        focus.append("重点从财报、业绩指引、行业新闻和成交量配合度判断这次放量是否有真实催化。")

    business_lines: list[str] = []
    business_rules = [
        ("硬件产品", ("hardware", "device", "smartphone", "computer", "equipment", "server")),
        ("软件平台", ("software", "platform", "cloud", "subscription", "saas")),
        ("芯片/零部件", ("semiconductor", "chip", "processor", "memory", "component")),
        ("服务收入", ("service", "services", "support", "subscription")),
        ("金融业务", ("loan", "deposit", "insurance", "asset management", "capital markets")),
        ("能源/资源", ("oil", "gas", "mining", "metal", "energy")),
        ("药品/治疗管线", ("drug", "therapy", "clinical", "pharmaceutical", "biotechnology")),
        ("零售/渠道", ("retail", "store", "restaurant", "e-commerce")),
    ]
    for label, keywords in business_rules:
        if any(keyword in text for keyword in keywords):
            business_lines.append(label)
    if not business_lines:
        business_lines.append(industry if industry != "-" else "主营业务需进一步核对")
    return business_lines[:4], themes[:3], focus[:3]


def render_us_company_watch_points(
    company_name: str,
    sector: str,
    industry: str,
    asset_type: str,
    website: str,
    raw_summary: str,
    raw_sector: str = "",
    raw_industry: str = "",
) -> str:
    display_name = company_name if company_name != "-" else "该标的"
    business_lines, themes, focus_items = match_us_company_points(raw_summary, sector, industry, raw_sector, raw_industry)
    line_html = "".join(f"<li>{html.escape(item)}</li>" for item in business_lines)
    theme_html = " ".join(f'<span class="condition-tag condition-primary">{html.escape(item)}</span>' for item in themes)
    focus_html = "".join(f"<li>{html.escape(item)}</li>" for item in focus_items)
    source_note = "官网可用于核对主营业务。" if website.startswith(("http://", "https://")) else "暂未获取官网，建议用Yahoo/雪球继续核对。"
    return f"""
  <section class="result" style="margin-top:12px;">
    <div class="strategy-condition-head" style="padding-left:0;padding-right:0;border-bottom:0;background:transparent;">
      <div>
        <strong>公司看点</strong>
        <p class="hint">{html.escape(display_name)}：用于判断这次放量是否有业务或题材支撑。</p>
      </div>
    </div>
    <div class="condition-grid">
      <div class="condition-card">
        <h3>主营业务线</h3>
        <ul class="condition-list">{line_html}</ul>
      </div>
      <div class="condition-card">
        <h3>可能题材</h3>
        <div class="condition-tags" style="justify-content:flex-start;">{theme_html}</div>
        <p class="condition-note">题材由公司介绍、板块和行业关键词提取，只作为二次看图前的线索。</p>
      </div>
      <div class="condition-card">
        <h3>二次确认重点</h3>
        <ul class="condition-list">{focus_html}</ul>
      </div>
      <div class="condition-card">
        <h3>使用方式</h3>
        <ul class="condition-list">
          <li><b>先看催化</b><span>财报/订单/指引/行业共振</span></li>
          <li><b>再看图形</b><span>B点、量能、上方空间</span></li>
          <li><b>信息源</b><span>{html.escape(source_note)}</span></li>
        </ul>
      </div>
    </div>
  </section>
"""


def candidate_from_latest_scan(symbol: str) -> dict[str, object]:
    latest = load_latest_scan()
    wanted = symbol.upper()
    if not latest:
        return {}
    for row in latest.get("candidates", []):
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == wanted:
            return row
    return {}


def fetch_us_company_profile(symbol: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    try:
        rows = watchlist_metadata_by_symbol([symbol])
        metadata.update(rows.get(symbol.upper(), {}))
    except Exception:
        pass
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).get_info() or {}
        for source, target in (
            ("longName", "company_name"),
            ("shortName", "company_name"),
            ("sector", "sector"),
            ("industry", "industry"),
            ("marketCap", "market_cap"),
            ("trailingPE", "trailing_pe"),
            ("forwardPE", "forward_pe"),
            ("trailingEps", "eps"),
            ("fiftyTwoWeekHigh", "week_52_high"),
            ("fiftyTwoWeekLow", "week_52_low"),
            ("website", "website"),
            ("longBusinessSummary", "business_summary"),
        ):
            value = info.get(source)
            if value not in ("", None):
                metadata[target] = value
    except Exception:
        pass
    return metadata


def render_us_company_info_panel(symbol: str, signal_result: SignalResult | None) -> str:
    latest_row = candidate_from_latest_scan(symbol)
    profile = fetch_us_company_profile(symbol)
    merged: dict[str, object] = {}
    if signal_result:
        merged.update(
            {
                "company_name": signal_result.company_name,
                "market_cap": signal_result.market_cap,
                "sector": signal_result.sector,
                "industry": signal_result.industry,
                "asset_type": signal_result.asset_type,
                "next_earnings_date": signal_result.next_earnings_date,
                "earnings_days": signal_result.earnings_days,
                "earnings_status": signal_result.earnings_status,
            }
        )
    merged.update({key: value for key, value in latest_row.items() if value not in ("", None, 0)})
    merged.update({key: value for key, value in profile.items() if value not in ("", None, 0)})

    company_name = str(merged.get("company_name") or symbol)
    raw_sector = str(merged.get("sector") or "")
    raw_industry = str(merged.get("industry") or "")
    sector = us_sector_zh(raw_sector)
    industry = us_industry_zh(raw_industry)
    asset_type = us_asset_type_zh(str(merged.get("asset_type") or "Stock"))
    earnings_date = str(merged.get("next_earnings_date") or "")
    earnings_days = merged.get("earnings_days", "")
    earnings_status = str(merged.get("earnings_status") or "")
    if earnings_date and str(earnings_days) not in ("", "9999"):
        earnings_text = f"{earnings_date}（约 {earnings_days} 天，{earnings_status_zh(earnings_status or 'Estimated')}）"
    elif earnings_date:
        earnings_text = earnings_date
    else:
        earnings_text = "未知"
    website = str(merged.get("website") or "")
    website_html = f'<a href="{html.escape(website)}" target="_blank">官网</a>' if website.startswith(("http://", "https://")) else "-"
    raw_summary = str(merged.get("business_summary") or "")
    watch_points_html = render_us_company_watch_points(company_name, sector, industry, asset_type, website, raw_summary, raw_sector, raw_industry)

    return f"""
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">公司名称</div><div class="stat-value">{html.escape(company_name)}</div></div>
    <div class="stat-card"><div class="stat-label">证券类型</div><div class="stat-value">{html.escape(asset_type)}</div></div>
    <div class="stat-card"><div class="stat-label">市值</div><div class="stat-value">{format_us_money(merged.get("market_cap"))}</div></div>
    <div class="stat-card"><div class="stat-label">所属板块</div><div class="stat-value">{html.escape(sector)}</div></div>
    <div class="stat-card"><div class="stat-label">所属行业</div><div class="stat-value">{html.escape(industry)}</div></div>
    <div class="stat-card"><div class="stat-label">下一次财报</div><div class="stat-value">{html.escape(earnings_text)}</div></div>
  </section>
  <div class="table-wrap">
    <table>
      <thead><tr><th>市盈率TTM</th><th>预期市盈率</th><th>每股收益TTM</th><th>52周高点</th><th>52周低点</th><th>官网</th><th>消息入口</th></tr></thead>
      <tbody><tr>
        <td>{format_us_number(merged.get("trailing_pe"))}</td>
        <td>{format_us_number(merged.get("forward_pe"))}</td>
        <td>{format_us_number(merged.get("eps"))}</td>
        <td>{format_us_number(merged.get("week_52_high"))}</td>
        <td>{format_us_number(merged.get("week_52_low"))}</td>
        <td>{website_html}</td>
        <td><a href="{html.escape(yahoo_news_url(symbol))}" target="_blank">Yahoo</a> <a href="{html.escape(xueqiu_news_url(symbol, company_name))}" target="_blank">雪球</a></td>
      </tr></tbody>
    </table>
  </div>
  {watch_points_html}
"""


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
    vol_high_days: int = 3,
    vol_high_multiplier: float = 1.0,
    massive_window: int = 7,
    massive_min_count: int = 1,
    massive_max_count: int = 2,
    b1_require_20ma_gt_50ma: bool = False,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> tuple[str, SignalResult | None, str | None]:
    try:
        signal_start = min(start, (date.fromisoformat(end) - timedelta(days=420)).isoformat())
        bars = fetch_bars("yfinance", symbol, signal_start, end, "qfq", None)
        result = latest_b_signal(
            symbol,
            bars,
            ma_length,
            vol_length,
            vol_multiplier,
            reentry_pct,
            min_price,
            min_avg_dollar_volume,
            vol_high_days,
            vol_high_multiplier,
            massive_window,
            massive_min_count,
            massive_max_count,
            b1_require_20ma_gt_50ma,
            require_ma5_rising,
            require_5ma_gt_20ma,
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
    if latest_scan and field(params, "_skip_latest_banner", "0") != "1":
        summary = latest_scan.get("summary", {})
        latest_html = f"""
<section class="result latest-scan-card scan-result-alert">
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
    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", True) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", True) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", True) else ""
    secondary_big_red_checked = " checked" if checkbox_field(params, "secondary_big_red_b1", False) else ""
    secondary_above_ma5_checked = " checked" if checkbox_field(params, "secondary_above_ma5_3d", False) else ""
    earnings_filter = field(params, "earnings_filter", "show")
    default_symbols = "ASTS,NVDA,TSLA,AAPL,MSFT,QQQ"
    return f"""
<section class="page-head">
  <div>
    <h1>下一交易日 B 点选股器</h1>
    <p class="hint">扫描最后一根已完成日 K 是否出现 B1/B2 信号。符合条件的股票按策略在下一交易日开盘执行；不使用盘后或夜盘价格。</p>
  </div>
  <div class="mode-pill">盘后复盘 | Daily Close</div>
</section>
{latest_html}
{render_market_environment_bar()}
{render_data_health_panel("us")}
{render_us_strategy_condition_panel(params, "scanner")}
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">模式</div><div class="stat-value">盘后复盘</div></div>
  <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{scan_end.isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{next_market_weekday(scan_end).isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">默认过滤</div><div class="stat-value">{DEFAULT_MIN_MARKET_CAP_100M_USD} 亿美元+</div></div>
</section>
<form class="form" id="scanner-form" action="/scan" method="get" data-async-submit="true">
  <div class="form-section-title">扫描范围 <span>股票池、数量和并发</span></div>
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
  <div class="form-section-title">基础过滤 <span>日期、价格、流动性和财报风险</span></div>
  <label>开始日期<input type="date" name="start" value="{value("start", default_scan_start_date(scan_end).isoformat())}"></label>
  <label>结束日期<input type="date" name="end" value="{value("end", scan_end.isoformat())}"></label>
  <label>最低价格<input name="min_price" value="{value("min_price", "5")}"></label>
  <label>20日最低成交额<input name="min_avg_dollar_volume" value="{value("min_avg_dollar_volume", "20000000")}"></label>
  <label>财报风险
    <select name="earnings_filter">
      <option value="show"{selected(earnings_filter, "show")}>显示全部</option>
      <option value="hide_3d"{selected(earnings_filter, "hide_3d")}>隐藏3天内财报</option>
      <option value="hide_7d"{selected(earnings_filter, "hide_7d")}>隐藏7天内财报</option>
      <option value="hide_unknown"{selected(earnings_filter, "hide_unknown")}>隐藏未知财报</option>
    </select>
  </label>
  <label class="checkbox-label"><input type="checkbox" name="hide_weak" value="1"{hide_weak_checked}> 隐藏 Weak 候选</label>
  <div class="form-section-title">信号参数 <span>B1/B2、放量和回踩距离</span></div>
  <label>均线周期<input name="ma_length" value="{value("ma_length", "5")}"></label>
  <label>均量周期<input name="vol_length" value="{value("vol_length", "20")}"></label>
  <label>连续放量天数<input name="vol_high_days" value="{value("vol_high_days", "3")}"></label>
  <label>连续放量倍数<input name="vol_high_multiplier" value="{value("vol_high_multiplier", "1.0")}"></label>
  <label>巨量倍数<input name="vol_multiplier" value="{value("vol_multiplier", "1.45")}"></label>
  <label>巨量观察窗口<input name="massive_window" value="{value("massive_window", "7")}"></label>
  <label>巨量最少次数<input name="massive_min_count" value="{value("massive_min_count", "1")}"></label>
  <label>反抽距离 %<input name="reentry_pct" value="{value("reentry_pct", "4.5")}"></label>
  <div class="form-options">
    <span>可选买入条件</span>
    <input type="hidden" name="require_ma5_rising" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{require_ma5_rising_checked}> MA5向上</label>
    <input type="hidden" name="require_5ma_gt_20ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{require_5ma_gt_20ma_checked}> MA5&gt;MA20</label>
    <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{b1_require_20ma_gt_50ma_checked}> 20MA&gt;50MA</label>
    <input type="hidden" name="secondary_big_red_b1" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_big_red_b1" value="1"{secondary_big_red_checked}> 大阴线B1</label>
    <input type="hidden" name="secondary_above_ma5_3d" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_above_ma5_3d" value="1"{secondary_above_ma5_checked}> 连续三天&gt;MA5</label>
  </div>
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
      window.initializeSecondaryFilters?.(scanResult);
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

async function restoreActiveScan() {{
  try {{
    const res = await fetch("/scan/active");
    const data = await res.json();
    if (!data.job_id) return;
    activeScanJobId = data.job_id;
    progressBox.style.display = "block";
    scanResult.innerHTML = data.result_html || "";
    if (scanResult.innerHTML) {{
      initializeResizableTables(scanResult);
      initializeSortableTables(scanResult);
      window.initializeSecondaryFilters?.(scanResult);
    }}
    updateProgress(data);
    if (!["done", "error", "stopped"].includes(data.status)) {{
      pollScan(data.job_id);
    }}
  }} catch (error) {{
    console.warn("restore scan failed", error);
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
window.initializeSecondaryFilters?.(document);
restoreActiveScan();
</script>
"""

def render_candidate_table(rows: list[SignalResult], params: dict[str, list[str]] | None = None) -> str:
    params = params or {}

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

    def divergence_note(event: dict[str, object] | None) -> str:
        if not event:
            return "-"
        parts = [
            str(event.get("event_date", "-")),
            str(event.get("importance_label", "-")),
            str(event.get("divergence_type_label", "-")),
        ]
        note = str(event.get("note", "") or "").strip()
        if note:
            parts.append(note)
        return " · ".join(parts)

    rendered_rows = []
    for r in rows:
        divergence = best_divergence_for_symbol(r.symbol, r.signal_date)
        filter_attrs = " ".join(
            f'{result_filter_attr(key)}="{1 if result_filter_value(r, key) else 0}"'
            for key, _, _ in OPTIONAL_RESULT_FILTERS
        )
        rendered_rows.append(
            f'<tr data-secondary-row {filter_attrs}><td><button type="button" class="symbol-button" data-candidate-symbol="{html.escape(r.symbol)}">{html.escape(r.symbol)}</button></td><td>{html.escape(r.company_name or "-")}</td>'
            f"<td>{r.market_cap / 1_000_000_000:.2f}</td>"
            f"<td>{html.escape(candidate_summary(r))}</td>"
            f'<td><span class="condition-tags">{render_optional_condition_tags(r)}</span></td>'
            f"<td>{render_divergence_chip(divergence)}</td>"
            f"<td>{(divergence or {}).get('score', 0)}/5</td>"
            f"<td>{html.escape(divergence_note(divergence))}</td>"
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
        )
    table_rows = "\n".join(rendered_rows)
    if not table_rows:
        table_rows = '<tr><td colspan="27" class="empty">No visible candidates.</td></tr>'
    return f"""
{render_result_filter_panel(params, len(rows))}
<div class="table-wrap">
<table class="resizable-table" data-secondary-filter-table>
  <thead><tr><th>Symbol</th><th>Company</th><th>Mkt Cap $B</th><th>Summary</th><th>可选条件</th><th>Divergence</th><th>D Score</th><th>D Event</th><th>Tech</th><th>Total</th><th>Rating</th><th>Watch</th><th>Next Earnings</th><th>Catalyst</th><th>Sector Score</th><th>Space</th><th>Candle</th><th>Sector</th><th>Industry</th><th>Signal Date</th><th>Signal</th><th>Close</th><th>MA</th><th>Dist</th><th>Vol Ratio</th><th>Massive 7D</th><th>20D $Vol</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
</div>
"""


def render_failure_table(failures: list[tuple[str, str]]) -> str:
    if not failures:
        return ""
    table_rows = "\n".join(
        f"<tr><td>{html.escape(symbol)}</td><td>{html.escape(classify_scan_error(reason))}</td><td>{html.escape(reason)}</td></tr>"
        for symbol, reason in failures
    )
    return f"""
<h2>失败原因</h2>
{render_error_category_chips(failures)}
<div class="table-wrap">
<table class="resizable-table">
  <thead><tr><th>Symbol</th><th>分类</th><th>Reason</th></tr></thead>
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
    saved_params["_skip_latest_banner"] = ["1"]
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
  {render_candidate_table(candidates, saved_params)}
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
    vol_high_days = int(number_field(params, "vol_high_days", 3))
    vol_high_multiplier = number_field(params, "vol_high_multiplier", 1.0)
    massive_window = int(number_field(params, "massive_window", 7))
    massive_min_count = int(number_field(params, "massive_min_count", 1))
    massive_max_count = int(number_field(params, "massive_max_count", 2))
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", True)
    require_ma5_rising = checkbox_field(params, "require_ma5_rising", True)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", True)
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
            signal_start = min(start, (date.fromisoformat(end) - timedelta(days=420)).isoformat())
            bars = fetch_bars("yfinance", symbol, signal_start, end, "qfq", None)
            result = latest_b_signal(
                symbol,
                bars,
                ma_length,
                vol_length,
                vol_multiplier,
                reentry_pct,
                min_price,
                min_avg_dollar_volume,
                vol_high_days,
                vol_high_multiplier,
                massive_window,
                massive_min_count,
                massive_max_count,
                b1_require_20ma_gt_50ma,
                require_ma5_rising,
                require_5ma_gt_20ma,
            )
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
  {render_candidate_table(display_rows, params)}
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
    signal_start = min(start, (date.fromisoformat(end) - timedelta(days=420)).isoformat())
    bars = fetch_bars("yfinance", symbol, signal_start, end, "qfq", None)
    signal_result = latest_b_signal(
        symbol,
        bars,
        int(number_field(params, "ma_length", 5)),
        int(number_field(params, "vol_length", 20)),
        number_field(params, "vol_multiplier", 1.45),
        number_field(params, "reentry_pct", 4.5),
        number_field(params, "min_price", 5),
        number_field(params, "min_avg_dollar_volume", 20_000_000),
        int(number_field(params, "vol_high_days", 3)),
        number_field(params, "vol_high_multiplier", 1.0),
        int(number_field(params, "massive_window", 7)),
        int(number_field(params, "massive_min_count", 1)),
        int(number_field(params, "massive_max_count", 2)),
        checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        checkbox_field(params, "require_ma5_rising", True),
        checkbox_field(params, "require_5ma_gt_20ma", True),
    )
    company_info_panel = render_us_company_info_panel(symbol, signal_result)
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
    strategy_settings = {
        "vol_high_days": int(number_field(params, "vol_high_days", 3)),
        "vol_high_multiplier": number_field(params, "vol_high_multiplier", 1.0),
        "massive_window": int(number_field(params, "massive_window", 7)),
        "massive_min_count": int(number_field(params, "massive_min_count", 1)),
        "massive_max_count": int(number_field(params, "massive_max_count", 2)),
        "b1_require_20ma_gt_50ma": checkbox_field(params, "b1_require_20ma_gt_50ma", True),
        "require_ma5_rising": checkbox_field(params, "require_ma5_rising", True),
        "require_5ma_gt_20ma": checkbox_field(params, "require_5ma_gt_20ma", True),
        "reentry_pct": number_field(params, "reentry_pct", 4.5),
        "stop_5ma_pct": 7.5,
        "hard_stop_pct": 20,
        "below_20ma_stop_days": int(number_field(params, "below_20ma_stop_days", 2)),
    }
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
        vol_high_days=int(strategy_settings["vol_high_days"]),
        vol_high_multiplier=float(strategy_settings["vol_high_multiplier"]),
        massive_window=int(strategy_settings["massive_window"]),
        massive_min_count=int(strategy_settings["massive_min_count"]),
        massive_max_count=int(strategy_settings["massive_max_count"]),
        b1_require_20ma_gt_50ma=bool(strategy_settings["b1_require_20ma_gt_50ma"]),
        require_ma5_rising=bool(strategy_settings["require_ma5_rising"]),
        require_5ma_gt_20ma=bool(strategy_settings["require_5ma_gt_20ma"]),
        below_20ma_stop_days=int(strategy_settings["below_20ma_stop_days"]),
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
        strategy_settings=strategy_settings,
        report_mode="candidate",
    )
    report_url = f"/reports/{quote(report_path.name)}"
    return f"""
<section class="result">
  <p class="links">
    <strong>{html.escape(symbol)}</strong>
    <a href="{report_url}" target="_blank">打开完整图表</a>
  </p>
  <p class="hint">下方只用于看图确认候选股：保留 K 线、均线、成交量、KDJ 和策略信号点；收益统计请在回测页面查看。</p>
  {company_info_panel}
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
            result = latest_b_signal(
                symbol,
                bars,
                ma_length=5,
                vol_length=20,
                vol_multiplier=1.45,
                reentry_pct=4.5,
                min_price=0,
                min_avg_dollar_volume=0,
            )
            b_status = "B点" if result else "-"
            buy_signal, _, _, _, _, _ = build_ratchet_inputs(bars, 5, 20, 1.45, 4.5 / 100)
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
    requested_symbol = normalize_yahoo_symbol(field(params, "symbol", "")) if params else None
    default_symbol = requested_symbol.upper() if requested_symbol and requested_symbol.upper() in symbols else (symbols[0] if symbols else "")
    cache = price_cache_summary(symbols)
    default_event_date = default_scan_end_date().isoformat()
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
    <div class="divergence-panel">
      <div class="divergence-head">
        <strong>市场分歧事件</strong>
        <span class="hint">按 D+13 到 D+27 交易日作为观察窗口</span>
      </div>
      <form class="divergence-form" action="/watchlist/divergence/add" method="get">
        <input type="hidden" name="symbol" id="divergence-symbol-input" value="{html.escape(default_symbol)}">
        <label>事件日期<input type="date" name="event_date" value="{html.escape(default_event_date)}"></label>
        <label>方向<select name="event_type"><option value="bullish">利好</option><option value="bearish">利空</option></select></label>
        <label>分歧类型<select name="divergence_type"><option value="good_news_ignored">利好未涨</option><option value="bad_news_resilient">利空不跌</option></select></label>
        <label>级别<select name="importance"><option value="major">重大</option><option value="medium">中等</option><option value="minor">一般</option></select></label>
        <label>备注<input name="note" placeholder="财报、政策、合同、监管等"></label>
        <button type="submit">保存事件</button>
      </form>
      <div class="divergence-list" id="divergence-list">{render_divergence_event_list(default_symbol) if default_symbol else '<div class="divergence-empty">先选择一个股票。</div>'}</div>
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
      <div id="watchlist-chart" class="watchlist-chart watchlist-price-chart"></div>
      <div id="watchlist-kdj-chart" class="watchlist-chart watchlist-kdj-chart"></div>
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
let watchKdjChart = null;
let watchSeries = {{}};
const watchChartEl = document.getElementById("watchlist-chart");
const watchKdjEl = document.getElementById("watchlist-kdj-chart");
const watchTooltip = document.getElementById("watchlist-tooltip");
const watchTitle = document.getElementById("watch-chart-title");
const watchSubtitle = document.getElementById("watch-chart-subtitle");
const watchLoading = document.getElementById("watch-chart-loading");
const divergenceSymbolInput = document.getElementById("divergence-symbol-input");
const divergenceList = document.getElementById("divergence-list");
function attr(node, name, fallback = "-") {{
  return node?.getAttribute(name) || fallback;
}}
function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, ch => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[ch]));
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
  if (divergenceSymbolInput) divergenceSymbolInput.value = attr(button, "data-watch-symbol", "");
}}

function renderDivergenceEvents(events) {{
  if (!divergenceList) return;
  if (!events || !events.length) {{
    divergenceList.innerHTML = '<div class="divergence-empty">当前股票还没有记录分歧事件。</div>';
    return;
  }}
  divergenceList.innerHTML = events.map(event => {{
    const status = event.status_key || "none";
    const note = escapeHtml(event.note || "-");
    const eventDate = escapeHtml(event.event_date || "-");
    const importance = escapeHtml(event.importance_label || "-");
    const divergenceType = escapeHtml(event.divergence_type_label || "-");
    const statusLabel = escapeHtml(event.status_label || "-");
    const score = escapeHtml(event.score ?? 0);
    const dayCount = escapeHtml(event.day_count ?? "-");
    return `<div class="divergence-item divergence-${{status}}">
      <div><strong>${{eventDate}} · ${{importance}} ${{divergenceType}}</strong>
      <span>D+${{dayCount}} · ${{statusLabel}} · ${{score}}/5</span></div>
      <p>${{note}}</p>
      <a class="delete-link" href="/watchlist/divergence/delete?id=${{encodeURIComponent(event.id)}}&symbol=${{encodeURIComponent(event.symbol || watchCurrentSymbol)}}" onclick="return confirm('确认删除这条分歧事件？');">删除</a>
    </div>`;
  }}).join("");
}}

function destroyWatchChart() {{
  if (watchChart) {{
    watchChart.remove();
    watchChart = null;
    watchSeries = {{}};
  }}
  if (watchKdjChart) {{
    watchKdjChart.remove();
    watchKdjChart = null;
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
  const ma20 = watchChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, title: "20MA", priceLineVisible: false, lastValueVisible: false }});
  ma20.setData(payload.ma20 || []);
  const volume = watchChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
  volume.setData(payload.volume);
  watchChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
  const volMa = watchChart.addLineSeries({{ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false, lastValueVisible: false }});
  volMa.setData(payload.volMa);
  const volThreshold = watchChart.addLineSeries({{ color: "#f97316", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceScaleId: "", title: `${{payload.volMultiplier || ""}}x Vol`, priceLineVisible: false, lastValueVisible: false }});
  volThreshold.setData(payload.volThreshold || []);
  watchKdjChart = LightweightCharts.createChart(watchKdjEl, {{
    layout: {{ background: {{ type: "solid", color: "#ffffff" }}, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif" }},
    width: watchKdjEl.clientWidth,
    height: watchKdjEl.clientHeight,
    rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.14, bottom: 0.14 }} }},
    timeScale: {{ borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }},
    grid: {{ vertLines: {{ color: "#f1f3f6" }}, horzLines: {{ color: "#f1f3f6" }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
    handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
  }});
  watchKdjChart.addLineSeries({{ color: "#2563eb", lineWidth: 1.5, title: "K", priceLineVisible: false }}).setData(payload.kdjK || []);
  watchKdjChart.addLineSeries({{ color: "#f59e0b", lineWidth: 1.5, title: "D", priceLineVisible: false }}).setData(payload.kdjD || []);
  watchKdjChart.addLineSeries({{ color: "#7c3aed", lineWidth: 2, title: "J", priceLineVisible: false }}).setData(payload.kdjJ || []);
  watchKdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "80", priceLineVisible: false, lastValueVisible: false }}).setData(payload.kdjUpper || []);
  watchKdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "20", priceLineVisible: false, lastValueVisible: false }}).setData(payload.kdjLower || []);
  let syncingWatchRange = false;
  function syncWatchRange(source, target) {{
    source.timeScale().subscribeVisibleLogicalRangeChange(range => {{
      if (!range || syncingWatchRange) return;
      syncingWatchRange = true;
      target.timeScale().setVisibleLogicalRange(range);
      syncingWatchRange = false;
    }});
  }}
  syncWatchRange(watchChart, watchKdjChart);
  syncWatchRange(watchKdjChart, watchChart);
  candle.setMarkers(payload.markers || []);
  const rowByTime = new Map(payload.rows.map(row => [row.time, row]));
  watchChart.subscribeCrosshairMove(param => {{
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0 || param.point.x > watchChartEl.clientWidth || param.point.y > watchChartEl.clientHeight) {{ watchTooltip.style.display = "none"; return; }}
    const row = rowByTime.get(param.time);
    if (!row) {{ watchTooltip.style.display = "none"; return; }}
    const up = row.close >= row.open;
    const f = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
    const fv = value => value === null || value === undefined ? "-" : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: 0 }});
    const eventHtml = (row.events || []).map(event => `<div>分歧事件：${{escapeHtml(event.event_date)}} · ${{escapeHtml(event.importance_label)}} ${{escapeHtml(event.divergence_type_label)}} · D+${{escapeHtml(event.day_count)}}<br>${{escapeHtml(event.note || "")}}</div>`).join("");
    watchTooltip.innerHTML = `<strong>${{row.time}}</strong><div><span class="${{up ? "up" : "down"}}">开 ${{f(row.open)}} 高 ${{f(row.high)}} 低 ${{f(row.low)}} 收 ${{f(row.close)}}</span></div><div>成交量 ${{fv(row.volume)}} &nbsp; 阈值量 ${{fv(row.volThreshold)}} &nbsp; 5MA ${{f(row.ma)}} &nbsp; 20MA ${{f(row.ma20)}}</div><div>KDJ K ${{f(row.kdjK)}} &nbsp; D ${{f(row.kdjD)}} &nbsp; J ${{f(row.kdjJ)}}</div>${{eventHtml}}`;
    watchTooltip.style.display = "block";
    watchTooltip.style.left = Math.min(param.point.x + 16, watchChartEl.clientWidth - 250) + "px";
    watchTooltip.style.top = Math.max(44, param.point.y - 72) + "px";
  }});
  new ResizeObserver(entries => {{
    if (!watchChart) return;
    const rect = entries[0].contentRect;
    watchChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(watchChartEl);
  new ResizeObserver(entries => {{
    if (!watchKdjChart) return;
    const rect = entries[0].contentRect;
    watchKdjChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(watchKdjEl);
  watchChart.timeScale().fitContent();
  watchKdjChart.timeScale().fitContent();
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
    renderDivergenceEvents(payload.divergenceEvents || []);
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
const initialWatchButton = document.querySelector(`[data-watch-symbol="${{watchInitialSymbol}}"]`) || document.querySelector("[data-watch-symbol]");
if (initialWatchButton) updateWatchDetail(initialWatchButton);
if (watchInitialSymbol) loadWatchChart(watchInitialSymbol, "1y");
</script>
"""


def watchlist_chart_payload(params: dict[str, list[str]]) -> dict[str, object]:
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        return {"error": "缺少股票代码。"}
    preset = field(params, "preset", "1y").lower()
    vol_multiplier = number_field(params, "vol_multiplier", 1.45)
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
            vol_multiplier=vol_multiplier,
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
        {
            "time": str(row.get("date", "")),
            "position": "belowBar",
            "color": "#089981",
            "shape": "arrowUp",
            "text": "买",
        }
        for row in equity_curve
        if str(row.get("buy_action", ""))
    ] + [
        {"time": trade.exit_date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": "卖"}
        for trade in trades
    ]
    symbol_events = divergence_events_for_symbol(symbol, date.fromisoformat(bars[-1].date))
    event_by_time: dict[str, list[dict[str, object]]] = {}
    bar_dates = [date.fromisoformat(bar.date) for bar in bars]
    for event in symbol_events:
        try:
            event_day = date.fromisoformat(str(event.get("event_date", "")))
        except ValueError:
            continue
        marker_day = None
        for bar_day in bar_dates:
            if bar_day >= event_day:
                marker_day = bar_day
                break
        if marker_day is None:
            continue
        marker_time = marker_day.isoformat()
        marker_color = "#f97316" if event.get("event_type") == "bearish" else "#2962ff"
        marker_text = "分歧"
        markers.append(
            {
                "time": marker_time,
                "position": "aboveBar",
                "color": marker_color,
                "shape": "circle",
                "text": marker_text,
            }
        )
        display_event = dict(event)
        display_event["marker_time"] = marker_time
        event_by_time.setdefault(marker_time, []).append(display_event)
    trade_exit_dates = {trade.exit_date for trade in trades}
    rows = []
    ma_points = []
    ma20_points = []
    vol_ma_points = []
    vol_threshold_points = []
    k_values, d_values, j_values = calculate_kdj(bars)
    k_points = []
    d_points = []
    j_points = []
    dynamic_points = []
    trend_stop_points = []
    volume_points = []
    for i, bar in enumerate(bars):
        row = equity_curve[i]
        ma = None if row["ma"] == "" else float(row["ma"])
        ma20 = None if row.get("ma20", "") in ("", None) else float(row["ma20"])
        vol_ma = None if row["vol_ma"] == "" else float(row["vol_ma"])
        dynamic_stop = None if row.get("dynamic_stop", "") in ("", None) else float(row["dynamic_stop"])
        trend_stop = None if row.get("trend_stop", "") in ("", None) else float(row["trend_stop"])
        rows.append(
            {
                "time": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "ma": ma,
                "ma20": ma20,
                "volMa": vol_ma,
                "volThreshold": vol_ma * vol_multiplier if vol_ma is not None else None,
                "kdjK": k_values[i],
                "kdjD": d_values[i],
                "kdjJ": j_values[i],
                "dynamicStop": dynamic_stop,
                "trendStop": trend_stop,
                "events": event_by_time.get(bar.date, []),
            }
        )
        if ma is not None:
            ma_points.append({"time": bar.date, "value": ma})
        if ma20 is not None:
            ma20_points.append({"time": bar.date, "value": ma20})
        if vol_ma is not None:
            vol_ma_points.append({"time": bar.date, "value": vol_ma})
            vol_threshold_points.append({"time": bar.date, "value": vol_ma * vol_multiplier})
        if k_values[i] is not None:
            k_points.append({"time": bar.date, "value": k_values[i]})
        if d_values[i] is not None:
            d_points.append({"time": bar.date, "value": d_values[i]})
        if j_values[i] is not None:
            j_points.append({"time": bar.date, "value": j_values[i]})
        if dynamic_stop is not None:
            dynamic_points.append({"time": bar.date, "value": dynamic_stop})
        if trend_stop is not None:
            trend_stop_points.append({"time": bar.date, "value": trend_stop})
        volume_points.append({"time": bar.date, "value": bar.volume, "color": "rgba(8,153,129,0.42)" if bar.close >= bar.open else "rgba(242,54,69,0.42)"})
        position = float(row.get("position_shares", 0) or 0)
        if int(row.get("buy_signal", 0)):
            stage = str(row.get("buy_stage", "B") or "B")
            markers.append({"time": bar.date, "position": "belowBar", "color": "#2563eb" if stage == "B2" else "#84cc16", "shape": "circle", "text": stage})
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
        "ma20": ma20_points,
        "volMa": vol_ma_points,
        "volThreshold": vol_threshold_points,
        "volMultiplier": vol_multiplier,
        "kdjK": k_points,
        "kdjD": d_points,
        "kdjJ": j_points,
        "kdjUpper": [{"time": bar.date, "value": 80} for bar in bars],
        "kdjLower": [{"time": bar.date, "value": 20} for bar in bars],
        "dynamicStop": dynamic_points,
        "trendStop": trend_stop_points,
        "markers": sorted(markers, key=lambda item: str(item["time"])),
        "rows": rows,
        "divergenceEvents": symbol_events,
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
  {render_candidate_table(display_rows, params)}
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
        vol_high_days = int(number_field(params, "vol_high_days", 3))
        vol_high_multiplier = number_field(params, "vol_high_multiplier", 1.0)
        massive_window = int(number_field(params, "massive_window", 7))
        massive_min_count = int(number_field(params, "massive_min_count", 1))
        massive_max_count = int(number_field(params, "massive_max_count", 2))
        b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", True)
        require_ma5_rising = checkbox_field(params, "require_ma5_rising", True)
        require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", True)
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
                        vol_high_days,
                        vol_high_multiplier,
                        massive_window,
                        massive_min_count,
                        massive_max_count,
                        b1_require_20ma_gt_50ma,
                        require_ma5_rising,
                        require_5ma_gt_20ma,
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
        max_symbols = int(number_field(params, "max_symbols", ASHARE_DEFAULT_MAX_SCAN_SYMBOLS))
        max_workers = max(1, min(12, int(number_field(params, "max_workers", 6))))
        j_threshold = number_field(params, "j_threshold", 14.0)
        min_avg_amount_20d = number_field(params, "min_avg_amount_20d_100m", 1.0) * 100_000_000
        min_control_amount_20d = number_field(params, "min_control_amount_20d_100m", 2.0) * 100_000_000
        vol_high_days = int(number_field(params, "vol_high_days", 2))
        vol_high_multiplier = number_field(params, "vol_high_multiplier", 1.0)
        vol_multiplier = number_field(params, "vol_multiplier", 1.45)
        massive_window = int(number_field(params, "massive_window", 7))
        massive_min_count = int(number_field(params, "massive_min_count", 1))
        reentry_pct = number_field(params, "reentry_pct", 4.5) / 100
        strong_volume_score = number_field(params, "strong_volume_score", 4.0)
        medium_volume_score = number_field(params, "medium_volume_score", 2.5)
        b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
        require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
        require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
        selected_boards = normalize_ashare_boards(params.get("boards", []))
        board_label = ashare_board_filter_label(selected_boards)

        def universe_progress(message: str) -> None:
            set_job(job_id, stage="拉取股票池", message=message, detail=f"最低市值 {min_market_cap:g} 亿元，板块：{board_label}，最多扫描 {max_symbols} 只。")

        fetch_limit = max_symbols if set(selected_boards) == set(ASHARE_BOARD_LABELS) else max(1000, max_symbols * 4)
        universe, universe_source, market_cap_filter_applied = load_ashare_universe_for_scan(min_market_cap, fetch_limit, universe_progress)
        universe = filter_ashare_universe_by_board(universe, selected_boards)[: max(1, max_symbols)]
        filter_text = "市值过滤已生效" if market_cap_filter_applied else "当前数据源没有总市值字段，市值过滤未生效"
        filter_text = f"{filter_text}；板块：{board_label}"
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
            snapshot = latest_ashare_signal(
                item.symbol,
                j_threshold,
                fetch_name_value=False,
                min_avg_amount_20d=min_avg_amount_20d,
                min_control_amount_20d=min_control_amount_20d,
                vol_multiplier=vol_multiplier,
                vol_high_days=vol_high_days,
                vol_high_multiplier=vol_high_multiplier,
                massive_window=massive_window,
                massive_min_count=massive_min_count,
                reentry_pct=reentry_pct,
                strong_volume_score=strong_volume_score,
                medium_volume_score=medium_volume_score,
                b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
                require_ma5_rising=require_ma5_rising,
                require_5ma_gt_20ma=require_5ma_gt_20ma,
            )
            snapshot.name = item.name
            snapshot.sector = item.sector
            return snapshot

        stopped = False
        with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(universe)))) as executor:
            universe_iter = iter(universe)
            future_map = {}
            worker_limit = min(max_workers, max(1, len(universe)))

            def submit_next() -> bool:
                if job_stop_requested(job_id):
                    return False
                try:
                    item = next(universe_iter)
                except StopIteration:
                    return False
                future_map[executor.submit(run_one, item)] = item
                return True

            for _ in range(worker_limit):
                submit_next()

            while future_map:
                if job_stop_requested(job_id):
                    stopped = True
                    for future in future_map:
                        future.cancel()
                    set_job(job_id, stage="正在终止", message="正在终止，等待当前批次结束", current="")
                    break
                done, _ = wait(future_map, timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    item = future_map.pop(future)
                    set_job(job_id, stage="扫描日线", message="正在扫描 A 股日线信号", current=f"{item.symbol} {item.name}")
                    try:
                        snapshot = future.result()
                        if snapshot.signal:
                            candidates.append(snapshot)
                    except Exception as exc:
                        errors.append((f"{item.symbol} {item.name}", str(exc)))
                    scanned += 1
                    set_job(job_id, scanned=scanned, candidates=len(candidates), errors=len(errors))
                    submit_next()

            if stopped:
                for future in list(future_map):
                    if future.cancelled():
                        future_map.pop(future, None)
                for future, item in list(future_map.items()):
                    if not future.cancelled():
                        try:
                            snapshot = future.result(timeout=0)
                            if snapshot.signal:
                                candidates.append(snapshot)
                            scanned += 1
                        except Exception:
                            pass
                set_job(job_id, scanned=scanned, candidates=len(candidates), errors=len(errors))

        if stopped:
            rating_order = {"Strong": 0, "Medium": 1, "Watch": 2, "None": 3}
            candidates.sort(key=lambda row: (rating_order.get(row.candidate_rating, 9), -row.volume_score, row.j_value))
            save_latest_ashare_scan(candidates, errors, scanned, universe_source, market_cap_filter_applied, params)
            result_html = render_ashare_scan_result(candidates, errors, scanned, universe_source, market_cap_filter_applied, params)
            set_job(
                job_id,
                status="stopped",
                stage="已终止",
                message="A 股扫描已终止，当前结果已保留",
                detail=f"数据源：{universe_source}",
                data_source=universe_source,
                current="",
                scanned=scanned,
                candidates=len(candidates),
                errors=len(errors),
                result_html=result_html,
            )
            return

        rating_order = {"Strong": 0, "Medium": 1, "Watch": 2, "None": 3}
        candidates.sort(key=lambda row: (rating_order.get(row.candidate_rating, 9), -row.volume_score, row.j_value))
        save_latest_ashare_scan(candidates, errors, scanned, universe_source, market_cap_filter_applied, params)
        result_html = render_ashare_scan_result(candidates, errors, scanned, universe_source, market_cap_filter_applied, params)
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
        if job_stop_requested(job_id):
            set_job(job_id, status="stopped", stage="已终止", message="A 股扫描已终止", detail="已按用户请求终止。", current="")
            return
        set_job(job_id, status="error", stage="失败", message="A 股扫描失败", detail="请查看错误信息；通常是外部数据源返回异常或网络超时。", error=str(exc))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        raw_path = parsed.path
        market = "us"
        route_path = raw_path
        if raw_path == "/" or raw_path == "/cache/clear":
            market = "global"
            route_path = raw_path
        elif raw_path == ASHARE_ROUTE:
            market = "cn"
            route_path = "/scanner"
        elif raw_path == "/us" or raw_path.startswith("/us/"):
            market = "us"
            route_path = raw_path[3:] or "/"
        elif raw_path == "/cn" or raw_path.startswith("/cn/"):
            market = "cn"
            route_path = raw_path[3:] or "/"
        try:
            if market == "global":
                if route_path == "/cache/clear":
                    self.clear_cache(params)
                else:
                    self.send_bytes(page_shell(render_action_dashboard(params), "home", "global"))
            elif market == "cn":
                if route_path in ("/", ""):
                    self.redirect("/cn/scanner")
                elif route_path == "/suggest":
                    self.send_json({"suggestions": suggest_ashare_symbols(field(params, "q", ""), int(number_field(params, "limit", 12)))})
                elif route_path == "/scanner":
                    content = render_ashare_scanner(params)
                    self.send_bytes(content.encode("utf-8") if field(params, "embed", "0") == "1" else page_shell(content, "scanner", "cn"))
                elif route_path == "/scan/start":
                    self.start_ashare_scan_job(params)
                elif route_path == "/scan/status":
                    self.ashare_scan_job_status(params)
                elif route_path == "/scan/active":
                    self.ashare_scan_job_active()
                elif route_path == "/scan/stop":
                    self.stop_ashare_scan_job(params)
                elif route_path == "/scan/latest":
                    self.send_bytes(page_shell(latest_ashare_scan_to_html(), "scanner", "cn"))
                elif route_path == "/scan/delete":
                    self.delete_ashare_scan_result()
                elif route_path == "/watchlist":
                    self.send_bytes(page_shell(render_ashare_watchlist_page(params), "watchlist", "cn"))
                elif route_path == "/watchlist/add":
                    self.add_ashare_watchlist_item(params)
                elif route_path == "/watchlist/delete":
                    self.delete_ashare_watchlist_item(params)
                elif route_path == "/watchlist/chart":
                    self.ashare_watchlist_chart(params)
                elif route_path == "/backtest":
                    self.send_bytes(page_shell(render_ashare_backtest_form(params), "backtest", "cn"))
                elif route_path == "/run":
                    self.send_bytes(page_shell(run_ashare_strategy(params), "backtest", "cn"))
                elif route_path == "/batch":
                    self.send_bytes(page_shell(render_market_placeholder("batch"), "batch", "cn"))
                else:
                    self.send_error(404)
            elif route_path == "/":
                self.redirect("/us/scanner")
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
            elif route_path == "/watchlist/divergence/add":
                self.add_watchlist_divergence_event(params)
            elif route_path == "/watchlist/divergence/delete":
                self.delete_watchlist_divergence_event(params)
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
            elif route_path == "/scan/active":
                self.scan_job_active()
            elif route_path == "/candidate":
                self.send_bytes(render_candidate_detail(params).encode("utf-8"))
            elif route_path.startswith("/reports/"):
                self.send_report(route_path)
            else:
                self.send_error(404)
        except Exception as exc:
            active = "scanner" if route_path in ("/scanner", "/scan") or route_path.startswith("/scan/") else "watchlist" if route_path.startswith("/watchlist") else "batch" if route_path.startswith("/batch") else "home" if route_path in ("/", "") else "backtest"
            form = render_action_dashboard(params) if market == "global" else render_ashare_scanner(params) if market == "cn" and active in ("scanner", "home") else render_ashare_watchlist_page(params) if market == "cn" and active == "watchlist" else render_market_placeholder(active) if market == "cn" else render_scanner_form(params) if active in ("scanner", "home") else render_watchlist_page(params) if active == "watchlist" else render_batch_form(params) if active == "batch" else render_backtest_form(params)
            self.send_bytes(page_shell(form + f'<div class="error">{html.escape(str(exc))}</div>', active, market), 500)

    def clear_cache(self, params: dict[str, list[str]]) -> None:
        area = field(params, "area", "")
        message = clear_cache_area(area)
        self.redirect(f"/?cache_message={quote(message)}")

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

    def add_watchlist_divergence_event(self, params: dict[str, list[str]]) -> None:
        try:
            event = add_divergence_event(params)
            content = render_watchlist_page({"symbol": [str(event["symbol"])]})
        except ValueError as exc:
            content = render_watchlist_page(params) + f'<div class="error">{html.escape(str(exc))}</div>'
        self.send_bytes(page_shell(content, "watchlist", "us"))

    def delete_watchlist_divergence_event(self, params: dict[str, list[str]]) -> None:
        delete_divergence_event(field(params, "id", ""))
        symbol = field(params, "symbol", "")
        self.send_bytes(page_shell(render_watchlist_page({"symbol": [symbol]}), "watchlist", "us"))

    def start_ashare_scan_job(self, params: dict[str, list[str]]) -> None:
        active = active_scan_job("cn")
        if active:
            job_id, _ = active
            self.send_json(
                {
                    "status": "error",
                    "error": f"已有 A 股扫描任务正在运行：{job_id}。请等待完成，或先终止当前 A 股任务。",
                },
                status=409,
            )
            return
        job_id = f"ashare-{uuid.uuid4().hex[:10]}"
        set_job(job_id, market="cn", status="queued", message="排队中", total=0, scanned=0, candidates=0, errors=0, current="", pause_requested=False, stop_requested=False)
        worker = threading.Thread(target=execute_ashare_scan_job, args=(job_id, params), daemon=True)
        worker.start()
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "queued"}))

    def ashare_scan_job_status(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "job_id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到 A 股扫描任务"}, status=404)
            return
        self.send_json(normalize_job_payload(job_id, job))

    def ashare_scan_job_active(self) -> None:
        latest_job = latest_job_for_market("cn", include_finished=True)
        if latest_job:
            job_id, job = latest_job
            self.send_json(normalize_job_payload(job_id, job))
            return
        self.send_json({"status": "idle", "job_id": ""})

    def stop_ashare_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "job_id", field(params, "id", ""))
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到 A 股扫描任务"}, status=404)
            return
        if not str(job_id).startswith("ashare-"):
            self.send_json({"status": "error", "error": "不是 A 股扫描任务"}, status=400)
            return
        if job.get("status") in ("done", "error", "stopped"):
            self.send_json(normalize_job_payload(job_id, job))
            return
        set_job(job_id, stop_requested=True, pause_requested=False, status="stopping", stage="正在终止", message="正在终止，当前批次完成后保留结果")
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "stopping"}))

    def delete_ashare_scan_result(self) -> None:
        deleted = delete_latest_ashare_scan()
        message = "已删除当前 A 股扫描结果。" if deleted else "当前没有可删除的 A 股扫描结果。"
        content = render_ashare_scanner({}) + f'<section class="result"><p class="hint">{html.escape(message)}</p></section>'
        self.send_bytes(page_shell(content, "scanner", "cn"))

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

    def ashare_watchlist_chart(self, params: dict[str, list[str]]) -> None:
        symbol = field(params, "symbol", "")
        if not symbol.strip():
            self.send_json({"error": "缺少股票代码"}, status=400)
            return
        try:
            j_threshold = number_field(params, "j_threshold", 14.0)
            payload = ashare_chart_payload(symbol, j_threshold)
            payload["j_threshold"] = j_threshold
            self.send_json(payload)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def start_scan_job(self, params: dict[str, list[str]]) -> None:
        scan_end = default_scan_end_date()
        start = field(params, "start", default_scan_start_date(scan_end).isoformat())
        end = field(params, "end", scan_end.isoformat())
        try:
            validate_scan_range(start, end)
        except ValueError as exc:
            self.send_json({"status": "error", "error": str(exc)}, status=400)
            return
        active = active_scan_job("us")
        if active:
            active_id, active_job = active
            payload = normalize_job_payload(active_id, active_job)
            payload.update({"status": "busy", "error": "已有美股扫描任务正在运行，请等待完成，或先暂停/终止当前美股任务。", "active_job_id": active_id})
            self.send_json(payload, status=409)
            return
        job_id = uuid.uuid4().hex
        set_job(job_id, market="us", status="queued", message="排队中", total=0, scanned=0, candidates=0, errors=0, current="", pause_requested=False, stop_requested=False)
        worker = threading.Thread(target=execute_scan_job, args=(job_id, params), daemon=True)
        worker.start()
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "queued"}))

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
            self.send_json(normalize_job_payload(job_id, job))
            return
        set_job(job_id, pause_requested=True, status="pausing", message="正在暂停，当前股票处理完后显示结果")
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "pausing"}))

    def resume_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error"):
            self.send_json(normalize_job_payload(job_id, job))
            return
        set_job(job_id, pause_requested=False, status="running", message="继续扫描 B 点信号")
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "running"}))

    def stop_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error", "stopped"):
            self.send_json(normalize_job_payload(job_id, job))
            return
        set_job(job_id, stop_requested=True, pause_requested=False, status="stopping", message="正在终止，当前股票处理完后保留结果")
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "stopping"}))

    def scan_job_status(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        self.send_json(normalize_job_payload(job_id, job))

    def scan_job_active(self) -> None:
        latest_job = latest_job_for_market("us", include_finished=True)
        if latest_job:
            job_id, job = latest_job
            self.send_json(normalize_job_payload(job_id, job))
            return
        self.send_json({"status": "idle", "job_id": ""})

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

    def redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
