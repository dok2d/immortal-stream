"""FFmpeg command builders for each streaming state."""
from typing import List, Optional

from config import Config, VideoConfig, PlaceholderConfig

# Default font for drawtext when no font_path is configured.
DEFAULT_FONT = "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf"

# Directory for pre-processed images (overlay, placeholder).
# Persistent across restarts; controlled by CACHE_DIR env var.
import os as _os
_OVERLAY_CACHE_DIR = _os.environ.get(
    "CACHE_DIR", "/media/opt/.cache"
)


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

def _bufsize(bitrate: str) -> str:
    """2x bitrate as bufsize string (e.g. '6000k' -> '12000k', '4.5m' -> '9.0m')."""
    b = bitrate.lower().strip()
    try:
        if b.endswith("k"):
            return f"{float(b[:-1]) * 2:g}k"
        if b.endswith("m"):
            return f"{float(b[:-1]) * 2:g}m"
        return f"{float(b) * 2:g}"
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


_NAMED_COLORS_LIGHT = {
    "white", "yellow", "cyan", "lime", "aqua", "lightyellow", "lightyellow",
    "lightcyan", "lightgreen", "ivory", "snow", "seashell", "mintcream",
    "azure", "ghostwhite", "floralwhite", "honeydew", "lemonchiffon",
    "cornsilk", "beige", "linen", "oldlace", "lavenderblush", "mistyrose",
    "papayawhip", "blanchedalmond", "bisque", "moccasin", "navajowhite",
    "peachpuff", "wheat", "antiquewhite", "lavender", "thistle", "pink",
    "lightpink", "lightsalmon", "lightskyblue", "lightsteelblue",
    "lightblue", "lightcoral", "palegreen", "palegoldenrod", "paleturquoise",
    "palevioletred", "powderblue", "khaki", "gold", "orange", "plum",
    "silver", "gainsboro", "lightgray", "lightgrey",
}

_NAMED_COLORS_DARK = {
    "black", "darkblue", "darkred", "darkgreen", "darkmagenta", "darkcyan",
    "darkviolet", "darkolivegreen", "darkslategray", "darkslategrey",
    "darkslateblue", "midnightblue", "navy", "maroon", "indigo", "brown",
    "saddlebrown", "sienna", "dimgray", "dimgrey",
}


def _is_light_color(color: str) -> bool:
    """Determine if a color name or hex value is 'light' (high luminance).

    Uses simple heuristics: named-color lookup, then hex-value luminance.
    Falls back to True (assume light) for unknown names.
    """
    c = color.lower().strip()
    if c in _NAMED_COLORS_LIGHT:
        return True
    if c in _NAMED_COLORS_DARK:
        return False
    # Try hex parsing: #RGB, #RRGGBB, 0xRRGGBB
    hex_str = c.lstrip("#").lstrip("0x")
    try:
        if len(hex_str) == 3:
            r, g, b = (int(h * 2, 16) for h in hex_str)
        elif len(hex_str) == 6:
            r = int(hex_str[0:2], 16)
            g = int(hex_str[2:4], 16)
            b = int(hex_str[4:6], 16)
        else:
            return True  # unknown → assume light
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return lum > 128
    except (ValueError, TypeError):
        return True  # unknown → assume light


def _border_opts(font_color: str) -> list:
    """Return drawtext border options: thin outline in contrasting color."""
    if _is_light_color(font_color):
        return ["borderw=2", "bordercolor=black"]
    else:
        return ["borderw=2", "bordercolor=white"]


def _escape_drawtext(text: str) -> str:
    r"""Escape text for FFmpeg drawtext filter inside a filter_complex string.

    Characters special to drawtext (\\, ', :) AND to the filter graph
    parser (",", ;, [, ]) are all backslash-escaped so no quoting wrapper
    is needed around the value.
    """
    return (
        text
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace(":", "\\:")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


# Position presets for drawtext filter (x_expr, y_expr).
# Uses FFmpeg drawtext variables: w, h, text_w, text_h.
_DRAWTEXT_POSITIONS = {
    "top-left":      ("20", "20"),
    "top-center":    ("(w-text_w)/2", "20"),
    "top-right":     ("w-text_w-20", "20"),
    "left":          ("20", "(h-text_h)/2"),
    "center":        ("(w-text_w)/2", "(h-text_h)/2"),
    "right":         ("w-text_w-20", "(h-text_h)/2"),
    "bottom-left":   ("20", "h-text_h-20"),
    "bottom-center": ("(w-text_w)/2", "h-text_h-20"),
    "bottom-right":  ("w-text_w-20", "h-text_h-20"),
}

# Position presets for overlay filter (x_expr, y_expr).
# Uses FFmpeg overlay variables: main_w, main_h, overlay_w, overlay_h.
_OVERLAY_POSITIONS = {
    "top-left":      ("20", "20"),
    "top-center":    ("(main_w-overlay_w)/2", "20"),
    "top-right":     ("main_w-overlay_w-20", "20"),
    "left":          ("20", "(main_h-overlay_h)/2"),
    "center":        ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
    "right":         ("main_w-overlay_w-20", "(main_h-overlay_h)/2"),
    "bottom-left":   ("20", "main_h-overlay_h-20"),
    "bottom-center": ("(main_w-overlay_w)/2", "main_h-overlay_h-20"),
    "bottom-right":  ("main_w-overlay_w-20", "main_h-overlay_h-20"),
}


def _resolve_drawtext_pos(position: str, x: int = 0, y: int = 0):
    """Resolve position preset to (x_expr, y_expr) for drawtext."""
    if position in _DRAWTEXT_POSITIONS:
        return _DRAWTEXT_POSITIONS[position]
    return (str(x) if x else "(w-text_w)/2", str(y) if y else "(h-text_h)/2")


def _resolve_overlay_pos(position: str, x: int = 0, y: int = 0):
    """Resolve position preset to (x_expr, y_expr) for overlay filter."""
    if position in _OVERLAY_POSITIONS:
        return _OVERLAY_POSITIONS[position]
    return (str(x), str(y))


def _encoding_flags(cfg: Config) -> List[str]:
    """Common encoding flags for the compositor output (video + audio)."""
    v = cfg.output.video
    a = cfg.output.audio
    return [
        "-c:v", "libx264",
        "-preset", v.preset,
        "-tune", v.tune,
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
    """UDP/MPEG-TS URL the compositor pushes to.

    UDP is connectionless — the output FFmpeg can start listening before
    or after the compositor, and compositor restarts do NOT break the
    output FFmpeg process.  This is the key to uninterrupted streaming.
    """
    return f"udp://127.0.0.1:{cfg.internal_udp_port}?pkt_size=1316"


def _ffmpeg_base() -> List[str]:
    """Common FFmpeg prefix flags."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats"]


def _anullsrc(sample_rate: int) -> List[str]:
    """lavfi silence source input."""
    return ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]


async def prepare_image(
    src: str,
    *,
    width: int = 0,
    height: int = 0,
    max_height: int = 0,
    opacity: float = 1.0,
) -> str:
    """Pre-process a static image once, return path to cached result.

    Runs ffmpeg once at compositor start instead of applying filters every
    frame.  Result is cached in _OVERLAY_CACHE_DIR by (path, mtime, params).

    Modes (mutually exclusive):
      width + height : scale + pad to exact dimensions (placeholder images)
      max_height     : scale preserving aspect ratio (overlay images)

    opacity < 1.0 bakes the alpha channel into the image (RGBA PNG output).
    """
    import asyncio
    import hashlib
    import os

    os.makedirs(_OVERLAY_CACHE_DIR, exist_ok=True)

    stat = os.stat(src)
    key = f"{src}:{stat.st_mtime}:{width}:{height}:{max_height}:{opacity}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    dst = os.path.join(_OVERLAY_CACHE_DIR, f"img_{h}.png")

    if os.path.isfile(dst):
        return dst

    # Build the filter chain
    vf_parts: list[str] = []
    if width and height:
        vf_parts.append(
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )
    elif max_height > 0:
        vf_parts.append(
            f"scale=-1:{max_height}:force_original_aspect_ratio=decrease"
        )
    if opacity < 1.0:
        vf_parts.append(f"format=rgba,colorchannelmixer=aa={opacity:.3f}")

    vf = ",".join(vf_parts) if vf_parts else "copy"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-vf", vf,
        "-frames:v", "1",
        "-y", dst,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return src
    if proc.returncode != 0:
        import logging
        logging.getLogger("ffmpeg_cmd").warning(
            "Image prepare failed (code %d): %s", proc.returncode,
            (stderr or b"").decode(errors="replace")[:200],
        )
        return src
    return dst


# Cache for file_has_audio() — keyed by (path, mtime).
_audio_probe_cache: dict[str, tuple[float, bool]] = {}


async def file_has_audio(path: str) -> bool:
    """Async ffprobe check for an audio stream in a local file.

    Results are cached by (path, mtime) to avoid repeated subprocess
    calls for the same unchanged file.
    """
    import asyncio
    import os

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False

    cached = _audio_probe_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        result = b"audio" in (stdout or b"")
        _audio_probe_cache[path] = (mtime, result)
        return result
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


def _placeholder_text_filter(
    ph: PlaceholderConfig, src_label: str, dst_label: str,
) -> Optional[str]:
    """Build a drawtext filter for placeholder text overlay.

    Text is additive — it overlays on top of whatever base type is
    active (black, testcard, image, video).  Returns None if no text
    is configured.
    """
    if not ph.text:
        return None
    escaped = _escape_drawtext(ph.text)
    font = ph.font_path or DEFAULT_FONT
    x_expr, y_expr = _resolve_drawtext_pos(ph.text_position, ph.text_x, ph.text_y)
    opts = [
        f"fontfile={font}",
        f"text={escaped}",
        f"x={x_expr}",
        f"y={y_expr}",
        f"fontsize={ph.font_size}",
        f"fontcolor={ph.font_color}@{ph.text_opacity:.3f}",
    ] + _border_opts(ph.font_color)
    return f"[{src_label}]drawtext={':'.join(opts)}[{dst_label}]"


# ---------------------------------------------------------------------------
#  Compositor: IDLE state
# ---------------------------------------------------------------------------

def build_compositor_idle(
    cfg: Config,
    video_has_audio: bool = False,
    placeholder_image_path: Optional[str] = None,
) -> List[str]:
    """Compositor command for IDLE state (no incoming stream).

    Layers (bottom → top): background → image → video → text.
    Each layer is independent and optional (except background).

    video_has_audio: pre-probed result for placeholder video_path.
    placeholder_image_path: pre-processed image (scaled+padded+opacity baked).
    """
    v = cfg.output.video
    a = cfg.output.audio
    ph = cfg.placeholder
    dest = _composite_dest(cfg)

    cmd = _ffmpeg_base()
    filters: List[str] = []
    last_v = "vbase"
    input_idx = 0
    audio_from_video = False

    # ── Layer 0: Background (always present) ──────────────────────────
    if ph.background == "testcard":
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"testsrc2=s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
    else:
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
    filters.append("[0:v]copy[vbase]")
    input_idx = 1

    # ── Layer 1: Image (optional) ─────────────────────────────────────
    if ph.image_path:
        img = placeholder_image_path or ph.image_path
        cmd += ["-loop", "1", "-i", img]
        if placeholder_image_path:
            # Pre-processed (full-frame or max_height + opacity baked)
            filters.append(f"[{input_idx}:v]format=rgba[ph_img]")
        elif ph.image_max_height > 0:
            # Positioned mode: scale to max height
            alpha = (
                f",colorchannelmixer=aa={ph.image_opacity:.3f}"
                if ph.image_opacity < 1.0 else ""
            )
            filters.append(
                f"[{input_idx}:v]scale=-1:{ph.image_max_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"format=rgba{alpha}[ph_img]"
            )
        else:
            # Full frame: scale+pad to output resolution
            filters.append(_scale_pad(v, f"{input_idx}:v", "ph_iscaled"))
            if ph.image_opacity < 1.0:
                filters.append(
                    f"[ph_iscaled]format=rgba,"
                    f"colorchannelmixer=aa={ph.image_opacity:.3f}[ph_img]"
                )
            else:
                filters.append("[ph_iscaled]format=rgba[ph_img]")
        if ph.image_max_height > 0 and not placeholder_image_path:
            ox, oy = _resolve_overlay_pos(
                ph.image_position, ph.image_x, ph.image_y,
            )
        else:
            ox, oy = "0", "0"
        filters.append(f"[{last_v}][ph_img]overlay={ox}:{oy}[vimg]")
        last_v = "vimg"
        input_idx += 1

    # ── Layer 2: Video (optional) ─────────────────────────────────────
    if ph.video_path:
        cmd += ["-re", "-stream_loop", "-1", "-i", ph.video_path]
        vid_idx = input_idx
        if ph.video_max_height > 0:
            alpha = (
                f",colorchannelmixer=aa={ph.video_opacity:.3f}"
                if ph.video_opacity < 1.0 else ""
            )
            filters.append(
                f"[{vid_idx}:v]scale=-1:{ph.video_max_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"format=rgba{alpha}[ph_vid]"
            )
            ox, oy = _resolve_overlay_pos(
                ph.video_position, ph.video_x, ph.video_y,
            )
        else:
            filters.append(_scale_pad(v, f"{vid_idx}:v", "ph_vscaled"))
            if ph.video_opacity < 1.0:
                filters.append(
                    f"[ph_vscaled]format=rgba,"
                    f"colorchannelmixer=aa={ph.video_opacity:.3f}[ph_vid]"
                )
            else:
                filters.append("[ph_vscaled]copy[ph_vid]")
            ox, oy = "0", "0"
        filters.append(f"[{last_v}][ph_vid]overlay={ox}:{oy}[vvid]")
        last_v = "vvid"
        if video_has_audio:
            filters.append(
                f"[{vid_idx}:a]aresample={a.sample_rate},"
                f"aformat=channel_layouts=stereo[aout]"
            )
            audio_from_video = True
        input_idx += 1

    # ── Silence source (if no video audio) ────────────────────────────
    if not audio_from_video:
        cmd += _anullsrc(a.sample_rate)
        silence_idx = input_idx
        input_idx += 1

    # ── Layer 3: Text (optional) ──────────────────────────────────────
    text_f = _placeholder_text_filter(ph, last_v, "vout")
    if text_f:
        filters.append(text_f)
    else:
        filters.append(f"[{last_v}]copy[vout]")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]"]
    if audio_from_video:
        cmd += ["-map", "[aout]"]
    else:
        cmd += ["-map", f"{silence_idx}:a"]
    cmd += _encoding_flags(cfg)
    cmd += ["-f", "mpegts", dest]
    return cmd


# ---------------------------------------------------------------------------
#  Compositor: LIVE state
# ---------------------------------------------------------------------------

def build_compositor_live(
    cfg: Config,
    stream_path: str,
    has_audio: bool,
    overlay_image_path: Optional[str] = None,
) -> List[str]:
    """Compositor command for LIVE state.

    Reads incoming stream from mediamtx (RTSP), applies optional overlay,
    and outputs to the internal UDP/MPEG-TS destination.

    overlay_image_path: pre-processed overlay image (resized + opacity baked).
        When provided, no runtime scale/opacity filters are applied.
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

    # Overlay layers (only in LIVE mode) — image and text are independent
    if ov.enabled:
        # Layer 1: image overlay (pre-processed: resized + opacity baked)
        img_path = overlay_image_path or ov.path
        if img_path:
            cmd += ["-loop", "1", "-i", img_path]
            if overlay_image_path:
                # Already resized + opacity baked — just pass through
                filters.append(
                    f"[{input_idx}:v]format=rgba[ov_img]"
                )
            else:
                alpha = (
                    f",colorchannelmixer=aa={ov.image_opacity:.3f}"
                    if ov.image_opacity < 1.0 else ""
                )
                filters.append(
                    f"[{input_idx}:v]format=rgba{alpha}[ov_img]"
                )
            ox, oy = _resolve_overlay_pos(
                ov.image_position, ov.image_x, ov.image_y,
            )
            filters.append(
                f"[{last_v}][ov_img]overlay={ox}:{oy}[vwith_ov]"
            )
            last_v = "vwith_ov"
            input_idx += 1

        # Layer 2: text overlay (drawn on top of image overlay)
        if ov.text:
            escaped = _escape_drawtext(ov.text)
            font = ov.font_path or DEFAULT_FONT
            tx, ty = _resolve_drawtext_pos(
                ov.text_position, ov.text_x, ov.text_y,
            )
            opts = [
                f"fontfile={font}",
                f"text={escaped}",
                f"x={tx}",
                f"y={ty}",
                f"fontsize={ov.font_size}",
                f"fontcolor={ov.font_color}@{ov.text_opacity:.3f}",
            ] + _border_opts(ov.font_color)
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
    cmd += ["-f", "mpegts", dest]
    return cmd


# ---------------------------------------------------------------------------
#  Compositor: AUDIO-ONLY state
# ---------------------------------------------------------------------------

def build_compositor_audio_only(
    cfg: Config,
    stream_path: str,
    video_has_audio: bool = False,
    placeholder_image_path: Optional[str] = None,
) -> List[str]:
    """Compositor for audio-only input — keeps placeholder video layers,
    replaces audio with the incoming stream's audio.

    Layers (bottom → top): background → image → video → text.
    Audio comes from the incoming stream (not from placeholder video).
    """
    v = cfg.output.video
    a = cfg.output.audio
    ph = cfg.placeholder
    dest = _composite_dest(cfg)
    stream_url = f"rtsp://127.0.0.1:{cfg.internal_rtsp_port}/{stream_path}"

    cmd = _ffmpeg_base()
    filters: List[str] = []
    last_v = "vbase"
    input_idx = 0

    # ── Layer 0: Background ───────────────────────────────────────────
    if ph.background == "testcard":
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"testsrc2=s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
    else:
        cmd += [
            "-re", "-f", "lavfi", "-i",
            f"color=c=black:s={v.width}x{v.height}:r={v.fps}:sar=1/1",
        ]
    filters.append("[0:v]copy[vbase]")
    input_idx = 1

    # ── Layer 1: Image ────────────────────────────────────────────────
    if ph.image_path:
        img = placeholder_image_path or ph.image_path
        cmd += ["-loop", "1", "-i", img]
        if placeholder_image_path:
            filters.append(f"[{input_idx}:v]format=rgba[ph_img]")
        elif ph.image_max_height > 0:
            alpha = (
                f",colorchannelmixer=aa={ph.image_opacity:.3f}"
                if ph.image_opacity < 1.0 else ""
            )
            filters.append(
                f"[{input_idx}:v]scale=-1:{ph.image_max_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"format=rgba{alpha}[ph_img]"
            )
        else:
            filters.append(_scale_pad(v, f"{input_idx}:v", "ph_iscaled"))
            if ph.image_opacity < 1.0:
                filters.append(
                    f"[ph_iscaled]format=rgba,"
                    f"colorchannelmixer=aa={ph.image_opacity:.3f}[ph_img]"
                )
            else:
                filters.append("[ph_iscaled]format=rgba[ph_img]")
        if ph.image_max_height > 0 and not placeholder_image_path:
            ox, oy = _resolve_overlay_pos(
                ph.image_position, ph.image_x, ph.image_y,
            )
        else:
            ox, oy = "0", "0"
        filters.append(f"[{last_v}][ph_img]overlay={ox}:{oy}[vimg]")
        last_v = "vimg"
        input_idx += 1

    # ── Layer 2: Video ────────────────────────────────────────────────
    if ph.video_path:
        cmd += ["-re", "-stream_loop", "-1", "-i", ph.video_path]
        vid_idx = input_idx
        if ph.video_max_height > 0:
            alpha = (
                f",colorchannelmixer=aa={ph.video_opacity:.3f}"
                if ph.video_opacity < 1.0 else ""
            )
            filters.append(
                f"[{vid_idx}:v]scale=-1:{ph.video_max_height}:"
                f"force_original_aspect_ratio=decrease,"
                f"format=rgba{alpha}[ph_vid]"
            )
            ox, oy = _resolve_overlay_pos(
                ph.video_position, ph.video_x, ph.video_y,
            )
        else:
            filters.append(_scale_pad(v, f"{vid_idx}:v", "ph_vscaled"))
            if ph.video_opacity < 1.0:
                filters.append(
                    f"[ph_vscaled]format=rgba,"
                    f"colorchannelmixer=aa={ph.video_opacity:.3f}[ph_vid]"
                )
            else:
                filters.append("[ph_vscaled]copy[ph_vid]")
            ox, oy = "0", "0"
        filters.append(f"[{last_v}][ph_vid]overlay={ox}:{oy}[vvid]")
        last_v = "vvid"
        input_idx += 1

    # ── Layer 3: Text ─────────────────────────────────────────────────
    text_f = _placeholder_text_filter(ph, last_v, "vout")
    if text_f:
        filters.append(text_f)
    else:
        filters.append(f"[{last_v}]copy[vout]")

    # ── Audio from the incoming stream ────────────────────────────────
    cmd += ["-rtsp_transport", "tcp", "-i", stream_url]
    filters.append(
        f"[{input_idx}:a]aresample={a.sample_rate},"
        f"aformat=channel_layouts=stereo[aout]"
    )

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += _encoding_flags(cfg)
    cmd += ["-f", "mpegts", dest]
    return cmd


# ---------------------------------------------------------------------------
#  Output FFmpeg (persistent)
# ---------------------------------------------------------------------------

def build_output(cfg: Config) -> List[str]:
    """Output FFmpeg — reads the compositor output from UDP/MPEG-TS,
    writes to all configured RTMP targets.

    This process NEVER restarts during normal operation; it holds the
    persistent RTMP connection to the target services.

    UDP is connectionless: compositor restarts cause a brief pause in
    incoming packets, but the output FFmpeg keeps running and resumes
    forwarding automatically when the new compositor starts sending.
    """
    udp_url = (
        f"udp://127.0.0.1:{cfg.internal_udp_port}"
        f"?fifo_size=5000000&overrun_nonfatal=1"
    )
    targets = cfg.output.targets

    if not targets:
        raise ValueError("output.targets must not be empty")

    cmd = _ffmpeg_base() + [
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-f", "mpegts",
        "-i", udp_url,
    ]

    cmd += ["-map", "0", "-c", "copy"]
    # MPEG-TS codec tags differ from FLV:
    #   H.264 video: MPEG-TS=0x1B(27), FLV=0x07(7)
    #   AAC audio:   MPEG-TS=0x0F(15), FLV=0x0A(10)
    # Without explicit remapping, the flv muxer rejects the stream.
    cmd += ["-tag:v", "7", "-tag:a", "10"]
    cmd += ["-avoid_negative_ts", "make_zero"]

    if len(targets) == 1:
        cmd += ["-f", "flv", targets[0]]
    else:
        tee_str = "|".join(
            f"[f=flv:onfail=ignore]{_escape_tee_url(t)}" for t in targets
        )
        cmd += ["-f", "tee", tee_str]

    return cmd
