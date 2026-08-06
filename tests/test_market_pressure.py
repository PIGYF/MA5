import unittest

from backtest import Bar, double_index_pressure_dates, index_ma_pressure_dates


def bars(closes: list[float], prefix: str = "2026-01-") -> list[Bar]:
    return [
        Bar(
            date=f"{prefix}{index:02d}",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000_000,
        )
        for index, close in enumerate(closes, start=1)
    ]


class MarketPressureTests(unittest.TestCase):
    def test_requires_two_closes_below_a_falling_ma5(self) -> None:
        rows = bars([10, 10, 10, 10, 10, 9, 8])

        pressured = index_ma_pressure_dates(rows)

        self.assertNotIn("2026-01-06", pressured)
        self.assertIn("2026-01-07", pressured)

    def test_flat_or_rising_ma_is_not_pressured(self) -> None:
        rows = bars([10, 10, 10, 10, 10, 9, 11])

        self.assertNotIn("2026-01-07", index_ma_pressure_dates(rows))

    def test_double_pressure_requires_both_indexes_on_same_date(self) -> None:
        nasdaq = bars([10, 10, 10, 10, 10, 9, 8])
        sp500 = bars([20, 20, 20, 20, 20, 19, 18])
        strong_sp500 = bars([20, 20, 20, 20, 20, 19, 22])

        self.assertIn("2026-01-07", double_index_pressure_dates(nasdaq, sp500))
        self.assertNotIn("2026-01-07", double_index_pressure_dates(nasdaq, strong_sp500))


if __name__ == "__main__":
    unittest.main()
