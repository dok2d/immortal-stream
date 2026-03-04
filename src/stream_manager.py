"""
Stream state manager — orchestrates compositor and output FFmpeg processes.

Stream detection: polls the mediamtx HTTP API every second.
This avoids runOnPublish/runOnUnpublish hook compatibility issues.

Architecture:
  External stream → mediamtx (/live/KEY or /live)
                         ↓ API poll (every 1 s)
                    StreamManager
                         ↓ manages
                    Compositor FFmpeg → mediamtx /composite
                                              ↓ internal relay
                                        mediamtx /relay
                                              ↓ (never restarts)
                                        Output FFmpeg → target services
"""
import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Dict

from config import Config
from ffmpeg_cmd import build_compositor_idle, build_compositor_live, build_output

log = logging.getLogger("stream_manager")

PROBE_TIMEOUT = 8.0       # seconds to wait for ffprobe
COMPOSITOR_GRACE = 2.0    # seconds for new compositor to connect before killing old
POLL_INTERVAL = 1.0       # seconds between mediamtx API polls


@dataclass
class StreamInfo:
    path: str
    conn_type: str
    conn_id: str
    has_audio: bool = False
    has_video: bool = True
    codec_video: str = "unknown"
    codec_audio: str = "n/a"
    width: int = 0
    height: int = 0
    fps: str = "unknown"
    started_at: float = field(default_factory=time.monotonic)


class StreamManager:
    def __init__(self, cfg: Config, notifier):
        self.cfg = cfg
        self.notifier = notifier
        self._compositor: Optional[asyncio.subprocess.Process] = None
        self._output: Optional[asyncio.subprocess.Process] = None
        self._current_stream: Optional[StreamInfo] = None
        self._lock = asyncio.Lock()
        self._running = False
        # Paths currently known to have an active publisher
        self._known_active: Dict[str, str] = {}  # path → conn_id

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        log.info("Starting output FFmpeg (persistent)")
        await self._start_output()
        log.info("Starting compositor in IDLE mode")
        await self._start_compositor_idle()
        asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        self._running = False
        for proc in (self._compositor, self._output):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

    # ------------------------------------------------------------------ #
    #  mediamtx API polling — stream detection                            #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        """
        Poll the mediamtx /v3/paths/list API every POLL_INTERVAL seconds.
        Detect publisher connect/disconnect and call on_stream_start/stop.
        """
        api_url = (
            f"http://127.0.0.1:{self.cfg.mediamtx_api_port}/v3/paths/list"
        )
        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(api_url, timeout=2).read(),
                )
                data = json.loads(raw)
            except Exception as e:
                log.debug("mediamtx API poll error: %s", e)
                continue

            # Build the current set of active ingest paths
            # (exclude internal paths: composite, relay)
            active_now: Dict[str, dict] = {}
            for item in data.get("items", []):
                name: str = item.get("name", "")
                if not self._is_ingest_path(name):
                    continue
                if item.get("ready", False) and item.get("source"):
                    src = item["source"]
                    active_now[name] = {
                        "conn_type": src.get("type", "unknown"),
                        "conn_id": src.get("id", ""),
                    }

            # Detect new streams
            for path, info in active_now.items():
                if path not in self._known_active:
                    self._known_active[path] = info["conn_id"]
                    asyncio.create_task(
                        self.on_stream_start(
                            path, info["conn_type"], info["conn_id"]
                        )
                    )

            # Detect stopped streams
            for path in list(self._known_active):
                if path not in active_now:
                    conn_id = self._known_active.pop(path)
                    asyncio.create_task(self.on_stream_stop(path, conn_id))

    def _is_ingest_path(self, name: str) -> bool:
        """True for paths that correspond to external ingest streams."""
        if name in ("composite", "relay"):
            return False
        return name.startswith("live")

    # ------------------------------------------------------------------ #
    #  Stream state transitions                                            #
    # ------------------------------------------------------------------ #

    async def on_stream_start(
        self, path: str, conn_type: str, conn_id: str
    ) -> None:
        async with self._lock:
            if self._current_stream:
                log.warning(
                    "New stream on %s while %s is active — replacing",
                    path, self._current_stream.path,
                )

            info = StreamInfo(path=path, conn_type=conn_type, conn_id=conn_id)
            log.info(
                "Stream started: path=%s type=%s id=%s", path, conn_type, conn_id
            )

            # Give mediamtx a moment to expose the path via RTSP
            await asyncio.sleep(0.5)

            # Probe stream for codec/format details
            stream_url = (
                f"rtsp://127.0.0.1:{self.cfg.internal_rtsp_port}/{path}"
            )
            probe = await self._probe(stream_url)
            if probe:
                info.has_audio = probe["has_audio"]
                info.has_video = probe["has_video"]
                info.codec_video = probe.get("codec_video", "unknown")
                info.codec_audio = probe.get("codec_audio", "n/a")
                info.width = probe.get("width", 0)
                info.height = probe.get("height", 0)
                info.fps = probe.get("fps", "unknown")

            self._current_stream = info
            await self._start_compositor_live(info)

            self.notifier.send(
                "🟢 <b>Stream started</b>\n"
                f"Path: <code>{path}</code>\n"
                f"Protocol: {conn_type}\n"
                f"ID: <code>{conn_id}</code>\n"
                f"Video: {info.codec_video} "
                f"{info.width}×{info.height} @{info.fps}fps\n"
                f"Audio: {info.codec_audio if info.has_audio else 'none'}"
            )

    async def on_stream_stop(self, path: str, conn_id: str) -> None:
        async with self._lock:
            if not self._current_stream or self._current_stream.path != path:
                log.debug(
                    "Stop event for unknown/stale path %s, ignoring", path
                )
                return

            info = self._current_stream
            self._current_stream = None
            duration = int(time.monotonic() - info.started_at)
            log.info(
                "Stream stopped: path=%s duration=%ds", path, duration
            )

            await self._start_compositor_idle()

            self.notifier.send(
                "🔴 <b>Stream stopped</b>\n"
                f"Path: <code>{path}</code>\n"
                f"Duration: {_fmt_duration(duration)}\n"
                "Switched to placeholder"
            )

    # ------------------------------------------------------------------ #
    #  Compositor management                                               #
    # ------------------------------------------------------------------ #

    async def _start_compositor_idle(self) -> None:
        try:
            cmd = build_compositor_idle(self.cfg)
        except Exception as e:
            log.error("Failed to build idle compositor command: %s", e)
            self.notifier.send(f"⚠️ Compositor build error: {e}")
            return
        await self._replace_compositor(cmd, "IDLE")

    async def _start_compositor_live(self, info: StreamInfo) -> None:
        try:
            cmd = build_compositor_live(self.cfg, info.path, info.has_audio)
        except Exception as e:
            log.error("Failed to build live compositor command: %s", e)
            self.notifier.send(f"⚠️ Compositor build error: {e}")
            return
        await self._replace_compositor(cmd, f"LIVE({info.path})")

    async def _replace_compositor(self, cmd: list, label: str) -> None:
        """Start new compositor, wait for it to stabilise, then kill the old one."""
        log.debug("Starting compositor [%s]: %s", label, " ".join(cmd))
        new_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._log_stderr(new_proc, f"compositor[{label}]"))

        # Allow new compositor to connect and push first frames before
        # tearing down the old one (keeps the relay path continuously fed)
        await asyncio.sleep(COMPOSITOR_GRACE)

        old = self._compositor
        self._compositor = new_proc

        if old and old.returncode is None:
            old.terminate()
            try:
                await asyncio.wait_for(old.wait(), timeout=5)
            except asyncio.TimeoutError:
                old.kill()

        log.info("Compositor switched to [%s]", label)

    # ------------------------------------------------------------------ #
    #  Output FFmpeg (persistent)                                          #
    # ------------------------------------------------------------------ #

    async def _start_output(self) -> None:
        try:
            cmd = build_output(self.cfg)
        except Exception as e:
            log.error("Failed to build output command: %s", e)
            self.notifier.send(f"⚠️ Output FFmpeg build error: {e}")
            return
        log.debug("Output FFmpeg cmd: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._output = proc
        asyncio.create_task(self._log_stderr(proc, "output"))

    # ------------------------------------------------------------------ #
    #  Watchdog                                                            #
    # ------------------------------------------------------------------ #

    async def _watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(5)

            if self._output and self._output.returncode is not None:
                rc = self._output.returncode
                log.error("Output FFmpeg exited (code %d) — restarting", rc)
                self.notifier.send(
                    f"⚠️ <b>Output FFmpeg crashed</b> (exit {rc})\n"
                    "Restarting…"
                )
                await self._start_output()

            if self._compositor and self._compositor.returncode is not None:
                rc = self._compositor.returncode
                log.warning("Compositor exited (code %d) — restarting", rc)
                if self._current_stream:
                    await self._start_compositor_live(self._current_stream)
                else:
                    await self._start_compositor_idle()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _probe(self, url: str) -> Optional[dict]:
        """Run ffprobe on the URL, return a metadata dict or None."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-rtsp_transport", "tcp",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=PROBE_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("ffprobe timed out for %s", url)
                return None

            data = json.loads(stdout or b"{}")
            result: dict = {
                "has_audio": False,
                "has_video": False,
                "codec_video": "unknown",
                "codec_audio": "unknown",
                "width": 0,
                "height": 0,
                "fps": "unknown",
            }
            for s in data.get("streams", []):
                if s.get("codec_type") == "video":
                    result["has_video"] = True
                    result["codec_video"] = s.get("codec_name", "unknown")
                    result["width"] = s.get("width", 0)
                    result["height"] = s.get("height", 0)
                    r = s.get("avg_frame_rate", "0/1")
                    try:
                        n, d = r.split("/")
                        result["fps"] = (
                            str(int(n) // int(d)) if int(d) else "unknown"
                        )
                    except Exception:
                        result["fps"] = r
                elif s.get("codec_type") == "audio":
                    result["has_audio"] = True
                    result["codec_audio"] = s.get("codec_name", "unknown")
            return result
        except Exception as e:
            log.warning("ffprobe error for %s: %s", url, e)
            return None

    @staticmethod
    async def _log_stderr(
        proc: asyncio.subprocess.Process, label: str
    ) -> None:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            log.debug(
                "[ffmpeg/%s] %s",
                label,
                line.decode(errors="replace").rstrip(),
            )


def _fmt_duration(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
