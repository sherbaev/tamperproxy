"""mitmproxy addon: Map Local / Map Remote / Rewrite / Delay / Breakpoint rules + proxy settings.

Loaded via `mitmdump -s tamper_addon.py` (proxy.sh, harness.py) or `mitmweb -s …`.
mitmproxy owns the CLI, so configuration comes from environment variables:

    TAMPER_MAPPINGS_FILE  rules JSON array (format: see rules.py). Required.
    TAMPER_SERVED_LOG     JSONL, one record per mocked/rewritten response. Required.
    TAMPER_UNMATCHED_LOG  JSONL, one record per request no rule touched. Optional.
    TAMPER_BASE_DIR       base folder for relative local_file paths (default: rules file folder).
    TAMPER_PANEL_PORT     start the browser control panel on 127.0.0.1:<port>. Optional.
    TAMPER_SETTINGS_FILE  proxy settings (format: see settings.py). Default: settings.json next to the rules.

The rules file is re-read whenever it changes, so rules can be edited while the proxy runs
(by hand, from the panel or by any script). A broken edit keeps the previous rules and logs
a warning. Logged URLs drop query string and fragment, so tokens never land on disk.
"""

import asyncio
import collections
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mitmproxy import ctx, http

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rules as rules_lib  # noqa: E402
import settings as settings_lib  # noqa: E402
import snapshot  # noqa: E402
from breakpoints import Breakpoints  # noqa: E402

MISSING_FILE_STATUS = 502
BLOCKED_STATUS = 403
REQUEST_CACHE_HEADERS = ("if-none-match", "if-modified-since")
RESPONSE_CACHE_HEADERS = ("etag", "last-modified", "expires")
RECENT_LIMIT = 300
DETAIL_MAX_BODY = 128 * 1024
BREAKPOINT_MAX_BODY = 512 * 1024
MOCK_ACTIONS = ("map_local",)
REMOTE_ACTIONS = ("map_remote",)
DELAY_ACTIONS = ("delay",)
REWRITE_ACTIONS = ("rewrite",)
BREAKPOINT_ACTIONS = ("breakpoint",)


def _safe_url(url):
    """Scheme+host+path only: drop query and fragment (common secret carriers)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; the tamper addon cannot configure itself.")
    return value


def _apply_edit(message, edit):
    """Headers first, then body: setting the body re-encodes it and fixes Content-Length."""
    if "headers" in edit:
        message.headers = http.Headers([(str(k).encode(), str(v).encode()) for k, v in edit["headers"]])
    if edit.get("body") is not None:
        message.text = edit["body"]


def _set_headers(headers, changes):
    """Set or (value None) remove headers on mitmproxy's header object; repeated headers survive."""
    for name, value in changes.items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = str(value)


class TamperAddon:
    # mitmproxy derives the addon name from the class name; an explicit one avoids
    # clashing with its built-in `maplocal` addon (>= 11).
    name = "tamper"

    def __init__(self):
        self.rules_file = Path(_required_env("TAMPER_MAPPINGS_FILE"))
        self.base_dir = Path(os.environ.get("TAMPER_BASE_DIR") or self.rules_file.parent)
        # Startup fails loudly: silently serving no mocks would look like a passing run.
        self.rules = rules_lib.compile_rules(rules_lib.read_raw(self.rules_file), self.base_dir)
        self.loaded_mtime = _mtime(self.rules_file)
        self.served_log = Path(_required_env("TAMPER_SERVED_LOG"))
        self.served_log.write_text("", encoding="utf-8")
        unmatched = os.environ.get("TAMPER_UNMATCHED_LOG")
        self.unmatched_log = Path(unmatched) if unmatched else None
        if self.unmatched_log:
            self.unmatched_log.write_text("", encoding="utf-8")
        self.recent = collections.deque(maxlen=RECENT_LIMIT)
        self.details = collections.OrderedDict()
        self.breakpoints = Breakpoints()
        self.panel_server = None
        self.loop = None
        self.settings_file = Path(os.environ.get("TAMPER_SETTINGS_FILE") or self.rules_file.parent / "settings.json")
        self.settings_mtime = _mtime(self.settings_file)
        try:
            self.settings = settings_lib.load(self.settings_file)
        except settings_lib.SettingsError as exc:
            logging.warning("tamper settings NOT loaded, using defaults: %s", exc)
            self.settings = settings_lib.validate({})

    def running(self):
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self._apply_mitm_options()
        # Started in the lifecycle hook (not on import) so a script reload stops the old
        # panel in done() before the new addon instance binds the port again.
        panel_port = os.environ.get("TAMPER_PANEL_PORT")
        if not panel_port or self.panel_server is not None:
            return
        import panel  # noqa: WPS433 - only needed in interactive mode
        try:
            self.panel_server = panel.start(self, int(panel_port))
        except OSError as exc:
            # A warning, not an error: mitmproxy exits on any error logged during startup.
            logging.warning("tamper panel NOT started on 127.0.0.1:%s (%s); the proxy keeps running. "
                            "Pick another port with --panel-port.", panel_port, exc)

    def done(self):
        if self.panel_server is not None:
            self.panel_server.shutdown()
            self.panel_server.server_close()
            self.panel_server = None

    def update_settings(self, raw):
        """Validate, save and apply new settings (called from the panel thread). Raises SettingsError."""
        clean = settings_lib.validate(raw)
        settings_lib.save(self.settings_file, clean)
        self.settings_mtime = _mtime(self.settings_file)
        self._use_settings(clean)
        return clean

    def current_settings(self):
        """Settings for the panel, picking up a settings file edited by hand or by an assistant."""
        self._reload_if_changed()
        return self.settings

    def _use_settings(self, clean):
        self.settings = clean
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._apply_mitm_options)
        else:
            self._apply_mitm_options()

    def _reload_settings(self):
        self.settings_mtime = _mtime(self.settings_file)
        try:
            clean = settings_lib.load(self.settings_file)
        except settings_lib.SettingsError as exc:
            logging.warning("tamper settings NOT reloaded, keeping previous ones: %s", exc)
            return
        self._use_settings(clean)
        logging.info("tamper settings reloaded")

    def _apply_mitm_options(self):
        """SSL proxying hosts become mitmproxy's ignore_hosts / allow_hosts (applies to new connections)."""
        ignore, allow = settings_lib.mitm_host_options(self.settings)
        try:
            ctx.options.update(ignore_hosts=ignore, allow_hosts=allow)
        except (AttributeError, KeyError) as exc:  # no mitmproxy context (unit tests)
            logging.debug("mitm host options not applied: %s", exc)

    def reload(self):
        """Re-read the rules file. Returns None on success, the error message otherwise."""
        self.loaded_mtime = _mtime(self.rules_file)
        try:
            self.rules = rules_lib.compile_rules(rules_lib.read_raw(self.rules_file), self.base_dir)
        except (rules_lib.RuleError, ValueError) as exc:
            logging.warning("tamper rules NOT reloaded, keeping previous ones: %s", exc)
            return str(exc)
        logging.info("tamper rules reloaded: %d active", len(self.rules))
        return None

    def _reload_if_changed(self):
        if _mtime(self.rules_file) != self.loaded_mtime:
            self.reload()
        if _mtime(self.settings_file) != self.settings_mtime:
            self._reload_settings()

    def _append(self, log_path, record):
        line = json.dumps({**record, "ts": time.time()}, ensure_ascii=False)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _record_unmatched(self, flow):
        if not self.unmatched_log:
            return
        # Query parameter NAMES only: values can carry credentials.
        query = urlsplit(flow.request.pretty_url).query
        keys = sorted({key for key, _ in parse_qsl(query, keep_blank_values=True)})
        self._append(self.unmatched_log, {
            "url": _safe_url(flow.request.pretty_url), "method": flow.request.method, "query_keys": keys,
        })

    def _record_recent(self, flow):
        self.recent.append({
            "id": flow.id,
            "ts": time.time(),
            "method": flow.request.method,
            # Traffic lists the URL the app called, also when Map Remote sent it elsewhere.
            "url": flow.metadata.get("tamper_original_url") or _safe_url(flow.request.pretty_url),
            "status": flow.response.status_code if flow.response else None,
            "rule": flow.metadata.get("tamper_rule"),
            "action": flow.metadata.get("tamper_action"),
            "paused": bool(flow.metadata.get("tamper_paused")),
            "remote": flow.metadata.get("tamper_remote"),
            "aborted": bool(flow.metadata.get("tamper_aborted")),
        })
        self.details[flow.id] = {"request": snapshot.request(flow, DETAIL_MAX_BODY),
                                 "response": snapshot.response(flow, DETAIL_MAX_BODY)}
        while len(self.details) > RECENT_LIMIT:
            self.details.popitem(last=False)

    def _serve_local(self, flow, rule):
        record = {
            "url": _safe_url(flow.request.pretty_url),
            "method": flow.request.method,
            "matched_regex": rule["url_regex"],
            "action": "map_local",
            "local_file": str(rule["local_file"]),
        }
        if not rule["local_file"].is_file():
            flow.response = http.Response.make(
                MISSING_FILE_STATUS,
                json.dumps({"error": f"mock file missing: {rule['local_file']}"}).encode("utf-8"),
                {"Content-Type": "application/json"},
            )
            self._append(self.served_log, {**record, "status": MISSING_FILE_STATUS, "served": False,
                                           "error": "mock file missing"})
            return
        # Read on every request, so an edited mock applies without touching the rule.
        flow.response = http.Response.make(
            rule["status_code"], rule["local_file"].read_bytes(), {"Content-Type": rule["content_type"]},
        )
        self._append(self.served_log, {**record, "status": rule["status_code"], "served": True})

    def _resolve_local(self, path):
        return rules_lib.resolve_local(self.base_dir, path)

    async def _break(self, flow, rule, phase):
        """Pause the call until the panel decides, then apply that decision. False when aborted."""
        taken = snapshot.request(flow, BREAKPOINT_MAX_BODY) if phase == "request" else {
            "method": flow.request.method, "url": flow.request.pretty_url,
            **snapshot.response(flow, BREAKPOINT_MAX_BODY)}
        flow.metadata["tamper_paused"] = True
        flow.metadata.setdefault("tamper_rule", rule["id"])
        flow.metadata.setdefault("tamper_action", "breakpoint")
        decision = await self.breakpoints.pause(phase, rule["id"], taken)
        action = decision.get("action", "continue")
        if action == "abort":
            flow.metadata["tamper_aborted"] = True
            if flow.killable:
                flow.kill()
            return False
        if action == "respond" and phase == "request":
            self._respond(flow, decision)
            return True
        _apply_edit(flow.request if phase == "request" else flow.response, decision.get("edit", {}))
        status = decision.get("edit", {}).get("status")
        if phase == "response" and status:
            flow.response.status_code = int(status)
            flow.response.reason = http.status_codes.RESPONSES.get(int(status), "")
        return True

    def _respond(self, flow, decision):
        status = int(decision.get("status") or 200)
        body = decision.get("body")
        if body is None and decision.get("local_file"):
            local = self._resolve_local(decision["local_file"])
            body = local.read_text(encoding="utf-8", errors="replace") if local.is_file() else ""
        flow.response = http.Response.make(status, (body or "").encode("utf-8"),
                                           {"Content-Type": decision.get("content_type") or "application/json"})

    def _block(self, flow):
        flow.metadata["tamper_action"] = "blocked"
        flow.response = http.Response.make(BLOCKED_STATUS, b'{"error": "blocked by tamper-proxy"}',
                                           {"Content-Type": "application/json"})

    def _map_remote(self, flow, rule):
        """Send the call to the rule's target; Host follows the new host."""
        target = rule["target"]
        query = flow.request.path.partition("?")[2]  # raw query string, kept as sent
        flow.metadata["tamper_remote"] = target["url"]
        flow.metadata["tamper_original_url"] = _safe_url(flow.request.pretty_url)
        flow.request.scheme = target["scheme"]
        flow.request.host = target["host"]
        flow.request.port = target["port"]
        if target["path"]:
            flow.request.path = target["path"] + (f"?{query}" if query else "")

    async def request(self, flow: http.HTTPFlow) -> None:
        self._reload_if_changed()
        settings = self.settings
        if settings["latency_ms"]:
            await asyncio.sleep(settings["latency_ms"] / 1000)
        if settings["block"]["enabled"] and settings_lib.matches_any(settings["block"]["hosts"], flow.request.pretty_host):
            self._block(flow)
            return
        if settings["no_cache"]:
            for name in REQUEST_CACHE_HEADERS:
                flow.request.headers.pop(name, None)
        url = flow.request.pretty_url
        method = flow.request.method.upper()

        mock = rules_lib.find_rule(self.rules, url, method, MOCK_ACTIONS)
        rule = mock or rules_lib.find_rule(self.rules, url, method, DELAY_ACTIONS)
        if rule and rule["delay_ms"]:
            await asyncio.sleep(rule["delay_ms"] / 1000)
        if mock:
            flow.metadata["tamper_rule"] = mock["id"]
            flow.metadata["tamper_action"] = "map_local"
            self._serve_local(flow, mock)
            return
        if rule:
            flow.metadata["tamper_rule"] = rule["id"]
            flow.metadata["tamper_regex"] = rule["url_regex"]
            flow.metadata["tamper_action"] = "delay"
        rewrite = rules_lib.find_rule(self.rules, url, method, REWRITE_ACTIONS)
        if rewrite and rewrite["rewrite"]["request_headers"]:
            _set_headers(flow.request.headers, rewrite["rewrite"]["request_headers"])
            flow.metadata["tamper_rule"] = rewrite["id"]
            flow.metadata["tamper_action"] = "rewrite"
        remote = rules_lib.find_rule(self.rules, url, method, REMOTE_ACTIONS)
        if remote:
            if remote["delay_ms"]:
                await asyncio.sleep(remote["delay_ms"] / 1000)
            flow.metadata["tamper_rule"] = remote["id"]
            flow.metadata["tamper_regex"] = remote["url_regex"]
            flow.metadata["tamper_action"] = "map_remote"
            self._map_remote(flow, remote)
        pause = rules_lib.find_rule(self.rules, url, method, BREAKPOINT_ACTIONS, phase="request")
        if pause and not await self._break(flow, pause, "request"):
            self._record_recent(flow)  # killed: the response hook will not run

    async def response(self, flow: http.HTTPFlow) -> None:
        url = flow.request.pretty_url
        method = flow.request.method.upper()
        if self.settings["no_cache"] and flow.response is not None:
            for name in RESPONSE_CACHE_HEADERS:
                flow.response.headers.pop(name, None)
            flow.response.headers["Cache-Control"] = "no-store"
        if flow.metadata.get("tamper_action") == "blocked":
            self._log_served(flow, "blocked")
            self._record_recent(flow)
            return
        # Also called for responses set in request() (map_local): those are never rewritten.
        if flow.metadata.get("tamper_action") != "map_local":
            rule = rules_lib.find_rule(self.rules, url, method, REWRITE_ACTIONS)
            if rule:
                await self._rewrite(flow, rule)
        pause = rules_lib.find_rule(self.rules, url, method, BREAKPOINT_ACTIONS, phase="response")
        if pause:
            await self._break(flow, pause, "response")
        action = flow.metadata.get("tamper_action")
        if action is None:
            self._record_unmatched(flow)
        elif action == "map_remote":
            self._log_served(flow, action, target=flow.metadata["tamper_remote"])
        elif action == "delay":
            self._log_served(flow, action)
        self._record_recent(flow)

    def _log_served(self, flow, action, **extra):
        """served.jsonl record for a call that was blocked, redirected or delayed, with the status the app got."""
        url = flow.metadata.get("tamper_original_url") or _safe_url(flow.request.pretty_url)
        self._append(self.served_log, {
            "url": url, "method": flow.request.method, "matched_regex": flow.metadata.get("tamper_regex"),
            "action": action, "status": flow.response.status_code if flow.response else None,
            "served": True, **extra,
        })

    async def _rewrite(self, flow, rule):
        if rule["delay_ms"]:
            await asyncio.sleep(rule["delay_ms"] / 1000)
        response = flow.response
        rewrite = rule["rewrite"]
        status, body = rules_lib.rewrite_status_and_body(rewrite, response.status_code, response.content or b"")
        if status != response.status_code:
            response.status_code = status
            # mitmproxy keeps the old reason phrase ("403 OK") unless it is set too.
            response.reason = http.status_codes.RESPONSES.get(status, "")
        if rewrite["body_replace"]:
            response.content = body
        _set_headers(response.headers, rewrite["headers"])
        flow.metadata["tamper_rule"] = rule["id"]
        flow.metadata["tamper_action"] = "rewrite"
        self._append(self.served_log, {
            "url": _safe_url(flow.request.pretty_url), "method": flow.request.method,
            "matched_regex": rule["url_regex"], "action": "rewrite", "status": status, "served": True,
        })


addons = [TamperAddon()]
