from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CN_MARKET_HOLIDAYS = {
    # Shanghai Stock Exchange annual closure notices.
    date(2025, 1, 1),
    *[date(2025, 1, day) for day in range(28, 32)],
    *[date(2025, 2, day) for day in range(1, 5)],
    date(2025, 4, 4),
    *[date(2025, 5, day) for day in range(1, 6)],
    date(2025, 6, 2),
    *[date(2025, 10, day) for day in range(1, 9)],
    date(2026, 1, 1),
    date(2026, 1, 2),
    *[date(2026, 2, day) for day in range(16, 24)],
    date(2026, 4, 6),
    *[date(2026, 5, day) for day in range(1, 6)],
    date(2026, 6, 19),
    date(2026, 9, 25),
    *[date(2026, 10, day) for day in range(1, 8)],
}


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + (occurrence - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month = (h + length - 7 * m + 114) // 31
    day = ((h + length - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return holidays


def is_trading_day(day: date, market: str) -> bool:
    if day.weekday() >= 5:
        return False
    if market == "cn":
        return day not in CN_MARKET_HOLIDAYS
    if market == "us":
        return day not in us_market_holidays(day.year)
    raise ValueError(f"未知市场：{market}")


def previous_trading_day(day: date, market: str, *, include_day: bool = False) -> date:
    current = day if include_day else day - timedelta(days=1)
    while not is_trading_day(current, market):
        current -= timedelta(days=1)
    return current


def next_trading_day(day: date, market: str) -> date:
    current = day + timedelta(days=1)
    while not is_trading_day(current, market):
        current += timedelta(days=1)
    return current


def latest_completed_trading_day(market: str, now: datetime | None = None) -> date:
    timezone = ZoneInfo("Asia/Shanghai") if market == "cn" else ZoneInfo("America/New_York")
    cutoff = time(15, 30) if market == "cn" else time(16, 30)
    local_now = now.astimezone(timezone) if now and now.tzinfo else (now.replace(tzinfo=timezone) if now else datetime.now(timezone))
    if is_trading_day(local_now.date(), market) and local_now.time() >= cutoff:
        return local_now.date()
    return previous_trading_day(local_now.date(), market)
