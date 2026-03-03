"""Configuration loading and dataclasses for immortal-stream."""
from dataclasses import dataclass, field
from typing import Optional, List
import yaml
import os


@dataclass
class PlaceholderConfig:
    type: str = "black"       # black | image | video
    path: Optional[str] = None
    x: int = 0
    y: int = 0
    opacity: float = 1.0


@dataclass
class OverlayConfig:
    enabled: bool = False
    type: str = "image"       # image | text
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
    stream_key_required: bool = False
    allowed_key: Optional[str] = None


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
    ingest: IngestConfig = field(default_factory=IngestConfig)
    placeholder: PlaceholderConfig = field(default_factory=PlaceholderConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    # Internal ports (not user-configurable, set by entrypoint)
    internal_rtsp_port: int = 8554
    internal_rtmp_port: int = 1935
    webhook_port: int = 8888
    mediamtx_api_port: int = 9997
    internal_token: str = ""  # random token for internal RTMP auth


def _get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, None)
        if d is None:
            return default
    return d if d is not None else default


def load_config(path: str) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    cfg = Config()

    if "ingest" in data:
        i = data["ingest"]
        cfg.ingest = IngestConfig(
            port=i.get("port", 1935),
            srt_port=i.get("srt_port", 8890),
            stream_key_required=i.get("stream_key_required", False),
            allowed_key=i.get("allowed_key"),
        )

    if "placeholder" in data:
        p = data["placeholder"]
        cfg.placeholder = PlaceholderConfig(
            type=p.get("type", "black"),
            path=p.get("path"),
            x=p.get("x", 0),
            y=p.get("y", 0),
            opacity=float(p.get("opacity", 1.0)),
        )

    if "overlay" in data:
        o = data["overlay"]
        cfg.overlay = OverlayConfig(
            enabled=o.get("enabled", False),
            type=o.get("type", "image"),
            path=o.get("path"),
            text=o.get("text"),
            font_path=o.get("font_path"),
            font_size=o.get("font_size", 48),
            font_color=o.get("font_color", "white"),
            x=o.get("x", 10),
            y=o.get("y", 10),
            opacity=float(o.get("opacity", 1.0)),
        )

    if "output" in data:
        od = data["output"]
        vd = od.get("video", {})
        ad = od.get("audio", {})
        cfg.output = OutputConfig(
            targets=od.get("targets", []),
            video=VideoConfig(
                width=vd.get("width", 1920),
                height=vd.get("height", 1080),
                fps=vd.get("fps", 30),
                bitrate=vd.get("bitrate", "6000k"),
                preset=vd.get("preset", "ultrafast"),
                gop=vd.get("gop", 60),
            ),
            audio=AudioConfig(
                bitrate=ad.get("bitrate", "128k"),
                sample_rate=ad.get("sample_rate", 44100),
            ),
        )

    if "telegram" in data:
        t = data["telegram"]
        cfg.telegram = TelegramConfig(
            enabled=t.get("enabled", False),
            bot_token=t.get("bot_token", ""),
            chat_id=str(t.get("chat_id", "")),
        )

    # Internal settings from environment (set by entrypoint)
    cfg.internal_token = os.environ.get("INTERNAL_TOKEN", "changeme")

    return cfg
