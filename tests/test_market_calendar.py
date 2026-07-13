from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from market_calendar import is_trading_day, latest_completed_trading_day, next_trading_day, previous_trading_day


class MarketCalendarTests(unittest.TestCase):
    def test_cn_2026_exchange_holidays(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 1, 2), "cn"))
        self.assertFalse(is_trading_day(date(2026, 2, 23), "cn"))
        self.assertEqual(next_trading_day(date(2026, 2, 13), "cn"), date(2026, 2, 24))
        self.assertEqual(previous_trading_day(date(2026, 10, 8), "cn"), date(2026, 9, 30))

    def test_us_observed_holidays(self) -> None:
        self.assertFalse(is_trading_day(date(2026, 7, 3), "us"))
        self.assertFalse(is_trading_day(date(2026, 11, 26), "us"))
        self.assertEqual(next_trading_day(date(2026, 7, 2), "us"), date(2026, 7, 6))

    def test_latest_completed_day_respects_market_close(self) -> None:
        before_close = datetime(2026, 7, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        after_close = datetime(2026, 7, 13, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(latest_completed_trading_day("cn", before_close), date(2026, 7, 10))
        self.assertEqual(latest_completed_trading_day("cn", after_close), date(2026, 7, 13))


if __name__ == "__main__":
    unittest.main()
