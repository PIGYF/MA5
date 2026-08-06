from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backtest import Bar
from rebound_strategy import ReboundSettings, analyze_rebound, calculate_bias, calculate_rsi, rebound_signal_series, rolling_percentile


def sample_bars(count: int = 12) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(
            (start + timedelta(days=index)).isoformat(),
            100 + index,
            101 + index,
            99 + index,
            100.5 + index,
            1_000_000 + index * 10_000,
        )
        for index in range(count)
    ]


class ReboundStrategyTests(unittest.TestCase):
    def test_rsi_and_bias_series_keep_bar_alignment(self) -> None:
        bars = sample_bars(30)
        rsi = calculate_rsi(bars, 14)
        _, bias = calculate_bias(bars, 6)
        self.assertEqual(len(rsi), len(bars))
        self.assertEqual(len(bias), len(bars))
        self.assertIsNone(rsi[13])
        self.assertIsNotNone(rsi[14])
        self.assertIsNone(bias[4])
        self.assertIsNotNone(bias[5])

    def test_signal_executes_at_next_bar_open(self) -> None:
        bars = sample_bars()
        empty = [None] * len(bars)
        signal = [False] * len(bars)
        signal[2] = True
        setup_low = [None] * len(bars)
        setup_low[2] = 95.0
        mocked_series = {
            "rsi": [40.0] * len(bars),
            "rsi_threshold": [30.0] * len(bars),
            "ma_short": empty,
            "ma_mid": empty,
            "ma_long": empty,
            "bias_short": [-5.0] * len(bars),
            "bias_mid": [-7.0] * len(bars),
            "bias_mid_threshold": [-8.0] * len(bars),
            "bias_long": [-10.0] * len(bars),
            "volume_ma": empty,
            "decline": empty,
            "setup": [False] * len(bars),
            "signal": signal,
            "setup_low": setup_low,
        }
        settings = ReboundSettings(max_hold_days=2, exit_rsi=100, target_bias_long=100, hard_stop_pct=50)
        with patch("rebound_strategy.rebound_signal_series", return_value=mocked_series):
            result = analyze_rebound(bars, settings, commission_pct=0, slippage_pct=0)
        buy_marker = next(marker for marker in result["chart"]["markers"] if marker["text"] == "买")
        self.assertEqual(buy_marker["time"], bars[3].date)
        self.assertEqual(result["trades"][0]["entry_signal_date"], bars[2].date)
        self.assertEqual(result["trades"][0]["entry_date"], bars[3].date)

    def test_price_chart_uses_requested_trend_moving_averages(self) -> None:
        bars = sample_bars(220)
        result = analyze_rebound(bars, ReboundSettings())
        for period in (5, 20, 60, 120, 180):
            points = result["chart"][f"ma{period}"]
            self.assertEqual(points[0]["time"], bars[period - 1].date)
            self.assertEqual(len(points), len(bars) - period + 1)

    def test_setup_requires_configured_decline(self) -> None:
        bars = sample_bars(300)
        strict = ReboundSettings(
            decline_days=5,
            decline_pct=-10,
        )
        relaxed = ReboundSettings(**{**strict.__dict__, "decline_pct": 10})
        self.assertFalse(any(rebound_signal_series(bars, strict)["setup"]))
        self.assertTrue(any(rebound_signal_series(bars, relaxed)["setup"]))

    def test_setup_does_not_require_bias6_or_bias24_thresholds(self) -> None:
        bars = sample_bars(300)
        settings = ReboundSettings(
            decline_days=5,
            decline_pct=100,
        )
        series = rebound_signal_series(bars, settings)
        self.assertTrue(any(series["setup"]))

    def test_dynamic_percentile_uses_prior_values_only(self) -> None:
        thresholds = rolling_percentile([1.0, 2.0, 3.0, 100.0], 3, 50)
        self.assertEqual(thresholds[3], 2.0)

    def test_report_start_excludes_warmup_bars_and_events(self) -> None:
        bars = sample_bars(300)
        report_start = bars[280].date
        result = analyze_rebound(bars, ReboundSettings(decline_pct=100), report_start=report_start)
        self.assertEqual(result["chart"]["ohlc"][0]["time"], report_start)
        self.assertTrue(all(event["date"] >= report_start for event in result["events"]))


if __name__ == "__main__":
    unittest.main()
