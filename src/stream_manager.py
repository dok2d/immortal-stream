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

Redundancy:
  Multiple input sources can be configured with priority ordering.
  The compositor always uses the highest-priority active stream.
  When that stream drops, the system automatically fails over to the
  next available source; the output FFmpeg connection is never interrupted.
"""
import asyncio
import json
import logging
import time
import urllib.error
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
    remote_addr: str = "unknown"
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
        # Paths currently known to have an active publisher (from API poll)
        self._known_active: Dict[str, str] = {}  # path → conn_id
        # All fully-probed active streams, including standby sources
        self._active_streams: Dict[str, StreamInfo] = {}  # path → StreamInfo

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        # Compositor MUST start first so it registers the composite path in
        # mediamtx before the output FFmpeg tries to read from it.
        # _start_compositor_idle internally waits COMPOSITOR_GRACE seconds,
        # which is enough for FFmpeg to connect to mediamtx and push the
        # first frame (path becomes ready).
        log.info("Starting compositor in IDLE mode")
        await self._start_compositor_idle()
        log.info("Starting output FFmpeg (persistent)")
        await self._start_output()
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
        Poll the mediamtx paths API every POLL_INTERVAL seconds.

        Tries /v3/paths/list first (mediamtx v1.x); falls back to
        /v1/paths/list (rtsp-simple-server / mediamtx v0.x) on 404.

        Response formats handled:
          v1.x  items = list  [{"name":…, "ready": true, "source":{…}}]
          v0.x  items = dict  {"path_name": {"sourceReady": true, …}}
        """
        base = f"http://127.0.0.1:{self.cfg.mediamtx_api_port}"
        api_url = f"{base}/v3/paths/list"

        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(api_url, timeout=2).read(),
                )
                data = json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code == 404 and "v3" in api_url:
                    api_url = f"{base}/v1/paths/list"
                    log.info("mediamtx API v3 not found, switched to v1")
                else:
                    log.debug("mediamtx API poll error: %s", e)
                continue
            except Exception as e:
                log.debug("mediamtx API poll error: %s", e)
                continue

            # Normalise both list (v1) and dict (v0) item formats
            active_now: Dict[str, dict] = {}
            raw_items = data.get("items", [])

            if isinstance(raw_items, dict):
                # v0.x: {"path_name": {"sourceReady": true, "source": {…}}}
                for name, item in raw_items.items():
                    if not self._is_ingest_path(name):
                        continue
                    if item.get("sourceReady") or (item.get("ready") and item.get("source")):
                        src = item.get("source") or {}
                        active_now[name] = {
                            "conn_type": src.get("type", "unknown"),
                            "conn_id": src.get("id", ""),
                            "remote_addr": _extract_remote_addr(src),
                        }
            else:
                # v1.x: [{"name": "…", "ready": true, "source": {…}}]
                for item in raw_items:
                    name: str = item.get("name", "")
                    if not self._is_ingest_path(name):
                        continue
                    if item.get("ready") and item.get("source"):
                        src = item["source"]
                        active_now[name] = {
                            "conn_type": src.get("type", "unknown"),
                            "conn_id": src.get("id", ""),
                            "remote_addr": _extract_remote_addr(src),
                        }

            # Detect new streams
            for path, info in active_now.items():
                if path not in self._known_active:
                    self._known_active[path] = info["conn_id"]
                    asyncio.create_task(
                        self.on_stream_start(
                            path, info["conn_type"], info["conn_id"],
                            info["remote_addr"],
                        )
                    )

            # Detect stopped streams
            for path in list(self._known_active):
                if path not in active_now:
                    conn_id = self._known_active.pop(path)
                    asyncio.create_task(self.on_stream_stop(path, conn_id))

    def _is_ingest_path(self, name: str) -> bool:
        """True for paths that correspond to external ingest streams."""
        # Always ignore the internal compositor path
        if name == self.cfg.composite_path or name.startswith("_c"):
            return False

        sources = self.cfg.ingest.redundant_sources
        if sources:
            # In redundancy mode, only track explicitly configured source paths
            allowed = {f"live/{s}" if s else "live" for s in sources}
            return name in allowed

        # Stream key filtering: only accept streams with the correct key
        if self.cfg.ingest.stream_key_required and self.cfg.ingest.allowed_key:
            return name == f"live/{self.cfg.ingest.allowed_key}"

        return name.startswith("live")

    # ------------------------------------------------------------------ #
    #  Priority selection                                                  #
    # ------------------------------------------------------------------ #

    def _select_best_stream(self) -> Optional[str]:
        """
        Return the path of the highest-priority active stream.

        When redundant_sources is configured, priority follows the list order
        (index 0 = highest priority).  When not configured, any active stream
        is returned (first-come first-served legacy behaviour).
        """
        sources = self.cfg.ingest.redundant_sources
        if not sources:
            return next(iter(self._active_streams), None)

        for source_key in sources:
            path = f"live/{source_key}" if source_key else "live"
            if path in self._active_streams:
                return path
        return None

    def _priority_label(self, path: str) -> str:
        """Human-readable priority rank for a path, e.g. '#1 (primary)'."""
        sources = self.cfg.ingest.redundant_sources
        if not sources:
            return ""
        for idx, key in enumerate(sources, start=1):
            candidate = f"live/{key}" if key else "live"
            if candidate == path:
                return f"[#{idx} {key or 'default'}]"
        return ""

    # ------------------------------------------------------------------ #
    #  Stream state transitions                                            #
    # ------------------------------------------------------------------ #

    async def on_stream_start(
        self, path: str, conn_type: str, conn_id: str,
        remote_addr: str = "unknown",
    ) -> None:
        async with self._lock:
            info = StreamInfo(
                path=path, conn_type=conn_type, conn_id=conn_id,
                remote_addr=remote_addr,
            )
            log.info(
                "Stream started: path=%s type=%s id=%s remote=%s",
                path, conn_type, conn_id, remote_addr,
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

            self._active_streams[path] = info

            # Is this stream the highest-priority active source?
            best_path = self._select_best_stream()
            if best_path != path:
                # A higher-priority stream is already compositing; park this
                # one as a standby — it will be promoted automatically if the
                # current stream drops.
                current_label = (
                    f"{self._current_stream.path} "
                    f"{self._priority_label(self._current_stream.path)}"
                    if self._current_stream else "placeholder"
                )
                log.info(
                    "Stream %s registered as standby (current compositor: %s)",
                    path, current_label,
                )
                self.notifier.send(
                    "📡 <b>Standby stream connected</b>\n"
                    f"Source: <code>{path}</code> "
                    f"{self._priority_label(path)}\n"
                    f"Remote: <code>{remote_addr}</code>\n"
                    f"Protocol: {conn_type}\n"
                    f"Video: {info.codec_video} "
                    f"{info.width}×{info.height} @{info.fps}fps\n"
                    f"Audio: {info.codec_audio if info.has_audio else 'none'}\n"
                    f"Active: {current_label}"
                )
                return

            # This is the best available stream — switch compositor to it
            old_stream = self._current_stream
            self._current_stream = info
            await self._start_compositor_live(info)

            if old_stream:
                # Higher-priority source arrived — preempt the current one
                log.info(
                    "Higher-priority stream %s preempts %s",
                    path, old_stream.path,
                )
                self.notifier.send(
                    "🔄 <b>Stream switched (higher priority arrived)</b>\n"
                    f"From: <code>{old_stream.path}</code> "
                    f"{self._priority_label(old_stream.path)}\n"
                    f"To: <code>{path}</code> "
                    f"{self._priority_label(path)}\n"
                    f"Remote: <code>{remote_addr}</code>\n"
                    f"Protocol: {conn_type}\n"
                    f"Video: {info.codec_video} "
                    f"{info.width}×{info.height} @{info.fps}fps\n"
                    f"Audio: {info.codec_audio if info.has_audio else 'none'}"
                )
            else:
                self.notifier.send(
                    "🟢 <b>Stream started</b>\n"
                    f"Path: <code>{path}</code> "
                    f"{self._priority_label(path)}\n"
                    f"Remote: <code>{remote_addr}</code>\n"
                    f"Protocol: {conn_type}\n"
                    f"ID: <code>{conn_id}</code>\n"
                    f"Video: {info.codec_video} "
                    f"{info.width}×{info.height} @{info.fps}fps\n"
                    f"Audio: {info.codec_audio if info.has_audio else 'none'}"
                )

    async def on_stream_stop(self, path: str, conn_id: str) -> None:
        async with self._lock:
            self._active_streams.pop(path, None)

            if not self._current_stream or self._current_stream.path != path:
                # A standby source dropped — no compositor change needed
                log.info("Standby stream dropped: path=%s", path)
                self.notifier.send(
                    "📡 <b>Standby stream disconnected</b>\n"
                    f"Source: <code>{path}</code> "
                    f"{self._priority_label(path)}"
                )
                return

            info = self._current_stream
            duration = int(time.monotonic() - info.started_at)
            log.info(
                "Stream stopped: path=%s duration=%ds", path, duration
            )

            # Try to fail over to the next best available stream
            next_path = self._select_best_stream()
            if next_path and next_path in self._active_streams:
                next_info = self._active_streams[next_path]
                self._current_stream = next_info
                await self._start_compositor_live(next_info)
                log.info(
                    "Failover: %s → %s (was active %ds)", path, next_path, duration
                )
                self.notifier.send(
                    "🔄 <b>Failover</b>\n"
                    f"Lost: <code>{path}</code> "
                    f"{self._priority_label(path)} "
                    f"(after {_fmt_duration(duration)})\n"
                    f"Now using: <code>{next_path}</code> "
                    f"{self._priority_label(next_path)}\n"
                    f"Video: {next_info.codec_video} "
                    f"{next_info.width}×{next_info.height} @{next_info.fps}fps"
                )
            else:
                self._current_stream = None
                await self._start_compositor_idle()
                self.notifier.send(
                    "🔴 <b>Stream stopped</b>\n"
                    f"Path: <code>{path}</code>\n"
                    f"Duration: {_fmt_duration(duration)}\n"
                    "Switched to placeholder"
                )

    # ------------------------------------------------------------------ #
    #  Public hot-reload API (called by Telegram bot)                      #
    # ------------------------------------------------------------------ #

    async def reload_compositor(self) -> None:
        """Restart compositor with the current config (placeholder/overlay changed)."""
        async with self._lock:
            if self._current_stream:
                await self._start_compositor_live(self._current_stream)
            else:
                await self._start_compositor_idle()

    async def reload_output(self) -> None:
        """Restart output FFmpeg (targets changed). Briefly interrupts connection."""
        if self._output and self._output.returncode is None:
            self._output.terminate()
            try:
                await asyncio.wait_for(self._output.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._output.kill()
        await self._start_output()

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
        if not self.cfg.output.targets:
            log.info("No output targets configured — output FFmpeg not started")
            return

        # Wait for compositor RTSP source to have streams before launching
        relay_url = f"rtsp://127.0.0.1:{self.cfg.internal_rtsp_port}/{self.cfg.composite_path}"
        for attempt in range(1, 6):
            info = await self._probe(relay_url)
            if info and info.get("has_video"):
                break
            log.info("Waiting for compositor RTSP source (%d/5)…", attempt)
            await asyncio.sleep(2)
        else:
            log.warning("Compositor RTSP source still not ready — starting output anyway")

        try:
            cmd = build_output(self.cfg)
        except Exception as e:
            log.error("Failed to build output command: %s", e)
            self.notifier.send(f"⚠️ Output FFmpeg build error: {e}")
            return
        log.info("Output FFmpeg cmd: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._output = proc
        asyncio.create_task(self._log_stderr(proc, "output", level=logging.WARNING))

    # ------------------------------------------------------------------ #
    #  Watchdog                                                            #
    # ------------------------------------------------------------------ #

    async def _watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(5)

            # Compositor FIRST: restarting it waits COMPOSITOR_GRACE so the
            # path is registered in mediamtx before we (re)start the output.
            # If both crashed simultaneously, this ordering ensures the output
            # restart always finds a live path — no 400 Bad Request.
            if self._compositor and self._compositor.returncode is not None:
                rc = self._compositor.returncode
                log.warning("Compositor exited (code %d) — restarting", rc)
                if self._current_stream:
                    await self._start_compositor_live(self._current_stream)
                else:
                    await self._start_compositor_idle()

            if self._output and self._output.returncode is not None:
                rc = self._output.returncode
                log.error("Output FFmpeg exited (code %d) — restarting", rc)
                self.notifier.send(
                    f"⚠️ <b>Output FFmpeg crashed</b> (exit {rc})\n"
                    "Restarting…"
                )
                await self._start_output()

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
        proc: asyncio.subprocess.Process, label: str,
        level: int = logging.DEBUG,
    ) -> None:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            log.log(
                level,
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


def _extract_remote_addr(source: dict) -> str:
    """Extract the remote IP/address from a mediamtx API source object.

    The source 'id' field typically looks like:
      "rtmpConn 192.168.1.5:52341"   or  "srtConn 10.0.0.1:9999"
    We extract the IP:port part.  Falls back to the raw id or 'unknown'.
    """
    raw_id = source.get("id", "")
    if not raw_id:
        return "unknown"
    # Try to extract the address part after the type prefix
    parts = raw_id.split()
    if len(parts) >= 2:
        return parts[-1]
    return raw_id
