from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ASHARE_ROUTE = "/ashare"
ROOT = Path(__file__).resolve().parent
ASHARE_CACHE_DIR = Path(os.environ.get("MA5_DATA_DIR", ROOT / "data")).expanduser().resolve() / "ashare"
ASHARE_SECTOR_CACHE_PATH = ASHARE_CACHE_DIR / "sector_map.json"
ASHARE_SECTOR_CACHE_SECONDS = 7 * 24 * 60 * 60


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


def ashare_chart_payload(symbol: str, j_threshold: float = 14.0) -> dict[str, object]:
    clean = normalize_ashare_symbol(symbol)
    bars, source = fetch_ashare_bars(clean)
    points = ashare_indicator_series(bars, j_threshold)
    name, sector = fetch_ashare_profile(clean)
    return {
        "symbol": clean,
        "name": name,
        "sector": sector,
        "source": source,
        "ohlc": [{"x": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": [
            {
                "x": bar.date,
                "y": bar.volume,
                "color": "#ef4444" if i > 0 and bar.close >= bars[i - 1].close else "#06b6d4",
            }
            for i, bar in enumerate(bars)
        ],
        "zx_short_trend": [{"x": point["time"], "y": point["zx_short_trend"]} for point in points if point["zx_short_trend"] is not None],
        "zx_multi_trend": [{"x": point["time"], "y": point["zx_multi_trend"]} for point in points if point["zx_multi_trend"] is not None],
        "k": [{"x": point["time"], "y": point["k"]} for point in points if point["k"] is not None],
        "d": [{"x": point["time"], "y": point["d"]} for point in points if point["d"] is not None],
        "j": [{"x": point["time"], "y": point["j"]} for point in points if point["j"] is not None],
        "signals": [
            {"x": bar.date, "y": bar.low, "text": "B"}
            for bar, point in zip(bars, points)
            if point["signal"]
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


def fetch_ashare_bars(symbol: str, start: str | None = None, end: str | None = None) -> tuple[list[AShareBar], str]:
    clean = normalize_ashare_symbol(symbol)
    end_day = date.today() if end is None else date.fromisoformat(end)
    start_day = end_day - timedelta(days=520) if start is None else date.fromisoformat(start)
    start_ak = start_day.strftime("%Y%m%d")
    end_ak = end_day.strftime("%Y%m%d")

    try:
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
        import akshare as ak

        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"].astype(str) == clean]
        if not row.empty:
            return str(row.iloc[0].get("名称", ""))
    except Exception:
        pass
    try:
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


def fetch_ashare_profile(symbol: str) -> tuple[str, str]:
    clean = normalize_ashare_symbol(symbol)
    name = fetch_ashare_name(clean)
    sector_map = load_ashare_sector_map()
    return name, sector_map.get(clean, "")


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


def load_ashare_universe(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> list[AShareUniverseItem]:
    items, _, _ = load_ashare_universe_with_meta(min_market_cap_100m, max_symbols)
    return items


def load_ashare_universe_with_meta(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> tuple[list[AShareUniverseItem], str, bool]:
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        source = "akshare.stock_zh_a_spot_em"
        has_market_cap = True
    except Exception as exc:
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot()
            source = f"akshare.stock_zh_a_spot fallback：总市值接口失败，{exc}"
            has_market_cap = False
        except Exception as fallback_exc:
            raise RuntimeError(f"A 股股票池拉取失败：{fallback_exc}") from fallback_exc

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
    return items[: max(1, int(max_symbols))], source, has_market_cap


def load_ashare_universe_for_scan(min_market_cap_100m: float = 50.0, max_symbols: int = 300) -> tuple[list[AShareUniverseItem], str, bool]:
    try:
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        source = "akshare.stock_zh_a_spot_em"
        has_market_cap = True
    except Exception as exc:
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot()
            source = f"akshare.stock_zh_a_spot fallback：总市值接口失败，{exc}"
            has_market_cap = False
        except Exception as fallback_exc:
            raise RuntimeError(f"A 股股票池拉取失败：{fallback_exc}") from fallback_exc

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
    return items[: max(1, int(max_symbols))], source, has_market_cap


def latest_ashare_signal(symbol: str, j_threshold: float = 14.0, fetch_name_value: bool = True) -> AShareSignalSnapshot:
    clean = normalize_ashare_symbol(symbol)
    bars, source = fetch_ashare_bars(clean)
    if len(bars) < 130:
        raise ValueError(f"数据不足：至少需要 130 根日 K，当前 {len(bars)} 根。")

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]

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

    i = len(bars) - 1
    if zx_multi[i] is None or zx_multi[i - 1] is None or j[i] is None:
        raise ValueError("指标数据不足。")

    trend_ok = zx_short[i] > zx_multi[i] and zx_multi[i] > zx_multi[i - 1]
    j_oversold = j[i] < j_threshold

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
    volume_score += 0.5 if red_volumes and len(red_volumes) > len(green_volumes) else 0.0
    volume_score = round(min(5.0, volume_score), 2)
    hard_candidate = bool(trend_ok and j_oversold)
    if hard_candidate and volume_score >= 4.0:
        candidate_rating = "Strong"
    elif hard_candidate and volume_score >= 2.5:
        candidate_rating = "Medium"
    elif hard_candidate:
        candidate_rating = "Watch"
    else:
        candidate_rating = "None"

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
        zx_short_trend=zx_short[i],
        zx_multi_trend=zx_multi[i] or 0.0,
        zx_multi_slope=(zx_multi[i] or 0.0) - (zx_multi[i - 1] or 0.0),
        j_value=j[i] or 0.0,
        trend_ok=bool(trend_ok),
        j_oversold=bool(j_oversold),
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
    )


def scan_ashare_candidates(
    min_market_cap_100m: float = 50.0,
    max_symbols: int = 300,
    j_threshold: float = 14.0,
    max_workers: int = 6,
) -> AShareScanResult:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    universe, universe_source, market_cap_filter_applied = load_ashare_universe_for_scan(min_market_cap_100m, max_symbols)
    by_symbol = {item.symbol: item for item in universe}
    candidates: list[AShareSignalSnapshot] = []
    errors: list[tuple[str, str]] = []

    def run_one(item: AShareUniverseItem) -> AShareSignalSnapshot:
        snapshot = latest_ashare_signal(item.symbol, j_threshold, fetch_name_value=False)
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
