import unittest

from backtest import Bar, build_ratchet_inputs


def bar(day: int, close: float, volume: float, *, open_price: float | None = None, high: float | None = None, low: float | None = None) -> Bar:
    open_value = close if open_price is None else open_price
    return Bar(
        date=f"2026-01-{day:02d}",
        open=open_value,
        high=max(open_value, close) if high is None else high,
        low=min(open_value, close) if low is None else low,
        close=close,
        volume=volume,
    )


def signals(bars: list[Bar], **options: bool) -> tuple[list[bool], list[str]]:
    buy, _, _, _, _, stages = build_ratchet_inputs(
        bars,
        ma_length=2,
        vol_length=2,
        vol_multiplier=1.45,
        reentry_pct=0.045,
        vol_high_days=1,
        vol_high_multiplier=1.0,
        massive_window=1,
        massive_min_count=1,
        require_ma5_rising=False,
        require_5ma_gt_20ma=False,
        **options,
    )
    return buy, stages


class OptionalEntryConditionTests(unittest.TestCase):
    def test_big_red_b1_accepts_only_matching_b1(self) -> None:
        red_bars = [
            bar(1, 100, 100),
            bar(2, 100, 100),
            bar(3, 101, 300, open_price=104, high=105, low=100),
        ]
        green_bars = [*red_bars[:2], bar(3, 101, 300, open_price=100, high=102, low=99)]

        red_buy, red_stages = signals(red_bars, secondary_big_red_b1=True)
        green_buy, green_stages = signals(green_bars, secondary_big_red_b1=True)

        self.assertTrue(red_buy[-1])
        self.assertEqual("B1", red_stages[-1])
        self.assertFalse(green_buy[-1])
        self.assertEqual("", green_stages[-1])

    def test_three_closes_above_ma_filter(self) -> None:
        matching = [bar(1, 90, 100), bar(2, 100, 100), bar(3, 101, 100), bar(4, 102, 300)]
        not_matching = [bar(1, 100, 100), bar(2, 100, 100), bar(3, 100, 100), bar(4, 102, 300)]

        matching_buy, _ = signals(matching, secondary_above_ma5_3d=True)
        not_matching_buy, _ = signals(not_matching, secondary_above_ma5_3d=True)

        self.assertTrue(matching_buy[-1])
        self.assertFalse(not_matching_buy[-1])


if __name__ == "__main__":
    unittest.main()
