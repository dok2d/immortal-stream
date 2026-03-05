"""Telegram notification sender with async queue."""
import asyncio
import json
import logging
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen, Request

log = logging.getLogger("telegram")

MAX_RETRIES = 3
HTTP_TIMEOUT = 10


class TelegramNotifier:
    """Queued, async-safe Telegram notification sender."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def send(self, text: str) -> None:
        """Non-blocking enqueue of a notification message."""
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("Telegram queue full, dropping message")

    async def _worker(self) -> None:
        """Process queued messages with retry and exponential backoff."""
        while True:
            text = await self._queue.get()
            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._post, text
                    )
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        log.error("Telegram send failed after retries: %s", e)
            self._queue.task_done()

    def _post(self, text: str) -> None:
        """Synchronous HTTP POST to Telegram sendMessage API."""
        url = f"{self._base}/sendMessage"
        data = urlencode(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error: {result}")


class NoopNotifier:
    """Stub notifier used when Telegram is disabled."""

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def send(self, text: str) -> None:
        log.info("[NOTIFY] %s", text)
