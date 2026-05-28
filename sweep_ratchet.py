from __future__ import annotations

from itertools import product

from backtest import backtest, fetch_bars, summarize


SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "QQQ"]
START = "2020-01-01"
END = "2026-05-26"
INITIAL_CASH = 100000
COMMISSION_PCT = 0.1


def main() -> None:
    bars_by_symbol = {
        symbol: fetch_bars("yfinance", symbol, START, END, "qfq", None)
        for symbol in SYMBOLS
    }
    rows = []
    for stop_5ma_pct, hard_stop_pct in product([5.0, 7.5], [10.0, 15.0, 20.0]):
        returns = []
        drawdowns = []
        trades = []
        for symbol, bars in bars_by_symbol.items():
            closed_trades, equity = backtest(
                bars,
                ma_length=5,
                vol_length=50,
                vol_multiplier=1.45,
                initial_cash=INITIAL_CASH,
                commission_pct=COMMISSION_PCT,
                slippage_pct=0,
                strategy_name="ratchet",
                stop_5ma_pct=stop_5ma_pct,
                hard_stop_pct=hard_stop_pct,
                reentry_pct=4.5,
            )
            summary = summarize(closed_trades, equity, INITIAL_CASH)
            returns.append(summary["return_pct"])
            drawdowns.append(summary["max_drawdown_pct"])
            trades.append(summary["trades"])
        rows.append(
            {
                "stop_5ma_pct": stop_5ma_pct,
                "hard_stop_pct": hard_stop_pct,
                "avg_return_pct": sum(returns) / len(returns),
                "avg_max_dd_pct": sum(drawdowns) / len(drawdowns),
                "avg_trades": sum(trades) / len(trades),
                "return_to_dd": (sum(returns) / len(returns)) / (sum(drawdowns) / len(drawdowns)),
            }
        )

    rows.sort(key=lambda row: row["return_to_dd"], reverse=True)
    print("stop_5ma,hard_stop,avg_return,avg_max_dd,avg_trades,return/dd")
    for row in rows:
        print(
            f"{row['stop_5ma_pct']:.1f},"
            f"{row['hard_stop_pct']:.1f},"
            f"{row['avg_return_pct']:.2f},"
            f"{row['avg_max_dd_pct']:.2f},"
            f"{row['avg_trades']:.1f},"
            f"{row['return_to_dd']:.2f}"
        )


if __name__ == "__main__":
    main()
