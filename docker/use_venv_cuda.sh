#!/usr/bin/env bash
set -euo pipefail

# The container already runs inside the packaged Python environment from the
# base image. This shim preserves the existing on-machine launch contract used
# by the upstream scripts.
export PATH="/usr/local/bin:${PATH}"
