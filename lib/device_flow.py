"""Drive an Android device through a spec's device_steps and judge the UI assertion.

Used two ways:
  * imported by harness.py                 -> run_flow(spec, artifacts_dir)
  * standalone, to debug a flow (no proxy) -> python lib/device_flow.py --spec spec.json

Device interaction goes through uiautomator2 (the Python package, no Appium/Node).
This module only touches the UI, never the network.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from adb import adb_base

try:
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - dependency guard
    u2 = None

DEFAULT_STEP_TIMEOUT = 10.0
SETTLE_SECONDS = 1.0
SEGMENT_DEVICE_PATH = "/sdcard/tamper_segment.mp4"
SEGMENT_TIME_LIMIT_SECONDS = "180"
SCREENRECORD_BITRATE = "6000000"
RECORDER_FLUSH_SECONDS = 1.5
MANUAL_ROTATE_TIMEOUT = 120.0
PROGRAMMATIC_ROTATE_WAIT = 4.0

_LANDSCAPE = {"left", "right"}
_PORTRAIT = {"natural", "upsidedown"}
_ORIENTATION_ALIASES = {"portrait": "natural", "landscape": "left"}


class Session:
    """What a flow needs besides the device: target app, adb serial, open recording."""

    def __init__(self, device, package, serial):
        self.device = device
        self.package = package
        self.serial = serial
        self.recording = None


def _connect(serial):
    if u2 is None:
        raise RuntimeError("uiautomator2 is not installed. Run: bash setup.sh")
    return u2.connect(serial) if serial else u2.connect()


def _tap(device, selector, value, timeout):
    element = device(**{selector: value})
    if not element.exists(timeout=timeout):
        return False, f"element not found: {selector}={value!r}"
    element.click()
    return True, f"tapped {selector}={value!r}"


def _orientation_matches(device, target):
    """True if the device's live orientation is in the same family as `target`."""
    current = device.orientation
    return (current in _LANDSCAPE) if target in _LANDSCAPE else (current in _PORTRAIT)


def _wait_orientation(device, target, timeout, poll):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _orientation_matches(device, target):
            return True
        time.sleep(poll)
    return False


def _rotate(session, step):
    """mode=auto tries programmatic rotation first; SCREEN_ORIENTATION_SENSOR screens ignore
    it (they follow the accelerometer), so it falls back to asking for a physical rotation."""
    device = session.device
    wanted = step.get("orientation", "natural")
    target = _ORIENTATION_ALIASES.get(wanted, wanted)
    if step.get("mode", "auto") != "manual":
        try:
            device.set_orientation(target)
        except Exception:  # noqa: BLE001 - some devices reject programmatic rotation
            pass
        if _wait_orientation(device, target, PROGRAMMATIC_ROTATE_WAIT, 0.3):
            return True, f"rotated to {wanted} (programmatic)"
    manual_timeout = float(step.get("timeout") or MANUAL_ROTATE_TIMEOUT)
    sys.stderr.write(
        f"\n>>> MANUAL ACTION NEEDED: physically rotate the device to {wanted.upper()} "
        f"and hold it (waiting up to {int(manual_timeout)}s) <<<\n"
    )
    sys.stderr.flush()
    if _wait_orientation(device, target, manual_timeout, 0.5):
        return True, f"rotated to {wanted} (physical)"
    return False, f"device did not reach {wanted} within {int(manual_timeout)}s"


def _record_start(session, artifacts_dir, name):
    """Start a screen-recording segment; only one runs at a time, so a running one is closed first."""
    _record_stop(session, artifacts_dir)
    adb = adb_base(session.serial)
    subprocess.run(adb + ["shell", "rm", "-f", SEGMENT_DEVICE_PATH], check=False)
    proc = subprocess.Popen(
        adb + ["shell", "screenrecord", "--time-limit", SEGMENT_TIME_LIMIT_SECONDS,
               "--bit-rate", SCREENRECORD_BITRATE, SEGMENT_DEVICE_PATH],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    session.recording = (proc, name)
    return True, f"recording started: {name}"


def _record_stop(session, artifacts_dir):
    """Stop the running segment and pull it as screen_recording_<name>.mp4. No-op when none runs."""
    if session.recording is None:
        return True, "no recording running"
    proc, name = session.recording
    session.recording = None
    adb = adb_base(session.serial)
    subprocess.run(adb + ["shell", "pkill", "-INT", "screenrecord"], check=False)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(RECORDER_FLUSH_SECONDS)  # screenrecord needs a moment to finalize the mp4
    local = Path(artifacts_dir) / f"screen_recording_{name}.mp4"
    subprocess.run(adb + ["pull", SEGMENT_DEVICE_PATH, str(local)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(adb + ["shell", "rm", "-f", SEGMENT_DEVICE_PATH], check=False)
    if not local.exists():
        return False, f"recording {name} could not be pulled"
    return True, f"recording saved: {local.name}"


def _input_text(device, step):
    # `env` reads the value from an environment variable (e.g. a login password), so
    # secrets never live in the spec or the logs; only the length is reported.
    text = os.environ[step["env"]] if step.get("env") else step["text"]
    device.send_keys(text, clear=False)
    return True, f"typed {len(text)} chars"


def _run_step(session, step, artifacts_dir, index):
    """Execute a single device step. Returns (ok, detail)."""
    device = session.device
    action = step["action"]
    timeout = float(step.get("timeout", DEFAULT_STEP_TIMEOUT))

    if action == "launch_app":
        device.app_start(session.package, stop=True)
        return True, "app launched"
    if action == "stop_app":
        device.app_stop(session.package)
        return True, "app stopped"
    if action == "wait":
        seconds = float(step.get("seconds", SETTLE_SECONDS))
        time.sleep(seconds)
        return True, f"waited {seconds}s"
    if action == "wait_text":
        ok = device(textContains=step["text"]).wait(timeout=timeout)
        return bool(ok), f"wait_text {step['text']!r} -> {'present' if ok else 'absent'}"
    if action == "tap_text":
        return _tap(device, "textContains", step["text"], timeout)
    if action == "tap_desc":
        return _tap(device, "description", step["desc"], timeout)
    if action == "tap_id":
        return _tap(device, "resourceId", step["resource_id"], timeout)
    if action == "tap_xy":
        device.click(step["x"], step["y"])
        return True, f"tapped ({step['x']},{step['y']})"
    if action == "input_text":
        return _input_text(device, step)
    if action == "clear_text":
        device.clear_text()
        return True, "cleared field"
    if action == "press":
        device.press(step["key"])
        return True, f"pressed {step['key']}"
    if action == "swipe":
        device.swipe_ext(step["direction"])
        return True, f"swiped {step['direction']}"
    if action == "rotate":
        return _rotate(session, step)
    if action == "wait_orientation":
        wanted = step.get("orientation", "portrait")
        target = _ORIENTATION_ALIASES.get(wanted, wanted)
        ok = _wait_orientation(device, target, timeout, 0.3)
        return ok, f"orientation {wanted}: {'reached' if ok else 'not reached'} (last={device.orientation})"
    if action == "unfreeze_rotation":
        device.freeze_rotation(False)
        return True, "rotation unfrozen"
    if action == "record_start":
        return _record_start(session, artifacts_dir, step.get("name", f"segment_{index}"))
    if action == "record_stop":
        return _record_stop(session, artifacts_dir)
    if action == "screenshot":
        target = Path(artifacts_dir) / f"{step.get('name', f'step_{index}')}.png"
        device.screenshot(str(target))
        return True, f"screenshot {target.name}"

    return False, f"unknown action: {action}"


def _evaluate_ui(device, ui):
    """Return (passed, detail) for the UI assertion block."""
    timeout = float(ui.get("timeout", DEFAULT_STEP_TIMEOUT))
    kind = ui["type"]

    if kind in ("text_present", "text_absent"):
        found = device(textContains=ui["text"]).exists(timeout=timeout)
        want = kind == "text_present"
        return (found == want), f"{kind} {ui['text']!r}: found={found}"

    if kind in ("element_exists", "element_absent"):
        selector = {}
        if ui.get("resource_id"):
            selector["resourceId"] = ui["resource_id"]
        if ui.get("desc"):
            selector["description"] = ui["desc"]
        if ui.get("text"):
            selector["textContains"] = ui["text"]
        if not selector:
            return False, "element assertion has no selector (resource_id/desc/text)"
        found = device(**selector).exists(timeout=timeout)
        want = kind == "element_exists"
        return (found == want), f"{kind} {selector}: found={found}"

    return False, f"unknown ui assertion type: {kind}"


def _step_screenshot(device, steps_dir, index, step, ok, is_skipped):
    suffix = "" if ok else ("_SKIPPED" if is_skipped else "_FAIL")
    shot = steps_dir / f"{index:02d}_{step['action']}{suffix}.png"
    try:
        device.screenshot(str(shot))
        return str(shot)
    except Exception:  # noqa: BLE001 - a missing per-step shot must not fail the run
        return None


def run_flow(spec, artifacts_dir):
    """Run the device flow and judge the UI assertion. Returns a result dict."""
    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    serial = spec["device"].get("serial")
    session = Session(_connect(serial), spec["device"]["app_package"], serial)

    # A screenshot after every step (steps/) makes a failed run easy to diagnose.
    # Disable with TAMPER_SHOT_EVERY_STEP=0.
    capture_each = os.environ.get("TAMPER_SHOT_EVERY_STEP", "1") != "0"
    steps_dir = artifacts_dir / "steps"
    if capture_each:
        steps_dir.mkdir(parents=True, exist_ok=True)

    steps_log = []
    flow_complete = True
    for index, step in enumerate(spec.get("device_steps", [])):
        try:
            ok, detail = _run_step(session, step, artifacts_dir, index)
        except Exception as exc:  # noqa: BLE001 - record, don't crash the whole run
            ok, detail = False, f"error: {exc}"
        # An optional step guards a screen that only shows up sometimes (onboarding,
        # cookie pop-in, biometrics sheet): when it can't run, the flow goes on.
        is_skipped = not ok and bool(step.get("optional"))
        entry = {"action": step["action"], "ok": ok, "detail": detail, "skipped": is_skipped}
        if capture_each:
            shot = _step_screenshot(session.device, steps_dir, index, step, ok, is_skipped)
            if shot:
                entry = {**entry, "screenshot": shot}
        steps_log.append(entry)
        # Any failed step except a screenshot is a broken precondition: stop, so the UI
        # assertion isn't judged against the wrong screen.
        if not ok and not is_skipped and step["action"] != "screenshot":
            flow_complete = False
            break

    ui_pass, ui_detail = _evaluate_ui(session.device, spec["assertions"]["ui"])
    # A segment left open (no record_stop, or the flow aborted) still ends up in artifacts.
    _record_stop(session, artifacts_dir)

    final_shot = artifacts_dir / "final.png"
    session.device.screenshot(str(final_shot))

    summary = f"UI {'PASS' if ui_pass else 'FAIL'} - {ui_detail}"
    if not flow_complete:
        summary += " (flow incomplete: a prerequisite step failed)"

    return {
        "screenshot": str(final_shot),
        "ui": {"pass": ui_pass, "type": spec["assertions"]["ui"]["type"], "detail": ui_detail},
        "steps": steps_log,
        "flow_complete": flow_complete,
        "summary": summary,
    }


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Run a spec's device steps only (no proxy).")
    parser.add_argument("--spec", required=True, help="Path to the spec JSON")
    parser.add_argument("--artifacts", default=None, help="Artifacts output dir")
    args = parser.parse_args(argv)

    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    artifacts = Path(args.artifacts or spec.get("artifacts_dir") or f"artifacts/{spec['name']}")
    if not artifacts.is_absolute():
        artifacts = spec_path.parent / artifacts
    result = run_flow(spec, artifacts)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if (result["ui"]["pass"] and result.get("flow_complete", True)) else 1


if __name__ == "__main__":
    sys.exit(_main())
