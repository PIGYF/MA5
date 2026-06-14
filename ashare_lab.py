from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen


ASHARE_ROUTE = "/ashare"
ROOT = Path(__file__).resolve().parent
ASHARE_CACHE_DIR = Path(os.environ.get("MA5_DATA_DIR", ROOT / "data")).expanduser().resolve() / "ashare"
ASHARE_SECTOR_CACHE_PATH = ASHARE_CACHE_DIR / "sector_map.json"
ASHARE_SECTOR_CACHE_SECONDS = 7 * 24 * 60 * 60
ASHARE_UNIVERSE_CACHE_PATH = ASHARE_CACHE_DIR / "universe_cache.json"
ASHARE_UNIVERSE_CACHE_SECONDS = 18 * 60 * 60


@dataclass
class AShareBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0


@dataclass
class AShareSignalSnapshot:
    symbol: str
    name: str
    sector: str
    data_source: str
    latest_date: str
    close: float
    zx_short_trend: float
    zx_multi_trend: float
    zx_multi_slope: float
    j_value: float
    trend_ok: bool
    j_oversold: bool
    volume_structure_ok: bool
    signal: bool
    volume_score: float
    candidate_rating: str
    base_volume: float
    recent_peak_volume: float
    recent_peak_to_base: float
    recent_avg10_to_base: float
    red_days: int
    green_days: int
    red_avg_to_green_avg: float
    top5_red_count: int
    bars_count: int
    signal_type: str = ""
    ma5: float = 0.0
    ma20: float = 0.0
    volume_ratio: float = 0.0
    avg_amount_20d: float = 0.0
    amount_ok: bool = False
    limit_state: str = ""
    volume_context: str = ""
    execution_note: str = ""


@dataclass
class AShareUniverseItem:
    symbol: str
    name: str
    sector: str
    market_cap_100m: float
    exchange: str
    turnover: float = 0.0


@dataclass
class AShareScanResult:
    total: int
    scanned: int
    candidates: list[AShareSignalSnapshot]
    errors: list[tuple[str, str]]
    universe_source: str
    market_cap_filter_applied: bool


def ashare_indicator_series(bars: list[AShareBar], j_threshold: float = 14.0) -> list[dict[str, object]]:
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]

    ma14 = sma(closes, 14)
    ma28 = sma(closes, 28)
    ma57 = sma(closes, 57)
    ma114 = sma(closes, 114)
    ema1 = ema(closes, 10)
    zx_short = ema(ema1, 10)
    zx_multi: list[float | None] = [
        None if any(value is None for value in group) else sum(value for value in group if value is not None) / 4
        for group in zip(ma14, ma28, ma57, ma114)
    ]

    rsv: list[float] = []
    for i, close in enumerate(closes):
        if i < 8:
            rsv.append(50.0)
            continue
        high = max(highs[i - 8 : i + 1])
        low = min(lows[i - 8 : i + 1])
        rsv.append(50.0 if high == low else 100 * (close - low) / (high - low))
    k = sma(rsv, 3)
    k_clean = [50.0 if value is None else value for value in k]
    d = sma(k_clean, 3)
    j = [None if k[i] is None or d[i] is None else 3 * k[i] - 2 * d[i] for i in range(len(closes))]

    points: list[dict[str, object]] = []
    for i, bar in enumerate(bars):
        trend_ok = bool(
            i > 0
            and zx_multi[i] is not None
            and zx_multi[i - 1] is not None
            and zx_short[i] > float(zx_multi[i])
            and float(zx_multi[i]) > float(zx_multi[i - 1])
        )
        j_oversold = bool(j[i] is not None and j[i] < j_threshold)
        points.append(
            {
                "time": bar.date,
                "zx_short_trend": zx_short[i],
                "zx_multi_trend": zx_multi[i],
                "k": k[i],
                "d": d[i],
                "j": j[i],
                "signal": trend_ok and j_oversold,
            }
        )
    return points


def ashare_chart_payload(
    symbol: str,
    j_threshold: float = 14.0,
    b1_require_20ma_gt_50ma: bool = True,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> dict[str, object]:
    clean = normalize_ashare_symbol(symbol)
    bars, source = fetch_ashare_bars(clean)
    from backtest import adjust_limit_volumes, build_ratchet_inputs, calculate_kdj, rolling_sma

    bt_bars = ashare_to_backtest_bars(bars)
    closes = [bar.close for bar in bt_bars]
    limit_pct = ashare_limit_pct(clean)
    adjusted_volumes = adjust_limit_volumes(bt_bars, limit_pct)
    volumes = adjusted_volumes
    amounts = [bar.amount if bar.amount > 0 else bar.close * bar.volume for bar in bars]
    ma5 = rolling_sma(closes, 5)
    ma20 = rolling_sma(closes, 20)
    amount20 = rolling_sma(amounts, 20)
    volume_ma20 = rolling_sma(volumes, 20)
    k_values, d_values, j_values = calculate_kdj(bt_bars)
    buy_signal, _, _, _, buy_target_pct, buy_stage = build_ratchet_inputs(
        bt_bars,
        ma_length=5,
        vol_length=20,
        vol_multiplier=1.45,
        reentry_pct=0.045,
        vol_high_days=2,
        vol_high_multiplier=1.0,
        massive_window=7,
        massive_min_count=1,
        massive_max_count=2,
        b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
        require_ma5_rising=require_ma5_rising,
        require_5ma_gt_20ma=require_5ma_gt_20ma,
        signal_volumes=adjusted_volumes,
    )
    name, sector = fetch_ashare_profile(clean)
    volume_ratio = [
        None if not volume_ma20[i] else adjusted_volumes[i] / float(volume_ma20[i])
        for i in range(len(bars))
    ]
    return {
        "symbol": clean,
        "name": name,
        "sector": sector,
        "source": source,
        "strategy": "A股 MA5/B点",
        "ohlc": [{"x": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": [
            {
                "x": bar.date,
                "y": bar.volume,
                "color": "#089981" if i > 0 and bar.close >= bars[i - 1].close else "#f23645",
            }
            for i, bar in enumerate(bars)
        ],
        "ma5": [{"x": bars[i].date, "y": value} for i, value in enumerate(ma5) if value is not None],
        "ma20": [{"x": bars[i].date, "y": value} for i, value in enumerate(ma20) if value is not None],
        "amount20": [{"x": bars[i].date, "y": value / 100_000_000} for i, value in enumerate(amount20) if value is not None],
        "volume_ma20": [{"x": bars[i].date, "y": value} for i, value in enumerate(volume_ma20) if value is not None],
        "volume_ratio": [{"x": bars[i].date, "y": value} for i, value in enumerate(volume_ratio) if value is not None],
        "k": [{"x": bars[i].date, "y": value} for i, value in enumerate(k_values) if value is not None],
        "d": [{"x": bars[i].date, "y": value} for i, value in enumerate(d_values) if value is not None],
        "j": [{"x": bars[i].date, "y": value} for i, value in enumerate(j_values) if value is not None],
        "zx_short_trend": [{"x": bars[i].date, "y": value} for i, value in enumerate(ma5) if value is not None],
        "zx_multi_trend": [{"x": bars[i].date, "y": value} for i, value in enumerate(ma20) if value is not None],
        "signals": [
            {"x": bars[i].date, "y": bars[i].low, "text": buy_stage[i] or "B", "target_pct": buy_target_pct[i]}
            for i, signal in enumerate(buy_signal)
            if signal
        ],
    }

def normalize_ashare_symbol(symbol: str) -> str:
    clean = "".join(ch for ch in symbol.strip().upper() if ch.isalnum())
    if clean.startswith(("SH", "SS", "SZ", "BJ")):
        clean = clean[2:]
    if clean.endswith(("SH", "SS", "SZ", "BJ")):
        clean = clean[:6]
    if len(clean) != 6 or not clean.isdigit():
        raise ValueError("请输入 6 位 A 股代码，例如 600487。")
    return clean


def yahoo_suffix(symbol: str) -> str:
    if symbol.startswith(("4", "8", "9")):
        return ".BJ"
    if symbol.startswith(("6", "9")):
        return ".SS"
    if symbol.startswith(("0", "2", "3")):
        return ".SZ"
    if symbol.startswith(("4", "8")):
        return ".BJ"
    return ".SS"


def sma(values: list[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        result.append(total / length if i >= length - 1 else None)
    return result


def ema(values: list[float], length: int) -> list[float]:
    result: list[float] = []
    alpha = 2 / (length + 1)
    prev: float | None = None
    for value in values:
        prev = value if prev is None else alpha * value + (1 - alpha) * prev
        result.append(prev)
    return result


def row_value(row: Any, names: tuple[str, ...], fallback_index: int) -> Any:
    for name in names:
        try:
            value = row[name]
            if value is not None:
                return value
        except Exception:
            pass
    return row.iloc[fallback_index]


TDX_SERVERS = [
    ("180.153.18.170", 7709),
    ("119.147.212.81", 7709),
    ("47.103.48.45", 7709),
    ("114.80.63.12", 7709),
    ("218.108.98.244", 7709),
    ("14.17.75.71", 7709),
]


def tdx_market(symbol: str) -> int:
    clean = normalize_ashare_symbol(symbol)
    if clean.startswith(("6", "9", "688")):
        return 1
    return 0


def fetch_tdx_ashare_bars(symbol: str, start_day: date, end_day: date) -> tuple[list[AShareBar], str]:
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams

    clean = normalize_ashare_symbol(symbol)
    market = tdx_market(clean)
    last_error: Exception | None = None
    # Daily bars are enough for this strategy. Count includes weekends gaps implicitly absent from returned bars.
    count = max(300, min(800, (end_day - start_day).days + 80))
    for host, port in TDX_SERVERS:
        api = TdxHq_API(raise_exception=True, auto_retry=False)
        try:
            api.connect(host, port, time_out=2)
            rows = api.get_security_bars(TDXParams.KLINE_TYPE_DAILY, market, clean, 0, count)
            bars: list[AShareBar] = []
            for row in rows or []:
                bar_date = date(int(row["year"]), int(row["month"]), int(row["day"]))
                if bar_date < start_day or bar_date > end_day:
                    continue
                bars.append(
                    AShareBar(
                        date=bar_date.isoformat(),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("vol", 0.0) or 0.0),
                        amount=float(row.get("amount", 0.0) or 0.0),
                    )
                )
            if bars:
                bars.sort(key=lambda bar: bar.date)
                return bars, f"tdx {host}:{port}"
        except Exception as exc:
            last_error = exc
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError(str(last_error) if last_error else "通达信日线接口不可用")


def tencent_symbol(symbol: str) -> str:
    clean = normalize_ashare_symbol(symbol)
    prefix = "sh" if clean.startswith(("6", "9", "688")) else "sz"
    return f"{prefix}{clean}"


def fetch_tencent_market_caps(symbols: list[str], chunk_size: int = 80) -> dict[str, tuple[str, float, float]]:
    result: dict[str, tuple[str, float, float]] = {}
    clean_symbols = [normalize_ashare_symbol(symbol) for symbol in symbols]
    for start in range(0, len(clean_symbols), max(1, chunk_size)):
        chunk = clean_symbols[start : start + max(1, chunk_size)]
        query = ",".join(tencent_symbol(symbol) for symbol in chunk)
        if not query:
            continue
        request = Request(f"https://qt.gtimg.cn/q={quote(query)}", headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=6) as response:
                text = response.read().decode("gbk", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if '="' not in line:
                continue
            try:
                body = line.split('="', 1)[1].rsplit('"', 1)[0]
                parts = body.split("~")
                if len(parts) < 45:
                    continue
                symbol = normalize_ashare_symbol(parts[2])
                name = parts[1].strip()
                latest = float(parts[3] or 0.0)
                market_cap = float(parts[44] or 0.0)
                if latest <= 0 or market_cap <= 0:
                    continue
                result[symbol] = (name, market_cap, latest)
            except Exception:
                continue
    return result


def fetch_tencent_qfq_bars(symbol: str, start_day: date, end_day: date) -> tuple[list[AShareBar], str]:
    code = tencent_symbol(symbol)
    count = max(300, min(900, (end_day - start_day).days + 80))
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={quote(f'{code},day,,,{count},qfq')}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    stock = (payload.get("data") or {}).get(code) or {}
    rows = stock.get("qfqday") or stock.get("day") or []
    bars: list[AShareBar] = []
    for row in rows:
        try:
            bar_date = date.fromisoformat(str(row[0])[:10])
            if bar_date < start_day or bar_date > end_day:
                continue
            bars.append(
                AShareBar(
                    date=bar_date.isoformat(),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]),
                    amount=0.0,
                )
            )
        except Exception:
            continue
    if not bars:
        raise RuntimeError("腾讯前复权日线返回为空")
    bars.sort(key=lambda bar: bar.date)
    return bars, "tencent qfq"


def fetch_ashare_bars(symbol: str, start: str | None = None, end: str | None = None) -> tuple[list[AShareBar], str]:
    clean = normalize_ashare_symbol(symbol)
    end_day = date.today() if end is None else date.fromisoformat(end)
    start_day = end_day - timedelta(days=520) if start is None else date.fromisoformat(start)
    start_ak = start_day.strftime("%Y%m%d")
    end_ak = end_day.strftime("%Y%m%d")

    try:
        return fetch_tencent_qfq_bars(clean, start_day, end_day)
    except Exception:
        pass

    try:
        import efinance as ef

        df = ef.stock.get_quote_history(clean, beg=start_ak, end=end_ak, klt=101, fqt=1)
        bars = [
            AShareBar(
                date=str(row_value(row, ("日期", "date"), 2)),
                open=float(row_value(row, ("开盘", "open"), 3)),
                high=float(row_value(row, ("最高", "high"), 5)),
                low=float(row_value(row, ("最低", "low"), 6)),
                close=float(row_value(row, ("收盘", "close"), 4)),
                volume=float(row_value(row, ("成交量", "volume"), 7)),
                amount=float(row.get("成交额", row.get("amount", 0.0))),
            )
            for _, row in df.iterrows()
        ]
        if bars:
            return bars, "efinance"
    except Exception:
        pass

    try:
        return fetch_tdx_ashare_bars(clean, start_day, end_day)
    except Exception:
        pass

    try:
        raise RuntimeError("akshare disabled")
        raise RuntimeError("akshare disabled")
        import akshare as ak

        df = ak.stock_zh_a_hist(symbol=clean, period="daily", start_date=start_ak, end_date=end_ak, adjust="qfq")
        bars = [
            AShareBar(
                date=str(row_value(row, ("日期", "date"), 0)),
                open=float(row_value(row, ("开盘", "open"), 1)),
                high=float(row_value(row, ("最高", "high"), 2)),
                low=float(row_value(row, ("最低", "low"), 3)),
                close=float(row_value(row, ("收盘", "close"), 4)),
                volume=float(row_value(row, ("成交量", "volume"), 5)),
                amount=float(row.get("成交额", row.get("amount", 0.0))),
            )
            for _, row in df.iterrows()
        ]
        if bars:
            return bars, "akshare"
    except Exception:
        pass

    raise RuntimeError(f"{clean} 没有可用 A 股日线数据源")

    try:
        import yfinance as yf

        yf_symbol = clean + yahoo_suffix(clean)
        df = yf.download(
            yf_symbol,
            start=start_day.isoformat(),
            end=(end_day + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=False,
        )
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = [col[0] for col in df.columns]
        bars = [
            AShareBar(
                date=str(index.date()),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
                amount=0.0,
            )
            for index, row in df.iterrows()
        ]
        if bars:
            return bars, "yfinance"
    except Exception as exc:
        raise RuntimeError(f"A 股日线拉取失败：{exc}") from exc

    raise RuntimeError(f"{clean} 没有可用日线数据。")


def fetch_ashare_name(symbol: str) -> str:
    clean = normalize_ashare_symbol(symbol)
    try:
        raise RuntimeError("akshare disabled")
        import akshare as ak

        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"].astype(str) == clean]
        if not row.empty:
            return str(row.iloc[0].get("名称", ""))
    except Exception:
        pass
    try:
        raise RuntimeError("akshare disabled")
        import akshare as ak

        names = ak.stock_info_a_code_name()
        row = names[names["code"].astype(str) == clean]
        if not row.empty:
            return str(row.iloc[0].get("name", ""))
    except Exception:
        pass
    try:
        import akshare as ak

        spot = ak.stock_zh_a_spot()
        normalized = spot["代码"].astype(str).map(lambda value: "".join(ch for ch in value.upper() if ch.isalnum())[-6:])
        row = spot[normalized == clean]
        if not row.empty:
            return str(row.iloc[0].get("名称", ""))
    except Exception:
        pass
    return ""


def read_json_cache(path: Path, max_age_seconds: int) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > max_age_seconds:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def ashare_universe_cache_key(min_market_cap_100m: float, max_symbols: int) -> str:
    return f"{float(min_market_cap_100m):.2f}:{int(max_symbols)}"


def serialize_universe_item(item: AShareUniverseItem) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "name": item.name,
        "sector": item.sector,
        "market_cap_100m": item.market_cap_100m,
        "exchange": item.exchange,
        "turnover": item.turnover,
    }


def deserialize_universe_item(raw: dict[str, Any]) -> AShareUniverseItem:
    return AShareUniverseItem(
        symbol=str(raw.get("symbol", "")),
        name=str(raw.get("name", "") or ""),
        sector=str(raw.get("sector", "") or ""),
        market_cap_100m=float(raw.get("market_cap_100m") or 0.0),
        exchange=str(raw.get("exchange", "") or ""),
        turnover=float(raw.get("turnover") or 0.0),
    )


def read_ashare_universe_cache(
    min_market_cap_100m: float,
    max_symbols: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[AShareUniverseItem], str, bool] | None:
    payload = read_json_cache(ASHARE_UNIVERSE_CACHE_PATH, ASHARE_UNIVERSE_CACHE_SECONDS)
    if not payload:
        return None
    key = ashare_universe_cache_key(min_market_cap_100m, max_symbols)
    entry = (payload.get("entries") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    if "akshare" in str(entry.get("source", "")).lower():
        return None
    if min_market_cap_100m > 0 and not bool(entry.get("has_market_cap", False)):
        return None
    raw_items = entry.get("items") or []
    items = [deserialize_universe_item(raw) for raw in raw_items if isinstance(raw, dict)]
    if not items:
        return None
    if progress:
        progress(f"使用当天股票池缓存：{len(items)} 只")
    return items, f"{entry.get('source', 'unknown')} cache", bool(entry.get("has_market_cap", False))


def write_ashare_universe_cache(
    min_market_cap_100m: float,
    max_symbols: int,
    items: list[AShareUniverseItem],
    source: str,
    has_market_cap: bool,
) -> None:
    payload = read_json_cache(ASHARE_UNIVERSE_CACHE_PATH, 30 * 24 * 60 * 60) or {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    key = ashare_universe_cache_key(min_market_cap_100m, max_symbols)
    entries[key] = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "has_market_cap": has_market_cap,
        "items": [serialize_universe_item(item) for item in items],
    }
    write_json_cache(
        ASHARE_UNIVERSE_CACHE_PATH,
        {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "entries": entries},
    )


def load_ashare_sector_map() -> dict[str, str]:
    cached = read_json_cache(ASHARE_SECTOR_CACHE_PATH, ASHARE_SECTOR_CACHE_SECONDS)
    if cached and isinstance(cached.get("sectors"), dict):
        return {str(k): str(v) for k, v in cached["sectors"].items()}

    sectors: dict[str, str] = {}
    try:
        import akshare as ak

        boards = ak.stock_sector_spot()
        for _, board in boards.iterrows():
            label = str(board.get("label", "") or "")
            sector_name = str(board.get("板块", "") or "")
            if not label or not sector_name:
                continue
            try:
                detail = ak.stock_sector_detail(sector=label)
            except Exception:
                continue
            for _, row in detail.iterrows():
                try:
                    symbol = normalize_ashare_symbol(str(row.get("code", row.get("代码", ""))))
                    sectors.setdefault(symbol, sector_name)
                except Exception:
                    continue
    except Exception:
        return {}

    write_json_cache(ASHARE_SECTOR_CACHE_PATH, {"updated_at": date.today().isoformat(), "sectors": sectors})
    return sectors


def fetch_ashare_name(symbol: str) -> str:
    clean = normalize_ashare_symbol(symbol)
    cached = read_json_cache(ASHARE_UNIVERSE_CACHE_PATH, 30 * 24 * 60 * 60) or {}
    for entry in (cached.get("entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("items") or []:
            if isinstance(raw, dict) and normalize_ashare_symbol(str(raw.get("symbol", ""))) == clean:
                return str(raw.get("name", "") or "")
    return ""


def load_ashare_sector_map() -> dict[str, str]:
    cached = read_json_cache(ASHARE_SECTOR_CACHE_PATH, 30 * 24 * 60 * 60)
    if cached and isinstance(cached.get("sectors"), dict):
        return {str(k): str(v) for k, v in cached["sectors"].items()}
    return {}


def fetch_ashare_profile(symbol: str) -> tuple[str, str]:
    clean = normalize_ashare_symbol(symbol)
    name = fetch_ashare_name(clean)
    sector_map = load_ashare_sector_map()
    return name, sector_map.get(clean, "")


def cached_ashare_universe_items() -> list[AShareUniverseItem]:
    payload = read_json_cache(ASHARE_UNIVERSE_CACHE_PATH, 30 * 24 * 60 * 60) or {}
    items_by_symbol: dict[str, AShareUniverseItem] = {}
    for entry in (payload.get("entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("items") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = deserialize_universe_item(raw)
                if item.symbol and item.symbol not in items_by_symbol:
                    items_by_symbol[item.symbol] = item
            except Exception:
                continue
    return sorted(items_by_symbol.values(), key=lambda item: item.turnover or item.market_cap_100m, reverse=True)


def suggest_ashare_symbols(query: str, limit: int = 12) -> list[dict[str, object]]:
    q_raw = query.strip()
    q = q_raw.upper()
    if not q:
        return []
    items = cached_ashare_universe_items()
    if not items:
        try:
            items, _, _ = load_ashare_universe_for_scan(0, 800)
        except Exception:
            items = []
    scored: list[tuple[int, AShareUniverseItem]] = []
    for item in items:
        symbol = item.symbol.upper()
        name = item.name.upper()
        if symbol == q or item.name == q_raw:
            score = 0
        elif symbol.startswith(q):
            score = 1
        elif q in symbol:
            score = 2
        elif item.name.startswith(q_raw):
            score = 3
        elif q in name or q_raw in item.name:
            score = 4
        else:
            continue
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], -(pair[1].turnover or pair[1].market_cap_100m)))
    return [
        {
            "symbol": item.symbol,
            "name": item.name,
            "sector": item.sector,
            "exchange": item.exchange,
            "label": f"{item.symbol} {item.name}".strip(),
            "value": f"{item.symbol} {item.name}".strip(),
        }
        for _, item in scored[: max(1, int(limit))]
    ]


def resolve_ashare_symbol_query(query: str) -> str:
    raw = query.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 6:
        return normalize_ashare_symbol(digits[:6])
    suggestions = suggest_ashare_symbols(raw, 1)
    if suggestions:
        return normalize_ashare_symbol(str(suggestions[0]["symbol"]))
    return normalize_ashare_symbol(raw)


def ashare_exchange(symbol: str) -> str:
    if symbol.startswith(("4", "8", "9")):
        return "BJ"
    if symbol.startswith("688"):
        return "STAR"
    if symbol.startswith("6"):
        return "SH"
    if symbol.startswith(("0", "3")):
        return "SZ"
    if symbol.startswith(("4", "8")):
        return "BJ"
    return "-"


ASHARE_BOARD_LABELS = {
    "main": "沪深主板",
    "chinext": "创业板",
    "star": "科创板",
    "bj": "北交所",
}


def ashare_board(symbol: str) -> str:
    clean = normalize_ashare_symbol(symbol)
    if clean.startswith(("4", "8", "9")):
        return "bj"
    if clean.startswith("688"):
        return "star"
    if clean.startswith("3"):
        return "chinext"
    if clean.startswith(("0", "6")):
        return "main"
    return "other"


def normalize_ashare_boards(boards: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    valid = set(ASHARE_BOARD_LABELS)
    selected = [str(board).strip().lower() for board in (boards or []) if str(board).strip().lower() in valid]
    return selected or list(ASHARE_BOARD_LABELS)


def ashare_board_filter_label(boards: list[str] | tuple[str, ...] | set[str] | None) -> str:
    selected = normalize_ashare_boards(boards)
    if set(selected) == set(ASHARE_BOARD_LABELS):
        return "全部板块"
    return "、".join(ASHARE_BOARD_LABELS[board] for board in selected)


def filter_ashare_universe_by_board(items: list[AShareUniverseItem], boards: list[str] | tuple[str, ...] | set[str] | None) -> list[AShareUniverseItem]:
    selected = set(normalize_ashare_boards(boards))
    if selected == set(ASHARE_BOARD_LABELS):
        return items
    return [item for item in items if ashare_board(item.symbol) in selected]


def fetch_eastmoney_ashare_universe(
    min_market_cap_100m: float = 50.0,
    max_symbols: int = 300,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[AShareUniverseItem], str, bool]:
    import requests

    urls = [
        "http://82.push2.eastmoney.com/api/qt/clist/get",
        "http://33.push2.eastmoney.com/api/qt/clist/get",
    ]
    fields = "f12,f14,f2,f6,f20,f8"
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    page_size = max(100, min(500, int(max_symbols) * 2))
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
    }
    last_error: Exception | None = None
    rows: list[dict[str, Any]] = []
    source_url = ""
    for attempt in range(1):
        if attempt:
            if progress:
                progress(f"东方财富股票池重试中，第 {attempt + 1} 次")
            time.sleep(0.8 * attempt)
        for url in urls:
            try:
                rows = []
                source_url = url.split("//", 1)[-1].split("/", 1)[0]
                if progress:
                    progress(f"正在尝试东方财富节点：{source_url}")
                for page in range(1, 8):
                    params = {
                        "pn": page,
                        "pz": page_size,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f20",
                        "fs": fs,
                        "fields": fields,
                        "_": int(time.time() * 1000),
                    }
                    response = requests.get(url, params=params, headers=headers, timeout=2)
                    response.raise_for_status()
                    payload = response.json()
                    data = payload.get("data") or {}
                    diff = data.get("diff") or []
                    if not diff:
                        break
                    rows.extend(diff)
                    if len(rows) >= max(1000, int(max_symbols)):
                        break
                if rows:
                    if progress:
                        progress(f"东方财富股票池已返回 {len(rows)} 条原始记录")
                    break
            except Exception as exc:
                last_error = exc
                if progress:
                    progress(f"{source_url or '东方财富节点'} 暂不可用，切换下一个数据源")
                rows = []
                continue
        if rows:
            break
    if not rows:
        raise RuntimeError(str(last_error) if last_error else "东方财富股票池返回为空")

    items: list[AShareUniverseItem] = []
    for row in rows:
        try:
            symbol = normalize_ashare_symbol(str(row.get("f12", "")))
            name = str(row.get("f14", "") or "")
            if not name or "ST" in name.upper() or "退" in name:
                continue
            latest = row.get("f2")
            if latest in (None, "", "-", "--") or float(latest) <= 0:
                continue
            market_cap = float(row.get("f20") or 0.0) / 100_000_000
            if market_cap < min_market_cap_100m and len(items) >= max(1, int(max_symbols)):
                break
            if market_cap < min_market_cap_100m:
                continue
            items.append(
                AShareUniverseItem(
                    symbol=symbol,
                    name=name,
                    sector="",
                    market_cap_100m=market_cap,
                    exchange=ashare_exchange(symbol),
                    turnover=float(row.get("f6") or 0.0),
                )
            )
        except Exception:
            continue
    items.sort(key=lambda item: item.market_cap_100m, reverse=True)
    return items[: max(1, int(max_symbols))], f"eastmoney.qt.clist {source_url}", True


def fetch_efinance_ashare_universe(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> tuple[list[AShareUniverseItem], str, bool]:
    import efinance as ef

    df = ef.stock.get_realtime_quotes()
    items: list[AShareUniverseItem] = []
    for _, row in df.iterrows():
        try:
            symbol = normalize_ashare_symbol(str(row.get("股票代码", row.get("code", ""))))
            name = str(row.get("股票名称", row.get("name", "")) or "")
            if not name or "ST" in name.upper() or "退" in name:
                continue
            latest = row.get("最新价", row.get("price", ""))
            if str(latest).strip() in ("", "-", "--", "nan", "None") or float(latest) <= 0:
                continue
            market_cap = float(row.get("总市值", row.get("market_cap", 0.0)) or 0.0) / 100_000_000
            if market_cap < min_market_cap_100m:
                continue
            items.append(
                AShareUniverseItem(
                    symbol=symbol,
                    name=name,
                    sector="",
                    market_cap_100m=market_cap,
                    exchange=ashare_exchange(symbol),
                    turnover=float(row.get("成交额", row.get("amount", 0.0)) or 0.0),
                )
            )
        except Exception:
            continue
    items.sort(key=lambda item: item.market_cap_100m, reverse=True)
    return items[: max(1, int(max_symbols))], "efinance.get_realtime_quotes", True


def fetch_tdx_ashare_universe(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> tuple[list[AShareUniverseItem], str, bool]:
    from pytdx.hq import TdxHq_API
    from pytdx.params import TDXParams

    allowed = {
        TDXParams.MARKET_SH: ("600", "601", "603", "605", "688"),
        TDXParams.MARKET_SZ: ("000", "001", "002", "003", "300", "301"),
    }
    items_by_symbol: dict[str, AShareUniverseItem] = {}
    last_error: Exception | None = None
    for host, port in TDX_SERVERS:
        api = TdxHq_API(raise_exception=False, auto_retry=False)
        try:
            api.connect(host, port, time_out=2)
            for market, prefixes in allowed.items():
                count = api.get_security_count(market) or 0
                for start in range(0, min(count + 1000, 30000), 1000):
                    try:
                        rows = api.get_security_list(market, start) or []
                    except Exception:
                        continue
                    for row in rows:
                        symbol = normalize_ashare_symbol(str(row.get("code", "")))
                        name = str(row.get("name", "") or "")
                        if len(symbol) != 6 or not symbol.startswith(prefixes):
                            continue
                        if not name or "ST" in name.upper() or "退" in name or symbol in items_by_symbol:
                            continue
                        items_by_symbol[symbol] = AShareUniverseItem(
                            symbol=symbol,
                            name=name,
                            sector="",
                            market_cap_100m=0.0,
                            exchange=ashare_exchange(symbol),
                            turnover=0.0,
                        )
            if items_by_symbol:
                caps = fetch_tencent_market_caps(list(items_by_symbol))
                items: list[AShareUniverseItem] = []
                for symbol, item in items_by_symbol.items():
                    quote_row = caps.get(symbol)
                    if not quote_row:
                        continue
                    quote_name, market_cap, _ = quote_row
                    if market_cap < min_market_cap_100m:
                        continue
                    items.append(
                        AShareUniverseItem(
                            symbol=symbol,
                            name=quote_name or item.name,
                            sector="",
                            market_cap_100m=market_cap,
                            exchange=item.exchange,
                            turnover=item.turnover,
                        )
                    )
                items.sort(key=lambda item: item.market_cap_100m, reverse=True)
                if items:
                    return items[: max(1, int(max_symbols))], f"tdx security list {host}:{port} + tencent market cap", True
        except Exception as exc:
            last_error = exc
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    raise RuntimeError(str(last_error) if last_error else "通达信股票池不可用")


def fetch_akshare_code_name_universe(max_symbols: int = 300) -> tuple[list[AShareUniverseItem], str, bool]:
    raise RuntimeError("akshare disabled")
    import akshare as ak

    df = ak.stock_info_a_code_name()
    items: list[AShareUniverseItem] = []
    for _, row in df.iterrows():
        try:
            symbol = normalize_ashare_symbol(str(row.get("code", row.iloc[0])))
            name = str(row.get("name", row.iloc[1] if len(row) > 1 else "") or "")
            if not name or "ST" in name.upper() or "退" in name:
                continue
            items.append(
                AShareUniverseItem(
                    symbol=symbol,
                    name=name,
                    sector="",
                    market_cap_100m=0.0,
                    exchange=ashare_exchange(symbol),
                    turnover=0.0,
                )
            )
        except Exception:
            continue
    exchange_order = {"SH": 0, "SZ": 1, "STAR": 2, "BJ": 3}
    items.sort(key=lambda item: (exchange_order.get(item.exchange, 9), item.symbol))
    return items[: max(1, int(max_symbols))], "akshare.stock_info_a_code_name fallback", False


def load_ashare_universe(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> list[AShareUniverseItem]:
    items, _, _ = load_ashare_universe_with_meta(min_market_cap_100m, max_symbols)
    return items


def load_ashare_universe_with_meta(
    min_market_cap_100m: float = 50.0,
    max_symbols: int = 300,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[AShareUniverseItem], str, bool]:
    cached = read_ashare_universe_cache(min_market_cap_100m, max_symbols, progress)
    if cached:
        return cached
    try:
        if progress:
            progress("正在尝试 efinance 股票池")
        result = fetch_efinance_ashare_universe(min_market_cap_100m, max_symbols)
        write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
        return result
    except Exception:
        if progress:
            progress("efinance 股票池不可用，切换东方财富直连")
    try:
        result = fetch_eastmoney_ashare_universe(min_market_cap_100m, max_symbols, progress)
        write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
        return result
    except Exception as eastmoney_exc:
        eastmoney_error = eastmoney_exc
        if progress:
            progress("东方财富股票池不可用，切换通达信股票列表兜底")
        try:
            result = fetch_tdx_ashare_universe(min_market_cap_100m, max_symbols)
            write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
            return result
        except Exception as tdx_exc:
            raise RuntimeError(f"A 股股票池拉取失败：efinance、东方财富直连、通达信列表均不可用；东方财富 {eastmoney_error}；通达信 {tdx_exc}") from tdx_exc
        if progress:
            progress("东方财富股票池不可用，切换代码/名称表快速兜底")
        try:
            result = fetch_akshare_code_name_universe(max_symbols)
            write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
            return result
        except Exception:
            if progress:
                progress("代码/名称表兜底不可用，正在尝试 akshare 总市值接口")
    try:
        raise RuntimeError("akshare disabled")
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        source = "akshare.stock_zh_a_spot_em"
        has_market_cap = True
    except Exception as exc:
        try:
            raise RuntimeError("akshare disabled")
            import akshare as ak

            if progress:
                progress("akshare 总市值接口不可用，正在尝试普通行情接口")
            df = ak.stock_zh_a_spot()
            source = "akshare.stock_zh_a_spot fallback"
            has_market_cap = False
        except Exception as fallback_exc:
            if progress:
                progress("akshare 行情接口不可用，切换代码/名称表兜底")
            try:
                result = fetch_akshare_code_name_universe(max_symbols)
                write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
                return result
            except Exception as code_name_exc:
                raise RuntimeError(f"A 股股票池拉取失败：东方财富 {eastmoney_error}；akshare {fallback_exc}；代码表 {code_name_exc}") from code_name_exc

    sector_map = load_ashare_sector_map()
    items: list[AShareUniverseItem] = []
    for _, row in df.iterrows():
        try:
            symbol = normalize_ashare_symbol(str(row_value(row, ("代码", "code"), 1)))
            name = str(row_value(row, ("名称", "name"), 2))
            if "ST" in name.upper() or "退" in name:
                continue
            latest = row.get("最新价", row.get("price", ""))
            if str(latest).strip() in ("", "-", "--", "nan", "None"):
                continue
            market_cap = float(row.get("总市值", row.get("market_cap", 0.0))) / 100_000_000 if has_market_cap else 0.0
            if has_market_cap and market_cap < min_market_cap_100m:
                continue
            turnover = float(row.get("成交额", row.get("amount", 0.0)))
            items.append(
                AShareUniverseItem(
                    symbol=symbol,
                    name=name,
                    sector=sector_map.get(symbol, ""),
                    market_cap_100m=market_cap,
                    exchange=ashare_exchange(symbol),
                    turnover=turnover,
                )
            )
        except Exception:
            continue
    if has_market_cap:
        items.sort(key=lambda item: item.market_cap_100m, reverse=True)
    else:
        items.sort(key=lambda item: item.turnover, reverse=True)
    result = items[: max(1, int(max_symbols))], source, has_market_cap
    write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
    return result


def load_ashare_universe_for_scan(
    min_market_cap_100m: float = 50.0,
    max_symbols: int = 300,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[AShareUniverseItem], str, bool]:
    cached = read_ashare_universe_cache(min_market_cap_100m, max_symbols, progress)
    if cached:
        return cached
    try:
        if progress:
            progress("正在尝试 efinance 股票池")
        result = fetch_efinance_ashare_universe(min_market_cap_100m, max_symbols)
        write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
        return result
    except Exception:
        if progress:
            progress("efinance 股票池不可用，切换东方财富直连")
    try:
        result = fetch_eastmoney_ashare_universe(min_market_cap_100m, max_symbols, progress)
        write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
        return result
    except Exception as eastmoney_exc:
        eastmoney_error = eastmoney_exc
        if progress:
            progress("东方财富股票池不可用，切换通达信股票列表兜底")
        try:
            result = fetch_tdx_ashare_universe(min_market_cap_100m, max_symbols)
            write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
            return result
        except Exception as tdx_exc:
            raise RuntimeError(f"A 股股票池拉取失败：efinance、东方财富直连、通达信列表均不可用；东方财富 {eastmoney_error}；通达信 {tdx_exc}") from tdx_exc
        if progress:
            progress("东方财富股票池不可用，切换代码/名称表快速兜底")
        try:
            result = fetch_akshare_code_name_universe(max_symbols)
            write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
            return result
        except Exception:
            if progress:
                progress("代码/名称表兜底不可用，正在尝试 akshare 总市值接口")
    try:
        raise RuntimeError("akshare disabled")
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        source = "akshare.stock_zh_a_spot_em"
        has_market_cap = True
    except Exception as exc:
        try:
            raise RuntimeError("akshare disabled")
            import akshare as ak

            if progress:
                progress("akshare 总市值接口不可用，正在尝试普通行情接口")
            df = ak.stock_zh_a_spot()
            source = "akshare.stock_zh_a_spot fallback"
            has_market_cap = False
        except Exception as fallback_exc:
            if progress:
                progress("akshare 行情接口不可用，切换代码/名称表兜底")
            try:
                result = fetch_akshare_code_name_universe(max_symbols)
                write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
                return result
            except Exception as code_name_exc:
                raise RuntimeError(f"A 股股票池拉取失败：东方财富 {eastmoney_error}；akshare {fallback_exc}；代码表 {code_name_exc}") from code_name_exc

    sector_map = load_ashare_sector_map()
    items: list[AShareUniverseItem] = []
    for _, row in df.iterrows():
        try:
            raw_symbol = str(row.get("代码", row.get("code", row.iloc[0])))
            symbol = normalize_ashare_symbol(raw_symbol)
            name = str(row.get("名称", row.get("name", row.iloc[1])) or "")
            if not name or "ST" in name.upper() or "退" in name:
                continue
            latest = row.get("最新价", row.get("price", row.iloc[2] if len(row) > 2 else ""))
            if str(latest).strip() in ("", "-", "--", "nan", "None"):
                continue
            if float(latest or 0) <= 0:
                continue
            market_cap = float(row.get("总市值", row.get("market_cap", 0.0)) or 0.0) / 100_000_000 if has_market_cap else 0.0
            if has_market_cap and market_cap < min_market_cap_100m:
                continue
            turnover = float(row.get("成交额", row.get("amount", row.iloc[12] if len(row) > 12 else 0.0)) or 0.0)
            items.append(
                AShareUniverseItem(
                    symbol=symbol,
                    name=name,
                    sector=sector_map.get(symbol, ""),
                    market_cap_100m=market_cap,
                    exchange=ashare_exchange(symbol),
                    turnover=turnover,
                )
            )
        except Exception:
            continue
    if has_market_cap:
        items.sort(key=lambda item: item.market_cap_100m, reverse=True)
    else:
        items.sort(key=lambda item: item.turnover, reverse=True)
    result = items[: max(1, int(max_symbols))], source, has_market_cap
    write_ashare_universe_cache(min_market_cap_100m, max_symbols, *result)
    return result


def ashare_limit_pct(symbol: str) -> float:
    clean = normalize_ashare_symbol(symbol)
    if clean.startswith(("4", "8", "9")):
        return 30.0
    if clean.startswith(("3", "688")):
        return 20.0
    return 10.0


def ashare_to_backtest_bars(bars: list[AShareBar]):
    from backtest import Bar

    return [Bar(date=bar.date, open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume) for bar in bars]


def ashare_limit_volume_context(bar: AShareBar, previous_close: float, limit_pct: float) -> tuple[str, str]:
    if previous_close <= 0 or limit_pct <= 0:
        return "正常", "正常交易日：成交量按原始值参与评分"
    up_threshold = previous_close * (1 + (limit_pct - 0.35) / 100)
    down_threshold = previous_close * (1 - (limit_pct - 0.35) / 100)
    sealed = (bar.high - bar.low) / previous_close <= 0.005
    if bar.open >= up_threshold and bar.high >= up_threshold and bar.close >= up_threshold:
        if sealed:
            return "一字涨停", "一字涨停：成交量失真，量能评分已剔除该日成交量"
        return "涨停", "涨停分歧：成交量按 50% 权重参与评分"
    if bar.open <= down_threshold and bar.low <= down_threshold and bar.close <= down_threshold:
        if sealed:
            return "一字跌停", "一字跌停：成交量失真，量能评分已剔除该日成交量"
        return "跌停", "跌停分歧：成交量按 50% 权重参与评分"
    return "正常", "正常交易日：成交量按原始值参与评分"


def latest_ashare_signal(
    symbol: str,
    j_threshold: float = 14.0,
    fetch_name_value: bool = True,
    min_avg_amount_20d: float = 100_000_000,
    min_control_amount_20d: float = 200_000_000,
    vol_multiplier: float = 1.45,
    vol_high_days: int = 2,
    vol_high_multiplier: float = 1.0,
    massive_window: int = 7,
    massive_min_count: int = 1,
    reentry_pct: float = 0.045,
    strong_volume_score: float = 4.0,
    medium_volume_score: float = 2.5,
    b1_require_20ma_gt_50ma: bool = True,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> AShareSignalSnapshot:
    clean = normalize_ashare_symbol(symbol)
    bars, source = fetch_ashare_bars(clean)
    if len(bars) < 130:
        raise ValueError(f"数据不足：至少需要 130 根日 K，当前 {len(bars)} 根。")

    from backtest import adjust_limit_volumes, build_ratchet_inputs, rolling_sma

    bt_bars = ashare_to_backtest_bars(bars)
    closes = [bar.close for bar in bt_bars]
    limit_pct = ashare_limit_pct(clean)
    adjusted_volumes = adjust_limit_volumes(bt_bars, limit_pct)
    volumes = adjusted_volumes
    amounts = [bar.amount if bar.amount > 0 else bar.close * bar.volume for bar in bars]
    ma5_series = rolling_sma(closes, 5)
    ma20_series = rolling_sma(closes, 20)
    vol_ma20 = rolling_sma(adjusted_volumes, 20)
    avg_amount20_series = rolling_sma(amounts, 20)
    buy_signal, _, ma, vol_ma, buy_target_pct, buy_stage = build_ratchet_inputs(
        bt_bars,
        ma_length=5,
        vol_length=20,
        vol_multiplier=vol_multiplier,
        reentry_pct=reentry_pct,
        vol_high_days=max(1, int(vol_high_days)),
        vol_high_multiplier=vol_high_multiplier,
        massive_window=max(1, int(massive_window)),
        massive_min_count=max(1, int(massive_min_count)),
        massive_max_count=2,
        b1_require_20ma_gt_50ma=b1_require_20ma_gt_50ma,
        require_ma5_rising=require_ma5_rising,
        require_5ma_gt_20ma=require_5ma_gt_20ma,
        signal_volumes=adjusted_volumes,
    )

    i = len(bars) - 1
    latest = bt_bars[i]
    previous_close = bt_bars[i - 1].close if i > 0 else 0.0
    limit_up = previous_close > 0 and latest.close >= previous_close * (1 + (limit_pct - 0.35) / 100)
    limit_down = previous_close > 0 and latest.close <= previous_close * (1 - (limit_pct - 0.35) / 100)
    limit_state, volume_context = ashare_limit_volume_context(bars[i], previous_close, limit_pct)
    avg_amount_20d = float(avg_amount20_series[i] or 0.0)
    amount_ok = avg_amount_20d >= min_avg_amount_20d
    ma5 = float(ma5_series[i] or 0.0)
    ma20 = float(ma20_series[i] or 0.0)
    volume_ratio = adjusted_volumes[i] / float(vol_ma20[i] or 0.0) if vol_ma20[i] else 0.0
    ma5_rising_ok = i > 0 and ma5_series[i - 1] and ma5 > float(ma5_series[i - 1])
    ma5_gt_20_ok = bool(ma5 and ma20 and ma5 > ma20)
    ma50_series = rolling_sma(closes, 50)
    ma20_gt_50_ok = bool(ma20_series[i] is not None and ma50_series[i] is not None and float(ma20_series[i]) > float(ma50_series[i]))
    trend_ok = bool(
        ma5
        and latest.close > ma5
        and (ma5_gt_20_ok or not require_5ma_gt_20ma)
        and (ma5_rising_ok or not require_ma5_rising)
        and (ma20_gt_50_ok or not b1_require_20ma_gt_50ma)
    )
    signal_type = str(buy_stage[i] or "")
    hard_candidate = bool(buy_signal[i] and amount_ok)

    base_start = max(0, i - 80)
    base_end = max(0, i - 20)
    base_volume = sum(volumes[base_start:base_end]) / max(1, base_end - base_start)
    recent_start = max(0, i - 20)
    recent_indexes = list(range(recent_start, i + 1))
    recent_peak = max(volumes[recent_start : i + 1])
    recent_avg10 = sum(volumes[max(0, i - 9) : i + 1]) / min(10, i + 1)

    red_volumes: list[float] = []
    green_volumes: list[float] = []
    for idx in recent_indexes:
        if idx == 0:
            continue
        if closes[idx] >= closes[idx - 1]:
            red_volumes.append(volumes[idx])
        else:
            green_volumes.append(volumes[idx])
    red_avg = sum(red_volumes) / len(red_volumes) if red_volumes else 0.0
    green_avg = sum(green_volumes) / len(green_volumes) if green_volumes else 0.0
    top5 = sorted(recent_indexes, key=lambda idx: volumes[idx], reverse=True)[:5]
    top5_red_count = sum(1 for idx in top5 if idx > 0 and closes[idx] >= closes[idx - 1])

    peak_to_base = recent_peak / base_volume if base_volume else 0.0
    avg10_to_base = recent_avg10 / base_volume if base_volume else 0.0
    red_to_green = red_avg / green_avg if green_avg else 0.0
    volume_structure_ok = (
        base_volume > 0
        and peak_to_base > 3
        and avg10_to_base > 1.5
        and red_to_green > 1.3
        and top5_red_count >= 3
    )
    volume_score = 0.0
    volume_score += min(1.5, peak_to_base / 3 * 1.5) if base_volume else 0.0
    volume_score += min(1.0, avg10_to_base / 1.5) if base_volume else 0.0
    volume_score += min(1.0, red_to_green / 1.3) if green_avg else 0.0
    volume_score += min(1.0, top5_red_count / 3) if top5 else 0.0
    volume_score += min(0.5, volume_ratio / 2) if volume_ratio else 0.0
    volume_score = round(min(5.0, volume_score), 2)

    if hard_candidate and volume_score >= strong_volume_score and not limit_up:
        candidate_rating = "Strong"
    elif hard_candidate and volume_score >= medium_volume_score:
        candidate_rating = "Medium"
    elif hard_candidate:
        candidate_rating = "Watch"
    else:
        candidate_rating = "None"

    execution_note = ""
    if limit_up:
        execution_note = "信号日接近涨停，次日若一字板或高开过大应跳过"
    elif avg_amount_20d < min_control_amount_20d:
        execution_note = "成交额达标但不高，实盘需控制仓位"
    else:
        execution_note = "按 A 股执行规则：次日非涨停且高开不超过 6% 才买入"
    execution_note = f"{execution_note}；{volume_context}"

    name = ""
    sector = ""
    if fetch_name_value:
        name, sector = fetch_ashare_profile(clean)
    return AShareSignalSnapshot(
        symbol=clean,
        name=name,
        sector=sector,
        data_source=source,
        latest_date=bars[i].date,
        close=bars[i].close,
        zx_short_trend=ma5,
        zx_multi_trend=ma20,
        zx_multi_slope=(ma5 - float(ma5_series[i - 1] or ma5)) if i > 0 else 0.0,
        j_value=volume_ratio,
        trend_ok=bool(trend_ok),
        j_oversold=bool(amount_ok),
        volume_structure_ok=bool(volume_structure_ok),
        signal=hard_candidate,
        volume_score=volume_score,
        candidate_rating=candidate_rating,
        base_volume=base_volume,
        recent_peak_volume=recent_peak,
        recent_peak_to_base=peak_to_base,
        recent_avg10_to_base=avg10_to_base,
        red_days=len(red_volumes),
        green_days=len(green_volumes),
        red_avg_to_green_avg=red_to_green,
        top5_red_count=top5_red_count,
        bars_count=len(bars),
        signal_type=signal_type,
        ma5=ma5,
        ma20=ma20,
        volume_ratio=volume_ratio,
        avg_amount_20d=avg_amount_20d,
        amount_ok=amount_ok,
        limit_state=limit_state,
        volume_context=volume_context,
        execution_note=execution_note,
    )

def scan_ashare_candidates(
    min_market_cap_100m: float = 50.0,
    max_symbols: int = 300,
    j_threshold: float = 14.0,
    max_workers: int = 6,
    boards: list[str] | None = None,
    min_avg_amount_20d: float = 100_000_000,
    min_control_amount_20d: float = 200_000_000,
    vol_multiplier: float = 1.45,
    vol_high_days: int = 2,
    vol_high_multiplier: float = 1.0,
    massive_window: int = 7,
    massive_min_count: int = 1,
    reentry_pct: float = 0.045,
    strong_volume_score: float = 4.0,
    medium_volume_score: float = 2.5,
    b1_require_20ma_gt_50ma: bool = True,
    require_ma5_rising: bool = True,
    require_5ma_gt_20ma: bool = True,
) -> AShareScanResult:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    selected_boards = normalize_ashare_boards(boards)
    fetch_limit = max_symbols if set(selected_boards) == set(ASHARE_BOARD_LABELS) else max(1000, max_symbols * 4)
    universe, universe_source, market_cap_filter_applied = load_ashare_universe_for_scan(min_market_cap_100m, fetch_limit)
    universe = filter_ashare_universe_by_board(universe, selected_boards)[: max(1, int(max_symbols))]
    by_symbol = {item.symbol: item for item in universe}
    candidates: list[AShareSignalSnapshot] = []
    errors: list[tuple[str, str]] = []

    def run_one(item: AShareUniverseItem) -> AShareSignalSnapshot:
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

    workers = max(1, min(max_workers, len(universe)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_one, item): item.symbol for item in universe}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                snapshot = future.result()
                if snapshot.signal:
                    candidates.append(snapshot)
            except Exception as exc:
                item = by_symbol.get(symbol)
                label = f"{symbol} {item.name}" if item else symbol
                errors.append((label, str(exc)))

    rating_order = {"Strong": 0, "Medium": 1, "Watch": 2, "None": 3}
    candidates.sort(key=lambda row: (rating_order.get(row.candidate_rating, 9), -row.volume_score, row.j_value))
    return AShareScanResult(
        total=len(universe),
        scanned=len(universe),
        candidates=candidates,
        errors=errors,
        universe_source=universe_source,
        market_cap_filter_applied=market_cap_filter_applied,
    )
