from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from httpx import HTTPError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

from idblu_tts_wrapper.config import Settings
from idblu_tts_wrapper.voice_registry import VoiceRegistry, VoiceValidationError

logger = logging.getLogger(__name__)
UPSTREAM_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=15.0, pool=5.0)
PROCESS_STARTED_AT = time.time()
ACTIVE_TTS_JOBS = 0
TOTAL_TTS_JOBS = 0
FAILED_TTS_JOBS = 0


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    input: str
    model: str | None = None
    voice_id: str | None = None
    voice: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    response_format: str | None = None
    task_type: str | None = None
    language: str | None = None
    instructions: str | None = None
    stream: bool | None = None
    max_new_tokens: int | None = None
    initial_codec_chunk_frames: int | None = None
    x_vector_only_mode: bool | None = None
    speaker_embedding: list[float] | None = None
    speed: float | None = None
    seed: int | None = None
    extra_params: dict[str, Any] | None = None

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("input must not be empty")
        return trimmed


settings = Settings.from_env()
voice_registry = VoiceRegistry(settings.voice_cache_dir)
app = FastAPI(title="idblu_tts", version="1.0.0")


def _private_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return None


def _gpu_metrics() -> dict[str, float | None]:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return {
            "gpu_percent": None,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "gpu_memory_percent": None,
        }

    rows: list[tuple[float, float, float]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1]), float(parts[2])))
        except ValueError:
            continue

    if not rows:
        return {
            "gpu_percent": None,
            "gpu_memory_used_mb": None,
            "gpu_memory_total_mb": None,
            "gpu_memory_percent": None,
        }

    used_mb = sum(row[1] for row in rows)
    total_mb = sum(row[2] for row in rows)
    return {
        "gpu_percent": round(max(row[0] for row in rows), 2),
        "gpu_memory_used_mb": round(used_mb, 2),
        "gpu_memory_total_mb": round(total_mb, 2),
        "gpu_memory_percent": round((used_mb / total_mb) * 100, 2) if total_mb else None,
    }


def _runtime_metrics() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    memory = psutil.virtual_memory()
    process = psutil.Process(os.getpid())
    return {
        "service": "idblu-tts",
        "environment": os.getenv("ENVIRONMENT", "shared") or "shared",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "host": {
            "hostname": socket.gethostname(),
            "private_ip": _private_ip(),
            "platform": platform.platform(),
        },
        "process": {
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - PROCESS_STARTED_AT, 2),
            "rss_bytes": process.memory_info().rss,
            "threads": process.num_threads(),
        },
        "resources": {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": memory.percent,
            "memory_used_bytes": memory.used,
            "memory_total_bytes": memory.total,
            "disk_percent": round((disk.used / disk.total) * 100, 2) if disk.total else None,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
            **_gpu_metrics(),
        },
        "runtime": {
            "active_tts_jobs": ACTIVE_TTS_JOBS,
            "queue_depth": 0,
            "total_tts_jobs": TOTAL_TTS_JOBS,
            "failed_tts_jobs": FAILED_TTS_JOBS,
        },
        "image": {
            "tag": os.getenv("IMAGE_TAG") or os.getenv("DOCKER_IMAGE_TAG") or "",
            "digest": os.getenv("IMAGE_DIGEST") or "",
        },
    }


async def _authorize(
    x_admin_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    bearer_token = ""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            bearer_token = token.strip()

    presented_key = (x_admin_key or "").strip() or bearer_token
    if presented_key != settings.admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key")


@app.get("/health")
async def healthcheck() -> JSONResponse:
    return JSONResponse(content={"status": "ok"})


@app.get("/ready")
async def readiness(voice_id: str | None = Query(default=None)) -> JSONResponse:
    cache_error = voice_registry.cache_error()
    if cache_error:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "component": "voice_cache", "reason": cache_error},
        )

    requested_voice_id = (voice_id or "").strip()
    if requested_voice_id:
        voice_error = voice_registry.readiness_error(requested_voice_id)
        if voice_error:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "component": "voice_cache",
                    "voice_id": requested_voice_id,
                    "reason": voice_error,
                },
            )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.upstream_url}/health")
            response.raise_for_status()
            payload: dict[str, Any] = {"status_code": response.status_code}
            content_type = response.headers.get("content-type", "").lower()
            body = response.text.strip()
            if body:
                if "json" in content_type:
                    payload["body"] = response.json()
                else:
                    payload["body"] = body
    except Exception as exc:  # pragma: no cover - error path is exercised in runtime
        logger.warning("Upstream readiness check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "component": "upstream",
                "voice_id": requested_voice_id or None,
                "reason": f"Upstream health check failed: {exc}",
            },
        )
    content: dict[str, Any] = {"status": "ready", "upstream": payload}
    if requested_voice_id:
        content["voice_id"] = requested_voice_id
    return JSONResponse(content=content)


@app.get("/metrics/runtime", dependencies=[Depends(_authorize)])
async def runtime_metrics() -> dict[str, Any]:
    return _runtime_metrics()


@app.get("/v1/audio/voices", dependencies=[Depends(_authorize)])
async def list_voices() -> dict[str, Any]:
    return {"data": voice_registry.list_voices()}


@app.post("/v1/audio/speech", dependencies=[Depends(_authorize)])
async def create_speech(request: SpeechRequest, raw_request: Request) -> StreamingResponse:
    request_received_at = time.monotonic()
    trace_id = raw_request.headers.get("X-TTS-Trace-Id") or f"idblu-wrapper-{uuid.uuid4().hex[:12]}"
    payload = request.model_dump(exclude_none=True)
    payload["model"] = payload.get("model") or settings.default_model
    payload["task_type"] = payload.get("task_type") or settings.default_task_type
    payload["response_format"] = payload.get("response_format") or settings.default_response_format
    payload["stream"] = True if payload.get("stream") is None else payload["stream"]
    if (
        settings.default_instructions_enabled
        and settings.default_instructions
        and not str(payload.get("instructions") or "").strip()
    ):
        payload["instructions"] = settings.default_instructions

    resolved_locally = False
    requested_voice_id = payload.pop("voice_id", None) or payload.get("voice")

    if payload.get("ref_audio") is None and not requested_voice_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="voice_id is required when ref_audio is not provided",
        )

    try:
        if payload.get("ref_audio") is None and requested_voice_id:
            voice_resolution_started_at = time.monotonic()
            voice_spec = voice_registry.resolve(requested_voice_id)
            voice_resolution_elapsed = time.monotonic() - voice_resolution_started_at
            payload["ref_audio"] = voice_spec.ref_audio
            if not payload.get("ref_text") and voice_spec.ref_text:
                payload["ref_text"] = voice_spec.ref_text
            payload.pop("voice", None)
            resolved_locally = True
            try:
                source_size_bytes = voice_spec.source_path.stat().st_size
            except OSError:
                source_size_bytes = -1
            logger.info(
                "[%s] Wrapper resolved local voice %s in %.3fs: source=%s source_bytes=%d ref_audio_scheme=%s "
                "ref_audio_chars=%d ref_text_chars=%d",
                trace_id,
                requested_voice_id,
                voice_resolution_elapsed,
                voice_spec.source_path,
                source_size_bytes,
                "data",
                len(voice_spec.ref_audio),
                len(voice_spec.ref_text or ""),
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except VoiceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if payload.get("ref_audio") and not payload.get("ref_text"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ref_text is required when ref_audio is provided",
        )

    logger.info(
        "[%s] Wrapper request received: model=%s voice=%s resolved_locally=%s language=%s "
        "stream=%s max_new_tokens=%s initial_codec_chunk_frames=%s text_len=%d",
        trace_id,
        payload.get("model"),
        requested_voice_id,
        resolved_locally,
        payload.get("language", "Auto"),
        payload.get("stream"),
        payload.get("max_new_tokens"),
        payload.get("initial_codec_chunk_frames"),
        len(payload.get("input", "")),
    )
    logger.info(
        "[%s] Wrapper request prepared in %.3fs: has_ref_audio=%s ref_audio_scheme=%s ref_audio_chars=%d ref_text_chars=%d",
        trace_id,
        time.monotonic() - request_received_at,
        bool(payload.get("ref_audio")),
        "data" if isinstance(payload.get("ref_audio"), str) and payload["ref_audio"].startswith("data:") else "other",
        len(payload["ref_audio"]) if isinstance(payload.get("ref_audio"), str) else 0,
        len(payload.get("ref_text", "")) if isinstance(payload.get("ref_text"), str) else 0,
    )

    async def stream() -> AsyncGenerator[bytes, None]:
        global ACTIVE_TTS_JOBS, TOTAL_TTS_JOBS, FAILED_TTS_JOBS
        ACTIVE_TTS_JOBS += 1
        TOTAL_TTS_JOBS += 1
        async with httpx.AsyncClient(timeout=UPSTREAM_STREAM_TIMEOUT) as client:
            try:
                upstream_started_at = time.monotonic()
                logger.info("[%s] Wrapper upstream request starting after %.3fs", trace_id, upstream_started_at - request_received_at)
                async with client.stream(
                    "POST",
                    f"{settings.upstream_url}/v1/audio/speech",
                    headers={"X-TTS-Trace-Id": trace_id},
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    first_upstream_chunk_logged = False
                    first_downstream_chunk_logged = False
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            if not first_upstream_chunk_logged:
                                logger.info(
                                    "[%s] Wrapper first upstream audio chunk received after %.3fs (%d bytes)",
                                    trace_id,
                                    time.monotonic() - request_received_at,
                                    len(chunk),
                                )
                                first_upstream_chunk_logged = True
                            if not first_downstream_chunk_logged:
                                logger.info(
                                    "[%s] Wrapper first downstream chunk yielded after %.3fs (%d bytes)",
                                    trace_id,
                                    time.monotonic() - request_received_at,
                                    len(chunk),
                                )
                                first_downstream_chunk_logged = True
                            yield chunk
            except HTTPError as exc:
                FAILED_TTS_JOBS += 1
                logger.warning("[%s] Upstream TTS request failed: %s", trace_id, exc)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream TTS request failed: {exc}",
                ) from exc
            except Exception:
                FAILED_TTS_JOBS += 1
                raise
            finally:
                ACTIVE_TTS_JOBS = max(0, ACTIVE_TTS_JOBS - 1)

    return StreamingResponse(stream(), media_type=_media_type_for_format(payload["response_format"]))


def _media_type_for_format(response_format: str) -> str:
    normalized = response_format.lower()
    if normalized == "pcm":
        return "audio/pcm"
    if normalized == "wav":
        return "audio/wav"
    if normalized == "mp3":
        return "audio/mpeg"
    return "application/octet-stream"
