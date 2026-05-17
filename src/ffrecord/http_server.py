"""Lightweight HTTP control/status server (stdlib http.server).

Endpoints:
  GET  /status                — full JSON status
  GET  /healthz               — "ok\n"
  POST /pause                 — global pause (all outputs stop writing; capture keeps running)
  POST /resume                — resume all outputs
  POST /outputs/{name}/enable — enable one output
  POST /outputs/{name}/disable — disable one output
  POST /stop                  — graceful shutdown
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger("ffrecord.http")


class _Handler(BaseHTTPRequestHandler):
    service = None   # injected before the server starts
    on_shutdown: Optional[Callable] = None  # injected before the server starts

    def log_message(self, fmt, *args):
        logger.debug("http %s - - [%s] %s", self.address_string(), self.log_date_time_string(), fmt % args)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_text(200, "ok\n")
        elif self.path == "/status":
            try:
                status = self.service.get_status()
                self._send_json(200, status)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif self.path == "/test_stop":
            self._send_json(200, {"on_shutdown": self.on_shutdown is not None, "type": str(type(self.on_shutdown))})
        else:
            self._send_text(404, "not found\n")

    def do_POST(self):
        if self.path == "/pause":
            self.service.set_global_pause(True)
            self._send_json(200, {"paused": True})
        elif self.path == "/resume":
            self.service.set_global_pause(False)
            self._send_json(200, {"paused": False})
        elif self.path == "/stop":
            self._send_json(200, {"status": "shutting down"})
            if self.on_shutdown:
                def _shutdown_wrapper():
                    try:
                        logger.info("HTTP /stop: invoking shutdown callback")
                        self.on_shutdown()
                    except Exception as e:
                        logger.error("HTTP /stop: shutdown callback failed: %s", e, exc_info=True)
                threading.Thread(target=_shutdown_wrapper, daemon=False).start()
        elif self.path.startswith("/outputs/"):
            parts = self.path.split("/")
            if len(parts) == 4 and parts[3] in ("enable", "disable"):
                output_name = parts[2]
                enabled = parts[3] == "enable"
                ok = self.service.set_output_enabled(output_name, enabled)
                if ok:
                    self._send_json(200, {"output": output_name, "enabled": enabled})
                else:
                    self._send_json(404, {"error": f"output '{output_name}' not found"})
            else:
                self._send_text(404, "not found\n")
        else:
            self._send_text(404, "not found\n")

    def _send_text(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code: int, obj) -> None:
        data = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class HttpServer:
    def __init__(self, bind: str, port: int, service, on_shutdown: Optional[Callable] = None):
        _Handler.service = service
        _Handler.on_shutdown = on_shutdown
        self._server = ThreadingHTTPServer((bind, port), _Handler)
        self._thread: Optional[threading.Thread] = None
        logger.info("HTTP server listening on %s:%d", bind, port)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="http-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5.0)
