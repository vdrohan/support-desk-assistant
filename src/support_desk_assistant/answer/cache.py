"""Response cache: one JSON file per request, holding the raw API response.

Completed responses are cached even when they are refusals or ``max_tokens`` stops;
HTTP/network errors never reach this module. The caller re-parses and re-validates on
every read, so fixing the validator needs no re-spend.
"""

import hashlib
import json
from pathlib import Path
from typing import Any


def cache_key(request: dict[str, Any]) -> str:
    """Hash of everything that determines the response: model, system prompt text, the
    rendered user message (question + chunk IDs, text and dates), max_tokens, effort,
    thinking and the schema version. ``cache_control`` is excluded (it doesn't change output).
    """
    blob = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(key).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def put(self, key: str, raw_response: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(raw_response, ensure_ascii=False))
        tmp.replace(self._path(key))
