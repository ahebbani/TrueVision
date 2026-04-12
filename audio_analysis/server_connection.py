"""Server health checker with mDNS discovery fallback.

Used by the Pi to detect whether the TrueVision server is available.
When the server is up, the Pi can offload audio transcription and run
face recognition + audio simultaneously.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Optional


class ServerConnection:
    """Thread-safe server availability tracker.

    Usage::

        sc = ServerConnection()
        sc.start()           # spawns background health-check thread
        ...
        if sc.is_available:
            # offload audio to server
        ...
        sc.stop()
    """

    MDNS_SERVICE_TYPE = "_truevision._tcp.local."

    def __init__(
        self,
        url: Optional[str] = None,
        check_interval_sec: float = 5.0,
        timeout_sec: float = 3.0,
    ):
        self._explicit_url = url or os.environ.get("TRUEVISION_SERVER_URL", "").rstrip("/")
        self._check_interval = check_interval_sec
        self._timeout = timeout_sec

        self._url: Optional[str] = self._explicit_url or None
        self._available = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._server_info: dict = {}

    # ── Public properties ────────────────────────────────────────────────

    @property
    def is_available(self) -> bool:
        with self._lock:
            return self._available

    @property
    def url(self) -> Optional[str]:
        with self._lock:
            return self._url

    @property
    def ws_url(self) -> Optional[str]:
        """WebSocket URL derived from the HTTP URL."""
        with self._lock:
            if self._url is None:
                return None
            return self._url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/audio"

    @property
    def server_info(self) -> dict:
        with self._lock:
            return dict(self._server_info)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="server-health")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    # ── One-shot check ───────────────────────────────────────────────────

    def check(self) -> bool:
        """Probe the server right now and return availability."""
        if not self._url:
            self._try_mdns_discovery()
        if self._url:
            available = self._ping(self._url)
            with self._lock:
                self._available = available
            return available
        with self._lock:
            self._available = False
        return False

    # ── Internal ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Do an immediate check on start
        self.check()
        while not self._stop.wait(self._check_interval):
            self.check()

    def _ping(self, base_url: str) -> bool:
        try:
            req = urllib.request.Request(f"{base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                with self._lock:
                    self._server_info = data
                return bool(data.get("ok"))
        except (urllib.error.URLError, socket.timeout, OSError, ValueError):
            return False

    def _try_mdns_discovery(self) -> None:
        """Attempt to discover the server via mDNS/Zeroconf."""
        try:
            from zeroconf import ServiceBrowser, Zeroconf

            found_url: Optional[str] = None
            done = threading.Event()

            class _Listener:
                def add_service(self, zc: Zeroconf, stype: str, name: str) -> None:
                    nonlocal found_url
                    info = zc.get_service_info(stype, name)
                    if info and info.addresses:
                        addr = socket.inet_ntoa(info.addresses[0])
                        port = info.port
                        found_url = f"http://{addr}:{port}"
                        done.set()

                def remove_service(self, zc: Zeroconf, stype: str, name: str) -> None:
                    pass

                def update_service(self, zc: Zeroconf, stype: str, name: str) -> None:
                    pass

            zc = Zeroconf()
            browser = ServiceBrowser(zc, self.MDNS_SERVICE_TYPE, _Listener())
            done.wait(timeout=3.0)
            zc.close()

            if found_url:
                with self._lock:
                    self._url = found_url
                print(f"[server_connection] Discovered server via mDNS: {found_url}")

        except ImportError:
            pass  # zeroconf not installed — skip discovery
        except Exception as e:
            print(f"[server_connection] mDNS discovery error: {e}")
