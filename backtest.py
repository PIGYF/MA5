import argparse
import csv
import html
import json
import math
import os
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


DATE_KEYS = ("date", "time", "datetime", "交易日期", "日期", "时间")
OPEN_KEYS = ("open", "开盘", "开盘价")
HIGH_KEYS = ("high", "最高", "最高价")
LOW_KEYS = ("low", "最低", "最低价")
CLOSE_KEYS = ("close", "收盘", "收盘价")
VOLUME_KEYS = ("volume", "vol", "成交量", "成交量(股)", "成交量(手)")
PRICE_CACHE_DIR = Path(os.environ.get("MA5_PRICE_CACHE_DIR", Path(os.environ.get("MA5_DATA_DIR", Path(__file__).resolve().parent / "data")) / "cache" / "prices")).expanduser().resolve()
PRICE_CACHE_MAX_BARS = 1300


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    entry_signal_date: str
    entry_date: str
    entry_signal_close: float
    entry_price: float
    entry_gap_pct: float
    shares: int
    exit_signal_date: str
    exit_date: str
    exit_signal_close: float
    exit_price: float
    exit_gap_pct: float
    pnl: float
    pnl_pct: float
    bars_held: int
    max_favorable_pct: float
    max_drawdown_pct: float
    exit_reason: str


def normalize_key(key: str) -> str:
    return key.strip().lower().replace(" ", "").replace("_", "")


def find_column(fieldnames, candidates):
    normalized = {normalize_key(name): name for name in fieldnames}
    for candidate in candidates:
        key = normalize_key(candidate)
        if key in normalized:
            return normalized[key]
    raise ValueError(f"CSV 缺少列：{', '.join(candidates)}")


def parse_float(value: str) -> float:
    if value is None:
        raise ValueError("空值不能转成数字")
    clean = value.strip().replace(",", "")
    if clean == "":
        raise ValueError("空值不能转成数字")
    return float(clean)


def read_bars(csv_path: Path) -> list[Bar]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头")

        date_col = find_column(reader.fieldnames, DATE_KEYS)
        open_col = find_column(reader.fieldnames, OPEN_KEYS)
        high_col = find_column(reader.fieldnames, HIGH_KEYS)
        low_col = find_column(reader.fieldnames, LOW_KEYS)
        close_col = find_column(reader.fieldnames, CLOSE_KEYS)
        volume_col = find_column(reader.fieldnames, VOLUME_KEYS)

        bars = []
        for row in reader:
            bars.append(
                Bar(
                    date=row[date_col].strip(),
                    open=parse_float(row[open_col]),
                    high=parse_float(row[high_col]),
                    low=parse_float(row[low_col]),
                    close=parse_float(row[close_col]),
                    volume=parse_float(row[volume_col]),
                )
            )

    if len(bars) < 2:
        raise ValueError("CSV 至少需要两行行情数据")
    return bars


def cache_symbol_name(symbol: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in symbol.upper())


def price_cache_path(symbol: str) -> Path:
    return PRICE_CACHE_DIR / f"{cache_symbol_name(symbol)}.csv"


def read_price_cache(symbol: str) -> list[Bar]:
    path = price_cache_path(symbol)
    if not path.exists():
        return []
    try:
        return read_bars(path)
    except Exception:
        return []


def write_price_cache(symbol: str, bars: list[Bar]) -> None:
    PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    merged = {bar.date: bar for bar in bars}
    ordered = [merged[key] for key in sorted(merged)]
    if len(ordered) > PRICE_CACHE_MAX_BARS:
        ordered = ordered[-PRICE_CACHE_MAX_BARS:]
    with price_cache_path(symbol).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for bar in ordered:
            writer.writerow(bar.__dict__)


def slice_bars(bars: list[Bar], start_date: str, end_date: str) -> list[Bar]:
    return [bar for bar in bars if start_date <= bar.date <= end_date]


def cache_covers_range(bars: list[Bar], start_date: str, end_date: str) -> bool:
    if not bars:
        return False
    dates = [bar.date for bar in bars]
    return min(dates) <= start_date and max(dates) >= end_date


def dataframe_to_bars(df, source: str) -> list[Bar]:
    if df.empty:
        raise ValueError("没有拉到行情数据，请检查股票代码、日期范围或网络")

    bars = []
    if source == "akshare":
        for _, row in df.iterrows():
            bars.append(
                Bar(
                    date=str(row["日期"]),
                    open=float(row["开盘"]),
                    high=float(row["最高"]),
                    low=float(row["最低"]),
                    close=float(row["收盘"]),
                    volume=float(row["成交量"]),
                )
            )
    elif source == "yfinance":
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        for _, row in df.iterrows():
            bars.append(
                Bar(
                    date=str(row[date_col])[:10],
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                )
            )
    else:
        raise ValueError(f"未知数据源: {source}")

    if len(bars) < 2:
        raise ValueError("行情数据至少需要两行")
    return bars


def fetch_bars(
    source: str,
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str,
    cache_csv: Path | None,
) -> list[Bar]:
    if source == "akshare":
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError(
                "缺少 akshare。安装命令：python -m pip install akshare -U"
            ) from exc

        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust=adjust,
        )
        if cache_csv:
            df.to_csv(cache_csv, index=False, encoding="utf-8-sig")
        return dataframe_to_bars(df, "akshare")

    if source == "yfinance":
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "缺少 yfinance。安装命令：python -m pip install yfinance -U"
            ) from exc

        cached_bars = read_price_cache(symbol) if symbol and cache_csv is None else []
        if cache_csv is None and cache_covers_range(cached_bars, start_date, end_date):
            sliced = slice_bars(cached_bars, start_date, end_date)
            if len(sliced) >= 2:
                return sliced

        download_start = start_date
        if cache_csv is None and cached_bars:
            latest_cached = max(bar.date for bar in cached_bars)
            earliest_cached = min(bar.date for bar in cached_bars)
            if earliest_cached <= start_date and latest_cached >= start_date:
                download_start = (
                    datetime.strptime(latest_cached, "%Y-%m-%d").date() - timedelta(days=7)
                ).isoformat()

        yf_end_date = (
            datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()

        df = yf.download(
            symbol,
            start=download_start,
            end=yf_end_date,
            interval="1d",
            auto_adjust=(adjust != ""),
            progress=False,
        )
        if cache_csv:
            df.to_csv(cache_csv, encoding="utf-8-sig")
        downloaded_bars = dataframe_to_bars(df, "yfinance")
        if cache_csv is None and symbol:
            write_price_cache(symbol, cached_bars + downloaded_bars)
            cached_bars = read_price_cache(symbol)
            sliced = slice_bars(cached_bars, start_date, end_date)
            if len(sliced) >= 2:
                return sliced
        return downloaded_bars

    raise ValueError("source 只能是 akshare 或 yfinance")


def rolling_sma(values: list[float], length: int) -> list[float | None]:
    result = []
    window = deque()
    total = 0.0
    for value in values:
        window.append(value)
        total += value
        if len(window) > length:
            total -= window.popleft()
        result.append(total / length if len(window) == length else None)
    return result


def rolling_sum(values: list[int], length: int) -> list[int | None]:
    result = []
    window = deque()
    total = 0
    for value in values:
        window.append(value)
        total += value
        if len(window) > length:
            total -= window.popleft()
        result.append(total if len(window) == length else None)
    return result


def apply_buy_price(open_price: float, slippage_pct: float) -> float:
    return open_price * (1 + slippage_pct / 100)


def apply_sell_price(open_price: float, slippage_pct: float) -> float:
    return open_price * (1 - slippage_pct / 100)


def build_signals(
    bars: list[Bar],
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
) -> tuple[list[bool], list[bool], list[float | None], list[float | None]]:
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma = rolling_sma(closes, ma_length)
    vol_ma = rolling_sma(volumes, vol_length)

    is_vol_high = [
        vol_ma[i] is not None and bars[i].volume > vol_ma[i] for i in range(len(bars))
    ]
    is_massive_vol = [
        vol_ma[i] is not None and bars[i].volume > vol_ma[i] * vol_multiplier
        for i in range(len(bars))
    ]
    massive_counts = rolling_sum([1 if x else 0 for x in is_massive_vol], 7)

    buy_signal = []
    sell_signal = []
    for i, bar in enumerate(bars):
        vol_3_days_high = i >= 2 and is_vol_high[i] and is_vol_high[i - 1] and is_vol_high[i - 2]
        has_massive_vol = massive_counts[i] is not None and massive_counts[i] >= 1
        price_above_ma = ma[i] is not None and bar.close > ma[i]
        buy_signal.append(price_above_ma and vol_3_days_high and has_massive_vol)

        crossed_under = (
            i > 0
            and ma[i] is not None
            and ma[i - 1] is not None
            and bars[i - 1].close >= ma[i - 1]
            and bar.close < ma[i]
        )
        sell_signal.append(crossed_under)

    return buy_signal, sell_signal, ma, vol_ma


def build_ratchet_inputs(
    bars: list[Bar],
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
    reentry_pct: float,
) -> tuple[list[bool], list[bool], list[float | None], list[float | None]]:
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma = rolling_sma(closes, ma_length)
    vol_ma = rolling_sma(volumes, vol_length)

    is_vol_high = [
        vol_ma[i] is not None and bars[i].volume > vol_ma[i] for i in range(len(bars))
    ]
    is_massive_vol = [
        vol_ma[i] is not None and bars[i].volume >= vol_ma[i] * vol_multiplier
        for i in range(len(bars))
    ]
    massive_counts = rolling_sum([1 if x else 0 for x in is_massive_vol], 7)

    buy_signal = []
    for i, bar in enumerate(bars):
        ma5_is_rising = i > 0 and ma[i] is not None and ma[i - 1] is not None and ma[i] > ma[i - 1]
        vol_3_days_high = i >= 2 and is_vol_high[i] and is_vol_high[i - 1] and is_vol_high[i - 2]
        has_massive_vol = massive_counts[i] is not None and 1 <= massive_counts[i] <= 2
        price_above_ma = ma[i] is not None and bar.close > ma[i]
        initial_buy = price_above_ma and vol_3_days_high and has_massive_vol and ma5_is_rising

        dist_to_ma = abs(bar.close - ma[i]) / ma[i] if ma[i] else 0.0
        reentry_buy = (
            is_massive_vol[i]
            and price_above_ma
            and dist_to_ma <= reentry_pct
            and bar.close > bar.open
            and ma5_is_rising
        )
        buy_signal.append(initial_buy or reentry_buy)

    return buy_signal, is_massive_vol, ma, vol_ma


def backtest(
    bars: list[Bar],
    ma_length: int,
    vol_length: int,
    vol_multiplier: float,
    initial_cash: float,
    commission_pct: float,
    slippage_pct: float,
    strategy_name: str = "classic",
    stop_5ma_pct: float = 7.5,
    hard_stop_pct: float = 20.0,
    reentry_pct: float = 4.5,
) -> tuple[list[Trade], list[dict[str, float | str]]]:
    if strategy_name == "ratchet":
        buy_signal, _, ma, vol_ma = build_ratchet_inputs(
            bars, ma_length, vol_length, vol_multiplier, reentry_pct / 100
        )
        sell_signal = [False] * len(bars)
    else:
        buy_signal, sell_signal, ma, vol_ma = build_signals(
            bars, ma_length, vol_length, vol_multiplier
        )

    cash = initial_cash
    shares = 0
    entry_price = 0.0
    entry_date = ""
    entry_signal_date = ""
    entry_signal_close = 0.0
    entry_index = 0
    pending_action = None
    pending_signal_date = ""
    pending_signal_close = 0.0
    pending_exit_reason = ""
    highest_b_price = None
    max_high_since_entry = 0.0
    min_low_since_entry = 0.0
    trades = []
    equity_curve = []

    for i, bar in enumerate(bars):
        if pending_action == "buy" and shares == 0:
            fill_price = apply_buy_price(bar.open, slippage_pct)
            cost_per_share = fill_price * (1 + commission_pct / 100)
            shares_to_buy = math.floor(cash / cost_per_share)
            if shares_to_buy > 0:
                shares = shares_to_buy
                cash -= shares * cost_per_share
                entry_price = fill_price
                entry_date = bar.date
                entry_signal_date = pending_signal_date
                entry_signal_close = pending_signal_close
                entry_index = i
                max_high_since_entry = bar.high
                min_low_since_entry = bar.low
                if strategy_name == "ratchet" and highest_b_price is None:
                    highest_b_price = entry_price

        elif pending_action == "sell" and shares > 0:
            fill_price = apply_sell_price(bar.open, slippage_pct)
            gross = shares * fill_price
            fee = gross * commission_pct / 100
            cash += gross - fee
            pnl = (fill_price - entry_price) * shares - fee - (entry_price * shares * commission_pct / 100)
            pnl_pct = (fill_price / entry_price - 1) * 100 if entry_price else 0.0
            max_favorable_pct = (max_high_since_entry / entry_price - 1) * 100 if entry_price else 0.0
            max_drawdown_pct = (1 - min_low_since_entry / entry_price) * 100 if entry_price else 0.0
            trades.append(
                Trade(
                    entry_signal_date=entry_signal_date,
                    entry_date=entry_date,
                    entry_signal_close=entry_signal_close,
                    entry_price=entry_price,
                    entry_gap_pct=(entry_price / entry_signal_close - 1) * 100 if entry_signal_close else 0.0,
                    shares=shares,
                    exit_signal_date=pending_signal_date,
                    exit_date=bar.date,
                    exit_signal_close=pending_signal_close,
                    exit_price=fill_price,
                    exit_gap_pct=(fill_price / pending_signal_close - 1) * 100 if pending_signal_close else 0.0,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    bars_held=i - entry_index,
                    max_favorable_pct=max_favorable_pct,
                    max_drawdown_pct=max_drawdown_pct,
                    exit_reason=pending_exit_reason or "Signal exit",
                )
            )
            shares = 0
            entry_price = 0.0
            entry_date = ""
            entry_signal_date = ""
            entry_signal_close = 0.0
            highest_b_price = None
            max_high_since_entry = 0.0
            min_low_since_entry = 0.0

        pending_action = None
        pending_signal_date = ""
        pending_signal_close = 0.0
        pending_exit_reason = ""

        dynamic_stop = ""
        ratchet_sell_today = False
        exit_reason_today = ""
        if strategy_name == "ratchet" and shares > 0:
            max_high_since_entry = max(max_high_since_entry, bar.high)
            min_low_since_entry = min(min_low_since_entry, bar.low)
            if buy_signal[i] and (highest_b_price is None or bar.close > highest_b_price):
                highest_b_price = bar.close
            dynamic_stop = "" if highest_b_price is None else highest_b_price * (1 - hard_stop_pct / 100)
            stop_condition_1 = ma[i] is not None and bar.close < ma[i] * (1 - stop_5ma_pct / 100)
            stop_condition_2 = (
                highest_b_price is not None
                and bar.close < highest_b_price * (1 - hard_stop_pct / 100)
            )
            ratchet_sell_today = stop_condition_1 or stop_condition_2
            if stop_condition_1 and stop_condition_2:
                exit_reason_today = "MA defense + ratchet stop"
            elif stop_condition_1:
                exit_reason_today = "MA defense"
            elif stop_condition_2:
                exit_reason_today = "Ratchet stop"
            sell_signal[i] = ratchet_sell_today
        elif shares > 0:
            max_high_since_entry = max(max_high_since_entry, bar.high)
            min_low_since_entry = min(min_low_since_entry, bar.low)

        market_value = shares * bar.close
        equity = cash + market_value
        equity_curve.append(
            {
                "date": bar.date,
                "close": bar.close,
                "ma": "" if ma[i] is None else ma[i],
                "vol_ma": "" if vol_ma[i] is None else vol_ma[i],
                "buy_signal": int(buy_signal[i]),
                "sell_signal": int(sell_signal[i]),
                "position_shares": shares,
                "cash": cash,
                "equity": equity,
                "dynamic_stop": dynamic_stop,
            }
        )

        if i < len(bars) - 1:
            if shares == 0 and buy_signal[i]:
                if strategy_name == "ratchet":
                    highest_b_price = bar.close
                pending_action = "buy"
                pending_signal_date = bar.date
                pending_signal_close = bar.close
            elif shares > 0:
                if strategy_name == "ratchet":
                    if ratchet_sell_today:
                        pending_action = "sell"
                        pending_signal_date = bar.date
                        pending_signal_close = bar.close
                        pending_exit_reason = exit_reason_today
                elif sell_signal[i]:
                    pending_action = "sell"
                    pending_signal_date = bar.date
                    pending_signal_close = bar.close
                    pending_exit_reason = "MA crossunder"

    return trades, equity_curve


def summarize(trades: list[Trade], equity_curve: list[dict[str, float | str]], initial_cash: float) -> dict[str, float | int]:
    final_equity = float(equity_curve[-1]["equity"])
    net_profit = final_equity - initial_cash
    returns_pct = net_profit / initial_cash * 100

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl <= 0]
    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    avg_trade = sum(trade.pnl for trade in trades) / len(trades) if trades else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    best_trade = max([trade.pnl for trade in trades], default=0.0)
    worst_trade = min([trade.pnl for trade in trades], default=0.0)
    avg_bars_held = sum(trade.bars_held for trade in trades) / len(trades) if trades else 0.0
    avg_max_favorable = sum(trade.max_favorable_pct for trade in trades) / len(trades) if trades else 0.0
    avg_trade_drawdown = sum(trade.max_drawdown_pct for trade in trades) / len(trades) if trades else 0.0

    peak = initial_cash
    max_drawdown = 0.0
    for row in equity_curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else 0.0,
        "final_equity": final_equity,
        "net_profit": net_profit,
        "return_pct": returns_pct,
        "max_drawdown_pct": max_drawdown,
        "profit_factor": gross_profit / gross_loss if gross_loss else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_trade": avg_trade,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "avg_bars_held": avg_bars_held,
        "avg_max_favorable_pct": avg_max_favorable,
        "avg_trade_drawdown_pct": avg_trade_drawdown,
    }


def write_trades(path: Path, trades: list[Trade]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "entry_signal_date",
                "entry_date",
                "entry_signal_close",
                "entry_price",
                "entry_gap_pct",
                "shares",
                "exit_signal_date",
                "exit_date",
                "exit_signal_close",
                "exit_price",
                "exit_gap_pct",
                "pnl",
                "pnl_pct",
                "bars_held",
                "max_favorable_pct",
                "max_drawdown_pct",
                "exit_reason",
            ],
        )
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade.__dict__)


def write_equity(path: Path, equity_curve: list[dict[str, float | str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(equity_curve[0].keys()))
        writer.writeheader()
        writer.writerows(equity_curve)


def svg_polyline(points: list[tuple[float, float]], color: str, width: int = 2) -> str:
    if not points:
        return ""
    point_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return (
        f'<polyline points="{point_text}" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round" />'
    )




def make_report(
    path: Path,
    title: str,
    bars: list[Bar],
    trades: list[Trade],
    equity_curve: list[dict[str, float | str]],
    summary: dict[str, float | int],
    benchmark: dict[str, object] | None = None,
) -> None:
    labels = {
        "net_profit": "\u51c0\u5229\u6da6",
        "return_pct": "\u6536\u76ca\u7387",
        "max_drawdown": "\u6700\u5927\u56de\u64a4",
        "trades": "\u4ea4\u6613\u6b21\u6570",
        "win_rate": "\u80dc\u7387",
        "profit_factor": "\u76c8\u4e8f\u56e0\u5b50",
        "overview": "\u603b\u89c8",
        "trade_analysis": "\u4ea4\u6613\u5206\u6790",
        "initial_cash": "\u521d\u59cb\u8d44\u91d1",
        "final_equity": "\u671f\u672b\u6743\u76ca",
        "gross_profit": "\u603b\u76c8\u5229",
        "gross_loss": "\u603b\u4e8f\u635f",
        "avg_trade": "\u5e73\u5747\u6bcf\u7b14",
        "avg_bars": "\u5e73\u5747\u6301\u4ed3K\u7ebf",
        "wins": "\u76c8\u5229\u7b14\u6570",
        "losses": "\u4e8f\u635f\u7b14\u6570",
        "avg_win": "\u5e73\u5747\u76c8\u5229",
        "avg_loss": "\u5e73\u5747\u4e8f\u635f",
        "best_trade": "\u6700\u4f73\u4ea4\u6613",
        "worst_trade": "\u6700\u5dee\u4ea4\u6613",
        "avg_mfe": "\u5e73\u5747\u6700\u5927\u6d6e\u76c8",
        "avg_dd": "\u5e73\u5747\u4ea4\u6613\u56de\u64a4",
        "strategy_vs": "\u7b56\u7565 vs",
        "strategy_return": "\u7b56\u7565\u533a\u95f4\u6536\u76ca",
        "benchmark_return": "\u533a\u95f4\u6536\u76ca",
        "main_chart": "K\u7ebf\u3001\u5747\u7ebf\u3001\u6210\u4ea4\u91cf\u4e0e\u4ea4\u6613\u70b9",
        "pnl_chart": "\u5355\u7b14\u4ea4\u6613\u6536\u76ca",
        "trade_detail": "\u4ea4\u6613\u660e\u7ec6",
        "buy_signal_date": "\u4e70\u5165\u4fe1\u53f7\u65e5",
        "buy_action_date": "\u4e70\u5165\u64cd\u4f5c\u65e5",
        "buy_signal_close": "\u4e70\u5165\u4fe1\u53f7\u6536\u76d8",
        "buy_fill": "\u4e70\u5165\u6210\u4ea4",
        "buy_gap": "\u4e70\u5165\u8df3\u7a7a",
        "sell_signal_date": "\u5356\u51fa\u4fe1\u53f7\u65e5",
        "sell_action_date": "\u5356\u51fa\u64cd\u4f5c\u65e5",
        "sell_signal_close": "\u5356\u51fa\u4fe1\u53f7\u6536\u76d8",
        "sell_fill": "\u5356\u51fa\u6210\u4ea4",
        "sell_gap": "\u5356\u51fa\u8df3\u7a7a",
        "bars_held": "\u6301\u4ed3K\u7ebf",
        "shares": "\u80a1\u6570",
        "entry_value": "\u4e70\u5165\u91d1\u989d",
        "exit_value": "\u5356\u51fa\u91d1\u989d",
        "pnl_amount": "\u6536\u76ca\u91d1\u989d",
        "max_favorable": "\u6700\u5927\u6d6e\u76c8",
        "exit_reason": "\u5356\u51fa\u539f\u56e0",
        "empty_trades": "\u8fd9\u4e2a\u533a\u95f4\u6ca1\u6709\u5b8c\u6210\u4ea4\u6613\u3002",
        "date": "\u65e5\u671f",
        "open": "\u5f00",
        "high": "\u9ad8",
        "low": "\u4f4e",
        "close": "\u6536",
        "volume": "\u6210\u4ea4\u91cf",
        "volume_ma": "\u6210\u4ea4\u91cf\u5747\u7ebf",
        "dynamic_stop": "\u52a8\u6001\u6b62\u635f",
        "buy": "\u4e70\u5165",
        "sell": "\u5356\u51fa",
        "hold_buy": "\u6301\u4ed3B\u70b9",
        "hold_sell": "\u6301\u4ed3\u5356\u70b9",
        "trade_no": "\u4ea4\u6613\u5e8f\u53f7",
        "equity": "\u6743\u76ca",
        "reset_view": "\u91cd\u7f6e\u89c6\u56fe",
        "chart_hint": "\u6eda\u8f6e\u7f29\u653e\uff0c\u6309\u4f4f\u62d6\u52a8\uff0c\u60ac\u505c\u67e5\u770bOHLC\u548c\u6210\u4ea4\u91cf\u3002",
    }

    dates = [bar.date for bar in bars]
    ma_values = [None if row["ma"] == "" else float(row["ma"]) for row in equity_curve]
    vol_ma_values = [None if row["vol_ma"] == "" else float(row["vol_ma"]) for row in equity_curve]
    dynamic_stop_values = [None if row.get("dynamic_stop", "") in ("", None) else float(row["dynamic_stop"]) for row in equity_curve]
    volume_colors = ["rgba(8,153,129,0.42)" if bar.close >= bar.open else "rgba(242,54,69,0.42)" for bar in bars]

    trade_by_entry = {trade.entry_date: trade for trade in trades}
    trade_by_exit = {trade.exit_date: trade for trade in trades}
    entry_text = [
        f"{labels['buy_action_date']}: {trade.entry_date}<br>{labels['buy_signal_date']}: {trade.entry_signal_date}<br>{labels['buy_fill']}: {trade.entry_price:.2f}<br>{labels['buy_signal_close']}: {trade.entry_signal_close:.2f}<br>{labels['buy_gap']}: {trade.entry_gap_pct:.2f}%"
        for trade in trades
    ]
    exit_text = [
        f"{labels['sell_action_date']}: {trade.exit_date}<br>{labels['sell_signal_date']}: {trade.exit_signal_date}<br>{labels['sell_fill']}: {trade.exit_price:.2f}<br>{labels['sell_signal_close']}: {trade.exit_signal_close:.2f}<br>{labels['pnl_amount']}: {trade.pnl:.2f}<br>{labels['return_pct']}: {trade.pnl_pct:.2f}%<br>{labels['exit_reason']}: {html.escape(trade.exit_reason)}"
        for trade in trades
    ]

    hold_buy_x: list[str] = []
    hold_buy_y: list[float] = []
    hold_buy_text: list[str] = []
    hold_sell_x: list[str] = []
    hold_sell_y: list[float] = []
    hold_sell_text: list[str] = []
    for i, row in enumerate(equity_curve):
        position = float(row.get("position_shares", 0) or 0)
        if position <= 0:
            continue
        bar = bars[i]
        if int(row.get("buy_signal", 0)) and bar.date not in trade_by_entry:
            hold_buy_x.append(bar.date)
            hold_buy_y.append(bar.low)
            hold_buy_text.append(f"{labels['hold_buy']}<br>{labels['date']}: {bar.date}<br>{labels['close']}: {bar.close:.2f}")
        if int(row.get("sell_signal", 0)) and bar.date not in trade_by_exit:
            hold_sell_x.append(bar.date)
            hold_sell_y.append(bar.high)
            hold_sell_text.append(f"{labels['hold_sell']}<br>{labels['date']}: {bar.date}<br>{labels['close']}: {bar.close:.2f}")

    chart_payload = {
        "labels": labels,
        "dates": dates,
        "ohlc": [{"time": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": [{"time": bar.date, "value": bar.volume, "color": volume_colors[i]} for i, bar in enumerate(bars)],
        "ma": [{"time": dates[i], "value": value} for i, value in enumerate(ma_values) if value is not None],
        "volMa": [{"time": dates[i], "value": value} for i, value in enumerate(vol_ma_values) if value is not None],
        "dynamicStop": [{"time": dates[i], "value": value} for i, value in enumerate(dynamic_stop_values) if value is not None],
        "rows": [
            {
                "time": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "ma": ma_values[i],
                "volMa": vol_ma_values[i],
                "dynamicStop": dynamic_stop_values[i],
            }
            for i, bar in enumerate(bars)
        ],
        "entryMarkers": [
            {"time": trade.entry_date, "position": "belowBar", "color": "#089981", "shape": "arrowUp", "text": labels["buy"]}
            for trade in trades
        ],
        "exitMarkers": [
            {"time": trade.exit_date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": labels["sell"]}
            for trade in trades
        ],
        "holdBuyMarkers": [
            {"time": day, "position": "belowBar", "color": "#84cc16", "shape": "circle", "text": "B"}
            for day in hold_buy_x
        ],
        "holdSellMarkers": [
            {"time": day, "position": "aboveBar", "color": "#f97316", "shape": "circle", "text": "S"}
            for day in hold_sell_x
        ],
        "entryText": entry_text,
        "exitText": exit_text,
        "holdBuyText": hold_buy_text,
        "holdSellText": hold_sell_text,
    }

    benchmark_html = ""
    if benchmark and benchmark.get("curve"):
        benchmark_symbol = html.escape(str(benchmark.get("symbol", "Benchmark")))
        benchmark_return = float(benchmark.get("return_pct", 0.0))
        strategy_curve = [[row["date"], float(row["equity"])] for row in equity_curve]
        benchmark_curve = [[day, value] for day, value in benchmark["curve"]]
        benchmark_payload = {
            "strategy": strategy_curve,
            "benchmark": benchmark_curve,
            "benchmarkSymbol": benchmark_symbol,
        }
        benchmark_html = f"""
<h2>{labels['strategy_vs']} {benchmark_symbol}</h2>
<section class="panel">
  <div class="compare-note">{labels['strategy_return']} {summary['return_pct']:.2f}% / {benchmark_symbol} {labels['benchmark_return']} {benchmark_return:.2f}%</div>
  <div id="compare-chart" class="chart compare-chart"></div>
</section>
<script type="application/json" id="benchmark-data">{json.dumps(benchmark_payload, ensure_ascii=False)}</script>
"""

    cards = [
        (labels["net_profit"], f"{summary['net_profit']:.2f}"),
        (labels["return_pct"], f"{summary['return_pct']:.2f}%"),
        (labels["max_drawdown"], f"{summary['max_drawdown_pct']:.2f}%"),
        (labels["trades"], f"{summary['trades']}"),
        (labels["win_rate"], f"{summary['win_rate_pct']:.2f}%"),
        (labels["profit_factor"], f"{summary['profit_factor']:.2f}"),
    ]
    card_html = "\n".join(
        f'<section class="metric"><span>{html.escape(label)}</span><strong>{value}</strong></section>'
        for label, value in cards
    )

    initial_cash_value = summary["final_equity"] - summary["net_profit"]
    overview_rows = [
        (labels["initial_cash"], f"{initial_cash_value:.2f}"),
        (labels["final_equity"], f"{summary['final_equity']:.2f}"),
        (labels["gross_profit"], f"{summary['gross_profit']:.2f}"),
        (labels["gross_loss"], f"-{summary['gross_loss']:.2f}"),
        (labels["avg_trade"], f"{summary['avg_trade']:.2f}"),
        (labels["avg_bars"], f"{summary['avg_bars_held']:.1f}"),
    ]
    analysis_rows = [
        (labels["wins"], f"{summary['wins']}"),
        (labels["losses"], f"{summary['losses']}"),
        (labels["avg_win"], f"{summary['avg_win']:.2f}"),
        (labels["avg_loss"], f"{summary['avg_loss']:.2f}"),
        (labels["best_trade"], f"{summary['best_trade']:.2f}"),
        (labels["worst_trade"], f"{summary['worst_trade']:.2f}"),
        (labels["avg_mfe"], f"{summary['avg_max_favorable_pct']:.2f}%"),
        (labels["avg_dd"], f"{summary['avg_trade_drawdown_pct']:.2f}%"),
    ]
    stat_tables = "".join(f"<tr><td>{html.escape(label)}</td><td>{value}</td></tr>" for label, value in overview_rows)
    analysis_tables = "".join(f"<tr><td>{html.escape(label)}</td><td>{value}</td></tr>" for label, value in analysis_rows)

    trade_rows = "\n".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td>{html.escape(trade.entry_signal_date)}</td>"
        f"<td>{html.escape(trade.entry_date)}</td>"
        f"<td>{trade.entry_signal_close:.2f}</td>"
        f"<td>{trade.entry_price:.2f}</td>"
        f"<td>{trade.entry_gap_pct:.2f}%</td>"
        f"<td>{html.escape(trade.exit_signal_date)}</td>"
        f"<td>{html.escape(trade.exit_date)}</td>"
        f"<td>{trade.exit_signal_close:.2f}</td>"
        f"<td>{trade.exit_price:.2f}</td>"
        f"<td>{trade.exit_gap_pct:.2f}%</td>"
        f"<td>{trade.bars_held}</td>"
        f"<td>{trade.shares}</td>"
        f"<td>{trade.entry_price * trade.shares:.2f}</td>"
        f"<td>{trade.exit_price * trade.shares:.2f}</td>"
        f"<td class=\"{'pos' if trade.pnl >= 0 else 'neg'}\">{trade.pnl:.2f}</td>"
        f"<td class=\"{'pos' if trade.pnl_pct >= 0 else 'neg'}\">{trade.pnl_pct:.2f}%</td>"
        f"<td>{trade.max_favorable_pct:.2f}%</td>"
        f"<td>{trade.max_drawdown_pct:.2f}%</td>"
        f"<td>{html.escape(trade.exit_reason)}</td>"
        "</tr>"
        for i, trade in enumerate(trades, 1)
    )
    if not trade_rows:
        trade_rows = f'<tr><td colspan="20" class="empty">{labels["empty_trades"]}</td></tr>'

    pnl_payload = {
        "labels": [str(i) for i in range(1, len(trades) + 1)],
        "pnl": [trade.pnl for trade in trades],
        "text": [
            f"#{i}<br>{trade.entry_date} -> {trade.exit_date}<br>{labels['pnl_amount']}: {trade.pnl:.2f}<br>{labels['return_pct']}: {trade.pnl_pct:.2f}%"
            for i, trade in enumerate(trades, 1)
        ],
    }

    report = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{html.escape(title)}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f0f3f7; color: #131722; font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", Arial, sans-serif; }}
main {{ max-width: 1360px; margin: 0 auto; padding: 22px; }}
h1 {{ margin: 0 0 16px; font-size: 24px; font-weight: 800; }}
h2 {{ margin: 24px 0 10px; font-size: 17px; }}
.metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-bottom: 14px; }}
.metric {{ background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 10px 12px; }}
.metric span {{ display: block; color: #6b7280; font-size: 11px; font-weight: 800; text-transform: uppercase; margin-bottom: 6px; }}
.metric strong {{ font-size: 18px; }}
.tester {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 14px; }}
.tester table {{ margin: 0; }}
.tester caption {{ text-align: left; padding: 10px 12px; font-weight: 800; background: #f5f7fa; border-bottom: 1px solid #eef1f5; }}
.panel {{ position: relative; background: #fff; border: 1px solid #d6dbe3; border-radius: 6px; padding: 10px; margin-bottom: 14px; overflow: hidden; }}
.chart-shell {{ position: relative; height: 660px; min-width: 760px; }}
.chart-toolbar {{ position: absolute; top: 12px; left: 12px; z-index: 5; display: flex; gap: 8px; align-items: center; }}
.chart-toolbar button {{ border: 1px solid #d6dbe3; background: rgba(255,255,255,0.92); color: #131722; border-radius: 4px; height: 28px; padding: 0 10px; font-weight: 700; cursor: pointer; }}
.chart-toolbar span {{ color: #64748b; font-size: 12px; background: rgba(255,255,255,0.86); padding: 5px 8px; border-radius: 4px; }}
.tv-chart {{ width: 100%; height: 100%; }}
.chart-tooltip {{ position: absolute; z-index: 6; display: none; min-width: 220px; pointer-events: none; border: 1px solid #d6dbe3; background: rgba(255,255,255,0.96); border-radius: 6px; box-shadow: 0 8px 22px rgba(15,23,42,0.12); padding: 8px 10px; font-size: 12px; line-height: 1.6; }}
.chart-tooltip strong {{ display: block; margin-bottom: 4px; font-size: 13px; }}
.chart-tooltip .up {{ color: #089981; }}
.chart-tooltip .down {{ color: #f23645; }}
.chart {{ width: 100%; min-height: 360px; }}
.compare-chart {{ min-height: 360px; }}
.pnl-chart {{ min-height: 320px; }}
.compare-note {{ font-size: 13px; color: #475569; margin: 0 0 8px; }}
.table-wrap {{ width: 100%; overflow: auto; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; }}
table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #fff; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eef1f5; text-align: right; font-size: 12px; white-space: nowrap; }}
th {{ position: sticky; top: 0; background: #f5f7fa; color: #5d6675; font-size: 11px; font-weight: 800; text-transform: uppercase; z-index: 1; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3), th:nth-child(7), td:nth-child(7), th:nth-child(8), td:nth-child(8), th:last-child, td:last-child {{ text-align: left; }}
.pos {{ color: #089981; }}
.neg {{ color: #f23645; }}
.empty {{ text-align: center; color: #607080; }}
@media (max-width: 900px) {{ main {{ padding: 12px; }} .metrics {{ grid-template-columns: repeat(2, 1fr); }} .tester {{ grid-template-columns: 1fr; }} .chart-shell {{ min-width: 0; height: 560px; }} .chart-toolbar span {{ display: none; }} }}
</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<div class="metrics">{card_html}</div>
<section class="tester">
<table><caption>{labels['overview']}</caption><tbody>{stat_tables}</tbody></table>
<table><caption>{labels['trade_analysis']}</caption><tbody>{analysis_tables}</tbody></table>
</section>
{benchmark_html}
<h2>{labels['main_chart']}</h2>
<section class="panel">
  <div class="chart-shell">
    <div class="chart-toolbar"><button id="fit-chart">{labels['reset_view']}</button><span>{labels['chart_hint']}</span></div>
    <div id="price-chart" class="tv-chart"></div>
    <div id="price-tooltip" class="chart-tooltip"></div>
  </div>
</section>
<h2>{labels['pnl_chart']}</h2>
<section class="panel"><div id="pnl-chart" class="chart pnl-chart"></div></section>
<h2>{labels['trade_detail']}</h2>
<div class="table-wrap">
<table>
<thead><tr><th>#</th><th>{labels['buy_signal_date']}</th><th>{labels['buy_action_date']}</th><th>{labels['buy_signal_close']}</th><th>{labels['buy_fill']}</th><th>{labels['buy_gap']}</th><th>{labels['sell_signal_date']}</th><th>{labels['sell_action_date']}</th><th>{labels['sell_signal_close']}</th><th>{labels['sell_fill']}</th><th>{labels['sell_gap']}</th><th>{labels['bars_held']}</th><th>{labels['shares']}</th><th>{labels['entry_value']}</th><th>{labels['exit_value']}</th><th>{labels['pnl_amount']}</th><th>{labels['return_pct']}</th><th>{labels['max_favorable']}</th><th>{labels['max_drawdown']}</th><th>{labels['exit_reason']}</th></tr></thead>
<tbody>{trade_rows}</tbody>
</table>
</div>
</main>
<script type="application/json" id="chart-data">{json.dumps(chart_payload, ensure_ascii=False)}</script>
<script type="application/json" id="pnl-data">{json.dumps(pnl_payload, ensure_ascii=False)}</script>
<script>
const chartData = JSON.parse(document.getElementById('chart-data').textContent);
const chartLabels = chartData.labels;
const chartElement = document.getElementById('price-chart');
const tooltip = document.getElementById('price-tooltip');
const priceChart = LightweightCharts.createChart(chartElement, {{
  layout: {{ background: {{ type: 'solid', color: '#ffffff' }}, textColor: '#131722', fontFamily: 'Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif' }},
  width: chartElement.clientWidth,
  height: chartElement.clientHeight,
  rightPriceScale: {{ borderColor: '#d6dbe3', scaleMargins: {{ top: 0.08, bottom: 0.28 }} }},
  timeScale: {{ borderColor: '#d6dbe3', timeVisible: false, secondsVisible: false, rightOffset: 6, barSpacing: 8, minBarSpacing: 3, fixLeftEdge: false, fixRightEdge: false }},
  grid: {{ vertLines: {{ color: '#f1f3f6' }}, horzLines: {{ color: '#f1f3f6' }} }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal, vertLine: {{ color: '#9ca3af', width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' }}, horzLine: {{ color: '#9ca3af', width: 1, style: LightweightCharts.LineStyle.Dashed, labelBackgroundColor: '#2962ff' }} }},
  handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
  handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
}});
const candleSeries = priceChart.addCandlestickSeries({{
  upColor: '#089981', downColor: '#f23645', borderUpColor: '#089981', borderDownColor: '#f23645', wickUpColor: '#089981', wickDownColor: '#f23645', priceLineVisible: false,
}});
candleSeries.setData(chartData.ohlc);
candleSeries.priceScale().applyOptions({{ scaleMargins: {{ top: 0.08, bottom: 0.28 }} }});
const maSeries = priceChart.addLineSeries({{ color: '#f5a623', lineWidth: 2, title: '5MA', priceLineVisible: false }});
maSeries.setData(chartData.ma);
const stopSeries = priceChart.addLineSeries({{ color: '#f97316', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: chartLabels.dynamic_stop, priceLineVisible: false }});
stopSeries.setData(chartData.dynamicStop);
const volumeSeries = priceChart.addHistogramSeries({{ color: 'rgba(41,98,255,0.25)', priceFormat: {{ type: 'volume' }}, priceScaleId: '', priceLineVisible: false, lastValueVisible: false }});
volumeSeries.setData(chartData.volume);
priceChart.priceScale('').applyOptions({{ scaleMargins: {{ top: 0.78, bottom: 0 }} }});
const volMaSeries = priceChart.addLineSeries({{ color: '#2962ff', lineWidth: 1, priceScaleId: '', title: chartLabels.volume_ma, priceLineVisible: false, lastValueVisible: false }});
volMaSeries.setData(chartData.volMa);
const markerData = [...chartData.entryMarkers, ...chartData.exitMarkers, ...chartData.holdBuyMarkers, ...chartData.holdSellMarkers].sort((a, b) => a.time.localeCompare(b.time));
candleSeries.setMarkers(markerData);
const rowByTime = new Map(chartData.rows.map(row => [row.time, row]));
function formatNumber(value, digits = 2) {{ return value === null || value === undefined || Number.isNaN(value) ? '-' : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: digits, minimumFractionDigits: digits }}); }}
function formatVolume(value) {{ return value === null || value === undefined ? '-' : Number(value).toLocaleString(undefined, {{ maximumFractionDigits: 0 }}); }}
priceChart.subscribeCrosshairMove(param => {{
  if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0 || param.point.x > chartElement.clientWidth || param.point.y > chartElement.clientHeight) {{ tooltip.style.display = 'none'; return; }}
  const row = rowByTime.get(param.time);
  if (!row) {{ tooltip.style.display = 'none'; return; }}
  const up = row.close >= row.open;
  tooltip.innerHTML = `<strong>${{row.time}}</strong>` +
    `<div><span class="${{up ? 'up' : 'down'}}">${{chartLabels.open}} ${{formatNumber(row.open)}} &nbsp; ${{chartLabels.high}} ${{formatNumber(row.high)}} &nbsp; ${{chartLabels.low}} ${{formatNumber(row.low)}} &nbsp; ${{chartLabels.close}} ${{formatNumber(row.close)}}</span></div>` +
    `<div>${{chartLabels.volume}} ${{formatVolume(row.volume)}} &nbsp; 5MA ${{formatNumber(row.ma)}} &nbsp; ${{chartLabels.dynamic_stop}} ${{formatNumber(row.dynamicStop)}}</div>`;
  tooltip.style.display = 'block';
  const left = Math.min(param.point.x + 16, chartElement.clientWidth - 250);
  const top = Math.max(44, param.point.y - 72);
  tooltip.style.left = `${{left}}px`;
  tooltip.style.top = `${{top}}px`;
}});
new ResizeObserver(entries => {{
  const rect = entries[0].contentRect;
  priceChart.applyOptions({{ width: Math.floor(rect.width), height: Math.floor(rect.height) }});
}}).observe(chartElement);
document.getElementById('fit-chart').addEventListener('click', () => priceChart.timeScale().fitContent());
priceChart.timeScale().fitContent();

const chartConfig = {{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] }};
const pnlData = JSON.parse(document.getElementById('pnl-data').textContent);
Plotly.newPlot('pnl-chart', [{{ type: 'bar', x: pnlData.labels, y: pnlData.pnl, text: pnlData.text, marker: {{ color: pnlData.pnl.map(v => v >= 0 ? '#089981' : '#f23645') }}, hovertemplate: '%{{text}}<extra></extra>' }}], {{ margin: {{ l: 64, r: 28, t: 20, b: 48 }}, paper_bgcolor: '#fff', plot_bgcolor: '#fff', xaxis: {{ title: chartLabels.trade_no }}, yaxis: {{ title: chartLabels.pnl_amount, zeroline: true, zerolinecolor: '#9ca3af', gridcolor: '#eef1f5' }}, font: {{ family: 'Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif', size: 12, color: '#131722' }} }}, chartConfig);

const benchmarkEl = document.getElementById('benchmark-data');
if (benchmarkEl) {{
  const b = JSON.parse(benchmarkEl.textContent);
  Plotly.newPlot('compare-chart', [
    {{ type: 'scatter', mode: 'lines', name: 'Strategy', x: b.strategy.map(p => p[0]), y: b.strategy.map(p => p[1]), line: {{ color: '#2962ff', width: 2 }} }},
    {{ type: 'scatter', mode: 'lines', name: b.benchmarkSymbol, x: b.benchmark.map(p => p[0]), y: b.benchmark.map(p => p[1]), line: {{ color: '#7c3aed', width: 2 }} }},
  ], {{ margin: {{ l: 64, r: 28, t: 20, b: 44 }}, hovermode: 'x unified', paper_bgcolor: '#fff', plot_bgcolor: '#fff', xaxis: {{ showgrid: true, gridcolor: '#eef1f5' }}, yaxis: {{ title: chartLabels.equity, showgrid: true, gridcolor: '#eef1f5' }}, legend: {{ orientation: 'h', x: 0, y: 1.08 }}, font: {{ family: 'Inter, Microsoft YaHei UI, PingFang SC, Arial, sans-serif', size: 12, color: '#131722' }} }}, chartConfig);
}}
</script>
</body>
</html>
"""
    path.write_text(report, encoding="utf-8")

def print_summary(summary: dict[str, float | int]) -> None:
    print("========== 回测结果 ==========")
    print(f"交易次数: {summary['trades']}")
    print(f"胜率: {summary['win_rate_pct']:.2f}%")
    print(f"期末权益: {summary['final_equity']:.2f}")
    print(f"净利润: {summary['net_profit']:.2f}")
    print(f"收益率: {summary['return_pct']:.2f}%")
    print(f"最大回撤: {summary['max_drawdown_pct']:.2f}%")
    print(f"盈亏因子: {summary['profit_factor']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="动能主升浪 / 5日线战法本地回测")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--csv", type=Path, help="行情 CSV 文件路径")
    input_group.add_argument("--symbol", help="股票代码，例如 A 股 000001，美股 AAPL，港股 0700.HK")
    parser.add_argument("--source", choices=("akshare", "yfinance"), default="akshare", help="拉取行情的数据源")
    parser.add_argument("--start", default="2018-01-01", help="开始日期，格式 YYYY-MM-DD")
    parser.add_argument("--end", default=str(date.today()), help="结束日期，格式 YYYY-MM-DD")
    parser.add_argument("--adjust", default="qfq", help="复权方式：akshare 支持 qfq/hfq/空字符串；yfinance 非空表示自动复权")
    parser.add_argument("--cache-csv", type=Path, help="把拉到的行情保存成 CSV")
    parser.add_argument("--ma-length", type=int, default=5, help="生命线周期")
    parser.add_argument("--vol-length", type=int, default=20, help="平均成交量周期")
    parser.add_argument("--vol-multiplier", type=float, default=1.45, help="巨量倍数")
    parser.add_argument("--strategy-name", choices=("classic", "ratchet"), default="classic", help="策略版本")
    parser.add_argument("--stop-5ma-pct", type=float, default=7.5, help="棘轮版：跌破均线容忍百分比")
    parser.add_argument("--hard-stop-pct", type=float, default=20.0, help="棘轮版：B 点追踪止损百分比")
    parser.add_argument("--reentry-pct", type=float, default=4.5, help="棘轮版：反抽重返距离百分比")
    parser.add_argument("--initial-cash", type=float, default=100000, help="初始资金")
    parser.add_argument("--commission-pct", type=float, default=0.03, help="单边手续费百分比")
    parser.add_argument("--slippage-pct", type=float, default=0.0, help="单边滑点百分比")
    parser.add_argument("--trades-out", type=Path, default=Path("trades.csv"), help="交易明细输出")
    parser.add_argument("--equity-out", type=Path, default=Path("equity.csv"), help="权益曲线输出")
    parser.add_argument("--report-out", type=Path, default=Path("report.html"), help="HTML 图表报告输出")
    args = parser.parse_args()

    if args.csv:
        bars = read_bars(args.csv)
    else:
        bars = fetch_bars(
            source=args.source,
            symbol=args.symbol,
            start_date=args.start,
            end_date=args.end,
            adjust=args.adjust,
            cache_csv=args.cache_csv,
        )
    trades, equity_curve = backtest(
        bars=bars,
        ma_length=args.ma_length,
        vol_length=args.vol_length,
        vol_multiplier=args.vol_multiplier,
        initial_cash=args.initial_cash,
        commission_pct=args.commission_pct,
        slippage_pct=args.slippage_pct,
        strategy_name=args.strategy_name,
        stop_5ma_pct=args.stop_5ma_pct,
        hard_stop_pct=args.hard_stop_pct,
        reentry_pct=args.reentry_pct,
    )
    summary = summarize(trades, equity_curve, args.initial_cash)
    report_title = args.symbol or args.csv.stem

    write_trades(args.trades_out, trades)
    write_equity(args.equity_out, equity_curve)
    make_report(
        path=args.report_out,
        title=f"{report_title} backtest {args.start} to {args.end}",
        bars=bars,
        trades=trades,
        equity_curve=equity_curve,
        summary=summary,
    )
    print_summary(summary)
    print(f"交易明细已保存: {args.trades_out.resolve()}")
    print(f"权益曲线已保存: {args.equity_out.resolve()}")
    print(f"HTML 报告已保存: {args.report_out.resolve()}")


if __name__ == "__main__":
    main()
