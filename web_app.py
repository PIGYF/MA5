from __future__ import annotations

import csv
import html
import json
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from backtest import Bar, backtest, fetch_bars, make_report, rolling_sma, summarize, write_equity, write_trades
from scan_next_b import SignalResult, latest_b_signal, load_symbols, unique_symbols, write_html


ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"
DEFAULT_BENCHMARK = "^IXIC"
SCAN_JOBS: dict[str, dict[str, object]] = {}
SCAN_JOBS_LOCK = threading.Lock()
NASDAQ_CACHE_PATH = ROOT / "nasdaq_screener_cache.json"
NASDAQ_CACHE_SECONDS = 60 * 60 * 12
DEFAULT_SCAN_LOOKBACK_DAYS = 70


def field(params: dict[str, list[str]], name: str, default: str) -> str:
    return params.get(name, [default])[0].strip()


def number_field(params: dict[str, list[str]], name: str, default: float) -> float:
    return float(field(params, name, str(default)))


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def set_job(job_id: str, **updates: object) -> None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.setdefault(job_id, {})
        job.update(updates)


def get_job(job_id: str) -> dict[str, object] | None:
    with SCAN_JOBS_LOCK:
        job = SCAN_JOBS.get(job_id)
        return dict(job) if job else None


def job_pause_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("pause_requested"))


def job_stop_requested(job_id: str) -> bool:
    job = get_job(job_id)
    return bool(job and job.get("stop_requested"))


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
    return date.today() - timedelta(days=1)


def default_scan_start_date(end: date) -> date:
    return end - timedelta(days=DEFAULT_SCAN_LOOKBACK_DAYS)


def build_benchmark(symbol: str, start: str, end: str, initial_cash: float) -> dict[str, object]:
    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
    first_close = bars[0].close
    return {
        "symbol": symbol,
        "return_pct": (bars[-1].close / first_close - 1) * 100,
        "curve": [(bar.date, initial_cash * (bar.close / first_close)) for bar in bars],
    }


def page_shell(content: str, active: str = "backtest") -> bytes:
    backtest_active = " active" if active == "backtest" else ""
    scanner_active = " active" if active == "scanner" else ""
    text = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>本地交易工具</title>
<style>
body {{ margin: 0; background: #f4f6f8; color: #1f2933; font-family: Arial, "Microsoft YaHei", sans-serif; }}
main {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
.tabs {{ display: flex; gap: 8px; margin-bottom: 16px; }}
.tabs a {{ padding: 9px 12px; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; color: #334155; text-decoration: none; font-size: 14px; }}
.tabs a.active {{ background: #2563eb; color: #fff; border-color: #2563eb; }}
h1 {{ margin: 0 0 10px; font-size: 24px; }}
.hint {{ color: #607080; font-size: 13px; margin: 0 0 14px; }}
.form {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 10px; align-items: end; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 14px; margin-bottom: 16px; }}
label {{ display: block; font-size: 12px; color: #607080; }}
input, select, textarea {{ width: 100%; box-sizing: border-box; margin-top: 6px; padding: 9px 10px; border: 1px solid #cbd5df; border-radius: 6px; background: #fff; color: #1f2933; font-family: inherit; }}
textarea {{ min-height: 84px; resize: vertical; }}
button {{ padding: 10px 14px; border: 0; border-radius: 6px; background: #2563eb; color: #fff; font-weight: 700; cursor: pointer; }}
button.secondary {{ background: #64748b; }}
button.success {{ background: #16a34a; }}
button.danger {{ background: #dc2626; }}
.symbol-button {{ border: 0; background: transparent; color: #2563eb; padding: 0; font: inherit; font-weight: 700; cursor: pointer; }}
.symbol-button:hover {{ text-decoration: underline; }}
.wide {{ grid-column: span 3; }}
.error {{ background: #fff1f2; border: 1px solid #fecdd3; color: #9f1239; padding: 12px; border-radius: 8px; white-space: pre-wrap; }}
.result {{ background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 12px; margin-top: 16px; }}
.candidate-detail {{ margin-top: 16px; }}
.candidate-detail iframe {{ height: 980px; }}
.progress-box {{ display: none; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 12px; margin-top: 16px; }}
.progress-track {{ height: 10px; background: #e5eaf0; border-radius: 999px; overflow: hidden; margin: 8px 0; }}
.progress-bar {{ height: 100%; width: 0%; background: #2563eb; transition: width .2s ease; }}
.progress-meta {{ color: #475569; font-size: 13px; }}
.progress-actions {{ display: flex; gap: 8px; margin-top: 10px; }}
.progress-actions button[hidden] {{ display: none; }}
.links {{ margin: 0 0 12px; font-size: 14px; }}
.links a {{ color: #2563eb; text-decoration: none; margin-right: 12px; }}
iframe {{ width: 100%; height: 1320px; border: 1px solid #dde3ea; border-radius: 8px; background: #fff; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; overflow: hidden; }}
.table-wrap {{ width: 100%; overflow-x: auto; border: 1px solid #dde3ea; border-radius: 8px; background: #fff; }}
.table-wrap table {{ width: max-content; min-width: 100%; border: 0; border-radius: 0; table-layout: auto; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: right; font-size: 13px; white-space: nowrap; }}
th {{ background: #f8fafc; color: #475569; }}
th.resizable {{ position: relative; user-select: none; }}
.col-resizer {{ position: absolute; top: 0; right: -3px; width: 6px; height: 100%; cursor: col-resize; z-index: 2; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4), th:nth-child(5), td:nth-child(5), th:nth-child(6), td:nth-child(6), th:nth-child(7), td:nth-child(7) {{ text-align: left; }}
.empty {{ text-align: center; color: #607080; }}
@media (max-width: 980px) {{ .form {{ grid-template-columns: repeat(2, 1fr); }} .wide {{ grid-column: span 2; }} }}
</style>
</head>
<body><main>
<nav class="tabs">
  <a class="{backtest_active}" href="/">回测</a>
  <a class="{scanner_active}" href="/scanner">选股器</a>
</nav>
{content}
</main></body>
</html>"""
    return text.encode("utf-8")


def render_backtest_form(params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    today = date.today()
    preset = field(params, "preset", "1y")
    strategy = field(params, "strategy_name", "ratchet")
    start_default = start_for_preset(preset, today).isoformat()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""

    return f"""
<h1>本地 Strategy Tester</h1>
<p class="hint">输入股票代码，选择回测周期，然后点击运行。数据会从 yfinance 拉取最新可用日线，默认对比纳斯达克综合指数 ^IXIC。</p>
<form class="form" action="/run" method="get">
  <label>股票代码<input name="symbol" value="{value("symbol", "AAPL").upper()}" placeholder="AAPL"></label>
  <label>策略版本
    <select name="strategy_name">
      <option value="ratchet"{selected(strategy, "ratchet")}>棘轮趋势版</option>
      <option value="classic"{selected(strategy, "classic")}>原始版本</option>
    </select>
  </label>
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
  <label>均量周期<input name="vol_length" value="{value("vol_length", "20")}"></label>
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
</script>
"""


def run_strategy(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(exist_ok=True)
    symbol = field(params, "symbol", "AAPL").upper()
    strategy_name = field(params, "strategy_name", "ratchet")
    start = field(params, "start", start_for_preset("1y", date.today()).isoformat())
    end = field(params, "end", date.today().isoformat())
    benchmark_symbol = field(params, "benchmark", DEFAULT_BENCHMARK).upper()
    initial_cash = number_field(params, "initial_cash", 100000)

    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
    trades, equity_curve = backtest(
        bars=bars,
        ma_length=int(number_field(params, "ma_length", 5)),
        vol_length=int(number_field(params, "vol_length", 20)),
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
    <a href="{report_url}" target="_blank">打开完整报告</a>
    <a href="{trades_url}" target="_blank">交易明细 CSV</a>
    <a href="{equity_url}" target="_blank">权益曲线 CSV</a>
  </p>
  <iframe src="{report_url}" title="Backtest report"></iframe>
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


def format_metric(value: float, suffix: str = "%") -> str:
    if value == 999.0:
        return "N/A"
    return f"{value:.1f}{suffix}"


def space_score(label: str) -> int:
    return {
        "52周新高": 5,
        "接近新高": 4,
        "空间尚可": 3,
        "上方有前高": 2,
        "200日线压制": 1,
    }.get(label, 3)


def candle_score(label: str) -> int:
    return {
        "强阳": 5,
        "一般阳线": 3,
        "冲高回落": 2,
        "阴线": 1,
    }.get(label, 3)


def sector_score(label: str) -> int:
    return {
        "行业共振": 5,
        "板块共振": 4,
        "一般": 3,
        "孤立": 2,
    }.get(label, 1)


def update_total_score(row: SignalResult) -> None:
    row.second_stage_score_total = (
        int(row.catalyst_score or 0)
        + int(row.sector_score or 0)
        + int(row.space_score or 0)
        + int(row.candle_score or 0)
    )
    if row.second_stage_score_total >= 17:
        row.second_stage_rating = "强"
    elif row.second_stage_score_total >= 13:
        row.second_stage_rating = "中"
    else:
        row.second_stage_rating = "弱"


def add_space_and_candle_quality(result: SignalResult, bars: list[Bar]) -> SignalResult:
    if not bars:
        result.second_stage_rating = "待确认"
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
        result.above_200ma = "是" if close > ma200 else "否"
        result.distance_200ma_pct = (close / ma200 - 1) * 100
    else:
        result.above_200ma = "数据不足"
        result.distance_200ma_pct = 0.0

    prior_resistances = [
        bar.high for bar in bars[:-1]
        if close < bar.high <= close * 1.10
    ]
    nearest = min(prior_resistances) if prior_resistances else None
    result.nearest_resistance_pct = ((nearest / close - 1) * 100) if nearest else 999.0

    near_52w = result.distance_52w_high_pct >= -5
    if close >= high_52w * 0.995:
        result.space_label = "52周新高"
    elif ma200 and close < ma200:
        result.space_label = "200日线压制"
    elif nearest and result.nearest_resistance_pct <= 10:
        result.space_label = "上方有前高"
    elif near_52w:
        result.space_label = "接近新高"
    else:
        result.space_label = "空间尚可"

    body = abs(signal_bar.close - signal_bar.open)
    upper_shadow = signal_bar.high - max(signal_bar.open, signal_bar.close)
    full_range = signal_bar.high - signal_bar.low
    result.day_change_pct = (signal_bar.close / signal_bar.open - 1) * 100 if signal_bar.open else 0.0
    result.close_position_pct = ((signal_bar.close - signal_bar.low) / full_range * 100) if full_range else 50.0
    result.upper_shadow_body_ratio = upper_shadow / body if body > 0 else 999.0

    if signal_bar.close <= signal_bar.open:
        result.candle_label = "阴线"
    elif result.close_position_pct >= 80 and result.upper_shadow_body_ratio <= 0.5:
        result.candle_label = "强阳"
    elif result.upper_shadow_body_ratio > 0.5:
        result.candle_label = "冲高回落"
    else:
        result.candle_label = "一般阳线"

    result.catalyst_label = "待人工确认"
    result.catalyst_score = 3
    result.space_score = space_score(result.space_label)
    result.candle_score = candle_score(result.candle_label)
    result.catalyst_yahoo_url = yahoo_news_url(result.symbol)
    result.catalyst_google_url = google_news_url(result.symbol, result.company_name)
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
            row.sector_label = "行业共振"
        elif row.sector_peer_count >= 3:
            row.sector_label = "板块共振"
        elif row.sector_peer_count >= 2:
            row.sector_label = "一般"
        else:
            row.sector_label = "孤立"

        row.catalyst_score = row.catalyst_score or 3
        row.sector_score = sector_score(row.sector_label)
        row.space_score = row.space_score or space_score(row.space_label)
        row.candle_score = row.candle_score or candle_score(row.candle_label)
        update_total_score(row)


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
            result.second_stage_rating = "待确认"
            result.catalyst_label = "待人工确认"
            result.catalyst_score = 3
            result.space_score = result.space_score or 3
            result.candle_score = result.candle_score or 3
            update_total_score(result)
            result.catalyst_yahoo_url = yahoo_news_url(symbol)
            result.catalyst_google_url = google_news_url(symbol, result.company_name)
        return symbol, result, None
    except Exception as exc:
        return symbol, None, str(exc)


def fetch_nasdaq_screener_rows() -> list[dict[str, object]]:
    if NASDAQ_CACHE_PATH.exists():
        age = time.time() - NASDAQ_CACHE_PATH.stat().st_mtime
        if age < NASDAQ_CACHE_SECONDS:
            return json.loads(NASDAQ_CACHE_PATH.read_text(encoding="utf-8"))

    ps = r"""
$headers=@{
  'User-Agent'='Mozilla/5.0';
  'Accept'='application/json, text/plain, */*';
  'Origin'='https://www.nasdaq.com';
  'Referer'='https://www.nasdaq.com/market-activity/stocks/screener'
}
$url='https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true'
$r=Invoke-WebRequest -Uri $url -Headers $headers -UseBasicParsing -TimeoutSec 60
$r.Content
"""
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=90,
        check=True,
    )
    payload = json.loads(completed.stdout)
    rows = payload.get("data", {}).get("rows", [])
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
    today = date.today()
    scan_end = default_scan_end_date()

    def value(name: str, default: str) -> str:
        return html.escape(field(params, name, default))

    source = field(params, "universe_source", "auto")
    def selected(current: str, expected: str) -> str:
        return " selected" if current == expected else ""
    asset_type = field(params, "asset_type", "stocks")

    default_symbols = "ASTS,NVDA,TSLA,AAPL,MSFT,QQQ"
    return f"""
<h1>下一交易日 B 点选股器</h1>
<p class="hint">扫描“最后一根已完成日 K 出现 B 信号”的股票。它们不是今天追买，而是按策略在下一交易日开盘才有买入资格。</p>
<form class="form" id="scanner-form" action="/scan" method="get">
  <label>股票池来源
    <select name="universe_source">
      <option value="auto"{selected(source, "auto")}>按市值自动筛选美股</option>
      <option value="manual"{selected(source, "manual")}>手动输入股票池</option>
    </select>
  </label>
  <label>最低市值，亿美元<input name="min_market_cap_billion" value="{value("min_market_cap_billion", "20")}"></label>
  <label>最高市值，亿美元，0为不限<input name="max_market_cap_billion" value="{value("max_market_cap_billion", "0")}"></label>
  <label>最低当日成交量<input name="min_screener_volume" value="{value("min_screener_volume", "500000")}"></label>
  <label>最多扫描数量<input name="max_symbols" value="{value("max_symbols", "250")}"></label>
  <label>并发数<input name="max_workers" value="{value("max_workers", "6")}"></label>
  <label>资产类型
    <select name="asset_type">
      <option value="stocks"{selected(asset_type, "stocks")}>只扫 Stocks</option>
      <option value="etf"{selected(asset_type, "etf")}>只扫 ETF</option>
      <option value="all"{selected(asset_type, "all")}>Stocks + ETF</option>
    </select>
  </label>
  <label class="wide">股票池，逗号或换行分隔
    <textarea name="symbols" placeholder="ASTS,NVDA,TSLA">{value("symbols", default_symbols)}</textarea>
  </label>
  <label>开始日期<input type="date" name="start" value="{value("start", default_scan_start_date(scan_end).isoformat())}"></label>
  <label>结束日期<input type="date" name="end" value="{value("end", scan_end.isoformat())}"></label>
  <label>最低价格<input name="min_price" value="{value("min_price", "5")}"></label>
  <label>20日最低成交额<input name="min_avg_dollar_volume" value="{value("min_avg_dollar_volume", "20000000")}"></label>
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

async function pollScan(jobId) {{
  while (true) {{
    const res = await fetch(`/scan/status?id=${{encodeURIComponent(jobId)}}`);
    const job = await res.json();
    updateProgress(job);
    if (job.result_html && job.result_html !== lastResultHtml) {{
      lastResultHtml = job.result_html;
      scanResult.innerHTML = job.result_html;
      initializeResizableTables(scanResult);
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

scanResult.addEventListener("click", async event => {{
  const button = event.target.closest("[data-candidate-symbol]");
  if (!button) return;
  const symbol = button.dataset.candidateSymbol;
  let detail = document.getElementById("candidate-detail");
  if (!detail) {{
    detail = document.createElement("section");
    detail.id = "candidate-detail";
    detail.className = "candidate-detail";
    scanResult.appendChild(detail);
  }}
  detail.innerHTML = `<section class="result"><p class="hint">正在生成 ${{symbol}} 的日 K 线和策略交易点...</p></section>`;
  const params = new URLSearchParams(new FormData(scannerForm));
  params.set("symbol", symbol);
  const res = await fetch(`/candidate?${{params.toString()}}`);
  const html = await res.text();
  detail.innerHTML = html;
  detail.scrollIntoView({{ behavior: "smooth", block: "start" }});
}});

scannerForm.addEventListener("submit", async event => {{
  event.preventDefault();
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
  const params = new URLSearchParams(new FormData(scannerForm));
  const res = await fetch(`/scan/start?${{params.toString()}}`);
  const data = await res.json();
  if (!data.job_id) {{
    scanResult.innerHTML = `<div class="error">${{data.error || "无法启动扫描"}}</div>`;
    return;
  }}
  activeScanJobId = data.job_id;
  pollScan(data.job_id);
}});
</script>
"""


def render_candidate_table(rows: list[SignalResult]) -> str:
    table_rows = "\n".join(
        f'<tr><td><button type="button" class="symbol-button" data-candidate-symbol="{html.escape(r.symbol)}">{html.escape(r.symbol)}</button></td><td>{html.escape(r.company_name or "-")}</td>'
        f"<td>{r.market_cap / 1_000_000_000:.2f}</td>"
        f"<td>{r.second_stage_score_total}/20</td>"
        f"<td>{html.escape(r.second_stage_rating or '待确认')}</td>"
        f'<td>{r.catalyst_score}/5 {html.escape(r.catalyst_label or "待人工确认")} <a href="{html.escape(r.catalyst_yahoo_url or yahoo_news_url(r.symbol))}" target="_blank">Yahoo</a> <a href="{html.escape(r.catalyst_google_url or google_news_url(r.symbol, r.company_name))}" target="_blank">Google</a></td>'
        f"<td>{r.sector_score}/5 {html.escape(r.sector_label or '-')} ({r.sector_peer_count}/{r.industry_peer_count})</td>"
        f"<td>{r.space_score}/5 {html.escape(r.space_label or '-')} / 52W {r.distance_52w_high_pct:.1f}% / 200MA {html.escape(r.above_200ma or '-')}</td>"
        f"<td>{r.candle_score}/5 {html.escape(r.candle_label or '-')} / 收盘位 {r.close_position_pct:.0f}% / 上影 {format_metric(r.upper_shadow_body_ratio, 'x')}</td>"
        f"<td>{html.escape(r.sector or '-')}</td><td>{html.escape(r.industry or '-')}</td>"
        f"<td>{html.escape(r.signal_date)}</td><td>{html.escape(r.signal_type)}</td>"
        f"<td>{r.close:.2f}</td><td>{r.ma:.2f}</td><td>{r.dist_to_ma_pct:.2f}%</td>"
        f"<td>{r.volume_ratio:.2f}x</td><td>{r.massive_count_7d}</td><td>{r.avg_dollar_volume_20d / 1_000_000:.1f}M</td></tr>"
        for r in rows
    )
    if not table_rows:
        table_rows = '<tr><td colspan="19" class="empty">没有筛到候选股。</td></tr>'
    return f"""
<div class="table-wrap">
<table class="resizable-table">
  <thead><tr><th>Symbol</th><th>Company</th><th>Mkt Cap $B</th><th>总分</th><th>评级</th><th>Catalyst</th><th>Sector</th><th>Space</th><th>Candle</th><th>Sector</th><th>Industry</th><th>Signal Date</th><th>Signal</th><th>Close</th><th>MA</th><th>Dist</th><th>Vol Ratio</th><th>Massive 7D</th><th>20D $Vol</th></tr></thead>
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
<h2>失败理由</h2>
<div class="table-wrap">
<table class="resizable-table">
  <thead><tr><th>Symbol</th><th>Reason</th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>
</div>
"""


def run_scanner(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(exist_ok=True)
    source = field(params, "universe_source", "auto")
    symbols_text = field(params, "symbols", "")
    scan_end = default_scan_end_date()
    start = field(params, "start", default_scan_start_date(scan_end).isoformat())
    end = field(params, "end", scan_end.isoformat())
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
            number_field(params, "min_market_cap", 2_000_000_000) / 100_000_000,
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
            max_symbols=int(number_field(params, "max_symbols", 250)),
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
                    result.second_stage_rating = "待确认"
                    result.catalyst_label = "待人工确认"
                    result.catalyst_yahoo_url = yahoo_news_url(symbol)
                    result.catalyst_google_url = google_news_url(symbol, result.company_name)
                rows.append(result)
        except Exception as exc:
            errors.append((symbol, str(exc)))

    add_sector_and_rating(rows)
    rows.sort(key=lambda row: (row.second_stage_score_total, row.avg_dollar_volume_20d), reverse=True)
    stem = safe_name(f"next_b_{end}_{len(symbols)}")
    csv_path = REPORT_DIR / f"{stem}.csv"
    html_path = REPORT_DIR / f"{stem}.html"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SignalResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    write_html(html_path, rows, end)

    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {message[:80]}" for symbol, message in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 个代码扫描失败：{html.escape(sample)}</p>'

    return f"""
{render_scanner_form(params)}
<section class="result">
  <p class="links">
    <a href="/reports/{quote(html_path.name)}" target="_blank">打开选股报告</a>
    <a href="/reports/{quote(csv_path.name)}" target="_blank">下载候选 CSV</a>
  </p>
  <p class="hint">股票池来源：{html.escape(source)}。已扫描 {len(symbols)} 个代码，筛出 {len(rows)} 个“下一交易日 B 点候选”。</p>
  {error_note}
  {render_candidate_table(rows)}
  {render_failure_table(errors)}
</section>
"""


def render_candidate_detail(params: dict[str, list[str]]) -> str:
    REPORT_DIR.mkdir(exist_ok=True)
    symbol = field(params, "symbol", "").upper()
    if not symbol:
        raise ValueError("Missing symbol")

    scan_end = default_scan_end_date()
    start = field(params, "start", default_scan_start_date(scan_end).isoformat())
    end = field(params, "end", scan_end.isoformat())
    bars = fetch_bars("yfinance", symbol, start, end, "qfq", None)
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
  <p class="hint">下方图表使用当前选股器参数重新回测该候选股，买入/卖出按信号后一个交易日开盘成交。</p>
  <iframe src="{report_url}" title="{html.escape(symbol)} candidate detail"></iframe>
</section>
"""


def resolve_scan_symbols(params: dict[str, list[str]]) -> tuple[str, list[str], dict[str, dict[str, object]]]:
    source = field(params, "universe_source", "auto")
    symbols_text = field(params, "symbols", "")
    min_price = number_field(params, "min_price", 5)
    if source == "auto":
        min_market_cap = number_field(
            params,
            "min_market_cap_billion",
            number_field(params, "min_market_cap", 2_000_000_000) / 100_000_000,
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
            max_symbols=int(number_field(params, "max_symbols", 250)),
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
    stem = safe_name(f"next_b_{end}_{len(symbols)}_{int(time.time())}")
    csv_path = REPORT_DIR / f"{stem}.csv"
    html_path = REPORT_DIR / f"{stem}.html"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(SignalResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    write_html(html_path, rows, end)

    error_note = ""
    if errors:
        sample = "; ".join(f"{symbol}: {message[:80]}" for symbol, message in errors[:5])
        error_note = f'<p class="hint">有 {len(errors)} 个代码扫描失败：{html.escape(sample)}</p>'

    return f"""
<section class="result">
  <p class="links">
    <a href="/reports/{quote(html_path.name)}" target="_blank">打开选股报告</a>
    <a href="/reports/{quote(csv_path.name)}" target="_blank">下载候选 CSV</a>
  </p>
  <p class="hint">股票池来源：{html.escape(source)}。已扫描 {len(symbols)} 个代码，筛出 {len(rows)} 个“下一交易日 B 点候选”。</p>
  {error_note}
  {render_candidate_table(rows)}
  {render_failure_table(errors)}
</section>
"""


def execute_scan_job(job_id: str, params: dict[str, list[str]]) -> None:
    try:
        REPORT_DIR.mkdir(exist_ok=True)
        set_job(job_id, status="running", message="正在准备股票池", total=0, scanned=0, candidates=0, errors=0, current="")
        source, symbols, metadata_by_symbol = resolve_scan_symbols(params)
        scan_end = default_scan_end_date()
        start = field(params, "start", default_scan_start_date(scan_end).isoformat())
        end = field(params, "end", scan_end.isoformat())
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.send_bytes(page_shell(render_backtest_form(params), "backtest"))
            elif parsed.path == "/run":
                self.send_bytes(page_shell(run_strategy(params), "backtest"))
            elif parsed.path == "/scanner":
                self.send_bytes(page_shell(render_scanner_form(params), "scanner"))
            elif parsed.path == "/scan":
                self.send_bytes(page_shell(run_scanner(params), "scanner"))
            elif parsed.path == "/scan/start":
                self.start_scan_job(params)
            elif parsed.path == "/scan/pause":
                self.pause_scan_job(params)
            elif parsed.path == "/scan/resume":
                self.resume_scan_job(params)
            elif parsed.path == "/scan/stop":
                self.stop_scan_job(params)
            elif parsed.path == "/scan/status":
                self.scan_job_status(params)
            elif parsed.path == "/candidate":
                self.send_bytes(render_candidate_detail(params).encode("utf-8"))
            elif parsed.path.startswith("/reports/"):
                self.send_report(parsed.path)
            else:
                self.send_error(404)
        except Exception as exc:
            active = "scanner" if parsed.path in ("/scanner", "/scan") else "backtest"
            form = render_scanner_form(params) if active == "scanner" else render_backtest_form(params)
            self.send_bytes(page_shell(form + f'<div class="error">{html.escape(str(exc))}</div>', active), 500)

    def start_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = uuid.uuid4().hex
        set_job(job_id, status="queued", message="排队中", total=0, scanned=0, candidates=0, errors=0, current="", pause_requested=False, stop_requested=False)
        worker = threading.Thread(target=execute_scan_job, args=(job_id, params), daemon=True)
        worker.start()
        self.send_json({"job_id": job_id})

    def pause_scan_job(self, params: dict[str, list[str]]) -> None:
        job_id = field(params, "id", "")
        job = get_job(job_id)
        if not job:
            self.send_json({"status": "error", "error": "找不到扫描任务"}, status=404)
            return
        if job.get("status") in ("done", "error"):
            self.send_json(job)
            return
        set_job(job_id, pause_requested=True, status="pausing", message="正在暂停，当前股票处理完后会显示当前结果")
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
        set_job(job_id, stop_requested=True, pause_requested=False, status="stopping", message="正在终止，当前股票处理完后会保留当前结果")
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
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Open http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
