from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class MacroEvent:
    date: date
    title: str
    time_et: str
    category: str
    impact: str
    source: str
    source_url: str


FED_SOURCE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_SOURCE = "https://www.bls.gov/schedule/2026/home.htm"
BEA_SOURCE = "https://www.bea.gov/news/schedule/full"


MACRO_EVENTS_2026: list[MacroEvent] = [
    MacroEvent(date(2026, 6, 10), "\u7f8e\u56fd CPI 5\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 6, 11), "\u7f8e\u56fd PPI 5\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 6, 17), "FOMC \u5229\u7387\u51b3\u8bae", "14:00", "\u7f8e\u8054\u50a8", "high", "Federal Reserve", FED_SOURCE),
    MacroEvent(date(2026, 6, 25), "\u7f8e\u56fd GDP \u4e09\u8bfb Q1", "08:30", "\u589e\u957f", "medium", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 7, 2), "\u975e\u519c\u5c31\u4e1a 6\u6708", "08:30", "\u5c31\u4e1a", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 7, 7), "PCE / \u4e2a\u4eba\u6536\u5165 5\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 7, 14), "\u7f8e\u56fd CPI 6\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 7, 15), "\u7f8e\u56fd PPI 6\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 7, 29), "FOMC \u5229\u7387\u51b3\u8bae", "14:00", "\u7f8e\u8054\u50a8", "high", "Federal Reserve", FED_SOURCE),
    MacroEvent(date(2026, 7, 30), "\u7f8e\u56fd GDP \u521d\u503c Q2", "08:30", "\u589e\u957f", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 8, 4), "PCE / \u4e2a\u4eba\u6536\u5165 6\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 8, 7), "\u975e\u519c\u5c31\u4e1a 7\u6708", "08:30", "\u5c31\u4e1a", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 8, 12), "\u7f8e\u56fd CPI 7\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 8, 13), "\u7f8e\u56fd PPI 7\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 8, 26), "\u7f8e\u56fd GDP \u4e8c\u8bfb Q2", "08:30", "\u589e\u957f", "medium", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 9, 3), "PCE / \u4e2a\u4eba\u6536\u5165 7\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 9, 4), "\u975e\u519c\u5c31\u4e1a 8\u6708", "08:30", "\u5c31\u4e1a", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 9, 10), "\u7f8e\u56fd PPI 8\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 9, 11), "\u7f8e\u56fd CPI 8\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 9, 16), "FOMC \u5229\u7387\u51b3\u8bae", "14:00", "\u7f8e\u8054\u50a8", "high", "Federal Reserve", FED_SOURCE),
    MacroEvent(date(2026, 9, 30), "\u7f8e\u56fd GDP \u4e09\u8bfb Q2", "08:30", "\u589e\u957f", "medium", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 10, 6), "PCE / \u4e2a\u4eba\u6536\u5165 8\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 10, 14), "\u7f8e\u56fd CPI 9\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 10, 28), "FOMC \u5229\u7387\u51b3\u8bae", "14:00", "\u7f8e\u8054\u50a8", "high", "Federal Reserve", FED_SOURCE),
    MacroEvent(date(2026, 10, 29), "\u7f8e\u56fd GDP \u521d\u503c Q3", "08:30", "\u589e\u957f", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 11, 4), "PCE / \u4e2a\u4eba\u6536\u5165 9\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 11, 10), "\u7f8e\u56fd CPI 10\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 11, 25), "\u7f8e\u56fd GDP \u4e8c\u8bfb Q3", "08:30", "\u589e\u957f", "medium", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 12, 2), "PCE / \u4e2a\u4eba\u6536\u5165 10\u6708", "08:30", "\u901a\u80c0", "high", "BEA", BEA_SOURCE),
    MacroEvent(date(2026, 12, 9), "FOMC \u5229\u7387\u51b3\u8bae", "14:00", "\u7f8e\u8054\u50a8", "high", "Federal Reserve", FED_SOURCE),
    MacroEvent(date(2026, 12, 10), "\u7f8e\u56fd CPI 11\u6708", "08:30", "\u901a\u80c0", "high", "BLS", BLS_SOURCE),
    MacroEvent(date(2026, 12, 23), "\u7f8e\u56fd GDP \u4e09\u8bfb Q3", "08:30", "\u589e\u957f", "medium", "BEA", BEA_SOURCE),
]


def upcoming_macro_events(as_of: date, lookback_days: int = 1, lookahead_days: int = 21) -> list[MacroEvent]:
    start = as_of.toordinal() - lookback_days
    end = as_of.toordinal() + lookahead_days
    return [event for event in MACRO_EVENTS_2026 if start <= event.date.toordinal() <= end]


def macro_risk_state(as_of: date) -> dict[str, object]:
    events = upcoming_macro_events(as_of)
    high_events = [event for event in events if event.impact == "high"]
    days_to_high = [(event.date - as_of).days for event in high_events if (event.date - as_of).days >= 0]
    if any(day <= 1 for day in days_to_high):
        return {
            "tone": "bad",
            "label": "\u5b8f\u89c2\u9ad8\u6ce2\u52a8\u7a97\u53e3",
            "message": "\u672a\u67651\u5929\u5185\u6709\u9ad8\u5f71\u54cd\u5b8f\u89c2\u4e8b\u4ef6\uff0c\u5019\u9009\u4fdd\u7559\uff0c\u4f46\u5efa\u8bae\u964d\u4f4e\u8ffd\u9ad8\u548c\u6ee1\u4ed3\u4f18\u5148\u7ea7\u3002",
            "events": events,
        }
    if any(day <= 3 for day in days_to_high):
        return {
            "tone": "warn",
            "label": "\u5b8f\u89c2\u4e8b\u4ef6\u4e34\u8fd1",
            "message": "\u672a\u67653\u5929\u5185\u6709\u9ad8\u5f71\u54cd\u5b8f\u89c2\u4e8b\u4ef6\uff0c\u5efa\u8bae\u63a7\u5236\u4ed3\u4f4d\u5e76\u7b49\u5f85\u6ce2\u52a8\u843d\u5730\u3002",
            "events": events,
        }
    return {
        "tone": "neutral",
        "label": "\u5b8f\u89c2\u65e5\u5386\u6b63\u5e38",
        "message": "\u672a\u67653\u5929\u5185\u6ca1\u6709\u9ad8\u5f71\u54cd\u5b8f\u89c2\u4e8b\u4ef6\u3002",
        "events": events,
    }
