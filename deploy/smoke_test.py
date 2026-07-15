#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from email.message import Message
from urllib.request import urlopen


def read(base_url: str, path: str) -> tuple[int, str, Message, str]:
    with urlopen(f"{base_url.rstrip('/')}{path}", timeout=15) as response:
        return response.status, response.headers.get("content-type", ""), response.headers, response.read().decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    status, _, headers, body = read(args.base_url, "/api/health")
    assert status == 200 and json.loads(body).get("ok") is True
    assert "no-store" in headers.get("cache-control", "")

    status, _, headers, body = read(args.base_url, "/app/")
    assert status == 200 and '<div id="root"></div>' in body and "/app/assets/" in body
    assert "no-store" in headers.get("cache-control", "")
    asset_match = re.search(r'src="(/app/assets/[^"]+\.js)"', body)
    assert asset_match is not None
    asset_status, _, asset_headers, _ = read(args.base_url, asset_match.group(1))
    assert asset_status == 200 and "immutable" in asset_headers.get("cache-control", "")

    for market in ("us", "cn"):
        status, content_type, headers, body = read(args.base_url, f"/api/{market}/scanner/bootstrap")
        payload = json.loads(body)
        assert status == 200 and "application/json" in content_type
        assert "no-store" in headers.get("cache-control", "")
        assert payload.get("ok") is True and isinstance(payload.get("defaults"), dict)
        assert isinstance(payload.get("market_environment"), dict)

    print("MA5 smoke checks passed")


if __name__ == "__main__":
    main()
