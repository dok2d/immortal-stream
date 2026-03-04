#!/usr/bin/env python3
"""
immortal-stream — fault-tolerant live streaming with 3-layer compositing.

Entry point: loads config, starts mediamtx, stream manager (which polls
the mediamtx API for stream events), compositor and output FFmpeg processes.
"""
import asyncio
import logging
import os
import signal
import sys

from config import load_config
from mediamtx_manager import MediamtxManager
from stream_manager import StreamManager
from telegram import TelegramNotifier, NoopNotifier

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/etc/immortal-stream/config.yaml")


async def main() -> None:
    # ------------------------------------------------------------------ #
    #  Load configuration                                                  #
    # ------------------------------------------------------------------ #
    try:
        cfg = load_config(CONFIG_PATH)
    except Exception as e:
        log.critical("Failed to load config from %s: %s", CONFIG_PATH, e)
        sys.exit(1)

    log.info(
        "Config loaded: ingest=%d, srt=%d, targets=%d, placeholder=%s",
        cfg.ingest.port,
        cfg.ingest.srt_port,
        len(cfg.output.targets),
        cfg.placeholder.type,
    )

    # ------------------------------------------------------------------ #
    #  Telegram notifier                                                   #
    # ------------------------------------------------------------------ #
    if cfg.telegram.enabled and cfg.telegram.bot_token and cfg.telegram.chat_id:
        notifier = TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id)
        log.info("Telegram notifications enabled (chat=%s)", cfg.telegram.chat_id)
    else:
        notifier = NoopNotifier()
        log.info("Telegram notifications disabled")

    notifier.start()

    # ------------------------------------------------------------------ #
    #  mediamtx                                                            #
    # ------------------------------------------------------------------ #
    mediamtx = MediamtxManager(cfg)
    await mediamtx.start()
    ready = await mediamtx.wait_ready(timeout=15)
    if not ready:
        log.critical("mediamtx failed to start")
        notifier.send("💀 <b>immortal-stream failed to start</b>: mediamtx not ready")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    #  Stream manager (polls mediamtx API for stream events)              #
    # ------------------------------------------------------------------ #
    manager = StreamManager(cfg, notifier)
    await manager.start()

    # ------------------------------------------------------------------ #
    #  Startup notification                                                #
    # ------------------------------------------------------------------ #
    target_list = "\n".join(f"  • {t}" for t in cfg.output.targets) or "  (none)"
    notifier.send(
        "✅ <b>immortal-stream started</b>\n"
        f"Ingest RTMP: :{cfg.ingest.port}\n"
        f"Ingest SRT:  :{cfg.ingest.srt_port}\n"
        f"Placeholder: {cfg.placeholder.type}\n"
        f"Overlay: {'enabled' if cfg.overlay.enabled else 'disabled'}\n"
        f"Targets:\n{target_list}"
    )

    # ------------------------------------------------------------------ #
    #  Graceful shutdown                                                   #
    # ------------------------------------------------------------------ #
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    log.info("Shutting down...")
    notifier.send("🔌 <b>immortal-stream stopping</b>")
    await manager.stop()
    await mediamtx.stop()
    await notifier.stop()
    log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
