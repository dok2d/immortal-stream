"""Session-based stream recorder — records raw RTSP input to file.

Records the live input stream (via mediamtx RTSP) with -c copy
(no re-encoding, zero CPU overhead).  Completely independent of the
compositor/output pipeline — errors here never affect streaming.

Recording lifecycle:
  resume_all()  → on_session_start()   (new session)
  stream LIVE   → on_stream_live(info) (start FFmpeg segment)
  stream IDLE   → on_stream_idle()     (stop FFmpeg, keep session)
  pause_all()   → on_session_end()     (finalize + send to Telegram)

Within a session, only LIVE segments are recorded.  Segments are
concatenated into a single file at finalization.
"""
import asyncio
import datetime
import logging
import os
import shutil
import time
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger("recorder")

MONITOR_INTERVAL = 10  # seconds between disk/size checks


class SessionRecorder:
    """Records live input segments and sends them to Telegram."""

    def __init__(self, cfg: "Config", notifier):
        self.cfg = cfg
        self.notifier = notifier
        self._session_id: Optional[str] = None
        self._rec_proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._segments: List[str] = []
        self._part_num: int = 0
        self._monitor_task: Optional[asyncio.Task] = None
        self._disk_full: bool = False
        self._rtsp_url: Optional[str] = None
        self._segment_start: float = 0.0
        self._session_start: float = 0.0
        # Serialization lock — prevent concurrent start/stop races
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  Public API (called from StreamManager, always try/except outside)  #
    # ------------------------------------------------------------------ #

    async def on_session_start(self) -> None:
        """Begin a new recording session (called on resume_all)."""
        if not self.cfg.recording.enabled:
            return
        async with self._lock:
            rec_dir = self.cfg.recording.directory
            os.makedirs(rec_dir, exist_ok=True)
            # Verify write permissions
            test_file = os.path.join(rec_dir, ".write_test")
            try:
                with open(test_file, "w") as f:
                    f.write("ok")
                os.unlink(test_file)
            except OSError as e:
                log.error("Recording directory not writable: %s — %s", rec_dir, e)
                self.notifier.send(
                    f"\u26a0\ufe0f <b>Recording disabled</b>: "
                    f"directory not writable\n"
                    f"<code>{rec_dir}</code>\n{e}"
                )
                return
            self._session_id = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self._segments = []
            self._part_num = 0
            self._disk_full = False
            self._session_start = time.monotonic()
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(), name="rec-monitor"
            )
            log.info("Recording session started: %s", self._session_id)

    async def on_session_end(self) -> None:
        """Finalize current session (called on pause_all)."""
        async with self._lock:
            if not self._session_id:
                return
            await self._stop_ffmpeg()
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                self._monitor_task = None
            if self._segments:
                await self._finalize_segments()
            self._session_id = None
            log.info("Recording session ended")

    async def on_stream_live(self, info) -> None:
        """Input stream went LIVE — start recording segment."""
        if not self.cfg.recording.enabled or not self._session_id:
            return
        if self._disk_full:
            log.warning("Recording skipped: disk full")
            return
        async with self._lock:
            rtsp_url = (
                f"rtsp://127.0.0.1:{self.cfg.internal_rtsp_port}/{info.path}"
            )
            self._rtsp_url = rtsp_url
            await self._start_ffmpeg(rtsp_url)

    async def on_stream_idle(self) -> None:
        """Input stream dropped — stop recording segment."""
        async with self._lock:
            await self._stop_ffmpeg()

    # ------------------------------------------------------------------ #
    #  FFmpeg process management                                          #
    # ------------------------------------------------------------------ #

    async def _start_ffmpeg(self, rtsp_url: str) -> None:
        if self._rec_proc and self._rec_proc.returncode is None:
            return  # already recording
        seg_num = len(self._segments) + 1
        seg_path = os.path.join(
            self.cfg.recording.directory,
            f"{self._session_id}_seg{seg_num:03d}.ts",
        )
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-c", "copy",
            "-f", "mpegts",
            seg_path,
        ]
        log.info("Recording cmd: %s", " ".join(cmd))
        try:
            self._rec_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            log.error("Failed to start recording FFmpeg: %s", e)
            self._rec_proc = None
            return
        self._segments.append(seg_path)
        self._segment_start = time.monotonic()
        # Spawn stderr reader so pipe doesn't fill up and errors are logged
        self._stderr_task = asyncio.create_task(
            self._read_stderr(self._rec_proc), name="rec-stderr"
        )

    @staticmethod
    async def _read_stderr(proc: asyncio.subprocess.Process) -> None:
        """Drain stderr and log each line."""
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            log.warning("rec-ffmpeg: %s", line.decode(errors="replace").rstrip())

    async def _stop_ffmpeg(self) -> None:
        proc = self._rec_proc
        if not proc:
            return
        if proc.returncode is not None:
            # Already dead — log why
            log.warning(
                "Recording FFmpeg already exited (rc=%d)", proc.returncode
            )
            self._rec_proc = None
            self._cancel_stderr_task()
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        self._rec_proc = None
        self._cancel_stderr_task()
        log.info("Recording segment stopped")

    def _cancel_stderr_task(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

    # ------------------------------------------------------------------ #
    #  Finalization: concat segments → MP4 → send to Telegram             #
    # ------------------------------------------------------------------ #

    async def _finalize_segments(self, part_label: str = "") -> None:
        """Concatenate TS segments into MP4 and send to Telegram."""
        # Filter to segments that actually exist and have content
        valid = [s for s in self._segments if os.path.isfile(s) and os.path.getsize(s) > 0]
        if not valid:
            missing = [
                s for s in self._segments if not os.path.isfile(s)
            ]
            empty = [
                s for s in self._segments
                if os.path.isfile(s) and os.path.getsize(s) == 0
            ]
            if missing:
                log.warning("Segments missing (never created): %s", missing)
            if empty:
                log.warning("Segments empty (0 bytes): %s", empty)
                for s in empty:
                    self._try_remove(s)
            self._segments = []
            return

        self._part_num += 1
        label = part_label or (
            f"part{self._part_num}" if self._part_num > 1 else "full"
        )
        mp4_name = f"{self._session_id}_{label}.mp4"
        mp4_path = os.path.join(self.cfg.recording.directory, mp4_name)

        try:
            if len(valid) == 1:
                # Single segment — remux directly
                ok = await self._remux_to_mp4(valid[0], mp4_path)
            else:
                # Multiple segments — concat then remux
                concat_ts = os.path.join(
                    self.cfg.recording.directory,
                    f"{self._session_id}_{label}_concat.ts",
                )
                ok = await self._concat_ts(valid, concat_ts)
                if ok:
                    ok = await self._remux_to_mp4(concat_ts, mp4_path)
                    self._try_remove(concat_ts)
        except Exception as e:
            log.error("Finalization failed: %s", e)
            ok = False

        # Clean up segment files
        for seg in valid:
            self._try_remove(seg)
        self._segments = []

        if not ok or not os.path.isfile(mp4_path):
            log.error("No MP4 produced, skipping Telegram send")
            return

        # Build caption
        duration = int(time.monotonic() - self._session_start)
        mins, secs = divmod(duration, 60)
        ts_str = (self._session_id or "unknown").replace("_", " ")
        size_mb = os.path.getsize(mp4_path) / (1024 * 1024)
        caption = (
            f"Recording {ts_str}\n"
            f"Duration: {mins}m {secs}s | "
            f"Size: {size_mb:.1f} MB"
        )
        if self._part_num > 1:
            caption = f"Part {self._part_num} | " + caption

        # Send to Telegram
        sent = await self.notifier.send_document(mp4_path, caption)
        if sent:
            log.info("Recording sent to Telegram: %s", mp4_name)
            self._try_remove(mp4_path)
        else:
            log.warning(
                "Failed to send recording to Telegram, keeping file: %s",
                mp4_path,
            )

    async def _concat_ts(self, segments: List[str], output: str) -> bool:
        """Concatenate TS files using FFmpeg concat protocol."""
        concat_input = "concat:" + "|".join(segments)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-y",
            "-i", concat_input,
            "-c", "copy",
            "-f", "mpegts",
            output,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                log.error("TS concat failed (rc=%d): %s", proc.returncode,
                          stderr.decode(errors="replace")[:500])
                return False
            return True
        except Exception as e:
            log.error("TS concat error: %s", e)
            return False

    async def _remux_to_mp4(self, ts_path: str, mp4_path: str) -> bool:
        """Remux TS to MP4 with faststart."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-y",
            "-i", ts_path,
            "-c", "copy",
            "-movflags", "+faststart",
            mp4_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                log.error("Remux to MP4 failed (rc=%d): %s", proc.returncode,
                          stderr.decode(errors="replace")[:500])
                return False
            return True
        except Exception as e:
            log.error("Remux error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    #  Monitor loop: disk space + file size                               #
    # ------------------------------------------------------------------ #

    async def _monitor_loop(self) -> None:
        """Periodically check disk space and current file size."""
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            try:
                await self._check_limits()
            except Exception as e:
                log.error("Recording monitor error: %s", e)

    async def _check_limits(self) -> None:
        rec_dir = self.cfg.recording.directory
        try:
            usage = shutil.disk_usage(rec_dir)
            free_mb = usage.free / (1024 * 1024)
        except OSError:
            return

        # Critical: <100MB — stop recording entirely
        if free_mb < self.cfg.recording.min_disk_stop_mb:
            if not self._disk_full:
                self._disk_full = True
                log.error(
                    "Disk space critical (%.0f MB free) — stopping recording",
                    free_mb,
                )
                self.notifier.send(
                    "\u26a0\ufe0f <b>Recording stopped</b>: "
                    f"disk space critical ({free_mb:.0f} MB free)"
                )
                async with self._lock:
                    await self._stop_ffmpeg()
                    if self._segments:
                        await self._finalize_segments()
            return

        # Low: <1GB — finalize current file, start new part
        if free_mb < self.cfg.recording.min_disk_free_mb:
            if self._rec_proc and self._rec_proc.returncode is None:
                log.warning(
                    "Disk space low (%.0f MB free) — rotating recording file",
                    free_mb,
                )
                async with self._lock:
                    await self._stop_ffmpeg()
                    if self._segments:
                        await self._finalize_segments()
                    # Restart recording if stream is still live
                    if self._rtsp_url:
                        await self._start_ffmpeg(self._rtsp_url)
            return

        # Check file size limit
        if self._segments and self._rec_proc and self._rec_proc.returncode is None:
            current_seg = self._segments[-1]
            try:
                size_mb = os.path.getsize(current_seg) / (1024 * 1024)
            except OSError:
                return
            if size_mb >= self.cfg.recording.max_file_size_mb:
                log.info(
                    "Recording file reached %.0f MB — rotating", size_mb,
                )
                async with self._lock:
                    await self._stop_ffmpeg()
                    if self._segments:
                        await self._finalize_segments()
                    if self._rtsp_url:
                        await self._start_ffmpeg(self._rtsp_url)

    # ------------------------------------------------------------------ #
    #  Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _try_remove(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass
