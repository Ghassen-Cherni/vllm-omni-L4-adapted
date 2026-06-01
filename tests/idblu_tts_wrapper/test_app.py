import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_CACHE_DIR = Path("/tmp/idblu-tts-wrapper-tests")
TEST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("IDBLU_TTS_ADMIN_KEY", "test-key")
os.environ.setdefault("IDBLU_TTS_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
os.environ.setdefault("IDBLU_TTS_DEFAULT_VOICE_ID", "")
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
    assert response.json()["component"] == "upstream"
    assert "Upstream health check failed" in response.json()["reason"]


def test_ready_validates_requested_voice_before_upstream(monkeypatch, client_with_voice_cache):
    client, _ = client_with_voice_cache
    called = False

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            nonlocal called
            called = True
            raise AssertionError("upstream should not be checked when voice is missing")

    monkeypatch.setattr("idblu_tts_wrapper.app.httpx.AsyncClient", lambda *args, **kwargs: FakeClient())

    response = client.get("/ready?voice_id=missing")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["component"] == "voice_cache"
    assert response.json()["voice_id"] == "missing"
    assert "not found" in response.json()["reason"]
    assert called is False


def test_ready_reports_invalid_requested_voice(monkeypatch, client_with_voice_cache):
    client, cache_dir = client_with_voice_cache
    voice_dir = cache_dir / "eliane"
    (voice_dir / "metadata.json").write_text('{"voice_id":"eliane","display_name":"Eliane","audio_file":"reference.wav"}')

    response = client.get("/ready?voice_id=eliane")

    assert response.status_code == 503
    assert response.json()["component"] == "voice_cache"
    assert response.json()["reason"] == "Voice 'eliane' is missing ref_text"


def test_ready_includes_requested_voice_when_ready(client_with_empty_voice_cache):
    client, cache_dir = client_with_empty_voice_cache
    _write_voice_cache(cache_dir)

    response = client.get("/ready?voice_id=eliane")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["voice_id"] == "eliane"


def test_ready_allows_empty_voice_cache(monkeypatch, client_with_empty_voice_cache):
    client, _ = client_with_empty_voice_cache

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_list_voices_requires_auth(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.get("/v1/audio/voices")
    assert response.status_code == 401


def test_list_voices_reads_flat_cache(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.get("/v1/audio/voices", headers={"X-Admin-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["data"][0]["voice_id"] == "eliane"


def test_list_voices_can_be_empty(client_with_empty_voice_cache):
    client, _ = client_with_empty_voice_cache
    response = client.get("/v1/audio/voices", headers={"X-Admin-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["data"] == []


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

        def stream(self, method, url, headers, json):
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


def test_speech_injects_default_instructions(monkeypatch, client_with_voice_cache):
    from idblu_tts_wrapper.config import DEFAULT_TTS_INSTRUCTIONS

    client, _ = client_with_voice_cache
    captured = _capture_speech_payload(monkeypatch)

    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "eliane"},
    )

    assert response.status_code == 200
    assert captured["json"]["instructions"] == DEFAULT_TTS_INSTRUCTIONS


def test_speech_preserves_explicit_instructions(monkeypatch, client_with_voice_cache):
    client, _ = client_with_voice_cache
    captured = _capture_speech_payload(monkeypatch)

    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={
            "input": "bonjour",
            "voice_id": "eliane",
            "instructions": "Parlez avec une joie tranquille.",
        },
    )

    assert response.status_code == 200
    assert captured["json"]["instructions"] == "Parlez avec une joie tranquille."


def test_speech_can_disable_default_instructions(monkeypatch, client_with_voice_cache):
    client, cache_dir = client_with_voice_cache
    _set_app_state(monkeypatch, cache_dir, default_instructions_enabled=False)
    captured = _capture_speech_payload(monkeypatch)

    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "eliane"},
    )

    assert response.status_code == 200
    assert "instructions" not in captured["json"]


def test_speech_returns_not_found_when_voice_missing(client_with_voice_cache):
    client, _ = client_with_voice_cache
    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour", "voice_id": "missing"},
    )
    assert response.status_code == 404


def test_speech_requires_voice_or_ref_audio(client_with_empty_voice_cache):
    client, _ = client_with_empty_voice_cache
    response = client.post(
        "/v1/audio/speech",
        headers={"X-Admin-Key": "test-key"},
        json={"input": "bonjour"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "voice_id is required when ref_audio is not provided"


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


def _capture_speech_payload(monkeypatch):
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

        def stream(self, method, url, headers, json):
            captured["json"] = json
            return FakeStreamResponse()

    monkeypatch.setattr("idblu_tts_wrapper.app.httpx.AsyncClient", lambda *args, **kwargs: FakeClient())
    return captured


def _set_app_state(
    monkeypatch,
    cache_dir: Path,
    *,
    default_instructions: str | None = None,
    default_instructions_enabled: bool = True,
) -> None:
    from idblu_tts_wrapper import app as app_module
    from idblu_tts_wrapper.config import DEFAULT_TTS_INSTRUCTIONS, Settings
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
            default_voice_id="",
            default_instructions=DEFAULT_TTS_INSTRUCTIONS if default_instructions is None else default_instructions,
            default_instructions_enabled=default_instructions_enabled,
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


@pytest.fixture()
def client_with_empty_voice_cache(monkeypatch, tmp_path):
    cache_dir = tmp_path / "voices"
    cache_dir.mkdir(parents=True, exist_ok=True)
    _set_app_state(monkeypatch, cache_dir)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"status":"ok"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr("idblu_tts_wrapper.app.httpx.AsyncClient", lambda *args, **kwargs: FakeClient())
    return TestClient(app), cache_dir
