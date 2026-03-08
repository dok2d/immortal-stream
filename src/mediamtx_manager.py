"""
mediamtx process manager.

Detects the installed mediamtx version at startup and generates a compatible
YAML configuration.  Two formats are supported:

  v1.x  (mediamtx >= 1.0)  — flat top-level keys, YAML 1.2 booleans:
      api: true
      rtmpAddress: :1935
      srt: true

  v0.x  (rtsp-simple-server / mediamtx < 1.0)  — flat XxxEnabled keys:
      apiEnabled: yes
      rtmpAddress: :1935

Version is detected by running `mediamtx --version` before the process is
started.  If detection fails, v1.x is assumed (matches the Containerfile ARG).

mediamtx is used only for external stream ingest.  The compositor→output
link uses UDP/MPEG-TS directly, bypassing mediamtx entirely.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
import urllib.request
from typing import Optional, Tuple

from config import Config

log = logging.getLogger("mediamtx")

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")


# ---------------------------------------------------------------------------
#  Version detection
# ---------------------------------------------------------------------------

def _detect_version(bin_path: str) -> Tuple[int, int]:
    """Run ``mediamtx --version`` and return (major, minor).

    Returns (1, 0) when detection fails (assumes latest format).
    """
    try:
        result = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = result.stdout + result.stderr
        m = re.search(r"v?(\d+)\.(\d+)", text)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            log.info("mediamtx version: v%d.%d", major, minor)
            return major, minor
        log.warning(
            "cannot parse mediamtx version from output: %r", text[:200]
        )
    except FileNotFoundError:
        log.error("mediamtx binary not found at %s", bin_path)
    except Exception as e:
        log.warning("mediamtx version detection failed: %s", e)
    log.info("assuming mediamtx v1.x config format")
    return 1, 0


# ---------------------------------------------------------------------------
#  Config generators
# ---------------------------------------------------------------------------

def _gen_config_v1(cfg: Config) -> str:
    """mediamtx v1.x — flat top-level keys (YAML 1.2 compliant).

    Uses true/false instead of yes/no for YAML 1.2 compatibility
    (mediamtx >= v1.15 uses goccy/go-yaml which is YAML 1.2 strict).

    Stream detection uses runOnPublish/runOnUnpublish hooks that POST
    to the internal hook server, giving near-instant event delivery
    instead of polling.
    """
    hls_enabled = "true" if cfg.ingest.hls else "false"
    hook_port = cfg.hook_server_port
    return (
        "logLevel: warn\n"
        "logDestinations: [stdout]\n"
        "\n"
        "api: true\n"
        f"apiAddress: 127.0.0.1:{cfg.mediamtx_api_port}\n"
        "\n"
        f"rtmpAddress: :{cfg.internal_rtmp_port}\n"
        "\n"
        f"rtspAddress: :{cfg.internal_rtsp_port}\n"
        "rtspTransports: [tcp]\n"
        "\n"
        "srt: true\n"
        f"srtAddress: :{cfg.ingest.srt_port}\n"
        "\n"
        f"hls: {hls_enabled}\n"
        f"hlsAddress: :{cfg.ingest.hls_port}\n"
        "\n"
        "webrtc: false\n"
        "\n"
        "pathDefaults:\n"
        f'  runOnReady: "curl -sf -X POST http://127.0.0.1:{hook_port}/on_publish -d \'{{\\\"path\\\":\\\"$MTX_PATH\\\",\\\"conn_type\\\":\\\"$MTX_SOURCE_TYPE\\\",\\\"conn_id\\\":\\\"$MTX_SOURCE_ID\\\"}}\' -H \'Content-Type: application/json\'"\n'
        f'  runOnNotReady: "curl -sf -X POST http://127.0.0.1:{hook_port}/on_unpublish -d \'{{\\\"path\\\":\\\"$MTX_PATH\\\"}}\' -H \'Content-Type: application/json\'"\n'
        "paths:\n"
        "  all_others:\n"
    )


def _gen_config_v0(cfg: Config) -> str:
    """rtsp-simple-server / mediamtx v0.x — flat XxxEnabled keys."""
    hls_enabled = "yes" if cfg.ingest.hls else "no"
    hook_port = cfg.hook_server_port
    return (
        "logLevel: warn\n"
        "logDestinations: [stdout]\n"
        "\n"
        "apiEnabled: yes\n"
        f"apiAddress: 127.0.0.1:{cfg.mediamtx_api_port}\n"
        "\n"
        "rtmpEnabled: yes\n"
        f"rtmpAddress: :{cfg.internal_rtmp_port}\n"
        "\n"
        "rtspEnabled: yes\n"
        f"rtspAddress: :{cfg.internal_rtsp_port}\n"
        "rtspProtocols: [tcp]\n"
        "\n"
        "srtEnabled: yes\n"
        f"srtAddress: :{cfg.ingest.srt_port}\n"
        "\n"
        f"hlsEnabled: {hls_enabled}\n"
        "webRTCEnabled: no\n"
        "\n"
        "paths:\n"
        "  all:\n"
        f'    runOnPublish: "curl -sf -X POST http://127.0.0.1:{hook_port}/on_publish -d \'{{\\\"path\\\":\\\"$RTSP_PATH\\\",\\\"conn_type\\\":\\\"$RTSP_SOURCE_TYPE\\\",\\\"conn_id\\\":\\\"$RTSP_SOURCE_ID\\\"}}\' -H \'Content-Type: application/json\'"\n'
        f'    runOnUnpublish: "curl -sf -X POST http://127.0.0.1:{hook_port}/on_unpublish -d \'{{\\\"path\\\":\\\"$RTSP_PATH\\\"}}\' -H \'Content-Type: application/json\'"\n'
    )


def generate_mediamtx_config(cfg: Config, bin_path: str = MEDIAMTX_BIN) -> str:
    """Generate a version-appropriate mediamtx config string."""
    major, _ = _detect_version(bin_path)
    return _gen_config_v1(cfg) if major >= 1 else _gen_config_v0(cfg)


# ---------------------------------------------------------------------------
#  Process manager
# ---------------------------------------------------------------------------

class MediamtxManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._config_file: Optional[str] = None
        self._log_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Generate config, write to temp file, and start mediamtx."""
        config_content = generate_mediamtx_config(self.cfg, MEDIAMTX_BIN)

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
        self._log_task = asyncio.create_task(self._log_output())
        log.info("mediamtx started (pid=%d)", self._proc.pid)

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Poll mediamtx API until it responds or the process dies."""
        url = f"http://127.0.0.1:{self.cfg.mediamtx_api_port}/v3/paths/list"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._proc and self._proc.returncode is not None:
                log.error(
                    "mediamtx exited with code %d before becoming ready",
                    self._proc.returncode,
                )
                return False
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
        """Terminate mediamtx and clean up the temp config file."""
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        if self._config_file and os.path.exists(self._config_file):
            os.unlink(self._config_file)

    async def _log_output(self) -> None:
        """Read and log mediamtx stdout/stderr lines."""
        if not self._proc or not self._proc.stdout:
            return
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").rstrip()
            level = logging.WARNING if "ERR" in decoded else logging.DEBUG
            log.log(level, "[mediamtx] %s", decoded)
