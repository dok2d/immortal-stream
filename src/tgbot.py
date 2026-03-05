"""
Telegram bot for runtime configuration of immortal-stream.

Uses long-polling (no public webhook needed). All commands are restricted
to the configured chat_id. Changes take effect immediately by restarting
the relevant FFmpeg process.

Commands
--------
/status                          -- current state
/placeholder black               -- black screen
/placeholder text <text>         -- text placeholder
/placeholder image <path>        -- image placeholder (JPEG/PNG)
/placeholder video <path>        -- video placeholder (loops)
/placeholder opacity <0.0-1.0>   -- placeholder opacity
/overlay off                     -- disable overlay
/overlay text <text>             -- text overlay (shown on live stream)
/overlay image <path>            -- image overlay
/overlay x|y <pixels>            -- overlay position
/overlay opacity <0.0-1.0>       -- overlay opacity
/overlay size <px>               -- font size (text overlays)
/overlay color <name|#hex>       -- font color (text overlays)
/target list                     -- list output RTMP targets
/target add <rtmp://...>         -- add target
/target remove <rtmp://...>      -- remove target
/target set <rtmp://...>         -- replace all targets with one
/output bitrate <value>          -- video bitrate (e.g. 6000k)
/output fps <n>                  -- frame rate
/output size <WxH>               -- output resolution (e.g. 1920x1080)
/output preset <name>            -- x264 preset
/help                            -- this message
"""
import asyncio
import json
import logging
import os
import urllib.request
from typing import Callable, Optional, TYPE_CHECKING
from urllib.parse import urlencode
from urllib.request import Request

from config import Config, _X264_PRESETS

if TYPE_CHECKING:
    from stream_manager import StreamManager

log = logging.getLogger("tgbot")

POLL_TIMEOUT = 30  # long-poll timeout in seconds


class TelegramBot:
    def __init__(self, cfg: Config, manager: "StreamManager"):
        self.cfg = cfg
        self.manager = manager
        self._base = f"https://api.telegram.org/bot{cfg.telegram.bot_token}"
        self._chat_id = cfg.telegram.chat_id
        self._running = False

    def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())
        log.info("Telegram bot started (chat_id=%s)", self._chat_id)

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ #
    #  Polling                                                             #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        offset = 0
        while self._running:
            try:
                updates = await self._get_updates(offset)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    asyncio.create_task(self._handle_update(upd))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Bot poll error: %s", e)
                await asyncio.sleep(5)

    async def _get_updates(self, offset: int) -> list:
        url = (
            f"{self._base}/getUpdates"
            f"?offset={offset}&timeout={POLL_TIMEOUT}"
            f'&allowed_updates=["message"]'
        )
        raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 5).read(),
        )
        return json.loads(raw).get("result", [])

    async def _send(self, text: str) -> None:
        data = urlencode(
            {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = Request(
            f"{self._base}/sendMessage",
            data=data,
            method="POST",
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=10)
        )

    # ------------------------------------------------------------------ #
    #  Dispatch                                                            #
    # ------------------------------------------------------------------ #

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        # Security: only accept messages from the configured chat
        if str(msg.get("chat", {}).get("id", "")) != self._chat_id:
            return

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return

        # Split into command and rest; strip bot username suffix (@BotName)
        head, _, arg_str = text.partition(" ")
        cmd = head.lstrip("/").lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        args = arg_str.split() if arg_str else []

        log.info("Bot command: /%s %s", cmd, arg_str[:60])

        try:
            reply = await self._dispatch(cmd, args, arg_str.strip())
        except Exception as e:
            log.exception("Bot command error")
            reply = f"\u274c Error: {e}"

        if reply:
            try:
                await self._send(reply)
            except Exception as e:
                log.warning("Bot send failed: %s", e)

    async def _dispatch(self, cmd: str, args: list, arg_str: str) -> str:
        handlers = {
            "start": lambda: _HELP_TEXT,
            "help": lambda: _HELP_TEXT,
            "status": lambda: self._status(),
            "placeholder": lambda: self._placeholder(args, arg_str),
            "overlay": lambda: self._overlay(args, arg_str),
            "target": lambda: self._target(args),
            "output": lambda: self._output(args),
        }
        handler = handlers.get(cmd)
        if handler:
            result = handler()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return f"\u2753 Unknown command: <code>/{cmd}</code>\n/help for list"

    # ------------------------------------------------------------------ #
    #  /status                                                             #
    # ------------------------------------------------------------------ #

    def _status(self) -> str:
        stream = self.manager._current_stream
        ph = self.cfg.placeholder
        ov = self.cfg.overlay
        v = self.cfg.output.video

        if stream:
            state = (
                f"\U0001f7e2 <b>LIVE</b> \u2014 <code>{stream.path}</code>\n"
                f"  {stream.codec_video} {stream.width}\u00d7{stream.height}"
                f" @{stream.fps}fps"
                + (f"  audio: {stream.codec_audio}" if stream.has_audio else "")
            )
        else:
            state = "\u26ab <b>IDLE</b> (placeholder active)"

        ph_desc = ph.type
        if ph.type == "text" and ph.text:
            ph_desc += f": <code>{ph.text}</code>"
        elif ph.path:
            ph_desc += f": <code>{ph.path}</code>"
        if ph.opacity < 1.0:
            ph_desc += f" opacity={ph.opacity:.2f}"

        if ov.enabled:
            if ov.type == "text":
                ov_desc = f"text <code>{ov.text}</code>"
            else:
                ov_desc = f"image <code>{ov.path}</code>"
            ov_desc += f" at ({ov.x},{ov.y})"
            if ov.opacity < 1.0:
                ov_desc += f" opacity={ov.opacity:.2f}"
        else:
            ov_desc = "disabled"

        targets = (
            "\n".join(f"  \u2022 <code>{t}</code>" for t in self.cfg.output.targets)
            or "  (none \u2014 use /target add)"
        )

        return (
            f"{state}\n\n"
            f"<b>Placeholder:</b> {ph_desc}\n"
            f"<b>Overlay:</b> {ov_desc}\n"
            f"<b>Output:</b> {v.width}\u00d7{v.height} @{v.fps}fps "
            f"{v.bitrate} preset={v.preset}\n"
            f"<b>Targets:</b>\n{targets}"
        )

    # ------------------------------------------------------------------ #
    #  /placeholder                                                        #
    # ------------------------------------------------------------------ #

    async def _placeholder(self, args: list, arg_str: str) -> str:
        if not args:
            return (
                "Usage:\n"
                "/placeholder black\n"
                "/placeholder text <text>\n"
                "/placeholder image <path>\n"
                "/placeholder video <path>\n"
                "/placeholder opacity <0.0\u20131.0>"
            )

        sub = args[0].lower()

        if sub == "black":
            self.cfg.placeholder.type = "black"
            self.cfg.placeholder.path = None
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return "\u2705 Placeholder \u2192 black screen"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /placeholder text <your text here>"
            self.cfg.placeholder.type = "text"
            self.cfg.placeholder.text = text
            self.cfg.placeholder.path = None
            await self.manager.reload_compositor()
            return f"\u2705 Placeholder \u2192 text: <code>{text}</code>"

        if sub in ("image", "video"):
            path = arg_str[len(sub):].strip()
            if not path:
                return f"Usage: /placeholder {sub} <path>"
            if not os.path.isfile(path):
                return f"\u274c File not found: <code>{path}</code>"
            self.cfg.placeholder.type = sub
            self.cfg.placeholder.path = path
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return f"\u2705 Placeholder \u2192 {sub}: <code>{path}</code>"

        if sub == "opacity":
            return await _set_float(
                args, self.cfg.placeholder, "opacity",
                0.0, 1.0, self.manager.reload_compositor,
                "Placeholder opacity",
            )

        return f"\u2753 Unknown: /placeholder {sub}"

    # ------------------------------------------------------------------ #
    #  /overlay                                                            #
    # ------------------------------------------------------------------ #

    async def _overlay(self, args: list, arg_str: str) -> str:
        if not args:
            return (
                "Usage:\n"
                "/overlay off\n"
                "/overlay text <text>\n"
                "/overlay image <path>\n"
                "/overlay x|y <pixels>\n"
                "/overlay opacity <0.0\u20131.0>\n"
                "/overlay size <px>\n"
                "/overlay color <name|#hex>"
            )

        sub = args[0].lower()
        reload_if_active: Optional[Callable] = (
            self.manager.reload_compositor if self.cfg.overlay.enabled else None
        )

        if sub == "off":
            self.cfg.overlay.enabled = False
            await self.manager.reload_compositor()
            return "\u2705 Overlay disabled"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /overlay text <your text here>"
            self.cfg.overlay.enabled = True
            self.cfg.overlay.type = "text"
            self.cfg.overlay.text = text
            await self.manager.reload_compositor()
            return f"\u2705 Overlay \u2192 text: <code>{text}</code>"

        if sub == "image":
            path = arg_str[len("image"):].strip()
            if not path:
                return "Usage: /overlay image <path>"
            if not os.path.isfile(path):
                return f"\u274c File not found: <code>{path}</code>"
            self.cfg.overlay.enabled = True
            self.cfg.overlay.type = "image"
            self.cfg.overlay.path = path
            await self.manager.reload_compositor()
            return f"\u2705 Overlay \u2192 image: <code>{path}</code>"

        if sub == "x":
            return await _set_int(
                args, self.cfg.overlay, "x", reload_if_active, "Overlay X",
            )
        if sub == "y":
            return await _set_int(
                args, self.cfg.overlay, "y", reload_if_active, "Overlay Y",
            )
        if sub == "opacity":
            return await _set_float(
                args, self.cfg.overlay, "opacity",
                0.0, 1.0, reload_if_active, "Overlay opacity",
            )
        if sub == "size":
            return await _set_int(
                args, self.cfg.overlay, "font_size",
                reload_if_active, "Font size",
            )
        if sub == "color":
            if len(args) < 2:
                return "Usage: /overlay color <name or #RRGGBB>"
            self.cfg.overlay.font_color = args[1]
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Overlay color \u2192 {args[1]}"

        return f"\u2753 Unknown: /overlay {sub}"

    # ------------------------------------------------------------------ #
    #  /target                                                             #
    # ------------------------------------------------------------------ #

    async def _target(self, args: list) -> str:
        if not args:
            return (
                "Usage:\n"
                "/target list\n"
                "/target add <rtmp://...>\n"
                "/target remove <rtmp://...>\n"
                "/target set <rtmp://...>"
            )

        sub = args[0].lower()

        if sub == "list":
            if not self.cfg.output.targets:
                return "No targets. Use /target add <url>"
            lines = "\n".join(
                f"{i+1}. <code>{t}</code>"
                for i, t in enumerate(self.cfg.output.targets)
            )
            return f"<b>Output targets:</b>\n{lines}"

        if sub == "add":
            if len(args) < 2:
                return "Usage: /target add <rtmp://...>"
            url = args[1]
            if url in self.cfg.output.targets:
                return f"Already present: <code>{url}</code>"
            self.cfg.output.targets.append(url)
            await self.manager.reload_output()
            return f"\u2705 Added: <code>{url}</code>"

        if sub == "remove":
            if len(args) < 2:
                return "Usage: /target remove <rtmp://...>"
            url = args[1]
            if url not in self.cfg.output.targets:
                return f"Not in list: <code>{url}</code>"
            self.cfg.output.targets.remove(url)
            if self.cfg.output.targets:
                await self.manager.reload_output()
                return f"\u2705 Removed: <code>{url}</code>"
            return (
                f"\u2705 Removed: <code>{url}</code>\n"
                "\u26a0\ufe0f No targets left \u2014 output stopped"
            )

        if sub == "set":
            if len(args) < 2:
                return "Usage: /target set <rtmp://...>"
            self.cfg.output.targets = [args[1]]
            await self.manager.reload_output()
            return f"\u2705 Target set to: <code>{args[1]}</code>"

        return f"\u2753 Unknown: /target {sub}"

    # ------------------------------------------------------------------ #
    #  /output                                                             #
    # ------------------------------------------------------------------ #

    async def _output(self, args: list) -> str:
        presets = sorted(_X264_PRESETS)

        if not args:
            return (
                "Usage:\n"
                "/output bitrate <value>   e.g. 6000k or 8M\n"
                "/output fps <n>\n"
                "/output size <WxH>        e.g. 1920x1080\n"
                f"/output preset <name>     one of: {', '.join(presets)}"
            )

        sub = args[0].lower()

        if sub == "bitrate":
            if len(args) < 2:
                return "Usage: /output bitrate <value> (e.g. 6000k)"
            self.cfg.output.video.bitrate = args[1]
            await self.manager.reload_compositor()
            return f"\u2705 Video bitrate \u2192 {args[1]}"

        if sub == "fps":
            if len(args) < 2:
                return "Usage: /output fps <number>"
            try:
                fps = int(args[1])
                if not 1 <= fps <= 120:
                    raise ValueError
                self.cfg.output.video.fps = fps
                self.cfg.output.video.gop = fps * 2
                await self.manager.reload_compositor()
                return f"\u2705 FPS \u2192 {fps} (gop={fps * 2})"
            except ValueError:
                return "\u274c FPS must be 1\u2013120"

        if sub == "size":
            if len(args) < 2:
                return "Usage: /output size <WxH> (e.g. 1920x1080)"
            try:
                w_str, h_str = args[1].lower().split("x")
                w, h = int(w_str), int(h_str)
                if not (160 <= w <= 7680 and 90 <= h <= 4320):
                    raise ValueError
                self.cfg.output.video.width = w
                self.cfg.output.video.height = h
                await self.manager.reload_compositor()
                return f"\u2705 Output size \u2192 {w}\u00d7{h}"
            except (ValueError, TypeError):
                return "\u274c Format: WxH (e.g. 1920x1080)"

        if sub == "preset":
            if len(args) < 2 or args[1] not in _X264_PRESETS:
                return f"Usage: /output preset <{' | '.join(presets)}>"
            self.cfg.output.video.preset = args[1]
            await self.manager.reload_compositor()
            return f"\u2705 Preset \u2192 {args[1]}"

        return f"\u2753 Unknown: /output {sub}"


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

async def _set_int(
    args: list, obj, attr: str,
    reload_fn: Optional[Callable], label: str,
) -> str:
    """Generic integer parameter setter with reload."""
    if len(args) < 2:
        return f"Usage: /... {attr} <integer>"
    try:
        v = int(args[1])
        setattr(obj, attr, v)
        if reload_fn:
            await reload_fn()
        return f"\u2705 {label} \u2192 {v}"
    except ValueError:
        return f"\u274c {label} must be an integer"


async def _set_float(
    args: list, obj, attr: str,
    min_v: float, max_v: float,
    reload_fn: Optional[Callable], label: str,
) -> str:
    """Generic float parameter setter with range validation and reload."""
    if len(args) < 2:
        return f"Usage: /... {attr} <{min_v}\u2013{max_v}>"
    try:
        v = float(args[1])
        if not min_v <= v <= max_v:
            raise ValueError
        setattr(obj, attr, v)
        if reload_fn:
            await reload_fn()
        return f"\u2705 {label} \u2192 {v:.2f}"
    except ValueError:
        return f"\u274c {label} must be {min_v}\u2013{max_v}"


_HELP_TEXT = (
    "<b>immortal-stream bot commands</b>\n\n"
    "<b>Status</b>\n"
    "/status \u2014 current stream state and settings\n\n"
    "<b>Placeholder</b> (shown when no stream)\n"
    "/placeholder black\n"
    "/placeholder text <i>text</i>\n"
    "/placeholder image <i>path</i>\n"
    "/placeholder video <i>path</i>\n"
    "/placeholder opacity <i>0.0\u20131.0</i>\n\n"
    "<b>Overlay</b> (shown on top of live stream)\n"
    "/overlay off\n"
    "/overlay text <i>text</i>\n"
    "/overlay image <i>path</i>\n"
    "/overlay x|y <i>pixels</i>\n"
    "/overlay opacity <i>0.0\u20131.0</i>\n"
    "/overlay size <i>px</i>   (text only)\n"
    "/overlay color <i>name|#hex</i>   (text only)\n\n"
    "<b>Targets</b>\n"
    "/target list\n"
    "/target add <i>rtmp://...</i>\n"
    "/target remove <i>rtmp://...</i>\n"
    "/target set <i>rtmp://...</i>\n\n"
    "<b>Output encoding</b>\n"
    "/output bitrate <i>6000k</i>\n"
    "/output fps <i>30</i>\n"
    "/output size <i>1920x1080</i>\n"
    "/output preset <i>ultrafast</i>"
)
