#!/bin/bash
# Launch vLLM-Omni server for Qwen3-TTS models
#
# Usage:
#   ./run_server.sh                           # Default: CustomVoice model
#   ./run_server.sh CustomVoice               # CustomVoice model
#   ./run_server.sh VoiceDesign               # VoiceDesign model
#   ./run_server.sh Base                      # Base (voice clone) model
#   ./run_server.sh Base safe                 # Conservative safe profile
#   ./run_server.sh Base latency              # Safe profile with async_chunk + async_scheduling
#   ./run_server.sh Base default              # Bundled default config

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../../../../use_venv_cuda.sh"

TASK_TYPE="${1:-CustomVoice}"
PROFILE="${2:-}"

case "$TASK_TYPE" in
    CustomVoice)
        MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
        ;;
    VoiceDesign)
        MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
        ;;
    Base)
        MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-Base"
        ;;
    *)
        echo "Unknown task type: $TASK_TYPE"
        echo "Supported: CustomVoice, VoiceDesign, Base"
        exit 1
        ;;
esac

echo "Starting Qwen3-TTS server with model: $MODEL"

case "$PROFILE" in
    "")
        DEPLOY_CONFIG="${QWEN3_TTS_DEPLOY_CONFIG:-vllm_omni/deploy/qwen3_tts.yaml}"
        PROFILE_LABEL="custom/default"
        ;;
    safe)
        DEPLOY_CONFIG="vllm_omni/deploy/qwen3_tts_safe.yaml"
        PROFILE_LABEL="safe"
        ;;
    latency|async)
        DEPLOY_CONFIG="vllm_omni/deploy/qwen3_tts_safe_async.yaml"
        PROFILE_LABEL="latency"
        ;;
    default|fast)
        DEPLOY_CONFIG="vllm_omni/deploy/qwen3_tts.yaml"
        PROFILE_LABEL="default"
        ;;
    *)
        echo "Unknown profile: $PROFILE"
        echo "Supported profiles: safe, latency, default"
        exit 1
        ;;
esac

export VLLM_WORKER_MULTIPROC_METHOD="${VLLM_WORKER_MULTIPROC_METHOD:-spawn}"
HOST="${VLLM_OMNI_HOST:-0.0.0.0}"
PORT="${VLLM_OMNI_PORT:-8091}"
echo "Using deploy config: $DEPLOY_CONFIG"
echo "Using launch profile: $PROFILE_LABEL"
echo "Using worker multiprocessing method: $VLLM_WORKER_MULTIPROC_METHOD"
echo "Binding vllm-omni to ${HOST}:${PORT}"

GPU_MEMORY_ARGS=()
if [ -n "${VLLM_GLOBAL_GPU_MEMORY_UTILIZATION:-}" ]; then
    GPU_MEMORY_ARGS+=(--gpu-memory-utilization "$VLLM_GLOBAL_GPU_MEMORY_UTILIZATION")
    echo "Using global GPU memory utilization override: $VLLM_GLOBAL_GPU_MEMORY_UTILIZATION"
else
    echo "Using GPU memory utilization from deploy config"
fi

vllm-omni serve "$MODEL" \
    --deploy-config "$DEPLOY_CONFIG" \
    --host "$HOST" \
    --port "$PORT" \
    "${GPU_MEMORY_ARGS[@]}" \
    --trust-remote-code \
    --omni
