"""mDNS / Zeroconf service advertisement for the TrueVision server.

Advertises ``_truevision._tcp.local.`` so that Pi clients on the same LAN
can discover the server without explicit URL configuration.
"""
from __future__ import annotations

import socket
import threading
from typing import Optional

from server.config import ServerConfig

try:
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:
    ServiceInfo = None  # type: ignore[assignment,misc]
    Zeroconf = None  # type: ignore[assignment,misc]


class DiscoveryAdvertiser:
    """Advertise the TrueVision server over mDNS."""

    def __init__(self, cfg: Optional[ServerConfig] = None):
        self.cfg = cfg or ServerConfig()
        self._zc: Optional[Zeroconf] = None  # type: ignore[assignment]
        self._info: Optional[ServiceInfo] = None  # type: ignore[assignment]

    def start(self) -> bool:
        if Zeroconf is None or ServiceInfo is None:
            print("[discovery] zeroconf not installed — mDNS advertisement disabled")
            return False

        try:
            local_ip = self._get_local_ip()
            self._info = ServiceInfo(
                self.cfg.mdns_service_type,
                self.cfg.mdns_service_name,
                addresses=[socket.inet_aton(local_ip)],
                port=self.cfg.port,
                properties={
                    "version": "1",
                    "whisper_model": self.cfg.whisper_model,
                    "whisper_device": self.cfg.whisper_device,
                },
            )
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            print(f"[discovery] Advertising on mDNS: {self.cfg.mdns_service_name} "
                  f"at {local_ip}:{self.cfg.port}")
            return True
        except Exception as e:
            print(f"[discovery] mDNS advertisement failed: {e}")
            return False

    def stop(self) -> None:
        if self._zc is not None and self._info is not None:
            try:
                self._zc.unregister_service(self._info)
            except Exception:
                pass
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._info = None

    @staticmethod
    def _get_local_ip() -> str:
        """Best-effort detection of the machine's LAN IP address."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()
