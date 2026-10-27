"""Proxy-wide settings (Charles' Proxy menu): SSL proxying hosts, Focus, Block list, No caching,
global latency. Pure functions, no mitmproxy import; saved as settings.json next to rules.json.

    {
      "ssl":   {"mode": "all", "hosts": []},        all | only | except
      "focus": {"enabled": false, "hosts": []},     Traffic shows only these hosts
      "block": {"enabled": false, "hosts": []},     answered 403, never reach the server
      "no_cache": false,                            strip cache headers both ways
      "latency_ms": 0,                              added to every call (throttling)
      "app_timeout_s": 30                           breakpoint countdown (your app's read timeout)
    }

Host patterns: "api.example.com" (exact), "*.example.com" (any subdomain, not the bare
domain), "*" (everything). A ":port" suffix is not needed.
"""

import json
import os
import re
import tempfile
from pathlib import Path

SSL_MODES = ("all", "only", "except")
MAX_LATENCY_MS = 60_000
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 5, 600
HOST_PATTERN = re.compile(r"^(\*|(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*)$")
DEFAULTS = {
    "ssl": {"mode": "all", "hosts": []},
    "focus": {"enabled": False, "hosts": []},
    "block": {"enabled": False, "hosts": []},
    "no_cache": False,
    "latency_ms": 0,
    "app_timeout_s": 30,
}


class SettingsError(ValueError):
    """Settings that cannot be used, with a message naming the field."""


def _hosts(raw, field):
    if not isinstance(raw, list):
        raise SettingsError(f"{field}: expected a list of host patterns")
    hosts = [str(h).strip().lower() for h in raw if str(h).strip()]
    bad = [h for h in hosts if not HOST_PATTERN.match(h)]
    if bad:
        raise SettingsError(f"{field}: not a host pattern: {', '.join(bad)} (use api.example.com or *.example.com)")
    return list(dict.fromkeys(hosts))


def _number(raw, field, low, high):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{field}: expected a whole number")
    if not low <= value <= high:
        raise SettingsError(f"{field}: must be between {low} and {high}")
    return value


def validate(raw):
    """Return a complete, cleaned settings dict (missing keys get defaults). Raises SettingsError."""
    if not isinstance(raw, dict):
        raise SettingsError("settings must be an object")
    ssl = {**DEFAULTS["ssl"], **(raw.get("ssl") or {})}
    if ssl["mode"] not in SSL_MODES:
        raise SettingsError(f"ssl.mode must be one of {', '.join(SSL_MODES)}")
    focus = {**DEFAULTS["focus"], **(raw.get("focus") or {})}
    block = {**DEFAULTS["block"], **(raw.get("block") or {})}
    return {
        "ssl": {"mode": ssl["mode"], "hosts": _hosts(ssl["hosts"], "ssl.hosts")},
        "focus": {"enabled": bool(focus["enabled"]), "hosts": _hosts(focus["hosts"], "focus.hosts")},
        "block": {"enabled": bool(block["enabled"]), "hosts": _hosts(block["hosts"], "block.hosts")},
        "no_cache": bool(raw.get("no_cache", DEFAULTS["no_cache"])),
        "latency_ms": _number(raw.get("latency_ms", 0), "latency_ms", 0, MAX_LATENCY_MS),
        "app_timeout_s": _number(raw.get("app_timeout_s", DEFAULTS["app_timeout_s"]), "app_timeout_s",
                                 MIN_TIMEOUT_S, MAX_TIMEOUT_S),
    }


def load(path):
    """Settings from `path`, or the defaults when the file does not exist yet."""
    path = Path(path)
    if not path.is_file():
        return validate({})
    try:
        return validate(json.loads(path.read_text(encoding="utf-8") or "{}"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"settings file is not valid JSON: {exc}")


def save(path, settings):
    """Write atomically, so the proxy never reads a half-written file."""
    path = Path(path)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(settings, out, indent=2)
        out.write("\n")
    os.replace(tmp, path)


def host_regex(pattern):
    """mitmproxy matches ignore_hosts/allow_hosts against "host:port"; anchor and allow any port."""
    if pattern == "*":
        return r"^.*$"
    if pattern.startswith("*."):
        body = r"[^:]+\." + re.escape(pattern[2:])
    else:
        body = re.escape(pattern)
    return rf"^{body}(:\d+)?$"


def host_matches(pattern, host):
    return re.match(host_regex(pattern), host.lower()) is not None


def matches_any(patterns, host):
    return any(host_matches(p, host) for p in patterns)


def mitm_host_options(settings):
    """(ignore_hosts, allow_hosts) for mitmproxy from the SSL proxying setting."""
    regexes = [host_regex(h) for h in settings["ssl"]["hosts"]]
    mode = settings["ssl"]["mode"]
    if mode == "only" and regexes:
        return [], regexes
    if mode == "except" and regexes:
        return regexes, []
    return [], []
