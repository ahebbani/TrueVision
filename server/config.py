"""Server configuration — env-var–driven with sensible defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ServerConfig:
    host: str = os.environ.get("TRUEVISION_SERVER_HOST", "0.0.0.0")
    port: int = int(os.environ.get("TRUEVISION_SERVER_PORT", "8008"))

    # Whisper (transcription)
    whisper_model: str = os.environ.get("WHISPER_MODEL", "small")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "cuda")
    whisper_compute_type: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
    caption_interval_sec: float = float(os.environ.get("CAPTION_INTERVAL_SEC", "0.7"))

    # Audio
    sample_rate: int = 16000
    channels: int = 1
    bytes_per_sample: int = 2  # 16-bit PCM

    # Backfill
    backfill_interval_sec: float = float(os.environ.get("TRUEVISION_BACKFILL_INTERVAL", "300"))
    backfill_upload_dir: str = os.environ.get(
        "TRUEVISION_BACKFILL_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
    )

    # mDNS
    mdns_service_type: str = "_truevision._tcp.local."
    mdns_service_name: str = "TrueVision Server._truevision._tcp.local."
