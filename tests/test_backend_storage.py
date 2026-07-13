from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend_storage import atomic_write_json


class AtomicStorageTests(unittest.TestCase):
    def test_concurrent_json_writes_never_leave_partial_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"

            def write(index: int) -> None:
                atomic_write_json(path, {"index": index, "values": list(range(100))})

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(40)))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(payload["index"], range(40))
            self.assertEqual(payload["values"], list(range(100)))


if __name__ == "__main__":
    unittest.main()
