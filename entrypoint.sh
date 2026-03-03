#!/bin/sh
# immortal-stream entrypoint
set -e

CONFIG_PATH="${CONFIG_PATH:-/etc/immortal-stream/config.yaml}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

# Generate a random internal token for securing the composite RTMP path
# (prevents external publishers from hijacking the internal relay)
if [ -z "$INTERNAL_TOKEN" ]; then
    INTERNAL_TOKEN="$(cat /dev/urandom | tr -dc 'a-f0-9' | head -c 32)"
fi
export INTERNAL_TOKEN
export CONFIG_PATH
export LOG_LEVEL

echo "[entrypoint] Starting immortal-stream"
echo "[entrypoint] Config: $CONFIG_PATH"
echo "[entrypoint] Log level: $LOG_LEVEL"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[entrypoint] ERROR: Config file not found: $CONFIG_PATH"
    echo "[entrypoint] Mount your config with: -v /path/to/config.yaml:$CONFIG_PATH:ro"
    exit 1
fi

exec python3 /app/main.py
