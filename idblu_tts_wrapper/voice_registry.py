from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path


class VoiceValidationError(ValueError):
    """Raised when a voice exists but is incomplete or invalid."""


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    display_name: str
    ref_audio: str
    ref_text: str | None
    source_path: Path


class VoiceRegistry:
    """Resolves logical voice ids from a synced local cache directory."""

    def __init__(self, cache_dir: str):
        self._cache_dir = Path(cache_dir)

    def list_voices(self) -> list[dict[str, str]]:
        if not self._cache_dir.exists():
            return []

        voices: list[dict[str, str]] = []
        for metadata_path in sorted(self._cache_dir.rglob("metadata.json")):
            voice_id = metadata_path.parent.name
            payload = self._load_json(metadata_path)
            voices.append(
                {
                    "voice_id": voice_id,
                    "name": str(payload.get("display_name") or payload.get("voice_id") or voice_id),
                    "ref_text": str(payload.get("ref_text") or ""),
                }
            )

        for audio_path in sorted(self._cache_dir.rglob("*.wav")):
            voice_id = audio_path.stem
            if any(voice["voice_id"] == voice_id for voice in voices):
                continue
            ref_text_path = audio_path.with_suffix(".txt")
            voices.append(
                {
                    "voice_id": voice_id,
                    "name": voice_id,
                    "ref_text": ref_text_path.read_text().strip() if ref_text_path.exists() else "",
                }
            )
        return voices

    def resolve(self, voice_id: str) -> VoiceSpec:
        normalized = voice_id.strip()
        if not normalized:
            raise VoiceValidationError("Voice id is empty")

        metadata_path = self._cache_dir / normalized / "metadata.json"
        if metadata_path.exists():
            payload = self._load_json(metadata_path)
            audio_path = self._resolve_audio_path(metadata_path.parent, payload)
            ref_text = self._resolve_ref_text(metadata_path.parent, payload)
            if not ref_text:
                raise VoiceValidationError(f"Voice '{normalized}' is missing ref_text")
            return VoiceSpec(
                voice_id=normalized,
                display_name=str(payload.get("display_name") or payload.get("voice_id") or normalized),
                ref_audio=self._encode_audio_data_url(audio_path),
                ref_text=ref_text,
                source_path=audio_path,
            )

        flat_audio_path = self._first_existing(
            [
                self._cache_dir / f"{normalized}.wav",
                self._cache_dir / f"{normalized}.mp3",
                self._cache_dir / f"{normalized}.flac",
            ]
        )
        if flat_audio_path is None:
            recursive_matches = sorted(self._cache_dir.rglob(f"{normalized}.*"))
            flat_audio_path = self._first_existing(
                [path for path in recursive_matches if path.suffix.lower() in {".wav", ".mp3", ".flac"}]
            )
        if flat_audio_path is None:
            raise FileNotFoundError(f"Voice '{normalized}' not found in {self._cache_dir}")

        ref_text_path = flat_audio_path.with_suffix(".txt")
        ref_text = ref_text_path.read_text().strip() if ref_text_path.exists() else None
        if not ref_text:
            raise VoiceValidationError(
                f"Voice '{normalized}' is missing ref_text; migrate it to {self._cache_dir / normalized / 'metadata.json'}"
            )
        return VoiceSpec(
            voice_id=normalized,
            display_name=normalized,
            ref_audio=self._encode_audio_data_url(flat_audio_path),
            ref_text=ref_text,
            source_path=flat_audio_path,
        )

    def readiness_error(self, voice_id: str) -> str | None:
        if not self._cache_dir.exists() or not self._cache_dir.is_dir():
            return f"Voice cache directory is unavailable: {self._cache_dir}"

        normalized = voice_id.strip()
        if not normalized:
            return "Default voice id is empty"

        metadata_dir = self._cache_dir / normalized
        metadata_path = metadata_dir / "metadata.json"
        if metadata_dir.exists() and not metadata_dir.is_dir():
            return f"Voice directory is invalid for '{normalized}'"
        if metadata_dir.exists() and not metadata_path.exists():
            return f"Voice '{normalized}' is missing metadata.json"
        if metadata_path.exists():
            try:
                self.resolve(normalized)
            except (FileNotFoundError, VoiceValidationError, ValueError) as exc:
                return str(exc)
            return None

        legacy_audio = self._first_existing(
            [
                self._cache_dir / f"{normalized}.wav",
                self._cache_dir / f"{normalized}.mp3",
                self._cache_dir / f"{normalized}.flac",
            ]
        )
        if legacy_audio is not None:
            return (
                f"Voice '{normalized}' is still using legacy flat files; "
                f"migrate it to {self._cache_dir / normalized / 'metadata.json'}"
            )
        return f"Voice '{normalized}' not found in {self._cache_dir}"

    @staticmethod
    def _load_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise VoiceValidationError(f"Voice metadata at {path} is not valid JSON") from exc

    @staticmethod
    def _first_existing(candidates: list[Path]) -> Path | None:
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _resolve_audio_path(self, directory: Path, payload: dict) -> Path:
        explicit = payload.get("audio_file")
        candidates = []
        if explicit:
            candidates.append(directory / str(explicit))
        candidates.extend(
            [
                directory / "reference.wav",
                directory / "reference.mp3",
                directory / "reference.flac",
                directory / f"{directory.name}.wav",
                directory / f"{directory.name}.mp3",
                directory / f"{directory.name}.flac",
            ]
        )
        audio_path = self._first_existing(candidates)
        if audio_path is None:
            raise VoiceValidationError(f"Voice '{directory.name}' is missing its reference audio file")
        return audio_path

    def _resolve_ref_text(self, directory: Path, payload: dict) -> str | None:
        explicit = payload.get("ref_text")
        if explicit:
            return str(explicit).strip()

        text_candidates = [
            directory / "reference.txt",
            directory / f"{directory.name}.txt",
        ]
        text_path = self._first_existing(text_candidates)
        if text_path is None:
            return None
        return text_path.read_text().strip()

    @staticmethod
    def _encode_audio_data_url(audio_path: Path) -> str:
        mime_type, _ = mimetypes.guess_type(audio_path.name)
        if mime_type is None:
            mime_type = "audio/wav"
        encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
