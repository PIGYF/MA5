from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare_lab import AShareBar, latest_ashare_signal
from backtest import rolling_sma
from web_app import save_latest_ashare_scan


class AShareScannerLogicTests(unittest.TestCase):
    @staticmethod
    def structured_bars(structured_volume: bool = True) -> list[AShareBar]:
        start = date(2026, 1, 1)
        bars: list[AShareBar] = []
        close = 100.0
        for index in range(140):
            if index < 119:
                close += 0.01
                volume = 100.0
            elif structured_volume:
                if (index - 119) % 2 == 0:
                    close += 1.0
                    volume = 400.0 if index == 139 else 250.0
                else:
                    close -= 0.2
                    volume = 100.0
            else:
                close += 0.01
                volume = 100.0
            bars.append(
                AShareBar(
                    date=(start + timedelta(days=index)).isoformat(),
                    open=close - 0.1,
                    high=close + 0.5,
                    low=close - 0.5,
                    close=close,
                    volume=volume,
                    amount=200_000_000,
                )
            )
        return bars

    @staticmethod
    def ratchet_inputs(bars, *args, **kwargs):
        closes = [bar.close for bar in bars]
        signals = [False] * len(bars)
        stages = [""] * len(bars)
        signals[-1] = True
        stages[-1] = "B1"
        return signals, [False] * len(bars), rolling_sma(closes, 5), [100.0] * len(bars), [0.0] * len(bars), stages

    def snapshot(self, j_value: float, structured_volume: bool = True):
        bars = self.structured_bars(structured_volume)
        j_values = [j_value] * len(bars)
        with (
            patch("ashare_lab.fetch_ashare_bars", return_value=(bars, "test")),
            patch("backtest.build_ratchet_inputs", side_effect=self.ratchet_inputs),
            patch("backtest.calculate_kdj", return_value=([50.0] * len(bars), [50.0] * len(bars), j_values)),
        ):
            return latest_ashare_signal("600519", j_threshold=14, fetch_name_value=False)

    def test_kdj_and_volume_structure_are_reference_only(self) -> None:
        accepted = self.snapshot(10)
        self.assertTrue(accepted.j_oversold)
        self.assertTrue(accepted.volume_structure_ok)
        self.assertTrue(accepted.signal)

        high_j = self.snapshot(20)
        self.assertEqual(high_j.j_value, 20)
        self.assertFalse(high_j.j_oversold)
        self.assertTrue(high_j.signal)

        weak_volume = self.snapshot(10, structured_volume=False)
        self.assertFalse(weak_volume.volume_structure_ok)
        self.assertTrue(weak_volume.signal)

    def test_empty_scan_uses_latest_completed_trading_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "latest.json"
            with (
                patch("web_app.ashare_latest_scan_path", return_value=target),
                patch("web_app.ashare_required_latest_date", return_value=date(2026, 7, 10)),
                patch("web_app.update_scan_history_index"),
            ):
                save_latest_ashare_scan([], [], 20, "test", True, {})
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["signal_date"], "2026-07-10")


if __name__ == "__main__":
    unittest.main()
