"""Configuration loading and dataclasses for immortal-stream."""
from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional, List
import logging
import os
import secrets
import shutil
import socket

import yaml

log = logging.getLogger("config")

_VALID_PLACEHOLDER_TYPES = {"black", "image", "video", "testcard"}
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
    type: str = "testcard"
    path: Optional[str] = None
    text: Optional[str] = None
    font_path: Optional[str] = None
    font_size: int = 72
    font_color: str = "white"
    text_position: str = "center"
    x: int = 0
    y: int = 0
    opacity: float = 1.0


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
    if ph.type not in _VALID_PLACEHOLDER_TYPES:
        raise ValueError(
            f"placeholder.type must be one of {_VALID_PLACEHOLDER_TYPES}, "
            f"got {ph.type!r}"
        )
    if ph.type in ("image", "video") and not ph.path:
        raise ValueError(f"placeholder.path is required for type={ph.type!r}")
    if not 0.0 <= ph.opacity <= 1.0:
        raise ValueError(f"placeholder.opacity must be 0.0–1.0, got {ph.opacity}")

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

    # Validate placeholder/overlay file existence
    if ph.type in ("image", "video") and ph.path and not os.path.isfile(ph.path):
        raise ValueError(
            f"placeholder.path {ph.path!r} does not exist "
            f"(required for type={ph.type!r})"
        )
    if ov.enabled and ov.path and not os.path.isfile(ov.path):
        raise ValueError(f"overlay.path {ov.path!r} does not exist")

    # Validate font files
    for label, font_path in [
        ("placeholder.font_path", ph.font_path),
        ("overlay.font_path", ov.font_path),
    ]:
        if font_path and not os.path.isfile(font_path):
            raise ValueError(f"{label} {font_path!r} does not exist")


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

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
        if "opacity" in p:
            p["opacity"] = float(p["opacity"])
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

    if "telegram" in data:
        t = data["telegram"]
        # chat_id is always stored as string
        if "chat_id" in t:
            t["chat_id"] = str(t["chat_id"])
        cfg.telegram = _populate(TelegramConfig, t)

    _validate(cfg)
    return cfg
