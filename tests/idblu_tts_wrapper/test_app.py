import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_CACHE_DIR = Path("/tmp/idblu-tts-wrapper-tests")
TEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("IDBLU_TTS_ADMIN_KEY", "test-key")
os.environ.setdefault("IDBLU_TTS_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
os.environ.setdefault("IDBLU_TTS_DEFAULT_VOICE_ID", "eliane")
os.environ.setdefault("IDBLU_TTS_VOICE_CACHE_DIR", str(TEST_CACHE_DIR))

from idblu_tts_wrapper.app import app


def test_health_is_liveness_only():
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_unhealthy_when_upstream_fails(monkeypatch, client_with_voice_cache):
    client, _ = client_with_voice_cache

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            raise RuntimeError("boom")

    monkeypatch.setattr("idblu_tts_wrapper.app.httpx.AsyncClient", lambda *args, **kwargs: FakeClient())

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert "Upstream health check failed" in response.json()["reason"]


def test_ready_reports_missing_metadata_for_default_voice(client_with_voice_cache):
    client, cache_dir = client_with_voice_cache
    voice_dir = cache_dir / "eliane"
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "metadata.json").unlink(missing_ok=True)
    (cache_dir / "eliane.wav").unlink(missing_ok=True)
    (cache_dir / "eliane.txt").unlink(missing_ok=True)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["reason"] == f"Voice 'eliane' is missing metadata.json"


def test_list_voices_requires_auth(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.get("/v1/audio/voices")
    assert response.status_code == 401


def test_list_voices_reads_flat_cache(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.get("/v1/audio/voices", headers={"X-Admin-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["data"][0]["voice_id"] == "eliane"


def test_speech_injects_local_voice_assets(monkeypatch, client_with_voice_cache):
    client, cache_dir = client_with_voice_cache
    captured = {}

    class FakeStreamResponse:
        status_code = 200
        text = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"pcm"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, method, url, json):
            captured["json"] = json
            return FakeStreamResponse()

    monkeypatch.setattr("idblu_tts_wrapper.app.httpx.AsyncClient", lambda *args, **kwargs: FakeClient())

    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "eliane"},
    )

    assert response.status_code == 200
    assert response.content == b"pcm"
    assert captured["json"]["ref_audio"].startswith("data:audio/")
    assert captured["json"]["ref_text"] == "Bonjour reference"
    assert "voice_id" not in captured["json"]
    assert "voice" not in captured["json"]


def test_speech_returns_not_found_when_voice_missing(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "missing"},
    )
    assert response.status_code == 404


def test_speech_returns_422_when_voice_missing_ref_text(client_with_voice_cache):
    client, cache_dir = client_with_voice_cache
    voice_dir = cache_dir / "eliane"
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "metadata.json").write_text('{"voice_id":"eliane","display_name":"Eliane","audio_file":"reference.wav"}')
    (voice_dir / "reference.wav").write_bytes(b"RIFFtest")
    (cache_dir / "eliane.wav").unlink(missing_ok=True)
    (cache_dir / "eliane.txt").unlink(missing_ok=True)

    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "eliane"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Voice 'eliane' is missing ref_text"


def _write_voice_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    voice_dir = cache_dir / "eliane"
    voice_dir.mkdir(parents=True, exist_ok=True)
    (voice_dir / "metadata.json").write_text(
        '{"voice_id":"eliane","display_name":"Eliane","audio_file":"reference.wav","ref_text":"Bonjour reference"}'
    )
    (voice_dir / "reference.wav").write_bytes(b"RIFFtest")


def _set_app_state(monkeypatch, cache_dir: Path) -> None:
    from idblu_tts_wrapper import app as app_module
    from idblu_tts_wrapper.config import Settings
    from idblu_tts_wrapper.voice_registry import VoiceRegistry

    monkeypatch.setattr(
        app_module,
        "settings",
        Settings(
            admin_key="test-key",
            upstream_url="http://127.0.0.1:8091",
            default_model="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            default_task_type="Base",
            default_response_format="pcm",
            default_voice_id="eliane",
            voice_cache_dir=str(cache_dir),
            health_public=True,
        ),
    )
    monkeypatch.setattr(app_module, "voice_registry", VoiceRegistry(str(cache_dir)))


@pytest.fixture()
def client_with_voice_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "voices"
    _write_voice_cache(cache_dir)
    _set_app_state(monkeypatch, cache_dir)
    return TestClient(app), cache_dir
