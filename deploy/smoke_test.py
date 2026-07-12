#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from urllib.request import urlopen


def read(base_url: str, path: str) -> tuple[int, str, str]:
    with urlopen(f"{base_url.rstrip('/')}{path}", timeout=15) as response:
        return response.status, response.headers.get("content-type", ""), response.read().decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    status, _, body = read(args.base_url, "/api/health")
    assert status == 200 and json.loads(body).get("ok") is True

    status, _, body = read(args.base_url, "/app/")
    assert status == 200 and '<div id="root"></div>' in body and "/app/assets/" in body

    for market in ("us", "cn"):
        status, content_type, body = read(args.base_url, f"/api/{market}/scanner/bootstrap")
        payload = json.loads(body)
        assert status == 200 and "application/json" in content_type
        assert payload.get("ok") is True and isinstance(payload.get("defaults"), dict)
        assert isinstance(payload.get("market_environment"), dict)

    print("MA5 smoke checks passed")


if __name__ == "__main__":
    main()
