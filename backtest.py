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
) -> tuple[list[bool], list[bool], list[float | None], list[float | None], list[float], list[str]]:
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma = rolling_sma(closes, ma_length)
    vol_ma = rolling_sma(volumes, vol_length)
    ma50 = rolling_sma(closes, 50)

    is_vol_high = [
        vol_ma[i] is not None and bars[i].volume > vol_ma[i] for i in range(len(bars))
    ]
    is_massive_vol = [
        vol_ma[i] is not None and bars[i].volume >= vol_ma[i] * vol_multiplier
        for i in range(len(bars))
    ]
    massive_counts = rolling_sum([1 if x else 0 for x in is_massive_vol], 7)

    buy_signal = []
    buy_target_pct = []
    buy_stage = []
    trend_confirmed = False
    for i, bar in enumerate(bars):
        was_trend_confirmed = trend_confirmed
        ma5_is_rising = i > 0 and ma[i] is not None and ma[i - 1] is not None and ma[i] > ma[i - 1]
        vol_3_days_high = i >= 2 and is_vol_high[i] and is_vol_high[i - 1] and is_vol_high[i - 2]
        has_massive_vol = massive_counts[i] is not None and 1 <= massive_counts[i] <= 2
        price_above_ma = ma[i] is not None and bar.close > ma[i]
        dist_to_ma = abs(bar.close - ma[i]) / ma[i] if ma[i] else 0.0

        recent_high_60 = max(closes[max(0, i - 59) : i + 1])
        ma50_rising = ma50[i] is not None and i >= 5 and ma50[i - 5] is not None and ma50[i] > ma50[i - 5]
        above_ma50 = ma50[i] is not None and bar.close > ma50[i]
        near_stage_high = recent_high_60 > 0 and bar.close >= recent_high_60 * 0.80
        bull_quality_ok = above_ma50 and ma50_rising and near_stage_high

        trend_confirm = price_above_ma and vol_3_days_high and has_massive_vol and ma5_is_rising
        if trend_confirm:
            trend_confirmed = True

        full_range = bar.high - bar.low
        body = abs(bar.close - bar.open)
        upper_shadow = bar.high - max(bar.open, bar.close)
        close_position = (bar.close - bar.low) / full_range if full_range > 0 else 0.5
        upper_shadow_ok = upper_shadow <= max(body * 0.75, full_range * 0.20)
        reentry_buy = (
            was_trend_confirmed
            and is_massive_vol[i]
            and price_above_ma
            and dist_to_ma <= reentry_pct
            and bar.close > bar.open
            and ma5_is_rising
            and bull_quality_ok
            and upper_shadow_ok
        )
        failed_trend = ma[i] is not None and bar.close < ma[i] * 0.925
        if failed_trend:
            trend_confirmed = False

        target = 0.0
        stage = ""
        if trend_confirm:
            target = 50.0
            stage = "B1"
        if reentry_buy:
            target = max(target, 100.0)
            stage = "B2" if target == 100.0 else stage

        buy_signal.append(target > 0)
        buy_target_pct.append(target)
        buy_stage.append(stage)

    return buy_signal, is_massive_vol, ma, vol_ma, buy_target_pct, buy_stage


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
        buy_signal, _, ma, vol_ma, buy_target_pct, buy_stage = build_ratchet_inputs(
            bars, ma_length, vol_length, vol_multiplier, reentry_pct / 100
        )
        sell_signal = [False] * len(bars)
        closes = [bar.close for bar in bars]
        ma20 = rolling_sma(closes, 20)
    else:
        buy_signal, sell_signal, ma, vol_ma = build_signals(
            bars, ma_length, vol_length, vol_multiplier
        )
        ma20 = [None] * len(bars)
        buy_target_pct = [100.0 if signal else 0.0 for signal in buy_signal]
        buy_stage = ["B" if signal else "" for signal in buy_signal]

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
    pending_target_pct = 0.0
    pending_stage = ""
    max_high_since_entry = 0.0
    min_low_since_entry = 0.0
    highest_close_since_entry = 0.0
    below_20ma_days = 0
    trades = []
    equity_curve = []

    for i, bar in enumerate(bars):
        buy_action = ""
        buy_action_signal_date = ""
        buy_action_target_pct = 0.0
        buy_action_stage = ""
        sell_action = ""
        if pending_action == "buy":
            fill_price = apply_buy_price(bar.open, slippage_pct)
            cost_per_share = fill_price * (1 + commission_pct / 100)
            old_shares = shares
            current_equity_at_fill = cash + shares * fill_price
            target_value = current_equity_at_fill * max(0.0, min(100.0, pending_target_pct)) / 100
            current_position_value = shares * fill_price
            shares_to_buy = math.floor(max(0.0, target_value - current_position_value) / cost_per_share)
            if shares_to_buy > 0:
                buy_action = f"买到{pending_target_pct:.0f}%" if old_shares == 0 else f"加到{pending_target_pct:.0f}%"
                buy_action_signal_date = pending_signal_date
                buy_action_target_pct = pending_target_pct
                buy_action_stage = pending_stage
                old_position_cost = entry_price * old_shares
                shares = old_shares + shares_to_buy
                cash -= shares_to_buy * cost_per_share
                if old_shares > 0:
                    entry_price = (old_position_cost + fill_price * shares_to_buy) / shares
                    max_high_since_entry = max(max_high_since_entry, bar.high)
                    min_low_since_entry = min(min_low_since_entry, bar.low)
                    highest_close_since_entry = max(highest_close_since_entry, bar.close)
                else:
                    entry_price = fill_price
                    entry_date = bar.date
                    entry_signal_date = pending_signal_date
                    entry_signal_close = pending_signal_close
                    entry_index = i
                    max_high_since_entry = bar.high
                    min_low_since_entry = bar.low
                    highest_close_since_entry = bar.close
                    below_20ma_days = 0

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
            sell_action = "卖出"
            shares = 0
            entry_price = 0.0
            entry_date = ""
            entry_signal_date = ""
            entry_signal_close = 0.0
            max_high_since_entry = 0.0
            min_low_since_entry = 0.0
            highest_close_since_entry = 0.0
            below_20ma_days = 0

        pending_action = None
        pending_signal_date = ""
        pending_signal_close = 0.0
        pending_exit_reason = ""
        pending_target_pct = 0.0
        pending_stage = ""

        dynamic_stop = ""
        trend_stop_line = ""
        defense_warning = False
        ratchet_sell_today = False
        exit_reason_today = ""
        if strategy_name == "ratchet" and shares > 0:
            max_high_since_entry = max(max_high_since_entry, bar.high)
            min_low_since_entry = min(min_low_since_entry, bar.low)
            highest_close_since_entry = max(highest_close_since_entry, bar.close)
            dynamic_stop = entry_price * (1 - hard_stop_pct / 100) if entry_price else ""
            trend_stop_line = "" if ma20[i] is None else ma20[i]
            defense_warning = ma[i] is not None and bar.close < ma[i] * (1 - stop_5ma_pct / 100)
            if ma20[i] is not None and bar.close < ma20[i]:
                below_20ma_days += 1
            else:
                below_20ma_days = 0
            trend_stop = below_20ma_days >= 2
            cost_stop = entry_price > 0 and bar.close < entry_price * (1 - hard_stop_pct / 100)

            stop_reasons = []
            if defense_warning:
                stop_reasons.append("5MA 7.5% stop")
            if trend_stop:
                stop_reasons.append("2-day 20MA break")
            if cost_stop:
                stop_reasons.append("Cost 20% forced stop")
            ratchet_sell_today = bool(stop_reasons)
            exit_reason_today = " + ".join(stop_reasons)
            sell_signal[i] = ratchet_sell_today
        elif shares > 0:
            max_high_since_entry = max(max_high_since_entry, bar.high)
            min_low_since_entry = min(min_low_since_entry, bar.low)
            highest_close_since_entry = max(highest_close_since_entry, bar.close)

        market_value = shares * bar.close
        equity = cash + market_value
        equity_curve.append(
            {
                "date": bar.date,
                "close": bar.close,
                "ma": "" if ma[i] is None else ma[i],
                "ma20": "" if ma20[i] is None else ma20[i],
                "vol_ma": "" if vol_ma[i] is None else vol_ma[i],
                "buy_signal": int(buy_signal[i]),
                "buy_target_pct": buy_target_pct[i],
                "buy_stage": buy_stage[i],
                "buy_action": buy_action,
                "buy_action_signal_date": buy_action_signal_date,
                "buy_action_target_pct": buy_action_target_pct,
                "buy_action_stage": buy_action_stage,
                "sell_action": sell_action,
                "sell_signal": int(sell_signal[i]),
                "defense_warning": int(defense_warning),
                "below_20ma_days": below_20ma_days if shares > 0 else "",
                "position_shares": shares,
                "entry_date": entry_date,
                "entry_price": entry_price,
                "entry_signal_date": entry_signal_date,
                "entry_signal_close": entry_signal_close,
                "entry_index": entry_index if shares > 0 else "",
                "max_high_since_entry": max_high_since_entry if shares > 0 else "",
                "min_low_since_entry": min_low_since_entry if shares > 0 else "",
                "highest_close_since_entry": highest_close_since_entry if shares > 0 else "",
                "cash": cash,
                "equity": equity,
                "dynamic_stop": dynamic_stop,
                "trend_stop": trend_stop_line,
            }
        )

        if i < len(bars) - 1:
            current_position_pct = (shares * bar.close / equity * 100) if equity else 0.0
            target_pct_today = buy_target_pct[i]
            if strategy_name == "ratchet" and shares == 0 and buy_stage[i] == "B2":
                target_pct_today = min(target_pct_today, 50.0)
            if buy_signal[i] and target_pct_today > current_position_pct + 1:
                pending_action = "buy"
                pending_signal_date = bar.date
                pending_signal_close = bar.close
                pending_target_pct = target_pct_today
                pending_stage = buy_stage[i]
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


def open_position_snapshot(equity_curve: list[dict[str, float | str]]) -> dict[str, float | int | str] | None:
    if not equity_curve:
        return None
    row = equity_curve[-1]
    shares = int(float(row.get("position_shares", 0) or 0))
    entry_price = float(row.get("entry_price", 0) or 0)
    if shares <= 0 or entry_price <= 0:
        return None
    close = float(row.get("close", 0) or 0)
    entry_index = int(float(row.get("entry_index", 0) or 0))
    bars_held = max(0, len(equity_curve) - 1 - entry_index)
    max_high = float(row.get("max_high_since_entry", close) or close)
    min_low = float(row.get("min_low_since_entry", close) or close)
    pnl = (close - entry_price) * shares
    pnl_pct = (close / entry_price - 1) * 100 if entry_price else 0.0
    return {
        "entry_signal_date": str(row.get("entry_signal_date", "")),
        "entry_date": str(row.get("entry_date", "")),
        "entry_signal_close": float(row.get("entry_signal_close", 0) or 0),
        "entry_price": entry_price,
        "entry_gap_pct": (entry_price / float(row.get("entry_signal_close", 0) or 0) - 1) * 100 if float(row.get("entry_signal_close", 0) or 0) else 0.0,
        "shares": shares,
        "mark_date": str(row.get("date", "")),
        "mark_price": close,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "bars_held": bars_held,
        "max_favorable_pct": (max_high / entry_price - 1) * 100 if entry_price else 0.0,
        "max_drawdown_pct": (1 - min_low / entry_price) * 100 if entry_price else 0.0,
    }


def svg_polyline(points: list[tuple[float, float]], color: str, width: int = 2) -> str:
    if not points:
        return ""
    point_text = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return (
        f'<polyline points="{point_text}" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round" />'
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def signal_rating(score: float) -> str:
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Medium"
    return "Weak"


def score_signal_strength(
    bars: list[Bar],
    equity_curve: list[dict[str, float | str]],
    index: int,
    signal_type: str,
) -> dict[str, float | str]:
    bar = bars[index]
    row = equity_curve[index]
    ma_raw = row.get("ma", "")
    vol_ma_raw = row.get("vol_ma", "")
    ma = 0.0 if ma_raw in ("", None) else float(ma_raw)
    vol_ma = 0.0 if vol_ma_raw in ("", None) else float(vol_ma_raw)
    volume_ratio = bar.volume / vol_ma if vol_ma else 0.0
    dist_ma_pct = (bar.close / ma - 1) * 100 if ma else 0.0
    prev_ma_raw = equity_curve[index - 1].get("ma", "") if index > 0 else ""
    prev_ma = 0.0 if prev_ma_raw in ("", None) else float(prev_ma_raw)
    ma_rising = bool(ma and prev_ma and ma > prev_ma)
    ma_falling = bool(ma and prev_ma and ma < prev_ma)

    full_range = bar.high - bar.low
    body = abs(bar.close - bar.open)
    close_position = ((bar.close - bar.low) / full_range * 100) if full_range else 50.0
    upper_shadow = bar.high - max(bar.open, bar.close)
    upper_shadow_ratio = upper_shadow / body if body else 0.0
    lower_shadow = min(bar.open, bar.close) - bar.low
    lower_shadow_ratio = lower_shadow / body if body else 0.0
    lookback = bars[max(0, index - 251) : index + 1]
    high_52w = max((item.high for item in lookback), default=bar.high)
    dist_52w_high_pct = (bar.close / high_52w - 1) * 100 if high_52w else 0.0
    dynamic_stop_raw = row.get("dynamic_stop", "")
    dynamic_stop = 0.0 if dynamic_stop_raw in ("", None) else float(dynamic_stop_raw)
    notes: list[str] = []

    if str(signal_type).startswith("B"):
        if volume_ratio >= 1.45:
            volume_score = 18 + min(7, (volume_ratio - 1.45) * 6)
        elif volume_ratio >= 1:
            volume_score = 10 + (volume_ratio - 1) * 16
        else:
            volume_score = volume_ratio * 8
            notes.append("量能低于均量")
        if volume_ratio > 4:
            volume_score = min(volume_score, 18)
            notes.append("量能过热")
        volume_score = clamp(volume_score, 0, 25)

        trend_score = 0.0
        if ma and bar.close > ma:
            trend_score += 7
        else:
            notes.append("未站稳均线")
        if ma_rising:
            trend_score += 8
        else:
            notes.append("均线斜率不足")
        if 0 <= dist_ma_pct <= 4.5:
            trend_score += 5
        elif 4.5 < dist_ma_pct <= 8:
            trend_score += 3
            notes.append("距离均线偏高")
        elif dist_ma_pct > 8:
            notes.append("短线乖离过大")
        trend_score = clamp(trend_score, 0, 20)

        candle_score = 0.0
        if bar.close > bar.open:
            candle_score += 6
        else:
            notes.append("非阳线")
        candle_score += clamp((close_position - 45) / 55 * 8, 0, 8)
        if upper_shadow_ratio <= 0.5:
            candle_score += 6
        elif upper_shadow_ratio <= 1:
            candle_score += 3
            notes.append("上影线偏长")
        else:
            notes.append("上影线较重")
        candle_score = clamp(candle_score, 0, 20)

        if dist_52w_high_pct >= -2:
            space_score = 20
        elif dist_52w_high_pct >= -5:
            space_score = 16
        elif dist_52w_high_pct >= -10:
            space_score = 12
        elif dist_52w_high_pct >= -20:
            space_score = 8
            notes.append("距离52周高点较远")
        else:
            space_score = 4
            notes.append("上方空间压力可能较重")

        if 0 <= dist_ma_pct <= 4.5:
            risk_score = 15
        elif 4.5 < dist_ma_pct <= 8:
            risk_score = 11
        elif 8 < dist_ma_pct <= 12:
            risk_score = 7
            notes.append("入场防守距离偏大")
        elif dist_ma_pct < 0:
            risk_score = 4
        else:
            risk_score = 3
            notes.append("入场追高风险高")
    else:
        volume_score = clamp(volume_ratio / 2 * 25, 0, 25)
        trend_score = 0.0
        if ma and bar.close < ma:
            trend_score += 10
        if ma_falling:
            trend_score += 5
        trend_score += clamp(abs(min(dist_ma_pct, 0)) / 8 * 5, 0, 5)
        candle_score = 0.0
        if bar.close < bar.open:
            candle_score += 8
        candle_score += clamp((55 - close_position) / 55 * 8, 0, 8)
        if lower_shadow_ratio <= 0.5:
            candle_score += 4
        else:
            notes.append("下影线显示承接")
        space_score = clamp(abs(min(dist_ma_pct, 0)) / 12 * 20, 0, 20)
        risk_score = 0.0
        if dynamic_stop and bar.close < dynamic_stop:
            risk_score += 8
        if ma and bar.close < ma:
            risk_score += 7
        risk_score = clamp(risk_score, 0, 15)
        if trend_score < 10:
            notes.append("破位趋势不强")

    total = volume_score + trend_score + candle_score + space_score + risk_score
    return {
        "signal_score": round(total, 1),
        "signal_rating": signal_rating(total),
        "volume_score": round(volume_score, 1),
        "trend_score": round(trend_score, 1),
        "candle_score": round(candle_score, 1),
        "space_score": round(space_score, 1),
        "risk_score": round(risk_score, 1),
        "score_notes": "；".join(notes[:4]) if notes else "技术结构完整",
    }


def build_signal_detail_rows(
    bars: list[Bar],
    trades: list[Trade],
    equity_curve: list[dict[str, float | str]],
) -> list[dict[str, float | int | str]]:
    entry_signal_dates = {trade.entry_signal_date for trade in trades}
    exit_signal_dates = {trade.exit_signal_date for trade in trades}
    open_position = open_position_snapshot(equity_curve)
    if open_position and open_position.get("entry_signal_date"):
        entry_signal_dates.add(str(open_position["entry_signal_date"]))
    rows: list[dict[str, float | int | str]] = []

    for i, row in enumerate(equity_curve):
        bar = bars[i]
        signals: list[tuple[str, bool]] = []
        if int(row.get("buy_signal", 0)):
            signals.append(("B", bar.date in entry_signal_dates))
        if int(row.get("sell_signal", 0)):
            signals.append(("S", bar.date in exit_signal_dates))
        if not signals:
            continue

        ma = row.get("ma", "")
        vol_ma = row.get("vol_ma", "")
        ma_value = 0.0 if ma in ("", None) else float(ma)
        vol_ma_value = 0.0 if vol_ma in ("", None) else float(vol_ma)
        future = bars[i + 1 : min(len(bars), i + 21)]

        def future_return(days: int) -> float | str:
            target = i + days
            if target >= len(bars) or not bar.close:
                return ""
            return (bars[target].close / bar.close - 1) * 100

        max_up: float | str = ""
        max_down: float | str = ""
        if future and bar.close:
            max_up = (max(item.high for item in future) / bar.close - 1) * 100
            max_down = (min(item.low for item in future) / bar.close - 1) * 100

        next_sell = ""
        for j in range(i + 1, min(len(equity_curve), i + 21)):
            if int(equity_curve[j].get("sell_signal", 0)):
                next_sell = str(equity_curve[j].get("date", ""))
                break

        position = float(row.get("position_shares", 0) or 0)
        for signal_type, executed in signals:
            strength = score_signal_strength(bars, equity_curve, i, signal_type)
            rows.append(
                {
                    "date": bar.date,
                    "signal_type": signal_type,
                    "status": "交易信号" if executed else "持仓中信号" if position > 0 else "观察信号",
                    "executed": int(executed),
                    **strength,
                    "close": bar.close,
                    "ma": ma_value if ma_value else "",
                    "dist_ma_pct": (bar.close / ma_value - 1) * 100 if ma_value else "",
                    "volume_ratio": bar.volume / vol_ma_value if vol_ma_value else "",
                    "in_position": int(position > 0),
                    "ret_1d": future_return(1),
                    "ret_3d": future_return(3),
                    "ret_5d": future_return(5),
                    "ret_10d": future_return(10),
                    "ret_20d": future_return(20),
                    "max_up_20d": max_up,
                    "max_down_20d": max_down,
                    "next_sell_signal": next_sell,
                }
            )
    return rows



def build_signal_detail_rows(
    bars: list[Bar],
    trades: list[Trade],
    equity_curve: list[dict[str, float | str]],
) -> list[dict[str, float | int | str]]:
    exit_signal_dates = {trade.exit_signal_date for trade in trades}
    rows: list[dict[str, float | int | str]] = []

    for i, row in enumerate(equity_curve):
        bar = bars[i]
        signals: list[tuple[str, bool]] = []
        if int(row.get("buy_signal", 0)):
            next_row = equity_curve[i + 1] if i + 1 < len(equity_curve) else {}
            executed_buy = str(next_row.get("buy_action_signal_date", "")) == bar.date
            signals.append((str(row.get("buy_stage", "B") or "B"), executed_buy))
        if int(row.get("sell_signal", 0)):
            signals.append(("S", bar.date in exit_signal_dates))
        if not signals:
            continue

        ma = row.get("ma", "")
        vol_ma = row.get("vol_ma", "")
        ma_value = 0.0 if ma in ("", None) else float(ma)
        vol_ma_value = 0.0 if vol_ma in ("", None) else float(vol_ma)
        future = bars[i + 1 : min(len(bars), i + 21)]

        def future_return(days: int) -> float | str:
            target = i + days
            if target >= len(bars) or not bar.close:
                return ""
            return (bars[target].close / bar.close - 1) * 100

        max_up: float | str = ""
        max_down: float | str = ""
        if future and bar.close:
            max_up = (max(item.high for item in future) / bar.close - 1) * 100
            max_down = (min(item.low for item in future) / bar.close - 1) * 100

        next_sell = ""
        for j in range(i + 1, min(len(equity_curve), i + 21)):
            if int(equity_curve[j].get("sell_signal", 0)):
                next_sell = str(equity_curve[j].get("date", ""))
                break

        position = float(row.get("position_shares", 0) or 0)
        for signal_type, executed in signals:
            status = "交易信号" if executed else "持仓中信号" if position > 0 else "观察信号"
            action = ""
            action_reason = ""
            if str(signal_type).startswith("B"):
                next_row = equity_curve[i + 1] if i + 1 < len(equity_curve) else {}
                if executed:
                    action = str(next_row.get("buy_action", "买入"))
                    action_reason = "信号后下一交易日开盘执行"
                else:
                    target = float(row.get("buy_target_pct", 0) or 0)
                    equity = float(row.get("equity", 0) or 0)
                    position_pct = (position * bar.close / equity * 100) if equity else 0.0
                    action = "未执行"
                    if i + 1 >= len(equity_curve):
                        action_reason = "区间最后一日，缺少下一交易日开盘价"
                    elif position_pct + 1 >= target:
                        action_reason = f"当前仓位约{position_pct:.0f}%，已达到{signal_type}目标仓位{target:.0f}%"
                    else:
                        action_reason = "信号存在，但未生成有效买入数量"
            else:
                action = "卖出" if executed else "未执行"
                action_reason = "信号后下一交易日开盘执行" if executed else "区间结束或无持仓"

            strength = score_signal_strength(bars, equity_curve, i, signal_type)
            rows.append(
                {
                    "date": bar.date,
                    "signal_type": signal_type,
                    "status": status,
                    "executed": int(executed),
                    "action": action,
                    "action_reason": action_reason,
                    **strength,
                    "close": bar.close,
                    "ma": ma_value if ma_value else "",
                    "dist_ma_pct": (bar.close / ma_value - 1) * 100 if ma_value else "",
                    "volume_ratio": bar.volume / vol_ma_value if vol_ma_value else "",
                    "in_position": int(position > 0),
                    "ret_1d": future_return(1),
                    "ret_3d": future_return(3),
                    "ret_5d": future_return(5),
                    "ret_10d": future_return(10),
                    "ret_20d": future_return(20),
                    "max_up_20d": max_up,
                    "max_down_20d": max_down,
                    "next_sell_signal": next_sell,
                }
            )
    return rows




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
        "dynamic_stop": "\u6210\u672c20%\u6b62\u635f",
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
    ma20_values = [None if row.get("ma20", "") in ("", None) else float(row["ma20"]) for row in equity_curve]
    vol_ma_values = [None if row["vol_ma"] == "" else float(row["vol_ma"]) for row in equity_curve]
    dynamic_stop_values = [None if row.get("dynamic_stop", "") in ("", None) else float(row["dynamic_stop"]) for row in equity_curve]
    trend_stop_values = [None if row.get("trend_stop", "") in ("", None) else float(row["trend_stop"]) for row in equity_curve]
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

    buy_signal_markers = [
        {
            "time": bars[i].date,
            "position": "belowBar",
            "color": "#2563eb" if str(row.get("buy_stage", "")) == "B2" else "#84cc16",
            "shape": "circle",
            "text": str(row.get("buy_stage", "B") or "B"),
        }
        for i, row in enumerate(equity_curve)
        if int(row.get("buy_signal", 0))
    ]
    buy_action_markers = [
        {
            "time": str(row.get("date", "")),
            "position": "belowBar",
            "color": "#089981",
            "shape": "arrowUp",
            "text": "买",
        }
        for row in equity_curve
        if str(row.get("buy_action", ""))
    ]

    chart_payload = {
        "labels": labels,
        "dates": dates,
        "ohlc": [{"time": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": [{"time": bar.date, "value": bar.volume, "color": volume_colors[i]} for i, bar in enumerate(bars)],
        "ma": [{"time": dates[i], "value": value} for i, value in enumerate(ma_values) if value is not None],
        "ma20": [{"time": dates[i], "value": value} for i, value in enumerate(ma20_values) if value is not None],
        "volMa": [{"time": dates[i], "value": value} for i, value in enumerate(vol_ma_values) if value is not None],
        "dynamicStop": [{"time": dates[i], "value": value} for i, value in enumerate(dynamic_stop_values) if value is not None],
        "trendStop": [{"time": dates[i], "value": value} for i, value in enumerate(trend_stop_values) if value is not None],
        "rows": [
            {
                "time": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "ma": ma_values[i],
                "ma20": ma20_values[i],
                "volMa": vol_ma_values[i],
                "dynamicStop": dynamic_stop_values[i],
                "trendStop": trend_stop_values[i],
            }
            for i, bar in enumerate(bars)
        ],
        "entryMarkers": buy_action_markers,
        "exitMarkers": [
            {"time": trade.exit_date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": "卖"}
            for trade in trades
        ],
        "holdBuyMarkers": buy_signal_markers,
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
        buy_hold_symbol = html.escape(str(benchmark.get("buy_hold_symbol", "Buy & Hold")))
        buy_hold_return = float(benchmark.get("buy_hold_return_pct", 0.0))
        strategy_curve = [[row["date"], float(row["equity"])] for row in equity_curve]
        benchmark_curve = [[day, value] for day, value in benchmark["curve"]]
        buy_hold_curve = [[day, value] for day, value in benchmark.get("buy_hold_curve", [])]
        benchmark_payload = {
            "strategy": strategy_curve,
            "benchmark": benchmark_curve,
            "benchmarkSymbol": benchmark_symbol,
            "buyHold": buy_hold_curve,
            "buyHoldSymbol": buy_hold_symbol,
        }
        benchmark_html = f"""
<h2>{labels['strategy_vs']} {buy_hold_symbol} / {benchmark_symbol}</h2>
<section class="panel">
  <div class="compare-note">{labels['strategy_return']} {summary['return_pct']:.2f}% / {buy_hold_symbol} 买入持有 {buy_hold_return:.2f}% / {benchmark_symbol} {labels['benchmark_return']} {benchmark_return:.2f}%</div>
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
    open_position = open_position_snapshot(equity_curve)
    if open_position:
        row_class = "pos" if float(open_position["pnl"]) >= 0 else "neg"
        trade_rows += (
            "<tr>"
            f"<td>{len(trades) + 1}</td>"
            f"<td>{html.escape(str(open_position['entry_signal_date']))}</td>"
            f"<td>{html.escape(str(open_position['entry_date']))}</td>"
            f"<td>{float(open_position['entry_signal_close']):.2f}</td>"
            f"<td>{float(open_position['entry_price']):.2f}</td>"
            f"<td>{float(open_position['entry_gap_pct']):.2f}%</td>"
            "<td>未触发</td>"
            f"<td>未平仓</td>"
            f"<td>{float(open_position['mark_price']):.2f}</td>"
            f"<td>{float(open_position['mark_price']):.2f}</td>"
            "<td>-</td>"
            f"<td>{int(open_position['bars_held'])}</td>"
            f"<td>{int(open_position['shares'])}</td>"
            f"<td>{float(open_position['entry_price']) * int(open_position['shares']):.2f}</td>"
            f"<td>{float(open_position['mark_price']) * int(open_position['shares']):.2f}</td>"
            f"<td class=\"{row_class}\">{float(open_position['pnl']):.2f}</td>"
            f"<td class=\"{row_class}\">{float(open_position['pnl_pct']):.2f}%</td>"
            f"<td>{float(open_position['max_favorable_pct']):.2f}%</td>"
            f"<td>{float(open_position['max_drawdown_pct']):.2f}%</td>"
            f"<td>未平仓，按 {html.escape(str(open_position['mark_date']))} 收盘价估值</td>"
            "</tr>"
        )
    if not trade_rows:
        trade_rows = f'<tr><td colspan="20" class="empty">{labels["empty_trades"]}</td></tr>'

    def fmt_pct(value: float | int | str) -> str:
        if value == "" or value is None:
            return "-"
        number = float(value)
        cls = "pos" if number >= 0 else "neg"
        return f'<span class="{cls}">{number:.2f}%</span>'

    def fmt_num(value: float | int | str) -> str:
        if value == "" or value is None:
            return "-"
        return f"{float(value):.2f}"

    def score_badge(row: dict[str, float | int | str]) -> str:
        rating = html.escape(str(row["signal_rating"]))
        score = fmt_num(row["signal_score"])
        return f'<span class="score-badge score-{rating}">{score}</span>'

    def rating_badge(row: dict[str, float | int | str]) -> str:
        rating = html.escape(str(row["signal_rating"]))
        return f'<span class="rating rating-{rating}">{rating}</span>'

    signal_rows_data = build_signal_detail_rows(bars, trades, equity_curve)
    signal_rows = "\n".join(
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
        for i, row in enumerate(signal_rows_data, 1)
    )
    if not signal_rows:
        signal_rows = '<tr><td colspan="26" class="empty">这个区间没有信号。</td></tr>'

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
.compare-note {{ font-size: 13px; color: #475569; margin: 0 0 8px; }}
.rule-strip {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
.rule-pill {{ display: inline-flex; align-items: center; min-height: 28px; border: 1px solid #d6dbe3; background: #f8fafc; color: #334155; border-radius: 4px; padding: 0 9px; font-size: 12px; font-weight: 700; }}
.table-wrap {{ width: 100%; overflow: auto; border: 1px solid #d6dbe3; border-radius: 6px; background: #fff; }}
table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: #fff; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eef1f5; text-align: right; font-size: 12px; white-space: nowrap; }}
th {{ position: sticky; top: 0; background: #f5f7fa; color: #5d6675; font-size: 11px; font-weight: 800; text-transform: uppercase; z-index: 1; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3), th:nth-child(7), td:nth-child(7), th:nth-child(8), td:nth-child(8), th:last-child, td:last-child {{ text-align: left; }}
.rating, .score-badge {{ display: inline-flex; align-items: center; justify-content: center; border-radius: 4px; padding: 3px 7px; font-weight: 900; font-size: 12px; }}
.rating {{ min-width: 64px; }}
.score-badge {{ min-width: 46px; }}
.rating-Strong, .score-Strong {{ color: #067a6b; background: rgba(8,153,129,.12); }}
.rating-Medium, .score-Medium {{ color: #b26b00; background: rgba(245,158,11,.16); }}
.rating-Weak, .score-Weak {{ color: #c22736; background: rgba(242,54,69,.13); }}
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
<div class="rule-strip">
  <span class="rule-pill">B1：站上5MA + 连续3日放量 + 7日内1-2次巨量 + 5MA向上</span>
  <span class="rule-pill">B2：已有B1趋势后，巨量阳线回踩5MA {html.escape(str('4.5%'))} 内</span>
  <span class="rule-pill">买入：B1 到 50%；B2 到 100%；信号后下一交易日开盘执行</span>
  <span class="rule-pill">卖出：5MA 下 7.5% / 连续 2 日跌破 20MA / 跌破成本 20%</span>
</div>
<section class="panel">
  <div class="chart-shell">
    <div class="chart-toolbar"><button id="fit-chart">{labels['reset_view']}</button><span>{labels['chart_hint']}</span></div>
    <div id="price-chart" class="tv-chart"></div>
    <div id="price-tooltip" class="chart-tooltip"></div>
  </div>
</section>
</main>
<script type="application/json" id="chart-data">{json.dumps(chart_payload, ensure_ascii=False)}</script>
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
const ma20Series = priceChart.addLineSeries({{ color: '#94a3b8', lineWidth: 1, title: '20MA', priceLineVisible: false, lastValueVisible: false }});
ma20Series.setData(chartData.ma20 || []);
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
    `<div>${{chartLabels.volume}} ${{formatVolume(row.volume)}} &nbsp; 5MA ${{formatNumber(row.ma)}} &nbsp; 20MA ${{formatNumber(row.ma20)}}</div>`;
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

const benchmarkEl = document.getElementById('benchmark-data');
if (benchmarkEl) {{
  const b = JSON.parse(benchmarkEl.textContent);
  Plotly.newPlot('compare-chart', [
    {{ type: 'scatter', mode: 'lines', name: 'Strategy', x: b.strategy.map(p => p[0]), y: b.strategy.map(p => p[1]), line: {{ color: '#2962ff', width: 2 }} }},
    {{ type: 'scatter', mode: 'lines', name: b.buyHoldSymbol || 'Buy & Hold', x: (b.buyHold || []).map(p => p[0]), y: (b.buyHold || []).map(p => p[1]), line: {{ color: '#089981', width: 2 }} }},
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
