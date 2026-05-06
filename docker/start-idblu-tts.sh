#!/usr/bin/env bash
set -euo pipefail

export VLLM_OMNI_HOST="${VLLM_OMNI_HOST:-127.0.0.1}"
export VLLM_OMNI_PORT="${VLLM_OMNI_PORT:-8091}"
export IDBLU_TTS_PORT="${IDBLU_TTS_PORT:-8080}"
export IDBLU_TTS_TASK_TYPE="${IDBLU_TTS_TASK_TYPE:-Base}"
export IDBLU_TTS_PROFILE="${IDBLU_TTS_PROFILE:-safe}"

cd /app/vllm-omni

./examples/online_serving/qwen3_tts/run_server.sh "${IDBLU_TTS_TASK_TYPE}" "${IDBLU_TTS_PROFILE}" &
UPSTREAM_PID=$!

shutdown() {
  if kill -0 "${UPSTREAM_PID}" 2>/dev/null; then
    kill "${UPSTREAM_PID}" 2>/dev/null || true
    wait "${UPSTREAM_PID}" 2>/dev/null || true
  fi
}

trap shutdown EXIT INT TERM

exec uvicorn idblu_tts_wrapper.app:app --host 0.0.0.0 --port "${IDBLU_TTS_PORT}"
