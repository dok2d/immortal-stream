"""
Webhook HTTP server that receives mediamtx event hooks.

mediamtx calls runOnPublish/runOnUnpublish shell commands which in turn
send HTTP POST requests to this server.
"""
import asyncio
import logging
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from typing import Optional

log = logging.getLogger("webhook")


class WebhookServer:
    def __init__(self, port: int, manager, cfg):
        self.port = port
        self.manager = manager
        self.cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        mgr = self.manager
        cfg = self.cfg
        loop = self._loop

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode(errors="replace") if length else ""
                params = {}
                for item in body.split("&"):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        params[k] = v

                path_part = self.path.rstrip("/").split("/")[-1]  # "publish" or "unpublish"

                stream_path = params.get("path", "").replace("%2F", "/")
                conn_type = params.get("conn_type", "unknown")
                conn_id = params.get("conn_id", "")

                log.info("Hook: %s path=%s type=%s id=%s",
                         path_part, stream_path, conn_type, conn_id)

                # Key validation
                if (cfg.ingest.stream_key_required
                        and cfg.ingest.allowed_key
                        and path_part == "publish"):
                    allowed_path = f"live/{cfg.ingest.allowed_key}"
                    if stream_path != allowed_path and stream_path != cfg.ingest.allowed_key:
                        log.warning(
                            "Rejected stream on path=%s (key mismatch)", stream_path
                        )
                        self.send_response(403)
                        self.end_headers()
                        self.wfile.write(b"Forbidden: invalid stream key")
                        return

                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

                if path_part == "publish":
                    asyncio.run_coroutine_threadsafe(
                        mgr.on_stream_start(stream_path, conn_type, conn_id),
                        loop,
                    )
                elif path_part == "unpublish":
                    asyncio.run_coroutine_threadsafe(
                        mgr.on_stream_stop(stream_path, conn_id),
                        loop,
                    )

        def serve():
            server = HTTPServer(("127.0.0.1", self.port), Handler)
            log.info("Webhook server listening on 127.0.0.1:%d", self.port)
            server.serve_forever()

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
