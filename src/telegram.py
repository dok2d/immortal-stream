"""Telegram notification sender with async queue and rate limiting."""
import asyncio
import json
import logging
import time
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen, Request

log = logging.getLogger("telegram")

MAX_RETRIES = 3
HTTP_TIMEOUT = 10
# Rate limiting: minimum seconds between messages to the same chat.
# Prevents spam during rapid stream connect/disconnect cycles.
MIN_SEND_INTERVAL = 2.0
# Maximum burst of messages allowed before rate limiting kicks in.
MAX_BURST = 5
# Window (seconds) for burst counting.
BURST_WINDOW = 10.0


class TelegramNotifier:
    """Queued, async-safe Telegram notification sender with rate limiting."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{token}"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._send_times: list = []

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
        """Process queued messages with retry, backoff, and rate limiting."""
        while True:
            text = await self._queue.get()

            # Rate limiting: enforce minimum interval and burst limit
            await self._rate_limit()

            for attempt in range(MAX_RETRIES):
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._post, text
                    )
                    self._send_times.append(time.monotonic())
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        log.error("Telegram send failed after retries: %s", e)
            self._queue.task_done()

    async def _rate_limit(self) -> None:
        """Enforce rate limiting before sending a message."""
        now = time.monotonic()

        # Prune old timestamps outside the burst window
        self._send_times = [
            t for t in self._send_times if now - t < BURST_WINDOW
        ]

        # If we've hit the burst limit, wait until the oldest message
        # in the window expires
        if len(self._send_times) >= MAX_BURST:
            wait = BURST_WINDOW - (now - self._send_times[0])
            if wait > 0:
                log.debug("Rate limiting: waiting %.1fs (burst limit)", wait)
                await asyncio.sleep(wait)
                # Prune again after sleep
                now = time.monotonic()
                self._send_times = [
                    t for t in self._send_times if now - t < BURST_WINDOW
                ]

        # Enforce minimum interval between messages
        if self._send_times:
            elapsed = now - self._send_times[-1]
            if elapsed < MIN_SEND_INTERVAL:
                await asyncio.sleep(MIN_SEND_INTERVAL - elapsed)

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
