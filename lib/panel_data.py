"""Data the panel serves: endpoint/base-URL lists, tamper files, and checked breakpoint decisions.

Pure helpers (no HTTP), so they are easy to test. Only these folders are ever read:
responses/, mocks/ and the TAMPER_LIBRARY folders.
"""

import os
import re
import uuid
from pathlib import Path

import rules as rules_lib

TOOLKIT_DIR = Path(__file__).resolve().parent.parent
# A profile can bring ready responses in its own backend format (proxy.sh --profile).
RESPONSES_DIR = Path(os.environ.get("TAMPER_RESPONSES") or TOOLKIT_DIR / "responses")
# With a profile, saved mocks go into the profile's own mocks/ folder.
MOCKS_DIR = Path(os.environ.get("TAMPER_MOCKS") or TOOLKIT_DIR / "mocks")
MAX_FILE_RESULTS = 80
MAX_LIBRARY_FILES = 5000
MAX_SCAN_DEPTH = 3
MIN_STATUS, MAX_STATUS = 100, 599
MOCK_SUFFIXES = {".json", ".txt", ".xml", ".html", ".pdf", ""}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
ENDPOINT_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)")
BASE_LINE = re.compile(r"^([A-Z0-9_]+)\s+([a-z]+)\s+(https?://\S+)")
GROUP_SEPARATOR = "·"


def roots():
    """Folders the panel may list/read: stock responses, saved mocks, configured libraries."""
    libraries = [Path(p).expanduser() for p in os.environ.get("TAMPER_LIBRARY", "").split(":") if p.strip()]
    return [root.resolve() for root in [RESPONSES_DIR, MOCKS_DIR, *libraries] if root.is_dir()]


def inside_roots(path):
    resolved = Path(path).expanduser().resolve()
    return any(resolved == root or root in resolved.parents for root in roots())


def resolve(base_dir, path):
    return rules_lib.resolve_local(base_dir, path)


def parse_endpoints(text):
    """[{method, path, group, base}] from endpoints.txt ('# Group · BASE' lines start a group)."""
    group, base = "", ""
    endpoints = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            label = stripped.lstrip("# ").strip()
            group, _, base = (part.strip() for part in label.partition(GROUP_SEPARATOR))
            continue
        match = ENDPOINT_LINE.match(stripped)
        if match:
            endpoints.append({"method": match.group(1), "path": match.group(2), "group": group, "base": base})
    return endpoints


def parse_bases(text):
    """{KEY: {env: url}} from base-urls.txt."""
    bases = {}
    for line in text.splitlines():
        match = BASE_LINE.match(line.strip())
        if match:
            key, env, url = match.groups()
            bases.setdefault(key, {})[env] = url
    return bases


def load_endpoints():
    own = TOOLKIT_DIR / "endpoints.txt"
    default = own if own.is_file() else TOOLKIT_DIR / "profiles" / "example" / "endpoints.txt"
    endpoints_file = Path(os.environ.get("TAMPER_ENDPOINTS") or default)
    bases_file = endpoints_file.with_name("base-urls.txt")
    return {
        "endpoints": parse_endpoints(endpoints_file.read_text(encoding="utf-8")) if endpoints_file.is_file() else [],
        "bases": parse_bases(bases_file.read_text(encoding="utf-8")) if bases_file.is_file() else {},
    }


def _walk(root, depth=0):
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and depth < MAX_SCAN_DEPTH:
            yield from _walk(entry, depth + 1)
        elif entry.is_file() and entry.suffix.lower() in MOCK_SUFFIXES:
            yield entry


def _file_entry(root, path):
    return {"root": root.name, "path": str(path), "name": str(path.relative_to(root)), "size": path.stat().st_size}


def search_files(query):
    """Files under the allowed folders whose relative name contains every word of `query`."""
    words = [w for w in query.lower().split() if w]
    results = []
    for root in roots():
        for path in _walk(root):
            if all(word in str(path.relative_to(root)).lower() for word in words):
                results.append(_file_entry(root, path))
                if len(results) >= MAX_FILE_RESULTS:
                    return results
    return results


def library_names():
    """Every tamper file name (not the stock responses), for matching files to endpoints."""
    names = []
    for root in roots():
        if root == RESPONSES_DIR.resolve():
            continue
        for path in _walk(root):
            names.append({"root": root.name, "path": str(path), "name": str(path.relative_to(root))})
            if len(names) >= MAX_LIBRARY_FILES:
                return names
    return names


def unique_mock_path(name):
    """mocks/<safe name>.json, never overwriting an existing file."""
    raw = Path(name or "mock.json")
    stem = SAFE_NAME.sub("_", raw.stem).strip("._") or "mock"
    suffix = raw.suffix.lower() if raw.suffix.lower() in MOCK_SUFFIXES - {""} else ".json"
    candidate = MOCKS_DIR / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = MOCKS_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def with_ids(raw_rules):
    return [{**rule, "id": rule.get("id") or uuid.uuid4().hex[:8]} for rule in raw_rules]


def _status(value):
    status = int(value)
    if not MIN_STATUS <= status <= MAX_STATUS:
        raise ValueError(f"status code {status} is out of range")
    return status


def _headers(raw):
    if not isinstance(raw, list) or not all(isinstance(h, list) and len(h) == 2 for h in raw):
        raise ValueError("headers must be a list of [name, value] pairs")
    clean = [[str(name).strip(), str(value)] for name, value in raw]
    if any(not name or any(c in name + value for c in "\r\n") for name, value in clean):
        raise ValueError("header names must be non-empty and headers cannot contain line breaks")
    return clean


def clean_decision(raw, base_dir):
    """Validate a breakpoint decision from the browser; returns the cleaned dict or raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("decision must be an object")
    action = raw.get("action")
    if action == "abort":
        return {"action": "abort"}
    if action == "respond":
        decision = {"action": "respond", "status": _status(raw.get("status") or 200),
                    "content_type": str(raw.get("content_type") or "application/json")}
        if raw.get("body") is not None:
            return {**decision, "body": str(raw["body"])}
        target = resolve(base_dir, raw.get("local_file") or "")
        if not raw.get("local_file") or not inside_roots(target) or not target.is_file():
            raise ValueError("respond needs a body or a file from the tamper folders")
        return {**decision, "local_file": str(target)}
    if action == "continue":
        edit = raw.get("edit") or {}
        clean = {}
        if "headers" in edit:
            clean["headers"] = _headers(edit["headers"])
        if edit.get("body") is not None:
            clean["body"] = str(edit["body"])
        if edit.get("status") not in (None, ""):
            clean["status"] = _status(edit["status"])
        return {"action": "continue", "edit": clean}
    raise ValueError("action must be continue, abort or respond")
