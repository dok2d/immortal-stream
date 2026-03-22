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
    echo "[entrypoint] Mount your config with: -v /path/to/config.yaml:$CONFIG_PATH"
    exit 1
fi

# Ensure required directories exist and fix ownership on bind-mounted
# paths so streamer (uid 1000) can write.
mkdir -p /media/opt/.cache /media/records
chown streamer:streamer "$CONFIG_PATH" 2>/dev/null || true
chown -R streamer:streamer /media 2>/dev/null || true

exec su-exec streamer python3 /app/main.py
