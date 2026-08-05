from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

from backtest import Bar, rolling_sma


@dataclass(frozen=True)
class ReboundSettings:
    rsi_length: int = 14
    bias_short_length: int = 6
    bias_mid_length: int = 12
    bias_long_length: int = 24
    oversold_rsi: float = 30.0
    oversold_bias_short: float = -6.0
    oversold_bias_mid: float = -8.0
    oversold_bias_long: float = -10.0
    wait_days: int = 5
    trigger_rsi: float = 35.0
    require_bias_mid_turn: bool = True
    require_previous_high_break: bool = True
    require_bullish_candle: bool = True
    require_volume_confirmation: bool = True
    volume_length: int = 5
    volume_multiplier: float = 1.0
    low_stop_buffer_pct: float = 2.0
    hard_stop_pct: float = 8.0
    exit_rsi: float = 60.0
    target_bias_long: float = -2.0
    max_hold_days: int = 10


def calculate_rsi(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    if period < 2:
        raise ValueError("RSI 周期必须至少为 2。")
    values: list[float | None] = [None] * len(bars)
    if len(bars) <= period:
        return values
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = bars[index].close - bars[index - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    def rsi_value() -> float:
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    values[period] = rsi_value()
    for index in range(period + 1, len(bars)):
        change = bars[index].close - bars[index - 1].close
        average_gain = (average_gain * (period - 1) + max(change, 0.0)) / period
        average_loss = (average_loss * (period - 1) + max(-change, 0.0)) / period
        values[index] = rsi_value()
    return values


def calculate_bias(bars: Sequence[Bar], period: int) -> tuple[list[float | None], list[float | None]]:
    moving_average = rolling_sma([bar.close for bar in bars], period)
    bias = [None if average in (None, 0) else (bar.close / average - 1.0) * 100 for bar, average in zip(bars, moving_average)]
    return moving_average, bias


def rebound_signal_series(bars: Sequence[Bar], settings: ReboundSettings) -> dict[str, list[object]]:
    if settings.wait_days < 1:
        raise ValueError("观察窗口必须至少为 1 天。")
    rsi = calculate_rsi(bars, settings.rsi_length)
    ma_short, bias_short = calculate_bias(bars, settings.bias_short_length)
    ma_mid, bias_mid = calculate_bias(bars, settings.bias_mid_length)
    ma_long, bias_long = calculate_bias(bars, settings.bias_long_length)
    volume_ma = rolling_sma([bar.volume for bar in bars], settings.volume_length)
    setup = [False] * len(bars)
    signal = [False] * len(bars)
    setup_low_by_signal: list[float | None] = [None] * len(bars)
    active_until = -1
    active_low: float | None = None

    for index, bar in enumerate(bars):
        is_setup = (
            rsi[index] is not None
            and bias_short[index] is not None
            and bias_mid[index] is not None
            and bias_long[index] is not None
            and rsi[index] <= settings.oversold_rsi
            and bias_short[index] <= settings.oversold_bias_short
            and bias_mid[index] <= settings.oversold_bias_mid
            and bias_long[index] <= settings.oversold_bias_long
        )
        if is_setup:
            setup[index] = True
            if index > active_until:
                active_low = bar.low
            else:
                active_low = min(active_low if active_low is not None else bar.low, bar.low)
            active_until = index + settings.wait_days
        elif index <= active_until and active_low is not None:
            active_low = min(active_low, bar.low)

        if index < 1 or index > active_until or active_low is None:
            continue
        rsi_cross = rsi[index - 1] is not None and rsi[index] is not None and rsi[index - 1] <= settings.trigger_rsi < rsi[index]
        short_turn = bias_short[index - 1] is not None and bias_short[index] is not None and bias_short[index] > bias_short[index - 1]
        mid_turn = not settings.require_bias_mid_turn or (
            bias_mid[index - 1] is not None and bias_mid[index] is not None and bias_mid[index] >= bias_mid[index - 1]
        )
        high_break = not settings.require_previous_high_break or bar.close > bars[index - 1].high
        bullish = not settings.require_bullish_candle or bar.close > bar.open
        volume_confirmed = not settings.require_volume_confirmation or (
            volume_ma[index] is not None and bar.volume >= volume_ma[index] * settings.volume_multiplier
        )
        if rsi_cross and short_turn and mid_turn and high_break and bullish and volume_confirmed:
            signal[index] = True
            setup_low_by_signal[index] = active_low
            active_until = -1
            active_low = None

    return {
        "rsi": rsi,
        "ma_short": ma_short,
        "ma_mid": ma_mid,
        "ma_long": ma_long,
        "bias_short": bias_short,
        "bias_mid": bias_mid,
        "bias_long": bias_long,
        "volume_ma": volume_ma,
        "setup": setup,
        "signal": signal,
        "setup_low": setup_low_by_signal,
    }


def analyze_rebound(
    bars: Sequence[Bar],
    settings: ReboundSettings,
    initial_cash: float = 100000.0,
    commission_pct: float = 0.1,
    slippage_pct: float = 0.0,
) -> dict[str, object]:
    if not bars:
        raise ValueError("没有可用于超跌反弹验证的日线数据。")
    series = rebound_signal_series(bars, settings)
    closes = [bar.close for bar in bars]
    price_mas = {period: rolling_sma(closes, period) for period in (5, 20, 60, 120, 180)}
    cash = float(initial_cash)
    shares = 0
    entry_price = 0.0
    entry_date = ""
    entry_signal_date = ""
    entry_index = -1
    stop_low = 0.0
    pending: tuple[str, int, str] | None = None
    trades: list[dict[str, object]] = []
    equity_curve: list[dict[str, object]] = []
    execution_markers: list[dict[str, object]] = []
    signal_markers: list[dict[str, object]] = []
    holding_periods: list[dict[str, str]] = []

    for index, bar in enumerate(bars):
        if pending and pending[1] == index:
            action, _, reason = pending
            if action == "buy" and shares == 0:
                fill_price = bar.open * (1 + slippage_pct / 100)
                shares = int(cash / (fill_price * (1 + commission_pct / 100)))
                if shares > 0:
                    gross = shares * fill_price
                    cash -= gross + gross * commission_pct / 100
                    entry_price = fill_price
                    entry_date = bar.date
                    entry_index = index
                    stop_low = float(series["setup_low"][index - 1] or bars[index - 1].low)
                    execution_markers.append({"time": bar.date, "position": "belowBar", "color": "#f59e0b", "shape": "arrowUp", "text": "买"})
            elif action == "sell" and shares > 0:
                fill_price = bar.open * (1 - slippage_pct / 100)
                gross = shares * fill_price
                fee = gross * commission_pct / 100
                cash += gross - fee
                pnl = (fill_price - entry_price) * shares - fee - entry_price * shares * commission_pct / 100
                trades.append(
                    {
                        "entry_signal_date": entry_signal_date,
                        "entry_date": entry_date,
                        "entry_price": round(entry_price, 4),
                        "exit_signal_date": bars[index - 1].date,
                        "exit_date": bar.date,
                        "exit_price": round(fill_price, 4),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round((fill_price / entry_price - 1) * 100, 2),
                        "bars_held": index - entry_index,
                        "reason": reason,
                    }
                )
                holding_periods.append({"start": entry_date, "end": bar.date, "label": "反弹持仓"})
                execution_markers.append({"time": bar.date, "position": "aboveBar", "color": "#f23645", "shape": "arrowDown", "text": "卖"})
                shares = 0
                entry_price = 0.0
                entry_date = ""
                entry_signal_date = ""
                entry_index = -1
                stop_low = 0.0
            pending = None

        if bool(series["setup"][index]):
            signal_markers.append({"time": bar.date, "position": "belowBar", "color": "#8b5cf6", "shape": "circle", "text": "超跌"})
        if bool(series["signal"][index]):
            signal_markers.append({"time": bar.date, "position": "belowBar", "color": "#f59e0b", "shape": "circle", "text": "反弹"})

        equity_curve.append({"date": bar.date, "equity": cash + shares * bar.close, "shares": shares})
        if index >= len(bars) - 1:
            continue
        if shares == 0 and bool(series["signal"][index]):
            entry_signal_date = bar.date
            pending = ("buy", index + 1, "")
        elif shares > 0:
            rsi_value = series["rsi"][index]
            long_bias = series["bias_long"][index]
            stop_price = max(stop_low * (1 - settings.low_stop_buffer_pct / 100), entry_price * (1 - settings.hard_stop_pct / 100))
            reasons: list[str] = []
            if bar.close < stop_price:
                reasons.append("止损")
            if rsi_value is not None and rsi_value >= settings.exit_rsi:
                reasons.append("RSI止盈")
            if long_bias is not None and long_bias >= settings.target_bias_long:
                reasons.append("BIAS24修复")
            if settings.max_hold_days > 0 and index - entry_index >= settings.max_hold_days:
                reasons.append("时间止损")
            if reasons:
                pending = ("sell", index + 1, " / ".join(reasons))

    if shares > 0:
        holding_periods.append({"start": entry_date, "end": bars[-1].date, "label": "反弹持仓中"})

    final_equity = float(equity_curve[-1]["equity"])
    wins = [trade for trade in trades if float(trade["pnl"]) > 0]
    peak = float(initial_cash)
    max_drawdown = 0.0
    for row in equity_curve:
        equity = float(row["equity"])
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)

    def points(values: Sequence[object]) -> list[dict[str, object]]:
        return [{"time": bar.date, "value": value} for bar, value in zip(bars, values) if value is not None]

    chart = {
        "symbol": "",
        "ohlc": [{"time": bar.date, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close} for bar in bars],
        "volume": [{"time": bar.date, "value": bar.volume, "color": "rgba(8,153,129,.42)" if bar.close >= bar.open else "rgba(242,54,69,.42)"} for bar in bars],
        "ma5": points(price_mas[5]),
        "ma20": points(price_mas[20]),
        "ma60": points(price_mas[60]),
        "ma120": points(price_mas[120]),
        "ma180": points(price_mas[180]),
        "rsi": points(series["rsi"]),
        "bias6": points(series["bias_short"]),
        "bias12": points(series["bias_mid"]),
        "bias24": points(series["bias_long"]),
        "markers": sorted(execution_markers, key=lambda item: str(item["time"])),
        "signals": sorted(signal_markers, key=lambda item: str(item["time"])),
        "holdingPeriods": holding_periods,
        "settings": asdict(settings),
    }
    return {
        "ok": True,
        "settings": asdict(settings),
        "summary": {
            "initial_cash": round(initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "return_pct": round((final_equity / initial_cash - 1) * 100, 2),
            "trades": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            "max_drawdown": round(max_drawdown, 2),
            "signals": sum(bool(item) for item in series["signal"]),
            "setups": sum(bool(item) for item in series["setup"]),
        },
        "trades": trades,
        "chart": chart,
    }
