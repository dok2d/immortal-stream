#!/usr/bin/env python3
"""
immortal-stream — fault-tolerant live streaming with 3-layer compositing.

Entry point: loads config, starts mediamtx, stream manager (receives
mediamtx hook events), compositor and output FFmpeg processes.
"""
import asyncio
import logging
import os
import shutil
import signal
import socket
import sys

from config import load_config
from mediamtx_manager import MediamtxManager
from stream_manager import StreamManager
from telegram import TelegramNotifier, NoopNotifier
from tgbot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/etc/immortal-stream/config.yaml")


def _is_telegram_configured(cfg) -> bool:
    return cfg.telegram.enabled and cfg.telegram.bot_token and cfg.telegram.chat_id


def _check_port_available(port: int, proto: str = "udp") -> bool:
    """Check if a port is available for binding."""
    sock_type = socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET, sock_type) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _validate_startup_deps(cfg) -> None:
    """Validate all external dependencies are available before starting."""
    errors = []

    # Required binaries
    for binary in ("ffmpeg", "ffprobe", "curl"):
        if not shutil.which(binary):
            errors.append(f"Required binary not found: {binary}")

    # mediamtx binary
    mediamtx_bin = os.environ.get("MEDIAMTX_BIN", "/usr/local/bin/mediamtx")
    if not shutil.which(mediamtx_bin) and not os.path.isfile(mediamtx_bin):
        errors.append(f"mediamtx binary not found: {mediamtx_bin}")

    # UDP port for compositor→output link
    if not _check_port_available(cfg.internal_udp_port, "udp"):
        errors.append(
            f"UDP port {cfg.internal_udp_port} is not available "
            f"(used for compositor→output link)"
        )

    # Hook server TCP port
    if not _check_port_available(cfg.hook_server_port, "tcp"):
        errors.append(
            f"TCP port {cfg.hook_server_port} is not available "
            f"(used for mediamtx hook server)"
        )

    if errors:
        for e in errors:
            log.critical(e)
        raise RuntimeError(
            f"Startup validation failed: {len(errors)} error(s)"
        )


async def main() -> None:
    # -- Load configuration ------------------------------------------------
    try:
        cfg = load_config(CONFIG_PATH)
    except Exception as e:
        log.critical("Failed to load config from %s: %s", CONFIG_PATH, e)
        sys.exit(1)

    level = getattr(logging, cfg.log_level, logging.INFO)
    logging.getLogger().setLevel(level)

    # -- Validate startup dependencies ------------------------------------
    try:
        _validate_startup_deps(cfg)
    except RuntimeError as e:
        log.critical("%s", e)
        sys.exit(1)

    log.info(
        "Config loaded: ingest=%d, srt=%d, targets=%d, placeholder=%s",
        cfg.ingest.port,
        cfg.ingest.srt_port,
        len(cfg.output.targets),
        cfg.placeholder.type,
    )

    # -- Telegram notifier -------------------------------------------------
    if _is_telegram_configured(cfg):
        notifier = TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        log.info("Telegram notifications enabled (chat=%s)", cfg.telegram.chat_id)
    else:
        notifier = NoopNotifier()
        log.info("Telegram notifications disabled")

    notifier.start()

    # -- mediamtx ----------------------------------------------------------
    mediamtx = MediamtxManager(cfg)
    await mediamtx.start()
    ready = await mediamtx.wait_ready(timeout=15)
    if not ready:
        log.critical("mediamtx failed to start")
        notifier.send(
            "\U0001f480 <b>immortal-stream failed to start</b>: mediamtx not ready"
        )
        sys.exit(1)

    # -- Stream manager (hook server receives mediamtx events) --------------
    manager = StreamManager(cfg, notifier)
    await manager.start()

    # -- Telegram bot (runtime config changes) -----------------------------
    bot = None
    if _is_telegram_configured(cfg):
        bot = TelegramBot(cfg, manager)
        bot.start()

    # -- Startup notification ----------------------------------------------
    target_list = "\n".join(f"  \u2022 {t}" for t in cfg.output.targets) or "  (none)"
    hls_line = f"\nIngest HLS:  :{cfg.ingest.hls_port}" if cfg.ingest.hls else ""
    key_line = (
        f"\nStream key:  required ({cfg.ingest.allowed_key})"
        if cfg.ingest.stream_key_required and cfg.ingest.allowed_key
        else "\nStream key:  any"
    )
    notifier.send(
        "\u2705 <b>immortal-stream started</b>\n"
        f"Ingest RTMP: :{cfg.ingest.port}\n"
        f"Ingest SRT:  :{cfg.ingest.srt_port}"
        f"{hls_line}{key_line}\n"
        f"Placeholder: {cfg.placeholder.type}\n"
        f"Overlay: {'enabled' if cfg.overlay.enabled else 'disabled'}\n"
        f"Targets:\n{target_list}"
    )

    # -- Graceful shutdown -------------------------------------------------
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    log.info("Shutting down...")
    notifier.send("\U0001f50c <b>immortal-stream stopping</b>")
    if bot:
        await bot.stop()
    await manager.stop()
    await mediamtx.stop()
    await notifier.stop()
    log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
