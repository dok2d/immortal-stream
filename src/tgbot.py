"""
Telegram bot for runtime configuration of immortal-stream.

Menu-driven UI with inline keyboard buttons.  Text commands remain
supported for power users.  All interactions are restricted to the
configured chat_id.

Button navigation uses callback_query with data format "section:action".
When a button requires text input (e.g. adding a target URL), the bot
enters "awaiting" mode and treats the next text message as the value.

Media uploads (photos, videos, documents) are accepted in the
appropriate "awaiting" states — e.g. send a photo when setting the
placeholder image, or a video file when setting the placeholder video.
"""
import asyncio
import json
import logging
import os
import re
import urllib.request
from typing import Callable, Optional, TYPE_CHECKING

from config import Config, _X264_PRESETS, POSITION_PRESETS, save_config

if TYPE_CHECKING:
    from stream_manager import StreamManager

log = logging.getLogger("tgbot")

POLL_TIMEOUT = 30
MEDIA_DIR = "/media/opt"

# File-size limits for Telegram Bot API downloads (in bytes).
_MAX_PHOTO = 20 * 1024 * 1024      # 20 MB
_MAX_VIDEO = 50 * 1024 * 1024      # 50 MB (bot API file limit)


# ═══════════════════════════════════════════════════════════════════════════
#  Bot
# ═══════════════════════════════════════════════════════════════════════════

class TelegramBot:
    def __init__(
        self,
        cfg: Config,
        manager: "StreamManager",
        config_path: str = "",
    ):
        self.cfg = cfg
        self.manager = manager
        self._config_path = config_path
        self._base = f"https://api.telegram.org/bot{cfg.telegram.bot_token}"
        self._chat_id = cfg.telegram.chat_id
        self._running = False
        self._awaiting: Optional[str] = None  # e.g. "target:add"
        self._poll_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._poll_task.add_done_callback(self._on_poll_done)
        log.info("Telegram bot started (chat_id=%s)", self._chat_id)

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    @staticmethod
    def _on_poll_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("Bot poll loop crashed: %s", exc)

    # ------------------------------------------------------------------ #
    #  Config persistence helpers                                          #
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        """Persist bot-modifiable settings back to config.yaml."""
        if self._config_path:
            save_config(self.cfg, self._config_path)

    async def _save_state_async(self) -> None:
        """Async wrapper for _save_state (used as reload_fn callback)."""
        self._save_state()

    async def _reload_compositor(self) -> None:
        """Reload compositor and persist state."""
        await self.manager.reload_compositor()
        self._save_state()

    async def _reload_output(self) -> None:
        """Reload output FFmpeg and persist state."""
        await self.manager.reload_output()
        self._save_state()

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

    async def _send_prompt(self, text: str) -> dict:
        """Send a message with ForceReply markup."""
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

    async def _download_file(self, file_id: str, ext: str = "") -> str:
        """Download a Telegram file by file_id and return the local path."""
        resp = await self._api("getFile", {"file_id": file_id})
        file_path = resp.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError("Could not get file path from Telegram")

        dl_url = (
            f"https://api.telegram.org/file/bot"
            f"{self.cfg.telegram.bot_token}/{file_path}"
        )
        if not ext:
            ext = os.path.splitext(file_path)[1] or ".bin"
        local_path = os.path.join(MEDIA_DIR, f"tg_{file_id[:16]}{ext}")

        os.makedirs(MEDIA_DIR, exist_ok=True)

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
            try:
                await self._answer_cb(cb_id, toast or "")
            except Exception:
                log.debug("answerCallbackQuery expired")
        except Exception as e:
            log.exception("Callback error")
            try:
                await self._answer_cb(cb_id, f"Error: {e}")
            except Exception:
                log.debug("answerCallbackQuery expired (error path)")

    async def _route_callback(self, data: str):
        """Dispatch callback data → (text, keyboard, toast)."""
        p = data.split(":")
        section = p[0]
        handlers = {
            "menu":   self._cb_menu,
            "status": self._cb_status,
            "ph":     self._cb_placeholder,
            "phimg":    self._cb_ph_image,
            "phimgpos": self._cb_ph_img_pos,
            "phvid":    self._cb_ph_video,
            "phvidpos": self._cb_ph_vid_pos,
            "phtxt":    self._cb_ph_text,
            "phpos":    self._cb_ph_pos,
            "ov":       self._cb_overlay,
            "ovimg":    self._cb_ov_image,
            "ovimgpos": self._cb_ov_img_pos,
            "ovtxt":    self._cb_ov_text,
            "ovtxtpos": self._cb_ov_txt_pos,
            "target": self._cb_target,
            "out":    self._cb_output,
            "rec":    self._cb_recording,
            "power":  self._cb_power,
        }
        handler = handlers.get(section)
        if handler:
            return await handler(p)
        return None, None, "Unknown"

    # ------------------------------------------------------------------ #
    #  Message handler (text commands + awaited input + media uploads)      #
    # ------------------------------------------------------------------ #

    async def _on_message(self, msg: dict) -> None:
        if not msg or str(msg.get("chat", {}).get("id", "")) != self._chat_id:
            return

        text = (msg.get("text") or "").strip()

        # ── Media uploads when awaiting ──────────────────────────────────
        if self._awaiting:
            media_result = await self._try_handle_media(msg)
            if media_result:
                reply, kb = media_result
                await self._send(reply, kb)
                return

        # ── Awaited text input from a button flow ────────────────────────
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

    async def _try_handle_media(self, msg: dict):
        """Try to handle a media message in the current awaiting context.

        Returns (reply, keyboard) on success, or None if this message
        does not contain applicable media.
        """
        action = self._awaiting
        if not action:
            return None

        # Determine which media types are acceptable for this action
        accepts_image = action in ("ph:image", "ov:image")
        accepts_video = action in ("ph:video",)

        photo = msg.get("photo")
        video = msg.get("video")
        animation = msg.get("animation")   # GIF → treated as video
        document = msg.get("document")

        try:
            # ── Photo upload ─────────────────────────────────────────
            if photo and accepts_image:
                self._awaiting = None
                file_id = photo[-1]["file_id"]
                local = await self._download_file(file_id, ".jpg")
                return await self._handle_awaited(action, local)

            # ── Video / animation upload ─────────────────────────────
            if (video or animation) and accepts_video:
                self._awaiting = None
                media = video or animation
                file_id = media["file_id"]
                mime = media.get("mime_type", "")
                ext = _ext_from_mime(mime) or ".mp4"
                local = await self._download_file(file_id, ext)
                return await self._handle_awaited(action, local)

            # ── Document upload (image or video by mime) ─────────────
            if document:
                mime = (document.get("mime_type") or "").lower()
                file_id = document["file_id"]

                if mime.startswith("image/") and accepts_image:
                    self._awaiting = None
                    ext = _ext_from_mime(mime) or ".jpg"
                    local = await self._download_file(file_id, ext)
                    return await self._handle_awaited(action, local)

                if mime.startswith("video/") and accepts_video:
                    self._awaiting = None
                    ext = _ext_from_mime(mime) or ".mp4"
                    local = await self._download_file(file_id, ext)
                    return await self._handle_awaited(action, local)

        except Exception as e:
            self._awaiting = None
            return (
                f"\u274c Failed to process file: {e}",
                [[_btn("\u25c0\ufe0f Menu", "menu:main")]],
            )

        return None

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
            self.cfg.placeholder.background = "black"
            await self._reload_compositor()
            return (
                self._text_ph() + "\n\n\u2705 Background: black",
                _kb_ph(self.cfg), "Black",
            )

        if act == "testcard":
            self.cfg.placeholder.background = "testcard"
            await self._reload_compositor()
            return (
                self._text_ph() + "\n\n\u2705 Background: testcard",
                _kb_ph(self.cfg), "Testcard",
            )

        return self._text_ph(), _kb_ph(self.cfg), ""

    # -- Placeholder Image submenu -----------------------------------------

    async def _cb_ph_image(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_image(), _kb_ph_image(self.cfg), ""

        if act == "set":
            self._awaiting = "ph:image"
            await self._send_prompt(
                "\U0001f4f7 Send a photo, or a file path on the server:"
            )
            return None, None, ""

        if act == "clear":
            self.cfg.placeholder.image_path = None
            await self._reload_compositor()
            return (
                self._text_ph_image() + "\n\n\u2705 Image removed",
                _kb_ph_image(self.cfg), "Removed",
            )

        if act == "pos":
            return self._text_ph_img_pos(), _kb_position("phimgpos"), ""

        if act == "opacity":
            self._awaiting = "ph:imgopacity"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.image_opacity:.2f}\n"
                "Send new value (0.0\u20131.0):"
            )
            return None, None, ""

        if act == "maxh":
            self._awaiting = "ph:imgmaxh"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.image_max_height or 'full frame'}\n"
                "Send max height in pixels (0 = full frame):"
            )
            return None, None, ""

        return self._text_ph_image(), _kb_ph_image(self.cfg), ""

    async def _cb_ph_img_pos(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_img_pos(), _kb_position("phimgpos"), ""

        if act == "custom":
            self._awaiting = "ph:imgcustompos"
            await self._send_prompt(
                "Send coordinates as <code>x,y</code> (pixels):"
            )
            return None, None, ""

        if act in POSITION_PRESETS and act != "custom":
            self.cfg.placeholder.image_position = act
            await self._reload_compositor()
            return (
                self._text_ph_img_pos() + f"\n\n\u2705 {act}",
                _kb_position("phimgpos"), act,
            )

    # -- Placeholder Video submenu -----------------------------------------

    async def _cb_ph_video(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_video(), _kb_ph_video(self.cfg), ""

        if act == "set":
            self._awaiting = "ph:video"
            await self._send_prompt(
                "\U0001f3ac Send a video file, or a file path on the server:\n"
                "<i>Videos up to 50 MB can be uploaded directly.</i>"
            )
            return None, None, ""

        if act == "clear":
            self.cfg.placeholder.video_path = None
            await self._reload_compositor()
            return (
                self._text_ph_video() + "\n\n\u2705 Video removed",
                _kb_ph_video(self.cfg), "Removed",
            )

        if act == "pos":
            return self._text_ph_vid_pos(), _kb_position("phvidpos"), ""

        if act == "opacity":
            self._awaiting = "ph:vidopacity"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.video_opacity:.2f}\n"
                "Send new value (0.0\u20131.0):"
            )
            return None, None, ""

        if act == "maxh":
            self._awaiting = "ph:vidmaxh"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.video_max_height or 'full frame'}\n"
                "Send max height in pixels (0 = full frame):"
            )
            return None, None, ""

        return self._text_ph_video(), _kb_ph_video(self.cfg), ""

    async def _cb_ph_vid_pos(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_vid_pos(), _kb_position("phvidpos"), ""

        if act == "custom":
            self._awaiting = "ph:vidcustompos"
            await self._send_prompt(
                "Send coordinates as <code>x,y</code> (pixels):"
            )
            return None, None, ""

        if act in POSITION_PRESETS and act != "custom":
            self.cfg.placeholder.video_position = act
            await self._reload_compositor()
            return (
                self._text_ph_vid_pos() + f"\n\n\u2705 {act}",
                _kb_position("phvidpos"), act,
            )

    # -- Placeholder Text submenu ------------------------------------------

    async def _cb_ph_text(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_text(), _kb_ph_text(self.cfg), ""

        if act == "content":
            self._awaiting = "ph:text"
            await self._send_prompt(
                "\u270f\ufe0f Send text to overlay on the placeholder.\n"
                "This text is additive \u2014 shown on top of the "
                "base (black/testcard/image/video).\n"
                "Send <code>off</code> to remove text."
            )
            return None, None, ""

        if act == "off":
            self.cfg.placeholder.text = None
            await self._reload_compositor()
            return (
                self._text_ph_text() + "\n\n\u2705 Text removed",
                _kb_ph_text(self.cfg), "Removed",
            )

        if act == "size":
            self._awaiting = "ph:fontsize"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.font_size}px\n"
                "Send font size (8\u2013500):"
            )
            return None, None, ""

        if act == "color":
            self._awaiting = "ph:fontcolor"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.font_color}\n"
                "Send color name or #RRGGBB:"
            )
            return None, None, ""

        if act == "pos":
            return self._text_ph_pos(), _kb_position("phpos"), ""

        if act == "opacity":
            self._awaiting = "ph:textopacity"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.text_opacity:.2f}\n"
                "Send text opacity (0.0\u20131.0):"
            )
            return None, None, ""

        if act == "font":
            self._awaiting = "ph:font"
            await self._send_prompt(
                f"Current: {self.cfg.placeholder.font_path or 'default (JetBrains Mono)'}\n"
                "Send path to TTF/OTF font file, or <code>default</code>:"
            )
            return None, None, ""

        return self._text_ph_text(), _kb_ph_text(self.cfg), ""

    async def _cb_ph_pos(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ph_pos(), _kb_position("phpos"), ""

        if act == "custom":
            self._awaiting = "ph:custompos"
            await self._send_prompt(
                "Send coordinates as <code>x,y</code> (pixels):"
            )
            return None, None, ""

        # Position preset
        if act in POSITION_PRESETS and act != "custom":
            self.cfg.placeholder.text_position = act
            await self._reload_compositor()
            return (
                self._text_ph_pos() + f"\n\n\u2705 {act}",
                _kb_position("phpos"), act,
            )

    # -- Overlay -----------------------------------------------------------

    async def _cb_overlay(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov(), _kb_ov(self.cfg), ""

        if act == "off":
            self.cfg.overlay.enabled = False
            await self._reload_compositor()
            return self._text_ov() + "\n\n\u2705 Disabled", _kb_ov(self.cfg), "Off"

        if act == "on":
            self.cfg.overlay.enabled = True
            await self._reload_compositor()
            return self._text_ov() + "\n\n\u2705 Enabled", _kb_ov(self.cfg), "On"

        return self._text_ov(), _kb_ov(self.cfg), ""

    # -- Overlay Image submenu -----------------------------------------------

    async def _cb_ov_image(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov_image(), _kb_ov_image(self.cfg), ""

        if act == "set":
            self._awaiting = "ov:image"
            await self._send_prompt(
                "\U0001f4f7 Send a photo (PNG recommended), "
                "or a file path on the server:"
            )
            return None, None, ""

        if act == "clear":
            self.cfg.overlay.path = None
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return (
                self._text_ov_image() + "\n\n\u2705 Image cleared",
                _kb_ov_image(self.cfg), "Cleared",
            )

        if act == "pos":
            return self._text_ov_img_pos(), _kb_position("ovimgpos"), ""

        if act == "opacity":
            self._awaiting = "ov:imgopacity"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.image_opacity:.2f}\n"
                "Send value (0.0\u20131.0):"
            )
            return None, None, ""

        if act == "maxh":
            self._awaiting = "ov:imgmaxh"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.image_max_height or 'original'}\n"
                "Send max height in pixels (0 = original size):"
            )
            return None, None, ""

        return self._text_ov_image(), _kb_ov_image(self.cfg), ""

    async def _cb_ov_img_pos(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov_img_pos(), _kb_position("ovimgpos"), ""

        if act == "custom":
            self._awaiting = "ov:imgcustompos"
            await self._send_prompt(
                "Send coordinates as <code>x,y</code> (pixels):"
            )
            return None, None, ""

        if act in POSITION_PRESETS and act != "custom":
            self.cfg.overlay.image_position = act
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return (
                self._text_ov_img_pos() + f"\n\n\u2705 {act}",
                _kb_position("ovimgpos"), act,
            )

    # -- Overlay Text submenu -----------------------------------------------

    async def _cb_ov_text(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov_text(), _kb_ov_text(self.cfg), ""

        if act == "content":
            self._awaiting = "ov:text"
            await self._send_prompt("\u270f\ufe0f Send overlay text:")
            return None, None, ""

        if act == "clear":
            self.cfg.overlay.text = None
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return (
                self._text_ov_text() + "\n\n\u2705 Text cleared",
                _kb_ov_text(self.cfg), "Cleared",
            )

        if act == "size":
            self._awaiting = "ov:size"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.font_size}px\n"
                "Send font size (8\u2013500):"
            )
            return None, None, ""

        if act == "color":
            self._awaiting = "ov:color"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.font_color}\n"
                "Send color name or #RRGGBB:"
            )
            return None, None, ""

        if act == "pos":
            return self._text_ov_txt_pos(), _kb_position("ovtxtpos"), ""

        if act == "opacity":
            self._awaiting = "ov:textopacity"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.text_opacity:.2f}\n"
                "Send text opacity (0.0\u20131.0):"
            )
            return None, None, ""

        if act == "font":
            self._awaiting = "ov:font"
            await self._send_prompt(
                f"Current: {self.cfg.overlay.font_path or 'default (JetBrains Mono)'}\n"
                "Send path to TTF/OTF font file, or <code>default</code>:"
            )
            return None, None, ""

        return self._text_ov_text(), _kb_ov_text(self.cfg), ""

    async def _cb_ov_txt_pos(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_ov_txt_pos(), _kb_position("ovtxtpos"), ""

        if act == "custom":
            self._awaiting = "ov:txtcustompos"
            await self._send_prompt(
                "Send coordinates as <code>x,y</code> (pixels):"
            )
            return None, None, ""

        if act in POSITION_PRESETS and act != "custom":
            self.cfg.overlay.text_position = act
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return (
                self._text_ov_txt_pos() + f"\n\n\u2705 {act}",
                _kb_position("ovtxtpos"), act,
            )

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
                    await self._reload_output()
                else:
                    self._save_state()
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
            return self._text_output(), _kb_out(self.cfg), ""

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
            return self._text_output(), _kb_presets(self.cfg), ""

        # Handle preset selection: "p_ultrafast"
        if act.startswith("p_"):
            preset = act[2:]
            if preset in _X264_PRESETS:
                self.cfg.output.video.preset = preset
                await self._reload_compositor()
                return (
                    self._text_output() + f"\n\n\u2705 Preset \u2192 {preset}",
                    _kb_out(self.cfg), preset,
                )

        return self._text_output(), _kb_out(self.cfg), ""

    # -- Recording ---------------------------------------------------------

    async def _cb_recording(self, p):
        act = p[1] if len(p) > 1 else "menu"

        if act == "menu":
            return self._text_recording(), _kb_recording(self.cfg), ""

        if act == "toggle":
            new_state = not self.cfg.recording.enabled
            await self.manager.set_recording_enabled(new_state)
            self._save_state()
            label = "ON" if new_state else "OFF"
            return (
                self._text_recording(),
                _kb_recording(self.cfg),
                f"Recording {label}",
            )

        return self._text_recording(), _kb_recording(self.cfg), ""

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
            val = text.strip().strip("\"'")
            if val.lower() == "off":
                self.cfg.placeholder.text = None
                await self._reload_compositor()
                return "\u2705 Placeholder text removed", _kb_ph_text(self.cfg)
            self.cfg.placeholder.text = val
            await self._reload_compositor()
            return (
                f"\u2705 Placeholder text:\n<code>{val}</code>",
                _kb_ph_text(self.cfg),
            )

        if action == "ph:image":
            if not os.path.isfile(text):
                return f"\u274c File not found: <code>{text}</code>", _kb_ph_image(self.cfg)
            self.cfg.placeholder.image_path = text
            await self._reload_compositor()
            return (
                f"\u2705 Placeholder image:\n\U0001f5bc <code>{text}</code>",
                _kb_ph_image(self.cfg),
            )

        if action == "ph:video":
            if not os.path.isfile(text):
                return f"\u274c File not found: <code>{text}</code>", _kb_ph_video(self.cfg)
            self.cfg.placeholder.video_path = text
            await self._reload_compositor()
            return (
                f"\u2705 Placeholder video:\n\U0001f3ac <code>{text}</code>",
                _kb_ph_video(self.cfg),
            )

        if action == "ph:imgopacity":
            try:
                v = float(text)
            except (ValueError, TypeError):
                return "\u274c Must be a number 0.0\u20131.0", _kb_ph_image(self.cfg)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ph_image(self.cfg)
            self.cfg.placeholder.image_opacity = v
            await self._reload_compositor()
            return f"\u2705 Image opacity: {v:.2f}", _kb_ph_image(self.cfg)

        if action == "ph:imgmaxh":
            try:
                v = int(text)
            except (ValueError, TypeError):
                return "\u274c Must be an integer", _kb_ph_image(self.cfg)
            if v < 0:
                return "\u274c Must be >= 0 (0 = full frame)", _kb_ph_image(self.cfg)
            self.cfg.placeholder.image_max_height = v
            await self._reload_compositor()
            label = f"{v}px" if v > 0 else "full frame"
            return f"\u2705 Max height: {label}", _kb_ph_image(self.cfg)

        if action == "ph:imgcustompos":
            parts = text.replace(" ", "").split(",")
            if len(parts) != 2:
                return "\u274c Format: <code>x,y</code>", _kb_position("phimgpos")
            try:
                x, y = int(parts[0]), int(parts[1])
            except (ValueError, TypeError):
                return "\u274c Coordinates must be integers", _kb_position("phimgpos")
            self.cfg.placeholder.image_position = "custom"
            self.cfg.placeholder.image_x = x
            self.cfg.placeholder.image_y = y
            await self._reload_compositor()
            return f"\u2705 Position: ({x},{y})", _kb_position("phimgpos")

        if action == "ph:vidopacity":
            try:
                v = float(text)
            except (ValueError, TypeError):
                return "\u274c Must be a number 0.0\u20131.0", _kb_ph_video(self.cfg)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ph_video(self.cfg)
            self.cfg.placeholder.video_opacity = v
            await self._reload_compositor()
            return f"\u2705 Video opacity: {v:.2f}", _kb_ph_video(self.cfg)

        if action == "ph:vidmaxh":
            try:
                v = int(text)
            except (ValueError, TypeError):
                return "\u274c Must be an integer", _kb_ph_video(self.cfg)
            if v < 0:
                return "\u274c Must be >= 0 (0 = full frame)", _kb_ph_video(self.cfg)
            self.cfg.placeholder.video_max_height = v
            await self._reload_compositor()
            label = f"{v}px" if v > 0 else "full frame"
            return f"\u2705 Max height: {label}", _kb_ph_video(self.cfg)

        if action == "ph:vidcustompos":
            parts = text.replace(" ", "").split(",")
            if len(parts) != 2:
                return "\u274c Format: <code>x,y</code>", _kb_position("phvidpos")
            try:
                x, y = int(parts[0]), int(parts[1])
            except (ValueError, TypeError):
                return "\u274c Coordinates must be integers", _kb_position("phvidpos")
            self.cfg.placeholder.video_position = "custom"
            self.cfg.placeholder.video_x = x
            self.cfg.placeholder.video_y = y
            await self._reload_compositor()
            return f"\u2705 Position: ({x},{y})", _kb_position("phvidpos")

        if action == "ph:fontsize":
            try:
                v = int(text)
            except (ValueError, TypeError):
                return "\u274c Must be an integer", _kb_ph_text(self.cfg)
            if not 8 <= v <= 500:
                return "\u274c Font size must be 8\u2013500", _kb_ph_text(self.cfg)
            self.cfg.placeholder.font_size = v
            await self._reload_compositor()
            return f"\u2705 Font size: {v}px", _kb_ph_text(self.cfg)

        if action == "ph:fontcolor":
            self.cfg.placeholder.font_color = text.strip()
            await self._reload_compositor()
            return f"\u2705 Color: {text.strip()}", _kb_ph_text(self.cfg)

        if action == "ph:textopacity":
            try:
                v = float(text)
            except (ValueError, TypeError):
                return "\u274c Must be a number 0.0\u20131.0", _kb_ph_text(self.cfg)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ph_text(self.cfg)
            self.cfg.placeholder.text_opacity = v
            await self._reload_compositor()
            return f"\u2705 Text opacity: {v:.2f}", _kb_ph_text(self.cfg)

        if action == "ph:font":
            val = text.strip()
            if val.lower() == "default":
                self.cfg.placeholder.font_path = None
            elif os.path.isfile(val):
                self.cfg.placeholder.font_path = val
            else:
                return f"\u274c File not found: <code>{val}</code>", _kb_ph_text(self.cfg)
            await self._reload_compositor()
            return f"\u2705 Font: {val}", _kb_ph_text(self.cfg)

        if action == "ph:custompos":
            parts = text.replace(" ", "").split(",")
            if len(parts) != 2:
                return "\u274c Format: <code>x,y</code>", _kb_position("phpos")
            try:
                x, y = int(parts[0]), int(parts[1])
            except (ValueError, TypeError):
                return "\u274c Coordinates must be integers", _kb_position("phpos")
            self.cfg.placeholder.text_position = "custom"
            self.cfg.placeholder.text_x = x
            self.cfg.placeholder.text_y = y
            await self._reload_compositor()
            return f"\u2705 Position: ({x},{y})", _kb_position("phpos")

        # -- Overlay Image --
        if action == "ov:image":
            if not os.path.isfile(text):
                return f"\u274c File not found: <code>{text}</code>", _kb_ov_image(self.cfg)
            self.cfg.overlay.enabled = True
            self.cfg.overlay.path = text
            await self._reload_compositor()
            return f"\u2705 Overlay image:\n<code>{text}</code>", _kb_ov_image(self.cfg)

        if action == "ov:imgcustompos":
            parts = text.replace(" ", "").split(",")
            if len(parts) != 2:
                return "\u274c Format: <code>x,y</code>", _kb_position("ovimgpos")
            try:
                x, y = int(parts[0]), int(parts[1])
            except (ValueError, TypeError):
                return "\u274c Coordinates must be integers", _kb_position("ovimgpos")
            self.cfg.overlay.image_position = "custom"
            self.cfg.overlay.image_x = x
            self.cfg.overlay.image_y = y
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Position: ({x},{y})", _kb_position("ovimgpos")

        if action == "ov:imgopacity":
            try:
                v = float(text)
            except (ValueError, TypeError):
                return "\u274c Must be a number 0.0\u20131.0", _kb_ov_image(self.cfg)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ov_image(self.cfg)
            self.cfg.overlay.image_opacity = v
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Image opacity: {v:.2f}", _kb_ov_image(self.cfg)

        if action == "ov:imgmaxh":
            try:
                v = int(text)
            except (ValueError, TypeError):
                return "\u274c Must be an integer", _kb_ov_image(self.cfg)
            if v < 0:
                return "\u274c Must be >= 0 (0 = original)", _kb_ov_image(self.cfg)
            self.cfg.overlay.image_max_height = v
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            label = f"{v}px" if v > 0 else "original"
            return f"\u2705 Max height: {label}", _kb_ov_image(self.cfg)

        # -- Overlay Text --
        if action == "ov:text":
            self.cfg.overlay.enabled = True
            self.cfg.overlay.text = text.strip("\"'")
            await self._reload_compositor()
            return f"\u2705 Overlay text:\n<code>{text}</code>", _kb_ov_text(self.cfg)

        if action == "ov:txtcustompos":
            parts = text.replace(" ", "").split(",")
            if len(parts) != 2:
                return "\u274c Format: <code>x,y</code>", _kb_position("ovtxtpos")
            try:
                x, y = int(parts[0]), int(parts[1])
            except (ValueError, TypeError):
                return "\u274c Coordinates must be integers", _kb_position("ovtxtpos")
            self.cfg.overlay.text_position = "custom"
            self.cfg.overlay.text_x = x
            self.cfg.overlay.text_y = y
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Position: ({x},{y})", _kb_position("ovtxtpos")

        if action == "ov:textopacity":
            try:
                v = float(text)
            except (ValueError, TypeError):
                return "\u274c Must be a number 0.0\u20131.0", _kb_ov_text(self.cfg)
            if not 0.0 <= v <= 1.0:
                return "\u274c Must be 0.0\u20131.0", _kb_ov_text(self.cfg)
            self.cfg.overlay.text_opacity = v
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Text opacity: {v:.2f}", _kb_ov_text(self.cfg)

        if action == "ov:size":
            try:
                v = int(text)
            except (ValueError, TypeError):
                return "\u274c Must be an integer", _kb_ov_text(self.cfg)
            if not 8 <= v <= 500:
                return "\u274c Font size must be 8\u2013500", _kb_ov_text(self.cfg)
            self.cfg.overlay.font_size = v
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Font size: {v}px", _kb_ov_text(self.cfg)

        if action == "ov:color":
            self.cfg.overlay.font_color = text.strip()
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Color: {text.strip()}", _kb_ov_text(self.cfg)

        if action == "ov:font":
            val = text.strip()
            if val.lower() == "default":
                self.cfg.overlay.font_path = None
            elif os.path.isfile(val):
                self.cfg.overlay.font_path = val
            else:
                return f"\u274c File not found: <code>{val}</code>", _kb_ov_text(self.cfg)
            if self.cfg.overlay.enabled:
                await self._reload_compositor()
            else:
                self._save_state()
            return f"\u2705 Font: {val}", _kb_ov_text(self.cfg)

        # -- Targets --
        if action == "target:add":
            url = text.strip()
            if not _is_valid_rtmp_url(url):
                return (
                    "\u274c Invalid URL. Must start with "
                    "<code>rtmp://</code> or <code>rtmps://</code>",
                    _kb_targets(self.cfg),
                )
            if url in self.cfg.output.targets:
                return f"Already present:\n<code>{url}</code>", _kb_targets(self.cfg)
            self.cfg.output.targets.append(url)
            await self._reload_output()
            return f"\u2705 Added:\n<code>{url}</code>", _kb_targets(self.cfg)

        # -- Output encoding --
        if action == "out:bitrate":
            normalized = _normalize_bitrate(text)
            if not normalized:
                return (
                    "\u274c Invalid bitrate.\n"
                    "Examples: <code>6000</code>, <code>6000k</code>, <code>8m</code>\n"
                    f"Range: {_MIN_BITRATE_KBPS}k\u2013{_MAX_BITRATE_KBPS}k",
                    _kb_out(self.cfg),
                )
            self.cfg.output.video.bitrate = normalized
            await self._reload_compositor()
            return f"\u2705 Bitrate: {normalized}", _kb_out(self.cfg)

        if action == "out:fps":
            try:
                fps = int(text.strip())
            except (ValueError, TypeError):
                return f"\u274c FPS must be an integer ({_MIN_FPS}\u2013{_MAX_FPS})", _kb_out(self.cfg)
            if not _MIN_FPS <= fps <= _MAX_FPS:
                return f"\u274c FPS must be {_MIN_FPS}\u2013{_MAX_FPS}", _kb_out(self.cfg)
            self.cfg.output.video.fps = fps
            self.cfg.output.video.gop = fps * 2
            await self._reload_compositor()
            return f"\u2705 FPS: {fps} (gop={fps * 2})", _kb_out(self.cfg)

        if action == "out:size":
            try:
                w_s, h_s = text.strip().lower().split("x")
                w, h = int(w_s.strip()), int(h_s.strip())
            except (ValueError, TypeError):
                return "\u274c Format: <code>WxH</code> (e.g. 1920x1080)", _kb_out(self.cfg)
            if not (_MIN_WIDTH <= w <= _MAX_WIDTH and _MIN_HEIGHT <= h <= _MAX_HEIGHT):
                return (
                    f"\u274c Size out of range "
                    f"({_MIN_WIDTH}\u2013{_MAX_WIDTH} x {_MIN_HEIGHT}\u2013{_MAX_HEIGHT})",
                    _kb_out(self.cfg),
                )
            self.cfg.output.video.width = w
            self.cfg.output.video.height = h
            await self._reload_compositor()
            return f"\u2705 Size: {w}\u00d7{h}", _kb_out(self.cfg)

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

        if sub in ("black", "testcard"):
            self.cfg.placeholder.background = sub
            await self._reload_compositor()
            return f"\u2705 Background \u2192 {sub}"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /placeholder text <text>  (or 'off' to remove)"
            if text.lower() == "off":
                self.cfg.placeholder.text = None
                await self._reload_compositor()
                return "\u2705 Placeholder text removed"
            self.cfg.placeholder.text = text
            await self._reload_compositor()
            return f"\u2705 Placeholder text: <code>{text}</code>"

        if sub == "image":
            path = arg_str[len("image"):].strip()
            if not path:
                return "Usage: /placeholder image <path>  (or 'off' to remove)"
            if path.lower() == "off":
                self.cfg.placeholder.image_path = None
                await self._reload_compositor()
                return "\u2705 Placeholder image removed"
            if not os.path.isfile(path):
                return f"\u274c File not found: <code>{path}</code>"
            self.cfg.placeholder.image_path = path
            await self._reload_compositor()
            return f"\u2705 Placeholder image: <code>{path}</code>"

        if sub == "video":
            path = arg_str[len("video"):].strip()
            if not path:
                return "Usage: /placeholder video <path>  (or 'off' to remove)"
            if path.lower() == "off":
                self.cfg.placeholder.video_path = None
                await self._reload_compositor()
                return "\u2705 Placeholder video removed"
            if not os.path.isfile(path):
                return f"\u274c File not found: <code>{path}</code>"
            self.cfg.placeholder.video_path = path
            await self._reload_compositor()
            return f"\u2705 Placeholder video: <code>{path}</code>"

        if sub == "opacity":
            return await _set_float(
                args, self.cfg.placeholder, "image_opacity",
                0.0, 1.0, self._reload_compositor, "Image opacity",
            )

        if sub in ("pos", "position"):
            val = arg_str[len(sub):].strip()
            if not val:
                presets = ", ".join(
                    p for p in POSITION_PRESETS if p != "custom"
                )
                return (
                    "Usage: /placeholder pos <preset>\n"
                    f"Presets: {presets}\n"
                    "Or: /placeholder pos custom <x>,<y>"
                )
            if val in POSITION_PRESETS and val != "custom":
                self.cfg.placeholder.text_position = val
                await self._reload_compositor()
                return f"\u2705 Text position: {val}"
            if val == "custom" or "," in val:
                coords = val.replace("custom", "").strip().strip(",").strip()
                if not coords:
                    return "Usage: /placeholder pos custom <x>,<y>"
                parts = coords.replace(" ", "").split(",")
                if len(parts) != 2:
                    return "\u274c Format: <code>x,y</code>"
                try:
                    x, y = int(parts[0]), int(parts[1])
                except ValueError:
                    return "\u274c Coordinates must be integers"
                self.cfg.placeholder.text_position = "custom"
                self.cfg.placeholder.text_x = x
                self.cfg.placeholder.text_y = y
                await self._reload_compositor()
                return f"\u2705 Text position: custom ({x},{y})"
            return f"\u274c Unknown position: <code>{val}</code>"

        return f"\u2753 Unknown: /placeholder {sub}"

    # -- Text: /overlay ----------------------------------------------------

    async def _txt_overlay(self, args: list, arg_str: str):
        if not args:
            return self._text_ov(), _kb_ov(self.cfg)

        sub = args[0].lower()
        reload_fn = (
            self._reload_compositor if self.cfg.overlay.enabled
            else self._save_state_async
        )

        if sub == "off":
            self.cfg.overlay.enabled = False
            await self._reload_compositor()
            return "\u2705 Overlay disabled"

        if sub == "on":
            self.cfg.overlay.enabled = True
            await self._reload_compositor()
            return "\u2705 Overlay enabled"

        if sub == "text":
            text = arg_str[len("text"):].strip().strip("\"'")
            if not text:
                return "Usage: /overlay text <text>"
            if text.lower() == "off":
                self.cfg.overlay.text = None
                if self.cfg.overlay.enabled:
                    await self._reload_compositor()
                else:
                    self._save_state()
                return "\u2705 Overlay text removed"
            self.cfg.overlay.enabled = True
            self.cfg.overlay.text = text
            await self._reload_compositor()
            return f"\u2705 Overlay text: <code>{text}</code>"

        if sub == "image":
            path = arg_str[len("image"):].strip()
            if not path:
                return "Usage: /overlay image <path>"
            if path.lower() == "off":
                self.cfg.overlay.path = None
                if self.cfg.overlay.enabled:
                    await self._reload_compositor()
                else:
                    self._save_state()
                return "\u2705 Overlay image removed"
            if not os.path.isfile(path):
                return f"\u274c File not found: <code>{path}</code>"
            self.cfg.overlay.enabled = True
            self.cfg.overlay.path = path
            await self._reload_compositor()
            return f"\u2705 Overlay image: <code>{path}</code>"

        if sub == "maxheight":
            return await _set_int(
                args, self.cfg.overlay, "image_max_height",
                reload_fn, "Image max height",
            )

        if sub == "opacity":
            return await _set_float(
                args, self.cfg.overlay, "image_opacity",
                0.0, 1.0, reload_fn, "Image opacity",
            )
        if sub == "textopacity":
            return await _set_float(
                args, self.cfg.overlay, "text_opacity",
                0.0, 1.0, reload_fn, "Text opacity",
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
                await self._reload_compositor()
            else:
                self._save_state()
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
            if not _is_valid_rtmp_url(url):
                return "\u274c URL must start with rtmp:// or rtmps://"
            if url in self.cfg.output.targets:
                return f"Already present: <code>{url}</code>"
            self.cfg.output.targets.append(url)
            await self._reload_output()
            return f"\u2705 Added: <code>{url}</code>"

        if sub == "remove":
            if len(args) < 2:
                return "Usage: /target remove <rtmp://...>"
            url = args[1]
            if url not in self.cfg.output.targets:
                return f"Not in list: <code>{url}</code>"
            self.cfg.output.targets.remove(url)
            if self.cfg.output.targets:
                await self._reload_output()
            else:
                self._save_state()
            return f"\u2705 Removed: <code>{url}</code>"

        if sub == "set":
            if len(args) < 2:
                return "Usage: /target set <rtmp://...>"
            if not _is_valid_rtmp_url(args[1]):
                return "\u274c URL must start with rtmp:// or rtmps://"
            self.cfg.output.targets = [args[1]]
            await self._reload_output()
            return f"\u2705 Target set: <code>{args[1]}</code>"

        return f"\u2753 Unknown: /target {sub}"

    # -- Text: /output -----------------------------------------------------

    async def _txt_output(self, args: list):
        if not args:
            return self._text_output(), _kb_out(self.cfg)

        sub = args[0].lower()

        if sub == "bitrate":
            if len(args) < 2:
                return "Usage: /output bitrate <value>"
            normalized = _normalize_bitrate(" ".join(args[1:]))
            if not normalized:
                return (
                    "\u274c Invalid bitrate.\n"
                    f"Examples: 6000, 6000k, 8m\n"
                    f"Range: {_MIN_BITRATE_KBPS}k\u2013{_MAX_BITRATE_KBPS}k"
                )
            self.cfg.output.video.bitrate = normalized
            await self._reload_compositor()
            return f"\u2705 Bitrate \u2192 {normalized}"

        if sub == "fps":
            if len(args) < 2:
                return "Usage: /output fps <n>"
            try:
                fps = int(args[1])
                if not _MIN_FPS <= fps <= _MAX_FPS:
                    raise ValueError
                self.cfg.output.video.fps = fps
                self.cfg.output.video.gop = fps * 2
                await self._reload_compositor()
                return f"\u2705 FPS \u2192 {fps} (gop={fps * 2})"
            except ValueError:
                return f"\u274c FPS must be {_MIN_FPS}\u2013{_MAX_FPS}"

        if sub == "size":
            if len(args) < 2:
                return "Usage: /output size WxH"
            try:
                w_s, h_s = args[1].lower().split("x")
                w, h = int(w_s), int(h_s)
                if not (_MIN_WIDTH <= w <= _MAX_WIDTH
                        and _MIN_HEIGHT <= h <= _MAX_HEIGHT):
                    raise ValueError
                self.cfg.output.video.width = w
                self.cfg.output.video.height = h
                await self._reload_compositor()
                return f"\u2705 Size \u2192 {w}\u00d7{h}"
            except (ValueError, TypeError):
                return (
                    "\u274c Format: WxH (e.g. 1920x1080)\n"
                    f"Range: {_MIN_WIDTH}\u2013{_MAX_WIDTH} x "
                    f"{_MIN_HEIGHT}\u2013{_MAX_HEIGHT}"
                )

        if sub == "preset":
            if len(args) < 2 or args[1] not in _X264_PRESETS:
                return (
                    "Usage: /output preset "
                    f"<{' | '.join(sorted(_X264_PRESETS))}>"
                )
            self.cfg.output.video.preset = args[1]
            await self._reload_compositor()
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

        ph_desc = f"bg={ph.background}"
        if ph.image_path:
            ph_desc += f"\n  image: <code>{os.path.basename(ph.image_path)}</code>"
            if ph.image_opacity < 1.0:
                ph_desc += f" opacity={ph.image_opacity:.2f}"
        if ph.video_path:
            ph_desc += f"\n  video: <code>{os.path.basename(ph.video_path)}</code>"
        if ph.text:
            ph_desc += f"\n  text: <code>{ph.text}</code>"
            pos_str = ph.text_position
            if pos_str == "custom":
                pos_str += f" ({ph.text_x},{ph.text_y})"
            ph_desc += f" [{pos_str}]"
            if ph.text_opacity < 1.0:
                ph_desc += f" opacity={ph.text_opacity:.2f}"

        if ov.enabled:
            parts = []
            if ov.path:
                img_desc = f"image <code>{os.path.basename(ov.path)}</code>"
                if ov.image_max_height > 0:
                    img_desc += f" (max {ov.image_max_height}px)"
                img_desc += f" [{ov.image_position}]"
                if ov.image_opacity < 1.0:
                    img_desc += f" opacity={ov.image_opacity:.2f}"
                parts.append(img_desc)
            if ov.text:
                txt_desc = f"text <code>{ov.text}</code>"
                txt_desc += f" [{ov.text_position}]"
                if ov.text_opacity < 1.0:
                    txt_desc += f" opacity={ov.text_opacity:.2f}"
                parts.append(txt_desc)
            ov_desc = " + ".join(parts) if parts else "enabled (no layers)"
        else:
            ov_desc = "disabled"

        targets = (
            "\n".join(
                f"  \u2022 <code>{_short_url(t)}</code>"
                for t in self.cfg.output.targets
            )
            or "  (none)"
        )

        # CPU load hints (compare input vs output)
        hints = _cpu_hints(self.cfg, stream)
        hints_block = ""
        if hints:
            hints_block = (
                "\n\n\U0001f525 <b>CPU load:</b>\n"
                + "\n".join(f"  \u2022 {h}" for h in hints)
            )

        return (
            f"{state}\n\n"
            f"<b>Placeholder:</b> {ph_desc}\n"
            f"<b>Overlay:</b> {ov_desc}\n"
            f"<b>Output:</b> {v.width}\u00d7{v.height} "
            f"@{v.fps}fps {v.bitrate} preset={v.preset}\n"
            f"<b>Targets:</b>\n{targets}"
            f"{hints_block}"
        )

    def _text_ph(self) -> str:
        ph = self.cfg.placeholder
        desc = f"<b>Placeholder</b>\nBackground: {ph.background}"
        if ph.image_path:
            desc += f"\n\U0001f5bc Image: <code>{os.path.basename(ph.image_path)}</code>"
            desc += f" (opacity: {ph.image_opacity:.2f})"
        if ph.video_path:
            desc += f"\n\U0001f3ac Video: <code>{os.path.basename(ph.video_path)}</code>"
        if ph.text:
            desc += f"\n\U0001f4dd Text: <code>{ph.text}</code>"
        return desc

    def _text_ph_image(self) -> str:
        ph = self.cfg.placeholder
        desc = "\U0001f5bc <b>Placeholder Image</b>\n"
        if ph.image_path:
            desc += f"File: <code>{os.path.basename(ph.image_path)}</code>\n"
            desc += f"Position: {ph.image_position}"
            if ph.image_position == "custom":
                desc += f" ({ph.image_x},{ph.image_y})"
            desc += f"\nOpacity: {ph.image_opacity:.2f}"
            mh = ph.image_max_height
            desc += f"\nMax height: {f'{mh}px' if mh > 0 else 'full frame'}"
        else:
            desc += "No image configured"
        return desc

    def _text_ph_img_pos(self) -> str:
        ph = self.cfg.placeholder
        desc = f"\U0001f4cd <b>Image Position</b>\nCurrent: {ph.image_position}"
        if ph.image_position == "custom":
            desc += f" ({ph.image_x},{ph.image_y})"
        return desc

    def _text_ph_video(self) -> str:
        ph = self.cfg.placeholder
        desc = "\U0001f3ac <b>Placeholder Video</b>\n"
        if ph.video_path:
            desc += f"File: <code>{os.path.basename(ph.video_path)}</code>\n"
            desc += f"Position: {ph.video_position}"
            if ph.video_position == "custom":
                desc += f" ({ph.video_x},{ph.video_y})"
            desc += f"\nOpacity: {ph.video_opacity:.2f}"
            mh = ph.video_max_height
            desc += f"\nMax height: {f'{mh}px' if mh > 0 else 'full frame'}"
        else:
            desc += "No video configured"
        return desc

    def _text_ph_vid_pos(self) -> str:
        ph = self.cfg.placeholder
        desc = f"\U0001f4cd <b>Video Position</b>\nCurrent: {ph.video_position}"
        if ph.video_position == "custom":
            desc += f" ({ph.video_x},{ph.video_y})"
        return desc

    def _text_ph_text(self) -> str:
        ph = self.cfg.placeholder
        if ph.text:
            desc = f"\U0001f4dd <b>Placeholder Text</b>\n"
            desc += f"Text: <code>{ph.text}</code>\n"
            desc += f"Size: {ph.font_size}px | Color: {ph.font_color}\n"
            desc += f"Position: {ph.text_position}"
            if ph.text_position == "custom":
                desc += f" ({ph.text_x},{ph.text_y})"
            desc += f"\nOpacity: {ph.text_opacity:.2f}"
            if ph.font_path:
                desc += f"\nFont: <code>{os.path.basename(ph.font_path)}</code>"
        else:
            desc = "\U0001f4dd <b>Placeholder Text</b>\nNo text configured"
        return desc

    def _text_ph_pos(self) -> str:
        ph = self.cfg.placeholder
        desc = f"\U0001f4cd <b>Text Position</b>\nCurrent: {ph.text_position}"
        if ph.text_position == "custom":
            desc += f" ({ph.text_x},{ph.text_y})"
        return desc

    def _text_ov(self) -> str:
        ov = self.cfg.overlay
        status = "enabled" if ov.enabled else "disabled"
        desc = f"<b>Overlay:</b> {status}"
        if ov.path:
            desc += f"\n\U0001f5bc Image: <code>{os.path.basename(ov.path)}</code>"
            if ov.image_max_height > 0:
                desc += f" (max {ov.image_max_height}px)"
        if ov.text:
            desc += f"\n\U0001f4dd Text: <code>{ov.text}</code>"
        if not ov.path and not ov.text:
            desc += "\n(no layers configured)"
        return desc

    def _text_ov_image(self) -> str:
        ov = self.cfg.overlay
        desc = "\U0001f5bc <b>Overlay Image</b>\n"
        if ov.path:
            desc += f"File: <code>{os.path.basename(ov.path)}</code>\n"
        else:
            desc += "File: (not set)\n"
        desc += f"Position: {ov.image_position}"
        if ov.image_position == "custom":
            desc += f" ({ov.image_x},{ov.image_y})"
        desc += f"\nOpacity: {ov.image_opacity:.2f}"
        mh = ov.image_max_height
        desc += f"\nMax height: {f'{mh}px' if mh > 0 else 'original'}"
        return desc

    def _text_ov_text(self) -> str:
        ov = self.cfg.overlay
        desc = "\U0001f4dd <b>Overlay Text</b>\n"
        if ov.text:
            desc += f"Text: <code>{ov.text}</code>\n"
        else:
            desc += "Text: (not set)\n"
        desc += f"Size: {ov.font_size}px | Color: {ov.font_color}\n"
        desc += f"Position: {ov.text_position}"
        if ov.text_position == "custom":
            desc += f" ({ov.text_x},{ov.text_y})"
        desc += f"\nOpacity: {ov.text_opacity:.2f}"
        if ov.font_path:
            desc += f"\nFont: <code>{os.path.basename(ov.font_path)}</code>"
        return desc

    def _text_ov_img_pos(self) -> str:
        ov = self.cfg.overlay
        desc = f"\U0001f4cd <b>Image Position</b>\nCurrent: {ov.image_position}"
        if ov.image_position == "custom":
            desc += f" ({ov.image_x},{ov.image_y})"
        return desc

    def _text_ov_txt_pos(self) -> str:
        ov = self.cfg.overlay
        desc = f"\U0001f4cd <b>Text Position</b>\nCurrent: {ov.text_position}"
        if ov.text_position == "custom":
            desc += f" ({ov.text_x},{ov.text_y})"
        return desc

    def _text_targets(self) -> str:
        if not self.cfg.output.targets:
            return "<b>Targets:</b> none"
        lines = "\n".join(
            f"  {i+1}. <code>{_short_url(t)}</code>"
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
            f"  Preset: {v.preset} / tune: {v.tune}\n"
            f"  Audio: {a.bitrate} / {a.sample_rate}Hz"
        )

    def _text_recording(self) -> str:
        r = self.cfg.recording
        status = "\u2705 ON" if r.enabled else "\u274c OFF"
        lines = [
            f"<b>Recording:</b> {status}",
            f"  Directory: <code>{r.directory}</code>",
            f"  Max file: {r.max_file_size_mb} MB",
        ]
        try:
            import shutil as _shutil
            usage = _shutil.disk_usage(r.directory)
            free_gb = usage.free / (1024 ** 3)
            lines.append(f"  Disk free: {free_gb:.1f} GB")
        except OSError:
            lines.append("  Disk free: N/A")
        return "\n".join(lines)

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
        _btn("\u23fa Recording", "rec:menu"),
    ],
    [
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
    bg_check = lambda t: " \u2705" if ph.background == t else ""
    img_label = "\U0001f5bc Image \u2705" if ph.image_path else "\U0001f5bc Image"
    vid_label = "\U0001f3ac Video \u2705" if ph.video_path else "\U0001f3ac Video"
    txt_label = "\U0001f4dd Text \u2705" if ph.text else "\U0001f4dd Text"
    rows = [
        [
            _btn(f"\u2b1b Black{bg_check('black')}", "ph:black"),
            _btn(f"\U0001f4fa Testcard{bg_check('testcard')}", "ph:testcard"),
        ],
        [
            _btn(f"{img_label} \u25b8", "phimg:menu"),
            _btn(f"{vid_label} \u25b8", "phvid:menu"),
        ],
        [_btn(f"{txt_label} \u25b8", "phtxt:menu")],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]
    return rows


def _kb_ph_image(cfg: Config):
    ph = cfg.placeholder
    mh = ph.image_max_height
    mh_label = f"{mh}px" if mh > 0 else "full"
    rows = [
        [
            _btn("\U0001f4f7 Set image", "phimg:set"),
            _btn("\u274c Remove", "phimg:clear"),
        ],
        [_btn(f"\U0001f4cd Position ({ph.image_position})", "phimg:pos")],
        [
            _btn(f"\U0001f4a7 Opacity ({ph.image_opacity:.1f})", "phimg:opacity"),
            _btn(f"\U0001f4cf Max H ({mh_label})", "phimg:maxh"),
        ],
        [_btn("\u25c0\ufe0f Placeholder", "ph:menu")],
    ]
    return rows


def _kb_ph_video(cfg: Config):
    ph = cfg.placeholder
    mh = ph.video_max_height
    mh_label = f"{mh}px" if mh > 0 else "full"
    rows = [
        [
            _btn("\U0001f3ac Set video", "phvid:set"),
            _btn("\u274c Remove", "phvid:clear"),
        ],
        [_btn(f"\U0001f4cd Position ({ph.video_position})", "phvid:pos")],
        [
            _btn(f"\U0001f4a7 Opacity ({ph.video_opacity:.1f})", "phvid:opacity"),
            _btn(f"\U0001f4cf Max H ({mh_label})", "phvid:maxh"),
        ],
        [_btn("\u25c0\ufe0f Placeholder", "ph:menu")],
    ]
    return rows


def _kb_ph_text(cfg: Config):
    ph = cfg.placeholder
    rows = [
        [
            _btn("\u270f\ufe0f Content", "phtxt:content"),
            _btn("\u274c Remove", "phtxt:off"),
        ],
        [
            _btn(f"\U0001f524 Size ({ph.font_size})", "phtxt:size"),
            _btn(f"\U0001f3a8 Color ({ph.font_color})", "phtxt:color"),
        ],
        [
            _btn(
                f"\U0001f4cd Position ({ph.text_position})",
                "phtxt:pos",
            ),
        ],
        [
            _btn(f"\U0001f4a7 Opacity ({ph.text_opacity:.1f})", "phtxt:opacity"),
            _btn("\U0001f4c1 Font", "phtxt:font"),
        ],
        [_btn("\u25c0\ufe0f Placeholder", "ph:menu")],
    ]
    return rows


_POSITION_BACK = {
    "phpos":     "phtxt:menu",
    "phimgpos":  "phimg:menu",
    "phvidpos":  "phvid:menu",
    "ovimgpos":  "ovimg:menu",
    "ovtxtpos":  "ovtxt:menu",
}


def _kb_position(prefix: str):
    """Position preset keyboard. prefix is 'phpos', 'ovimgpos', or 'ovtxtpos'."""
    back = _POSITION_BACK.get(prefix, "menu:main")
    return [
        [
            _btn("\u2196 TL", f"{prefix}:top-left"),
            _btn("\u2b06\ufe0f TC", f"{prefix}:top-center"),
            _btn("\u2197 TR", f"{prefix}:top-right"),
        ],
        [
            _btn("\u2b05\ufe0f L", f"{prefix}:left"),
            _btn("\u23fa C", f"{prefix}:center"),
            _btn("\u27a1\ufe0f R", f"{prefix}:right"),
        ],
        [
            _btn("\u2199 BL", f"{prefix}:bottom-left"),
            _btn("\u2b07\ufe0f BC", f"{prefix}:bottom-center"),
            _btn("\u2198 BR", f"{prefix}:bottom-right"),
        ],
        [_btn("\U0001f4d0 Custom x,y", f"{prefix}:custom")],
        [_btn("\u25c0\ufe0f Back", back)],
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
            _btn("\U0001f5bc Image \u25b8", "ovimg:menu"),
            _btn("\U0001f4dd Text \u25b8", "ovtxt:menu"),
        ],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_ov_image(cfg: Config):
    ov = cfg.overlay
    mh = ov.image_max_height
    mh_label = f"{mh}px" if mh > 0 else "orig"
    rows = [
        [
            _btn("\U0001f4f7 Set image", "ovimg:set"),
            _btn("\u274c Clear", "ovimg:clear"),
        ],
        [
            _btn(f"\U0001f4cd Position ({ov.image_position})", "ovimg:pos"),
        ],
        [
            _btn(f"\U0001f4a7 Opacity ({ov.image_opacity:.1f})", "ovimg:opacity"),
            _btn(f"\U0001f4cf Max H ({mh_label})", "ovimg:maxh"),
        ],
        [_btn("\u25c0\ufe0f Overlay", "ov:menu")],
    ]
    return rows


def _kb_ov_text(cfg: Config):
    ov = cfg.overlay
    return [
        [
            _btn("\u270f\ufe0f Content", "ovtxt:content"),
            _btn("\u274c Clear", "ovtxt:clear"),
        ],
        [
            _btn(f"\U0001f524 Size ({ov.font_size})", "ovtxt:size"),
            _btn(f"\U0001f3a8 Color ({ov.font_color})", "ovtxt:color"),
        ],
        [
            _btn(f"\U0001f4cd Position ({ov.text_position})", "ovtxt:pos"),
        ],
        [
            _btn(f"\U0001f4a7 Opacity ({ov.text_opacity:.1f})", "ovtxt:opacity"),
            _btn("\U0001f4c1 Font", "ovtxt:font"),
        ],
        [_btn("\u25c0\ufe0f Overlay", "ov:menu")],
    ]


def _kb_targets(cfg: Config):
    rows = []
    for i, t in enumerate(cfg.output.targets):
        rows.append([_btn(f"\u274c {i+1}. {_short_url(t)}", f"target:rm:{i}")])
    rows.append([_btn("\u2795 Add target", "target:add")])
    rows.append([_btn("\u25c0\ufe0f Menu", "menu:main")])
    return rows


def _kb_out(cfg: Config):
    v = cfg.output.video
    return [
        [
            _btn(f"\U0001f4ca Bitrate ({v.bitrate})", "out:bitrate"),
            _btn(f"\U0001f3ac FPS ({v.fps})", "out:fps"),
        ],
        [
            _btn(f"\U0001f4d0 Size ({v.width}\u00d7{v.height})", "out:size"),
            _btn(f"\u2699\ufe0f Preset ({v.preset})", "out:preset"),
        ],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_recording(cfg: Config):
    label = "\u23f9 Disable recording" if cfg.recording.enabled else "\u23fa Enable recording"
    return [
        [_btn(label, "rec:toggle")],
        [_btn("\u25c0\ufe0f Menu", "menu:main")],
    ]


def _kb_presets(cfg: Config):
    current = cfg.output.video.preset
    presets = sorted(_X264_PRESETS)
    rows = []
    for i in range(0, len(presets), 3):
        row = []
        for p in presets[i:i+3]:
            label = f"\u2705 {p}" if p == current else p
            row.append(_btn(label, f"out:p_{p}"))
        rows.append(row)
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

def _is_valid_rtmp_url(url: str) -> bool:
    """Validate that URL starts with rtmp:// or rtmps://."""
    return bool(re.match(r"^rtmps?://\S+", url))


def _normalize_bitrate(raw: str) -> Optional[str]:
    """Normalize bitrate input to 'NNNNk' or 'Nm'.

    Accepts: '6000', '6000k', '6000 k', '6M', '6 m', '6000K'.
    Bare numbers are assumed kbps.
    Returns normalized string or None if invalid.
    """
    val = raw.strip().lower().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([km]?)$", val)
    if not m:
        return None
    num_str, suffix = m.group(1), m.group(2)
    num = float(num_str)
    if num <= 0:
        return None
    if not suffix:
        suffix = "k"
    # Validate against platform limits (500k - 51000k)
    kbps = num if suffix == "k" else num * 1000
    if not (500 <= kbps <= 51000):
        return None
    if "." in num_str:
        return f"{num:g}{suffix}"
    return f"{int(num)}{suffix}"


# Platform-safe limits for the Telegram bot validation.
# These are the union of YouTube/Twitch/Telegram Live limits.
_MIN_BITRATE_KBPS = 500
_MAX_BITRATE_KBPS = 51000
_MIN_FPS = 1
_MAX_FPS = 60
_MIN_WIDTH = 160
_MAX_WIDTH = 3840
_MIN_HEIGHT = 120
_MAX_HEIGHT = 2160


_PRESET_WEIGHT = {
    "ultrafast": 0, "superfast": 1, "veryfast": 2, "faster": 3,
    "fast": 4, "medium": 5, "slow": 6, "slower": 7, "veryslow": 8,
}


def _parse_fps(fps_str: str) -> float:
    """Parse fps string like '30', '29.97', '30/1', '60000/1001'."""
    try:
        if "/" in fps_str:
            num, den = fps_str.split("/", 1)
            return float(num) / float(den)
        return float(fps_str)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _cpu_hints(cfg: Config, stream=None) -> list:
    """Return CPU load hints based on input/output mismatches."""
    hints = []
    v = cfg.output.video
    a = cfg.output.audio
    pw = _PRESET_WEIGHT.get(v.preset, 5)
    out_px = v.width * v.height

    if stream and stream.has_video and stream.width > 0 and stream.height > 0:
        in_w, in_h = stream.width, stream.height
        in_px = in_w * in_h

        # --- Resolution mismatch ---
        if in_w != v.width or in_h != v.height:
            if in_px > out_px:
                ratio = in_px / out_px
                hints.append(
                    f"<b>Downscale {in_w}\u00d7{in_h} \u2192 "
                    f"{v.width}\u00d7{v.height}</b> ({ratio:.1f}x pixels) "
                    f"(/output size)"
                )
            else:
                ratio = out_px / in_px
                hints.append(
                    f"<b>Upscale {in_w}\u00d7{in_h} \u2192 "
                    f"{v.width}\u00d7{v.height}</b> ({ratio:.1f}x pixels) "
                    f"\u2014 wasteful, match input (/output size)"
                )

        # --- Aspect ratio mismatch (padding) ---
        in_ar = in_w / in_h if in_h else 0
        out_ar = v.width / v.height if v.height else 0
        if in_ar and out_ar and abs(in_ar - out_ar) > 0.05:
            hints.append(
                f"<b>Aspect ratio mismatch</b> "
                f"({in_ar:.2f} \u2192 {out_ar:.2f}) \u2014 "
                f"padding added (/output size)"
            )

        # --- FPS mismatch ---
        in_fps = _parse_fps(stream.fps)
        if in_fps > 0 and in_fps != v.fps:
            if in_fps > v.fps:
                hints.append(
                    f"<b>FPS drop {in_fps:.0f} \u2192 {v.fps}</b> "
                    f"\u2014 frame skipping (/output fps)"
                )
            else:
                hints.append(
                    f"<b>FPS up {in_fps:.0f} \u2192 {v.fps}</b> "
                    f"\u2014 frame duplication, wasteful (/output fps)"
                )

        # --- Codec decode complexity ---
        codec = (stream.codec_video or "").lower()
        if codec in ("hevc", "h265", "h.265"):
            hints.append(
                "<b>H.265 input</b> \u2014 heavy decode + re-encode to H.264"
            )
        elif codec in ("av1",):
            hints.append(
                "<b>AV1 input</b> \u2014 very heavy decode + re-encode to H.264"
            )
        elif codec in ("vp9",):
            hints.append(
                "<b>VP9 input</b> \u2014 heavy decode + re-encode to H.264"
            )

        # --- Audio sample rate mismatch ---
        if stream.has_audio and stream.codec_audio != "n/a":
            # We can't probe input sample rate from StreamInfo,
            # but codec mismatch means re-encoding is guaranteed
            audio_codec = (stream.codec_audio or "").lower()
            if audio_codec and audio_codec not in ("aac",):
                hints.append(
                    f"<b>Audio {stream.codec_audio} \u2192 AAC</b> "
                    f"\u2014 transcoding (/output audio)"
                )

    elif not stream:
        # No live stream — placeholder mode
        hints.append(
            "<b>Placeholder active</b> \u2014 "
            "generating video from scratch (normal idle load)"
        )

    # --- Always-on factors (regardless of stream) ---

    # Preset weight
    if pw >= 6:
        hints.append(
            f"<b>Preset {v.preset}</b> \u2014 heavy encoder; "
            f"try <code>fast</code>/<code>veryfast</code> (/output preset)"
        )
    elif pw >= 4:
        hints.append(
            f"<b>Preset {v.preset}</b> \u2014 moderate encoder load "
            f"(/output preset)"
        )

    # Output resolution absolute cost
    if out_px >= 3840 * 2160:
        hints.append(
            f"<b>4K output</b> \u2014 extreme encoding cost (/output size)"
        )
    elif out_px >= 2560 * 1440:
        hints.append(
            f"<b>1440p output</b> \u2014 high encoding cost (/output size)"
        )

    # Overlay compositing
    if cfg.overlay.enabled:
        hints.append(
            "<b>Overlay</b> \u2014 compositing filter active (/overlay off)"
        )

    # Multi-target muxing
    n = len(cfg.output.targets)
    if n >= 3:
        hints.append(f"<b>{n} targets</b> \u2014 muxing overhead")

    if not hints:
        hints.append("\u2714 No bottlenecks detected")

    return hints


def _position_label(pos: str) -> str:
    """Short emoji label for a position preset."""
    labels = {
        "top-left": "\u2196", "top-center": "\u2b06\ufe0f",
        "top-right": "\u2197", "left": "\u2b05\ufe0f",
        "center": "\u23fa", "right": "\u27a1\ufe0f",
        "bottom-left": "\u2199", "bottom-center": "\u2b07\ufe0f",
        "bottom-right": "\u2198", "custom": "\U0001f4d0",
    }
    return labels.get(pos, pos)


def _short_url(url: str) -> str:
    """Shorten an RTMP URL for display in buttons/status."""
    # rtmp://a.rtmp.youtube.com/live2/xxxx-xxxx → youtube/xxxx...
    # rtmps://dc4-1.rtmp.t.me/s/... → t.me/...
    try:
        # Strip protocol
        rest = url.split("://", 1)[1] if "://" in url else url
        host, _, path = rest.partition("/")
        # Simplify host
        if "youtube" in host:
            host = "youtube"
        elif "twitch" in host:
            host = "twitch"
        elif "t.me" in host:
            host = "telegram"
        else:
            # Use last domain part
            parts = host.split(".")
            host = parts[-2] if len(parts) >= 2 else host
        # Shorten key
        key = path.rsplit("/", 1)[-1] if "/" in path else path
        if len(key) > 12:
            key = key[:12] + "\u2026"
        return f"{host}/{key}"
    except Exception:
        return url[:30]


def _ext_from_mime(mime: str) -> str:
    """Map common MIME types to file extensions."""
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
        "video/x-msvideo": ".avi",
    }
    return mapping.get(mime.lower(), "")


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
