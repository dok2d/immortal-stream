"""FFmpeg command builders for each streaming state."""
import subprocess
from typing import List

from config import Config, VideoConfig

# Default font for drawtext when no font_path is configured.
DEFAULT_FONT = "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf"


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

def _bufsize(bitrate: str) -> str:
    """2x bitrate as bufsize string (e.g. '6000k' -> '12000k')."""
    b = bitrate.lower()
    try:
        if b.endswith("k"):
            return f"{int(b[:-1]) * 2}k"
        if b.endswith("m"):
            return f"{int(b[:-1]) * 2}m"
        return f"{int(b) * 2}"
    except ValueError:
        return bitrate


def _scale_pad(v: VideoConfig, src_label: str, dst_label: str) -> str:
    """FFmpeg filter chain: scale + pad + setsar -> dst_label."""
    return (
        f"[{src_label}]scale={v.width}:{v.height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={v.width}:{v.height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1[{dst_label}]"
    )


def _escape_drawtext(text: str) -> str:
    r"""Escape text for FFmpeg drawtext filter inside a filter_complex string.

    Characters special to drawtext (\\, ', :) AND to the filter graph
    parser (", ;, [, ]) are all backslash-escaped so no quoting wrapper
    is needed around the value.
    """
    return (
        text
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace(":", "\\:")
        .replace(";", "\\;")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _encoding_flags(cfg: Config) -> List[str]:
    """Common encoding flags for the compositor output (video + audio)."""
    v = cfg.output.video
    a = cfg.output.audio
    return [
        "-c:v", "libx264",
        "-preset", v.preset,
        "-tune", "zerolatency",
        "-g", str(v.gop),
        "-keyint_min", str(v.gop),
        "-sc_threshold", "0",
        "-b:v", v.bitrate,
        "-maxrate", v.bitrate,
        "-bufsize", _bufsize(v.bitrate),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", a.bitrate,
        "-ar", str(a.sample_rate),
        "-ac", "2",
    ]


def _composite_dest(cfg: Config) -> str:
    """RTMP URL the compositor pushes to (internal mediamtx path).

    Uses a per-run secret path name instead of auth credentials,
    avoiding mediamtx publishUser/publishPass field compatibility issues.
    """
    return f"rtmp://127.0.0.1:{cfg.internal_rtmp_port}/{cfg.composite_path}"


def _ffmpeg_base() -> List[str]:
    """Common FFmpeg prefix flags."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats"]


def _anullsrc(sample_rate: int) -> List[str]:
    """lavfi silence source input."""
    return ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]


def _file_has_audio(path: str) -> bool:
    """Quick ffprobe check for an audio stream in a local file."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=5,
        )
        return "audio" in r.stdout
    except Exception:
        return False


def _escape_tee_url(url: str) -> str:
    r"""Escape special characters in a URL for FFmpeg tee muxer.

    The tee muxer uses \, [, ], and | as metacharacters.
    These must be backslash-escaped when they appear in target URLs.
    """
    return (
        url
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


# ---------------------------------------------------------------------------
#  Compositor: IDLE state
# ---------------------------------------------------------------------------

def build_compositor_idle(cfg: Config) -> List[str]:
    """Compositor command for IDLE state (no incoming stream).

    Outputs placeholder (black / image / video / text) to the internal
    composite path.
    """
    v = cfg.output.video
    a = cfg.output.audio
    ph = cfg.placeholder
    dest = _composite_dest(cfg)

    cmd = _ffmpeg_base()
    filters: List[str] = []

    if ph.type == "black":
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
        cmd += _anullsrc(a.sample_rate)
        cmd += ["-map", "0:v", "-map", "1:a"]
        cmd += _encoding_flags(cfg)
        cmd += ["-f", "flv", dest]
        return cmd

    if ph.type == "testcard":
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"testsrc2=s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
        cmd += _anullsrc(a.sample_rate)

        font = ph.font_path or DEFAULT_FONT
        # Two escaping levels in -filter_complex:
        #   1. Filter graph parser: \\  →  \   (strips one backslash)
        #   2. Option parser:       \:  →  :   (escaped colon, not separator)
        # So \\: in the string becomes \: after level 1, then : after level 2.
        # Single quotes do NOT work here — the filter graph parser strips them,
        # leaving colons unprotected for the option parser.
        time_text = "text=%{localtime\\\\:%H\\\\:%M\\\\:%S}"
        opts = [
            f"fontfile={font}",
            time_text,
            "fontsize=96",
            "fontcolor=white",
            "borderw=4",
            "bordercolor=black@0.8",
            "x=(w-text_w)/2",
            "y=h-text_h-60",
            "box=1",
            "boxcolor=black@0.5",
            "boxborderw=12",
        ]
        filters.append(f"[0:v]drawtext={':'.join(opts)}[vout]")

    elif ph.type == "text":
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
        cmd += _anullsrc(a.sample_rate)

        escaped = _escape_drawtext(ph.text)
        font = ph.font_path or DEFAULT_FONT
        x_expr = str(ph.x) if ph.x else "(w-text_w)/2"
        y_expr = str(ph.y) if ph.y else "(h-text_h)/2"
        opts = [
            f"fontfile={font}",
            f"text={escaped}",
            f"x={x_expr}",
            f"y={y_expr}",
            f"fontsize={ph.font_size}",
            f"fontcolor={ph.font_color}@{ph.opacity:.3f}",
            "borderw=2",
            "bordercolor=white",
        ]
        filters.append(f"[0:v]drawtext={':'.join(opts)}[vout]")

    elif ph.type == "image":
        cmd += ["-re", "-loop", "1", "-i", ph.path]
        cmd += _anullsrc(a.sample_rate)
        filters.append(_scale_pad(v, "0:v", "vscaled"))
        if ph.opacity < 1.0:
            filters.append(
                f"[vscaled]format=rgba,"
                f"colorchannelmixer=aa={ph.opacity:.3f}[vout]"
            )
        else:
            filters.append("[vscaled]copy[vout]")

    elif ph.type == "video":
        cmd += ["-re", "-stream_loop", "-1", "-i", ph.path]
        filters.append(_scale_pad(v, "0:v", "vout"))

        if _file_has_audio(ph.path):
            # Use the video's native audio, resampled to output settings
            filters.append(
                f"[0:a]aresample={a.sample_rate},"
                f"aformat=channel_layouts=stereo[aout]"
            )
            cmd += ["-filter_complex", ";".join(filters)]
            cmd += ["-map", "[vout]", "-map", "[aout]"]
        else:
            cmd += _anullsrc(a.sample_rate)
            cmd += ["-filter_complex", ";".join(filters)]
            cmd += ["-map", "[vout]", "-map", "1:a"]

        cmd += _encoding_flags(cfg)
        cmd += ["-f", "flv", dest]
        return cmd

    else:
        raise ValueError(f"Unknown placeholder.type: {ph.type!r}")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "1:a"]
    cmd += _encoding_flags(cfg)
    cmd += ["-f", "flv", dest]
    return cmd


# ---------------------------------------------------------------------------
#  Compositor: LIVE state
# ---------------------------------------------------------------------------

def build_compositor_live(
    cfg: Config,
    stream_path: str,
    has_audio: bool,
) -> List[str]:
    """Compositor command for LIVE state.

    Reads incoming stream from mediamtx (RTSP), applies optional overlay,
    and outputs to the internal composite path.
    """
    v = cfg.output.video
    a = cfg.output.audio
    ov = cfg.overlay
    dest = _composite_dest(cfg)

    stream_url = f"rtsp://127.0.0.1:{cfg.internal_rtsp_port}/{stream_path}"

    cmd = _ffmpeg_base() + ["-rtsp_transport", "tcp", "-i", stream_url]

    filters: List[str] = []
    input_idx = 1
    last_v = "vscaled"

    # Scale/pad incoming to output resolution
    filters.append(_scale_pad(v, "0:v", "vscaled"))

    # Overlay (only in LIVE mode)
    if ov.enabled:
        if ov.type == "image" and ov.path:
            cmd += ["-loop", "1", "-i", ov.path]
            alpha = (
                f",colorchannelmixer=aa={ov.opacity:.3f}"
                if ov.opacity < 1.0 else ""
            )
            filters.append(f"[{input_idx}:v]format=rgba{alpha}[ov_img]")
            filters.append(
                f"[{last_v}][ov_img]overlay={ov.x}:{ov.y}[vwith_ov]"
            )
            last_v = "vwith_ov"
            input_idx += 1

        elif ov.type == "text" and ov.text:
            escaped = _escape_drawtext(ov.text)
            font = ov.font_path or DEFAULT_FONT
            opts = [
                f"fontfile={font}",
                f"text={escaped}",
                f"x={ov.x}",
                f"y={ov.y}",
                f"fontsize={ov.font_size}",
                f"fontcolor={ov.font_color}@{ov.opacity:.3f}",
                "borderw=2",
                "bordercolor=white",
            ]
            filters.append(
                f"[{last_v}]drawtext={':'.join(opts)}[vwith_text]"
            )
            last_v = "vwith_text"

    filters.append(f"[{last_v}]copy[vout]")

    # Audio: use incoming if available, otherwise generate silence
    if has_audio:
        filters.append(
            f"[0:a]aresample={a.sample_rate},"
            f"aformat=channel_layouts=stereo[aout]"
        )
    else:
        cmd += _anullsrc(a.sample_rate)
        filters.append(
            f"[{input_idx}:a]aformat=sample_rates={a.sample_rate}:"
            f"channel_layouts=stereo[aout]"
        )

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += _encoding_flags(cfg)
    cmd += ["-f", "flv", dest]
    return cmd


# ---------------------------------------------------------------------------
#  Output FFmpeg (persistent)
# ---------------------------------------------------------------------------

def build_output(cfg: Config) -> List[str]:
    """Output FFmpeg — reads the compositor output from mediamtx (RTSP),
    writes to all configured RTMP targets.

    This process NEVER restarts during normal operation; it holds the
    persistent RTMP connection to the target services.

    Brief interruptions during compositor restarts (< 2 s) are absorbed
    by the internal RTSP relay.  If the compositor crashes, the watchdog
    restarts both processes.
    """
    relay_url = (
        f"rtsp://127.0.0.1:{cfg.internal_rtsp_port}/{cfg.composite_path}"
    )
    targets = cfg.output.targets

    if not targets:
        raise ValueError("output.targets must not be empty")

    cmd = _ffmpeg_base() + [
        "-fflags", "+genpts",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-use_wallclock_as_timestamps", "1",
        "-i", relay_url,
    ]

    # Explicit -map 0 ensures all streams from the RTSP source are
    # forwarded.  Without it the tee muxer may report "Output file does
    # not contain any stream" when auto-selection fails.
    cmd += ["-map", "0", "-c", "copy"]
    # Shift timestamps so they start at zero.  This avoids non-monotonic
    # DTS warnings in the tee muxer when the compositor restarts and the
    # RTSP source delivers packets with a fresh timestamp epoch.
    cmd += ["-avoid_negative_ts", "make_zero"]

    if len(targets) == 1:
        cmd += ["-f", "flv", targets[0]]
    else:
        tee_str = "|".join(
            f"[f=flv:onfail=ignore]{_escape_tee_url(t)}" for t in targets
        )
        cmd += ["-f", "tee", tee_str]

    return cmd
