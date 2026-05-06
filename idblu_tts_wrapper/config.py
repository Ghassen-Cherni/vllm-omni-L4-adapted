from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _truthy(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    admin_key: str
    upstream_url: str
    default_model: str
    default_task_type: str
    default_response_format: str
    default_voice_id: str
    voice_cache_dir: str
    health_public: bool

    @classmethod
    def from_env(cls) -> "Settings":
        admin_key = os.getenv("IDBLU_TTS_ADMIN_KEY", "").strip()
        upstream_url = os.getenv("IDBLU_TTS_UPSTREAM_URL", "http://127.0.0.1:8091").rstrip("/")
        default_model = os.getenv("IDBLU_TTS_MODEL_ID", "").strip()
        default_voice_id = os.getenv("IDBLU_TTS_DEFAULT_VOICE_ID", "").strip()
        voice_cache_dir = os.getenv("IDBLU_TTS_VOICE_CACHE_DIR", "").strip()

        if not admin_key:
            raise ValueError("IDBLU_TTS_ADMIN_KEY is required")
        if not default_model:
            raise ValueError("IDBLU_TTS_MODEL_ID is required")
        if not default_voice_id:
            raise ValueError("IDBLU_TTS_DEFAULT_VOICE_ID is required")
        if not voice_cache_dir:
            raise ValueError("IDBLU_TTS_VOICE_CACHE_DIR is required")

        cache_path = Path(voice_cache_dir)
        if not cache_path.exists() or not cache_path.is_dir():
            raise ValueError(f"IDBLU_TTS_VOICE_CACHE_DIR does not exist or is not a directory: {voice_cache_dir}")

        return cls(
            admin_key=admin_key,
            upstream_url=upstream_url,
            default_model=default_model,
            default_task_type=os.getenv("IDBLU_TTS_TASK_TYPE", "Base"),
            default_response_format=os.getenv("IDBLU_TTS_RESPONSE_FORMAT", "pcm"),
            default_voice_id=default_voice_id,
            voice_cache_dir=voice_cache_dir,
            health_public=_truthy(os.getenv("IDBLU_TTS_PUBLIC_HEALTH"), default=True),
        )
