#!/usr/bin/env bash
set -euo pipefail

exec uv run python -m databridge.generation_job "$@"
