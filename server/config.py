"""Server configuration — env-var–driven with sensible defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


@dataclass
class ServerConfig:
    host: str = os.environ.get("TRUEVISION_SERVER_HOST", "0.0.0.0")
    port: int = int(os.environ.get("TRUEVISION_SERVER_PORT", "8008"))

    # Whisper (transcription)
    whisper_model: str = os.environ.get("WHISPER_MODEL", "small")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "auto")
    whisper_compute_type: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
    caption_interval_sec: float = float(os.environ.get("CAPTION_INTERVAL_SEC", "0.7"))
    caption_window_sec: float = float(os.environ.get("CAPTION_WINDOW_SEC", "2.0"))
    caption_max_words: int = int(os.environ.get("CAPTION_MAX_WORDS", "30"))
    translation_source_languages_raw: str = os.environ.get(
        "TRANSLATION_SOURCE_LANGUAGES",
        "es,de",
    )
    translation_target_language: str = os.environ.get("TRANSLATION_TARGET_LANGUAGE", "en")
    translation_detection_min_probability: float = float(
        os.environ.get("TRANSLATION_DETECTION_MIN_PROBABILITY", "0.0")
    )

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

    def translation_source_languages(self) -> tuple[str, ...]:
        return _parse_csv(self.translation_source_languages_raw)

    def is_multilingual_whisper_model(self) -> bool:
        return not self.whisper_model.strip().lower().endswith(".en")

    def translation_available(self) -> bool:
        return (
            self.translation_target_language.strip().lower() == "en"
            and self.is_multilingual_whisper_model()
            and bool(self.translation_source_languages())
        )
