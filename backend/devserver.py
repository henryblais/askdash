"""
Zero-dependency dev server (Python standard library only).

    python -m backend.devserver [--port 8000]

Same routes as app.py. Useful for running the demo on a machine with no pip
access, and as the harness for the test suite.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .core import App

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))


def make_handler(core: App):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.abspath(os.path.join(FRONTEND_DIR, path.lstrip("/")))
            if not full.startswith(FRONTEND_DIR) or not os.path.isfile(full):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = urlparse(self.path).path
            if p == "/api/health":
                return self._json({"ok": True, "planner": core.planner.name})
            if p == "/api/schema":
                return self._json(core.schema())
            if p == "/api/dashboard":
                return self._json(core.dashboard())
            return self._static(p)

        def do_POST(self):
            p = urlparse(self.path).path
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "Invalid JSON"}, 400)
            if p == "/api/ask":
                return self._json(core.ask(body.get("request", ""), body.get("panels", []),
                                           body.get("history", []), client_id=self.client_address[0]))
            if p == "/api/query":
                return self._json(core.query(body.get("sql", "")))
            self.send_error(404)

        def log_message(self, fmt, *args):  # quieter logs
            if os.environ.get("ASKDASH_VERBOSE"):
                super().log_message(fmt, *args)

    return Handler


def serve(port: int = 8000, core: App | None = None):
    core = core or App()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(core))
    print(f"AskDash dev server on http://localhost:{port}  (planner: {core.planner.name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    serve(ap.parse_args().port)
