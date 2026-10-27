"""Small adb helpers shared by the test runner and the device-step runner."""

import os
import shutil
import subprocess
from pathlib import Path


def adb_bin():
    """Resolve adb even when PATH is minimal (non-login shells). Override with $ADB."""
    explicit = os.environ.get("ADB")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("adb")
    if found:
        return found
    bases = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "Library/Android/sdk"),
        str(Path.home() / "Android/Sdk"),
        "/usr/local/share/android-sdk",
        "/opt/homebrew/share/android-commandlinetools",
    ]
    for base in bases:
        if base:
            candidate = Path(base) / "platform-tools" / "adb"
            if candidate.exists():
                return str(candidate)
    return "adb"  # last resort; surfaces a clear "adb not found" error


def adb_base(serial):
    return [adb_bin()] + (["-s", serial] if serial else [])


def parse_adb_devices(output):
    """Serials of devices in the 'device' state from `adb devices` output."""
    serials = []
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or "\t" not in stripped:
            continue
        serial, state = stripped.split("\t", 1)
        if state.strip() == "device":
            serials.append(serial.strip())
    return serials


def list_devices():
    result = subprocess.run([adb_bin(), "devices"], capture_output=True, text=True, check=False)
    return parse_adb_devices(result.stdout or "")


def resolve_serial(serial, devices):
    """Pick the device serial to drive, failing clearly on ambiguity."""
    if serial:
        if serial not in devices:
            raise RuntimeError(f"device serial {serial!r} is not attached. Attached: {devices or 'none'}")
        return serial
    if not devices:
        raise RuntimeError("no Android device attached (adb devices is empty).")
    if len(devices) > 1:
        raise RuntimeError(f"multiple devices attached: {devices}. Set device.serial in the spec.")
    return devices[0]
