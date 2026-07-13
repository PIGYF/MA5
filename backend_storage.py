from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_lock(path: Path) -> threading.RLock:
    key = _resolved(path)
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    lock = _path_lock(path)
    with lock:
        yield


def atomic_write_bytes(path: Path, content: bytes) -> None:
    target = _resolved(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with locked_path(target):
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, content.encode(encoding))


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = None) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")


def atomic_write_csv(
    path: Path,
    fieldnames: list[str],
    rows: Iterable[Mapping[str, object]],
    *,
    encoding: str = "utf-8-sig",
) -> None:
    target = _resolved(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with locked_path(target):
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
                encoding=encoding,
                newline="",
            ) as handle:
                temp_path = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
