"""
mediamtx process manager.

Config philosophy: use the absolute minimum fields to avoid breaking on
mediamtx version differences. No `paths:` section — mediamtx creates paths
dynamically on first publish/subscribe. The compositor uses a secret random
path name (cfg.composite_path) instead of per-path auth credentials.
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
    Generate a minimal mediamtx YAML configuration.

    Only sets the fields that are stable across mediamtx v1.x versions:
    - log level / destinations
    - API address (for our polling)
    - protocol enable/disable and listen addresses
    No `paths:` section — paths are created dynamically by mediamtx.
    """
    return (
        "logLevel: warn\n"
        "logDestinations: [stdout]\n"
        "\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{cfg.mediamtx_api_port}\n"
        "\n"
        "rtmp:\n"
        "  enabled: yes\n"
        f"  address: :{cfg.internal_rtmp_port}\n"
        "\n"
        "rtsp:\n"
        "  enabled: yes\n"
        f"  address: :{cfg.internal_rtsp_port}\n"
        "  protocols: [tcp]\n"
        "\n"
        "srt:\n"
        "  enabled: yes\n"
        f"  address: :{cfg.ingest.srt_port}\n"
        "\n"
        "hls:\n"
        "  enabled: no\n"
        "\n"
        "webrtc:\n"
        "  enabled: no\n"
    )


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

        log.debug("mediamtx config:\n%s", config_content)

        self._proc = await asyncio.create_subprocess_exec(
            MEDIAMTX_BIN, path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._log_output())
        log.info("mediamtx started (pid=%d)", self._proc.pid)

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Poll mediamtx API until it responds or timeout expires."""
        import urllib.request
        url = f"http://127.0.0.1:{self.cfg.mediamtx_api_port}/v3/paths/list"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: urllib.request.urlopen(url, timeout=1)
                )
                log.info("mediamtx API ready")
                return True
            except Exception:
                await asyncio.sleep(0.5)
        log.error("mediamtx did not become ready within %.0fs", timeout)
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
