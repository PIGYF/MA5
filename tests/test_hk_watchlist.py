from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app


class HKWatchlistTests(unittest.TestCase):
    def test_normalize_hk_code(self) -> None:
        cases = {
            "700": "0700.HK",
            "00700": "0700.HK",
            "HK:0700": "0700.HK",
            "9988.HK": "9988.HK",
            "0005": "0005.HK",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(web_app.normalize_hk_code_for_storage(raw), expected)

    def test_normalize_hk_code_rejects_invalid_values(self) -> None:
        for raw in ("", "ABC", "0", "123456"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                web_app.normalize_hk_code_for_storage(raw)

    def test_hk_watchlist_storage_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(web_app, "DATA_DIR", Path(directory)):
            web_app.save_hk_watchlist_items(
                [{"symbol": "700", "name": "腾讯控股", "group": "观察", "added_at": "2026-08-04 09:30:00"}]
            )

            self.assertEqual(web_app.hk_watchlist_path(), Path(directory) / "hk" / "watchlist.json")
            self.assertFalse(web_app.ashare_watchlist_path().exists())
            self.assertEqual(
                web_app.load_hk_watchlist_items(),
                [
                    {
                        "symbol": "0700.HK",
                        "name": "腾讯控股",
                        "sector": "",
                        "group": "观察",
                        "note": "",
                        "added_at": "2026-08-04 09:30:00",
                        "market": "hk",
                        "currency": "HKD",
                    }
                ],
            )
            payload = json.loads(web_app.hk_watchlist_path().read_text(encoding="utf-8"))
            self.assertEqual(payload["items"][0]["symbol"], "0700.HK")


if __name__ == "__main__":
    unittest.main()
