from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from ashare_lab import (
    AShareBar,
    AShareUniverseItem,
    _load_ashare_universe_pipeline,
    fetch_ashare_bars,
    stale_ashare_universe_cache,
)


class AShareSourceFallbackTests(unittest.TestCase):
    def test_price_source_falls_through_to_next_provider(self) -> None:
        expected = [
            AShareBar("2026-07-09", 10, 11, 9, 10.5, 1000, 10000),
            AShareBar("2026-07-10", 10.5, 12, 10, 11.5, 1200, 13800),
        ]

        def failed(_symbol: str, _start: date, _end: date):
            raise RuntimeError("source down")

        def succeeded(_symbol: str, _start: date, _end: date):
            return expected, "secondary"

        with (
            patch("ashare_lab.read_ashare_price_cache", return_value=[]),
            patch("ashare_lab.ashare_bar_sources", return_value=[("first", failed), ("second", succeeded)]),
            patch("ashare_lab.write_ashare_price_cache", side_effect=lambda _symbol, bars: bars),
        ):
            bars, source = fetch_ashare_bars("600519", "2026-07-09", "2026-07-10")

        self.assertEqual(bars, expected)
        self.assertEqual(source, "secondary + cache")

    def test_universe_source_falls_through_to_next_provider(self) -> None:
        expected = [AShareUniverseItem("600519", "贵州茅台", "白酒", 10000, "SH")]

        def failed(_minimum: float, _maximum: int):
            raise RuntimeError("source down")

        def succeeded(_minimum: float, _maximum: int):
            return expected, "secondary", True

        with (
            patch("ashare_lab.read_ashare_universe_cache", return_value=None),
            patch("ashare_lab.ashare_universe_sources", return_value=[("first", failed), ("second", succeeded)]),
            patch("ashare_lab.write_ashare_universe_cache"),
            patch("ashare_lab.stale_ashare_universe_cache", return_value=None),
        ):
            items, source, has_market_cap = _load_ashare_universe_pipeline(50, 6000)

        self.assertEqual(items, expected)
        self.assertEqual(source, "secondary")
        self.assertTrue(has_market_cap)

    def test_universe_rejects_source_without_market_cap_when_filter_is_required(self) -> None:
        without_market_cap = [AShareUniverseItem("600519", "贵州茅台", "白酒", 0, "SH")]

        def names_only(_minimum: float, _maximum: int):
            return without_market_cap, "names only", False

        with (
            patch("ashare_lab.read_ashare_universe_cache", return_value=None),
            patch("ashare_lab.ashare_universe_sources", return_value=[("代码名称表", names_only)]),
            patch("ashare_lab.write_ashare_universe_cache"),
            patch("ashare_lab.stale_ashare_universe_cache", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "不能执行市值硬过滤"):
                _load_ashare_universe_pipeline(50, 6000)

    def test_stale_universe_does_not_bypass_market_cap_filter(self) -> None:
        without_market_cap = [AShareUniverseItem("600519", "贵州茅台", "白酒", 0, "SH")]
        with patch("ashare_lab.cached_ashare_universe_items", return_value=without_market_cap):
            self.assertIsNone(stale_ashare_universe_cache(50, 6000))


if __name__ == "__main__":
    unittest.main()
