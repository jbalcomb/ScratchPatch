"""Tiny JSON-file persistence used for favorites, recents, history and settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


def default_data_dir() -> Path:
    # `flet build` sets FLET_APP_STORAGE_DATA to the app's private storage on Android/iOS.
    root = os.environ.get("FLET_APP_STORAGE_DATA") or os.environ.get("TASKDECK_DATA_DIR")
    path = Path(root) if root else Path.home() / ".taskdeck"
    path.mkdir(parents=True, exist_ok=True)
    return path


class JsonStore:
    """A dict persisted to one JSON file, written atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)
