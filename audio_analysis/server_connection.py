from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass
class ServerConnectionConfig:
    url: str = os.environ.get(
        "TRUEVISION_SERVER_URL",
        os.environ.get("SUMMARIZER_URL", ""),
    ).rstrip("/")
    timeout_sec: float = float(os.environ.get("TRUEVISION_SERVER_TIMEOUT_SEC", "2.0"))


class ServerConnection:
    def __init__(self, url: str, timeout_sec: float = 2.0):
        self._url = (url or "").rstrip("/")
        self._timeout_sec = float(timeout_sec)

    @property
    def url(self) -> str:
        return self._url

    @property
    def ws_url(self) -> str:
        return self._url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/audio"

    def health(self) -> Optional[dict]:
        if not self._url:
            return None
        req = urllib.request.Request(f"{self._url}/health", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, socket.timeout, OSError):
            return None

        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            return None

        if not payload.get("ok"):
            return None
        return payload

    @property
    def is_available(self) -> bool:
        return self.health() is not None

    def wait_until_available(self, retries: int = 3, delay_sec: float = 2.0) -> bool:
        for attempt in range(max(1, int(retries))):
            if self.is_available:
                return True
            if attempt < max(1, int(retries)) - 1:
                time.sleep(float(delay_sec))
        return False