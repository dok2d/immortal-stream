"""FFmpeg command builders for each streaming state."""
from typing import List
from config import Config


def _bufsize(bitrate: str) -> str:
    """2× bitrate as bufsize string (e.g. '6000k' → '12000k')."""
    try:
        b = bitrate.lower()
        if b.endswith("k"):
            return f"{int(b[:-1]) * 2}k"
        if b.endswith("m"):
            return f"{int(b[:-1]) * 2}m"
        return f"{int(b) * 2}"
    except ValueError:
        return bitrate


def _scale_pad(v, src_label: str, dst_label: str) -> str:
    """FFmpeg filter chain: scale + pad + setsar → dst_label."""
    return (
        f"[{src_label}]scale={v.width}:{v.height}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={v.width}:{v.height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1[{dst_label}]"
    )


def _output_flags(cfg: Config, dest: str) -> List[str]:
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
        "-f", "flv",
        dest,
    ]


def _composite_dest(cfg: Config) -> str:
    """
    RTMP URL the compositor pushes to (internal mediamtx path).
    Uses a per-run secret path name instead of auth credentials,
    avoiding mediamtx publishUser/publishPass field compatibility issues.
    """
    return f"rtmp://127.0.0.1:{cfg.internal_rtmp_port}/{cfg.composite_path}"


def build_compositor_idle(cfg: Config) -> List[str]:
    """
    Compositor command for IDLE state (no incoming stream).
    Outputs placeholder (black / image / video / text) to the internal
    composite path.
    """
    v = cfg.output.video
    a = cfg.output.audio
    ph = cfg.placeholder
    dest = _composite_dest(cfg)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats"]
    filters: List[str] = []
    audio_map: str

    if ph.type == "black":
        cmd += [
            "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
            "-f", "lavfi", "-i",
            f"anullsrc=r={a.sample_rate}:cl=stereo",
        ]
        # No filter_complex needed for black — map directly
        cmd += [
            "-map", "0:v", "-map", "1:a",
        ]
        cmd += _output_flags(cfg, dest)
        return cmd

    elif ph.type == "text":
        if not ph.text:
            raise ValueError("placeholder.text is required for type=text")
        cmd += [
            "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
            "-f", "lavfi", "-i",
            f"anullsrc=r={a.sample_rate}:cl=stereo",
        ]
        escaped = (
            ph.text
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace(":", "\\:")
        )
        fp = f":fontfile='{ph.font_path}'" if ph.font_path else ""
        # Center text by default if x=0 and y=0
        x_expr = str(ph.x) if ph.x else "(w-text_w)/2"
        y_expr = str(ph.y) if ph.y else "(h-text_h)/2"
        filters.append(
            f"[0:v]drawtext{fp}"
            f":text='{escaped}'"
            f":x={x_expr}:y={y_expr}"
            f":fontsize={ph.font_size}"
            f":fontcolor={ph.font_color}@{ph.opacity:.3f}"
            f"[vout]"
        )
        audio_map = "1:a"

    elif ph.type == "image":
        if not ph.path:
            raise ValueError("placeholder.path is required for type=image")
        cmd += [
            "-re", "-loop", "1", "-i", ph.path,
            "-f", "lavfi", "-i",
            f"anullsrc=r={a.sample_rate}:cl=stereo",
        ]
        filters.append(_scale_pad(v, "0:v", "vscaled"))
        if ph.opacity < 1.0:
            filters.append(
                f"[vscaled]format=rgba,"
                f"colorchannelmixer=aa={ph.opacity:.3f}[vout]"
            )
        else:
            filters.append("[vscaled]copy[vout]")
        audio_map = "1:a"

    elif ph.type == "video":
        if not ph.path:
            raise ValueError("placeholder.path is required for type=video")
        cmd += [
            "-re", "-stream_loop", "-1", "-i", ph.path,
            "-f", "lavfi", "-i",
            f"anullsrc=r={a.sample_rate}:cl=stereo",
        ]
        filters.append(_scale_pad(v, "0:v", "vout"))
        # Always use silence for placeholder video; avoids issues with
        # videos that have no audio track
        audio_map = "1:a"

    else:
        raise ValueError(f"Unknown placeholder.type: {ph.type!r}")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", audio_map]
    cmd += _output_flags(cfg, dest)
    return cmd


def build_compositor_live(
    cfg: Config,
    stream_path: str,
    has_audio: bool,
) -> List[str]:
    """
    Compositor command for LIVE state.
    Reads incoming stream from mediamtx (RTSP), applies optional overlay,
    and outputs to the internal composite path.
    """
    v = cfg.output.video
    a = cfg.output.audio
    ov = cfg.overlay
    dest = _composite_dest(cfg)

    stream_url = f"rtsp://127.0.0.1:{cfg.internal_rtsp_port}/{stream_path}"

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
        "-rtsp_transport", "tcp",
        "-i", stream_url,
    ]

    filters: List[str] = []
    # input_count tracks the next FFmpeg input index (0 = incoming stream)
    input_count = 1
    last_v = "vscaled"

    # Scale/pad incoming to output resolution
    filters.append(_scale_pad(v, "0:v", "vscaled"))

    # Supplementary overlay (only in LIVE mode)
    if ov.enabled:
        if ov.type == "image" and ov.path:
            cmd += ["-loop", "1", "-i", ov.path]
            alpha_chain = ""
            if ov.opacity < 1.0:
                alpha_chain = f",colorchannelmixer=aa={ov.opacity:.3f}"
            filters.append(
                f"[{input_count}:v]format=rgba{alpha_chain}[ov_img]"
            )
            filters.append(
                f"[{last_v}][ov_img]overlay={ov.x}:{ov.y}[vwith_ov]"
            )
            last_v = "vwith_ov"
            input_count += 1

        elif ov.type == "text" and ov.text:
            escaped = (
                ov.text
                .replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace(":", "\\:")
            )
            fp = f":fontfile='{ov.font_path}'" if ov.font_path else ""
            filters.append(
                f"[{last_v}]drawtext{fp}"
                f":text='{escaped}'"
                f":x={ov.x}:y={ov.y}"
                f":fontsize={ov.font_size}"
                f":fontcolor={ov.font_color}@{ov.opacity:.3f}"
                f"[vwith_text]"
            )
            last_v = "vwith_text"

    filters.append(f"[{last_v}]copy[vout]")

    # Audio: use incoming audio if available, otherwise generate silence
    if has_audio:
        filters.append(
            f"[0:a]aresample={a.sample_rate},"
            f"aformat=channel_layouts=stereo[aout]"
        )
        audio_map = "[aout]"
    else:
        cmd += [
            "-f", "lavfi", "-i",
            f"anullsrc=r={a.sample_rate}:cl=stereo",
        ]
        filters.append(
            f"[{input_count}:a]aformat=sample_rates={a.sample_rate}:"
            f"channel_layouts=stereo[aout]"
        )
        audio_map = "[aout]"

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", audio_map]
    cmd += _output_flags(cfg, dest)
    return cmd


def build_output(cfg: Config) -> List[str]:
    """
    Output FFmpeg — reads the compositor output from mediamtx (RTSP),
    writes to all configured RTMP targets. This process NEVER restarts;
    it holds the persistent RTMP connection to the target services.

    Brief interruptions during compositor restarts (< 2 s) are handled by
    the grace period overlap: the new compositor starts pushing before the
    old one is terminated, so the RTSP path always has data.  If the
    compositor crashes, the watchdog restarts both processes.

    Note: -reconnect and -stimeout flags are not universally supported
    across FFmpeg builds (e.g. Alpine).  We rely on the watchdog to
    restart this process if the RTSP connection drops.
    """
    relay_url = f"rtsp://127.0.0.1:{cfg.internal_rtsp_port}/{cfg.composite_path}"
    targets = cfg.output.targets

    if not targets:
        raise ValueError("output.targets must not be empty")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
        "-rtsp_transport", "tcp",
        "-use_wallclock_as_timestamps", "1",
        "-i", relay_url,
    ]

    if len(targets) == 1:
        cmd += ["-c", "copy", "-f", "flv", targets[0]]
    else:
        # Tee muxer: send to multiple RTMP targets simultaneously
        tee_str = "|".join(f"[f=flv]{t}" for t in targets)
        cmd += ["-c", "copy", "-f", "tee", tee_str]

    return cmd
