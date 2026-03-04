"""
mediamtx process manager.

Detects the installed mediamtx version at startup and generates a compatible
YAML configuration.  Two formats are supported:

  v1.x  (mediamtx ≥ 1.0)  — nested objects:
      rtmp:
        enabled: yes
        address: :1935

  v0.x  (rtsp-simple-server / mediamtx < 1.0)  — flat keys:
      rtmpEnabled: yes
      rtmpAddress: :1935

Version is detected by running `mediamtx --version` before the process is
started.  If detection fails, v1.x is assumed (matches the Containerfile ARG).

The compositor uses a secret random path name (cfg.composite_path) so no
per-path auth credentials are needed regardless of version.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
from typing import Optional, Tuple

from config import Config

log = logging.getLogger("mediamtx")

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")


# ── Version detection ──────────────────────────────────────────────────────────

def _detect_version(bin_path: str) -> Tuple[int, int]:
    """
    Run ``mediamtx --version`` and return (major, minor).
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
        log.warning("cannot parse mediamtx version from output: %r", text[:200])
    except FileNotFoundError:
        log.error("mediamtx binary not found at %s", bin_path)
    except Exception as e:
        log.warning("mediamtx version detection failed: %s", e)
    log.info("assuming mediamtx v1.x config format")
    return 1, 0


# ── Config generators ──────────────────────────────────────────────────────────

def _gen_config_v1(cfg: Config) -> str:
    """mediamtx ≥ v1.0 — nested object fields."""
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


def _gen_config_v0(cfg: Config) -> str:
    """rtsp-simple-server / mediamtx v0.x — flat XxxEnabled keys."""
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
        "hlsEnabled: no\n"
        "webRTCEnabled: no\n"
    )


def generate_mediamtx_config(cfg: Config, bin_path: str = MEDIAMTX_BIN) -> str:
    major, _ = _detect_version(bin_path)
    return _gen_config_v1(cfg) if major >= 1 else _gen_config_v0(cfg)


# ── Process manager ────────────────────────────────────────────────────────────

class MediamtxManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._config_file: Optional[str] = None

    async def start(self) -> None:
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
        asyncio.create_task(self._log_output())
        log.info("mediamtx started (pid=%d)", self._proc.pid)

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        """Poll mediamtx API until it responds or the process dies or timeout."""
        import urllib.request
        url = f"http://127.0.0.1:{self.cfg.mediamtx_api_port}/v3/paths/list"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            # Bail early if the process already died
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
            decoded = line.decode(errors="replace").rstrip()
            # Surface config errors at WARNING so they're visible without DEBUG
            level = logging.WARNING if "ERR" in decoded else logging.DEBUG
            log.log(level, "[mediamtx] %s", decoded)
