from __future__ import annotations

import unittest

import task_runtime


class TaskRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        with task_runtime.SCAN_JOBS_LOCK:
            task_runtime.SCAN_JOBS.clear()

    def test_market_jobs_are_isolated_and_normalized(self) -> None:
        task_runtime.set_job("us-job", market="us", status="running", total=100, scanned=25)
        task_runtime.set_job("ashare-job", market="cn", status="running", total=200, scanned=50)

        us_id, us_job = task_runtime.active_scan_job("us") or ("", {})
        cn_id, cn_job = task_runtime.active_scan_job("cn") or ("", {})

        self.assertEqual(us_id, "us-job")
        self.assertEqual(cn_id, "ashare-job")
        self.assertEqual(task_runtime.normalize_job_payload(us_id, us_job)["progress_pct"], 25)
        self.assertEqual(task_runtime.normalize_job_payload(cn_id, cn_job)["market_label"], "A股")

    def test_error_categories_remain_stable_for_ui(self) -> None:
        summary = task_runtime.summarize_error_categories(
            [("AAPL", "connection timed out"), ("600519", "没有可用 A 股日线数据源")]
        )
        self.assertEqual(summary, {"网络/接口": 1, "无数据": 1})


if __name__ == "__main__":
    unittest.main()
