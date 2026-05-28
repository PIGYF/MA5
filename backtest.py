import argparse
import csv
import html
import math
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
    entry_price: float
    shares: int
    exit_signal_date: str
    exit_date: str
    exit_price: float
    pnl: float
    pnl_pct: float
    bars_held: int


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
        yf_end_date = (
            datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()

        df = yf.download(
            symbol,
            start=start_date,
            end=yf_end_date,
            interval="1d",
            auto_adjust=(adjust != ""),
            progress=False,
        )
        if cache_csv:
            df.to_csv(cache_csv, encoding="utf-8-sig")
        return dataframe_to_bars(df, "yfinance")

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
    entry_index = 0
    pending_action = None
    pending_signal_date = ""
    highest_b_price = None
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
                entry_index = i
                if strategy_name == "ratchet" and highest_b_price is None:
                    highest_b_price = entry_price

        elif pending_action == "sell" and shares > 0:
            fill_price = apply_sell_price(bar.open, slippage_pct)
            gross = shares * fill_price
            fee = gross * commission_pct / 100
            cash += gross - fee
            pnl = (fill_price - entry_price) * shares - fee - (entry_price * shares * commission_pct / 100)
            pnl_pct = (fill_price / entry_price - 1) * 100 if entry_price else 0.0
            trades.append(
                Trade(
                    entry_signal_date=entry_signal_date,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    shares=shares,
                    exit_signal_date=pending_signal_date,
                    exit_date=bar.date,
                    exit_price=fill_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    bars_held=i - entry_index,
                )
            )
            shares = 0
            entry_price = 0.0
            entry_date = ""
            entry_signal_date = ""
            highest_b_price = None

        pending_action = None
        pending_signal_date = ""

        dynamic_stop = ""
        ratchet_sell_today = False
        if strategy_name == "ratchet" and shares > 0:
            if buy_signal[i] and (highest_b_price is None or bar.close > highest_b_price):
                highest_b_price = bar.close
            dynamic_stop = "" if highest_b_price is None else highest_b_price * (1 - hard_stop_pct / 100)
            stop_condition_1 = ma[i] is not None and bar.close < ma[i] * (1 - stop_5ma_pct / 100)
            stop_condition_2 = (
                highest_b_price is not None
                and bar.close < highest_b_price * (1 - hard_stop_pct / 100)
            )
            ratchet_sell_today = stop_condition_1 or stop_condition_2
            sell_signal[i] = ratchet_sell_today

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
            elif shares > 0:
                if strategy_name == "ratchet":
                    if ratchet_sell_today:
                        pending_action = "sell"
                        pending_signal_date = bar.date
                elif sell_signal[i]:
                    pending_action = "sell"
                    pending_signal_date = bar.date

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
    }


def write_trades(path: Path, trades: list[Trade]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "entry_signal_date",
                "entry_date",
                "entry_price",
                "shares",
                "exit_signal_date",
                "exit_date",
                "exit_price",
                "pnl",
                "pnl_pct",
                "bars_held",
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
    width = 1180
    price_height = 520
    pnl_height = 320
    compare_height = 360
    pad_left = 72
    pad_right = 28
    pad_top = 28
    pad_bottom = 58
    plot_width = width - pad_left - pad_right
    plot_height = price_height - pad_top - pad_bottom

    closes = [bar.close for bar in bars]
    ma_values = [float(row["ma"]) for row in equity_curve if row["ma"] != ""]
    low = min(closes + ma_values)
    high = max(closes + ma_values)
    padding = (high - low) * 0.08 or 1
    low -= padding
    high += padding

    def x_at(index: int) -> float:
        if len(bars) == 1:
            return pad_left + plot_width / 2
        return pad_left + index / (len(bars) - 1) * plot_width

    def y_at(value: float) -> float:
        return pad_top + (high - value) / (high - low) * plot_height

    close_points = [(x_at(i), y_at(bar.close)) for i, bar in enumerate(bars)]
    ma_points = [
        (x_at(i), y_at(float(row["ma"])))
        for i, row in enumerate(equity_curve)
        if row["ma"] != ""
    ]
    bar_spacing = plot_width / max(1, len(bars) - 1)
    candle_width = max(2.0, min(8.0, bar_spacing * 0.55))
    candle_svg = []
    for i, bar in enumerate(bars):
        x = x_at(i)
        open_y = y_at(bar.open)
        close_y = y_at(bar.close)
        high_y = y_at(bar.high)
        low_y = y_at(bar.low)
        body_y = min(open_y, close_y)
        body_height = max(1.2, abs(close_y - open_y))
        klass = "candle-up" if bar.close >= bar.open else "candle-down"
        candle_svg.append(
            f'<g><line x1="{x:.2f}" y1="{high_y:.2f}" x2="{x:.2f}" y2="{low_y:.2f}" class="{klass}" />'
            f'<rect x="{x - candle_width / 2:.2f}" y="{body_y:.2f}" width="{candle_width:.2f}" height="{body_height:.2f}" class="{klass}" />'
            f'<title>{html.escape(bar.date)} O {bar.open:.2f} H {bar.high:.2f} L {bar.low:.2f} C {bar.close:.2f}</title></g>'
        )

    date_to_index = {bar.date: i for i, bar in enumerate(bars)}
    buy_marks = []
    sell_marks = []
    in_position_buy_signals = []
    in_position_sell_signals = []
    for trade in trades:
        if trade.entry_date in date_to_index:
            i = date_to_index[trade.entry_date]
            buy_marks.append((x_at(i), y_at(trade.entry_price), trade))
        if trade.exit_date in date_to_index:
            i = date_to_index[trade.exit_date]
            sell_marks.append((x_at(i), y_at(trade.exit_price), trade))

    trade_entry_dates = {trade.entry_date for trade in trades}
    trade_exit_dates = {trade.exit_date for trade in trades}
    for i, row in enumerate(equity_curve):
        position = float(row.get("position_shares", 0) or 0)
        if position <= 0:
            continue
        bar = bars[i]
        if int(row.get("buy_signal", 0)) and bar.date not in trade_entry_dates:
            marker_y = y_at(bar.low if hasattr(bar, "low") else bar.close) + 14
            in_position_buy_signals.append((x_at(i), marker_y, bar))
        if int(row.get("sell_signal", 0)) and bar.date not in trade_exit_dates:
            marker_y = y_at(bar.high if hasattr(bar, "high") else bar.close) - 14
            in_position_sell_signals.append((x_at(i), marker_y, bar))

    grid_lines = []
    for step in range(6):
        y = pad_top + step / 5 * plot_height
        value = high - step / 5 * (high - low)
        grid_lines.append(
            f'<line x1="{pad_left}" y1="{y:.2f}" x2="{width - pad_right}" y2="{y:.2f}" class="grid" />'
            f'<text x="16" y="{y + 4:.2f}" class="axis">{value:.2f}</text>'
        )

    date_ticks = []
    tick_count = min(8, len(bars))
    for step in range(tick_count):
        i = round(step * (len(bars) - 1) / max(1, tick_count - 1))
        x = x_at(i)
        date_ticks.append(
            f'<text x="{x:.2f}" y="{price_height - 22}" text-anchor="middle" class="axis">{html.escape(bars[i].date)}</text>'
        )

    buy_svg = "".join(
        f'<g><circle cx="{x:.2f}" cy="{y:.2f}" r="7" class="buy" />'
        f'<title>Buy {html.escape(t.entry_date)} @ {t.entry_price:.2f}</title></g>'
        for x, y, t in buy_marks
    )
    sell_svg = "".join(
        f'<g><circle cx="{x:.2f}" cy="{y:.2f}" r="7" class="sell" />'
        f'<title>Sell {html.escape(t.exit_date)} @ {t.exit_price:.2f}, PnL {t.pnl:.2f}</title></g>'
        for x, y, t in sell_marks
    )
    in_position_buy_svg = "".join(
        f'<g><circle cx="{x:.2f}" cy="{y:.2f}" r="8" class="hold-buy" />'
        f'<text x="{x:.2f}" y="{y + 4:.2f}" text-anchor="middle" class="signal-letter">B</text>'
        f'<title>Holding B signal {html.escape(bar.date)} close {bar.close:.2f}</title></g>'
        for x, y, bar in in_position_buy_signals
    )
    in_position_sell_svg = "".join(
        f'<g><circle cx="{x:.2f}" cy="{y:.2f}" r="8" class="hold-sell" />'
        f'<text x="{x:.2f}" y="{y + 4:.2f}" text-anchor="middle" class="signal-letter">S</text>'
        f'<title>Holding sell signal {html.escape(bar.date)} close {bar.close:.2f}</title></g>'
        for x, y, bar in in_position_sell_signals
    )

    max_abs_pnl = max([abs(trade.pnl) for trade in trades] + [1])
    bar_pad_left = 72
    bar_pad_right = 28
    bar_pad_top = 24
    bar_pad_bottom = 70
    bar_plot_width = width - bar_pad_left - bar_pad_right
    bar_mid = bar_pad_top + (pnl_height - bar_pad_top - bar_pad_bottom) / 2
    bar_scale = (pnl_height - bar_pad_top - bar_pad_bottom) / 2 / max_abs_pnl
    trade_gap = 6
    trade_bar_width = (
        max(10, (bar_plot_width - trade_gap * max(0, len(trades) - 1)) / max(1, len(trades)))
    )
    pnl_bars = []
    for i, trade in enumerate(trades):
        x = bar_pad_left + i * (trade_bar_width + trade_gap)
        h = abs(trade.pnl) * bar_scale
        y = bar_mid - h if trade.pnl >= 0 else bar_mid
        klass = "pnl-pos" if trade.pnl >= 0 else "pnl-neg"
        pnl_bars.append(
            f'<g><rect x="{x:.2f}" y="{y:.2f}" width="{trade_bar_width:.2f}" height="{h:.2f}" class="{klass}" />'
            f'<title>{html.escape(trade.entry_date)} -> {html.escape(trade.exit_date)}: {trade.pnl:.2f}</title>'
            f'<text x="{x + trade_bar_width / 2:.2f}" y="{pnl_height - 38}" text-anchor="middle" class="trade-label">{i + 1}</text></g>'
        )

    cards = [
        ("净利润", f"{summary['net_profit']:.2f}"),
        ("总收益率", f"{summary['return_pct']:.2f}%"),
        ("最大回撤", f"{summary['max_drawdown_pct']:.2f}%"),
        ("总交易", f"{summary['trades']}"),
        ("胜率", f"{summary['win_rate_pct']:.2f}%"),
        ("盈亏因子", f"{summary['profit_factor']:.2f}"),
    ]
    card_html = "\n".join(
        f'<section class="metric"><span>{label}</span><strong>{value}</strong></section>'
        for label, value in cards
    )
    overview_rows = [
        ("初始资金", f"{initial_cash:.2f}" if (initial_cash := summary["final_equity"] - summary["net_profit"]) else "0.00"),
        ("期末权益", f"{summary['final_equity']:.2f}"),
        ("毛利润", f"{summary['gross_profit']:.2f}"),
        ("毛亏损", f"-{summary['gross_loss']:.2f}"),
        ("平均每笔", f"{summary['avg_trade']:.2f}"),
        ("平均持仓K线", f"{summary['avg_bars_held']:.1f}"),
    ]
    analysis_rows = [
        ("盈利交易", f"{summary['wins']}"),
        ("亏损交易", f"{summary['losses']}"),
        ("平均盈利", f"{summary['avg_win']:.2f}"),
        ("平均亏损", f"{summary['avg_loss']:.2f}"),
        ("最佳交易", f"{summary['best_trade']:.2f}"),
        ("最差交易", f"{summary['worst_trade']:.2f}"),
    ]
    stat_tables = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{value}</td></tr>"
        for label, value in overview_rows
    )
    analysis_tables = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{value}</td></tr>"
        for label, value in analysis_rows
    )

    trade_rows = "\n".join(
        "<tr>"
        f"<td>{i}</td>"
        f"<td>{html.escape(trade.entry_date)}</td>"
        f"<td>{trade.entry_price:.2f}</td>"
        f"<td>{html.escape(trade.exit_date)}</td>"
        f"<td>{trade.exit_price:.2f}</td>"
        f"<td>{trade.shares}</td>"
        f"<td class=\"{'pos' if trade.pnl >= 0 else 'neg'}\">{trade.pnl:.2f}</td>"
        f"<td class=\"{'pos' if trade.pnl_pct >= 0 else 'neg'}\">{trade.pnl_pct:.2f}%</td>"
        "</tr>"
        for i, trade in enumerate(trades, 1)
    )
    if not trade_rows:
        trade_rows = '<tr><td colspan="8" class="empty">No closed trades in this range.</td></tr>'

    benchmark_html = ""
    if benchmark and benchmark.get("curve"):
        compare_pad_left = 72
        compare_pad_right = 28
        compare_pad_top = 26
        compare_pad_bottom = 54
        compare_plot_width = width - compare_pad_left - compare_pad_right
        compare_plot_height = compare_height - compare_pad_top - compare_pad_bottom
        strategy_points = [
            (row["date"], float(row["equity"]))
            for row in equity_curve
        ]
        benchmark_points = benchmark["curve"]
        all_values = [value for _, value in strategy_points] + [value for _, value in benchmark_points]
        compare_low = min(all_values)
        compare_high = max(all_values)
        compare_padding = (compare_high - compare_low) * 0.08 or 1
        compare_low -= compare_padding
        compare_high += compare_padding

        def compare_x(index: int, total: int) -> float:
            if total <= 1:
                return compare_pad_left + compare_plot_width / 2
            return compare_pad_left + index / (total - 1) * compare_plot_width

        def compare_y(value: float) -> float:
            return compare_pad_top + (compare_high - value) / (compare_high - compare_low) * compare_plot_height

        strategy_compare = [
            (compare_x(i, len(strategy_points)), compare_y(value))
            for i, (_, value) in enumerate(strategy_points)
        ]
        benchmark_compare = [
            (compare_x(i, len(benchmark_points)), compare_y(value))
            for i, (_, value) in enumerate(benchmark_points)
        ]
        compare_grid = []
        for step in range(5):
            y = compare_pad_top + step / 4 * compare_plot_height
            value = compare_high - step / 4 * (compare_high - compare_low)
            compare_grid.append(
                f'<line x1="{compare_pad_left}" y1="{y:.2f}" x2="{width - compare_pad_right}" y2="{y:.2f}" class="grid" />'
                f'<text x="16" y="{y + 4:.2f}" class="axis">{value:.0f}</text>'
            )
        benchmark_symbol = html.escape(str(benchmark.get("symbol", "Benchmark")))
        benchmark_return = float(benchmark.get("return_pct", 0.0))
        benchmark_html = f"""
<h2>策略 vs {benchmark_symbol}</h2>
<section class="panel">
<div class="compare-note">策略收益率 {summary['return_pct']:.2f}% / {benchmark_symbol} 同期收益率 {benchmark_return:.2f}%</div>
<svg viewBox="0 0 {width} {compare_height}" role="img">
{''.join(compare_grid)}
<line x1="{compare_pad_left}" y1="{compare_height - compare_pad_bottom}" x2="{width - compare_pad_right}" y2="{compare_height - compare_pad_bottom}" class="grid" />
{svg_polyline(strategy_compare, "#2563eb", 2)}
{svg_polyline(benchmark_compare, "#7c3aed", 2)}
<line x1="{width - 270}" y1="24" x2="{width - 232}" y2="24" stroke="#2563eb" stroke-width="3" /><text x="{width - 224}" y="28" class="legend">Strategy</text>
<line x1="{width - 142}" y1="24" x2="{width - 104}" y2="24" stroke="#7c3aed" stroke-width="3" /><text x="{width - 96}" y="28" class="legend">{benchmark_symbol}</text>
</svg>
</section>
"""

    report = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{html.escape(title)}</title>
<style>
body {{ margin: 0; background: #f4f6f8; color: #1f2933; font-family: Arial, "Microsoft YaHei", sans-serif; }}
main {{ max-width: 1240px; margin: 0 auto; padding: 28px; }}
h1 {{ margin: 0 0 18px; font-size: 26px; font-weight: 700; }}
h2 {{ margin: 28px 0 12px; font-size: 18px; }}
.metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 18px; }}
.metric {{ background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 12px; }}
.metric span {{ display: block; color: #607080; font-size: 12px; margin-bottom: 6px; }}
.metric strong {{ font-size: 18px; }}
.tester {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; margin-bottom: 18px; }}
.tester table {{ margin: 0; }}
.tester caption {{ text-align: left; padding: 12px; font-weight: 700; background: #f8fafc; border-bottom: 1px solid #edf1f5; }}
.panel {{ background: #fff; border: 1px solid #dde3ea; border-radius: 8px; padding: 12px; margin-bottom: 18px; overflow-x: auto; }}
.compare-note {{ font-size: 13px; color: #475569; margin: 0 0 8px; }}
svg {{ display: block; width: 100%; height: auto; }}
.grid {{ stroke: #e7ecf1; stroke-width: 1; }}
.axis {{ fill: #607080; font-size: 12px; }}
.close-line {{ stroke: #2563eb; }}
.ma-line {{ stroke: #f59e0b; }}
.candle-up {{ fill: rgba(22, 163, 74, .28); stroke: #16a34a; stroke-width: 1; }}
.candle-down {{ fill: rgba(220, 38, 38, .25); stroke: #dc2626; stroke-width: 1; }}
.buy {{ fill: #16a34a; stroke: #ffffff; stroke-width: 2; }}
.sell {{ fill: #dc2626; stroke: #ffffff; stroke-width: 2; }}
.hold-buy {{ fill: #84cc16; stroke: #365314; stroke-width: 1.5; }}
.hold-sell {{ fill: #f97316; stroke: #7c2d12; stroke-width: 1.5; }}
.signal-letter {{ fill: #ffffff; font-size: 11px; font-weight: 700; pointer-events: none; }}
.legend {{ fill: #334155; font-size: 13px; }}
.pnl-pos {{ fill: #16a34a; }}
.pnl-neg {{ fill: #dc2626; }}
.trade-label {{ fill: #607080; font-size: 11px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #dde3ea; border-radius: 8px; overflow: hidden; }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf1f5; text-align: right; font-size: 13px; }}
th {{ background: #f8fafc; color: #475569; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4) {{ text-align: left; }}
.pos {{ color: #15803d; }}
.neg {{ color: #b91c1c; }}
.empty {{ text-align: center; color: #607080; }}
@media (max-width: 900px) {{ .metrics {{ grid-template-columns: repeat(2, 1fr); }} main {{ padding: 16px; }} }}
@media (max-width: 900px) {{ .tester {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<div class="metrics">{card_html}</div>
<section class="tester">
<table><caption>Overview</caption><tbody>{stat_tables}</tbody></table>
<table><caption>Trade Analysis</caption><tbody>{analysis_tables}</tbody></table>
</section>
{benchmark_html}
<h2>价格、5日线与买卖点</h2>
<section class="panel">
<svg viewBox="0 0 {width} {price_height}" role="img">
{''.join(grid_lines)}
{''.join(date_ticks)}
<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{price_height - pad_bottom}" class="grid" />
<line x1="{pad_left}" y1="{price_height - pad_bottom}" x2="{width - pad_right}" y2="{price_height - pad_bottom}" class="grid" />
{''.join(candle_svg)}
{svg_polyline(close_points, "#2563eb", 2)}
{svg_polyline(ma_points, "#f59e0b", 2)}
{buy_svg}
{sell_svg}
{in_position_buy_svg}
{in_position_sell_svg}
<circle cx="{width - 320}" cy="22" r="5" class="buy" /><text x="{width - 308}" y="26" class="legend">Buy</text>
<circle cx="{width - 252}" cy="22" r="5" class="sell" /><text x="{width - 240}" y="26" class="legend">Sell</text>
<circle cx="{width - 176}" cy="22" r="6" class="hold-buy" /><text x="{width - 176}" y="26" text-anchor="middle" class="signal-letter">B</text><text x="{width - 164}" y="26" class="legend">Hold B</text>
<circle cx="{width - 92}" cy="22" r="6" class="hold-sell" /><text x="{width - 92}" y="26" text-anchor="middle" class="signal-letter">S</text><text x="{width - 80}" y="26" class="legend">Signal S</text>
<line x1="{width - 320}" y1="44" x2="{width - 284}" y2="44" class="close-line" stroke-width="3" /><text x="{width - 276}" y="48" class="legend">Close</text>
<line x1="{width - 220}" y1="44" x2="{width - 184}" y2="44" class="ma-line" stroke-width="3" /><text x="{width - 176}" y="48" class="legend">MA</text>
</svg>
</section>
<h2>每笔交易收益金额</h2>
<section class="panel">
<svg viewBox="0 0 {width} {pnl_height}" role="img">
<line x1="{bar_pad_left}" y1="{bar_mid:.2f}" x2="{width - bar_pad_right}" y2="{bar_mid:.2f}" class="grid" />
<text x="16" y="{bar_mid + 4:.2f}" class="axis">0</text>
{''.join(pnl_bars)}
</svg>
</section>
<h2>交易明细</h2>
<table>
<thead><tr><th>#</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>股数</th><th>收益金额</th><th>收益率</th></tr></thead>
<tbody>{trade_rows}</tbody>
</table>
</main>
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
