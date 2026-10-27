#!/usr/bin/env bash
# Run a repeatable tamper test: mocks + device steps + checks, then always clean up.
#
#   ./run.sh --spec profiles/example/spec.sample.json [--artifacts DIR]
#
# Writes artifacts/<name>/result.json (+ screenshots, screen recording) and prints the
# result. Exit code 0 only when the UI check AND the "mock was served" check pass.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${TAMPER_VENV:-$HOME/.cache/tamper-proxy-venv}"
PY="${TAMPER_PYTHON:-$VENV/bin/python}"

if [ ! -x "$PY" ] || ! "$PY" -c 'import uiautomator2, mitmproxy' >/dev/null 2>&1; then
  echo "Python deps missing. Run once: bash setup.sh" >&2
  exit 1
fi
# mitmdump must come from the same venv the runner uses.
export PATH="$(dirname "$PY"):$PATH"
exec "$PY" "$HERE/lib/harness.py" "$@"
