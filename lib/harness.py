"""Tamper + device test runner.

Given a spec JSON it:
  1. starts mitmdump with the tamper addon (serving the spec's mocks),
  2. routes the Android device's HTTP(S) traffic through that proxy,
  3. drives the device steps and judges the on-screen (UI) assertion,
  4. judges the network (traffic) assertion from what the addon actually served,
  5. tears everything down, scrubs the artifacts and writes result.json (also to stdout).

    bash run.sh --spec path/to/spec.json [--artifacts DIR]

Exit code is 0 only when BOTH assertions pass and the flow completed.

Environment:
    TAMPER_PROXY_HOST       IP the device uses to reach this machine (default: auto LAN IP;
                            10.0.2.2 for an emulator; set it when on VPN).
    TAMPER_SKIP_PROXY_SET   1 = the device proxy is set by hand in Wi-Fi settings (devices
                            that deny `adb shell settings put global http_proxy`, e.g. MIUI).
    TAMPER_MITM_OPTS        extra mitmdump flags, e.g. "--set http2=false".
    TAMPER_SCREENRECORD     0 = no whole-run screen recording.
    TAMPER_SHOT_EVERY_STEP  0 = no per-step screenshots.
"""

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import device_flow
import scrub
from adb import adb_base, list_devices, resolve_serial

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_PROXY_PORT = 8080
DEFAULT_ACCEPT_STATUS = [200, 201, 202, 203, 204]
PROXY_BOOT_TIMEOUT = 15.0
PROXY_POLL_INTERVAL = 0.3
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LIB_DIR = Path(__file__).resolve().parent
ADDON = str(LIB_DIR / "tamper_addon.py")
SCHEMA = LIB_DIR / "spec.schema.json"
REQUIRED_KEYS = ("name", "device", "mappings", "device_steps", "assertions")
UNMATCHED_LOG_NAME = "unmatched.jsonl"
RUN_DEVICE_PATH = "/sdcard/tamper_run.mp4"
RUN_RECORD_TIME_LIMIT = "180"
RUN_RECORD_BITRATE = "6000000"
RECORDER_FLUSH_SECONDS = 1.5
RUN_OUTPUT_PATTERNS = (
    "*.png", "screen_recording*.mp4", "result.json", "steps/*.png", "served.jsonl", UNMATCHED_LOG_NAME,
)


def _validate(spec):
    """Required keys, regex compilability and (when jsonschema is installed) the full schema."""
    missing = [k for k in REQUIRED_KEYS if k not in spec]
    if missing:
        raise ValueError(f"spec missing required keys: {missing}")
    for index, mapping in enumerate(spec.get("mappings", [])):
        try:
            re.compile(mapping["url_regex"])
        except re.error as exc:
            raise ValueError(f"mappings[{index}] has an invalid url_regex {mapping.get('url_regex')!r}: {exc}")
    try:
        import jsonschema  # noqa: WPS433 - optional dependency
    except ImportError:
        return
    jsonschema.validate(spec, json.loads(SCHEMA.read_text(encoding="utf-8")))


def _detect_device_host():
    """Host the device should use to reach this machine. Override with TAMPER_PROXY_HOST."""
    override = os.environ.get("TAMPER_PROXY_HOST")
    if override:
        return override
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it only picks the route
        return sock.getsockname()[0]
    finally:
        sock.close()


def _wait_for_port(probe_host, port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(PROXY_POLL_INTERVAL)
            if sock.connect_ex((probe_host, port)) == 0:
                return True
        time.sleep(PROXY_POLL_INTERVAL)
    return False


def _start_proxy(spec, artifacts_dir, spec_dir, listen_host, port, probe_host):
    mappings_file = artifacts_dir / "mappings.json"
    mappings_file.write_text(json.dumps(spec["mappings"], indent=2), encoding="utf-8")
    served_log = artifacts_dir / "served.jsonl"
    stderr_path = artifacts_dir / "mitmdump.stderr.log"

    env = {
        **os.environ,
        "TAMPER_MAPPINGS_FILE": str(mappings_file),
        "TAMPER_SERVED_LOG": str(served_log),
        "TAMPER_UNMATCHED_LOG": str(artifacts_dir / UNMATCHED_LOG_NAME),
        # local_file paths in the spec are relative to the spec's own folder.
        "TAMPER_BASE_DIR": str(spec_dir),
    }
    cmd = [
        "mitmdump", "-q",
        "-s", ADDON,
        "--listen-host", listen_host,
        "-p", str(port),
        "--set", "termlog_verbosity=warn",
        # Tolerate slightly-malformed upstream headers (e.g. load-balancer cookies with
        # trailing whitespace); strict HTTP/2 validation otherwise 502s real APIs.
        "--set", "validate_inbound_headers=false",
    ]
    extra_opts = os.environ.get("TAMPER_MITM_OPTS", "").strip()
    if extra_opts:
        cmd += shlex.split(extra_opts)
    # stderr -> file so a chatty mitmdump can never fill a pipe and deadlock the run.
    stderr_handle = open(stderr_path, "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=stderr_handle)
    if not _wait_for_port(probe_host, port, PROXY_BOOT_TIMEOUT):
        proc.terminate()
        stderr_handle.close()
        detail = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        raise RuntimeError(f"mitmdump did not start listening on {listen_host}:{port}. stderr:\n{detail}")
    return proc, stderr_handle, served_log


def _summarize_unmatched(unmatched_log):
    """Distinct `METHOD url?query_keys` lines of requests no rule matched, in first-seen order."""
    path = Path(unmatched_log)
    if not path.is_file():
        return []
    seen = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        keys = record.get("query_keys") or []
        entry = f"{record['method']} {record['url']}" + (f"?{'&'.join(keys)}" if keys else "")
        if entry not in seen:
            seen.append(entry)
    return seen


def _set_device_proxy(serial, device_host, port):
    target = f"{device_host}:{port}"
    if os.environ.get("TAMPER_SKIP_PROXY_SET"):
        sys.stderr.write(f"TAMPER_SKIP_PROXY_SET: expecting the device Wi-Fi proxy to be {target}.\n")
        return target
    subprocess.run(adb_base(serial) + ["shell", "settings", "put", "global", "http_proxy", target], check=True)
    return target


def clear_device_proxy(serial):
    base = adb_base(serial)
    subprocess.run(base + ["shell", "settings", "put", "global", "http_proxy", ":0"], check=False)
    subprocess.run(base + ["shell", "settings", "delete", "global", "http_proxy"], check=False)
    check = subprocess.run(base + ["shell", "settings", "get", "global", "http_proxy"],
                           capture_output=True, text=True, check=False)
    value = (check.stdout or "").strip()
    if value not in ("", ":0", "null"):
        sys.stderr.write(
            f"WARNING: device proxy may still be set to {value!r}. Clear it manually:\n"
            f"  adb shell settings put global http_proxy :0\n"
        )


def _evaluate_traffic(served_log, traffic):
    accept = set(traffic.get("accept_status", DEFAULT_ACCEPT_STATUS))
    records = []
    if Path(served_log).is_file():
        for line in Path(served_log).read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))

    detail = []
    all_ok = True
    for regex in traffic["expect_served"]:
        hits = [r for r in records if r.get("matched_regex") == regex and r.get("served") and r.get("status") in accept]
        ok = len(hits) > 0
        all_ok = all_ok and ok
        detail.append({"expect_served": regex, "served": ok, "hits": len(hits)})

    result = {"pass": all_ok, "detail": detail, "served_count": len(records)}
    if not all_ok and not records:
        result = {**result, "hint": (
            "served_count is 0: the proxy intercepted nothing. Check the device proxy, the mitmproxy "
            "CA trust and TAMPER_PROXY_HOST. This is an interception problem, not a failed fix."
        )}
    return result


def _clear_previous_run_outputs(artifacts_dir):
    """Drop the last run's screenshots, clips, result and logs so this run is judged on its own."""
    for pattern in RUN_OUTPUT_PATTERNS:
        for path in Path(artifacts_dir).glob(pattern):
            if path.is_file():
                path.unlink()


def _uses_segment_recording(spec):
    """True when the spec records only chosen screens (record_start steps) instead of the whole run."""
    return any(step.get("action") == "record_start" for step in spec.get("device_steps", []))


def _start_screenrecord(serial):
    if os.environ.get("TAMPER_SCREENRECORD") == "0":
        return None
    base = adb_base(serial)
    subprocess.run(base + ["shell", "pkill", "-INT", "screenrecord"], check=False)
    subprocess.run(base + ["shell", "rm", "-f", RUN_DEVICE_PATH], check=False)
    return subprocess.Popen(
        base + ["shell", "screenrecord", "--time-limit", RUN_RECORD_TIME_LIMIT,
                "--bit-rate", RUN_RECORD_BITRATE, RUN_DEVICE_PATH],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop_and_pull_screenrecord(serial, proc, artifacts_dir):
    if proc is None:
        return None
    base = adb_base(serial)
    subprocess.run(base + ["shell", "pkill", "-INT", "screenrecord"], check=False)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(RECORDER_FLUSH_SECONDS)  # screenrecord needs a moment to finalize the mp4
    local = Path(artifacts_dir) / "screen_recording.mp4"
    subprocess.run(base + ["pull", RUN_DEVICE_PATH, str(local)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(base + ["shell", "rm", "-f", RUN_DEVICE_PATH], check=False)
    return str(local) if local.exists() else None


def _stop_proxy(proc, stderr_handle):
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if stderr_handle is not None:
        stderr_handle.close()


def _build_result(spec, flow_result, traffic_result, video, artifacts_dir, proxy_info):
    flow_complete = flow_result.get("flow_complete", True)
    # 0 served + incomplete flow = the run never reached the network call (a nav/login step
    # failed), not an interception problem. Say so, so the right thing gets fixed.
    if not traffic_result["pass"] and traffic_result["served_count"] == 0 and not flow_complete:
        traffic_result = {**traffic_result, "hint": (
            "served_count is 0 because the flow stopped before the network call (a device step "
            "failed). Fix the failing step; this is NOT an interception/CA problem."
        )}
    overall = bool(flow_result["ui"]["pass"] and traffic_result["pass"] and flow_complete)
    return {
        "name": spec["name"],
        "pass": overall,
        "flow_complete": flow_complete,
        "ui": flow_result["ui"],
        "traffic": traffic_result,
        "screenshot": flow_result["screenshot"],
        "screen_recording": video or [str(p) for p in sorted(Path(artifacts_dir).glob("screen_recording_*.mp4"))],
        "steps": flow_result["steps"],
        "summary": (
            f"{'PASS' if overall else 'FAIL'} | {flow_result['summary']} | "
            f"traffic {'PASS' if traffic_result['pass'] else 'FAIL'}"
            f"{'' if flow_complete else ' | FLOW INCOMPLETE'}"
        ),
        "proxy": proxy_info,
        "unmatched_requests": _summarize_unmatched(Path(artifacts_dir) / UNMATCHED_LOG_NAME),
    }


def run(spec, artifacts_dir, spec_dir):
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    _clear_previous_run_outputs(artifacts_dir)

    proxy_cfg = spec.get("proxy", {})
    listen_host = proxy_cfg.get("host", DEFAULT_LISTEN_HOST)
    port = int(proxy_cfg.get("port", DEFAULT_PROXY_PORT))
    serial = resolve_serial(spec["device"].get("serial"), list_devices())
    # Pin the resolved serial so uiautomator2 targets the same device adb does.
    spec = {**spec, "device": {**spec["device"], "serial": serial}}
    device_host = _detect_device_host()
    if listen_host in LOOPBACK_HOSTS:
        sys.stderr.write(
            f"WARNING: proxy.host={listen_host} binds loopback only; a physical device cannot reach it.\n"
        )
    # Probe the interface the device will actually use (the LAN IP when bound to 0.0.0.0).
    probe_host = device_host if listen_host in ("0.0.0.0", "") else listen_host

    proc = stderr_handle = rec_proc = None
    proxy_set = False
    try:
        proc, stderr_handle, served_log = _start_proxy(spec, artifacts_dir, spec_dir, listen_host, port, probe_host)
        _set_device_proxy(serial, device_host, port)
        proxy_set = not os.environ.get("TAMPER_SKIP_PROXY_SET")
        if not _uses_segment_recording(spec):
            rec_proc = _start_screenrecord(serial)
        flow_result = device_flow.run_flow(spec, artifacts_dir)
        traffic_result = _evaluate_traffic(served_log, spec["assertions"]["traffic"])
    finally:
        # Stop + pull the recording first so it covers the whole flow, then always clean up.
        video = _stop_and_pull_screenrecord(serial, rec_proc, artifacts_dir)
        if proxy_set:
            clear_device_proxy(serial)
        _stop_proxy(proc, stderr_handle)

    proxy_info = {"listen_host": listen_host, "device_host": device_host, "port": port}
    result = _build_result(spec, flow_result, traffic_result, video, artifacts_dir, proxy_info)
    (artifacts_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    # Scrub every text artifact before anything gets shared.
    scrub.scrub_dir(artifacts_dir)
    return result


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Tamper + device test runner.")
    parser.add_argument("--spec", required=True, help="Path to the spec JSON")
    parser.add_argument("--artifacts", default=None, help="Artifacts output dir")
    args = parser.parse_args(argv)

    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    _validate(spec)
    artifacts = Path(args.artifacts or spec.get("artifacts_dir") or f"artifacts/{spec['name']}")
    # A relative artifacts dir is relative to the spec's folder, not the current directory.
    if not artifacts.is_absolute():
        artifacts = spec_path.parent / artifacts
    result = run(spec, artifacts, spec_path.parent)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(_main())
