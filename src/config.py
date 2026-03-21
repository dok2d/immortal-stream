"""Configuration loading and dataclasses for immortal-stream."""
from dataclasses import dataclass, field, fields as dc_fields, asdict
from typing import Optional, List
import logging
import os
import secrets
import shutil
import socket

import yaml

log = logging.getLogger("config")

_VALID_BACKGROUNDS = {"black", "testcard"}
_VALID_OVERLAY_TYPES = {"image", "text"}  # legacy, kept for config compat
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
}
_X264_TUNES = {
    "film", "animation", "grain", "stillimage",
    "fastdecode", "zerolatency",
}


POSITION_PRESETS = (
    "top-left", "top-center", "top-right",
    "left", "center", "right",
    "bottom-left", "bottom-center", "bottom-right",
    "custom",
)


@dataclass
class PlaceholderConfig:
    # Base background (always present as the bottom layer)
    background: str = "black"       # "black" | "testcard"
    # Image layer (overlaid on background)
    image_path: Optional[str] = None
    image_position: str = "center"
    image_x: int = 0
    image_y: int = 0
    image_opacity: float = 1.0
    image_max_height: int = 0       # 0 = full frame (scale+pad); >0 = scale to max px height
    # Video layer (overlaid on image)
    video_path: Optional[str] = None
    video_position: str = "center"
    video_x: int = 0
    video_y: int = 0
    video_opacity: float = 1.0
    video_max_height: int = 0       # 0 = full frame (scale+pad); >0 = scale to max px height
    # Text layer (overlaid on everything)
    text: Optional[str] = None
    font_path: Optional[str] = None
    font_size: int = 72
    font_color: str = "white"
    text_position: str = "center"
    text_x: int = 0
    text_y: int = 0
    text_opacity: float = 1.0


@dataclass
class OverlayConfig:
    enabled: bool = False
    # Image overlay
    path: Optional[str] = None
    image_position: str = "top-left"
    image_x: int = 20
    image_y: int = 20
    image_opacity: float = 1.0
    image_max_height: int = 0       # 0 = original size; >0 = scale to max px height
    # Text overlay
    text: Optional[str] = None
    font_path: Optional[str] = None
    font_size: int = 48
    font_color: str = "white"
    text_position: str = "top-left"
    text_x: int = 20
    text_y: int = 20
    text_opacity: float = 1.0


@dataclass
class IngestConfig:
    port: int = 1935
    srt_port: int = 8890
    hls: bool = False
    hls_port: int = 8888
    stream_key_required: bool = False
    allowed_key: Optional[str] = None
    redundant_sources: List[str] = field(default_factory=list)


@dataclass
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate: str = "6000k"
    preset: str = "ultrafast"
    tune: str = "zerolatency"
    gop: int = 60


@dataclass
class AudioConfig:
    bitrate: str = "128k"
    sample_rate: int = 44100


@dataclass
class OutputConfig:
    targets: List[str] = field(default_factory=list)
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)


@dataclass
class RecordingConfig:
    enabled: bool = False
    directory: str = "/media/records"
    max_file_size_mb: int = 45
    min_disk_free_mb: int = 1000
    min_disk_stop_mb: int = 100


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class Config:
    log_level: str = "INFO"
    ingest: IngestConfig = field(default_factory=IngestConfig)
    placeholder: PlaceholderConfig = field(default_factory=PlaceholderConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    # Internal — generated at runtime, not user-configurable
    internal_rtsp_port: int = 8554
    internal_rtmp_port: int = 1935
    mediamtx_api_port: int = 9997
    internal_udp_port: int = 5111
    hook_server_port: int = 9998
    composite_path: str = field(default_factory=lambda: f"_c{secrets.token_hex(8)}")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _populate(cls, data: dict):
    """Create a dataclass instance from a dict, using dataclass defaults
    for any missing keys.  Only keys matching field names are accepted."""
    valid = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def _validate(cfg: Config) -> None:
    """Validate configuration values after loading."""
    ph = cfg.placeholder
    if ph.background not in _VALID_BACKGROUNDS:
        raise ValueError(
            f"placeholder.background must be one of {_VALID_BACKGROUNDS}, "
            f"got {ph.background!r}"
        )
    if not 0.0 <= ph.image_opacity <= 1.0:
        raise ValueError(
            f"placeholder.image_opacity must be 0.0–1.0, got {ph.image_opacity}"
        )
    if ph.image_max_height < 0:
        raise ValueError(
            f"placeholder.image_max_height must be >= 0, got {ph.image_max_height}"
        )
    if not 0.0 <= ph.video_opacity <= 1.0:
        raise ValueError(
            f"placeholder.video_opacity must be 0.0–1.0, got {ph.video_opacity}"
        )
    if ph.video_max_height < 0:
        raise ValueError(
            f"placeholder.video_max_height must be >= 0, got {ph.video_max_height}"
        )
    if not 0.0 <= ph.text_opacity <= 1.0:
        raise ValueError(
            f"placeholder.text_opacity must be 0.0–1.0, got {ph.text_opacity}"
        )

    ov = cfg.overlay
    if not 0.0 <= ov.image_opacity <= 1.0:
        raise ValueError(
            f"overlay.image_opacity must be 0.0–1.0, got {ov.image_opacity}"
        )
    if not 0.0 <= ov.text_opacity <= 1.0:
        raise ValueError(
            f"overlay.text_opacity must be 0.0–1.0, got {ov.text_opacity}"
        )
    if ov.image_max_height < 0:
        raise ValueError(
            f"overlay.image_max_height must be >= 0, got {ov.image_max_height}"
        )

    v = cfg.output.video
    if v.preset not in _X264_PRESETS:
        raise ValueError(
            f"output.video.preset must be one of {sorted(_X264_PRESETS)}, "
            f"got {v.preset!r}"
        )
    if v.tune not in _X264_TUNES:
        raise ValueError(
            f"output.video.tune must be one of {sorted(_X264_TUNES)}, "
            f"got {v.tune!r}"
        )
    if not 1 <= v.fps <= 60:
        raise ValueError(
            f"output.video.fps must be 1–60 "
            f"(YouTube/Twitch max 60), got {v.fps}"
        )
    if not (160 <= v.width <= 3840 and 120 <= v.height <= 2160):
        raise ValueError(
            f"output.video size out of range: {v.width}x{v.height} "
            f"(allowed 160x120–3840x2160)"
        )
    if v.gop < v.fps:
        raise ValueError(
            f"output.video.gop ({v.gop}) must be >= fps ({v.fps}); "
            f"minimum keyframe interval is 1 second"
        )

    a = cfg.output.audio
    if a.sample_rate not in (44100, 48000):
        raise ValueError(
            f"output.audio.sample_rate must be 44100 or 48000, "
            f"got {a.sample_rate}"
        )

    if cfg.log_level not in _VALID_LOG_LEVELS:
        log.warning("Unknown log_level %r, falling back to INFO", cfg.log_level)
        cfg.log_level = "INFO"

    # Validate placeholder/overlay file existence — warn and clear missing
    # files instead of crashing (files from previous sessions may be gone).
    if ph.image_path and not os.path.isfile(ph.image_path):
        log.warning(
            "placeholder.image_path %r does not exist — clearing",
            ph.image_path,
        )
        ph.image_path = None
    if ph.video_path and not os.path.isfile(ph.video_path):
        log.warning(
            "placeholder.video_path %r does not exist — clearing",
            ph.video_path,
        )
        ph.video_path = None
    if ov.path and not os.path.isfile(ov.path):
        log.warning("overlay.path %r does not exist — clearing", ov.path)
        ov.path = None

    # Validate font files
    if ph.font_path and not os.path.isfile(ph.font_path):
        log.warning(
            "placeholder.font_path %r does not exist — clearing",
            ph.font_path,
        )
        ph.font_path = None
    if ov.font_path and not os.path.isfile(ov.font_path):
        log.warning(
            "overlay.font_path %r does not exist — clearing",
            ov.font_path,
        )
        ov.font_path = None


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def _migrate_placeholder(p: dict) -> None:
    """Migrate legacy placeholder format to layered fields.

    Legacy: type + path + opacity + x + y
    New:    background + image_path + image_opacity + video_path
            + text_x + text_y + text_opacity
    """
    if "type" in p:
        old_type = p.pop("type")
        old_path = p.pop("path", None)
        if old_type in ("black", "testcard"):
            p.setdefault("background", old_type)
        elif old_type == "image":
            p.setdefault("background", "black")
            if old_path:
                p.setdefault("image_path", old_path)
        elif old_type == "video":
            p.setdefault("background", "black")
            if old_path:
                p.setdefault("video_path", old_path)
    # Remove leftover "path" from legacy
    p.pop("path", None)
    # opacity → image_opacity
    if "opacity" in p:
        p.setdefault("image_opacity", float(p.pop("opacity")))
    # x/y → text_x/text_y
    if "x" in p and "text_x" not in p:
        p["text_x"] = p.pop("x")
    else:
        p.pop("x", None)
    if "y" in p and "text_y" not in p:
        p["text_y"] = p.pop("y")
    else:
        p.pop("y", None)
    # Ensure float types
    for k in ("image_opacity", "video_opacity", "text_opacity"):
        if k in p:
            p[k] = float(p[k])


def load_config(path: str) -> Config:
    """Load configuration from a YAML file, validate, and return a Config."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    cfg = Config()

    # Log level: config file takes precedence over LOG_LEVEL env var
    if "log_level" in data:
        cfg.log_level = str(data["log_level"]).upper()
    else:
        cfg.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if "ingest" in data:
        cfg.ingest = _populate(IngestConfig, data["ingest"])

    if "placeholder" in data:
        p = data["placeholder"]
        _migrate_placeholder(p)
        cfg.placeholder = _populate(PlaceholderConfig, p)

    if "overlay" in data:
        o = data["overlay"]
        # Migrate legacy flat format (type + shared position/opacity)
        legacy_type = o.pop("type", None)
        for field in ("position", "x", "y", "opacity"):
            if field in o:
                val = o.pop(field)
                if legacy_type == "text":
                    o.setdefault(f"text_{field}", val)
                else:
                    o.setdefault(f"image_{field}", val)
        if "image_opacity" in o:
            o["image_opacity"] = float(o["image_opacity"])
        if "text_opacity" in o:
            o["text_opacity"] = float(o["text_opacity"])
        cfg.overlay = _populate(OverlayConfig, o)

    if "output" in data:
        od = data["output"]
        cfg.output = OutputConfig(
            targets=od.get("targets", []),
            video=_populate(VideoConfig, od.get("video", {})),
            audio=_populate(AudioConfig, od.get("audio", {})),
        )

    if "recording" in data:
        cfg.recording = _populate(RecordingConfig, data["recording"])

    if "telegram" in data:
        t = data["telegram"]
        # chat_id is always stored as string
        if "chat_id" in t:
            t["chat_id"] = str(t["chat_id"])
        cfg.telegram = _populate(TelegramConfig, t)

    _validate(cfg)
    return cfg


# ---------------------------------------------------------------------------
#  Config persistence — write bot-modifiable settings back to config.yaml
# ---------------------------------------------------------------------------


def save_config(cfg: Config, path: str) -> None:
    """Merge bot-modifiable settings into config.yaml and write back.

    Reads the original file, updates only the sections the bot can change
    (placeholder, overlay, output, recording), and writes back in place.
    Uses a temp file + rename when possible, falls back to direct write
    for bind-mounted files where creating siblings is not allowed.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        log.warning("Cannot read %s for merging — writing from scratch", path)
        data = {}

    data["placeholder"] = asdict(cfg.placeholder)
    data["overlay"] = asdict(cfg.overlay)
    data["output"] = {
        "targets": list(cfg.output.targets),
        "video": asdict(cfg.output.video),
        # preserve audio from original config
        **({"audio": data["output"]["audio"]}
           if "output" in data and "audio" in data.get("output", {})
           else {}),
    }
    data["recording"] = asdict(cfg.recording)

    content = yaml.safe_dump(data, default_flow_style=False, allow_unicode=True)

    # Write to a temp file first, then move into place.
    # Use the same directory as the target so os.replace stays on the
    # same filesystem (avoids cross-device errors with /tmp).
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        log.debug("Config saved to %s (atomic)", path)
        return
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # Fallback: direct write (e.g. bind-mounted file where directory
    # is not writable but the file itself is).
    with open(path, "w") as f:
        f.write(content)
    log.debug("Config saved to %s (direct)", path)
