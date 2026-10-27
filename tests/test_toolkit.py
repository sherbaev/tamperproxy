"""Tests for the toolkit logic that needs no device.

    ~/.cache/tamper-proxy-venv/bin/python tests/test_toolkit.py   (or pytest)

Addon tests drive real mitmproxy flows (mitmproxy.test.tflow); they are skipped when
mitmproxy is not importable.
"""

import asyncio
import http.client
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

import adb  # noqa: E402
import device_flow  # noqa: E402
import harness  # noqa: E402
import panel  # noqa: E402
import panel_data  # noqa: E402
import rules  # noqa: E402
import scrub  # noqa: E402
import settings  # noqa: E402

try:
    from mitmproxy.test import tflow, tutils
    HAS_MITM = True
except ImportError:  # pragma: no cover
    HAS_MITM = False


def _expect_error(func, fragment):
    try:
        func()
    except (rules.RuleError, RuntimeError, ValueError) as exc:
        assert fragment in str(exc), str(exc)
        return
    raise AssertionError(f"expected an error containing {fragment!r}")


# ---------- rules ----------

def test_rule_validation_names_the_broken_rule():
    base = Path("/tmp")
    _expect_error(lambda: rules.compile_rule(0, {"url_regex": "([", "local_file": "a"}, base), "invalid url_regex")
    _expect_error(lambda: rules.compile_rule(1, {"url_regex": "x"}, base), "map_local needs local_file")
    _expect_error(lambda: rules.compile_rule(2, {"url_regex": "x", "action": "block"}, base), "unknown action")
    _expect_error(lambda: rules.compile_rule(3, {"url_regex": "x", "local_file": "a", "status_code": 99}, base),
                  "out of range")
    _expect_error(lambda: rules.compile_rule(4, {"url_regex": "x", "action": "rewrite",
                                                 "rewrite": {"body_replace": [{"find": ""}]}}, base), "non-empty")


def test_disabled_rules_are_left_out_and_relative_files_resolve_against_base():
    raw = [
        {"url_regex": "a", "local_file": "responses/x.json"},
        {"url_regex": "b", "local_file": "y.json", "enabled": False},
    ]
    actual = rules.compile_rules(raw, Path("/base"))
    assert [r["url_regex"] for r in actual] == ["a"]
    assert actual[0]["local_file"] == Path("/base/responses/x.json")


def test_first_matching_rule_of_the_requested_action_wins():
    compiled = rules.compile_rules([
        {"url_regex": "orders", "action": "delay", "delay_ms": 10},
        {"url_regex": "orders", "local_file": "a.json", "methods": ["POST"]},
        {"url_regex": "orders", "local_file": "b.json"},
    ], Path("/b"))
    actual = rules.find_rule(compiled, "https://h/v3/orders/get-orders", "GET", ("map_local",))
    assert actual["local_file"] == Path("/b/b.json")
    assert rules.find_rule(compiled, "https://h/v3/other", "GET", ("map_local",)) is None


def test_rewrite_changes_status_and_replaces_every_occurrence():
    rewrite = rules.compile_rule(0, {"url_regex": "x", "action": "rewrite", "rewrite": {
        "status_code": 401, "body_replace": [{"find": "ACTIVE", "replace": "CLOSED"}]}}, Path("/"))["rewrite"]
    status, body = rules.rewrite_status_and_body(rewrite, 200, b'{"a":"ACTIVE","b":"ACTIVE"}')
    assert status == 401
    assert body == b'{"a":"CLOSED","b":"CLOSED"}'


def test_rewrite_without_status_keeps_the_real_one():
    rewrite = rules.compile_rule(0, {"url_regex": "x", "action": "rewrite", "rewrite": {}}, Path("/"))["rewrite"]
    assert rules.rewrite_status_and_body(rewrite, 503, b"body") == (503, b"body")


def test_rules_are_saved_atomically_and_read_back():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        rules.save_raw(path, [{"url_regex": "é"}])
        assert rules.read_raw(path) == [{"url_regex": "é"}]
        assert [p.name for p in Path(tmp).iterdir()] == ["rules.json"]


# ---------- addon ----------

def _addon(tmp, raw_rules):
    tmp = Path(tmp)
    (tmp / "rules.json").write_text(json.dumps(raw_rules))
    os.environ.update({
        "TAMPER_MAPPINGS_FILE": str(tmp / "rules.json"),
        "TAMPER_SERVED_LOG": str(tmp / "served.jsonl"),
        "TAMPER_UNMATCHED_LOG": str(tmp / "unmatched.jsonl"),
        "TAMPER_BASE_DIR": str(tmp),
    })
    os.environ.pop("TAMPER_PANEL_PORT", None)
    import tamper_addon
    return tamper_addon.TamperAddon()


def _flow(url, method="GET"):
    flow = tflow.tflow()
    flow.request = tutils.treq(method=method.encode())
    flow.request.url = url
    return flow


def _records(path):
    text = Path(path).read_text() if Path(path).exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _records_without_time(path):
    return [{k: v for k, v in record.items() if k != "ts"} for record in _records(path)]


def test_map_local_serves_the_file_with_its_status_and_logs_it():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "mock.json").write_text('{"code": 500}')
        addon = _addon(tmp, [{"url_regex": "get-orders", "local_file": "mock.json", "status_code": 500}])
        flow = _flow("https://api.example/v3/orders/get-orders?token=SECRET")

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        assert flow.response.status_code == 500
        assert flow.response.content == b'{"code": 500}'
        served = _records(Path(tmp) / "served.jsonl")
        assert served[0]["url"] == "https://api.example/v3/orders/get-orders"
        assert served[0]["served"] is True
        assert addon.recent[-1]["action"] == "map_local"


def test_missing_mock_file_answers_502_instead_of_reaching_the_backend():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "x", "local_file": "missing.json"}])
        flow = _flow("https://api.example/x")

        asyncio.run(addon.request(flow))

        assert flow.response.status_code == 502
        assert _records(Path(tmp) / "served.jsonl")[0]["served"] is False


def test_rewrite_changes_the_real_response_status_headers_and_body():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "get-user", "action": "rewrite", "rewrite": {
            "status_code": 403, "headers": {"X-Added": "yes", "X-Remove": None},
            "body_replace": [{"find": "ACTIVE", "replace": "CLOSED"}]}}])
        flow = _flow("https://api.example/v3/get-user")
        flow.response = tutils.tresp(status_code=200, content=b'{"s":"ACTIVE"}')
        flow.response.headers["X-Remove"] = "1"

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        assert flow.response.status_code == 403
        assert flow.response.content == b'{"s":"CLOSED"}'
        assert flow.response.headers["X-Added"] == "yes"
        assert "X-Remove" not in flow.response.headers
        assert addon.recent[-1]["action"] == "rewrite"


def test_rewrite_sets_and_removes_request_headers_before_the_call_leaves():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "/api/profile", "action": "rewrite", "rewrite": {
            "request_headers": {"Authorization": "Bearer expired", "X-Debug": None}}}])
        flow = _flow("https://api.example/api/profile")
        flow.request.headers["Authorization"] = "Bearer good"
        flow.request.headers["X-Debug"] = "1"

        asyncio.run(addon.request(flow))

        assert flow.request.headers["Authorization"] == "Bearer expired"
        assert "X-Debug" not in flow.request.headers
        assert flow.metadata["tamper_action"] == "rewrite"


def test_rewrite_header_changes_reject_line_breaks():
    rule = {"url_regex": "x", "action": "rewrite", "rewrite": {"request_headers": {"X-A": "ok\r\nX-Evil: 1"}}}
    _expect_error(lambda: rules.compile_rule(0, rule, Path("/")), "line break")


def test_a_mocked_response_is_never_rewritten():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "m.json").write_text("ACTIVE")
        addon = _addon(tmp, [
            {"url_regex": "x", "local_file": "m.json"},
            {"url_regex": "x", "action": "rewrite", "rewrite": {"body_replace": [{"find": "ACTIVE", "replace": "NO"}]}},
        ])
        flow = _flow("https://api.example/x")

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        assert flow.response.content == b"ACTIVE"


def test_delay_rule_waits_and_lets_the_real_response_through():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "slow", "action": "delay", "delay_ms": 60}])
        flow = _flow("https://api.example/slow")
        flow.response = None
        started = time.monotonic()

        asyncio.run(addon.request(flow))

        assert time.monotonic() - started >= 0.06
        assert flow.response is None
        assert flow.metadata["tamper_action"] == "delay"
        flow.response = tutils.tresp()
        asyncio.run(addon.response(flow))
        assert _records_without_time(Path(tmp) / "served.jsonl") == [{"url": "https://api.example/slow", "method": "GET",
                                                         "matched_regex": "slow", "action": "delay",
                                                         "status": 200, "served": True}]


def test_unmatched_requests_log_query_names_but_not_values():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        flow = _flow("https://api.example/get-orders?user=123&token=SECRET")
        flow.response = tutils.tresp()

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        actual = _records(Path(tmp) / "unmatched.jsonl")[0]
        assert actual["query_keys"] == ["token", "user"]
        assert "SECRET" not in json.dumps(actual)


def test_rules_file_edits_apply_without_restart_and_broken_edits_keep_the_old_rules():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "m.json").write_text("{}")
        addon = _addon(tmp, [])
        rules_file = Path(tmp) / "rules.json"
        rules_file.write_text(json.dumps([{"url_regex": "x", "local_file": "m.json"}]))
        os.utime(rules_file, ns=(time.time_ns(), time.time_ns() + 1_000_000))

        asyncio.run(addon.request(_flow("https://api.example/x")))
        assert len(addon.rules) == 1

        rules_file.write_text("[{broken")
        os.utime(rules_file, ns=(time.time_ns(), time.time_ns() + 2_000_000))
        asyncio.run(addon.request(_flow("https://api.example/x")))
        assert len(addon.rules) == 1


# ---------- settings ----------

def test_settings_fill_defaults_and_reject_bad_values():
    assert settings.validate({}) == settings.DEFAULTS
    actual = settings.validate({"ssl": {"mode": "except", "hosts": [" API.Example.com ", "api.example.com"]}})
    assert actual["ssl"] == {"mode": "except", "hosts": ["api.example.com"]}
    _expect_error(lambda: settings.validate({"ssl": {"mode": "maybe"}}), "ssl.mode")
    _expect_error(lambda: settings.validate({"block": {"hosts": ["http://x.com/path"]}}), "not a host pattern")
    _expect_error(lambda: settings.validate({"latency_ms": -1}), "latency_ms")
    _expect_error(lambda: settings.validate({"app_timeout_s": "soon"}), "whole number")


def test_host_patterns_match_like_charles():
    assert settings.host_matches("api.example.com", "api.example.com:443")
    assert not settings.host_matches("api.example.com", "cdn.example.com")
    assert settings.host_matches("*.example.com", "a.b.example.com")
    assert not settings.host_matches("*.example.com", "example.com")
    assert not settings.host_matches("*.example.com", "evilexample.com")
    assert settings.host_matches("*", "anything.test")


def test_ssl_mode_becomes_mitmproxy_ignore_or_allow_hosts():
    only = settings.validate({"ssl": {"mode": "only", "hosts": ["api.example.com"]}})
    ignore, allow = settings.mitm_host_options(only)
    assert ignore == [] and len(allow) == 1
    assert __import__("re").match(allow[0], "api.example.com:443")
    skip = settings.validate({"ssl": {"mode": "except", "hosts": ["*.cdn.test"]}})
    assert settings.mitm_host_options(skip)[1] == [] and len(settings.mitm_host_options(skip)[0]) == 1
    assert settings.mitm_host_options(settings.validate({"ssl": {"mode": "only", "hosts": []}})) == ([], [])


def test_settings_are_saved_and_loaded_back():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        assert settings.load(path) == settings.DEFAULTS
        settings.save(path, settings.validate({"no_cache": True, "latency_ms": 250}))
        assert settings.load(path)["latency_ms"] == 250 and settings.load(path)["no_cache"] is True


def test_blocked_hosts_get_a_403_without_reaching_the_server():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        addon.settings = settings.validate({"block": {"enabled": True, "hosts": ["*.tracker.test"]}})
        flow = _flow("https://px.tracker.test/collect")

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        assert flow.response.status_code == 403
        assert addon.recent[-1]["action"] == "blocked"
        served = _records_without_time(Path(tmp) / "served.jsonl")
        assert served == [{"url": "https://px.tracker.test/collect", "method": "GET", "matched_regex": None,
                           "action": "blocked", "status": 403, "served": True}]


def test_no_caching_strips_cache_headers_both_ways():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        addon.settings = settings.validate({"no_cache": True})
        flow = _flow("https://api.example/items")
        flow.request.headers["If-None-Match"] = '"abc"'
        flow.response = tutils.tresp()
        flow.response.headers["ETag"] = '"abc"'

        asyncio.run(addon.request(flow))
        asyncio.run(addon.response(flow))

        assert "If-None-Match" not in flow.request.headers
        assert "ETag" not in flow.response.headers
        assert flow.response.headers["Cache-Control"] == "no-store"


def test_global_latency_delays_every_call():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        addon.settings = settings.validate({"latency_ms": 60})
        started = time.monotonic()

        asyncio.run(addon.request(_flow("https://api.example/anything")))

        assert time.monotonic() - started >= 0.06


def test_map_remote_sends_the_call_to_the_target_host_and_keeps_path_and_query():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "/api/payments", "action": "map_remote", "target": "http://staging.example:8081"}])
        flow = _flow("https://api.example/api/payments/42?currency=eur")

        asyncio.run(addon.request(flow))

        assert flow.request.url == "http://staging.example:8081/api/payments/42?currency=eur"
        assert flow.request.headers.get("Host", "staging.example:8081").startswith("staging.example")
        assert flow.metadata["tamper_action"] == "map_remote"
        flow.response = tutils.tresp()
        asyncio.run(addon.response(flow))
        assert addon.recent[-1]["url"] == "https://api.example/api/payments/42"
        assert addon.recent[-1]["remote"] == "http://staging.example:8081"
        served = _records_without_time(Path(tmp) / "served.jsonl")
        assert served == [{"url": "https://api.example/api/payments/42", "method": "GET",
                           "matched_regex": "/api/payments", "action": "map_remote", "status": 200,
                           "served": True, "target": "http://staging.example:8081"}]


def test_map_remote_with_a_target_path_replaces_the_path():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "/v1/user", "action": "map_remote", "target": "https://mock.example/v2/user"}])
        flow = _flow("https://api.example/v1/user?id=7")

        asyncio.run(addon.request(flow))

        assert flow.request.url == "https://mock.example/v2/user?id=7"


def test_map_remote_needs_a_real_target():
    _expect_error(lambda: rules.compile_rule(0, {"url_regex": "x", "action": "map_remote", "target": "staging"}, Path("/")),
                  "map_remote needs a target")


def test_updated_settings_are_saved_and_used():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        actual = addon.update_settings({"focus": {"enabled": True, "hosts": ["api.example.com"]}})
        assert actual["focus"]["hosts"] == ["api.example.com"]
        assert json.loads((Path(tmp) / "settings.json").read_text())["focus"]["enabled"] is True
        _expect_error(lambda: addon.update_settings({"ssl": {"mode": "nope"}}), "ssl.mode")


def test_settings_file_edited_on_disk_applies_on_the_next_call():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        path = Path(tmp) / "settings.json"
        path.write_text(json.dumps({"block": {"enabled": True, "hosts": ["px.tracker.test"]}}))
        flow = _flow("https://px.tracker.test/collect")

        asyncio.run(addon.request(flow))

        assert flow.response.status_code == 403


def test_broken_settings_file_keeps_the_previous_settings():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [])
        addon.update_settings({"latency_ms": 250})
        (Path(tmp) / "settings.json").write_text('{"latency_ms": ')

        addon._reload_if_changed()

        assert addon.settings["latency_ms"] == 250


# ---------- breakpoints ----------

def test_breakpoint_rules_validate_the_pause_phase():
    _expect_error(lambda: rules.compile_rule(0, {"url_regex": "x", "action": "breakpoint", "pause_on": "later"},
                                             Path("/")), "pause_on")
    compiled = rules.compile_rules([{"url_regex": "x", "action": "breakpoint", "pause_on": "request"}], Path("/"))
    assert rules.find_rule(compiled, "https://h/x", "GET", ("breakpoint",), phase="request") is not None
    assert rules.find_rule(compiled, "https://h/x", "GET", ("breakpoint",), phase="response") is None


async def _paused_then(addon, hook, flow, decision):
    """Run the hook until it pauses, take the panel's decision, let it finish. Returns the pause."""
    task = asyncio.create_task(hook(flow))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if addon.breakpoints.list():
            break
    paused = addon.breakpoints.list()[0]
    assert addon.breakpoints.decide(paused["id"], decision)
    await task
    return paused


def test_response_breakpoint_applies_the_edited_status_headers_and_body():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "get-orders", "action": "breakpoint", "pause_on": "response"}])
        flow = _flow("https://api.example/v3/get-orders")
        flow.response = tutils.tresp(status_code=200, content=b'{"statut":"ACTIVE"}')
        edit = {"status": 503, "headers": [["Content-Type", "application/json"], ["X-Edited", "yes"]],
                "body": '{"statut":"CLOSED"}'}

        paused = asyncio.run(_paused_then(addon, addon.response, flow, {"action": "continue", "edit": edit}))

        assert paused["phase"] == "response" and paused["body"] == '{"statut":"ACTIVE"}'
        assert flow.response.status_code == 503 and flow.response.reason == "Service Unavailable"
        assert flow.response.content == b'{"statut":"CLOSED"}'
        assert flow.response.headers["X-Edited"] == "yes"
        assert flow.response.headers["content-length"] == str(len(b'{"statut":"CLOSED"}'))
        assert addon.recent[-1]["paused"] is True
        assert addon.breakpoints.list() == []


def test_request_breakpoint_abort_kills_the_call_and_still_lists_it():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        addon = _addon(tmp, [{"url_regex": "get-user", "action": "breakpoint", "pause_on": "request"}])
        flow = _flow("https://api.example/v3/get-user")
        flow.response = None

        asyncio.run(_paused_then(addon, addon.request, flow, {"action": "abort"}))

        assert flow.error is not None and not flow.killable
        assert addon.recent[-1]["aborted"] is True


def test_request_breakpoint_can_answer_with_a_ready_response_instead_of_the_backend():
    if not HAS_MITM:
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "401.json").write_text('{"code": 401}')
        addon = _addon(tmp, [{"url_regex": "get-user", "action": "breakpoint", "pause_on": "both"}])
        flow = _flow("https://api.example/v3/get-user")
        flow.response = None

        asyncio.run(_paused_then(addon, addon.request, flow,
                                 {"action": "respond", "local_file": "401.json", "status": 401}))

        assert flow.response.status_code == 401
        assert flow.response.content == b'{"code": 401}'


def test_deciding_an_unknown_pause_is_refused():
    from breakpoints import Breakpoints
    registry = Breakpoints()
    assert registry.decide("nope", {"action": "continue"}) is False
    _expect_error(lambda: registry.decide("nope", {"action": "explode"}), "action must be")


# ---------- panel ----------

def test_endpoints_file_is_parsed_with_groups_and_base_keys():
    text = "# header\n\n# OrdersApi · API\nGET    /v3.6/orders/get-orders\nPOST   /v3/x/{id}/y\nnot a line\n"
    actual = panel_data.parse_endpoints(text)
    assert actual == [
        {"method": "GET", "path": "/v3.6/orders/get-orders", "group": "OrdersApi", "base": "API"},
        {"method": "POST", "path": "/v3/x/{id}/y", "group": "OrdersApi", "base": "API"},
    ]


def test_base_urls_file_is_parsed_per_environment():
    text = "# API: OrdersApi\nAPI   dev   https://api-dev.example/\nAPI   prod  https://api.example/\n"
    assert panel_data.parse_bases(text) == {"API": {"dev": "https://api-dev.example/", "prod": "https://api.example/"}}


def test_example_profile_endpoint_groups_all_have_a_base_url():
    loaded = panel_data.load_endpoints()
    assert len(loaded["endpoints"]) >= 10
    missing = {e["base"] for e in loaded["endpoints"]} - set(loaded["bases"])
    assert not missing, missing
    assert all(urls for urls in loaded["bases"].values())


def test_breakpoint_decisions_are_validated():
    base = panel_data.TOOLKIT_DIR
    assert panel_data.clean_decision({"action": "abort", "extra": 1}, base) == {"action": "abort"}
    actual = panel_data.clean_decision({"action": "continue", "edit": {"status": "503", "headers": [["A", "b"]]}}, base)
    assert actual == {"action": "continue", "edit": {"status": 503, "headers": [["A", "b"]]}}
    respond = panel_data.clean_decision({"action": "respond", "local_file": "responses/401_unauthorized.json",
                                         "status": 401}, base)
    assert respond["local_file"].endswith("401_unauthorized.json")
    _expect_error(lambda: panel_data.clean_decision({"action": "continue", "edit": {"headers": [["X", "a\r\nEvil: 1"]]}}, base),
                  "line breaks")
    _expect_error(lambda: panel_data.clean_decision({"action": "respond", "local_file": "/etc/hosts"}, base), "tamper folders")
    _expect_error(lambda: panel_data.clean_decision({"action": "fly"}, base), "action must be")


def test_pasted_mocks_never_overwrite_and_get_a_safe_name():
    with tempfile.TemporaryDirectory() as tmp:
        original = panel_data.MOCKS_DIR
        panel_data.MOCKS_DIR = Path(tmp)
        try:
            first = panel_data.unique_mock_path("../../etc/get orders.json")
            first.write_text("{}")
            second = panel_data.unique_mock_path("../../etc/get orders.json")
        finally:
            panel_data.MOCKS_DIR = original
        assert first == Path(tmp) / "get_orders.json"
        assert second == Path(tmp) / "get_orders_2.json"


def test_files_outside_the_tamper_folders_are_not_readable():
    assert panel_data.inside_roots(panel_data.RESPONSES_DIR / "500_server_error.json")
    assert not panel_data.inside_roots(panel_data.RESPONSES_DIR / ".." / "lib" / "panel.py")
    assert not panel_data.inside_roots("/etc/hosts")


class _FakeAddon:
    def __init__(self, tmp, base_dir=None):
        # The rules file always lives in the temp folder: never overwrite a real rules.json.
        self.rules_file = Path(tmp) / "rules.json"
        self.rules_file.write_text("[]")
        self.base_dir = Path(base_dir or tmp)
        self.rules = []
        self.recent = []
        self.details = {}
        from breakpoints import Breakpoints
        self.breakpoints = Breakpoints()
        self.settings = settings.validate({})

    def update_settings(self, raw):
        self.settings = settings.validate(raw)
        return self.settings

    def current_settings(self):
        return self.settings

    def reload(self):
        self.rules = rules.compile_rules(rules.read_raw(self.rules_file), self.base_dir)
        return None


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _page_token(port):
    page = _call(port, "GET", "/")[1].decode()
    return page.split('const TOKEN = "')[1].split('"')[0]


def test_panel_api_needs_the_page_token_and_a_local_host():
    with tempfile.TemporaryDirectory() as tmp:
        port = _free_port()
        server = panel.start(_FakeAddon(tmp), port)
        try:
            token = _page_token(port)

            assert len(token) > 20
            assert _call(port, "GET", "/api/state")[0] == 403
            assert _call(port, "GET", "/api/state", headers={"X-Panel-Token": token, "Host": "evil.example"})[0] == 403
            assert _call(port, "GET", "/api/state", headers={"X-Panel-Token": token})[0] == 200
            for asset in ("/panel.js", "/panel.css", "/panel-rules.js", "/panel-traffic.js",
                          "/panel-endpoints.js", "/panel-pause.js", "/panel-settings.js"):
                assert _call(port, "GET", asset)[0] == 200, asset
            assert _call(port, "POST", "/api/paused/abcdef1234", {"action": "continue"},
                         {"X-Panel-Token": token, "Content-Type": "application/json"})[0] == 404
            auth = {"X-Panel-Token": token, "Content-Type": "application/json"}
            assert _call(port, "POST", "/api/settings", {"latency_ms": 100}, auth)[0] == 200
            assert json.loads(_call(port, "GET", "/api/settings", headers=auth)[1])["latency_ms"] == 100
            assert _call(port, "POST", "/api/settings", {"ssl": {"mode": "bad"}}, auth)[0] == 400
            assert _call(port, "GET", "/panel.py")[0] == 404
        finally:
            server.shutdown()


def test_panel_saves_valid_rules_with_ids_and_rejects_broken_ones():
    with tempfile.TemporaryDirectory() as tmp:
        port = _free_port()
        addon = _FakeAddon(tmp)
        server = panel.start(addon, port)
        try:
            auth = {"X-Panel-Token": _page_token(port), "Content-Type": "application/json"}

            status, _ = _call(port, "POST", "/api/rules", [{"url_regex": "x", "action": "delay"}], auth)
            saved = json.loads(addon.rules_file.read_text())
            assert status == 200
            assert saved[0]["id"] and saved[0]["action"] == "delay"

            status, body = _call(port, "POST", "/api/rules", [{"url_regex": "(["}], auth)
            assert status == 400 and b"invalid url_regex" in body
            assert json.loads(addon.rules_file.read_text()) == saved
        finally:
            server.shutdown()


def test_panel_previews_a_relative_rule_file_from_the_rules_folder():
    with tempfile.TemporaryDirectory() as tmp:
        port = _free_port()
        addon = _FakeAddon(tmp, base_dir=panel_data.TOOLKIT_DIR)
        server = panel.start(addon, port)
        try:
            auth = {"X-Panel-Token": _page_token(port)}
            status, body = _call(port, "GET", "/api/file?path=responses/401_unauthorized.json", headers=auth)
            assert status == 200
            assert b"Unauthorized" in body
        finally:
            server.shutdown()


# ---------- runner ----------

def test_traffic_check_needs_each_expected_rule_served_with_an_accepted_status():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "served.jsonl"
        log.write_text("\n".join([
            json.dumps({"matched_regex": "/api/profile", "served": True, "status": 200}),
            json.dumps({"matched_regex": "/api/other", "served": True, "status": 500}),
        ]))
        assert harness._evaluate_traffic(log, {"expect_served": ["/api/profile"]})["pass"] is True
        assert harness._evaluate_traffic(log, {"expect_served": ["/api/other"]})["pass"] is False
        assert harness._evaluate_traffic(log, {"expect_served": ["/api/other"], "accept_status": [500]})["pass"] is True


def test_traffic_check_with_nothing_served_explains_interception():
    actual = harness._evaluate_traffic(Path("/nonexistent/served.jsonl"), {"expect_served": ["/x"]})
    assert actual["pass"] is False
    assert "intercepted nothing" in actual["hint"]


def test_unmatched_requests_are_listed_once_in_first_seen_order():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "unmatched.jsonl"
        log.write_text("\n".join(json.dumps(r) for r in [
            {"url": "https://h/auth/token", "method": "POST", "query_keys": []},
            {"url": "https://h/orders/get-orders", "method": "GET", "query_keys": ["orders_id"]},
            {"url": "https://h/auth/token", "method": "POST", "query_keys": []},
        ]))
        assert harness._summarize_unmatched(log) == [
            "POST https://h/auth/token",
            "GET https://h/orders/get-orders?orders_id",
        ]


def test_previous_run_outputs_are_cleared_before_a_new_run():
    with tempfile.TemporaryDirectory() as tmp:
        artifacts = Path(tmp)
        (artifacts / "steps").mkdir()
        for name in ["a.png", "screen_recording.mp4", "result.json", "steps/01_tap_FAIL.png",
                     "mappings.json", "served.jsonl", "unmatched.jsonl"]:
            (artifacts / name).write_bytes(b"old")

        harness._clear_previous_run_outputs(artifacts)

        remaining = sorted(str(p.relative_to(artifacts)) for p in artifacts.rglob("*") if p.is_file())
        assert remaining == ["mappings.json"], remaining


def test_device_serial_resolution_refuses_ambiguity():
    devices = adb.parse_adb_devices("List of devices attached\nemu-1\tdevice\nemu-2\tdevice\nZZ\toffline\n")
    assert devices == ["emu-1", "emu-2"]
    assert adb.resolve_serial(None, ["only"]) == "only"
    assert adb.resolve_serial("emu-2", devices) == "emu-2"
    _expect_error(lambda: adb.resolve_serial(None, devices), "multiple devices")
    _expect_error(lambda: adb.resolve_serial(None, []), "no Android device")
    _expect_error(lambda: adb.resolve_serial("nope", devices), "not attached")


class _FakeSelector:
    def __init__(self, present):
        self._present = present

    def exists(self, timeout=0):
        return self._present

    def wait(self, timeout=0):
        return self._present

    def click(self):
        pass


class _FakeDevice:
    def __init__(self, present_texts):
        self._present = set(present_texts)

    def __call__(self, **kwargs):
        target = kwargs.get("textContains") or kwargs.get("description") or kwargs.get("resourceId")
        return _FakeSelector(target in self._present)

    def app_start(self, pkg, stop=False):
        pass

    def screenshot(self, path):
        Path(path).write_bytes(b"")


def test_ui_check_text_present_and_absent():
    device = _FakeDevice({"Saved"})
    assert device_flow._evaluate_ui(device, {"type": "text_present", "text": "Saved"})[0] is True
    assert device_flow._evaluate_ui(device, {"type": "text_present", "text": "Error"})[0] is False
    assert device_flow._evaluate_ui(device, {"type": "text_absent", "text": "Error"})[0] is True


def test_a_failed_step_marks_the_flow_incomplete_and_optional_steps_are_skipped():
    device = _FakeDevice({"Saved"})
    original = device_flow._connect
    device_flow._connect = lambda serial: device
    try:
        with tempfile.TemporaryDirectory() as tmp:
            spec = {
                "device": {"app_package": "com.example.app"},
                "device_steps": [
                    {"action": "launch_app"},
                    {"action": "tap_text", "text": "Cookie popin", "timeout": 0, "optional": True},
                    {"action": "wait_text", "text": "Loading", "timeout": 0},
                ],
                "assertions": {"ui": {"type": "text_present", "text": "Saved", "timeout": 0}},
            }
            actual = device_flow.run_flow(spec, tmp)
    finally:
        device_flow._connect = original
    assert actual["flow_complete"] is False
    assert [s["skipped"] for s in actual["steps"]] == [False, True, False]


def test_scrub_removes_tokens_and_keeps_normal_text():
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "a.json"
        target.write_text(json.dumps({"authorization": "Bearer abc123DEF456ghijklmnop",
                                      "api_key": "sk-livesecret1234567890", "note": "keep me"}))
        files, redactions = scrub.scrub_dir(tmp)
        contents = target.read_text()
        assert files == 1 and redactions >= 2
        assert "livesecret" not in contents and "keep me" in contents


# ---------- profiles ----------

EXAMPLE_PROFILE = Path(__file__).resolve().parent.parent / "profiles" / "example"


def test_relative_files_come_from_the_profile_first_then_the_built_in_responses():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "mocks").mkdir()
        (Path(tmp) / "mocks" / "orders.json").write_text("[]")

        own = rules.resolve_local(tmp, "mocks/orders.json")
        built_in = rules.resolve_local(tmp, "responses/500_server_error.json")
        missing = rules.resolve_local(tmp, "mocks/nope.json")

        assert own == Path(tmp) / "mocks" / "orders.json"
        assert built_in == rules.TOOLKIT_DIR / "responses" / "500_server_error.json"
        assert missing == Path(tmp) / "mocks" / "nope.json"


def test_saved_mocks_go_to_the_folder_named_by_tamper_mocks():
    import importlib
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TAMPER_MOCKS"] = tmp
        try:
            importlib.reload(panel_data)
            actual = panel_data.unique_mock_path("orders.json")
        finally:
            os.environ.pop("TAMPER_MOCKS")
            importlib.reload(panel_data)

        assert actual == Path(tmp) / "orders.json"


def test_example_profile_samples_point_at_files_that_exist():
    sample_rules = rules.compile_rules(rules.read_raw(EXAMPLE_PROFILE / "rules.sample.json"), EXAMPLE_PROFILE)
    spec = json.loads((EXAMPLE_PROFILE / "spec.sample.json").read_text())

    for rule in sample_rules:
        if rule["action"] == "map_local":
            assert rule["local_file"].is_file(), rule["local_file"]
    for mapping in spec["mappings"]:
        assert rules.resolve_local(EXAMPLE_PROFILE, mapping["local_file"]).is_file(), mapping["local_file"]
    assert panel_data.parse_endpoints((EXAMPLE_PROFILE / "endpoints.txt").read_text())
    assert panel_data.parse_bases((EXAMPLE_PROFILE / "base-urls.txt").read_text())


def _run_all():
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed" + ("" if HAS_MITM else " (addon tests skipped: no mitmproxy)"))
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
