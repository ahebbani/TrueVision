from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests


@dataclass
class OllamaConfig:
    base_url: str = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model: str = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
    temperature: float = float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
    top_p: float = float(os.environ.get("OLLAMA_TOP_P", "0.9"))
    num_predict: int = int(os.environ.get("OLLAMA_NUM_PREDICT", "80"))
    timeout_sec: float = float(os.environ.get("OLLAMA_TIMEOUT_SEC", "30"))


def generate_one_shot(prompt: str, cfg: Optional[OllamaConfig] = None) -> Tuple[str, Dict[str, Any]]:
    """Call Ollama /api/generate with stream=false and return (text, meta)."""
    cfg = cfg or OllamaConfig()
    url = f"{cfg.base_url}/api/generate"

    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "num_predict": cfg.num_predict,
        },
    }

    t0 = time.time()
    resp = requests.post(url, json=payload, timeout=cfg.timeout_sec)
    resp.raise_for_status()
    data = resp.json() if resp.content else {}

    text = (data.get("response") or "").strip()
    meta: Dict[str, Any] = {
        "model": data.get("model") or cfg.model,
        "created_at": data.get("created_at"),
        "done": data.get("done"),
        "total_duration": data.get("total_duration"),
        "load_duration": data.get("load_duration"),
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
        "eval_duration": data.get("eval_duration"),
        "took_ms": int((time.time() - t0) * 1000),
    }
    return text, meta
