"""
mediamtx process manager — generates config and runs mediamtx.

Stream detection is done by polling the mediamtx HTTP API (/v3/paths/list)
instead of using runOnPublish hooks, which avoids version compatibility issues.
"""
import asyncio
import logging
import os
import tempfile
from typing import Optional

from config import Config

log = logging.getLogger("mediamtx")

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")


def generate_mediamtx_config(cfg: Config) -> str:
    """
    Generate mediamtx YAML configuration.

    Paths:
      live (or live/KEY)  — external ingest (RTMP/RTSP/SRT)
      composite           — compositor writes here (RTMP, auth-protected)
      relay               — auto-relays from composite; output FFmpeg reads this
    """
    token = cfg.internal_token

    if cfg.ingest.stream_key_required and cfg.ingest.allowed_key:
        ingest_path_name = f"live/{cfg.ingest.allowed_key}"
    else:
        ingest_path_name = "~^live"

    config_yaml = (
        f"logLevel: warn\n"
        f"logDestinations: [stdout]\n"
        f"\n"
        f"api: yes\n"
        f"apiAddress: 127.0.0.1:{cfg.mediamtx_api_port}\n"
        f"\n"
        f"rtmp:\n"
        f"  enabled: yes\n"
        f"  address: :{cfg.internal_rtmp_port}\n"
        f"\n"
        f"rtsp:\n"
        f"  enabled: yes\n"
        f"  address: :{cfg.internal_rtsp_port}\n"
        f"  protocols: [tcp]\n"
        f"\n"
        f"srt:\n"
        f"  enabled: yes\n"
        f"  address: :{cfg.ingest.srt_port}\n"
        f"\n"
        f"hls:\n"
        f"  enabled: no\n"
        f"\n"
        f"webrtc:\n"
        f"  enabled: no\n"
        f"\n"
        f"paths:\n"
        f"  {ingest_path_name}:\n"
        f"\n"
        f"  composite:\n"
        f"    publishUser: internal\n"
        f"    publishPass: {token}\n"
        f"\n"
        f"  relay:\n"
        f"    source: rtsp://127.0.0.1:{cfg.internal_rtsp_port}/composite\n"
        f"    sourceReconnectPeriod: 500ms\n"
    )
    return config_yaml


class MediamtxManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._config_file: Optional[str] = None

    async def start(self) -> None:
        config_content = generate_mediamtx_config(self.cfg)

        fd, path = tempfile.mkstemp(suffix=".yml", prefix="mediamtx-")
        self._config_file = path
        with os.fdopen(fd, "w") as f:
            f.write(config_content)

        log.debug("mediamtx config written to %s:\n%s", path, config_content)

        self._proc = await asyncio.create_subprocess_exec(
            MEDIAMTX_BIN, path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._log_output())
        log.info("mediamtx started (pid=%d)", self._proc.pid)

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Poll mediamtx API until it responds."""
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
