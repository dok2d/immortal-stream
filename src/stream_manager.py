"""
Stream state manager — orchestrates compositor and output FFmpeg processes.

Stream detection: mediamtx runOnPublish/runOnUnpublish hooks POST to an
internal HTTP server, providing near-instant event delivery.

Architecture:
  External stream -> mediamtx (/live/KEY or /live)
                         | hook POST (instant)
                    StreamManager._hook_server
                         | asyncio.Queue (serialized)
                    StreamManager._event_worker
                         | manages
                    Compositor FFmpeg --UDP/MPEG-TS--> Output FFmpeg -> targets

  The compositor→output link uses UDP/MPEG-TS (connectionless).
  Compositor restarts cause a brief pause (~1 s) in the UDP stream,
  but the output FFmpeg keeps running — no reconnection, no signal
  loss on YouTube/Twitch/Telegram.

Redundancy:
  Multiple input sources can be configured with priority ordering.
  The compositor always uses the highest-priority active stream.
  When that stream drops, the system automatically fails over to the
  next available source; the output FFmpeg connection is never interrupted.
"""
import asyncio
import http.server
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List

from config import Config
from ffmpeg_cmd import (
    build_compositor_idle, build_compositor_live, build_compositor_audio_only,
    build_output, file_has_audio, prepare_image,
)

log = logging.getLogger("stream_manager")

PROBE_TIMEOUT = 8.0       # seconds to wait for ffprobe
OUTPUT_RETRY_LIMIT = 3    # max retries for output FFmpeg quick failures
OUTPUT_QUICK_FAIL = 15.0  # seconds — if output exits faster than this, it's a quick fail


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
        # All fully-probed active streams, including standby sources
        self._active_streams: Dict[str, StreamInfo] = {}  # path -> StreamInfo
        # Event queue serializes hook callbacks to avoid race conditions
        self._event_queue: asyncio.Queue = asyncio.Queue()
        # Managed background tasks (stored to prevent GC and catch exceptions)
        self._tasks: List[asyncio.Task] = []
        self._hook_server: Optional[asyncio.Server] = None

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    @property
    def is_paused(self) -> bool:
        """True when all processes have been stopped via pause_all()."""
        return not self._running

    async def start(self) -> None:
        self._running = True
        log.info("Starting compositor in IDLE mode")
        await self._start_compositor_idle()
        log.info("Starting output FFmpeg (persistent)")
        await self._start_output()
        await self._start_hook_server()
        self._spawn_task(self._event_worker(), "event-worker")
        self._spawn_task(self._watchdog(), "watchdog")

    async def stop(self) -> None:
        self._running = False
        if self._hook_server:
            self._hook_server.close()
            await self._hook_server.wait_closed()
            self._hook_server = None
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for proc in (self._compositor, self._output):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()

    async def pause_all(self) -> None:
        """Stop compositor & output, pause event processing and watchdog.

        The service enters a fully stopped state.  Use resume_all() to
        restart everything.
        """
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._terminate_process(self._output)
        self._output = None
        await self._terminate_process(self._compositor)
        self._compositor = None
        log.info("All processes paused")

    async def resume_all(self) -> None:
        """Restart compositor, output, event processing and watchdog."""
        self._running = True
        if self._current_stream:
            await self._start_compositor_live(self._current_stream)
        else:
            await self._start_compositor_idle()
        await self._start_output()
        self._spawn_task(self._event_worker(), "event-worker")
        self._spawn_task(self._watchdog(), "watchdog")
        log.info("All processes resumed")

    def _spawn_task(self, coro, name: str) -> asyncio.Task:
        """Create a tracked background task with exception logging."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks = [t for t in self._tasks if t is not task]
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Background task %r crashed: %s", task.get_name(), exc)

    # ------------------------------------------------------------------ #
    #  Hook HTTP server — receives mediamtx runOnPublish/runOnUnpublish   #
    # ------------------------------------------------------------------ #

    async def _start_hook_server(self) -> None:
        """Start a minimal HTTP server for mediamtx hooks."""
        loop = asyncio.get_event_loop()

        async def _handle_request(reader, writer):
            try:
                raw = b""
                # Read HTTP request line + headers
                while True:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=5
                    )
                    raw += line
                    if line == b"\r\n" or line == b"\n" or not line:
                        break

                request_line = raw.split(b"\r\n")[0].decode(errors="replace")
                parts = request_line.split()
                method = parts[0] if parts else ""
                path = parts[1] if len(parts) > 1 else ""

                # Parse Content-Length
                content_length = 0
                for header_line in raw.decode(errors="replace").split("\r\n"):
                    if header_line.lower().startswith("content-length:"):
                        content_length = int(header_line.split(":", 1)[1].strip())

                body = b""
                if content_length > 0:
                    body = await asyncio.wait_for(
                        reader.readexactly(content_length), timeout=5
                    )

                status = "200 OK"
                if method == "POST" and path in ("/on_publish", "/on_unpublish"):
                    try:
                        data = json.loads(body) if body else {}
                        event_type = "publish" if path == "/on_publish" else "unpublish"
                        await self._event_queue.put((event_type, data))
                        log.debug("Hook event queued: %s %s", event_type, data)
                    except json.JSONDecodeError:
                        status = "400 Bad Request"
                        log.warning("Hook: invalid JSON body")
                else:
                    status = "404 Not Found"

                response = (
                    f"HTTP/1.1 {status}\r\n"
                    f"Content-Length: 0\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                )
                writer.write(response.encode())
                await writer.drain()
            except Exception as e:
                log.debug("Hook request error: %s", e)
            finally:
                writer.close()

        port = self.cfg.hook_server_port
        self._hook_server = await asyncio.start_server(
            _handle_request, "127.0.0.1", port,
        )
        log.info("Hook server listening on 127.0.0.1:%d", port)

    # ------------------------------------------------------------------ #
    #  Event worker — serialized processing of hook events                #
    # ------------------------------------------------------------------ #

    async def _event_worker(self) -> None:
        """Process stream events from the queue serially.

        Serialization through a single worker eliminates race conditions
        between concurrent publish/unpublish events.
        """
        while self._running:
            try:
                event_type, data = await asyncio.wait_for(
                    self._event_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            path = data.get("path", "")
            if not self._is_ingest_path(path):
                log.debug("Ignoring non-ingest hook event for path=%s", path)
                continue

            try:
                if event_type == "publish":
                    conn_type = data.get("conn_type", "unknown")
                    conn_id = data.get("conn_id", "")
                    remote_addr = _extract_remote_addr_from_id(conn_id)
                    await self._on_stream_start(
                        path, conn_type, conn_id, remote_addr
                    )
                elif event_type == "unpublish":
                    await self._on_stream_stop(path)
            except Exception:
                log.exception("Error processing %s event for path=%s",
                              event_type, path)

    def _is_ingest_path(self, name: str) -> bool:
        """True for paths that correspond to external ingest streams."""
        if name == self.cfg.composite_path or name.startswith("_c"):
            return False

        sources = self.cfg.ingest.redundant_sources
        if sources:
            allowed = {f"live/{s}" if s else "live" for s in sources}
            return name in allowed

        if self.cfg.ingest.stream_key_required and self.cfg.ingest.allowed_key:
            return name == f"live/{self.cfg.ingest.allowed_key}"

        return name.startswith("live")

    # ------------------------------------------------------------------ #
    #  Priority selection                                                  #
    # ------------------------------------------------------------------ #

    def _select_best_stream(self) -> Optional[str]:
        """Return the path of the highest-priority active stream.

        When redundant_sources is configured, priority follows the list
        order (index 0 = highest).  Otherwise, any active stream is
        returned (first-come first-served legacy behaviour).
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
        """Human-readable priority rank, e.g. '[#1 primary]'."""
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

    async def _on_stream_start(
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

            best_path = self._select_best_stream()
            if best_path != path:
                # Higher-priority stream is already compositing; standby
                self._notify_standby_connected(info)
                return

            # This is the best available stream — switch compositor to it
            old_stream = self._current_stream
            self._current_stream = info

            if info.has_audio and not info.has_video:
                # Audio-only: keep placeholder video, use incoming audio
                await self._start_compositor_audio_only(info)
            else:
                await self._start_compositor_live(info)

            if old_stream:
                self._notify_preemption(old_stream, info, remote_addr, conn_type)
            else:
                self._notify_stream_started(info, conn_id)

    async def _on_stream_stop(self, path: str) -> None:
        async with self._lock:
            self._active_streams.pop(path, None)

            if not self._current_stream or self._current_stream.path != path:
                log.info("Standby stream dropped: path=%s", path)
                self.notifier.send(
                    "\U0001f4e1 <b>Standby stream disconnected</b>\n"
                    f"Source: <code>{path}</code> "
                    f"{self._priority_label(path)}"
                )
                return

            info = self._current_stream
            duration = int(time.monotonic() - info.started_at)
            log.info("Stream stopped: path=%s duration=%ds", path, duration)

            # Try to fail over to the next best available stream
            next_path = self._select_best_stream()
            if next_path and next_path in self._active_streams:
                next_info = self._active_streams[next_path]
                self._current_stream = next_info
                if next_info.has_audio and not next_info.has_video:
                    await self._start_compositor_audio_only(next_info)
                else:
                    await self._start_compositor_live(next_info)
                log.info(
                    "Failover: %s -> %s (was active %ds)",
                    path, next_path, duration,
                )
                self.notifier.send(
                    "\U0001f504 <b>Failover</b>\n"
                    f"Lost: <code>{path}</code> "
                    f"{self._priority_label(path)} "
                    f"(after {_fmt_duration(duration)})\n"
                    f"Now using: <code>{next_path}</code> "
                    f"{self._priority_label(next_path)}\n"
                    f"Video: {next_info.codec_video} "
                    f"{next_info.width}\u00d7{next_info.height} "
                    f"@{next_info.fps}fps"
                )
            else:
                self._current_stream = None
                await self._start_compositor_idle()
                self.notifier.send(
                    "\U0001f534 <b>Stream stopped</b>\n"
                    f"Path: <code>{path}</code>\n"
                    f"Duration: {_fmt_duration(duration)}\n"
                    "Switched to placeholder"
                )

    # ------------------------------------------------------------------ #
    #  Public API (called by Telegram bot, keep signatures compatible)     #
    # ------------------------------------------------------------------ #

    async def on_stream_start(
        self, path: str, conn_type: str, conn_id: str,
        remote_addr: str = "unknown",
    ) -> None:
        """Public wrapper — enqueue a publish event."""
        await self._event_queue.put(("publish", {
            "path": path, "conn_type": conn_type,
            "conn_id": conn_id,
        }))

    async def on_stream_stop(self, path: str, conn_id: str = "") -> None:
        """Public wrapper — enqueue an unpublish event."""
        await self._event_queue.put(("unpublish", {"path": path}))

    # ------------------------------------------------------------------ #
    #  Notification helpers                                                #
    # ------------------------------------------------------------------ #

    def _notify_standby_connected(self, info: StreamInfo) -> None:
        current_label = (
            f"{self._current_stream.path} "
            f"{self._priority_label(self._current_stream.path)}"
            if self._current_stream else "placeholder"
        )
        log.info(
            "Stream %s registered as standby (current compositor: %s)",
            info.path, current_label,
        )
        self.notifier.send(
            "\U0001f4e1 <b>Standby stream connected</b>\n"
            f"Source: <code>{info.path}</code> "
            f"{self._priority_label(info.path)}\n"
            f"Remote: <code>{info.remote_addr}</code>\n"
            f"Protocol: {info.conn_type}\n"
            f"Video: {info.codec_video} "
            f"{info.width}\u00d7{info.height} @{info.fps}fps\n"
            f"Audio: {info.codec_audio if info.has_audio else 'none'}\n"
            f"Active: {current_label}"
        )

    def _notify_preemption(
        self, old: StreamInfo, new: StreamInfo,
        remote_addr: str, conn_type: str,
    ) -> None:
        log.info(
            "Higher-priority stream %s preempts %s", new.path, old.path,
        )
        self.notifier.send(
            "\U0001f504 <b>Stream switched (higher priority arrived)</b>\n"
            f"From: <code>{old.path}</code> "
            f"{self._priority_label(old.path)}\n"
            f"To: <code>{new.path}</code> "
            f"{self._priority_label(new.path)}\n"
            f"Remote: <code>{remote_addr}</code>\n"
            f"Protocol: {conn_type}\n"
            f"Video: {new.codec_video} "
            f"{new.width}\u00d7{new.height} @{new.fps}fps\n"
            f"Audio: {new.codec_audio if new.has_audio else 'none'}"
        )

    def _notify_stream_started(self, info: StreamInfo, conn_id: str) -> None:
        self.notifier.send(
            "\U0001f7e2 <b>Stream started</b>\n"
            f"Path: <code>{info.path}</code> "
            f"{self._priority_label(info.path)}\n"
            f"Remote: <code>{info.remote_addr}</code>\n"
            f"Protocol: {info.conn_type}\n"
            f"ID: <code>{conn_id}</code>\n"
            f"Video: {info.codec_video} "
            f"{info.width}\u00d7{info.height} @{info.fps}fps\n"
            f"Audio: {info.codec_audio if info.has_audio else 'none'}"
        )

    # ------------------------------------------------------------------ #
    #  Public hot-reload API (called by Telegram bot)                      #
    # ------------------------------------------------------------------ #

    async def reload_compositor(self) -> None:
        """Restart compositor with current config (placeholder/overlay changed)."""
        async with self._lock:
            if self._current_stream:
                info = self._current_stream
                if info.has_audio and not info.has_video:
                    await self._start_compositor_audio_only(info)
                else:
                    await self._start_compositor_live(info)
            else:
                await self._start_compositor_idle()

    async def reload_output(self) -> None:
        """Restart output FFmpeg (targets changed). Briefly interrupts connection."""
        await self._terminate_process(self._output)
        self._output = None
        await self._start_output()

    # ------------------------------------------------------------------ #
    #  Compositor management                                               #
    # ------------------------------------------------------------------ #

    async def _prepare_placeholder_image(self) -> Optional[str]:
        """Pre-process placeholder image (scale+pad+opacity) if applicable."""
        ph = self.cfg.placeholder
        if not ph.image_path:
            return None
        v = self.cfg.output.video
        return await prepare_image(
            ph.image_path, width=v.width, height=v.height,
            opacity=ph.image_opacity,
        )

    async def _prepare_overlay_image(self) -> Optional[str]:
        """Pre-process overlay image (resize+opacity) if applicable."""
        ov = self.cfg.overlay
        if not ov.enabled or not ov.path:
            return None
        return await prepare_image(
            ov.path,
            max_height=ov.image_max_height if ov.image_max_height > 0 else 0,
            opacity=ov.image_opacity,
        )

    async def _start_compositor_idle(self) -> None:
        try:
            ph = self.cfg.placeholder
            has_audio = (
                await file_has_audio(ph.video_path)
                if ph.video_path
                else False
            )
            ph_img = await self._prepare_placeholder_image()
            cmd = build_compositor_idle(
                self.cfg, video_has_audio=has_audio,
                placeholder_image_path=ph_img,
            )
        except Exception as e:
            log.error("Failed to build idle compositor command: %s", e)
            self.notifier.send(f"\u26a0\ufe0f Compositor build error: {e}")
            return
        await self._replace_compositor(cmd, "IDLE")

    async def _start_compositor_live(self, info: StreamInfo) -> None:
        try:
            overlay_img = await self._prepare_overlay_image()
            cmd = build_compositor_live(
                self.cfg, info.path, info.has_audio,
                overlay_image_path=overlay_img,
            )
        except Exception as e:
            log.error("Failed to build live compositor command: %s", e)
            self.notifier.send(f"\u26a0\ufe0f Compositor build error: {e}")
            return
        await self._replace_compositor(cmd, f"LIVE({info.path})")

    async def _start_compositor_audio_only(self, info: StreamInfo) -> None:
        """Audio-only mode: placeholder video layers + incoming audio."""
        try:
            ph = self.cfg.placeholder
            ph_has_audio = (
                await file_has_audio(ph.video_path)
                if ph.video_path
                else False
            )
            ph_img = await self._prepare_placeholder_image()
            cmd = build_compositor_audio_only(
                self.cfg, info.path, video_has_audio=ph_has_audio,
                placeholder_image_path=ph_img,
            )
        except Exception as e:
            log.error("Failed to build audio-only compositor: %s", e)
            self.notifier.send(f"\u26a0\ufe0f Compositor build error: {e}")
            return
        await self._replace_compositor(cmd, f"AUDIO({info.path})")

    async def _replace_compositor(self, cmd: list, label: str) -> None:
        """Replace the running compositor with a new one.

        The compositor->output link uses UDP/MPEG-TS which is
        connectionless.  The output FFmpeg simply pauses when no
        packets arrive, then resumes when the new compositor starts
        sending.  No output restart is needed.

        Sequence:
          1. Kill old compositor quickly (SIGTERM, 1 s timeout, SIGKILL).
          2. Start new compositor.
          3. Output FFmpeg sees a brief gap, then gets new packets.
        """
        old_proc = self._compositor

        # Kill old compositor fast to minimize the gap
        if old_proc and old_proc.returncode is None:
            old_proc.terminate()
            try:
                await asyncio.wait_for(old_proc.wait(), timeout=1)
            except asyncio.TimeoutError:
                old_proc.kill()

        log.info("Starting compositor [%s]: %s", label, " ".join(cmd))
        new_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._spawn_task(
            _log_stderr(new_proc, f"compositor[{label}]", level=logging.WARNING),
            f"stderr-compositor-{label}",
        )
        self._compositor = new_proc
        log.info("Compositor switched to [%s]", label)

    # ------------------------------------------------------------------ #
    #  Output FFmpeg (persistent)                                          #
    # ------------------------------------------------------------------ #

    async def _start_output(self) -> None:
        if not self.cfg.output.targets:
            log.info("No output targets configured — output FFmpeg not started")
            return

        # Guard: skip if output is already running (idempotent).
        if self._output is not None and self._output.returncode is None:
            return

        try:
            cmd = build_output(self.cfg)
        except Exception as e:
            log.error("Failed to build output command: %s", e)
            self.notifier.send(f"\u26a0\ufe0f Output FFmpeg build error: {e}")
            return

        # UDP/MPEG-TS input: no probe needed — FFmpeg blocks on the UDP
        # socket until the compositor starts sending packets.
        for retry in range(OUTPUT_RETRY_LIMIT):
            log.info("Output FFmpeg cmd: %s", " ".join(cmd))
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._output = proc
            self._spawn_task(
                _log_stderr(proc, "output", level=logging.WARNING),
                f"stderr-output-{retry}",
            )

            # Check for quick failure (exits within OUTPUT_QUICK_FAIL seconds)
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=OUTPUT_QUICK_FAIL
                )
            except asyncio.TimeoutError:
                # Still running after the grace period — success
                return

            # Process exited too quickly — retry with backoff
            rc = proc.returncode
            if retry < OUTPUT_RETRY_LIMIT - 1:
                delay = 2 ** (retry + 1)
                log.warning(
                    "Output FFmpeg exited immediately (code %d), "
                    "retrying in %ds (%d/%d)",
                    rc, delay, retry + 1, OUTPUT_RETRY_LIMIT,
                )
                await asyncio.sleep(delay)
            else:
                log.error(
                    "Output FFmpeg keeps failing (code %d) after %d retries",
                    rc, OUTPUT_RETRY_LIMIT,
                )
                self.notifier.send(
                    f"\u26a0\ufe0f <b>Output FFmpeg failed</b> (exit {rc}) "
                    f"after {OUTPUT_RETRY_LIMIT} retries"
                )

    # ------------------------------------------------------------------ #
    #  Watchdog                                                            #
    # ------------------------------------------------------------------ #

    async def _watchdog(self) -> None:
        """Monitor compositor and output health, restart on crash."""
        while self._running:
            await asyncio.sleep(5)

            compositor_crashed = (
                self._compositor is not None
                and self._compositor.returncode is not None
            )
            output_crashed = (
                self._output is not None
                and self._output.returncode is not None
            )

            if compositor_crashed:
                rc = self._compositor.returncode
                log.warning("Compositor exited (code %d) — restarting", rc)
                # Lock prevents race with _on_stream_start/stop and
                # reload_compositor running concurrently.
                async with self._lock:
                    # Re-check after acquiring lock — another task may
                    # have already restarted the compositor.
                    if (self._compositor is not None
                            and self._compositor.returncode is not None):
                        if self._current_stream:
                            info = self._current_stream
                            if info.has_audio and not info.has_video:
                                await self._start_compositor_audio_only(info)
                            else:
                                await self._start_compositor_live(info)
                        else:
                            await self._start_compositor_idle()

            if output_crashed:
                rc = self._output.returncode if self._output else -1
                log.error("Output FFmpeg exited (code %d) — restarting", rc)
                self.notifier.send(
                    f"\u26a0\ufe0f <b>Output FFmpeg crashed</b> (exit {rc})\n"
                    "Restarting\u2026"
                )
                await self._start_output()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _terminate_process(
        proc: Optional[asyncio.subprocess.Process],
    ) -> None:
        """Gracefully terminate a subprocess, falling back to kill."""
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()

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
                codec_type = s.get("codec_type")
                if codec_type == "video":
                    result["has_video"] = True
                    result["codec_video"] = s.get("codec_name", "unknown")
                    result["width"] = s.get("width", 0)
                    result["height"] = s.get("height", 0)
                    result["fps"] = _parse_fps(s.get("avg_frame_rate", "0/1"))
                elif codec_type == "audio":
                    result["has_audio"] = True
                    result["codec_audio"] = s.get("codec_name", "unknown")
            return result
        except Exception as e:
            log.warning("ffprobe error for %s: %s", url, e)
            return None


# ---------------------------------------------------------------------------
#  Module-level helpers
# ---------------------------------------------------------------------------

async def _log_stderr(
    proc: asyncio.subprocess.Process,
    label: str,
    level: int = logging.DEBUG,
) -> None:
    """Read and log stderr lines from an FFmpeg subprocess."""
    if not proc.stderr:
        return
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        # Suppress noise that carries no diagnostic value:
        # - Non-monotonic DTS: tee muxer auto-corrects timestamps
        # - deprecated pixel format: harmless swscaler format conversion
        if any(s in text for s in (
            "Non-monotonic DTS",
            "deprecated pixel format",
            "Discarding interleaved",
            "Last message repeated",
            "non-existing PPS",
            "decode_slice_header",
            "co located POCs",
            "mmco:",
            "no frame!",
            "Packet corrupt",
            "corrupt input packet",
            "out of order",
            "timestamp discontinuity",
        )) or "non monotone" in text.lower():
            continue
        log.log(
            level,
            "[ffmpeg/%s] %s",
            label,
            text,
        )


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _parse_fps(rate: str) -> str:
    """Parse an avg_frame_rate fraction like '30000/1001' into '29'."""
    try:
        n, d = rate.split("/")
        return str(int(n) // int(d)) if int(d) else "unknown"
    except Exception:
        return rate


def _extract_remote_addr_from_id(conn_id: str) -> str:
    """Extract the remote IP/address from a mediamtx connection ID.

    The conn_id field typically looks like:
      "rtmpConn 192.168.1.5:52341"   or  "srtConn 10.0.0.1:9999"
    We extract the IP:port part.  Falls back to the raw id or 'unknown'.
    """
    if not conn_id:
        return "unknown"
    parts = conn_id.split()
    if len(parts) >= 2:
        return parts[-1]
    return conn_id
