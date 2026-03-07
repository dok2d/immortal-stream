"""
Telegram bot for runtime configuration of immortal-stream.

Menu-driven UI with inline keyboard buttons.  Text commands remain
supported for power users.  All interactions are restricted to the
configured chat_id.

Button navigation uses callback_query with data format "section:action".
When a button requires text input (e.g. adding a target URL), the bot
enters "awaiting" mode and treats the next text message as the value.
"""
import asyncio
import json
import logging
import os
import urllib.request
from typing import Callable, Optional, TYPE_CHECKING

from config import Config, _X264_PRESETS

if TYPE_CHECKING:
    from stream_manager import StreamManager

log = logging.getLogger("tgbot")

POLL_TIMEOUT = 30


# ═══════════════════════════════════════════════════════════════════════════
#  Bot
# ═══════════════════════════════════════════════════════════════════════════

class TelegramBot:
    def __init__(self, cfg: Config, manager: "StreamManager"):
        self.cfg = cfg
        self.manager = manager
        self._base = f"https://api.telegram.org/bot{cfg.telegram.bot_token}"
        self._chat_id = cfg.telegram.chat_id
        self._running = False
        self._awaiting: Optional[str] = None  # e.g. "target:add"

    def start(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())
        log.info("Telegram bot started (chat_id=%s)", self._chat_id)

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------ #
    #  Telegram API helpers                                                #
    # ------------------------------------------------------------------ #

    async def _api(self, method: str, payload: dict) -> dict:
        url = f"{self._base}/{method}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=10).read()
        )
        return json.loads(raw)

    async def _send(self, text: str, keyboard=None) -> dict:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return await self._api("sendMessage", payload)

    async def _send_prompt(self, text: str, cancel_cb: str = "menu:main") -> dict:
        """Send a message with ForceReply markup.

        In Telegram groups the bot only sees replies to its own messages.
        Using force_reply ensures the user's response is tagged as a reply,
        making it visible to the bot regardless of privacy settings.
        """
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "force_reply": True,
                "selective": False,
            },
        }
        return await self._api("sendMessage", payload)

    async def _download_file(self, file_id: str, dest_dir: str = "/tmp/tgbot_media") -> str:
        """Download a Telegram file by file_id and return the local path."""
        resp = await self._api("getFile", {"file_id": file_id})
        file_path = resp.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError("Could not get file path from Telegram")

        dl_url = (
            f"https://api.telegram.org/file/bot"
            f"{self.cfg.telegram.bot_token}/{file_path}"
        )
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_path = os.path.join(dest_dir, f"tg_{file_id[:16]}{ext}")

        os.makedirs(dest_dir, exist_ok=True)

        def _fetch():
            urllib.request.urlretrieve(dl_url, local_path)

        await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return local_path

    async def _edit(self, msg_id: int, text: str, keyboard=None) -> dict:
        payload = {
            "chat_id": self._chat_id,
            "message_id": msg_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return await self._api("editMessageText", payload)

    async def _answer_cb(self, cb_id: str, text: str = "") -> None:
        payload = {"callback_query_id": cb_id}
        if text:
            payload["text"] = text
        await self._api("answerCallbackQuery", payload)

    # ------------------------------------------------------------------ #
    #  Polling                                                             #
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        offset = 0
        while self._running:
            try:
                url = (
                    f"{self._base}/getUpdates"
                    f"?offset={offset}&timeout={POLL_TIMEOUT}"
                    f'&allowed_updates=["message","callback_query"]'
                )
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(
                        url, timeout=POLL_TIMEOUT + 5
                    ).read(),
                )
                for upd in json.loads(raw).get("result", []):
                    offset = upd["update_id"] + 1
                    asyncio.create_task(self._handle_update(upd))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Bot poll error: %s", e)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------ #
    #  Update routing                                                      #
    # ------------------------------------------------------------------ #

    async def _handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self._on_callback(update["callback_query"])
            elif "message" in update or "edited_message" in update:
                await self._on_message(
                    update.get("message") or update["edited_message"]
                )
        except Exception:
            log.exception("Bot update error")

    def _check_chat(self, msg_or_cb: dict) -> bool:
        chat = msg_or_cb.get("message", msg_or_cb).get("chat", {})
        return str(chat.get("id", "")) == self._chat_id

    # ------------------------------------------------------------------ #
    #  Callback query handler (inline buttons)                             #
    # ------------------------------------------------------------------ #

    async def _on_callback(self, cb: dict) -> None:
        if not self._check_chat(cb):
            return

        data = cb.get("data", "")
        msg_id = cb.get("message", {}).get("message_id")
        cb_id = cb.get("id", "")
        log.info("Bot callback: %s", data)

        self._awaiting = None  # cancel any pending text input

        try:
            text, kb, toast = await self._route_callback(data)
            if text and msg_id:
                try:
                    await self._edit(msg_id, text, kb)
                except Exception:
                    # Message unchanged (same text) — ignore
                    pass
            await self._answer_cb(cb_id, toast or "")
        except Exception as e:
            log.exception("Callback error")
            await self._answer_cb(cb_id, f"Error: {e}")

    async def _route_callback(self, data: str):
        """Dispatch callback data → (text, keyboard, toast)."""
        p = data.split(":")
        section = p[0]
        handlers = {
            "menu":   self._cb_menu,
            "status": self._cb_status,
            "ph":     self._cb_placeholder,
            "ov":     self._cb_overlay,
            "target": self._cb_target,
            "out":    self._cb_output,
            "power":  self._cb_power,
        }
        handler = handlers.get(section)
        if handler:
            return await handler(p)
        return None, None, "Unknown"

    # ------------------------------------------------------------------ #
    #  Message handler (text commands + awaited input)                      #
    # ------------------------------------------------------------------ #

    async def _on_message(self, msg: dict) -> None:
        if not msg or str(msg.get("chat", {}).get("id", "")) != self._chat_id:
            return

        text = (msg.get("text") or "").strip()

        # Handle photo uploads when awaiting an image
        photo = msg.get("photo")
        if photo and self._awaiting and self._awaiting in (
            "ph:image", "ov:image",
        ):
            action = self._awaiting
            self._awaiting = None
            try:
                # Use the largest photo (last in the array)
                file_id = photo[-1]["file_id"]
                local_path = await self._download_file(file_id)
                reply, kb = await self._handle_awaited(action, local_path)
            except Exception as e:
                reply = f"\u274c Failed to download photo: {e}"
                kb = [[_btn("\u25c0\ufe0f Menu", "menu:main")]]
            await self._send(reply, kb)
            return

        # Awaited text input from a button flow
        if self._awaiting and not text.startswith("/"):
            action = self._awaiting
            self._awaiting = None
            try:
                reply, kb = await self._handle_awaited(action, text)
            except Exception as e:
                reply = f"\u274c {e}"
                kb = [[_btn("\u25c0\ufe0f Menu", "menu:main")]]
            await self._send(reply, kb)
            return

        if not text.startswith("/"):
            return

        head, _, arg_str = text.partition(" ")
        cmd = head.lstrip("/").lower().split("@")[0]
        args = arg_str.split() if arg_str else []
        log.info("Bot command: /%s %s", cmd, arg_str[:60])

        try:
            result = await self._route_text(cmd, args, arg_str.strip())
        except Exception as e:
            log.exception("Bot command error")
            result = f"\u274c Error: {e}"

        if result:
            try:
                if isinstance(result, tuple):
                    await self._send(result[0], result[1])
                else:
                    await self._send(result)
            except Exception as e:
                log.warning("Bot send failed: %s", e)

    # ═══════════════════════════════════════════════════════════════════ #
    #  CALLBACK HANDLERS (buttons)                                        #
    # ═══════════════════════════════════════════════════════════════════ #

    async def _cb_menu(self, p):
        return self._text_main_menu(), _KB_MAIN, ""

    async def _cb_status(self, p):
        return self._text_status(), _kb_status(), ""

    # -- Placeholder -------------------------------------------------------

    async def _cb_placeholder(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph(), _kb_ph(self.cfg), ""

        if act == "black":
            self.cfg.placeholder.type = "black"
            self.cfg.placeholder.path = None
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return (
                self._text_ph() + "\n\n\u2705 Black screen",
                _kb_ph(self.cfg), "Black",
            )

        if act == "testcard":
            self.cfg.placeholder.type = "testcard"
            self.cfg.placeholder.path = None
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return (
                self._text_ph() + "\n\n\u2705 Test card with clock",
                _kb_ph(self.cfg), "Testcard",
            )

        if act == "tz":
            self._awaiting = "ph:tz"
            await self._send_prompt(
                f"Current timezone: <code>{self.cfg.placeholder.timezone}</code>\n"
                "Send new timezone (e.g. <code>Europe/Moscow</code>, "
                "<code>US/Eastern</code>, <code>UTC</code>):"
            )
            return None, None, ""

        if act in ("text", "image", "video", "opacity"):
            labels = {
                "text": "\u270f\ufe0f Send placeholder text:",
                "image": "\U0001f4ce Send image file path or send a photo:",
                "video": "\U0001f3ac Send video file path:",
                "opacity": (
                    f"Current: {self.cfg.placeholder.opacity:.2f}\n"
                    "Send new value (0.0\u20131.0):"
                ),
            }
            self._awaiting = f"ph:{act}"
            await self._send_prompt(labels[act])
            return None, None, ""

        return self._text_ph(), _kb_ph(self.cfg), ""

    # -- Overlay -----------------------------------------------------------

    async def _cb_overlay(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov(), _kb_ov(self.cfg), ""

        if act == "off":
            self.cfg.overlay.enabled = False
            await self.manager.reload_compositor()
            return self._text_ov() + "\n\n\u2705 Disabled", _kb_ov(self.cfg), "Off"

        if act == "on":
            self.cfg.overlay.enabled = True
            await self.manager.reload_compositor()
            return self._text_ov() + "\n\n\u2705 Enabled", _kb_ov(self.cfg), "On"

        prompts = {
            "text":    "\u270f\ufe0f Send overlay text:",
            "image":   "\U0001f4ce Send overlay image path or send a photo:",
            "pos":     f"Current: ({self.cfg.overlay.x}, {self.cfg.overlay.y})\nSend as <code>X Y</code>:",
            "opacity": f"Current: {self.cfg.overlay.opacity:.2f}\nSend value (0.0\u20131.0):",
            "size":    f"Current: {self.cfg.overlay.font_size}px\nSend new size:",
            "color":   f"Current: {self.cfg.overlay.font_color}\nSend color name or #RRGGBB:",
        }
        if act in prompts:
            self._awaiting = f"ov:{act}"
            await self._send_prompt(prompts[act])
            return None, None, ""

        return self._text_ov(), _kb_ov(self.cfg), ""

    # -- Targets -----------------------------------------------------------

    async def _cb_target(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_targets(), _kb_targets(self.cfg), ""

        if act == "add":
            self._awaiting = "target:add"
            await self._send_prompt("\U0001f517 Send the RTMP/RTMPS URL:")
            return None, None, ""

        if act == "rm":
            idx = int(p[2]) if len(p) > 2 else -1
            if 0 <= idx < len(self.cfg.output.targets):
                removed = self.cfg.output.targets.pop(idx)
                if self.cfg.output.targets:
                    await self.manager.reload_output()
                return (
                    self._text_targets()
                    + f"\n\n\u2705 Removed:\n<code>{removed}</code>",
                    _kb_targets(self.cfg), "Removed",
                )
            return self._text_targets(), _kb_targets(self.cfg), "Invalid"

        return self._text_targets(), _kb_targets(self.cfg), ""

    # -- Output encoding ---------------------------------------------------

    async def _cb_output(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_output(), _kb_out(), ""

        prompts = {
            "bitrate": f"Current: {self.cfg.output.video.bitrate}\nSend new (e.g. 6000k, 8M):",
            "fps":     f"Current: {self.cfg.output.video.fps}\nSend new FPS (1\u2013120):",
            "size":    f"Current: {self.cfg.output.video.width}\u00d7{self.cfg.output.video.height}\nSend as WxH:",
        }
        if act in prompts:
            self._awaiting = f"out:{act}"
            await self._send_prompt(prompts[act])
            return None, None, ""

        if act == "preset":
            return self._text_output(), _kb_presets(), ""

        # Handle preset selection: "p_ultrafast"
        if act.startswith("p_"):
            preset = act[2:]
            if preset in _X264_PRESETS:
                self.cfg.output.video.preset = preset
                await self.manager.reload_compositor()
                return (
                    self._text_output() + f"\n\n\u2705 Preset \u2192 {preset}",
                    _kb_out(), preset,
                )

        return self._text_output(), _kb_out(), ""

    # -- Power -------------------------------------------------------------

    async def _cb_power(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_power(), _kb_power(self.manager), ""

        if act == "stop":
            await self.manager.pause_all()
            self.manager.notifier.send(
                "\u23f8\ufe0f <b>Service paused</b> (via bot)"
            )
            return self._text_power(), _kb_power(self.manager), "Stopped"

        if act == "start":
            await self.manager.resume_all()
            self.manager.notifier.send(
                "\u25b6\ufe0f <b>Service resumed</b> (via bot)"
            )
            return self._text_power(), _kb_power(self.manager), "Started"

        return self._text_power(), _kb_power(self.manager), ""

    # ═══════════════════════════════════════════════════════════════════ #
    #  AWAITED TEXT INPUT                                                  #
    # ═══════════════════════════════════════════════════════════════════ #

    async def _handle_awaited(self, action: str, text: str):
        """Process text input for the given awaited action.
        Returns (reply_text, keyboard)."""

        # -- Placeholder --
        if action == "ph:text":
            self.cfg.placeholder.type = "text"
            self.cfg.placeholder.text = text.strip("\"'")
            self.cfg.placeholder.path = None
            await self.manager.reload_compositor()
            return f"\u2705 Placeholder text:\n<code>{text}</code>", _kb_ph(self.cfg)

        if action in ("ph:image", "ph:video"):
            kind = action.split(":")[1]
            if not os.path.isfile(text):
                return f"\u274c File not found: <code>{text}</code>", _kb_ph(self.cfg)
            self.cfg.placeholder.type = kind
            self.cfg.placeholder.path = text
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return f"\u2705 Placeholder {kind}:\n<code>{text}</code>", _kb_ph(self.cfg)

        if action == "ph:opacity":
            v = float(text)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ph(self.cfg)
            self.cfg.placeholder.opacity = v
            await self.manager.reload_compositor()
            return f"\u2705 Opacity: {v:.2f}", _kb_ph(self.cfg)

        if action == "ph:tz":
            tz = text.strip()
            self.cfg.placeholder.timezone = tz
            await self.manager.reload_compositor()
            return f"\u2705 Timezone: <code>{tz}</code>", _kb_ph(self.cfg)

        # -- Overlay --
        if action == "ov:text":
            self.cfg.overlay.enabled = True
            self.cfg.overlay.type = "text"
            self.cfg.overlay.text = text.strip("\"'")
            await self.manager.reload_compositor()
            return f"\u2705 Overlay text:\n<code>{text}</code>", _kb_ov(self.cfg)

        if action == "ov:image":
            if not os.path.isfile(text):
                return f"\u274c File not found: <code>{text}</code>", _kb_ov(self.cfg)
            self.cfg.overlay.enabled = True
            self.cfg.overlay.type = "image"
            self.cfg.overlay.path = text
            await self.manager.reload_compositor()
            return f"\u2705 Overlay image:\n<code>{text}</code>", _kb_ov(self.cfg)

        if action == "ov:pos":
            parts = text.split()
            if len(parts) < 2:
                return "\u274c Send as: X Y", _kb_ov(self.cfg)
            x, y = int(parts[0]), int(parts[1])
            self.cfg.overlay.x = x
            self.cfg.overlay.y = y
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Position: ({x}, {y})", _kb_ov(self.cfg)

        if action == "ov:opacity":
            v = float(text)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ov(self.cfg)
            self.cfg.overlay.opacity = v
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Opacity: {v:.2f}", _kb_ov(self.cfg)

        if action == "ov:size":
            v = int(text)
            self.cfg.overlay.font_size = v
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Font size: {v}px", _kb_ov(self.cfg)

        if action == "ov:color":
            self.cfg.overlay.font_color = text.strip()
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Color: {text.strip()}", _kb_ov(self.cfg)

        # -- Targets --
        if action == "target:add":
            url = text.strip()
            if url in self.cfg.output.targets:
                return f"Already present:\n<code>{url}</code>", _kb_targets(self.cfg)
            self.cfg.output.targets.append(url)
            await self.manager.reload_output()
            return f"\u2705 Added:\n<code>{url}</code>", _kb_targets(self.cfg)

        # -- Output encoding --
        if action == "out:bitrate":
            self.cfg.output.video.bitrate = text.strip()
            await self.manager.reload_compositor()
            return f"\u2705 Bitrate: {text.strip()}", _kb_out()

        if action == "out:fps":
            fps = int(text)
            if not 1 <= fps <= 120:
                return "\u274c Must be 1\u2013120", _kb_out()
            self.cfg.output.video.fps = fps
            self.cfg.output.video.gop = fps * 2
            await self.manager.reload_compositor()
            return f"\u2705 FPS: {fps} (gop={fps * 2})", _kb_out()

        if action == "out:size":
            w_s, h_s = text.lower().split("x")
            w, h = int(w_s), int(h_s)
            if not (160 <= w <= 7680 and 90 <= h <= 4320):
                return "\u274c Invalid size range", _kb_out()
            self.cfg.output.video.width = w
            self.cfg.output.video.height = h
            await self.manager.reload_compositor()
            return f"\u2705 Size: {w}\u00d7{h}", _kb_out()

        return "\u274c Unknown action", [[_btn("\u25c0\ufe0f Menu", "menu:main")]]

    # ═══════════════════════════════════════════════════════════════════ #
    #  TEXT COMMAND ROUTING (backward-compatible)                          #
    # ═══════════════════════════════════════════════════════════════════ #

    async def _route_text(self, cmd: str, args: list, arg_str: str):
        """Handle text commands. Returns str or (text, keyboard)."""
        if cmd in ("start", "help", "menu"):
            return self._text_main_menu(), _KB_MAIN

        if cmd == "status":
            return self._text_status(), _kb_status()

        if cmd == "stop":
            if self.manager.is_paused:
                return "\u23f8\ufe0f Already paused"
            await self.manager.pause_all()
            self.manager.notifier.send(
                "\u23f8\ufe0f <b>Service paused</b> (via bot)"
            )
            return self._text_power(), _kb_power(self.manager)

        if cmd == "resume":
            if not self.manager.is_paused:
                return "\u25b6\ufe0f Already running"
            await self.manager.resume_all()
            self.manager.notifier.send(
                "\u25b6\ufe0f <b>Service resumed</b> (via bot)"
            )
            return self._text_power(), _kb_power(self.manager)

        if cmd == "placeholder":
            return await self._txt_placeholder(args, arg_str)
        if cmd == "overlay":
            return await self._txt_overlay(args, arg_str)
        if cmd == "target":
            return await self._txt_target(args)
        if cmd == "output":
            return await self._txt_output(args)

        return f"\u2753 Unknown: <code>/{cmd}</code>\nTry /menu"

    # -- Text: /placeholder ------------------------------------------------

    async def _txt_placeholder(self, args: list, arg_str: str):
        if not args:
            return self._text_ph(), _kb_ph(self.cfg)

        sub = args[0].lower()

        if sub == "black":
            self.cfg.placeholder.type = "black"
            self.cfg.placeholder.path = None
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return "\u2705 Placeholder \u2192 black"

        if sub == "testcard":
            self.cfg.placeholder.type = "testcard"
            self.cfg.placeholder.path = None
            self.cfg.placeholder.text = None
            await self.manager.reload_compositor()
            return "\u2705 Placeholder \u2192 testcard (clock overlay)"

        if sub == "timezone":
            tz = arg_str[len("timezone"):].strip()
            if not tz:
                return f"Current: <code>{self.cfg.placeholder.timezone}</code>\nUsage: /placeholder timezone <TZ>"
            self.cfg.placeholder.timezone = tz
            await self.manager.reload_compositor()
            return f"\u2705 Timezone \u2192 <code>{tz}</code>"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /placeholder text <text>"
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
                0.0, 1.0, self.manager.reload_compositor, "Opacity",
            )
        return f"\u2753 Unknown: /placeholder {sub}"

    # -- Text: /overlay ----------------------------------------------------

    async def _txt_overlay(self, args: list, arg_str: str):
        if not args:
            return self._text_ov(), _kb_ov(self.cfg)

        sub = args[0].lower()
        reload_fn = (
            self.manager.reload_compositor if self.cfg.overlay.enabled else None
        )

        if sub == "off":
            self.cfg.overlay.enabled = False
            await self.manager.reload_compositor()
            return "\u2705 Overlay disabled"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /overlay text <text>"
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
                args, self.cfg.overlay, "x", reload_fn, "Overlay X",
            )
        if sub == "y":
            return await _set_int(
                args, self.cfg.overlay, "y", reload_fn, "Overlay Y",
            )
        if sub == "opacity":
            return await _set_float(
                args, self.cfg.overlay, "opacity",
                0.0, 1.0, reload_fn, "Overlay opacity",
            )
        if sub == "size":
            return await _set_int(
                args, self.cfg.overlay, "font_size", reload_fn, "Font size",
            )
        if sub == "color":
            if len(args) < 2:
                return "Usage: /overlay color <name or #RRGGBB>"
            self.cfg.overlay.font_color = args[1]
            if self.cfg.overlay.enabled:
                await self.manager.reload_compositor()
            return f"\u2705 Color \u2192 {args[1]}"

        return f"\u2753 Unknown: /overlay {sub}"

    # -- Text: /target -----------------------------------------------------

    async def _txt_target(self, args: list):
        if not args or args[0].lower() == "list":
            return self._text_targets(), _kb_targets(self.cfg)

        sub = args[0].lower()

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

        if sub == "set":
            if len(args) < 2:
                return "Usage: /target set <rtmp://...>"
            self.cfg.output.targets = [args[1]]
            await self.manager.reload_output()
            return f"\u2705 Target set: <code>{args[1]}</code>"

        return f"\u2753 Unknown: /target {sub}"

    # -- Text: /output -----------------------------------------------------

    async def _txt_output(self, args: list):
        if not args:
            return self._text_output(), _kb_out()

        sub = args[0].lower()

        if sub == "bitrate":
            if len(args) < 2:
                return "Usage: /output bitrate <value>"
            self.cfg.output.video.bitrate = args[1]
            await self.manager.reload_compositor()
            return f"\u2705 Bitrate \u2192 {args[1]}"

        if sub == "fps":
            if len(args) < 2:
                return "Usage: /output fps <n>"
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
                return "Usage: /output size WxH"
            try:
                w_s, h_s = args[1].lower().split("x")
                w, h = int(w_s), int(h_s)
                if not (160 <= w <= 7680 and 90 <= h <= 4320):
                    raise ValueError
                self.cfg.output.video.width = w
                self.cfg.output.video.height = h
                await self.manager.reload_compositor()
                return f"\u2705 Size \u2192 {w}\u00d7{h}"
            except (ValueError, TypeError):
                return "\u274c Format: WxH (e.g. 1920x1080)"

        if sub == "preset":
            if len(args) < 2 or args[1] not in _X264_PRESETS:
                return (
                    "Usage: /output preset "
                    f"<{' | '.join(sorted(_X264_PRESETS))}>"
                )
            self.cfg.output.video.preset = args[1]
            await self.manager.reload_compositor()
            return f"\u2705 Preset \u2192 {args[1]}"

        return f"\u2753 Unknown: /output {sub}"

    # ═══════════════════════════════════════════════════════════════════ #
    #  STATUS TEXT BUILDERS                                               #
    # ═══════════════════════════════════════════════════════════════════ #

    def _text_main_menu(self) -> str:
        stream = self.manager._current_stream
        if self.manager.is_paused:
            state = "\u23f8\ufe0f <b>PAUSED</b>"
        elif stream:
            state = (
                f"\U0001f7e2 <b>LIVE</b> \u2014 "
                f"<code>{stream.path}</code>"
            )
        else:
            state = "\u26ab <b>IDLE</b>"
        return f"{state}\n\n\U0001f3ae <b>immortal-stream</b>"

    def _text_status(self) -> str:
        stream = self.manager._current_stream
        ph = self.cfg.placeholder
        ov = self.cfg.overlay
        v = self.cfg.output.video

        if self.manager.is_paused:
            state = "\u23f8\ufe0f <b>PAUSED</b> \u2014 all processes stopped"
        elif stream:
            state = (
                f"\U0001f7e2 <b>LIVE</b> \u2014 "
                f"<code>{stream.path}</code>\n"
                f"  {stream.codec_video} "
                f"{stream.width}\u00d7{stream.height} "
                f"@{stream.fps}fps"
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
            ov_desc = (
                f"text <code>{ov.text}</code>"
                if ov.type == "text"
                else f"image <code>{ov.path}</code>"
            )
            ov_desc += f" at ({ov.x},{ov.y})"
            if ov.opacity < 1.0:
                ov_desc += f" opacity={ov.opacity:.2f}"
        else:
            ov_desc = "disabled"

        targets = (
            "\n".join(
                f"  \u2022 <code>{t}</code>"
                for t in self.cfg.output.targets
            )
            or "  (none)"
        )

        return (
            f"{state}\n\n"
            f"<b>Placeholder:</b> {ph_desc}\n"
            f"<b>Overlay:</b> {ov_desc}\n"
            f"<b>Output:</b> {v.width}\u00d7{v.height} "
            f"@{v.fps}fps {v.bitrate} preset={v.preset}\n"
            f"<b>Targets:</b>\n{targets}"
        )

    def _text_ph(self) -> str:
        ph = self.cfg.placeholder
        desc = f"<b>Placeholder:</b> {ph.type}"
        if ph.type == "text" and ph.text:
            desc += f"\nText: <code>{ph.text}</code>"
        elif ph.type == "testcard":
            desc += f"\nTimezone: <code>{ph.timezone}</code>"
        elif ph.path:
            desc += f"\nFile: <code>{ph.path}</code>"
        desc += f"\nOpacity: {ph.opacity:.2f}"
        return desc

    def _text_ov(self) -> str:
        ov = self.cfg.overlay
        status = "enabled" if ov.enabled else "disabled"
        desc = f"<b>Overlay:</b> {status}"
        if ov.enabled:
            if ov.type == "text":
                desc += f"\nType: text \u2014 <code>{ov.text}</code>"
            else:
                desc += f"\nType: image \u2014 <code>{ov.path}</code>"
            desc += f"\nPosition: ({ov.x}, {ov.y})"
            desc += f"\nOpacity: {ov.opacity:.2f}"
            if ov.type == "text":
                desc += f"\nFont: {ov.font_size}px {ov.font_color}"
        return desc

    def _text_targets(self) -> str:
        if not self.cfg.output.targets:
            return "<b>Targets:</b> none"
        lines = "\n".join(
            f"  {i+1}. <code>{t}</code>"
            for i, t in enumerate(self.cfg.output.targets)
        )
        return f"<b>Targets ({len(self.cfg.output.targets)}):</b>\n{lines}"

    def _text_output(self) -> str:
        v = self.cfg.output.video
        a = self.cfg.output.audio
        return (
            "<b>Output encoding:</b>\n"
            f"  Resolution: {v.width}\u00d7{v.height}\n"
            f"  FPS: {v.fps} (gop={v.gop})\n"
            f"  Bitrate: {v.bitrate}\n"
            f"  Preset: {v.preset}\n"
            f"  Audio: {a.bitrate} / {a.sample_rate}Hz"
        )

    def _text_power(self) -> str:
        if self.manager.is_paused:
            return (
                "\u23f8\ufe0f <b>Service is PAUSED</b>\n"
                "All processes stopped."
            )
        return (
            "\u25b6\ufe0f <b>Service is RUNNING</b>\n"
            "Compositor and output are active."
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Keyboard builders
# ═══════════════════════════════════════════════════════════════════════════

def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


_KB_MAIN = [
    [
        _btn("\U0001f4ca Status", "status:show"),
        _btn("\U0001f3a8 Placeholder", "ph:menu"),
    ],
    [
        _btn("\U0001f4cc Overlay", "ov:menu"),
        _btn("\U0001f4e1 Targets", "target:menu"),
    ],
    [
        _btn("\u2699\ufe0f Output", "out:menu"),
        _btn("\u26a1 Power", "power:menu"),
    ],
]


def _kb_status():
    return [
        [
            _btn("\U0001f504 Refresh", "status:show"),
            _btn("\u25c0\ufe0f Menu", "menu:main"),
        ],
    ]


def _kb_ph(cfg: Config):
    ph = cfg.placeholder
    check = lambda t: " \u2705" if ph.type == t else ""
    return [
        [
            _btn(f"\u2b1b Black{check('black')}", "ph:black"),
            _btn(f"\U0001f4dd Text{check('text')}", "ph:text"),
        ],
        [
            _btn(f"\U0001f5bc Image{check('image')}", "ph:image"),
            _btn(f"\U0001f3ac Video{check('video')}", "ph:video"),
        ],
        [
            _btn(f"\U0001f4fa Testcard{check('testcard')}", "ph:testcard"),
        ],
        [
            _btn(f"\U0001f4a7 Opacity ({ph.opacity:.1f})", "ph:opacity"),
            _btn(f"\U0001f30d TZ ({ph.timezone})", "ph:tz"),
        ],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_ov(cfg: Config):
    ov = cfg.overlay
    toggle = (
        _btn("\u274c Disable", "ov:off")
        if ov.enabled
        else _btn("\u2705 Enable", "ov:on")
    )
    return [
        [toggle],
        [
            _btn("\U0001f4dd Text", "ov:text"),
            _btn("\U0001f5bc Image", "ov:image"),
        ],
        [
            _btn(f"\U0001f4cd Position ({ov.x},{ov.y})", "ov:pos"),
            _btn(f"\U0001f4a7 Opacity ({ov.opacity:.1f})", "ov:opacity"),
        ],
        [
            _btn(f"\U0001f524 Size ({ov.font_size}px)", "ov:size"),
            _btn(f"\U0001f3a8 Color ({ov.font_color})", "ov:color"),
        ],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_targets(cfg: Config):
    rows = []
    for i, t in enumerate(cfg.output.targets):
        # Show truncated URL with remove button
        short = t.split("/")[-1][:25] if "/" in t else t[:25]
        rows.append([_btn(f"\u274c {i+1}. {short}...", f"target:rm:{i}")])
    rows.append([_btn("\u2795 Add target", "target:add")])
    rows.append([_btn("\u25c0\ufe0f Menu", "menu:main")])
    return rows


def _kb_out():
    return [
        [
            _btn("\U0001f4ca Bitrate", "out:bitrate"),
            _btn("\U0001f3ac FPS", "out:fps"),
        ],
        [
            _btn("\U0001f4d0 Size", "out:size"),
            _btn("\u2699\ufe0f Preset", "out:preset"),
        ],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_presets():
    presets = sorted(_X264_PRESETS)
    rows = []
    for i in range(0, len(presets), 3):
        rows.append([_btn(p, f"out:p_{p}") for p in presets[i:i+3]])
    rows.append([_btn("\u25c0\ufe0f Back", "out:menu")])
    return rows


def _kb_power(manager):
    if manager.is_paused:
        return [
            [_btn("\u25b6\ufe0f Start", "power:start")],
            [_btn("\u25c0\ufe0f Menu", "menu:main")],
        ]
    return [
        [_btn("\u23f9\ufe0f Stop all", "power:stop")],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

async def _set_int(
    args: list, obj, attr: str,
    reload_fn: Optional[Callable], label: str,
) -> str:
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
