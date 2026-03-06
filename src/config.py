"""Configuration loading and dataclasses for immortal-stream."""
from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional, List
import logging
import os
import secrets

import yaml

log = logging.getLogger("config")

_VALID_PLACEHOLDER_TYPES = {"black", "text", "image", "video", "testcard"}
_VALID_OVERLAY_TYPES = {"image", "text"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
}


@dataclass
class PlaceholderConfig:
    type: str = "testcard"
    path: Optional[str] = None
    text: Optional[str] = None
    font_path: Optional[str] = None
    font_size: int = 72
    font_color: str = "white"
    x: int = 0
    y: int = 0
    opacity: float = 1.0
    timezone: str = "UTC"


@dataclass
class OverlayConfig:
    enabled: bool = False
    type: str = "image"
    path: Optional[str] = None
    text: Optional[str] = None
    font_path: Optional[str] = None
    font_size: int = 48
    font_color: str = "white"
    x: int = 10
    y: int = 10
    opacity: float = 1.0


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
    if ph.type == "text" and not ph.text:
        raise ValueError("placeholder.text is required for type='text'")
    if not 0.0 <= ph.opacity <= 1.0:
        raise ValueError(f"placeholder.opacity must be 0.0–1.0, got {ph.opacity}")

    ov = cfg.overlay
    if ov.enabled:
        if ov.type not in _VALID_OVERLAY_TYPES:
            raise ValueError(
                f"overlay.type must be one of {_VALID_OVERLAY_TYPES}, "
                f"got {ov.type!r}"
            )
        if ov.type == "image" and not ov.path:
            raise ValueError("overlay.path is required for type='image'")
        if ov.type == "text" and not ov.text:
            raise ValueError("overlay.text is required for type='text'")
    if not 0.0 <= ov.opacity <= 1.0:
        raise ValueError(f"overlay.opacity must be 0.0–1.0, got {ov.opacity}")

    v = cfg.output.video
    if v.preset not in _X264_PRESETS:
        raise ValueError(
            f"output.video.preset must be one of {sorted(_X264_PRESETS)}, "
            f"got {v.preset!r}"
        )
    if not 1 <= v.fps <= 120:
        raise ValueError(f"output.video.fps must be 1–120, got {v.fps}")
    if not (160 <= v.width <= 7680 and 90 <= v.height <= 4320):
        raise ValueError(
            f"output.video size out of range: {v.width}x{v.height}"
        )

    if cfg.log_level not in _VALID_LOG_LEVELS:
        log.warning("Unknown log_level %r, falling back to INFO", cfg.log_level)
        cfg.log_level = "INFO"


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
        if "opacity" in o:
            o["opacity"] = float(o["opacity"])
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
