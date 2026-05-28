from __future__ import annotations

import csv
from pathlib import Path

from backtest import backtest, fetch_bars, summarize


SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "QQQ"]
BENCHMARK = "^IXIC"
START = "2020-01-01"
END = "2026-05-26"
INITIAL_CASH = 100000
COMMISSION_PCT = 0.1


def buy_hold_return(bars) -> float:
    return (bars[-1].close / bars[0].close - 1) * 100


def run_one(symbol: str, benchmark_return: float) -> dict[str, float | int | str]:
    bars = fetch_bars("yfinance", symbol, START, END, "qfq", None)

    classic_trades, classic_equity = backtest(
        bars,
        ma_length=5,
        vol_length=50,
        vol_multiplier=1.45,
        initial_cash=INITIAL_CASH,
        commission_pct=COMMISSION_PCT,
        slippage_pct=0,
        strategy_name="classic",
    )
    ratchet_trades, ratchet_equity = backtest(
        bars,
        ma_length=5,
        vol_length=50,
        vol_multiplier=1.45,
        initial_cash=INITIAL_CASH,
        commission_pct=COMMISSION_PCT,
        slippage_pct=0,
        strategy_name="ratchet",
        stop_5ma_pct=7.5,
        hard_stop_pct=20,
        reentry_pct=4.5,
    )
    classic = summarize(classic_trades, classic_equity, INITIAL_CASH)
    ratchet = summarize(ratchet_trades, ratchet_equity, INITIAL_CASH)

    return {
        "symbol": symbol,
        "buy_hold_pct": buy_hold_return(bars),
        "benchmark_ixic_pct": benchmark_return,
        "classic_return_pct": classic["return_pct"],
        "classic_max_dd_pct": classic["max_drawdown_pct"],
        "classic_trades": classic["trades"],
        "classic_win_rate_pct": classic["win_rate_pct"],
        "ratchet_return_pct": ratchet["return_pct"],
        "ratchet_max_dd_pct": ratchet["max_drawdown_pct"],
        "ratchet_trades": ratchet["trades"],
        "ratchet_win_rate_pct": ratchet["win_rate_pct"],
        "ratchet_profit_factor": ratchet["profit_factor"],
    }


def main() -> None:
    benchmark_bars = fetch_bars("yfinance", BENCHMARK, START, END, "qfq", None)
    benchmark_return = buy_hold_return(benchmark_bars)
    rows = [run_one(symbol, benchmark_return) for symbol in SYMBOLS]

    out_path = Path("ratchet_evaluation.csv")
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Period: {START} to {END}")
    print(f"Benchmark {BENCHMARK}: {benchmark_return:.2f}%")
    print(
        "symbol,buy_hold,classic_ret,classic_dd,classic_trades,"
        "ratchet_ret,ratchet_dd,ratchet_trades,ratchet_win_rate"
    )
    for row in rows:
        print(
            f"{row['symbol']},"
            f"{row['buy_hold_pct']:.2f},"
            f"{row['classic_return_pct']:.2f},"
            f"{row['classic_max_dd_pct']:.2f},"
            f"{row['classic_trades']},"
            f"{row['ratchet_return_pct']:.2f},"
            f"{row['ratchet_max_dd_pct']:.2f},"
            f"{row['ratchet_trades']},"
            f"{row['ratchet_win_rate_pct']:.2f}"
        )
    print(f"Saved: {out_path.resolve()}")


if __name__ == "__main__":
    main()
