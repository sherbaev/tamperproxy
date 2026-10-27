#!/usr/bin/env bash
# One-time setup: an isolated Python venv with mitmproxy + uiautomator2.
#
# Why a venv: Homebrew's python is "externally managed" (pip into it is blocked) and the
# newest CPython often lacks wheels for uiautomator2. proxy.sh and run.sh find this venv.
#
#   bash setup.sh             # auto-pick a python
#   bash setup.sh python3.12  # force one
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${TAMPER_VENV:-$HOME/.cache/tamper-proxy-venv}"

pick_python() {
  # Widest wheel coverage first; skip interpreters with a broken pyexpat (Homebrew issue).
  for c in python3.12 python3.11 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import pyexpat" >/dev/null 2>&1; then echo "$c"; return; fi
  done
  echo ""
}

PY="${1:-$(pick_python)}"
if [ -z "$PY" ]; then
  echo "No healthy python3 found. Try: brew install python@3.12" >&2
  exit 1
fi

if [ -d "$VENV" ] && [ -f "$VENV/pyvenv.cfg" ]; then rm -rf "$VENV"; fi
echo "Creating venv at $VENV with $PY ($("$PY" --version 2>&1))"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"

echo
echo "Done. Next:"
echo "  ./proxy.sh                                  # proxy + browser panel"
echo "  \"$VENV/bin/python\" -m uiautomator2 init   # once per device, only for run.sh specs"
