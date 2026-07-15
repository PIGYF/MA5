from __future__ import annotations

import csv
import html
import importlib.util
import json
import mimetypes
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from backend_storage import atomic_write_json, locked_path
from backtest import (
    Bar,
    PRICE_CACHE_DIR,
    PRICE_CACHE_MAX_BARS,
    SPLIT_CACHE_PATH,
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
    slice_bars,
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
from market_calendar import next_trading_day
from ashare_lab import (
    ASHARE_ROUTE,
    ASHARE_BOARD_LABELS,
    ASHARE_PRICE_CACHE_DIR,
    AShareSignalSnapshot,
    ashare_board_filter_label,
    ashare_chart_payload,
    ashare_required_latest_date,
    ashare_limit_pct,
    ashare_to_backtest_bars,
    fetch_ashare_profile,
    fetch_ashare_bars,
    filter_ashare_universe_by_board,
    latest_ashare_signal,
    load_ashare_universe_for_scan,
    normalize_ashare_display_name,
    normalize_ashare_boards,
    read_ashare_price_cache,
    resolve_ashare_symbol_query,
    scan_ashare_candidates,
    suggest_ashare_symbols,
)
from scan_next_b import SignalResult, latest_b_signal, load_symbols, unique_symbols, write_html
from task_runtime import (
    active_scan_job,
    append_task_history,
    classify_scan_error,
    clear_jobs_for_market,
    get_job,
    job_pause_requested,
    job_stop_requested,
    latest_job_for_market,
    load_task_history,
    normalize_job_payload,
    set_job,
    summarize_error_categories,
)


def load_local_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()


FRONTEND_DIST_DIR = Path(__file__).resolve().parent / "frontend" / "dist"
ASHARE_DEFAULT_MAX_SCAN_SYMBOLS = 6000


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


def json_safe(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__") and value.__class__.__module__ != "builtins":
        return json_safe(vars(value))
    return value


def signal_result_api_row(row: SignalResult | dict[str, object]) -> dict[str, object]:
    data = dict(row.__dict__ if isinstance(row, SignalResult) else row)
    symbol = str(data.get("symbol", "")).upper()
    cached = merge_us_company_profile(data, symbol) if symbol else data
    data["symbol"] = symbol
    data["company_display_name"] = us_company_display_name(symbol, str(cached.get("company_name") or data.get("company_name") or ""))
    data["sector_zh"] = us_sector_zh(str(cached.get("sector") or data.get("sector") or ""))
    data["industry_zh"] = us_industry_zh(str(cached.get("industry") or data.get("industry") or ""))
    data["market_cap_billion"] = round(float(cached.get("market_cap") or data.get("market_cap") or 0) / 1_000_000_000, 2)
    data["signal_label"] = {"B1_trend_confirm": "B1", "B2_reentry": "B2"}.get(str(data.get("signal_type") or ""), str(data.get("signal_type") or "-"))
    data["watch_note"] = " ".join(part for part in (str(data.get("technical_rating") or ""), str(data.get("signal_label") or "")) if part)
    return json_safe(data)  # type: ignore[return-value]


def latest_scan_api_payload() -> dict[str, object]:
    latest = load_latest_scan()
    if not latest:
        return {"ok": True, "has_result": False, "latest": None}
    candidates = [
        signal_result_api_row(row)
        for row in latest.get("candidates", [])
        if isinstance(row, dict)
    ]
    candidates = annotate_candidate_history("us", str(latest.get("signal_date", "")), candidates)
    cache = price_cache_summary([str(row.get("symbol", "")) for row in candidates]) if candidates else {"cached_symbols": 0, "latest": "-", "size_mb": 0}
    return {
        "ok": True,
        "has_result": True,
        "latest": {
            "signal_date": latest.get("signal_date", ""),
            "planned_trade_date": latest.get("planned_trade_date", ""),
            "created_at": latest.get("created_at", ""),
            "source": latest.get("source", ""),
            "summary": latest.get("summary", {}),
            "report": latest.get("report", ""),
            "csv": latest.get("csv", ""),
            "params": latest.get("params", {}),
            "candidates": candidates,
            "errors": latest.get("errors", []),
            "cache": cache,
        },
    }


def scanner_bootstrap_api_payload(params: dict[str, list[str]]) -> dict[str, object]:
    scan_end = default_scan_end_date()
    return {
        "ok": True,
        "defaults": {
            "universe_source": "auto",
            "asset_type": "stocks",
            "min_market_cap_billion": DEFAULT_MIN_MARKET_CAP_100M_USD,
            "max_market_cap_billion": 0,
            "min_screener_volume": 500000,
            "max_symbols": DEFAULT_MAX_SCAN_SYMBOLS,
            "max_workers": 6,
            "start": default_scan_start_date(scan_end).isoformat(),
            "end": scan_end.isoformat(),
            "min_price": 5,
            "min_avg_dollar_volume": 20000000,
            "earnings_filter": "show",
            "hide_weak": DEFAULT_HIDE_WEAK_CANDIDATES,
            "ma_length": 5,
            "vol_length": 20,
            "vol_high_days": 3,
            "vol_high_multiplier": 1.0,
            "vol_multiplier": 1.45,
            "massive_window": 7,
            "massive_min_count": 1,
            "reentry_pct": 4.5,
            "require_ma5_rising": False,
            "require_5ma_gt_20ma": False,
            "b1_require_20ma_gt_50ma": False,
            "secondary_big_red_b1": False,
            "secondary_above_ma5_3d": False,
        },
        "market_environment": json_safe(market_environment()),
        "latest_scan": latest_scan_api_payload(),
    }


def ashare_latest_scan_api_payload() -> dict[str, object]:
    latest = load_latest_ashare_scan()
    if latest:
        latest = dict(latest)
        latest["candidates"] = annotate_candidate_history("cn", str(latest.get("signal_date", "")), [dict(row) for row in latest.get("candidates", []) if isinstance(row, dict)])
        latest["cache"] = {"cached_symbols": sum(1 for row in latest["candidates"] if read_ashare_price_cache(str(row.get("symbol", "")))), "latest": latest.get("signal_date", "-")}
    return {"ok": True, "latest": json_safe(latest) if latest else None}


def ashare_scanner_bootstrap_api_payload() -> dict[str, object]:
    latest = load_latest_ashare_scan()
    signal_date = str((latest or {}).get("signal_date", ashare_required_latest_date(date.today()).isoformat()))
    return {
        "ok": True,
        "defaults": {
            "min_market_cap": 50,
            "max_symbols": ASHARE_DEFAULT_MAX_SCAN_SYMBOLS,
            "max_workers": 6,
            "min_avg_amount_20d_100m": 1,
            "min_control_amount_20d_100m": 2,
            "j_threshold": 14,
            "vol_high_days": 2,
            "vol_high_multiplier": 1.0,
            "vol_multiplier": 1.45,
            "massive_window": 7,
            "massive_min_count": 1,
            "reentry_pct": 4.5,
            "strong_volume_score": 4.0,
            "medium_volume_score": 2.5,
            "boards": list(ASHARE_BOARD_LABELS),
            "require_ma5_rising": False,
            "require_5ma_gt_20ma": False,
            "b1_require_20ma_gt_50ma": False,
            "secondary_big_red_b1": False,
            "secondary_above_ma5_3d": False,
        },
        "boards": [{"value": key, "label": label} for key, label in ASHARE_BOARD_LABELS.items()],
        "market_environment": {
            "state": "盘后复盘",
            "symbol": "A股",
            "date": signal_date,
            "tone": "neutral",
        },
        "latest_scan": ashare_latest_scan_api_payload(),
    }


ICON_SVG: dict[str, str] = {
    "home": '<path d="M3 10.8 12 3l9 7.8"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/>',
    "scan": '<path d="M4 7V4h3"/><path d="M17 4h3v3"/><path d="M20 17v3h-3"/><path d="M7 20H4v-3"/><path d="M8 12h8"/><path d="M12 8v8"/>',
    "star": '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1L12 16.9l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3z"/>',
    "chart": '<path d="M4 19h16"/><path d="M6 15l4-4 3 3 5-7"/><path d="M18 7h-4"/><path d="M18 7v4"/>',
    "candles": '<path d="M7 4v16"/><path d="M17 4v16"/><rect x="5" y="8" width="4" height="7" rx="1"/><rect x="15" y="6" width="4" height="10" rx="1"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/>',
    "download": '<path d="M12 3v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
    "trash": '<path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M6 7l1 14h10l1-14"/><path d="M9 7V4h6v3"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "play": '<path d="M8 5v14l11-7-11-7z"/>',
    "pause": '<path d="M8 5v14"/><path d="M16 5v14"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v6h-6"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "back": '<path d="M19 12H5"/><path d="m12 5-7 7 7 7"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "alert": '<path d="M12 3 2.8 19h18.4L12 3z"/><path d="M12 8v5"/><path d="M12 17h.01"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/>',
}


def icon_svg(name: str, cls: str = "icon") -> str:
    body = ICON_SVG.get(name, "")
    if not body:
        return ""
    return f'<svg class="{html.escape(cls)}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{body}</svg>'


def icon_label(name: str, label: str, cls: str = "icon") -> str:
    return f'{icon_svg(name, cls)}<span>{html.escape(label)}</span>'


def render_data_health_panel(market: str) -> str:
    if market == "cn":
        packages = [("pytdx", "pytdx"), ("yfinance备用", "yfinance")]
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
        note = "A股选股股票池优先使用通达信，并用 Tencent 行情补充市值；这里显示依赖和缓存是否可用。"
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


MARKET_ENV_REFRESH_LOCK = threading.Lock()
MARKET_ENV_REFRESHING: set[str] = set()


def latest_cached_bar_date(symbol: str) -> str:
    bars = read_price_cache(symbol)
    return max((bar.date for bar in bars), default="")


def schedule_market_environment_refresh(symbols: list[str], end: date) -> None:
    stale_symbols = [symbol for symbol in symbols if latest_cached_bar_date(symbol) < end.isoformat()]
    if not stale_symbols:
        return
    refresh_key = ",".join(sorted(stale_symbols))
    with MARKET_ENV_REFRESH_LOCK:
        if refresh_key in MARKET_ENV_REFRESHING:
            return
        MARKET_ENV_REFRESHING.add(refresh_key)

    def refresh() -> None:
        start = end - timedelta(days=180)
        try:
            for symbol in stale_symbols:
                try:
                    fetch_bars("yfinance", symbol, start.isoformat(), end.isoformat(), "qfq", None)
                except Exception:
                    pass
        finally:
            with MARKET_ENV_REFRESH_LOCK:
                MARKET_ENV_REFRESHING.discard(refresh_key)

    threading.Thread(target=refresh, daemon=True).start()


def ensure_market_environment_cache(symbols: list[str], end: date, min_bars: int = 50) -> None:
    start = end - timedelta(days=180)
    for symbol in symbols:
        try:
            cached = [bar for bar in read_price_cache(symbol) if date.fromisoformat(bar.date) <= end]
            if len(cached) >= min_bars and max((bar.date for bar in cached), default="") >= end.isoformat():
                continue
            fetch_bars("yfinance", symbol, start.isoformat(), end.isoformat(), "qfq", None)
        except Exception:
            continue


def market_environment(symbol: str = "QQQ") -> dict[str, object]:
    end = default_scan_end_date()
    macro = macro_risk_state(date.today())
    env_symbols = [symbol, "^VIX"]
    ensure_market_environment_cache(env_symbols, end)
    schedule_market_environment_refresh(env_symbols, end)
    try:
        bars = [bar for bar in read_price_cache(symbol) if date.fromisoformat(bar.date) <= end]
        bars = bars[-120:]
        if len(bars) < 50:
            raise RuntimeError("本地大盘缓存不足")
        vix_value = 0.0
        vix_label = "Unavailable"
        try:
            vix_bars = [bar for bar in read_price_cache("^VIX") if date.fromisoformat(bar.date) <= end]
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
    tone_name = str(env.get("tone", "neutral"))
    state_icon = "check" if tone_name == "good" else "alert" if tone_name in {"bad", "warn"} else "globe"
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
      <strong>{icon_svg(state_icon)}{html.escape(str(env.get("state", "Unavailable")))}</strong>
      <span>{html.escape(str(env.get("symbol", "QQQ")))} {html.escape(str(env.get("date", "-")))}</span>
    </div>
    <p>{html.escape(str(env.get("symbol", "QQQ")))} 距20MA {float(env.get("dist20", 0.0)):.2f}% / 距50MA {float(env.get("dist50", 0.0)):.2f}% / 20MA {html.escape(str(env.get("ma20_direction", "-")))} / VIX {float(env.get("vix", 0.0)):.1f} {html.escape(str(env.get("vix_label", "")))}。{html.escape(str(env.get("message", "")))}</p>
  </div>
  <div class="macro-box macro-tone-{macro_tone}">
    <div>
      <strong>{icon_svg("alert" if macro_tone in {"bad", "warn"} else "check")}{html.escape(str(macro.get("label", "宏观日历")))}</strong>
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
    atomic_write_json(WATCHLIST_PATH, payload, indent=2)


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
    atomic_write_json(DIVERGENCE_EVENTS_PATH, payload, indent=2)


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
                "name": normalize_ashare_display_name(str(raw.get("name", "") or "")),
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
                "name": normalize_ashare_display_name(str(item.get("name", "") or ""))[:80],
                "sector": str(item.get("sector", "") or "").strip()[:80],
                "group": str(item.get("group", "") or "观察").strip()[:40],
                "note": str(item.get("note", "") or "").strip()[:240],
                "added_at": str(item.get("added_at", "") or time.strftime("%Y-%m-%d %H:%M:%S")),
            }
        )
    atomic_write_json(path, {"items": clean_items, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2)


def ashare_snapshot_to_dict(row: AShareSignalSnapshot) -> dict[str, object]:
    payload = dict(row.__dict__)
    payload["name"] = normalize_ashare_display_name(str(payload.get("name", "") or ""))
    return payload


def load_latest_ashare_scan() -> dict[str, object] | None:
    path = ashare_latest_scan_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        expected_signal_date = ashare_required_latest_date(date.today()).isoformat()
        if str(payload.get("signal_date", "")) != expected_signal_date:
            return None
        for candidate in payload.get("candidates", []):
            if isinstance(candidate, dict):
                candidate["name"] = normalize_ashare_display_name(str(candidate.get("name", "") or ""))
        return payload
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
    signal_date = max(signal_dates) if signal_dates else ashare_required_latest_date(date.today()).isoformat()
    signal_day = date.fromisoformat(signal_date)
    payload = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": signal_date,
        "planned_trade_date": next_trading_day(signal_day, "cn").isoformat(),
        "source": universe_source,
        "market_cap_filter_applied": market_cap_filter_applied,
        "summary": {"scanned": scanned, "candidates": len(candidates), "failed": len(errors)},
        "params": {key: values[-1] if len(values) == 1 else values for key, values in params.items()},
        "candidates": [ashare_snapshot_to_dict(row) for row in candidates],
        "errors": [{"symbol": symbol, "reason": reason} for symbol, reason in errors],
    }
    atomic_write_json(path, payload, indent=2)
    update_scan_history_index("cn", signal_date, [row.symbol for row in candidates])


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
    name = normalize_ashare_display_name(name)
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


def mixed_cache_summary(paths: list[Path], directories: list[Path] | None = None) -> dict[str, object]:
    files = [path for path in paths if path.exists() and path.is_file()]
    for directory in directories or []:
        if directory.exists():
            files.extend([item for item in directory.iterdir() if item.is_file()])
    return file_group_summary(files)


def cache_dashboard_summary() -> dict[str, dict[str, object]]:
    us_market_paths = [NASDAQ_CACHE_PATH, EARNINGS_CACHE_PATH, SPLIT_CACHE_PATH, US_COMPANY_PROFILE_CACHE_PATH]
    if LEGACY_NASDAQ_CACHE_PATH != NASDAQ_CACHE_PATH:
        us_market_paths.append(LEGACY_NASDAQ_CACHE_PATH)
    ashare_cache_paths = [DATA_DIR / "ashare" / "universe_cache.json", DATA_DIR / "ashare" / "sector_map.json"]
    latest_scan_paths = [LATEST_SCAN_PATH, ashare_latest_scan_path()]
    return {
        "reports": directory_file_summary(REPORT_DIR),
        "prices": directory_file_summary(PRICE_CACHE_DIR),
        "us_market": file_group_summary(us_market_paths),
        "ashare": mixed_cache_summary(ashare_cache_paths, [ASHARE_PRICE_CACHE_DIR]),
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
        deleted = delete_files([NASDAQ_CACHE_PATH, LEGACY_NASDAQ_CACHE_PATH, EARNINGS_CACHE_PATH, SPLIT_CACHE_PATH, US_COMPANY_PROFILE_CACHE_PATH])
        return f"已清理美股市场/财报缓存 {deleted} 个。"
    if area == "ashare":
        deleted = delete_files([DATA_DIR / "ashare" / "universe_cache.json", DATA_DIR / "ashare" / "sector_map.json"])
        deleted += delete_directory_files(ASHARE_PRICE_CACHE_DIR)
        return f"已清理 A 股股票池/行业/K线缓存 {deleted} 个。"
    if area == "latest":
        us_deleted = delete_latest_scan()
        cn_deleted = 1 if delete_latest_ashare_scan() else 0
        return f"已清理最新扫描结果 {us_deleted + cn_deleted} 个相关文件。"
    return "未知缓存类型，未执行清理。"


def watchlist_items_with_performance(items: list[dict[str, str]], market: str) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for item in items:
        row: dict[str, object] = dict(item)
        added_date = str(item.get("added_at", "") or "")[:10]
        try:
            bars = read_ashare_price_cache(item["symbol"]) if market == "cn" else read_price_cache(item["symbol"])
            eligible = [bar for bar in bars if bar.date >= added_date] if added_date else []
            if eligible and eligible[0].close and bars[-1].close:
                row.update(
                    {
                        "added_close": round(float(eligible[0].close), 4),
                        "latest_close": round(float(bars[-1].close), 4),
                        "performance_pct": round((float(bars[-1].close) / float(eligible[0].close) - 1) * 100, 2),
                        "performance_start": eligible[0].date,
                        "performance_end": bars[-1].date,
                    }
                )
        except Exception:
            pass
        enriched.append(row)
    return enriched


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
        batch_link = f'<a class="{batch_active}" href="{prefix}/batch">{icon_label("layers", "批量")}</a>' if market == "us" else ""
        subnav = f"""
    <nav class="tabs" aria-label="市场功能">
      <a class="{scanner_active}" href="{prefix}/scanner">{icon_label("scan", "选股")}</a>
      <a class="{watchlist_active}" href="{prefix}/watchlist">{icon_label("star", "自选")}</a>
      <a class="{backtest_active}" href="{prefix}/backtest">{icon_label("chart", "回测")}</a>
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
:root {{
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-muted: #f1f5f9;
  --surface-soft: #f8fbff;
  --text: #0f172a;
  --text-muted: #64748b;
  --text-subtle: #94a3b8;
  --border: #dbe3ef;
  --border-strong: #c7d2e1;
  --primary: #1e40af;
  --primary-hover: #1d4ed8;
  --primary-soft: #eff6ff;
  --secondary: #3b82f6;
  --accent: #d97706;
  --success: #089981;
  --success-soft: rgba(8, 153, 129, .10);
  --danger: #dc2626;
  --danger-soft: #fff5f6;
  --warning-soft: #fff7ed;
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
  --shadow-sm: 0 1px 2px rgba(15, 23, 42, .05);
  --shadow-md: 0 10px 24px rgba(15, 23, 42, .10);
  --ring: rgba(30, 64, 175, .28);
}}
html {{ color-scheme: light; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font-family: "Fira Sans", Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", Arial, sans-serif; font-size: 14px; line-height: 1.45; }}
main {{ width: 100%; max-width: 1680px; margin: 0 auto; padding: 0 16px 24px; }}
.icon {{ width: 14px; height: 14px; flex: 0 0 auto; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.icon-sm {{ width: 12px; height: 12px; flex: 0 0 auto; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }}
.icon-lg {{ width: 16px; height: 16px; flex: 0 0 auto; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}
.nav-icon-label, button > span, .btn > span, .mini-action > span, .delete-link > span {{ display: inline-flex; align-items: center; gap: 5px; min-width: 0; }}
.app-topbar {{ position: sticky; top: 0; z-index: 20; display: flex; justify-content: space-between; align-items: center; gap: 16px; height: 54px; margin: 0 -16px 16px; padding: 0 18px; background: linear-gradient(90deg, #0f172a, #172554); border-bottom: 1px solid rgba(219, 234, 254, .18); box-shadow: 0 1px 3px rgba(15, 23, 42, .22); }}
.brand {{ display: flex; flex-direction: row; align-items: center; gap: 8px; line-height: 1.1; color: #f8fafc; font-weight: 800; letter-spacing: 0; }}
.brand-text {{ display: flex; flex-direction: column; }}
.brand span {{ color: #9ca3af; font-size: 11px; font-weight: 600; margin-top: 3px; }}
.topbar-actions {{ display: flex; align-items: center; gap: 14px; min-width: 0; }}
.market-switch {{ display: flex; gap: 2px; padding: 2px; border: 1px solid #2a2e39; border-radius: 6px; background: #0f131d; }}
.market-switch a {{ display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px; border-radius: 4px; color: #d1d4dc; text-decoration: none; font-size: 12px; font-weight: 900; white-space: nowrap; }}
.market-switch a:hover {{ background: #1f2430; color: #fff; }}
.market-switch a.active {{ background: #2962ff; color: #fff; }}
.tabs {{ display: flex; gap: 2px; margin: 0; }}
.tabs a {{ display: inline-flex; align-items: center; gap: 5px; padding: 8px 12px; border: 1px solid transparent; border-radius: 4px; color: #d1d4dc; text-decoration: none; font-size: 13px; font-weight: 700; }}
.tabs a:hover {{ background: #1f2430; color: #fff; }}
.tabs a.active {{ background: #2962ff; color: #fff; border-color: #2962ff; }}
h1 {{ margin: 0 0 5px; font-size: 20px; line-height: 1.22; letter-spacing: 0; color: var(--text); font-weight: 900; }}
h2 {{ margin: 16px 0 9px; font-size: 15px; font-weight: 900; }}
.hint {{ color: var(--text-muted); font-size: 13px; margin: 0 0 14px; line-height: 1.55; }}
.form {{ display: grid; grid-template-columns: repeat(8, minmax(116px, 1fr)); gap: 10px; align-items: end; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px; margin-bottom: 14px; box-shadow: var(--shadow-sm); }}
label {{ display: block; font-size: 12px; color: var(--text-muted); font-weight: 700; }}
.checkbox-label {{ display: flex; align-items: center; gap: 8px; min-height: 38px; color: #334155; }}
.checkbox-label input {{ width: auto; margin: 0; }}
.form-options {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 8px; margin-top: 2px; border-top: 1px solid #e3e7ee; }}
.form-options > span {{ color: #64748b; font-size: 12px; font-weight: 900; margin-right: 2px; }}
.form-options .checkbox-label {{ min-height: 30px; padding: 3px 8px; border: 1px solid #e3e7ee; border-radius: 4px; background: #f8fafc; }}
.form-section-title {{ grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; margin-top: 4px; padding-top: 8px; border-top: 1px solid #e3e7ee; color: #131722; font-size: 12px; font-weight: 900; }}
.form-section-title:first-child {{ margin-top: 0; padding-top: 0; border-top: 0; }}
.form-section-title span {{ color: #64748b; font-weight: 700; }}
.form-advanced {{ grid-column: 1 / -1; border: 1px solid var(--border); border-radius: var(--radius-lg); background: #fbfdff; padding: 0; }}
.form-advanced summary {{ cursor: pointer; padding: 10px 12px; color: var(--text); font-size: 12px; font-weight: 900; }}
.form-advanced summary span {{ margin-left: 8px; color: var(--text-muted); font-weight: 700; }}
.form-advanced-grid {{ display: grid; grid-template-columns: repeat(8, minmax(116px, 1fr)); gap: 10px; align-items: end; padding: 0 12px 12px; }}
.form-advanced .form-section-title {{ margin-top: 0; }}
input, select, textarea {{ width: 100%; margin-top: 6px; padding: 8px 9px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font-family: inherit; font-size: 13px; outline: none; }}
input:focus, select:focus, textarea:focus {{ border-color: var(--primary); box-shadow: 0 0 0 2px var(--ring); }}
textarea {{ min-height: 78px; resize: vertical; line-height: 1.45; }}
.ashare-symbol-field {{ grid-column: span 2; }}
.ashare-suggest-panel {{ position: absolute; z-index: 1000; max-height: 240px; overflow-y: auto; border: 1px solid #c7ccd5; border-radius: 6px; background: #fff; box-shadow: 0 12px 30px rgba(19, 23, 34, .16); padding: 3px; }}
.ashare-suggest-panel[hidden] {{ display: none; }}
.ashare-suggest-item {{ width: 100%; display: grid; grid-template-columns: 62px minmax(86px, 1fr) minmax(54px, auto); gap: 6px; align-items: center; min-height: 28px; padding: 5px 6px; border: 0; border-radius: 4px; background: transparent; color: #131722; text-align: left; font-size: 12px; line-height: 1.2; }}
.ashare-suggest-item:hover, .ashare-suggest-item:focus {{ background: #eef4ff; color: #131722; }}
.ashare-suggest-symbol {{ font-weight: 900; color: #2962ff; font-size: 12px; }}
.ashare-suggest-name {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 800; font-size: 12px; }}
.ashare-suggest-meta {{ color: #64748b; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: right; }}
button, .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; padding: 8px 13px; border: 1px solid var(--primary); border-radius: var(--radius-sm); background: var(--primary); color: #fff; font: inherit; font-size: 13px; font-weight: 800; line-height: 1.2; text-decoration: none; cursor: pointer; transition: background-color .14s ease, border-color .14s ease, color .14s ease, box-shadow .14s ease, transform .08s ease; }}
button:hover, .btn:hover {{ filter: none; background: var(--primary-hover); border-color: var(--primary-hover); color: #fff; text-decoration: none; }}
button:active, .btn:active {{ transform: translateY(1px); }}
button:focus-visible, .btn:focus-visible, a:focus-visible, .chart-toggle:focus-within, .check-inline:focus-within {{ outline: 2px solid var(--ring); outline-offset: 2px; box-shadow: none; }}
button:disabled, .btn.disabled {{ cursor: not-allowed; opacity: .62; transform: none; }}
button.secondary, .btn-secondary {{ background: var(--surface); color: #334155; border-color: var(--border-strong); }}
button.secondary:hover, .btn-secondary:hover {{ background: var(--surface-muted); border-color: #aeb7c5; color: var(--text); }}
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
.mode-pill {{ background: #0f172a; color: #f8fafc; border-radius: 999px; padding: 6px 10px; font-size: 12px; white-space: nowrap; box-shadow: var(--shadow-sm); }}
.status-strip {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 8px; margin: 0 0 14px; }}
.tv-workbench {{ padding: 0; overflow: hidden; background: #fff; }}
.tv-workbench-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 9px 11px; border-bottom: 1px solid #e3e7ee; background: #f8fafc; }}
.tv-workbench-title {{ display: flex; align-items: baseline; gap: 10px; min-width: 0; }}
.tv-workbench-title strong {{ font-size: 14px; color: #131722; }}
.tv-workbench-title span {{ color: #64748b; font-size: 12px; white-space: nowrap; }}
.tv-workbench-actions {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; align-items: center; }}
.tv-workbench-actions .links {{ margin: 0; }}
.tv-summary-strip {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 10px; margin: 0; border-bottom: 1px solid #e3e7ee; background: #fff; }}
.tv-summary-strip .stat-card {{ display: inline-flex; align-items: center; gap: 7px; min-height: 28px; padding: 4px 8px; border-radius: 4px; box-shadow: none; background: #f8fafc; }}
.tv-summary-strip .stat-label {{ margin: 0; font-size: 10px; }}
.tv-summary-strip .stat-value {{ font-size: 13px; }}
.tv-workbench-body {{ padding: 10px; }}
.stat-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 10px 12px; box-shadow: var(--shadow-sm); }}
.stat-label {{ color: var(--text-muted); font-size: 11px; font-weight: 800; text-transform: uppercase; margin-bottom: 6px; }}
.stat-value {{ color: var(--text); font-size: 18px; font-weight: 900; font-variant-numeric: tabular-nums; }}
.market-bar {{ display: grid; grid-template-columns: minmax(280px, .9fr) minmax(360px, 1.3fr); align-items: start; gap: 14px; min-width: 0; border: 1px solid #d6dbe3; border-left-width: 4px; border-radius: 6px; background: #fff; padding: 10px 12px; margin: 0 0 14px; box-shadow: 0 1px 2px rgba(19, 23, 34, .04); }}
.market-bar .market-main > div, .macro-box > div:first-child {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
.market-bar strong {{ display: inline-flex; align-items: center; gap: 5px; font-size: 15px; }}
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
.notice {{ background: #eefbf7; border: 1px solid #9fd8cc; color: #067a6b; padding: 10px 12px; border-radius: var(--radius-md); margin: 0 0 12px; font-weight: 800; }}
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
.candidate-decision-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin: 0 0 10px; }}
.candidate-card {{ border: 1px solid var(--border); border-radius: 6px; background: #fff; padding: 8px 9px; box-shadow: none; }}
.candidate-card:hover {{ border-color: var(--border-strong); box-shadow: 0 4px 12px rgba(15, 23, 42, .05); }}
.candidate-card-head {{ display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 5px; }}
.candidate-symbol {{ display: inline-flex; align-items: baseline; gap: 5px; min-width: 0; }}
.candidate-symbol strong {{ font-size: 14px; }}
.candidate-symbol span {{ color: var(--text-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 130px; }}
.candidate-card-meta {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; margin: 5px 0; }}
.candidate-card-meta div {{ border: 1px solid #e3e7ee; border-radius: 5px; background: #f8fafc; padding: 4px 5px; min-width: 0; }}
.candidate-card-meta span {{ display: block; color: var(--text-muted); font-size: 10px; font-weight: 800; margin-bottom: 1px; }}
.candidate-card-meta b {{ display: block; color: var(--text); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.candidate-card .condition-tags {{ gap: 4px; }}
.candidate-card .condition-tag {{ padding: 2px 5px; font-size: 10px; border-radius: 4px; }}
.candidate-card-actions {{ display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin-top: 6px; }}
.candidate-card-actions .mini-action {{ min-height: 24px; padding: 3px 7px; font-size: 11px; }}
.candidate-card-actions .hint {{ font-size: 11px; }}
.detail-disclosure {{ margin: 10px 0 0; border: 1px solid var(--border); border-radius: 6px; background: rgba(255,255,255,.7); }}
.detail-disclosure summary {{ cursor: pointer; padding: 7px 9px; color: var(--text-muted); font-size: 12px; font-weight: 900; }}
.detail-disclosure[open] summary {{ border-bottom: 1px solid var(--border); }}
.inline-actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
.delete-form {{ display: inline; margin: 0; }}
.delete-link {{ display: inline-flex; align-items: center; justify-content: center; min-height: 26px; padding: 4px 8px; border: 1px solid #f3a6ad; border-radius: 4px; background: #fff; color: #d12030; font-size: 12px; font-weight: 800; cursor: pointer; text-decoration: none; }}
.delete-link:hover {{ background: #fff5f6; border-color: #f23645; color: #b42332; text-decoration: none; filter: none; }}
.mini-action {{ min-height: 26px; padding: 4px 8px; font-size: 12px; border-color: #c7ccd5; background: #fff; color: #2962ff; }}
.mini-action.added {{ color: #089981; border-color: #9fd8cc; background: rgba(8,153,129,.08); }}
.icon-action {{ padding-left: 8px; padding-right: 9px; }}
.delete-link .icon, .delete-link .icon-sm {{ color: #d12030; }}
.mini-action .icon, .btn-secondary .icon, button.secondary .icon {{ color: currentColor; }}
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
.error {{ background: var(--danger-soft); border: 1px solid #ffc9cf; color: #b42332; padding: 12px; border-radius: var(--radius-md); white-space: pre-wrap; }}
.result {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px; margin-top: 14px; margin-bottom: 14px; box-shadow: var(--shadow-sm); }}
.candidate-detail {{ margin-top: 14px; }}
.candidate-detail iframe {{ height: 980px; }}
.candidate-detail iframe.candidate-chart-frame {{ height: 800px; }}
.watchlist-grid {{ display: grid; grid-template-columns: 340px minmax(0, 1fr); gap: 14px; align-items: start; min-width: 0; }}
.watchlist-panel {{ min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px; box-shadow: var(--shadow-sm); }}
.watchlist-chart-shell {{ position: relative; height: 880px; min-width: 520px; }}
.watchlist-chart {{ width: 100%; height: 100%; }}
.watchlist-price-chart {{ height: 640px; }}
.watchlist-kdj-chart {{ height: 200px; margin-top: 12px; border-top: 1px solid #eef1f5; }}
.chart-toggle-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0 10px; }}
.chart-toggle {{ display: inline-flex; align-items: center; gap: 5px; min-height: 28px; border: 1px solid var(--border); background: var(--surface); color: #334155; border-radius: var(--radius-sm); padding: 0 8px; font-size: 12px; font-weight: 800; cursor: pointer; }}
.chart-toggle input {{ margin: 0; }}
.price-chart-wrap {{ position: relative; height: 560px; }}
.price-chart-wrap .watchlist-chart {{ height: 100%; }}
.holding-bands {{ position: absolute; inset: 0; z-index: 2; pointer-events: none; overflow: hidden; }}
.holding-band {{ position: absolute; top: 0; bottom: 0; background: rgba(8, 153, 129, .08); border-left: 1px solid rgba(8, 153, 129, .32); border-right: 1px solid rgba(8, 153, 129, .14); }}
.watchlist-list-wrap {{ max-height: 760px; overflow: auto; }}
.watch-row-button {{ width: 100%; justify-content: flex-start; border: 0; background: transparent; color: #131722; padding: 0; min-height: 0; text-align: left; }}
.watch-row-button:hover {{ background: transparent; color: #2962ff; }}
.watch-row-button.is-active {{ color: var(--primary); }}
.watch-row-cell {{ min-width: 220px; }}
.watch-row-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }}
.watch-row-actions {{ display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto; }}
.watch-row-actions .delete-link {{ min-height: 22px; padding: 2px 6px; font-size: 11px; }}
.watch-symbol-line {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 900; }}
.watch-meta-line {{ margin-top: 4px; color: #64748b; font-size: 12px; overflow: hidden; text-overflow: ellipsis; }}
.watchlist-panel-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; margin-bottom: 10px; }}
.watchlist-panel-head strong {{ display: block; font-size: 14px; }}
.watchlist-panel-head span {{ display: block; margin-top: 3px; color: var(--text-muted); font-size: 12px; }}
.watchlist-count-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 34px; height: 24px; padding: 0 8px; border: 1px solid var(--border); border-radius: 999px; background: var(--primary-soft); color: var(--primary); font-weight: 900; font-size: 12px; }}
.watchlist-chart-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 10px; }}
.watchlist-chart-head .hint {{ margin-bottom: 0; }}
.chart-control-panel {{ border: 1px solid var(--border); border-radius: var(--radius-lg); background: #fbfdff; padding: 10px; margin-bottom: 10px; }}
.chart-control-title {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text-muted); font-size: 12px; font-weight: 900; margin-bottom: 8px; }}
.chart-control-title span:last-child {{ color: var(--text-subtle); font-weight: 800; }}
.watch-detail-grid {{ display: grid; grid-template-columns: repeat(4, minmax(110px, 1fr)); gap: 8px; margin: 10px 0 12px; }}
.watch-detail-item {{ border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 6px; padding: 6px 8px; min-width: 0; }}
.watch-detail-item span {{ display: block; color: #64748b; font-size: 10px; font-weight: 900; margin-bottom: 2px; }}
.watch-detail-item strong {{ display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.watchlist-page .page-head {{ margin-bottom: 8px; }}
.watchlist-page .form {{ margin-bottom: 10px; }}
.watchlist-page .status-strip {{ margin-bottom: 10px; }}
.watchlist-page .chart-control-panel {{ padding: 8px; }}
.divergence-panel {{ border: 1px solid #e3e7ee; background: #f8fafc; border-radius: 6px; padding: 10px; margin: 0 0 12px; }}
.divergence-panel summary {{ cursor: pointer; list-style-position: inside; }}
.divergence-panel:not([open]) {{ padding: 8px 10px; }}
.divergence-panel:not([open]) .divergence-head {{ margin-bottom: 0; }}
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
.progress-box {{ display: none; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 12px; margin-top: 14px; box-shadow: var(--shadow-sm); }}
.progress-box.active {{ display: block; }}
.progress-track {{ height: 8px; background: #e6eaf0; border-radius: 999px; overflow: hidden; margin: 8px 0; }}
.progress-bar {{ height: 100%; width: 0%; background: var(--primary); transition: width .2s ease; }}
.progress-meta {{ color: #475569; font-size: 13px; }}
.progress-actions {{ display: flex; gap: 8px; margin-top: 10px; }}
.progress-actions button[hidden] {{ display: none; }}
.dashboard-grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 14px; align-items: start; min-width: 0; }}
.dashboard-panel {{ min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 14px; box-shadow: var(--shadow-sm); }}
.dashboard-panel h2 {{ margin: 0 0 10px; }}
.dashboard-panel:hover {{ box-shadow: 0 6px 18px rgba(15, 23, 42, .06); }}
details.dashboard-panel summary {{ cursor: pointer; list-style-position: inside; color: var(--text); font-weight: 900; }}
details.dashboard-panel summary .panel-kicker {{ display: inline; margin-left: 8px; font-weight: 800; }}
details.dashboard-panel[open] summary {{ margin-bottom: 10px; }}
.action-overview {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 4px 0 14px; color: var(--text-muted); font-size: 12px; }}
.action-overview span {{ display: inline-flex; align-items: center; min-height: 24px; padding: 3px 8px; border: 1px solid var(--border); border-radius: 999px; background: rgba(255,255,255,.58); }}
.action-overview b {{ margin-left: 4px; color: #475569; font-weight: 800; font-variant-numeric: tabular-nums; }}
.panel-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
.panel-head h2 {{ margin: 0; }}
.panel-kicker {{ color: var(--text-muted); font-size: 12px; font-weight: 800; }}
.workflow-rail {{ margin: 0 0 14px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: rgba(255,255,255,.52); color: var(--text-muted); }}
.workflow-rail summary {{ cursor: pointer; padding: 8px 10px; font-size: 12px; font-weight: 800; }}
.workflow-rail summary:hover {{ color: var(--primary); }}
.workflow-step-list {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 0 10px 10px; }}
.workflow-step {{ display: inline-flex; gap: 4px; align-items: baseline; min-height: 24px; border: 1px solid var(--border); border-radius: 999px; background: #fff; padding: 3px 8px; }}
.workflow-step span {{ color: var(--text-subtle); font-size: 11px; font-weight: 900; text-transform: uppercase; }}
.workflow-step strong {{ color: #475569; font-size: 12px; }}
.workflow-step p {{ display: none; }}
.quick-actions {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.checkbox-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 6px; }}
.check-inline {{ display: inline-flex; align-items: center; gap: 6px; min-height: 30px; padding: 5px 9px; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; color: #131722; font-size: 13px; font-weight: 800; cursor: pointer; }}
.check-inline input {{ width: auto; margin: 0; accent-color: #2962ff; }}
.links {{ margin: 0 0 12px; font-size: 13px; }}
.links a {{ color: #2962ff; text-decoration: none; margin-right: 12px; font-weight: 700; }}
.links a:hover {{ text-decoration: underline; }}
iframe {{ width: 100%; height: 1320px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); }}
table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #fff; }}
.table-wrap {{ width: 100%; max-width: 100%; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--surface); max-height: 680px; box-shadow: var(--shadow-sm); }}
.table-wrap table {{ width: max-content; min-width: 100%; table-layout: auto; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eef1f5; text-align: right; font-size: 12px; white-space: nowrap; }}
tbody tr:hover td {{ background: #f8fafc; }}
th {{ background: #f5f7fa; color: var(--text-muted); position: sticky; top: 0; z-index: 8; font-size: 11px; font-weight: 800; text-transform: uppercase; border-bottom: 1px solid var(--border); }}
th:first-child, td:first-child {{ position: sticky; left: 0; background: #fff; z-index: 3; box-shadow: 1px 0 0 #eef1f5; }}
th:first-child {{ background: #f5f7fa; z-index: 10; }}
th.resizable {{ position: sticky; user-select: none; }}
.col-resizer {{ position: absolute; top: 0; right: -3px; width: 6px; height: 100%; cursor: col-resize; z-index: 2; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6), th:nth-child(7), td:nth-child(7) {{ text-align: left; }}
.empty {{ text-align: center; color: #607080; }}
@media (prefers-reduced-motion: reduce) {{ *, *::before, *::after {{ animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; transition-duration: .01ms !important; }} }}
@media (max-width: 1200px) {{ .form, .form-advanced-grid {{ grid-template-columns: repeat(4, 1fr); }} .watchlist-grid {{ grid-template-columns: 1fr; }} .watchlist-chart-shell {{ min-width: 0; }} .divergence-form {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }} .condition-grid {{ grid-template-columns: repeat(2, minmax(220px, 1fr)); }} .condition-card:nth-child(2) {{ border-right: 0; }} .condition-card:nth-child(-n+2) {{ border-bottom: 1px solid #e3e7ee; }} .workflow-rail, .action-overview {{ grid-template-columns: repeat(2, minmax(180px, 1fr)); }} }}
@media (max-width: 760px) {{ main {{ padding: 0 10px 18px; overflow-x: hidden; }} .app-topbar {{ margin: 0 -10px 12px; height: auto; padding: 10px; align-items: flex-start; flex-direction: column; }} .topbar-actions {{ width: 100%; flex-direction: column; align-items: stretch; gap: 8px; }} .market-switch, .tabs {{ width: 100%; overflow-x: auto; }} .market-bar {{ grid-template-columns: 1fr; }} .macro-box {{ border-left: 0; padding-left: 0; border-top: 1px solid #e3e7ee; padding-top: 10px; }} .form, .form-advanced-grid, .status-strip, .dashboard-grid, .workflow-rail, .action-overview {{ grid-template-columns: minmax(0, 1fr); }} .wide {{ grid-column: span 1; }} .page-head {{ display: block; }} .mode-pill {{ display: inline-flex; margin-top: 6px; }} .condition-grid {{ grid-template-columns: 1fr; }} .condition-card, .condition-card:nth-child(2) {{ border-right: 0; border-bottom: 1px solid #e3e7ee; }} .condition-card:last-child {{ border-bottom: 0; }} button, .btn, input, select, textarea {{ min-height: 40px; }} }}
</style>
</head>
<body><main>
<header class="app-topbar">
  <div class="brand">{icon_svg("candles", "icon-lg")}<div class="brand-text">MA5 Strategy Lab<span>选股 | 自选 | 回测</span></div></div>
  <div class="topbar-actions">
    <nav class="market-switch" aria-label="一级菜单">
      <a class="{action_active}" href="/">{icon_label("home", "行动台")}</a>
      <a class="{us_market_active}" href="/us/scanner">{icon_label("globe", "美股")}</a>
      <a class="{cn_market_active}" href="/cn/scanner">{icon_label("candles", "A股")}</a>
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
    let search = wrap;
    let panel = null;
    while (search && !panel && search !== root) {{
      let cursor = search.previousElementSibling;
      while (cursor && !panel) {{
        if (cursor.matches?.("[data-secondary-filter-panel]")) panel = cursor;
        cursor = cursor.previousElementSibling;
      }}
      search = search.parentElement;
    }}
    if (!panel || !panel.matches("[data-secondary-filter-panel]") || panel.dataset.secondaryReady === "1") return;
    panel.dataset.secondaryReady = "1";
    const tableRows = Array.from(table.querySelectorAll("[data-secondary-row]"));
    const cardGrid = panel.parentElement?.querySelector("[data-secondary-card-grid]") || null;
    const cardRows = cardGrid ? Array.from(cardGrid.querySelectorAll("[data-secondary-row]")) : [];
    const rows = [...tableRows, ...cardRows];
    const filters = Array.from(panel.querySelectorAll("[data-secondary-filter]"));
    const totalEl = panel.querySelector("[data-secondary-total]");
    const visibleEl = panel.querySelector("[data-secondary-visible]");
    const countEls = Array.from(panel.querySelectorAll("[data-secondary-count]"));
    const clearButton = panel.querySelector("[data-secondary-clear]");
    function applyFilters() {{
      const activeFilters = filters.filter(input => input.checked).map(input => input.getAttribute("data-secondary-filter") || "");
      const counts = Object.fromEntries(countEls.map(el => [el.getAttribute("data-secondary-count") || "", 0]));
      let visible = 0;
      for (const row of tableRows) {{
        for (const key of Object.keys(counts)) {{
          if (row.getAttribute(`data-filter-${{key.replaceAll("_", "-")}}`) === "1") counts[key] += 1;
        }}
        const show = activeFilters.every(key => row.getAttribute(`data-filter-${{key.replaceAll("_", "-")}}`) === "1");
        row.hidden = !show;
        if (show) visible += 1;
      }}
      for (const row of cardRows) {{
        const show = activeFilters.every(key => row.getAttribute(`data-filter-${{key.replaceAll("_", "-")}}`) === "1");
        row.hidden = !show;
      }}
      if (totalEl) totalEl.textContent = String(tableRows.length);
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


def frame_shell(content: str, active: str = "backtest", market: str = "us") -> bytes:
    text = page_shell(content, active, market).decode("utf-8")
    frame_css = """
html, body { background: #fff; }
body > main { padding: 10px 12px 18px; max-width: none; }
.app-topbar { display: none !important; }
.page-head { margin-top: 0; }
.mode-pill { display: none; }
.chart-only-frame > .result > :not(.result) { display: none !important; }
.chart-only-frame > .result { margin: 0 !important; padding: 0 !important; border: 0 !important; box-shadow: none !important; }
.chart-only-frame > .result > .result { margin: 0 !important; border: 0 !important; }
"""
    return text.replace("<style>", f"<style>{frame_css}", 1).encode("utf-8")


def render_us_strategy_condition_panel(params: dict[str, list[str]], context: str = "scanner") -> str:
    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
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

    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", False) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", False) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", False) else ""
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
  <details class="form-advanced">
    <summary>高级参数 <span>策略、风控和可选买入条件</span></summary>
    <div class="form-advanced-grid">
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
    </div>
  </details>
  <button type="submit">{icon_label("play", "运行")}</button>
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
    active_jobs = [
        normalize_job_payload(job_id, job)
        for market in ("us", "cn", "us_profile")
        for item in [latest_job_for_market(market, include_finished=False)]
        if item
        for job_id, job in [item]
    ]
    history = load_task_history(6)
    us_latest = load_latest_scan()
    us_summary = us_latest.get("summary", {}) if us_latest else {}
    us_signal = str(us_latest.get("signal_date", "-")) if us_latest else "-"
    us_candidates = int(us_summary.get("visible_candidates", 0) or 0) if us_latest else 0
    us_strong = int(us_summary.get("strong", 0) or 0) if us_latest else 0
    us_watch_count = len(load_watchlist_items())
    us_profile_summary = us_company_profile_summary()
    latest_candidate_count = len(latest_scan_candidate_symbols())
    us_profile_job_item = latest_job_for_market("us_profile", include_finished=True)
    us_profile_job = normalize_job_payload(us_profile_job_item[0], us_profile_job_item[1]) if us_profile_job_item else {}
    if us_profile_job and not us_profile_job.get("is_active"):
        job_total = int(us_profile_job.get("total", 0) or 0)
        if job_total and latest_candidate_count and job_total > max(50, latest_candidate_count * 3):
            us_profile_job = {}
    cached_profile_progress = us_profile_summary.get("progress", {}) if isinstance(us_profile_summary.get("progress"), dict) else {}
    if us_profile_job:
        profile_progress_html = f'<span class="scan-fact" id="us-profile-progress"><span>更新任务</span>{html.escape(str(us_profile_job.get("status_label", "-")))} {int(us_profile_job.get("progress_pct", 0) or 0)}% · {int(us_profile_job.get("scanned", 0) or 0)}/{int(us_profile_job.get("total", 0) or 0)}</span>'
    elif cached_profile_progress:
        scanned = int(cached_profile_progress.get("scanned", 0) or 0)
        total = int(cached_profile_progress.get("total", 0) or 0)
        if total and latest_candidate_count and total > max(50, latest_candidate_count * 3):
            profile_progress_html = ""
        else:
            status_text = str(cached_profile_progress.get("status", "上次进度") or "上次进度")
            pct = 100 if total and scanned >= total else round(scanned / total * 100) if total else 0
            profile_progress_html = f'<span class="scan-fact" id="us-profile-progress"><span>{html.escape(status_text)}</span>{pct}% · {scanned}/{total}</span>'
    else:
        profile_progress_html = ""

    cn_latest = load_latest_ashare_scan()
    cn_summary = cn_latest.get("summary", {}) if cn_latest else {}
    cn_signal = str(cn_latest.get("signal_date", "-")) if cn_latest else "-"
    cn_candidates = int(cn_summary.get("candidates", 0) or 0) if cn_latest else 0
    cn_scanned = int(cn_summary.get("scanned", 0) or 0) if cn_latest else 0
    cn_watch_count = len(load_ashare_watchlist_items())
    cache = cache_dashboard_summary()
    cache_total_mb = sum(float(item.get("size_mb", 0) or 0) for item in cache.values())
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
        <button class="{cls}" type="submit">{icon_label("trash" if danger else "refresh", label)}</button>
      </form>"""

    us_company_profile_html = f"""
    <div class="toolbar" style="margin:12px 0 0;">
      <div>
        <div class="scan-facts" style="margin-top:0;">
          <span class="scan-fact"><span>公司信息</span>{int(us_profile_summary["count"])} 只</span>
          <span class="scan-fact"><span>中文名</span>{int(us_profile_summary.get("cn_name_count", 0) or 0)} 只</span>
          <span class="scan-fact"><span>FMP</span>{int(us_profile_summary.get("fmp_count", 0) or 0)} 只</span>
          <span class="scan-fact"><span>当前候选</span>{latest_candidate_count} 只</span>
          <span class="scan-fact"><span>更新时间</span>{html.escape(str(us_profile_summary["updated_at"]))}</span>
          {profile_progress_html}
        </div>
        <p class="hint" style="margin:6px 0 0;">只补全当前选股结果里缺失的信息；有 FMP_API_KEY 时会补英文公司名、行业、官网和业务简介。</p>
      </div>
      <form action="/us/company-profiles/update" method="get" onsubmit="this.querySelector('button').disabled=true; this.querySelector('button').textContent='更新中...';">
        <button class="secondary" type="submit">{icon_label("refresh", "更新公司信息")}</button>
      </form>
    </div>
    <script>
    (function() {{
      const progressEl = document.getElementById("us-profile-progress");
      if (!progressEl) return;
      async function refreshProfileProgress() {{
        try {{
          const res = await fetch("/us/company-profiles/status");
          const job = await res.json();
          if (!job || job.status === "idle") return false;
          const total = Number(job.total || 0);
          const scanned = Number(job.scanned || 0);
          const percent = job.progress_pct || (total ? Math.round(scanned / total * 100) : 0);
          const label = job.status_label || job.status || "更新中";
          progressEl.innerHTML = `<span>更新任务</span>${{label}} ${{percent}}% · ${{scanned}}/${{total || "-"}}`;
          return !["done", "error", "stopped"].includes(job.status);
        }} catch (error) {{
          return false;
        }}
      }}
      refreshProfileProgress().then(active => {{
        if (!active) return;
        const timer = window.setInterval(async () => {{
          const keep = await refreshProfileProgress();
          if (!keep) window.clearInterval(timer);
        }}, 3000);
      }});
    }})();
    </script>"""

    active_job_html = "".join(
        f"""
      <span class="scan-fact"><span>{html.escape(str(job.get("market_label", "-")))}</span>{html.escape(str(job.get("status_label", "-")))} {int(job.get("progress_pct") or 0)}% · {int(job.get("scanned") or 0)}/{int(job.get("total") or 0)}</span>"""
        for job in active_jobs
    ) or '<span class="scan-fact"><span>当前</span>没有运行中的扫描任务</span>'
    history_rows = "".join(
        f"""
      <tr>
        <td>{html.escape(str(item.get("created_at", "-")))}</td>
        <td>{html.escape(str(item.get("market_label", "-")))}</td>
        <td>{html.escape(str(item.get("status_label", "-")))}</td>
        <td>{int(item.get("scanned") or 0)}</td>
        <td>{int(item.get("candidates") or 0)}</td>
        <td>{int(item.get("errors") or 0)}</td>
        <td>{html.escape(str(item.get("source", "-"))[:60])}</td>
      </tr>"""
        for item in history
    ) or '<tr><td colspan="7" class="empty">还没有扫描任务历史。</td></tr>'
    today_focus_items = []
    if active_jobs:
        today_focus_items.append(f'<span class="scan-fact"><span>进行中</span>{len(active_jobs)} 个任务</span>')
    if us_candidates:
        today_focus_items.append(f'<span class="scan-fact"><span>美股候选</span>{us_candidates} 只待看图</span>')
    if cn_candidates:
        today_focus_items.append(f'<span class="scan-fact"><span>A股候选</span>{cn_candidates} 只待看图</span>')
    if not today_focus_items:
        today_focus_items.append('<span class="scan-fact"><span>当前</span>暂无新的候选任务</span>')
    today_focus_html = "".join(today_focus_items)

    return f"""
<section class="page-head">
  <div>
    <h1>行动台</h1>
    <p class="hint">按日常交易流程组织：扫描候选、加入自选、看图复盘、回测验证、维护缓存。</p>
  </div>
  <div class="mode-pill">Global | Action Desk</div>
</section>
{notice_html}
<section class="result latest-scan-card">
  <div class="toolbar">
    <div>
      <h2>今日待处理</h2>
      <div class="scan-facts">{today_focus_html}</div>
    </div>
    <div class="quick-actions">
      <a class="btn btn-secondary icon-action" href="/us/scan/latest">{icon_label("scan", "美股候选")}</a>
      <a class="btn btn-secondary icon-action" href="/cn/scan/latest">{icon_label("scan", "A股候选")}</a>
      <a class="btn btn-secondary icon-action" href="/us/watchlist">{icon_label("star", "美股自选")}</a>
      <a class="btn btn-secondary icon-action" href="/cn/watchlist">{icon_label("star", "A股自选")}</a>
    </div>
  </div>
</section>
<section class="dashboard-grid">
  <div class="dashboard-panel">
    <div class="panel-head">
      <div>
        <h2>美股工作区</h2>
        <div class="panel-kicker">选股结果 + 公司信息 + 回测</div>
      </div>
      <a class="btn btn-secondary btn-small icon-action" href="/us/scanner">{icon_label("scan", "进入")}</a>
    </div>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(us_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{us_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">Strong</div><div class="stat-value">{us_strong}</div></div>
      <div class="stat-card"><div class="stat-label">自选</div><div class="stat-value">{us_watch_count}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn btn-secondary icon-action" href="/us/scanner">{icon_label("scan", "选股")}</a>
      <a class="btn btn-secondary icon-action" href="/us/watchlist">{icon_label("star", "自选")}</a>
      <a class="btn btn-secondary icon-action" href="/us/backtest">{icon_label("chart", "回测")}</a>
      <a class="btn btn-secondary icon-action" href="/us/batch">{icon_label("layers", "批量")}</a>
    </div>
    {us_company_profile_html}
  </div>
  <div class="dashboard-panel">
    <div class="panel-head">
      <div>
        <h2>A股工作区</h2>
        <div class="panel-kicker">扫描候选 + 首字母检索 + A股图表</div>
      </div>
      <a class="btn btn-secondary btn-small icon-action" href="/cn/scanner">{icon_label("scan", "进入")}</a>
    </div>
    <section class="status-strip">
      <div class="stat-card"><div class="stat-label">信号日</div><div class="stat-value">{html.escape(cn_signal)}</div></div>
      <div class="stat-card"><div class="stat-label">候选</div><div class="stat-value">{cn_candidates}</div></div>
      <div class="stat-card"><div class="stat-label">扫描数量</div><div class="stat-value">{cn_scanned}</div></div>
      <div class="stat-card"><div class="stat-label">自选</div><div class="stat-value">{cn_watch_count}</div></div>
    </section>
    <div class="quick-actions">
      <a class="btn btn-secondary icon-action" href="/cn/scanner">{icon_label("scan", "选股")}</a>
      <a class="btn btn-secondary icon-action" href="/cn/watchlist">{icon_label("star", "自选")}</a>
      <a class="btn btn-secondary icon-action" href="/cn/backtest">{icon_label("chart", "回测")}</a>
    </div>
  </div>
</section>
<section class="action-overview" aria-label="辅助状态">
  <span>运行任务 <b>{len(active_jobs)}</b></span>
  <span>美股候选/自选 <b>{us_candidates}/{us_watch_count}</b></span>
  <span>A股候选/自选 <b>{cn_candidates}/{cn_watch_count}</b></span>
  <span>公司信息缓存 <b>{int(us_profile_summary["count"])}</b></span>
  <span>缓存体积 <b>{cache_total_mb:.1f} MB</b></span>
</section>
<details class="workflow-rail">
  <summary>查看日常流程提示</summary>
  <div class="workflow-step-list">
    <div class="workflow-step"><span>Step 1</span><strong>扫描</strong><p>先跑对应市场，生成当天候选。</p></div>
    <div class="workflow-step"><span>Step 2</span><strong>筛选</strong><p>看 Strong/Medium、行业和财报风险。</p></div>
    <div class="workflow-step"><span>Step 3</span><strong>自选</strong><p>把明天值得盯的票加入池子。</p></div>
    <div class="workflow-step"><span>Step 4</span><strong>看图</strong><p>确认 B 点、持仓区间和防守线。</p></div>
    <div class="workflow-step"><span>Step 5</span><strong>维护</strong><p>缓存变大或数据异常时再清理。</p></div>
  </div>
</details>
<section class="dashboard-grid">
  <details class="dashboard-panel">
    <summary>缓存维护 <span class="panel-kicker">需要时再处理，部署网页后也在服务器侧执行</span></summary>
    <p class="hint">清理的是服务器本地缓存和生成报告，不会删除美股/A股自选池；下次打开会自动重拉数据。</p>
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
  </details>
  <div class="dashboard-panel">
    <div class="panel-head">
      <div>
        <h2>任务状态</h2>
        <div class="panel-kicker">扫描和公司信息补全</div>
      </div>
    </div>
    <div class="scan-facts">
      {active_job_html}
    </div>
    <div class="table-wrap" style="margin-top:10px;">
      <table class="resizable-table">
        <thead><tr><th>时间</th><th>市场</th><th>状态</th><th>扫描</th><th>候选</th><th>失败</th><th>数据源</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
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
      <span class="scan-fact"><span>无结果</span>先跑全市场扫描</span>
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
<div id="ashare-symbol-suggestions" class="ashare-suggest-panel" hidden></div>
<script>
(function() {
  const panel = document.getElementById("ashare-symbol-suggestions");
  if (!panel || panel.dataset.ready === "true") return;
  panel.dataset.ready = "true";
  let timer = 0;
  let lastQuery = "";
  let activeInput = null;
  let activeItems = [];
  let requestSeq = 0;
  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, ch => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[ch]));
  }
  function positionPanel(input) {
    const rect = input.getBoundingClientRect();
    panel.style.left = `${rect.left + window.scrollX}px`;
    panel.style.top = `${rect.bottom + window.scrollY + 4}px`;
    panel.style.width = `${rect.width}px`;
  }
  function hidePanel() {
    panel.hidden = true;
    panel.innerHTML = "";
    activeItems = [];
  }
  function renderSuggestions(input, items) {
    activeInput = input;
    activeItems = items || [];
    if (!activeItems.length) {
      hidePanel();
      return;
    }
    positionPanel(input);
    panel.innerHTML = activeItems.map((item, index) => {
      const meta = [item.initials, item.sector, item.exchange].filter(Boolean).join(" / ");
      return `
        <button type="button" class="ashare-suggest-item" data-index="${index}">
          <span class="ashare-suggest-symbol">${escapeHtml(item.symbol)}</span>
          <span class="ashare-suggest-name">${escapeHtml(item.name)}</span>
          <span class="ashare-suggest-meta">${escapeHtml(meta)}</span>
        </button>`;
    }).join("");
    panel.hidden = false;
  }
  async function updateSuggestions(input, query) {
    const q = (query || "").trim();
    if (q.length < 1) {
      requestSeq += 1;
      hidePanel();
      return;
    }
    if (q === lastQuery && !panel.hidden) return;
    lastQuery = q;
    const requestId = ++requestSeq;
    try {
      const res = await fetch(`/cn/suggest?q=${encodeURIComponent(q)}`);
      const payload = await res.json();
      if (requestId !== requestSeq || input !== activeInput || input.value.trim() !== q) return;
      renderSuggestions(input, payload.suggestions || []);
    } catch (error) {
      if (requestId === requestSeq) hidePanel();
    }
  }
  document.addEventListener("input", event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.matches("[data-ashare-symbol-input]")) return;
    activeInput = input;
    window.clearTimeout(timer);
    timer = window.setTimeout(() => updateSuggestions(input, input.value), 120);
  });
  document.addEventListener("focusin", event => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.matches("[data-ashare-symbol-input]")) return;
    activeInput = input;
    if (input.value.trim()) updateSuggestions(input, input.value);
  });
  panel.addEventListener("mousedown", event => {
    const button = event.target.closest("[data-index]");
    if (!button || !activeInput) return;
    event.preventDefault();
    const item = activeItems[Number(button.dataset.index)];
    if (!item) return;
    activeInput.value = item.value || `${item.symbol} ${item.name || ""}`.trim();
    activeInput.dispatchEvent(new Event("change", { bubbles: true }));
    hidePanel();
  });
  window.addEventListener("scroll", () => {
    if (!panel.hidden && activeInput) positionPanel(activeInput);
  }, true);
  window.addEventListener("resize", () => {
    if (!panel.hidden && activeInput) positionPanel(activeInput);
  });
  document.addEventListener("mousedown", event => {
    if (event.target.closest("#ashare-symbol-suggestions") || event.target.closest("[data-ashare-symbol-input]")) return;
    hidePanel();
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
  <label class="ashare-symbol-field">股票代码/名称<input name="symbol" value="{value("symbol", defaults["symbol"])}" placeholder="600487 或 亨通光电" autocomplete="off" data-ashare-symbol-input></label>
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
  <details class="form-advanced">
    <summary>高级参数 <span>信号、止损、弱趋势和可选条件</span></summary>
    <div class="form-advanced-grid">
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
    </div>
  </details>
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
    if field(params, "_report_only", "0") == "1":
        return report_path.read_text(encoding="utf-8")
    return f"""
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


def render_us_candidate_reason_tags(row: SignalResult) -> str:
    signal_label = {
        "B1_trend_confirm": "B1",
        "B2_reentry": "B2",
    }.get(row.signal_type, row.signal_type or "B")
    tags = [
        ("condition-primary", signal_label),
        ("condition-on", f"三日放量" if row.signal_type else "放量"),
        ("condition-on", f"巨量{row.massive_count_7d}次"),
        ("condition-on", f"距MA5 {row.dist_to_ma_pct:.1f}%"),
    ]
    if row.ma5_rising:
        tags.append(("condition-on", "MA5向上"))
    if row.ma5_gt_20:
        tags.append(("condition-on", "MA5>MA20"))
    if row.ma20_gt_50:
        tags.append(("condition-on", "20MA>50MA"))
    if row.big_red_b1:
        tags.append(("condition-primary", "大阴线B1"))
    if row.above_ma5_3d:
        tags.append(("condition-on", "连续3天>MA5"))
    return "".join(f'<span class="condition-tag {cls}">{html.escape(label)}</span>' for cls, label in tags)


def render_ashare_candidate_reason_tags(row: AShareSignalSnapshot) -> str:
    tags = [
        ("condition-primary", row.signal_type or "B"),
        ("condition-on", f"巨量{row.volume_ratio:.2f}x"),
        ("condition-on", f"量能{row.volume_score:.1f}/5"),
        ("condition-on", f"20日额{row.avg_amount_20d / 100_000_000:.1f}亿"),
    ]
    if row.ma5_rising:
        tags.append(("condition-on", "MA5向上"))
    if row.ma5_gt_20:
        tags.append(("condition-on", "MA5>MA20"))
    if row.ma20_gt_50:
        tags.append(("condition-on", "20MA>50MA"))
    if row.big_red_b1:
        tags.append(("condition-primary", "大阴线B1"))
    if row.above_ma5_3d:
        tags.append(("condition-on", "连续3天>MA5"))
    if row.limit_state and row.limit_state != "正常":
        tags.append(("condition-off", row.limit_state))
    return "".join(f'<span class="condition-tag {cls}">{html.escape(label)}</span>' for cls, label in tags)


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
    cards = []
    rating_class = {"Strong": "score-Strong", "Medium": "score-Medium", "Watch": "score-Medium"}

    for row in candidates:
        cls = rating_class.get(row.candidate_rating, "score-Weak")
        filter_attrs = " ".join(
            f'{result_filter_attr(key)}="{1 if result_filter_value(row, key) else 0}"'
            for key, _, _ in OPTIONAL_RESULT_FILTERS
        )
        rows.append(
            f'<tr data-secondary-row {filter_attrs}>'
            f'<td><a class="btn btn-secondary btn-small" href="/cn/watchlist/add?symbol={quote(row.symbol)}&name={quote(row.name or "")}">加入自选</a></td>'
            f'<td><button type="button" class="symbol-button" data-ashare-candidate-symbol="{html.escape(row.symbol)}">{html.escape(row.symbol)}</button></td>'
            f"<td>{html.escape(row.name or '-')}</td>"
            f"<td>{html.escape(row.signal_type or '-')}</td>"
            f'<td><span class="condition-tags" style="justify-content:flex-start;">{render_ashare_candidate_reason_tags(row)}</span></td>'
            f"<td><span class=\"score-badge {cls}\">{html.escape(row.candidate_rating)}</span></td>"
            f"<td>{row.close:.2f}</td>"
            f"<td>{row.volume_ratio:.2f}x</td>"
            f"<td>{row.avg_amount_20d / 100_000_000:.2f}亿</td>"
            f"<td>{html.escape(row.latest_date)}</td>"
            f"<td>{html.escape(row.data_source)}</td>"
            "</tr>"
        )
        reason_html = render_ashare_candidate_reason_tags(row)
        cards.append(
            f'<article class="candidate-card" data-secondary-row {filter_attrs}>'
            f'<div class="candidate-card-head">'
            f'<div class="candidate-symbol"><strong>{html.escape(row.symbol)}</strong><span>{html.escape(row.name or "-")}</span></div>'
            f'<span class="score-badge {cls}">{html.escape(row.candidate_rating)}</span>'
            f'</div>'
            f'<div class="candidate-card-meta">'
            f'<div><span>B点</span><b>{html.escape(row.signal_type or "-")}</b></div>'
            f'<div><span>量比</span><b>{row.volume_ratio:.2f}x</b></div>'
            f'<div><span>量能分</span><b>{row.volume_score:.1f}/5</b></div>'
            f'<div><span>20日额</span><b>{row.avg_amount_20d / 100_000_000:.2f}亿</b></div>'
            f'<div><span>收盘</span><b>{row.close:.2f}</b></div>'
            f'<div><span>交易日</span><b>{html.escape(row.latest_date)}</b></div>'
            f'</div>'
            f'<div class="condition-tags">{reason_html}</div>'
            f'<div class="candidate-card-actions">'
            f'<a class="btn btn-secondary btn-small" href="/cn/watchlist/add?symbol={quote(row.symbol)}&name={quote(row.name or "")}">加入自选</a>'
            f'<button type="button" class="mini-action" data-ashare-candidate-symbol="{html.escape(row.symbol)}">看图</button>'
            f'<span class="hint" style="margin:0;">{html.escape(row.data_source)}</span>'
            f'</div>'
            f'</article>'
        )
    table_html = (
        """
  <section class="candidate-decision-grid" data-secondary-card-grid>
"""
        + "\n".join(cards)
        + """
  </section>
  <details class="detail-disclosure">
    <summary>查看明细表格</summary>
    <div class="table-wrap">
      <table class="sortable resizable-table" data-secondary-filter-table>
        <thead><tr><th>操作</th><th>代码</th><th>名称</th><th>B点</th><th>入选原因</th><th>评级</th><th>收盘</th><th>量比</th><th>20日均成交额</th><th>交易日</th><th>数据源</th></tr></thead>
        <tbody>
"""
        + "\n".join(rows)
        + """
        </tbody>
      </table>
    </div>
  </details>
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
          <li><b>市场范围</b><span>{html.escape(ashare_board_filter_label(selected_boards))}</span></li>
          <li><b>行业信息</b><span>仅在结果 / 自选池 / 回测中展示</span></li>
          <li><b>最低市值</b><span>{value("min_market_cap", "50")} 亿元</span></li>
          <li><b>最多扫描</b><span>{value("max_symbols", str(ASHARE_DEFAULT_MAX_SCAN_SYMBOLS))} 只</span></li>
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
    <div class="stat-card"><div class="stat-label">行业</div><div class="stat-value">{html.escape(sector)}</div></div>
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
    <div class="chart-toggle-row">
      <label class="chart-toggle"><input id="ashare-toggle-ma5-stop-25" type="checkbox">2.5%防守线</label>
      <label class="chart-toggle"><input id="ashare-toggle-ma5-stop-strategy" type="checkbox" checked>策略防守线</label>
      <label class="chart-toggle"><input id="ashare-toggle-signal-markers" type="checkbox" checked>B/S信号日</label>
    </div>
    <div class="watchlist-chart-shell" style="height:780px;">
      <div class="price-chart-wrap">
        <div id="ashare-main-chart" class="watchlist-chart"></div>
        <div id="ashare-holding-bands" class="holding-bands"></div>
      </div>
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
    const holdingBands = document.getElementById("ashare-holding-bands");
    const kdjEl = document.getElementById("ashare-kdj-chart");
    const tooltip = document.getElementById("ashare-tooltip");
    const toggleMa5Stop25 = document.getElementById("ashare-toggle-ma5-stop-25");
    const toggleMa5StopStrategy = document.getElementById("ashare-toggle-ma5-stop-strategy");
    const toggleSignalMarkers = document.getElementById("ashare-toggle-signal-markers");
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
    const shortTrend = mainChart.addLineSeries({{ color: "#f5a623", lineWidth: 2, title: "MA5", priceLineVisible: false }});
    shortTrend.setData(toLine(payload.ma5 || payload.zx_short_trend));
    const multiTrend = mainChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, title: "MA20", priceLineVisible: false, lastValueVisible: false }});
    multiTrend.setData(toLine(payload.ma20 || payload.zx_multi_trend));
    const ma5Stop25 = mainChart.addLineSeries({{ color: "#dc2626", lineWidth: 1, title: "5MA-2.5%", priceLineVisible: false, lastValueVisible: false }});
    const ma5Stop = mainChart.addLineSeries({{ color: "#ef4444", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: `5MA-${{payload.ma5StopPct || 7.5}}%`, priceLineVisible: false, lastValueVisible: false }});
    function refreshDefenseLines() {{
      ma5Stop25.setData(toggleMa5Stop25?.checked ? toLine(payload.ma5Stop25) : []);
      ma5Stop.setData(toggleMa5StopStrategy?.checked ? toLine(payload.ma5Stop) : []);
    }}
    refreshDefenseLines();
    toggleMa5Stop25?.addEventListener("change", refreshDefenseLines);
    toggleMa5StopStrategy?.addEventListener("change", refreshDefenseLines);
    const volSeries = mainChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
    volSeries.setData(volumeRows);
    const volMaSeries = mainChart.addLineSeries({{ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false, lastValueVisible: false }});
    volMaSeries.setData(toLine(payload.volume_ma20));
    mainChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
    const signalMarkerRows = (payload.signals || []).map(row => ({{
      time: row.x,
      position: "belowBar",
      color: "#16a34a",
      shape: "arrowUp",
      text: row.text || "B",
    }}));
    function refreshMarkers() {{
      const baseMarkers = payload.markers || [];
      const signalMarkers = toggleSignalMarkers?.checked ? signalMarkerRows : [];
      candle.setMarkers([...baseMarkers, ...signalMarkers].sort((a, b) => String(a.time).localeCompare(String(b.time))));
    }}
    refreshMarkers();
    toggleSignalMarkers?.addEventListener("change", refreshMarkers);
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
    let holdingBandFrame = null;
    function scheduleHoldingBandsRender() {{
      if (holdingBandFrame !== null) window.cancelAnimationFrame(holdingBandFrame);
      holdingBandFrame = window.requestAnimationFrame(() => {{
        holdingBandFrame = null;
        renderHoldingBands();
      }});
    }}
    function renderHoldingBands() {{
      if (!holdingBands) return;
      holdingBands.replaceChildren();
      const periods = payload.holdingPeriods || [];
      const barSpacing = mainChart.timeScale().options().barSpacing || 8;
      const width = mainEl.clientWidth;
      for (const period of periods) {{
        const startX = mainChart.timeScale().timeToCoordinate(period.start);
        const endXRaw = mainChart.timeScale().timeToCoordinate(period.end);
        if (startX === null || endXRaw === null) continue;
        const left = Math.max(0, Math.min(startX, endXRaw));
        const right = Math.min(width, Math.max(startX, endXRaw) + barSpacing);
        if (right <= 0 || left >= width || right - left < 2) continue;
        const band = document.createElement("div");
        band.className = "holding-band";
        band.style.left = `${{left}}px`;
        band.style.width = `${{Math.max(2, right - left)}}px`;
        band.title = `${{period.start}} - ${{period.end}}`;
        holdingBands.appendChild(band);
      }}
    }}
    mainChart.timeScale().subscribeVisibleLogicalRangeChange(() => scheduleHoldingBandsRender());
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
      scheduleHoldingBandsRender();
    }}).observe(mainEl);
    new ResizeObserver(entries => {{
      const rect = entries[0].contentRect;
      kdjChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
    }}).observe(kdjEl);
    mainChart.timeScale().fitContent();
    kdjChart.timeScale().fitContent();
    scheduleHoldingBandsRender();
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
  <label class="ashare-symbol-field">股票代码/名称<input name="symbol" value="{html.escape(symbol)}" placeholder="600487 或 亨通光电" autocomplete="off" data-ashare-symbol-input></label>
  <button type="submit">单票验证</button>
</form>
{render_ashare_symbol_autocomplete()}
<form class="form" id="ashare-scanner-form" action="/cn/scanner" method="get" data-async-submit="true">
  <input type="hidden" name="mode" value="market">
  <input type="hidden" name="j_threshold" value="{j_threshold:g}">
  <div class="form-section-title">扫描范围 <span>股票池、市值、并发和流动性</span></div>
  <label>最低市值（亿元）<input type="number" step="1" name="min_market_cap" value="{min_market_cap:g}"></label>
  <label>最多扫描<input type="number" step="1" name="max_symbols" value="{max_symbols}"></label>
  <label>并发数<input type="number" step="1" name="max_workers" value="{int(number_field(params, "max_workers", 6))}"></label>
  <label>20日均成交额（亿元）<input type="number" step="0.1" name="min_avg_amount_20d_100m" value="{min_avg_amount_20d_100m:g}"></label>
  <label>低流动性提示（亿元）<input type="number" step="0.1" name="min_control_amount_20d_100m" value="{min_control_amount_20d_100m:g}"></label>
  <div class="wide risk-note">A股全市场建议并发 4-6；首次建 K 线缓存或外部源超时较多时，用 3-4 更稳。</div>
  <div class="form-section-title">信号参数 <span>B1/B2、放量和量能评级</span></div>
  <label>连续放量天数<input type="number" step="1" name="vol_high_days" value="{vol_high_days}"></label>
  <label>连续放量倍数<input type="number" step="0.1" name="vol_high_multiplier" value="{vol_high_multiplier:g}"></label>
  <label>巨量倍数<input type="number" step="0.1" name="vol_multiplier" value="{vol_multiplier:g}"></label>
  <label>巨量观察窗口<input type="number" step="1" name="massive_window" value="{massive_window}"></label>
  <label>巨量最少次数<input type="number" step="1" name="massive_min_count" value="{massive_min_count}"></label>
  <label>B2回踩距离 %<input type="number" step="0.1" name="reentry_pct" value="{reentry_pct:g}"></label>
  <label>Strong量能分<input type="number" step="0.1" name="strong_volume_score" value="{strong_volume_score:g}"></label>
  <label>Medium量能分<input type="number" step="0.1" name="medium_volume_score" value="{medium_volume_score:g}"></label>
  <div class="form-options">
    <span>可选买入条件</span>
    <input type="hidden" name="require_ma5_rising" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_ma5_rising" value="1"{" checked" if require_ma5_rising else ""}> MA5向上</label>
    <input type="hidden" name="require_5ma_gt_20ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="require_5ma_gt_20ma" value="1"{" checked" if require_5ma_gt_20ma else ""}> MA5&gt;MA20</label>
    <input type="hidden" name="b1_require_20ma_gt_50ma" value="0">
    <label class="checkbox-label"><input type="checkbox" name="b1_require_20ma_gt_50ma" value="1"{" checked" if b1_require_20ma_gt_50ma else ""}> 20MA&gt;50MA</label>
    <input type="hidden" name="secondary_big_red_b1" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_big_red_b1" value="1"{" checked" if secondary_big_red_b1 else ""}> 大阴线B1</label>
    <input type="hidden" name="secondary_above_ma5_3d" value="0">
    <label class="checkbox-label"><input type="checkbox" name="secondary_above_ma5_3d" value="1"{" checked" if secondary_above_ma5_3d else ""}> 连续三天&gt;MA5</label>
  </div>
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
        meta = " · ".join(part for part in (name, sector, group) if part and part != "-")
        note_text = note if note and note != "-" else added_at
        rows.append(
            "<tr>"
            '<td class="watch-row-cell">'
            '<div class="watch-row-head">'
            f'<button type="button" class="watch-row-button" data-ashare-symbol="{html.escape(symbol)}">'
            f'<span class="watch-symbol-line"><span>{html.escape(symbol)}</span><span>{html.escape(group)}</span></span>'
            f'<span class="watch-meta-line">{html.escape(meta or name)}</span>'
            f'<span class="watch-meta-line">{html.escape(note_text)}</span>'
            '</button>'
            f'<span class="watch-row-actions"><a class="delete-link" href="/cn/watchlist/delete?symbol={quote(symbol)}" onclick="return confirm(\'确认删除 {html.escape(symbol)}？\');">删除</a></span>'
            '</div>'
            '</td>'
            "</tr>"
        )
    table_rows = "\n".join(rows) if rows else '<tr><td class="empty">暂无 A 股自选。可以先添加代码，或从 A 股选股结果加入。</td></tr>'
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
  <label class="ashare-symbol-field">股票代码/名称<input name="symbol" value="{html.escape(field(params, "symbol", ""))}" placeholder="600487 或 亨通光电" autocomplete="off" data-ashare-symbol-input></label>
  <label>分组<input name="group" value="{html.escape(field(params, "group", "观察"))}" placeholder="观察 / 候选 / 持仓"></label>
  <label class="wide">备注<input name="note" value="{html.escape(field(params, "note", ""))}" placeholder="关注原因、行业、阻力位等"></label>
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
    <div class="watchlist-panel-head">
      <div>
        <strong>A股自选列表</strong>
        <span>点击任意股票后在右侧看图</span>
      </div>
      <div class="watchlist-count-pill">{len(items)}</div>
    </div>
    <div class="table-wrap watchlist-list-wrap">
      <table class="sortable resizable-table">
        <thead><tr><th>自选</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  <div class="watchlist-panel">
    <div class="watchlist-chart-head">
      <div>
        <strong id="ashare-watch-title">{html.escape(default_symbol) if default_symbol else "选择一只 A 股"}</strong>
        <p class="hint" id="ashare-watch-subtitle">点击左侧代码或“看图”后，在这里显示策略图表。</p>
      </div>
    </div>
    <div class="watch-detail-grid">
      <div class="watch-detail-item"><span>名称</span><strong id="ashare-detail-name">-</strong></div>
      <div class="watch-detail-item"><span>行业</span><strong id="ashare-detail-sector">-</strong></div>
      <div class="watch-detail-item"><span>数据源</span><strong id="ashare-detail-source">-</strong></div>
      <div class="watch-detail-item"><span>交易日数量</span><strong id="ashare-detail-count">-</strong></div>
    </div>
    <div class="chart-control-panel">
      <div class="chart-control-title"><span>图表显示</span><span>条件只影响图上信号，不改自选列表</span></div>
      <div class="chart-toggle-row" id="ashare-watch-strategy-options">
        <label class="chart-toggle"><input data-ashare-watch-condition="require_ma5_rising" type="checkbox">MA5向上</label>
        <label class="chart-toggle"><input data-ashare-watch-condition="require_5ma_gt_20ma" type="checkbox">MA5&gt;MA20</label>
        <label class="chart-toggle"><input data-ashare-watch-condition="b1_require_20ma_gt_50ma" type="checkbox">20MA&gt;50MA</label>
      </div>
      <div class="chart-toggle-row">
        <label class="chart-toggle"><input id="ashare-watch-toggle-ma5-stop-25" type="checkbox">2.5%防守线</label>
        <label class="chart-toggle"><input id="ashare-watch-toggle-ma5-stop-strategy" type="checkbox" checked>策略防守线</label>
        <label class="chart-toggle"><input id="ashare-watch-toggle-signal-markers" type="checkbox">B/S信号日</label>
      </div>
    </div>
    <div class="watchlist-chart-shell" style="height:780px;">
      <div class="price-chart-wrap">
        <div id="ashare-watch-main-chart" class="watchlist-chart"></div>
        <div id="ashare-watch-holding-bands" class="holding-bands"></div>
      </div>
      <div id="ashare-watch-kdj-chart" class="watchlist-chart" style="height:200px;margin-top:12px;"></div>
      <div id="ashare-watch-tooltip" class="chart-tooltip"></div>
      <div id="ashare-watch-loading" class="loading-overlay"><div class="spinner"></div><div>正在拉取 A 股日 K 数据</div></div>
    </div>
  </div>
</section>
<script>
const ashareInitialSymbol = {json.dumps(default_symbol)};
let ashareCurrentSymbol = ashareInitialSymbol;
let ashareMainChart = null;
let ashareKdjChart = null;
const ashareMainEl = document.getElementById("ashare-watch-main-chart");
const ashareHoldingBands = document.getElementById("ashare-watch-holding-bands");
const ashareKdjEl = document.getElementById("ashare-watch-kdj-chart");
const ashareTooltip = document.getElementById("ashare-watch-tooltip");
const ashareLoading = document.getElementById("ashare-watch-loading");
const ashareTitle = document.getElementById("ashare-watch-title");
const ashareSubtitle = document.getElementById("ashare-watch-subtitle");
const ashareToggleMa5Stop25 = document.getElementById("ashare-watch-toggle-ma5-stop-25");
const ashareToggleMa5StopStrategy = document.getElementById("ashare-watch-toggle-ma5-stop-strategy");
const ashareToggleSignalMarkers = document.getElementById("ashare-watch-toggle-signal-markers");
const ashareStrategyOptions = document.getElementById("ashare-watch-strategy-options");

function clearAshareCharts() {{
  if (ashareMainChart) ashareMainChart.remove();
  if (ashareKdjChart) ashareKdjChart.remove();
  ashareMainChart = null;
  ashareKdjChart = null;
  if (ashareHoldingBands) ashareHoldingBands.innerHTML = "";
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
  const trend = ashareMainChart.addLineSeries({{ color: "#f5a623", lineWidth: 2, title: "MA5", priceLineVisible: false }});
  trend.setData(toLine(payload.ma5 || payload.zx_short_trend));
  const multi = ashareMainChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, title: "MA20", priceLineVisible: false, lastValueVisible: false }});
  multi.setData(toLine(payload.ma20 || payload.zx_multi_trend));
  const ma5Stop25 = ashareMainChart.addLineSeries({{ color: "#dc2626", lineWidth: 1, title: "5MA-2.5%", priceLineVisible: false, lastValueVisible: false }});
  const ma5Stop = ashareMainChart.addLineSeries({{ color: "#ef4444", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: `5MA-${{payload.ma5StopPct || 7.5}}%`, priceLineVisible: false, lastValueVisible: false }});
  function refreshAshareDefenseLines() {{
    ma5Stop25.setData(ashareToggleMa5Stop25?.checked ? toLine(payload.ma5Stop25) : []);
    ma5Stop.setData(ashareToggleMa5StopStrategy?.checked ? toLine(payload.ma5Stop) : []);
  }}
  refreshAshareDefenseLines();
  if (ashareToggleMa5Stop25) ashareToggleMa5Stop25.onchange = refreshAshareDefenseLines;
  if (ashareToggleMa5StopStrategy) ashareToggleMa5StopStrategy.onchange = refreshAshareDefenseLines;
  const volSeries = ashareMainChart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
  volSeries.setData(volumes);
  const volMa = ashareMainChart.addLineSeries({{ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false, lastValueVisible: false }});
  volMa.setData(toLine(payload.volume_ma20));
  ashareMainChart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
  const signalMarkerRows = (payload.signals || []).map(row => ({{ time: row.x, position: "belowBar", color: "#16a34a", shape: "arrowUp", text: row.text || "B" }}));
  function refreshAshareMarkers() {{
    const baseMarkers = payload.markers || [];
    const signalMarkers = ashareToggleSignalMarkers?.checked ? signalMarkerRows : [];
    candle.setMarkers([...baseMarkers, ...signalMarkers].sort((a, b) => String(a.time).localeCompare(String(b.time))));
  }}
  refreshAshareMarkers();
  if (ashareToggleSignalMarkers) ashareToggleSignalMarkers.onchange = refreshAshareMarkers;
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
  let ashareHoldingBandFrame = null;
  function scheduleAshareHoldingBandsRender() {{
    if (ashareHoldingBandFrame !== null) window.cancelAnimationFrame(ashareHoldingBandFrame);
    ashareHoldingBandFrame = window.requestAnimationFrame(() => {{
      ashareHoldingBandFrame = null;
      renderHoldingBands();
    }});
  }}
  function renderHoldingBands() {{
    if (!ashareHoldingBands) return;
    ashareHoldingBands.replaceChildren();
    const periods = payload.holdingPeriods || [];
    const barSpacing = ashareMainChart.timeScale().options().barSpacing || 8;
    const width = ashareMainEl.clientWidth;
    for (const period of periods) {{
      const startX = ashareMainChart.timeScale().timeToCoordinate(period.start);
      const endXRaw = ashareMainChart.timeScale().timeToCoordinate(period.end);
      if (startX === null || endXRaw === null) continue;
      const left = Math.max(0, Math.min(startX, endXRaw));
      const right = Math.min(width, Math.max(startX, endXRaw) + barSpacing);
      if (right <= 0 || left >= width || right - left < 2) continue;
      const band = document.createElement("div");
      band.className = "holding-band";
      band.style.left = `${{left}}px`;
      band.style.width = `${{Math.max(2, right - left)}}px`;
      band.title = `${{period.start}} - ${{period.end}}`;
      ashareHoldingBands.appendChild(band);
    }}
  }}
  ashareMainChart.timeScale().subscribeVisibleLogicalRangeChange(() => scheduleAshareHoldingBandsRender());
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
    scheduleAshareHoldingBandsRender();
  }}).observe(ashareMainEl);
  new ResizeObserver(entries => {{
    const rect = entries[0].contentRect;
    if (ashareKdjChart) ashareKdjChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
  }}).observe(ashareKdjEl);
  ashareMainChart.timeScale().fitContent();
  ashareKdjChart.timeScale().fitContent();
  scheduleAshareHoldingBandsRender();
}}

async function loadAshareWatchChart(symbol) {{
  if (!symbol) return;
  ashareCurrentSymbol = symbol;
  ashareTitle.textContent = symbol;
  ashareSubtitle.textContent = "正在加载...";
  ashareLoading?.classList.add("active");
  try {{
    const params = new URLSearchParams({{ symbol, j_threshold: "14" }});
    ashareStrategyOptions?.querySelectorAll("[data-ashare-watch-condition]").forEach(input => {{
      params.set(input.dataset.ashareWatchCondition, input.checked ? "1" : "0");
    }});
    const res = await fetch(`/cn/watchlist/chart?${{params.toString()}}`);
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
  document.querySelectorAll("[data-ashare-symbol]").forEach(item => item.classList.remove("is-active"));
  target.classList.add("is-active");
  loadAshareWatchChart(target.getAttribute("data-ashare-symbol"));
}});
ashareStrategyOptions?.addEventListener("change", event => {{
  if (!event.target.closest("[data-ashare-watch-condition]")) return;
  loadAshareWatchChart(ashareCurrentSymbol);
}});
const initialAshareButton = document.querySelector(`[data-ashare-symbol="${{ashareInitialSymbol}}"]`) || document.querySelector("[data-ashare-symbol]");
if (initialAshareButton) initialAshareButton.classList.add("is-active");
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
        b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", False),
        require_ma5_rising=checkbox_field(params, "require_ma5_rising", False),
        require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", False),
        below_20ma_stop_days=int(number_field(params, "below_20ma_stop_days", 2)),
        weak_trend_exit_mode=field(params, "weak_trend_exit_mode", "hybrid"),
        weak_ma5_reclaim_days=int(number_field(params, "weak_ma5_reclaim_days", 5)),
        weak_ma20_reclaim_days=int(number_field(params, "weak_ma20_reclaim_days", 10)),
        weak_volume_down_multiplier=number_field(params, "weak_volume_down_multiplier", 1.5),
        weak_event_low_lookback=int(number_field(params, "weak_event_low_lookback", 27)),
        symbol=symbol,
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
        "b1_require_20ma_gt_50ma": checkbox_field(params, "b1_require_20ma_gt_50ma", False),
        "require_ma5_rising": checkbox_field(params, "require_ma5_rising", False),
        "require_5ma_gt_20ma": checkbox_field(params, "require_5ma_gt_20ma", False),
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
    if field(params, "_report_only", "0") == "1":
        return report_path.read_text(encoding="utf-8")
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

    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", False) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", False) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", False) else ""

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
  <button type="submit">{icon_label("play", "运行")}</button>
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
                b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", False),
                require_ma5_rising=checkbox_field(params, "require_ma5_rising", False),
                require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", False),
                below_20ma_stop_days=int(number_field(params, "below_20ma_stop_days", 2)),
                weak_trend_exit_mode=field(params, "weak_trend_exit_mode", "hybrid"),
                weak_ma5_reclaim_days=int(number_field(params, "weak_ma5_reclaim_days", 5)),
                weak_ma20_reclaim_days=int(number_field(params, "weak_ma20_reclaim_days", 10)),
                weak_volume_down_multiplier=number_field(params, "weak_volume_down_multiplier", 1.5),
                weak_event_low_lookback=int(number_field(params, "weak_event_low_lookback", 27)),
                symbol=symbol,
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
    form_html = "" if field(params, "_report_only", "0") == "1" else render_batch_form(params)
    return f"""
  {form_html}
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


US_COMPANY_PROFILE_CACHE_PATH = DATA_DIR / "cache" / "us_company_profiles.json"
_US_COMPANY_PROFILE_CACHE_MTIME = 0.0
_US_COMPANY_PROFILE_CACHE: dict[str, dict[str, object]] = {}
EASTMONEY_SEARCH_TOKEN = "D43BF722C8FEB83B4D18D2FDC0E537F8"
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"


def normalize_us_profile_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text and text.split(".", 1)[0].isdigit():
        text = text.split(".", 1)[1]
    return text.replace(" ", "")


def read_us_company_profile_cache() -> dict[str, dict[str, object]]:
    global _US_COMPANY_PROFILE_CACHE_MTIME, _US_COMPANY_PROFILE_CACHE
    path = US_COMPANY_PROFILE_CACHE_PATH
    if not path.exists():
        _US_COMPANY_PROFILE_CACHE_MTIME = 0.0
        _US_COMPANY_PROFILE_CACHE = {}
        return {}
    mtime = path.stat().st_mtime
    if mtime == _US_COMPANY_PROFILE_CACHE_MTIME:
        return _US_COMPANY_PROFILE_CACHE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
    except Exception:
        profiles = {}
    if not isinstance(profiles, dict):
        profiles = {}
    _US_COMPANY_PROFILE_CACHE_MTIME = mtime
    _US_COMPANY_PROFILE_CACHE = {
        normalize_us_profile_symbol(symbol): profile
        for symbol, profile in profiles.items()
        if normalize_us_profile_symbol(symbol) and isinstance(profile, dict)
    }
    return _US_COMPANY_PROFILE_CACHE


def us_company_profile_summary() -> dict[str, object]:
    profiles = read_us_company_profile_cache()
    updated_at = "-"
    source = "-"
    warning = ""
    progress: dict[str, object] = {}
    if US_COMPANY_PROFILE_CACHE_PATH.exists():
        try:
            payload = json.loads(US_COMPANY_PROFILE_CACHE_PATH.read_text(encoding="utf-8"))
            updated_at = str(payload.get("updated_at", "-"))
            source = str(payload.get("source", "-"))
            warning = str(payload.get("warning", "") or "")
            raw_progress = payload.get("progress", {})
            progress = raw_progress if isinstance(raw_progress, dict) else {}
        except Exception:
            pass
    if not progress and warning:
        match = re.search(r"已处理\s*(\d+)\s*/\s*(\d+)", warning)
        if match:
            progress = {"status": "上次进度", "scanned": int(match.group(1)), "total": int(match.group(2))}
    cn_name_count = sum(1 for item in profiles.values() if item.get("cn_name"))
    fmp_count = sum(1 for item in profiles.values() if item.get("profile_source") == "fmp.profile" or item.get("description"))
    return {"count": len(profiles), "cn_name_count": cn_name_count, "fmp_count": fmp_count, "updated_at": updated_at, "source": source, "warning": warning, "progress": progress}


def fmp_api_key() -> str:
    return (os.environ.get("FMP_API_KEY") or os.environ.get("FINANCIAL_MODELING_PREP_API_KEY") or "").strip()


def fmp_company_profile(symbol: str) -> dict[str, object]:
    normalized = normalize_us_profile_symbol(symbol)
    key = fmp_api_key()
    if not key:
        raise RuntimeError("缺少 FMP_API_KEY 环境变量。")
    if not normalized:
        return {}
    url = f"{FMP_PROFILE_URL}?{urlencode({'symbol': normalized, 'apikey': key})}"
    request = Request(url, headers={"User-Agent": "stock-backtester/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload if isinstance(payload, list) else [payload]
    row = next((item for item in rows if isinstance(item, dict)), {})
    if not row:
        return {}
    return {
        "symbol": normalized,
        "company_name": str(row.get("companyName") or "").strip(),
        "english_name": str(row.get("companyName") or "").strip(),
        "sector": str(row.get("sector") or "").strip(),
        "industry": str(row.get("industry") or "").strip(),
        "description": str(row.get("description") or "").strip(),
        "website": str(row.get("website") or "").strip(),
        "market_cap": float(row.get("marketCap") or 0),
        "country": str(row.get("country") or "").strip(),
        "exchange": str(row.get("exchange") or "").strip(),
        "exchange_full_name": str(row.get("exchangeFullName") or "").strip(),
        "ceo": str(row.get("ceo") or "").strip(),
        "full_time_employees": str(row.get("fullTimeEmployees") or "").strip(),
        "ipo_date": str(row.get("ipoDate") or "").strip(),
        "image": str(row.get("image") or "").strip(),
        "asset_type": "ETF" if row.get("isEtf") else "Stock",
        "fmp_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "profile_source": "fmp.profile",
    }


def eastmoney_us_headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
    }


def eastmoney_us_search(symbol: str, session=None) -> dict[str, object]:
    import requests

    normalized = normalize_us_profile_symbol(symbol)
    if not normalized:
        return {}
    sess = session or requests.Session()
    response = sess.get(
        "https://searchapi.eastmoney.com/api/suggest/get",
        params={
            "input": normalized,
            "type": "14",
            "token": EASTMONEY_SEARCH_TOKEN,
            "count": "10",
        },
        headers=eastmoney_us_headers(),
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    rows = (((payload or {}).get("QuotationCodeTable") or {}).get("Data") or [])
    if not isinstance(rows, list):
        return {}
    exact_rows = [
        row for row in rows
        if isinstance(row, dict)
        and normalize_us_profile_symbol(row.get("Code")) == normalized
        and str(row.get("SecurityTypeName") or row.get("Classify") or "").lower() in ("美股", "usstock")
    ]
    row = exact_rows[0] if exact_rows else next((item for item in rows if isinstance(item, dict) and normalize_us_profile_symbol(item.get("Code")) == normalized), {})
    if not row:
        return {}
    mkt_num = str(row.get("MktNum") or "").strip()
    return {
        "symbol": normalized,
        "cn_name": str(row.get("Name") or "").strip(),
        "eastmoney_mkt_num": mkt_num,
        "eastmoney_quote_id": str(row.get("QuoteID") or f"{mkt_num}.{normalized}" if mkt_num else "").strip(),
        "exchange": str(row.get("JYS") or "").strip(),
        "pinyin": str(row.get("PinYin") or "").strip(),
    }


def eastmoney_us_stock_quote(symbol: str, mkt_num: str = "", session=None) -> dict[str, object]:
    import requests

    normalized = normalize_us_profile_symbol(symbol)
    if not normalized:
        return {}
    sess = session or requests.Session()
    prefixes = [mkt_num] if mkt_num else []
    prefixes.extend(prefix for prefix in ("105", "106", "107") if prefix not in prefixes)
    last_error: Exception | None = None
    for prefix in prefixes:
        if not prefix:
            continue
        try:
            response = sess.get(
                "https://push2.eastmoney.com/api/qt/stock/get",
                params={
                    "secid": f"{prefix}.{normalized}",
                    "fields": "f43,f44,f45,f46,f47,f48,f55,f57,f58,f59,f60,f116,f170",
                },
                headers=eastmoney_us_headers(),
                timeout=8,
            )
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            if not data:
                continue
            if normalize_us_profile_symbol(data.get("f57")) != normalized:
                continue
            divisor = 10 ** int(data.get("f59") or 3)
            market_cap = data.get("f116")
            return {
                "symbol": normalized,
                "cn_name": str(data.get("f58") or "").strip(),
                "eastmoney_mkt_num": prefix,
                "latest_price": float(data.get("f43") or 0) / divisor if data.get("f43") not in (None, "-") else 0,
                "change_pct": float(data.get("f170") or 0) / 100 if data.get("f170") not in (None, "-") else 0,
                "market_cap": float(market_cap or 0) if market_cap not in (None, "-") else 0,
            }
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return {}


def enrich_us_profile_with_eastmoney(symbol: str, base: dict[str, object], session=None) -> dict[str, object]:
    profile = dict(base)
    search_data: dict[str, object] = {}
    quote_data: dict[str, object] = {}
    try:
        search_data = eastmoney_us_search(symbol, session=session)
    except Exception as exc:
        profile["eastmoney_error"] = str(exc)
    if search_data:
        profile.update({key: value for key, value in search_data.items() if value not in ("", None, 0)})
    if not profile.get("cn_name"):
        try:
            quote_data = eastmoney_us_stock_quote(symbol, str(profile.get("eastmoney_mkt_num") or ""), session=session)
        except Exception as exc:
            profile["eastmoney_quote_error"] = str(exc)
    if quote_data:
        for key, value in quote_data.items():
            if key == "market_cap" and value:
                profile[key] = value
            elif value not in ("", None, 0):
                profile[key] = value
    return profile


def build_us_profile_payload(profiles: dict[str, dict[str, object]], source: str, warning: str = "", progress: dict[str, object] | None = None) -> dict[str, object]:
    if not profiles:
        raise RuntimeError("没有可写入的美股公司信息。")
    US_COMPANY_PROFILE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "warning": warning,
        "progress": progress or {},
        "profiles": profiles,
    }
    atomic_write_json(US_COMPANY_PROFILE_CACHE_PATH, payload, indent=2)
    read_us_company_profile_cache()
    cn_name_count = sum(1 for item in profiles.values() if item.get("cn_name"))
    return {
        "count": len(profiles),
        "cn_name_count": cn_name_count,
        "updated_at": payload["updated_at"],
        "source": source,
        "warning": warning,
    }


def fetch_us_company_profiles_from_nasdaq_cache() -> dict[str, dict[str, object]]:
    profiles: dict[str, dict[str, object]] = {}
    rows = sorted(fetch_nasdaq_screener_rows(), key=lambda row: money_to_float(str(row.get("marketCap", ""))), reverse=True)
    for row in rows:
        symbol = normalize_us_profile_symbol(row.get("symbol"))
        if not symbol:
            continue
        profiles[symbol] = {
            "symbol": symbol,
            "cn_name": "",
            "english_name": str(row.get("name", "") or ""),
            "sector": str(row.get("sector", "") or ""),
            "industry": str(row.get("industry", "") or ""),
            "concept_tags": "",
        }
    return profiles


def latest_scan_candidate_symbols() -> list[str]:
    latest = load_latest_scan()
    if not latest:
        return []
    symbols: list[str] = []
    for row in latest.get("candidates", []):
        if isinstance(row, dict):
            symbol = normalize_us_profile_symbol(row.get("symbol"))
            if symbol:
                symbols.append(symbol)
    return unique_symbols(symbols)


def missing_us_company_profile_symbols(symbols: list[str]) -> list[str]:
    cache = read_us_company_profile_cache()
    need_fmp = bool(fmp_api_key())
    missing: list[str] = []
    for symbol in unique_symbols(symbols):
        normalized = normalize_us_profile_symbol(symbol)
        if not normalized:
            continue
        profile = cache.get(normalized, {})
        if not profile.get("cn_name"):
            missing.append(normalized)
            continue
        if need_fmp and (not profile.get("company_name") or not profile.get("sector") or not profile.get("industry") or not profile.get("description")):
            missing.append(normalized)
    return missing


def start_us_profile_update_for_symbols(symbols: list[str], reason: str = "current scan") -> str:
    target_symbols = missing_us_company_profile_symbols(symbols)
    if not target_symbols:
        return ""
    active = active_scan_job("us_profile")
    if active:
        return active[0]
    job_id = f"profile-{uuid.uuid4().hex[:10]}"
    set_job(
        job_id,
        market="us_profile",
        status="queued",
        stage="排队中",
        message="美股公司信息后台更新已启动",
        total=len(target_symbols),
        scanned=0,
        candidates=0,
        errors=0,
        current="",
        stop_requested=False,
        symbols=target_symbols,
        reason=reason,
    )
    worker = threading.Thread(target=execute_us_company_profile_job, args=(job_id, target_symbols), daemon=True)
    worker.start()
    return job_id


def execute_us_company_profile_job(job_id: str, symbols: list[str]) -> None:
    import requests

    try:
        set_job(job_id, status="running", stage="准备", message="正在准备美股公司信息缓存", total=0, scanned=0, candidates=0, errors=0, current="")
        base_profiles = fetch_us_company_profiles_from_nasdaq_cache()
        existing_profiles = read_us_company_profile_cache()
        profiles = dict(base_profiles)
        for symbol, profile in existing_profiles.items():
            if symbol in profiles:
                merged = dict(profiles[symbol])
                merged.update({key: value for key, value in profile.items() if value not in ("", None, 0)})
                profiles[symbol] = merged
        target_symbols = missing_us_company_profile_symbols(symbols)
        for symbol in target_symbols:
            profiles.setdefault(symbol, {"symbol": symbol, "cn_name": "", "english_name": "", "sector": "", "industry": "", "concept_tags": ""})
        total = len(target_symbols)
        if total <= 0:
            set_job(job_id, status="done", stage="完成", message="当前候选股公司信息缓存已完整", total=0, scanned=0, candidates=0, errors=0, current="")
            return
        use_fmp = bool(fmp_api_key())
        set_job(job_id, stage="更新中文名", message="正在低速请求东方财富 searchapi", total=total, scanned=0, candidates=0, errors=0)
        session = requests.Session()
        success = 0
        errors = 0
        stopped = False
        for index, symbol in enumerate(target_symbols, 1):
            if job_stop_requested(job_id):
                stopped = True
                break
            set_job(job_id, current=symbol, scanned=index - 1, candidates=success, errors=errors, message="正在补全中文名")
            try:
                old_profile = profiles.get(symbol, {})
                merged = dict(old_profile)
                eastmoney_profile = enrich_us_profile_with_eastmoney(symbol, merged, session=session)
                merged.update({key: value for key, value in eastmoney_profile.items() if value not in ("", None, 0)})
                if use_fmp:
                    try:
                        fmp_profile = fmp_company_profile(symbol)
                        merged.update({key: value for key, value in fmp_profile.items() if value not in ("", None, 0)})
                    except Exception as fmp_exc:
                        merged["fmp_error"] = str(fmp_exc)
                profiles[symbol] = merged
                if merged.get("cn_name"):
                    success += 1
            except Exception as exc:
                errors += 1
                profiles[symbol]["eastmoney_error"] = str(exc)
            set_job(job_id, scanned=index, candidates=success, errors=errors, current=symbol)
            if index % 50 == 0:
                build_us_profile_payload(
                    profiles,
                    "eastmoney.searchapi + optional fmp",
                    f"后台更新进行中：已处理 {index}/{total}。",
                    {
                        "status": "更新中",
                        "scanned": index,
                        "total": total,
                        "cn_name_count": success,
                        "errors": errors,
                        "job_id": job_id,
                    },
                )
            if index < total:
                time.sleep(0.18 + random.random() * 0.22)
        status = "stopped" if stopped else "done"
        stage = "已终止" if stopped else "完成"
        message = "美股公司信息更新已终止，已保留当前进度" if stopped else "美股公司信息更新完成"
        warning = f"已处理 {int(get_job(job_id).get('scanned', 0) if get_job(job_id) else 0)}/{total}；东财失败 {errors} 只。" if stopped or errors else ""
        scanned_final = int(get_job(job_id).get("scanned", total) if get_job(job_id) else total)
        result = build_us_profile_payload(
            profiles,
            "eastmoney.searchapi + optional fmp",
            warning,
            {
                "status": "已终止" if stopped else "已完成",
                "scanned": scanned_final,
                "total": total,
                "cn_name_count": success,
                "errors": errors,
                "job_id": job_id,
            },
        )
        set_job(
            job_id,
            status=status,
            stage=stage,
            message=message,
            current="",
            total=total,
            scanned=scanned_final,
            candidates=success,
            errors=errors,
            data_source="eastmoney.searchapi + optional fmp",
            detail=f"中文名 {success} 只",
        )
        append_task_history(job_id, "us_profile", status, int(get_job(job_id).get("scanned", 0) if get_job(job_id) else 0), success, errors, "eastmoney.searchapi + optional fmp", None, message)
    except Exception as exc:
        set_job(job_id, status="error", stage="失败", message="美股公司信息更新失败", error=str(exc), current="")
        append_task_history(job_id, "us_profile", "error", 0, 0, 1, "eastmoney.searchapi + optional fmp", None, str(exc))


def us_company_profile_for_symbol(symbol: str) -> dict[str, object]:
    return read_us_company_profile_cache().get(normalize_us_profile_symbol(symbol), {})


def merge_us_company_profile(metadata: dict[str, object], symbol: str) -> dict[str, object]:
    profile = us_company_profile_for_symbol(symbol)
    if not profile:
        return metadata
    merged = dict(metadata)
    if profile.get("english_name") and not merged.get("company_name"):
        merged["company_name"] = profile["english_name"]
    for source_key, target_key in (
        ("cn_name", "cn_name"),
        ("company_name", "company_name"),
        ("english_name", "company_name"),
        ("sector", "sector"),
        ("industry", "industry"),
        ("description", "description"),
        ("website", "website"),
        ("exchange", "exchange"),
        ("ceo", "ceo"),
        ("country", "country"),
        ("asset_type", "asset_type"),
        ("market_cap", "market_cap"),
    ):
        if profile.get(source_key):
            merged[target_key] = profile[source_key]
    return merged


def enrich_signal_result(result: SignalResult, metadata: dict[str, object] | None) -> SignalResult:
    metadata = merge_us_company_profile(metadata or {}, result.symbol)
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


def load_us_company_name_overrides() -> dict[str, str]:
    path = Path(__file__).resolve().parent / "config" / "us_company_names_zh.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key).upper(): str(value) for key, value in payload.items() if str(value).strip()}


US_COMPANY_NAME_ZH_OVERRIDES = load_us_company_name_overrides()


def us_company_display_name(symbol: str, company_name: str) -> str:
    profile_name = str(us_company_profile_for_symbol(symbol).get("cn_name") or "")
    if profile_name:
        return profile_name
    override = US_COMPANY_NAME_ZH_OVERRIDES.get(symbol.upper())
    if override:
        return override
    return company_name or symbol.upper()


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
    atomic_write_json(EARNINGS_CACHE_PATH, cache, indent=2)


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


def signal_rating_from_score(score: float) -> str:
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Medium"
    return "Weak"


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


def render_us_company_info_panel(symbol: str, signal_result: SignalResult | None, include_live_profile: bool = True) -> str:
    latest_row = candidate_from_latest_scan(symbol)
    profile = fetch_us_company_profile(symbol) if include_live_profile else {}
    cached_profile = us_company_profile_for_symbol(symbol)
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
    merged.update({key: value for key, value in cached_profile.items() if value not in ("", None, 0)})

    company_name = str(merged.get("company_name") or symbol)
    display_company_name = us_company_display_name(symbol, company_name)
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
    profile_source = str(merged.get("profile_source") or "-")
    return f"""
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">公司名称</div><div class="stat-value">{html.escape(display_company_name)}</div></div>
    <div class="stat-card"><div class="stat-label">英文名称</div><div class="stat-value">{html.escape(company_name)}</div></div>
    <div class="stat-card"><div class="stat-label">证券类型</div><div class="stat-value">{html.escape(asset_type)}</div></div>
    <div class="stat-card"><div class="stat-label">市值</div><div class="stat-value">{format_us_money(merged.get("market_cap"))}</div></div>
    <div class="stat-card"><div class="stat-label">所属板块</div><div class="stat-value">{html.escape(sector)}</div></div>
    <div class="stat-card"><div class="stat-label">所属行业</div><div class="stat-value">{html.escape(industry)}</div></div>
    <div class="stat-card"><div class="stat-label">下一次财报</div><div class="stat-value">{html.escape(earnings_text)}</div></div>
    <div class="stat-card"><div class="stat-label">资料来源</div><div class="stat-value">{html.escape(profile_source)}</div></div>
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
        <td><a href="{html.escape(yahoo_news_url(symbol))}" target="_blank">Yahoo</a> <a href="{html.escape(xueqiu_news_url(symbol, display_company_name))}" target="_blank">雪球</a></td>
      </tr></tbody>
    </table>
  </div>
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
        visible = [row for row in visible if row.technical_rating != "Weak"]
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


SCAN_HISTORY_INDEX_PATH = DATA_DIR / "scan_history_index.json"


def load_scan_history_index() -> dict[str, dict[str, list[str]]]:
    try:
        payload = json.loads(SCAN_HISTORY_INDEX_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"us": {}, "cn": {}}
    except Exception:
        return {"us": {}, "cn": {}}


def update_scan_history_index(market: str, signal_date: str, symbols: list[str]) -> None:
    if not signal_date:
        return
    with locked_path(SCAN_HISTORY_INDEX_PATH):
        payload = load_scan_history_index()
        market_rows = payload.setdefault(market, {})
        market_rows[signal_date] = sorted(set(symbols))
        recent_dates = sorted(market_rows, reverse=True)[:45]
        payload[market] = {day: market_rows[day] for day in recent_dates}
        atomic_write_json(SCAN_HISTORY_INDEX_PATH, payload, indent=2)


def annotate_candidate_history(market: str, signal_date: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    history = load_scan_history_index().get(market, {})
    prior_dates = [day for day in sorted(history, reverse=True) if day < signal_date]
    previous = set(history.get(prior_dates[0], [])) if prior_dates else set()
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        streak = 1
        for day in prior_dates:
            if symbol not in set(history.get(day, [])):
                break
            streak += 1
        row["selection_streak"] = streak
        row["is_new_candidate"] = bool(prior_dates and symbol not in previous)
    return rows


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
            "strong": sum(1 for row in display_rows if row.technical_rating == "Strong"),
            "medium": sum(1 for row in display_rows if row.technical_rating == "Medium"),
            "failed": len(errors),
        },
        "report": f"/reports/{html_path.name}",
        "csv": f"/reports/{csv_path.name}",
        "candidates": [row.__dict__ for row in display_rows],
        "errors": [{"symbol": symbol, "reason": reason} for symbol, reason in errors],
    }
    atomic_write_json(LATEST_SCAN_PATH, payload, indent=2)
    update_scan_history_index("us", end, [row.symbol for row in display_rows])
    try:
        start_us_profile_update_for_symbols([row.symbol for row in display_rows], "latest scan")
    except Exception:
        pass


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
    require_ma5_rising: bool = False,
    require_5ma_gt_20ma: bool = False,
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
    atomic_write_json(NASDAQ_CACHE_PATH, rows)
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
      <a href="/scan/latest">{icon_label("chart", "结果")}</a>
      <a href="{html.escape(str(latest_scan.get("csv", "#")))}" target="_blank">{icon_label("download", "CSV")}</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">{icon_label("trash", "删除")}</button>
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
    require_ma5_rising_checked = " checked" if checkbox_field(params, "require_ma5_rising", False) else ""
    b1_require_20ma_gt_50ma_checked = " checked" if checkbox_field(params, "b1_require_20ma_gt_50ma", False) else ""
    require_5ma_gt_20ma_checked = " checked" if checkbox_field(params, "require_5ma_gt_20ma", False) else ""
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
  <div class="wide risk-note">美股建议并发 6-8；数据源失败变多时先降到 4-6。拆股事件会走本地缓存，只有疑似异常时才补查 yfinance。</div>
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
  <button type="submit">{icon_label("scan", "开始")}</button>
</form>
<section class="progress-box" id="scan-progress">
  <div class="progress-meta" id="scan-status">准备开始</div>
  <div class="progress-track"><div class="progress-bar" id="scan-bar"></div></div>
  <div class="progress-meta" id="scan-detail"></div>
  <div class="progress-actions">
    <button type="button" class="secondary icon-action" id="pause-scan" hidden>{icon_label("pause", "暂停")}</button>
    <button type="button" class="success icon-action" id="resume-scan" hidden>{icon_label("play", "继续")}</button>
    <button type="button" class="danger icon-action" id="stop-scan" hidden>{icon_label("stop", "终止")}</button>
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

    def technical_score_badge(row: SignalResult) -> str:
        rating = row.technical_rating or signal_rating_from_score(row.technical_score)
        cls = {
            "Strong": "score-Strong",
            "Medium": "score-Medium",
            "Weak": "score-Weak",
        }.get(rating, "score-Medium")
        return f'<span class="score-badge {cls}">{row.technical_score:.0f}</span>'

    rendered_rows = []
    rendered_cards = []
    for r in rows:
        filter_attrs = " ".join(
            f'{result_filter_attr(key)}="{1 if result_filter_value(r, key) else 0}"'
            for key, _, _ in OPTIONAL_RESULT_FILTERS
        )
        watch_note = " ".join(part for part in (r.technical_rating or "", r.signal_type or "") if part)
        cached = merge_us_company_profile(r.__dict__, r.symbol)
        sector = us_sector_zh(str(cached.get("sector") or r.sector or ""))
        industry = us_industry_zh(str(cached.get("industry") or r.industry or ""))
        company_display_name = us_company_display_name(r.symbol, str(cached.get("company_name") or r.company_name or ""))
        company_title = " / ".join(
            part
            for part in (
                str(cached.get("company_name") or ""),
                str(cached.get("profile_source") or ""),
            )
            if part
        )
        market_cap = float(cached.get("market_cap") or r.market_cap or 0)
        signal_label = html.escape({'B1_trend_confirm': 'B1', 'B2_reentry': 'B2'}.get(r.signal_type, r.signal_type or '-'))
        reasons_html = render_us_candidate_reason_tags(r)
        score_html = technical_score_badge(r)
        rendered_rows.append(
            f'<tr data-secondary-row {filter_attrs}>'
            f'<td class="action-cell">'
            f'<button type="button" class="mini-action icon-action" data-add-watchlist="{html.escape(r.symbol)}" data-watch-note="{html.escape(watch_note)}">{icon_label("star", "自选")}</button>'
            f'</td>'
            f'<td><button type="button" class="symbol-button" data-candidate-symbol="{html.escape(r.symbol)}">{html.escape(r.symbol)}</button></td>'
            f'<td title="{html.escape(company_title)}">{html.escape(company_display_name)}</td>'
            f"<td>{html.escape(sector)}</td>"
            f"<td>{html.escape(industry)}</td>"
            f"<td>{html.escape(r.signal_date)}</td>"
            f"<td>{signal_label}</td>"
            f"<td>{score_html}</td>"
            f'<td><span class="condition-tags" style="justify-content:flex-start;">{reasons_html}</span></td>'
            f"<td>{earnings_badge(r)}</td>"
            f"<td>{r.close:.2f}</td>"
            f"<td>{r.volume_ratio:.2f}x</td>"
            f"<td>{market_cap / 1_000_000_000:.2f}</td>"
            f"</tr>"
        )
        rendered_cards.append(
            f'<article class="candidate-card" data-secondary-row {filter_attrs}>'
            f'<div class="candidate-card-head">'
            f'<div class="candidate-symbol"><strong>{html.escape(r.symbol)}</strong><span title="{html.escape(company_title)}">{html.escape(company_display_name)}</span></div>'
            f'{score_html}'
            f'</div>'
            f'<div class="candidate-card-meta">'
            f'<div><span>B点</span><b>{signal_label}</b></div>'
            f'<div><span>财报</span><b>{earnings_badge(r)}</b></div>'
            f'<div><span>量比</span><b>{r.volume_ratio:.2f}x</b></div>'
            f'<div><span>板块</span><b>{html.escape(sector)}</b></div>'
            f'<div><span>行业</span><b>{html.escape(industry)}</b></div>'
            f'<div><span>市值</span><b>{market_cap / 1_000_000_000:.2f}B</b></div>'
            f'</div>'
            f'<div class="condition-tags">{reasons_html}</div>'
            f'<div class="candidate-card-actions">'
            f'<button type="button" class="mini-action icon-action" data-add-watchlist="{html.escape(r.symbol)}" data-watch-note="{html.escape(watch_note)}">{icon_label("star", "自选")}</button>'
            f'<button type="button" class="mini-action icon-action" data-candidate-symbol="{html.escape(r.symbol)}">{icon_label("chart", "图")}</button>'
            f'<span class="hint" style="margin:0;">信号日 {html.escape(r.signal_date)} · 收盘 {r.close:.2f}</span>'
            f'</div>'
            f'</article>'
        )
    table_rows = "\n".join(rendered_rows)
    if not table_rows:
        table_rows = '<tr><td colspan="13" class="empty">No visible candidates.</td></tr>'
    card_rows = "\n".join(rendered_cards) if rendered_cards else '<p class="hint">No visible candidates.</p>'
    return f"""
{render_result_filter_panel(params, len(rows))}
<div class="table-wrap">
<table class="resizable-table" data-secondary-filter-table>
  <thead><tr><th>操作</th><th>代码</th><th>公司</th><th>板块</th><th>行业</th><th>信号日</th><th>B点</th><th>技术分</th><th>入选原因</th><th>财报</th><th>收盘</th><th>量比</th><th>市值$B</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
</div>
<details class="detail-disclosure">
  <summary>卡片视图</summary>
  <section class="candidate-decision-grid" data-secondary-card-grid>
  {card_rows}
  </section>
</details>
"""


def render_candidate_table_script(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    query_params = {
        key: values[-1]
        for key, values in params.items()
        if values and not key.startswith("_")
    }
    return f"""
<script>
const candidateBaseParams = {json.dumps(query_params, ensure_ascii=False)};
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
  const host = button.closest(".result") || document.body;
  let detail = host.querySelector("#candidate-detail");
  if (!detail) {{
    detail = document.createElement("section");
    detail.id = "candidate-detail";
    detail.className = "candidate-detail";
    host.appendChild(detail);
  }}
  detail.innerHTML = `<section class="result"><div class="loading-overlay active" style="position:relative; min-height:140px;"><div class="spinner"></div><div>正在生成 ${{symbol}} 的日 K 线和策略交易点</div></div></section>`;
  const params = new URLSearchParams(candidateBaseParams);
  params.set("symbol", symbol);
  const res = await fetch(`/candidate?${{params.toString()}}`);
  const html = await res.text();
  detail.innerHTML = html;
  detail.scrollIntoView({{ behavior: "smooth", block: "start" }});
}});
</script>
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
<section class="status-strip tv-summary-strip">
  <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{html.escape(end)}</div></div>
  <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{plan_date.isoformat()}</div></div>
  <div class="stat-card"><div class="stat-label">扫描 / 技术候选</div><div class="stat-value">{symbols_count} / {technical_count}</div></div>
  <div class="stat-card"><div class="stat-label">显示 / 失败</div><div class="stat-value">{visible_count} / {errors_count}</div></div>
</section>
<p class="hint" style="margin:8px 10px 0;">股票池：{html.escape(source)}。这里只使用已完成的日 K 线；信号日期出现 B 点，代表策略可在下一交易日开盘执行。</p>
"""


def latest_scan_to_html() -> str:
    latest = load_latest_scan()
    if not latest:
        return f"""
<section class="page-head">
  <div>
    <h1>当前选股结果</h1>
    <p class="hint">当前信号日期还没有保存的扫描结果。</p>
  </div>
  <div class="mode-pill">Daily Close</div>
</section>
<section class="result">
  <div class="inline-actions links">
    <a href="/us/scanner">{icon_label("back", "返回选股")}</a>
  </div>
</section>
"""
    candidates = [SignalResult(**row) for row in latest.get("candidates", [])]
    candidates.sort(key=lambda row: (row.technical_score, row.avg_dollar_volume_20d), reverse=True)
    try:
        start_us_profile_update_for_symbols([row.symbol for row in candidates], "latest scan page")
    except Exception:
        pass
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
<section class="page-head">
  <div>
    <h1>当前选股结果</h1>
    <p class="hint">这里显示当天保存的美股选股结果。点击单个股票可在下方查看日 K、策略点和基础信息。</p>
  </div>
  <div class="mode-pill">Daily Close</div>
</section>
<section class="result tv-workbench">
  <div class="tv-workbench-head">
    <div class="tv-workbench-title">
      <strong>候选列表</strong>
      <span>{len(candidates)} symbols · Daily Close</span>
    </div>
    <div class="tv-workbench-actions inline-actions links">
      <a href="/us/scanner">{icon_label("back", "选股")}</a>
      <a href="{report}" target="_blank">{icon_label("chart", "报告")}</a>
      <a href="{csv_url}" target="_blank">{icon_label("download", "CSV")}</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">{icon_label("trash", "删除")}</button>
      </form>
    </div>
  </div>
  {render_scan_summary(source, int(summary.get("scanned", 0)), int(summary.get("technical_candidates", 0)), int(summary.get("visible_candidates", len(candidates))), int(summary.get("failed", len(errors))), end)}
  <div class="tv-workbench-body">
    {render_candidate_table(candidates, saved_params)}
    {render_failure_table(errors)}
  </div>
</section>
{render_candidate_table_script(saved_params)}
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
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
    require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
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
    rows.sort(key=lambda row: (row.technical_score, row.avg_dollar_volume_20d), reverse=True)
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
      <a href="/reports/{quote(html_path.name)}" target="_blank">{icon_label("chart", "报告")}</a>
      <a href="/reports/{quote(csv_path.name)}" target="_blank">{icon_label("download", "CSV")}</a>
      <form class="delete-form" action="/scan/delete" method="get" onsubmit="return confirm('确认删除当前扫描结果？删除后页面将恢复为未扫描状态。');">
        <button type="submit" class="delete-link">{icon_label("trash", "删除")}</button>
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
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        raise ValueError("Missing symbol")

    scan_end = default_scan_end_date()
    start = field(params, "start", default_scan_start_date(scan_end).isoformat())
    end = field(params, "end", scan_end.isoformat())
    latest_row = candidate_from_latest_scan(symbol)
    signal_result = SignalResult(**latest_row) if latest_row else None
    company_info_panel = render_us_company_info_panel(symbol, signal_result, include_live_profile=False)
    detail_panel = ""
    if signal_result:
        plan_date = next_market_weekday(date.fromisoformat(signal_result.signal_date)).isoformat()
        detail_panel = f"""
  <section class="status-strip">
    <div class="stat-card"><div class="stat-label">信号日期</div><div class="stat-value">{html.escape(signal_result.signal_date)}</div></div>
    <div class="stat-card"><div class="stat-label">计划买入日</div><div class="stat-value">{plan_date}</div></div>
    <div class="stat-card"><div class="stat-label">信号类型</div><div class="stat-value">{html.escape(signal_result.signal_type)}</div></div>
    <div class="stat-card"><div class="stat-label">20日均成交额</div><div class="stat-value">{signal_result.avg_dollar_volume_20d / 1_000_000:.1f}M</div></div>
  </section>
"""
    chart_params = urlencode_candidate_params(params, symbol)
    chart_url = f"/candidate/chart?{chart_params}"
    return f"""
<section class="result">
  <p class="links"><strong>{html.escape(symbol)}</strong></p>
  <p class="hint">下方只用于看图确认候选股：保留 K 线、均线、成交量、KDJ 和策略信号点；收益统计请在回测页面查看。</p>
  {company_info_panel}
  {detail_panel}
  <iframe class="candidate-chart-frame" src="{chart_url}" title="{html.escape(symbol)} candidate chart"></iframe>
</section>
"""


def urlencode_candidate_params(params: dict[str, list[str]], symbol: str) -> str:
    flat = {
        key: values[-1]
        for key, values in params.items()
        if values and not key.startswith("_")
    }
    flat["symbol"] = symbol
    flat.setdefault("preset", "1y")
    flat["fast"] = "1"
    return urlencode(flat)


def render_strategy_chart_payload(payload: dict[str, object]) -> bytes:
    error = str(payload.get("error", "")) if isinstance(payload, dict) else ""
    body = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #fff; color: #131722; font-family: Inter, "Microsoft YaHei UI", "PingFang SC", Arial, sans-serif; }}
.frame {{ height: 100vh; min-height: 420px; display: grid; grid-template-rows: 42px minmax(0, 1fr); overflow: hidden; }}
.chart-toolbar {{ min-width: 0; display: flex; align-items: center; gap: 8px; padding: 5px 8px; overflow-x: auto; border-bottom: 1px solid #d9dde5; background: #f7f8fa; scrollbar-width: thin; }}
.chart-meta {{ min-width: 140px; display: grid; gap: 1px; margin-right: 2px; }}
.chart-meta strong {{ overflow: hidden; color: #131722; font-size: 13px; white-space: nowrap; text-overflow: ellipsis; }}
.chart-meta span {{ color: #787b86; font-size: 10px; white-space: nowrap; }}
.periods {{ display: inline-flex; flex: 0 0 auto; overflow: hidden; border: 1px solid #d9dde5; border-radius: 6px; background: #fff; }}
.periods button {{ min-width: 34px; height: 28px; padding: 0 7px; color: #4f5360; border: 0; border-right: 1px solid #eceef2; background: transparent; cursor: pointer; font-size: 11px; font-weight: 800; }}
.periods button:last-child {{ border-right: 0; }}
.periods button:hover {{ background: #f3f6fc; }}
.periods button.active {{ color: #fff; background: #2962ff; }}
.chart-divider {{ width: 1px; height: 22px; flex: 0 0 auto; background: #d9dde5; }}
.chart-toggle {{ height: 28px; display: inline-flex; align-items: center; gap: 5px; flex: 0 0 auto; padding: 0 8px; color: #4f5360; border: 1px solid #d9dde5; border-radius: 6px; background: #fff; cursor: pointer; font-size: 11px; font-weight: 750; }}
.chart-toggle:hover {{ background: #f3f6fc; }}
.chart-toggle input {{ width: 13px; height: 13px; margin: 0; accent-color: #2962ff; }}
.icon-tool {{ width: 30px; height: 28px; display: grid; place-items: center; flex: 0 0 auto; padding: 0; color: #4f5360; border: 1px solid #d9dde5; border-radius: 6px; background: #fff; cursor: pointer; }}
.icon-tool:hover {{ color: #2962ff; background: #eaf0ff; }}
.icon-tool svg {{ width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }}
.chart-stack {{ min-height: 0; display: grid; grid-template-rows: minmax(260px, 3fr) minmax(115px, 1fr); gap: 0; padding: 0 6px 6px; }}
.chart-stack.hide-kdj {{ grid-template-rows: minmax(0, 1fr) 0; }}
#price, #kdj {{ min-height: 0; }}
#kdj {{ overflow: hidden; border-top: 1px solid #eceef2; }}
.chart-stack.hide-kdj #kdj {{ display: none; }}
.error {{ margin: 20px; padding: 12px; border: 1px solid #ffc9cf; background: #fff5f6; color: #b42332; border-radius: 6px; }}
@media (max-width: 620px) {{ .chart-meta {{ min-width: 105px; }} .chart-toolbar {{ gap: 6px; }} .chart-toggle {{ padding: 0 6px; }} }}
</style>
</head>
<body>
<div class="frame">
  <div class="chart-toolbar">
    <div class="chart-meta"><strong id="title">策略图表</strong><span id="range"></span></div>
    <div class="periods" id="periods">
      <button type="button" data-preset="1m">1M</button><button type="button" data-preset="3m">3M</button><button type="button" data-preset="6m">6M</button><button type="button" data-preset="1y">1Y</button><button type="button" data-preset="3y">3Y</button><button type="button" data-preset="5y">5Y</button>
    </div>
    <span class="chart-divider"></span>
    <label class="chart-toggle"><input id="toggle-ma5" type="checkbox" checked>MA5</label>
    <label class="chart-toggle"><input id="toggle-ma20" type="checkbox" checked>MA20</label>
    <label class="chart-toggle"><input id="toggle-volume" type="checkbox" checked>成交量</label>
    <label class="chart-toggle"><input id="toggle-kdj" type="checkbox" checked>KDJ</label>
    <label class="chart-toggle"><input id="toggle-signals" type="checkbox">B/S信号</label>
    <label class="chart-toggle"><input id="toggle-defense" type="checkbox">策略线</label>
    <button class="icon-tool" id="fit-chart" type="button" title="适应窗口" aria-label="适应窗口"><svg viewBox="0 0 24 24"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5M3 8l6-5M21 8l-6-5M3 16l6 5M21 16l-6 5"/></svg></button>
  </div>
  <div class="chart-stack" id="chart-stack"><div id="price"></div><div id="kdj"></div></div>
</div>
<script type="application/json" id="payload">{json.dumps(payload, ensure_ascii=False, allow_nan=False)}</script>
<script>
const payload = JSON.parse(document.getElementById("payload").textContent);
if (payload.error) {{
  document.body.innerHTML = `<div class="error">${{payload.error}}</div>`;
}} else {{
  const toLine = rows => (rows || []).map(row => row.time ? row : ({{ time: row.x, value: row.y }})).filter(row => row.value !== null && row.value !== undefined);
  const ohlc = (payload.ohlc || []).map(row => row.time ? row : ({{ time: row.x, open: row.open, high: row.high, low: row.low, close: row.close }}));
  const volumeData = (payload.volume || []).map(row => row.time ? row : ({{ time: row.x, value: row.y, color: row.color }}));
  const ma5Data = toLine(payload.ma || payload.ma5 || payload.zx_short_trend);
  const ma20Data = toLine(payload.ma20 || payload.zx_multi_trend);
  const volMaData = toLine(payload.volMa || payload.volume_ma20);
  const volThresholdData = toLine(payload.volThreshold);
  const kData = toLine(payload.kdjK || payload.k);
  const dData = toLine(payload.kdjD || payload.d);
  const jData = toLine(payload.kdjJ || payload.j);
  const dates = ohlc.map(row => row.time);
  const firstDate = payload.start || dates[0] || "";
  const lastDate = payload.end || dates[dates.length - 1] || "";
  document.getElementById("title").textContent = `${{payload.symbol}}${{payload.name ? " · " + payload.name : ""}}`;
  document.getElementById("range").textContent = `${{firstDate}} - ${{lastDate}}`;
  const activePreset = payload.preset || new URL(location.href).searchParams.get("preset") || "1y";
  document.querySelectorAll("[data-preset]").forEach(button => button.classList.toggle("active", button.dataset.preset === activePreset));
  document.getElementById("periods").addEventListener("click", event => {{
    const button = event.target.closest("[data-preset]");
    if (!button || button.dataset.preset === activePreset) return;
    const next = new URL(location.href);
    next.searchParams.set("preset", button.dataset.preset);
    localStorage.setItem("ma5.chart.preset", button.dataset.preset);
    location.href = next.toString();
  }});
  const priceEl = document.getElementById("price");
  const kdjEl = document.getElementById("kdj");
  const baseOptions = {{
    layout: {{ background: {{ type: "solid", color: "#ffffff" }}, textColor: "#131722", fontFamily: "Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif" }},
    rightPriceScale: {{ borderColor: "#d6dbe3" }},
    timeScale: {{ borderColor: "#d6dbe3", rightOffset: 6, barSpacing: 8, minBarSpacing: 3 }},
    grid: {{ vertLines: {{ color: "#f1f3f6" }}, horzLines: {{ color: "#f1f3f6" }} }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    handleScroll: {{ mouseWheel: true, pressedMouseMove: true }},
    handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
  }};
  const chart = LightweightCharts.createChart(priceEl, {{ ...baseOptions, width: priceEl.clientWidth, height: priceEl.clientHeight, rightPriceScale: {{ borderColor: "#d6dbe3", scaleMargins: {{ top: 0.08, bottom: 0.28 }} }} }});
  const candle = chart.addCandlestickSeries({{ upColor: "#089981", downColor: "#f23645", borderUpColor: "#089981", borderDownColor: "#f23645", wickUpColor: "#089981", wickDownColor: "#f23645", priceLineVisible: false }});
  candle.setData(ohlc);
  const ma5Series = chart.addLineSeries({{ color: "#f5a623", lineWidth: 2, title: "5MA", priceLineVisible: false }});
  const ma20Series = chart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, title: "20MA", priceLineVisible: false, lastValueVisible: false }});
  ma5Series.setData(ma5Data);
  ma20Series.setData(ma20Data);
  const ma5Stop = chart.addLineSeries({{ color: "#ef4444", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: `5MA-${{payload.ma5StopPct || 7.5}}%`, priceLineVisible: false, lastValueVisible: false }});
  const volume = chart.addHistogramSeries({{ priceScaleId: "", priceFormat: {{ type: "volume" }}, priceLineVisible: false, lastValueVisible: false }});
  chart.priceScale("").applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
  const volMaSeries = chart.addLineSeries({{ color: "#2962ff", lineWidth: 1, priceScaleId: "", title: "成交量均线", priceLineVisible: false, lastValueVisible: false }});
  const volThresholdSeries = chart.addLineSeries({{ color: "#f97316", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, priceScaleId: "", title: `${{payload.volMultiplier || ""}}x Vol`, priceLineVisible: false, lastValueVisible: false }});
  volume.setData(volumeData);
  volMaSeries.setData(volMaData);
  volThresholdSeries.setData(volThresholdData);
  function refreshMarkers() {{
    const baseMarkers = payload.markers || [];
    const ashareSignals = (payload.signals || []).map(row => ({{ time: row.x, position: "belowBar", color: "#089981", shape: "circle", text: row.text || "B" }}));
    const signalMarkers = document.getElementById("toggle-signals")?.checked ? ([...(payload.signalMarkers || []), ...ashareSignals]) : [];
    candle.setMarkers([...baseMarkers, ...signalMarkers].sort((a, b) => String(a.time).localeCompare(String(b.time))));
  }}
  refreshMarkers();
  const kdjChart = LightweightCharts.createChart(kdjEl, {{ ...baseOptions, width: kdjEl.clientWidth, height: kdjEl.clientHeight }});
  kdjChart.addLineSeries({{ color: "#2563eb", lineWidth: 1.5, title: "K", priceLineVisible: false }}).setData(kData);
  kdjChart.addLineSeries({{ color: "#f59e0b", lineWidth: 1.5, title: "D", priceLineVisible: false }}).setData(dData);
  kdjChart.addLineSeries({{ color: "#7c3aed", lineWidth: 2, title: "J", priceLineVisible: false }}).setData(jData);
  kdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "80", priceLineVisible: false, lastValueVisible: false }}).setData(dates.map(time => ({{ time, value: 80 }})));
  kdjChart.addLineSeries({{ color: "#94a3b8", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "20", priceLineVisible: false, lastValueVisible: false }}).setData(dates.map(time => ({{ time, value: 20 }})));
  function refreshChartOptions() {{
    ma5Series.setData(document.getElementById("toggle-ma5").checked ? ma5Data : []);
    ma20Series.setData(document.getElementById("toggle-ma20").checked ? ma20Data : []);
    const showVolume = document.getElementById("toggle-volume").checked;
    volume.setData(showVolume ? volumeData : []);
    volMaSeries.setData(showVolume ? volMaData : []);
    volThresholdSeries.setData(showVolume ? volThresholdData : []);
    ma5Stop.setData(document.getElementById("toggle-defense").checked ? toLine(payload.ma5Stop) : []);
    document.getElementById("chart-stack").classList.toggle("hide-kdj", !document.getElementById("toggle-kdj").checked);
    refreshMarkers();
  }}
  const chartStateKey = "ma5.chart.controls.v1";
  try {{
    const saved = JSON.parse(localStorage.getItem(chartStateKey) || "{{}}");
    document.querySelectorAll(".chart-toggle input").forEach(input => {{ if (typeof saved[input.id] === "boolean") input.checked = saved[input.id]; }});
  }} catch {{}}
  document.querySelectorAll(".chart-toggle input").forEach(input => input.addEventListener("change", () => {{
    let state = {{}}; try {{ state = JSON.parse(localStorage.getItem(chartStateKey) || "{{}}"); }} catch {{}} document.querySelectorAll(".chart-toggle input").forEach(item => state[item.id] = item.checked);
    localStorage.setItem(chartStateKey, JSON.stringify(state)); refreshChartOptions();
  }}));
  refreshChartOptions();
  let syncing = false;
  chart.timeScale().subscribeVisibleLogicalRangeChange(range => {{ if (!range || syncing) return; syncing = true; kdjChart.timeScale().setVisibleLogicalRange(range); syncing = false; }});
  kdjChart.timeScale().subscribeVisibleLogicalRangeChange(range => {{ if (!range || syncing) return; syncing = true; chart.timeScale().setVisibleLogicalRange(range); syncing = false; }});
  chart.timeScale().fitContent();
  kdjChart.timeScale().fitContent();
  document.getElementById("fit-chart").addEventListener("click", () => {{ chart.timeScale().fitContent(); kdjChart.timeScale().fitContent(); }});
  new ResizeObserver(entries => chart.applyOptions({{ width: Math.floor(entries[0].contentRect.width), height: Math.floor(entries[0].contentRect.height) }})).observe(priceEl);
  new ResizeObserver(entries => kdjChart.applyOptions({{ width: Math.floor(entries[0].contentRect.width), height: Math.floor(entries[0].contentRect.height) }})).observe(kdjEl);
}}
</script>
</body>
</html>
"""
    return body.encode("utf-8")


def render_candidate_chart_frame(params: dict[str, list[str]]) -> bytes:
    payload = watchlist_chart_payload(params)
    if isinstance(payload, dict):
        payload["market"] = "us"
    return render_strategy_chart_payload(payload)


def render_ashare_candidate_chart_frame(params: dict[str, list[str]]) -> bytes:
    try:
        payload = ashare_chart_payload(
            field(params, "symbol", ""),
            number_field(params, "j_threshold", 14.0),
            b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", False),
            require_ma5_rising=checkbox_field(params, "require_ma5_rising", False),
            require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", False),
            preset=field(params, "preset", "1y").lower(),
        )
        payload["market"] = "cn"
    except Exception as exc:
        payload = {"error": str(exc), "market": "cn"}
    return render_strategy_chart_payload(payload)


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
    metadata = merge_us_company_profile(metadata or {}, symbol)
    group = item.get("group", "观察")
    note = item.get("note", "")
    company = str(metadata.get("company_name", "") or "-")
    display_company = us_company_display_name(symbol, company if company != "-" else "")
    sector = str(metadata.get("sector", "") or "-")
    industry = str(metadata.get("industry", "") or "-")
    market_cap = float(metadata.get("market_cap", 0) or 0)
    latest_close = "-"
    change_pct = "-"
    dist_ma = "-"
    vol_ratio = "-"
    b_status = "-"
    ma_status = "-"
    s_status = "-"
    earnings = "未知"
    cache_bars = read_price_cache(symbol)
    try:
        end = default_scan_end_date()
        bars = [bar for bar in cache_bars if date.fromisoformat(bar.date) <= end][-120:]
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
            buy_signal, _, _, _, _, _ = build_ratchet_inputs(bars, 5, 20, 1.45, 4.5 / 100, symbol=symbol)
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
        "company": display_company,
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
        f'<button type="button" class="watch-row-button" data-watch-symbol="{html.escape(symbol)}" {data_attrs}><span class="watch-symbol-line"><span>{html.escape(symbol)}</span><span>{html.escape(change_pct)}</span></span><span class="watch-meta-line">{html.escape(display_company)} · {html.escape(group)}</span></button>'
        f'<span class="watch-row-actions"><a class="delete-link" href="/watchlist/delete?symbol={quote(symbol)}" onclick="return confirm(\'确认从自选池删除 {html.escape(symbol)}？\');">{icon_label("trash", "删")}</a></span>'
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
<div class="watchlist-page">
<section class="page-head">
  <div>
    <h1>自选池</h1>
  </div>
  <div class="mode-pill">Watchlist | Daily</div>
</section>
<form class="form" action="/watchlist/add" method="get">
  <label>代码<input name="symbol" value="{html.escape(field(params, "symbol", ""))}" placeholder="NVDA"></label>
  <label>分组<input name="group" value="{html.escape(field(params, "group", "观察"))}" placeholder="AI / 半导体 / 观察"></label>
  <label class="wide">备注<input name="note" value="{html.escape(field(params, "note", ""))}" placeholder="关注原因"></label>
  <button type="submit">{icon_label("plus", "添加")}</button>
</form>
<section class="status-strip">
  <div class="stat-card"><div class="stat-label">数量</div><div class="stat-value">{len(symbols)}</div></div>
  <div class="stat-card"><div class="stat-label">缓存</div><div class="stat-value">{cache["cached_symbols"]}/{len(symbols)}</div></div>
  <div class="stat-card"><div class="stat-label">最新</div><div class="stat-value">{html.escape(str(cache["latest"]))}</div></div>
  <div class="stat-card"><div class="stat-label">容量</div><div class="stat-value">{float(cache["size_mb"]):.1f} MB</div></div>
</section>
<section class="watchlist-grid">
  <div class="watchlist-panel">
    <div class="watchlist-panel-head">
      <div>
        <strong>列表</strong>
      </div>
      <div class="watchlist-count-pill">{len(symbols)}</div>
    </div>
    <div class="table-wrap watchlist-list-wrap">
      <table id="watchlist-table">
        <thead><tr><th>自选</th><th>Close</th><th>Tags</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
  <div class="watchlist-panel">
    <div class="watchlist-chart-head">
      <div>
        <strong id="watch-chart-title">{html.escape(default_symbol) if default_symbol else "选择一个股票"}</strong>
        <p class="hint" id="watch-chart-subtitle"></p>
      </div>
    </div>
    <div class="watch-detail-grid" id="watch-detail-grid">
      <div class="watch-detail-item"><span>公司</span><strong id="watch-detail-company">-</strong></div>
      <div class="watch-detail-item"><span>市值</span><strong id="watch-detail-market">-</strong></div>
      <div class="watch-detail-item"><span>行业</span><strong id="watch-detail-industry">-</strong></div>
      <div class="watch-detail-item"><span>加入</span><strong id="watch-detail-added">-</strong></div>
      <div class="watch-detail-item"><span>技术</span><strong id="watch-detail-tech">-</strong></div>
      <div class="watch-detail-item"><span>信号</span><strong id="watch-detail-signal">-</strong></div>
      <div class="watch-detail-item"><span>财报</span><strong id="watch-detail-earnings">-</strong></div>
      <div class="watch-detail-item"><span>备注</span><strong id="watch-detail-note">-</strong></div>
    </div>
    <details class="divergence-panel">
      <summary class="divergence-head">
        <strong>{icon_label("alert", "分歧事件")}</strong>
      </summary>
      <div class="divergence-head">
        <span class="hint">D+13 到 D+27</span>
      </div>
      <form class="divergence-form" action="/watchlist/divergence/add" method="get">
        <input type="hidden" name="symbol" id="divergence-symbol-input" value="{html.escape(default_symbol)}">
        <label>事件日期<input type="date" name="event_date" value="{html.escape(default_event_date)}"></label>
        <label>方向<select name="event_type"><option value="bullish">利好</option><option value="bearish">利空</option></select></label>
        <label>分歧类型<select name="divergence_type"><option value="good_news_ignored">利好未涨</option><option value="bad_news_resilient">利空不跌</option></select></label>
        <label>级别<select name="importance"><option value="major">重大</option><option value="medium">中等</option><option value="minor">一般</option></select></label>
        <label>备注<input name="note" placeholder="财报、政策、合同、监管等"></label>
        <button type="submit">{icon_label("plus", "保存")}</button>
      </form>
      <div class="divergence-list" id="divergence-list">{render_divergence_event_list(default_symbol) if default_symbol else '<div class="divergence-empty">先选择一个股票。</div>'}</div>
    </details>
    <div class="chart-control-panel">
      <div class="chart-control-title"><span>显示</span></div>
      <div class="period-tabs" id="watch-periods">
        <button type="button" data-preset="1m">1M</button>
        <button type="button" data-preset="3m">3M</button>
        <button type="button" data-preset="6m">6M</button>
        <button type="button" data-preset="1y" class="active">1Y</button>
        <button type="button" data-preset="3y">3Y</button>
        <button type="button" data-preset="5y">5Y</button>
      </div>
      <div class="chart-toggle-row" id="watch-strategy-options">
        <label class="chart-toggle"><input data-watch-condition="require_ma5_rising" type="checkbox">MA5向上</label>
        <label class="chart-toggle"><input data-watch-condition="require_5ma_gt_20ma" type="checkbox">MA5&gt;MA20</label>
        <label class="chart-toggle"><input data-watch-condition="b1_require_20ma_gt_50ma" type="checkbox">20MA&gt;50MA</label>
      </div>
      <div class="chart-toggle-row">
        <label class="chart-toggle"><input id="watch-toggle-ma5-stop-25" type="checkbox">2.5%防守线</label>
        <label class="chart-toggle"><input id="watch-toggle-ma5-stop-strategy" type="checkbox" checked>策略防守线</label>
        <label class="chart-toggle"><input id="watch-toggle-signal-markers" type="checkbox">B/S信号日</label>
      </div>
    </div>
    <div class="watchlist-chart-shell">
      <div id="watchlist-chart" class="watchlist-chart watchlist-price-chart"></div>
      <div id="watchlist-kdj-chart" class="watchlist-chart watchlist-kdj-chart"></div>
      <div id="watchlist-tooltip" class="chart-tooltip"></div>
      <div id="watch-chart-loading" class="loading-overlay"><div class="spinner"></div><div>加载中</div></div>
    </div>
  </div>
</section>
</div>
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
const watchToggleMa5Stop25 = document.getElementById("watch-toggle-ma5-stop-25");
const watchToggleMa5StopStrategy = document.getElementById("watch-toggle-ma5-stop-strategy");
const watchToggleSignalMarkers = document.getElementById("watch-toggle-signal-markers");
const watchStrategyOptions = document.getElementById("watch-strategy-options");
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
    divergenceList.innerHTML = '<div class="divergence-empty">暂无事件</div>';
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
      <a class="delete-link" href="/watchlist/divergence/delete?id=${{encodeURIComponent(event.id)}}&symbol=${{encodeURIComponent(event.symbol || watchCurrentSymbol)}}" onclick="return confirm('确认删除这条分歧事件？');">{icon_label("trash", "删")}</a>
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
  const ma5Stop25 = watchChart.addLineSeries({{ color: "#dc2626", lineWidth: 1, title: "5MA-2.5%", priceLineVisible: false, lastValueVisible: false }});
  const ma5Stop = watchChart.addLineSeries({{ color: "#ef4444", lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: `5MA-${{payload.ma5StopPct || 7.5}}%`, priceLineVisible: false, lastValueVisible: false }});
  function refreshWatchDefenseLines() {{
    ma5Stop25.setData(watchToggleMa5Stop25?.checked ? (payload.ma5Stop25 || []) : []);
    ma5Stop.setData(watchToggleMa5StopStrategy?.checked ? (payload.ma5Stop || []) : []);
  }}
  refreshWatchDefenseLines();
  if (watchToggleMa5Stop25) watchToggleMa5Stop25.onchange = refreshWatchDefenseLines;
  if (watchToggleMa5StopStrategy) watchToggleMa5StopStrategy.onchange = refreshWatchDefenseLines;
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
  function refreshWatchMarkers() {{
    const baseMarkers = payload.markers || [];
    const signalMarkers = watchToggleSignalMarkers?.checked ? (payload.signalMarkers || []) : [];
    candle.setMarkers([...baseMarkers, ...signalMarkers].sort((a, b) => String(a.time).localeCompare(String(b.time))));
  }}
  refreshWatchMarkers();
  if (watchToggleSignalMarkers) watchToggleSignalMarkers.onchange = refreshWatchMarkers;
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
  watchSubtitle.textContent = "加载中";
  watchLoading?.classList.add("active");
  try {{
    const params = new URLSearchParams({{ symbol, preset }});
    watchStrategyOptions?.querySelectorAll("[data-watch-condition]").forEach(input => {{
      params.set(input.dataset.watchCondition, input.checked ? "1" : "0");
    }});
    const res = await fetch(`/watchlist/chart?${{params.toString()}}`);
    const payload = await res.json();
    if (payload.error) {{
      watchSubtitle.textContent = payload.error;
      destroyWatchChart();
      window.showToast?.(payload.error, "error");
      return;
    }}
    watchSubtitle.textContent = `${{payload.start}} - ${{payload.end}}`;
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
  document.querySelectorAll("[data-watch-symbol]").forEach(item => item.classList.remove("is-active"));
  button.classList.add("is-active");
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
watchStrategyOptions?.addEventListener("change", event => {{
  if (!event.target.closest("[data-watch-condition]")) return;
  loadWatchChart(watchCurrentSymbol, watchCurrentPreset);
}});
if (window.initializeResizableTables) initializeResizableTables(document);
if (window.initializeSortableTables) initializeSortableTables(document);
const initialWatchButton = document.querySelector(`[data-watch-symbol="${{watchInitialSymbol}}"]`) || document.querySelector("[data-watch-symbol]");
if (initialWatchButton) {{
  initialWatchButton.classList.add("is-active");
  updateWatchDetail(initialWatchButton);
}}
if (watchInitialSymbol) loadWatchChart(watchInitialSymbol, "1y");
</script>
"""


def watchlist_chart_payload(params: dict[str, list[str]]) -> dict[str, object]:
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        return {"error": "缺少股票代码。"}
    preset = field(params, "preset", "1y").lower()
    vol_multiplier = number_field(params, "vol_multiplier", 1.45)
    require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
    require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
    b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
    end_day = default_scan_end_date()
    start_day = chart_start_for_preset(preset, end_day)
    try:
        fast_mode = field(params, "fast", "0") == "1"
        cached = read_price_cache(symbol) if fast_mode else []
        bars = slice_bars(cached, start_day.isoformat(), end_day.isoformat()) if cached else []
        if len(bars) < 2:
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
            b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
            require_ma5_rising=require_ma5_rising,
            require_5ma_gt_20ma=require_5ma_gt_20ma,
            symbol=symbol,
        )
    except Exception as exc:
        return {"error": str(exc)}
    execution_markers = [
        {
            "time": str(row.get("date", "")),
            "position": "belowBar",
            "color": "#089981",
            "shape": "arrowUp",
            "text": str(row.get("buy_action_stage", "") or "买"),
        }
        for index, row in enumerate(equity_curve)
        if float(row.get("position_shares", 0) or 0)
        > (float(equity_curve[index - 1].get("position_shares", 0) or 0) if index > 0 else 0.0)
    ] + [
        {"time": trade.exit_date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": "卖"}
        for trade in trades
    ]
    signal_markers: list[dict[str, object]] = []
    markers = list(execution_markers)
    holding_periods: list[dict[str, str]] = []
    holding_start = ""
    previous_position = 0.0
    for row in equity_curve:
        current_position = float(row.get("position_shares", 0) or 0)
        current_date = str(row.get("date", ""))
        if previous_position <= 0 < current_position:
            holding_start = current_date
        elif previous_position > 0 >= current_position and holding_start:
            holding_periods.append({"start": holding_start, "end": current_date, "label": "持仓"})
            holding_start = ""
        previous_position = current_position
    if holding_start and equity_curve:
        holding_periods.append({"start": holding_start, "end": str(equity_curve[-1].get("date", "")), "label": "持仓中"})
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
    ma5_stop_points = []
    ma5_stop_25_points = []
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
            ma5_stop_points.append({"time": bar.date, "value": ma * 0.925})
            ma5_stop_25_points.append({"time": bar.date, "value": ma * 0.975})
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
            signal_markers.append({"time": bar.date, "position": "belowBar", "color": "#2563eb" if stage == "B2" else "#84cc16", "shape": "circle", "text": stage})
        if position > 0 and int(row.get("sell_signal", 0)) and bar.date not in trade_exit_dates:
            signal_markers.append({"time": bar.date, "position": "aboveBar", "color": "#f97316", "shape": "circle", "text": "S"})
    return {
        "symbol": symbol,
        "preset": preset,
        "start": bars[0].date,
        "end": bars[-1].date,
        "ohlc": [{"time": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": volume_points,
        "ma": ma_points,
        "ma20": ma20_points,
        "ma5Stop": ma5_stop_points,
        "ma5Stop25": ma5_stop_25_points,
        "ma5StopPct": 7.5,
        "volMa": vol_ma_points,
        "volThreshold": vol_threshold_points,
        "volMultiplier": vol_multiplier,
        "conditions": {
            "require_ma5_rising": require_ma5_rising,
            "require_5ma_gt_20ma": require_5ma_gt_20ma,
            "b1_require_20ma_gt_50ma": b1_require_20ma_gt_50ma,
        },
        "kdjK": k_points,
        "kdjD": d_points,
        "kdjJ": j_points,
        "kdjUpper": [{"time": bar.date, "value": 80} for bar in bars],
        "kdjLower": [{"time": bar.date, "value": 20} for bar in bars],
        "dynamicStop": dynamic_points,
        "trendStop": trend_stop_points,
        "markers": sorted(markers, key=lambda item: str(item["time"])),
        "executionMarkers": sorted(execution_markers, key=lambda item: str(item["time"])),
        "signalMarkers": sorted(signal_markers, key=lambda item: str(item["time"])),
        "holdingPeriods": holding_periods,
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
    rows.sort(key=lambda row: (row.technical_score, row.avg_dollar_volume_20d), reverse=True)
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
    profile_job_id = ""
    profile_missing_count = 0
    display_symbols = [row.symbol for row in display_rows]
    if display_symbols:
        try:
            profile_missing_count = len(missing_us_company_profile_symbols(display_symbols))
            profile_job_id = start_us_profile_update_for_symbols(display_symbols, "scan results")
        except Exception:
            profile_job_id = ""

    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {message[:80]}" for symbol, message in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 个代码扫描失败：{html.escape(sample)}</p>'
    profile_note = ""
    if profile_job_id:
        profile_note = f'<p class="hint">公司信息缓存：发现 {profile_missing_count} 只候选缺公司信息，已后台更新 {html.escape(profile_job_id)}。</p>'

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
  {profile_note}
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
        b1_require_20ma_gt_50ma = checkbox_field(params, "b1_require_20ma_gt_50ma", False)
        require_ma5_rising = checkbox_field(params, "require_ma5_rising", False)
        require_5ma_gt_20ma = checkbox_field(params, "require_5ma_gt_20ma", False)
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
                    append_task_history(job_id, "us", "stopped", scanned, len(rows), len(errors), source, params, "已终止，保留当前结果")
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
        append_task_history(job_id, "us", "done", len(symbols), len(rows), len(errors), source, params, "扫描完成")
    except Exception as exc:
        set_job(job_id, status="error", message="扫描失败", error=str(exc))
        append_task_history(job_id, "us", "error", 0, 0, 1, "", params, str(exc))


def execute_ashare_scan_job(job_id: str, params: dict[str, list[str]]) -> None:
    try:
        set_job(
            job_id,
            status="running",
            stage="准备股票池",
            message="正在拉取 A 股股票池",
            detail="优先使用通达信股票列表，并用 Tencent 行情补充总市值；失败后使用本地缓存兜底。",
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
            set_job(job_id, stage="拉取股票池", message=message, detail=f"最低市值 {min_market_cap:g} 亿元，最多扫描 {max_symbols} 只。")

        fetch_limit = max_symbols if set(selected_boards) == set(ASHARE_BOARD_LABELS) else max(1000, max_symbols * 4)
        universe, universe_source, market_cap_filter_applied = load_ashare_universe_for_scan(min_market_cap, fetch_limit, universe_progress)
        universe = filter_ashare_universe_by_board(universe, selected_boards)[: max(1, max_symbols)]
        filter_text = "市值过滤已生效" if market_cap_filter_applied else "当前数据源没有总市值字段，市值过滤未生效"
        filter_text = f"{filter_text}；行业信息会在结果表中展示"
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
            append_task_history(job_id, "cn", "stopped", scanned, len(candidates), len(errors), universe_source, params, "A 股扫描已终止，当前结果已保留")
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
        append_task_history(job_id, "cn", "done", scanned, len(candidates), len(errors), universe_source, params, "A 股扫描完成")
    except Exception as exc:
        if job_stop_requested(job_id):
            set_job(job_id, status="stopped", stage="已终止", message="A 股扫描已终止", detail="已按用户请求终止。", current="")
            append_task_history(job_id, "cn", "stopped", 0, 0, 0, "", params, "A 股扫描已终止")
            return
        set_job(job_id, status="error", stage="失败", message="A 股扫描失败", detail="请查看错误信息；通常是外部数据源返回异常或网络超时。", error=str(exc))
        append_task_history(job_id, "cn", "error", 0, 0, 1, "", params, str(exc))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        raw_path = parsed.path
        legacy_pages = {
            "/": "/app/",
            "/us": "/app/scan",
            "/us/": "/app/scan",
            "/us/scanner": "/app/scan",
            "/us/scan": "/app/scan",
            "/us/scan/latest": "/app/scan",
            "/us/watchlist": "/app/watchlist",
            "/us/backtest": "/app/backtest",
            "/us/batch": "/app/batch",
            "/cn": "/app/cn/scan",
            "/cn/": "/app/cn/scan",
            "/cn/scanner": "/app/cn/scan",
            "/cn/scan/latest": "/app/cn/scan",
            "/cn/watchlist": "/app/cn/watchlist",
            "/cn/backtest": "/app/cn/backtest",
            "/cn/batch": "/app/cn/batch",
            ASHARE_ROUTE: "/app/cn/scan",
        }
        if raw_path in legacy_pages:
            self.redirect(legacy_pages[raw_path])
            return
        if raw_path == "/app" or raw_path.startswith("/app/"):
            self.send_frontend_app(raw_path)
            return
        if raw_path.startswith("/api/"):
            self.handle_api(raw_path, params)
            return
        market = "us"
        route_path = raw_path
        if raw_path == "/cache/clear":
            market = "global"
            route_path = raw_path
        elif raw_path == "/us" or raw_path.startswith("/us/"):
            market = "us"
            route_path = raw_path[3:] or "/"
        elif raw_path == "/cn" or raw_path.startswith("/cn/"):
            market = "cn"
            route_path = raw_path[3:] or "/"
        try:
            if market == "global":
                self.clear_cache(params)
            elif market == "cn":
                if route_path == "/suggest":
                    self.send_json({"suggestions": suggest_ashare_symbols(field(params, "q", ""), int(number_field(params, "limit", 12)))})
                elif route_path == "/scanner/frame":
                    self.send_bytes(frame_shell(render_ashare_scanner(params), "scanner", "cn"))
                elif route_path == "/candidate/chart":
                    self.send_bytes(render_ashare_candidate_chart_frame(params))
                elif route_path == "/scan/start":
                    self.start_ashare_scan_job(params)
                elif route_path == "/scan/status":
                    self.ashare_scan_job_status(params)
                elif route_path == "/scan/active":
                    self.ashare_scan_job_active()
                elif route_path == "/scan/stop":
                    self.stop_ashare_scan_job(params)
                elif route_path == "/scan/delete":
                    self.delete_ashare_scan_result()
                elif route_path == "/watchlist/frame":
                    self.send_bytes(frame_shell(render_ashare_watchlist_page(params), "watchlist", "cn"))
                elif route_path == "/watchlist/add":
                    self.add_ashare_watchlist_item(params)
                elif route_path == "/watchlist/delete":
                    self.delete_ashare_watchlist_item(params)
                elif route_path == "/watchlist/chart":
                    self.ashare_watchlist_chart(params)
                elif route_path == "/backtest/frame":
                    self.send_bytes(frame_shell(render_ashare_backtest_form(params), "backtest", "cn"))
                elif route_path == "/run/frame":
                    content = run_ashare_strategy(params)
                    self.send_bytes(content.encode("utf-8") if field(params, "_report_only", "0") == "1" else frame_shell(content, "backtest", "cn"))
                else:
                    self.send_error(404)
            elif route_path == "/company-profiles/update":
                self.update_us_company_profiles(params)
            elif route_path == "/company-profiles/status":
                self.us_company_profiles_status(params)
            elif route_path == "/company-profiles/stop":
                self.stop_us_company_profiles_job(params)
            elif route_path == "/run/frame":
                content = run_strategy(params)
                self.send_bytes(content.encode("utf-8") if field(params, "_report_only", "0") == "1" else frame_shell(content, "backtest", "us"))
            elif route_path == "/batch/run/frame":
                self.send_bytes(frame_shell(run_batch_backtest(params), "batch", "us"))
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
            elif route_path == "/candidate/chart":
                self.send_bytes(render_candidate_chart_frame(params))
            elif route_path.startswith("/reports/"):
                self.send_report(route_path)
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_error(500, explain=str(exc))

    def handle_api(self, route_path: str, params: dict[str, list[str]]) -> None:
        try:
            if route_path == "/api/health":
                self.send_json({"ok": True, "status": "ok"})
            elif route_path == "/api/cache/summary":
                self.send_json({"ok": True, "areas": cache_dashboard_summary()})
            elif route_path == "/api/cache/clear":
                area = field(params, "area", "")
                if area not in {"reports", "prices", "us_market", "ashare", "latest"}:
                    self.send_json({"ok": False, "error": "未知缓存类型，未执行清理。"}, status=400)
                else:
                    message = clear_cache_area(area)
                    self.send_json({"ok": True, "message": message, "areas": cache_dashboard_summary()})
            elif route_path == "/api/us/scanner/bootstrap":
                self.send_json(scanner_bootstrap_api_payload(params))
            elif route_path == "/api/us/scan/latest":
                self.send_json(latest_scan_api_payload())
            elif route_path == "/api/us/scan/start":
                self.start_scan_job(params)
            elif route_path == "/api/us/scan/status":
                self.scan_job_status({"id": params.get("id", params.get("job_id", [""]))})
            elif route_path == "/api/us/scan/active":
                self.scan_job_active()
            elif route_path == "/api/us/scan/pause":
                self.pause_scan_job({"id": params.get("id", params.get("job_id", [""]))})
            elif route_path == "/api/us/scan/resume":
                self.resume_scan_job({"id": params.get("id", params.get("job_id", [""]))})
            elif route_path == "/api/us/scan/stop":
                self.stop_scan_job({"id": params.get("id", params.get("job_id", [""]))})
            elif route_path == "/api/us/watchlist":
                self.send_json({"ok": True, "items": watchlist_items_with_performance(load_watchlist_items(), "us")})
            elif route_path == "/api/us/watchlist/add":
                self.add_watchlist_item_json(params)
            elif route_path == "/api/us/watchlist/delete":
                symbol = field(params, "symbol", "")
                delete_watchlist_symbol(symbol)
                self.send_json({"ok": True, "items": load_watchlist_items()})
            elif route_path == "/api/us/watchlist/chart":
                self.send_json(watchlist_chart_payload(params))
            elif route_path == "/api/cn/scanner/bootstrap":
                self.send_json(ashare_scanner_bootstrap_api_payload())
            elif route_path == "/api/cn/scan/latest":
                self.send_json(ashare_latest_scan_api_payload())
            elif route_path == "/api/cn/scan/start":
                self.start_ashare_scan_job(params)
            elif route_path == "/api/cn/scan/status":
                self.ashare_scan_job_status({"job_id": params.get("job_id", params.get("id", [""]))})
            elif route_path == "/api/cn/scan/active":
                self.ashare_scan_job_active()
            elif route_path == "/api/cn/scan/stop":
                self.stop_ashare_scan_job({"job_id": params.get("job_id", params.get("id", [""]))})
            elif route_path == "/api/cn/watchlist":
                self.send_json({"ok": True, "items": watchlist_items_with_performance(load_ashare_watchlist_items(), "cn")})
            elif route_path == "/api/cn/watchlist/add":
                items = add_ashare_watchlist_symbol(
                    field(params, "symbol", ""),
                    field(params, "group", "观察"),
                    field(params, "note", ""),
                    field(params, "name", ""),
                    field(params, "sector", ""),
                )
                self.send_json({"ok": True, "items": items})
            elif route_path == "/api/cn/watchlist/delete":
                items = delete_ashare_watchlist_symbol(field(params, "symbol", ""))
                self.send_json({"ok": True, "items": items})
            else:
                self.send_json({"ok": False, "error": "API route not found"}, status=404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def send_frontend_app(self, request_path: str) -> None:
        if not FRONTEND_DIST_DIR.exists():
            body = (
                "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                "<title>MA5 App</title><body style=\"font-family:system-ui;padding:24px\">"
                "<h1>前端尚未构建</h1><p>请在 frontend 目录运行 pnpm install && pnpm build。</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        relative = request_path.removeprefix("/app").lstrip("/")
        target = FRONTEND_DIST_DIR / (relative or "index.html")
        target = target.resolve()
        dist_root = FRONTEND_DIST_DIR.resolve()
        try:
            target.relative_to(dist_root)
        except ValueError:
            target = dist_root / "index.html"
        if not target.exists() or target.is_dir():
            target = dist_root / "index.html"
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix.lower() == ".js":
            content_type = "application/javascript"
        elif target.suffix.lower() == ".css":
            content_type = "text/css"
        elif target.suffix.lower() == ".html":
            content_type = "text/html; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if target.suffix.lower() == ".html":
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        elif target.parent == dist_root / "assets":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def clear_cache(self, params: dict[str, list[str]]) -> None:
        area = field(params, "area", "")
        message = clear_cache_area(area)
        self.redirect(f"/?cache_message={quote(message)}")

    def update_us_company_profiles(self, params: dict[str, list[str]] | None = None) -> None:
        params = params or {}
        active = active_scan_job("us_profile")
        if active:
            job_id, _ = active
            message = f"美股公司信息后台更新已在运行：{job_id}。"
            self.redirect(f"/?cache_message={quote(message)}")
            return
        symbols = latest_scan_candidate_symbols()
        if not symbols:
            message = "当前没有可更新的美股选股结果，请先完成一次美股选股。"
            self.redirect(f"/?cache_message={quote(message)}")
            return
        missing = missing_us_company_profile_symbols(symbols)
        if not missing:
            message = f"当前选股结果的公司信息已在缓存中，无需更新（候选 {len(symbols)} 只）。"
            self.redirect(f"/?cache_message={quote(message)}")
            return
        job_id = start_us_profile_update_for_symbols(symbols, "manual current scan")
        message = f"已启动当前选股结果公司信息更新：{job_id}。本次只查询缺失信息的 {len(missing)} 只股票。"
        self.redirect(f"/?cache_message={quote(message)}")

    def us_company_profiles_status(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        if job_id:
            job = get_job(job_id)
            if not job:
                self.send_json({"status": "error", "error": "找不到美股公司信息更新任务"}, status=404)
                return
            self.send_json(normalize_job_payload(job_id, job))
            return
        latest_job = latest_job_for_market("us_profile", include_finished=True)
        if latest_job:
            job_id, job = latest_job
            self.send_json(normalize_job_payload(job_id, job))
            return
        self.send_json({"status": "idle", "job_id": ""})

    def stop_us_company_profiles_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        if not job_id:
            latest_job = latest_job_for_market("us_profile", include_finished=False)
            job_id = latest_job[0] if latest_job else ""
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到美股公司信息更新任务"}, status=404)
            return
        if job.get("status") in ("done", "error", "stopped"):
            self.send_json(normalize_job_payload(job_id, job))
            return
        set_job(job_id, stop_requested=True, status="stopping", stage="正在终止", message="正在终止公司信息更新，已写入的缓存会保留")
        self.send_json(normalize_job_payload(job_id, get_job(job_id) or {"status": "stopping"}))

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
        latest_job = latest_job_for_market("cn", include_finished=False)
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
        clear_jobs_for_market("cn")
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
            payload = ashare_chart_payload(
                symbol,
                j_threshold,
                b1_require_20ma_gt_50ma=checkbox_field(params, "b1_require_20ma_gt_50ma", False),
                require_ma5_rising=checkbox_field(params, "require_ma5_rising", False),
                require_5ma_gt_20ma=checkbox_field(params, "require_5ma_gt_20ma", False),
                preset=field(params, "preset", "1y").lower(),
            )
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
        cleared_jobs = clear_jobs_for_market("us")
        message = f"已删除当前扫描结果（清理 {deleted} 个文件）。" if deleted else "当前没有可删除的扫描结果。"
        if cleared_jobs:
            message += f" 已清理后台任务状态 {cleared_jobs} 个。"
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
        latest_job = latest_job_for_market("us", include_finished=False)
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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
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

