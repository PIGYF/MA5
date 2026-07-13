from __future__ import annotations

import unittest
from datetime import date, timedelta

from backtest import Bar, build_ratchet_inputs
from scan_next_b import latest_b_signal


class SignalConsistencyTests(unittest.TestCase):
    def test_scanner_uses_same_latest_ratchet_stage_as_backtest_series(self) -> None:
        start = date(2026, 1, 1)
        bars = []
        for index in range(90):
            close = 100 + index * 0.25
            volume = 2600 if index >= 87 else 1000
            bars.append(
                Bar(
                    (start + timedelta(days=index)).isoformat(),
                    close - 0.1,
                    close + 0.4,
                    close - 0.3,
                    close,
                    volume,
                )
            )

        buy_signals, _, _, _, _, stages = build_ratchet_inputs(
            bars,
            5,
            20,
            1.45,
            0.045,
            3,
            1.0,
            7,
            1,
            2,
            False,
            False,
            False,
        )
        result = latest_b_signal(
            "TEST",
            bars,
            5,
            20,
            1.45,
            4.5,
            0,
            0,
            3,
            1.0,
            7,
            1,
            2,
            False,
            False,
            False,
        )

        self.assertTrue(buy_signals[-1])
        self.assertIsNotNone(result)
        expected = {"B1": "B1_trend_confirm", "B2": "B2_reentry"}[stages[-1]]
        self.assertEqual(result.signal_type, expected)


if __name__ == "__main__":
    unittest.main()
