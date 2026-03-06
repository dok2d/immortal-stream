#!/bin/sh
# immortal-stream entrypoint
set -e

CONFIG_PATH="${CONFIG_PATH:-/etc/immortal-stream/config.yaml}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

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
