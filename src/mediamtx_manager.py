"""
mediamtx process manager — generates config and runs mediamtx.
"""
import asyncio
import logging
import os
import signal
import tempfile
from typing import Optional

from config import Config

log = logging.getLogger("mediamtx")

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")


def generate_mediamtx_config(cfg: Config) -> str:
    """
    Generate mediamtx YAML configuration.

    Paths:
      live          — external ingest (RTMP/RTSP/SRT)
      composite     — compositor writes here (internal RTMP, auth-protected)
      relay         — auto-relay from composite → output FFmpeg reads this
    """
    token = cfg.internal_token
    webhook = f"http://127.0.0.1:{cfg.webhook_port}"

    # Decide which ingest path to configure
    if cfg.ingest.stream_key_required and cfg.ingest.allowed_key:
        ingest_path_name = f"live/{cfg.ingest.allowed_key}"
    else:
        ingest_path_name = "~^live"

    config_yaml = f"""
logLevel: warn
logDestinations: [stdout]

api: yes
apiAddress: 127.0.0.1:{cfg.mediamtx_api_port}

rtmp:
  enabled: yes
  address: :{cfg.internal_rtmp_port}

rtsp:
  enabled: yes
  address: :{cfg.internal_rtsp_port}
  protocols: [tcp]

srt:
  enabled: yes
  address: :{cfg.ingest.srt_port}

hls:
  enabled: no

webrtc:
  enabled: no

paths:
  # External incoming stream — any protocol
  "{ingest_path_name}":
    runOnPublish: >-
      curl -sf {webhook}/hook/publish
      -d "path=$MTX_PATH&conn_type=$MTX_CONN_TYPE&conn_id=$MTX_CONN_ID"
    runOnPublishRestart: no
    runOnUnpublish: >-
      curl -sf {webhook}/hook/unpublish
      -d "path=$MTX_PATH&conn_id=$MTX_CONN_ID"

  # Compositor output — internal only, protected by token
  composite:
    publishUser: internal
    publishPass: "{token}"
    maxReaders: 5

  # Relay path — mediamtx automatically reads from composite and serves it
  # Output FFmpeg reads from here; brief gaps during compositor restart
  # are absorbed by sourceReconnectPeriod
  relay:
    source: rtsp://127.0.0.1:{cfg.internal_rtsp_port}/composite
    sourceReconnectPeriod: 500ms
"""
    return config_yaml.strip()


class MediamtxManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._config_file: Optional[str] = None

    async def start(self) -> None:
        config_content = generate_mediamtx_config(self.cfg)

        # Write config to a temp file (will be cleaned up on stop)
        fd, path = tempfile.mkstemp(suffix=".yml", prefix="mediamtx-")
        self._config_file = path
        with os.fdopen(fd, "w") as f:
            f.write(config_content)

        log.debug("mediamtx config written to %s", path)
        log.debug("Config:\n%s", config_content)

        self._proc = await asyncio.create_subprocess_exec(
            MEDIAMTX_BIN, path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._log_output())
        log.info("mediamtx started (pid=%d)", self._proc.pid)

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        """Poll mediamtx API until it responds, or timeout."""
        import urllib.request
        deadline = asyncio.get_event_loop().time() + timeout
        url = f"http://127.0.0.1:{self.cfg.mediamtx_api_port}/v3/paths/list"
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: urllib.request.urlopen(url, timeout=1)
                )
                log.info("mediamtx API ready")
                return True
            except Exception:
                await asyncio.sleep(0.5)
        log.error("mediamtx did not become ready within %.1fs", timeout)
        return False

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        if self._config_file and os.path.exists(self._config_file):
            os.unlink(self._config_file)

    async def _log_output(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            log.debug("[mediamtx] %s", line.decode(errors="replace").rstrip())
