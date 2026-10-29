#!/bin/bash
# Start the Notetaker API in the foreground. Stop it with Ctrl-C.
# Host and port come from .env: NOTETAKER_HOST (default 127.0.0.1), NOTETAKER_PORT (default 8333).
set -euo pipefail
cd "$(dirname "$0")"
read -r HOST PORT < <(venv/bin/python -c 'import os, notetaker; print(os.environ.get("NOTETAKER_HOST", "127.0.0.1"), os.environ.get("NOTETAKER_PORT", "8333"))')
echo "Notetaker API on http://$HOST:$PORT"
exec venv/bin/uvicorn notetaker.api:app --host "$HOST" --port "$PORT"
