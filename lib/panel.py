"""Browser control panel for the tamper addon (Charles-style rules UI).

Started by tamper_addon.py when TAMPER_PANEL_PORT is set (proxy.sh does it). Listens on
127.0.0.1 only. Every /api call needs the per-run token embedded in the page, and the Host
header must be local, so other websites open in the same browser cannot drive it.

Environment (all optional, proxy.sh sets them):
    TAMPER_LIBRARY     folders with tamper files to pick from, separated by ':'
    TAMPER_ENDPOINTS   endpoints list (default: <toolkit>/endpoints.txt, base-urls.txt next to it)
    TAMPER_PROXY_HOST, TAMPER_PROXY_PORT, TAMPER_WEB_URL   shown in the panel header
"""

import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import panel_data as data
import rules as rules_lib
import settings as settings_lib

LIB_DIR = Path(__file__).resolve().parent
PAGE = LIB_DIR / "panel.html"
# Fixed allow-list of the page's own assets; nothing else is served as a static file.
ASSETS = {f"/{name}": (LIB_DIR / name, kind) for name, kind in [
    ("panel.css", "text/css"),
    ("panel.js", "text/javascript"),
    ("panel-rules.js", "text/javascript"),
    ("panel-traffic.js", "text/javascript"),
    ("panel-endpoints.js", "text/javascript"),
    ("panel-pause.js", "text/javascript"),
    ("panel-settings.js", "text/javascript"),
]}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_PREVIEW_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
PAUSED_ROUTE = re.compile(r"^/api/paused/([0-9a-f]{6,32})$")


def _proxy_info():
    return {
        "host": os.environ.get("TAMPER_PROXY_HOST", ""),
        "port": os.environ.get("TAMPER_PROXY_PORT", ""),
        "web_url": os.environ.get("TAMPER_WEB_URL", ""),
    }


def _make_handler(token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the proxy terminal readable
            return

        @property
        def addon(self):
            return self.server.addon

        def _send(self, status, payload, content_type="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _allowed(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS:
                return False
            if not self.path.startswith("/api/"):
                return True
            return secrets.compare_digest(self.headers.get("X-Panel-Token", ""), token)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD_BYTES:
                raise ValueError("request body too large")
            return json.loads(self.rfile.read(length) or b"null")

        def _state(self):
            try:
                raw = rules_lib.read_raw(self.addon.rules_file)
                error = None
            except rules_lib.RuleError as exc:
                raw, error = [], str(exc)
            return {"rules": raw, "active": len(self.addon.rules), "error": error,
                    "proxy": _proxy_info(), "roots": [str(r) for r in data.roots()]}

        def _file(self, path):
            # Relative rule paths (responses/500_server_error.json) are relative to the rules folder.
            target = data.resolve(self.addon.base_dir, path) if path else None
            if target is None or not data.inside_roots(target) or not target.is_file():
                return self._send(404, {"error": "file not found in the tamper folders"})
            content = target.read_bytes()
            return self._send(200, {"path": path, "size": len(content),
                                    "content": content[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")})

        def _flow(self, flow_id):
            detail = self.addon.details.get(flow_id)
            return self._send(200, detail) if detail else self._send(404, {"error": "request no longer in memory"})

        def do_GET(self):  # noqa: N802 - http.server API
            if not self._allowed():
                return self._send(403, {"error": "forbidden"})
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                page = PAGE.read_text(encoding="utf-8").replace("__PANEL_TOKEN__", token)
                return self._send(200, page.encode("utf-8"), "text/html")
            if url.path in ASSETS:
                asset, content_type = ASSETS[url.path]
                return self._send(200, asset.read_bytes(), content_type)
            routes = {
                "/api/state": lambda: self._send(200, self._state()),
                "/api/recent": lambda: self._send(200, list(reversed(self.addon.recent))),
                "/api/endpoints": lambda: self._send(200, data.load_endpoints()),
                "/api/library": lambda: self._send(200, data.library_names()),
                "/api/files": lambda: self._send(200, data.search_files(query.get("q", ""))),
                "/api/file": lambda: self._file(query.get("path", "")),
                "/api/flow": lambda: self._flow(query.get("id", "")),
                "/api/paused": lambda: self._send(200, self.addon.breakpoints.list()),
                "/api/settings": lambda: self._send(200, self.addon.current_settings()),
            }
            route = routes.get(url.path)
            return route() if route else self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802 - http.server API
            if not self._allowed():
                return self._send(403, {"error": "forbidden"})
            try:
                payload = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self._send(400, {"error": str(exc)})
            path = urlsplit(self.path).path
            paused = PAUSED_ROUTE.match(path)
            if paused:
                return self._decide(paused.group(1), payload)
            if path == "/api/rules":
                return self._save_rules(payload)
            if path == "/api/settings":
                return self._save_settings(payload)
            if path == "/api/mock":
                return self._save_mock(payload or {})
            return self._send(404, {"error": "not found"})

        def _decide(self, pause_id, payload):
            try:
                decision = data.clean_decision(payload, self.addon.base_dir)
            except (ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            if not self.addon.breakpoints.decide(pause_id, decision):
                return self._send(404, {"error": "this call is no longer paused (it continued or timed out)"})
            return self._send(200, {"ok": True})

        def _save_rules(self, payload):
            if not isinstance(payload, list):
                return self._send(400, {"error": "expected a JSON array of rules"})
            raw = data.with_ids(payload)
            try:
                rules_lib.compile_rules(raw, self.addon.base_dir)
            except (rules_lib.RuleError, ValueError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            rules_lib.save_raw(self.addon.rules_file, raw)
            error = self.addon.reload()
            if error:
                return self._send(400, {"error": error})
            return self._send(200, {"rules": raw, "active": len(self.addon.rules)})

        def _save_settings(self, payload):
            try:
                return self._send(200, self.addon.update_settings(payload))
            except settings_lib.SettingsError as exc:
                return self._send(400, {"error": str(exc)})

        def _save_mock(self, payload):
            content = payload.get("content")
            if not isinstance(content, str):
                return self._send(400, {"error": "content must be a string"})
            data.MOCKS_DIR.mkdir(exist_ok=True)
            target = data.unique_mock_path(payload.get("name", "mock.json"))
            target.write_text(content, encoding="utf-8")
            return self._send(200, {"path": str(target), "name": target.name})

    return Handler


def start(addon, port):
    """Serve the panel in a daemon thread; returns the server (stop it with shutdown())."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(secrets.token_urlsafe(24)))
    server.addon = addon
    threading.Thread(target=server.serve_forever, name="tamper-panel", daemon=True).start()
    return server
