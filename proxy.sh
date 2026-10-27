#!/usr/bin/env bash
# Start the proxy with the tamper rules and open the Tamper panel in the browser.
#
#   ./proxy.sh [start] [--profile DIR] [--library DIR]... [--port 8080] [--panel-port 8082]
#                      [--rules FILE] [--adb [SERIAL]] [--system-proxy] [--no-open]
#                      [--inspector [--web-port 8081]] [--headless]
#   ./proxy.sh cert-emulator [-s SERIAL]   Android emulator: install the CA as a system certificate
#   ./proxy.sh cert-ios-sim                iOS Simulator: install the CA into the booted simulator
#   ./proxy.sh cert-phone                  steps to trust the CA on a real Android phone / iPhone
#   ./proxy.sh clear-adb-proxy [SERIAL]    remove a proxy left on an Android device by --adb
#
# --profile       one app's folder: endpoints.txt, base-urls.txt, mocks/ (+ rules.json, settings.json kept there)
# --library       folder with your tamper files, searchable from the panel (repeatable; or TAMPER_LIBRARY=a:b)
# --adb           Android: set the device proxy over adb and clear it on exit
# --system-proxy  macOS: point the Mac's HTTP/HTTPS proxy here (iOS Simulator, desktop apps); restored on exit
# --no-open       do not open the panel in the browser automatically
# --inspector     also start mitmproxy's own raw traffic UI (mitmweb); not needed for normal work
# --headless      no panel at all: rules.json and settings.json only (both reload on save)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${TAMPER_VENV:-$HOME/.cache/tamper-proxy-venv}"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"

bin() {
  if [ -x "$VENV/bin/$1" ]; then echo "$VENV/bin/$1"; elif command -v "$1" >/dev/null 2>&1; then command -v "$1"; fi
}

adb_bin() {
  if [ -n "${ADB:-}" ]; then echo "$ADB"; return; fi
  if command -v adb >/dev/null 2>&1; then command -v adb; return; fi
  for base in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/Library/Android/sdk" "$HOME/Android/Sdk"; do
    if [ -n "$base" ] && [ -x "$base/platform-tools/adb" ]; then echo "$base/platform-tools/adb"; return; fi
  done
  echo adb
}

lan_ip() {
  if [ -n "${TAMPER_PROXY_HOST:-}" ]; then echo "$TAMPER_PROXY_HOST"; return; fi
  if command -v ipconfig >/dev/null 2>&1; then
    for iface in en0 en1 en2; do
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      if [ -n "$ip" ]; then echo "$ip"; return; fi
    done
  fi
  hostname -I 2>/dev/null | awk '{print $1}'
}

cert_phone() {
  cat <<'EOF'
Trust the mitmproxy certificate on a real device (start ./proxy.sh first, set the device's Wi-Fi
proxy to Manual -> the address it prints, then):

  Android: open http://mitm.it -> Android -> install it (Settings -> Security -> Install a
           certificate -> CA certificate). Only apps whose network_security_config trusts user
           CAs (usually debug builds) accept it.
  iPhone:  open http://mitm.it in Safari -> Apple -> Allow; Settings -> General -> VPN & Device
           Management -> install; then Settings -> General -> About -> Certificate Trust Settings
           -> enable full trust for mitmproxy.

Apps with certificate pinning do not work through any proxy. When done, set the proxy back to None/Off.
EOF
}

need_ca() {
  if [ ! -f "$CA" ]; then
    echo "mitmproxy CA not found at $CA. Run ./proxy.sh once (it creates the CA), then retry." >&2
    exit 1
  fi
}

cert_ios_sim() {
  need_ca
  command -v xcrun >/dev/null 2>&1 || { echo "xcrun not found: install Xcode command line tools." >&2; exit 1; }
  xcrun simctl keychain booted add-root-cert "$CA"
  echo "Installed in the booted simulator. Start with ./proxy.sh --system-proxy and restart the app."
}

case "${1:-start}" in
  cert-emulator) shift; exec bash "$HERE/lib/install_cert_emulator.sh" "$@" ;;
  cert-ios-sim) cert_ios_sim; exit 0 ;;
  cert-phone) cert_phone; exit 0 ;;
  clear-adb-proxy)
    SERIAL_ARGS=(); if [ -n "${2:-}" ]; then SERIAL_ARGS=(-s "$2"); fi
    "$(adb_bin)" ${SERIAL_ARGS[@]+"${SERIAL_ARGS[@]}"} shell settings put global http_proxy :0
    echo "device proxy cleared"; exit 0 ;;
  -h|--help|help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
  start) shift ;;
  -*) ;;
  *) echo "unknown command: $1 (try ./proxy.sh help)" >&2; exit 2 ;;
esac

PORT=8080; PANEL_PORT=8082; WEB_PORT=8081; HEADLESS=0; USE_ADB=0; SERIAL=""; INSPECTOR=0; OPEN_BROWSER=1
SYSTEM_PROXY=0; PROFILE=""; RULES=""
LIBRARY="${TAMPER_LIBRARY:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    --rules) RULES="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --profile) PROFILE="$(cd "$2" && pwd)"; shift 2 ;;
    --library) LIBRARY="${LIBRARY:+$LIBRARY:}$(cd "$2" && pwd)"; shift 2 ;;
    --headless) HEADLESS=1; shift ;;
    --inspector) INSPECTOR=1; shift ;;
    --no-open) OPEN_BROWSER=0; shift ;;
    --system-proxy) SYSTEM_PROXY=1; shift ;;
    --adb) USE_ADB=1; if [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then SERIAL="$2"; shift; fi; shift ;;
    *) echo "unknown option: $1 (try ./proxy.sh help)" >&2; exit 2 ;;
  esac
done

# A profile folder holds everything for one app: endpoints, base URLs, mocks, rules and settings.
# Without one, the same files live in the toolkit folder itself.
WORK_DIR="${PROFILE:-$HERE}"
if [ -z "$RULES" ]; then RULES="$WORK_DIR/rules.json"; fi
# First start of a profile: its rules.sample.json (if any) is the starting point.
if [ ! -f "$RULES" ] && [ -f "$WORK_DIR/rules.sample.json" ]; then cp "$WORK_DIR/rules.sample.json" "$RULES"; fi
[ -f "$RULES" ] || printf '[]\n' > "$RULES"
SETTINGS="$(dirname "$RULES")/settings.json"
MOCKS="$WORK_DIR/mocks"
mkdir -p "$HERE/logs" "$MOCKS"
HOST_IP="$(lan_ip)"
# Fixed-length read: a `tr </dev/urandom | head` pipe dies of SIGPIPE under pipefail.
WEB_TOKEN="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
WEB_URL="http://127.0.0.1:$WEB_PORT/?token=$WEB_TOKEN"

ENDPOINTS="$HERE/endpoints.txt"
if [ -n "$PROFILE" ] && [ -f "$PROFILE/endpoints.txt" ]; then ENDPOINTS="$PROFILE/endpoints.txt"
elif [ ! -f "$ENDPOINTS" ]; then ENDPOINTS="$HERE/profiles/example/endpoints.txt"; fi

export TAMPER_MAPPINGS_FILE="$RULES"
export TAMPER_SETTINGS_FILE="$SETTINGS"
# Relative local_file paths (mocks/orders_empty.json) resolve against the profile first, then the
# toolkit, so the built-in responses/401_unauthorized.json works from any profile.
export TAMPER_BASE_DIR="$WORK_DIR"
export TAMPER_MOCKS="$MOCKS"
export TAMPER_SERVED_LOG="$HERE/logs/served.jsonl"
export TAMPER_UNMATCHED_LOG="$HERE/logs/unmatched.jsonl"
export TAMPER_LIBRARY="$LIBRARY"
export TAMPER_ENDPOINTS="$ENDPOINTS"
if [ -n "$PROFILE" ] && [ -d "$PROFILE/responses" ]; then export TAMPER_RESPONSES="$PROFILE/responses"; fi
export TAMPER_PROXY_HOST="$HOST_IP"
export TAMPER_PROXY_PORT="$PORT"
COMMON=(--listen-host 0.0.0.0 -p "$PORT" -s "$HERE/lib/tamper_addon.py"
        --set validate_inbound_headers=false --set termlog_verbosity=warn)
if [ -n "${TAMPER_MITM_OPTS:-}" ]; then read -r -a EXTRA <<<"$TAMPER_MITM_OPTS"; COMMON+=("${EXTRA[@]}"); fi

CLEANUP=()
cleanup() { for step in ${CLEANUP[@]+"${CLEANUP[@]}"}; do eval "$step"; done; }
trap cleanup EXIT

if [ "$USE_ADB" = 1 ]; then
  ADB_ARGS=(); if [ -n "$SERIAL" ]; then ADB_ARGS=(-s "$SERIAL"); fi
  "$(adb_bin)" ${ADB_ARGS[@]+"${ADB_ARGS[@]}"} shell settings put global http_proxy "$HOST_IP:$PORT"
  CLEANUP+=('"$(adb_bin)" ${ADB_ARGS[@]+"${ADB_ARGS[@]}"} shell settings put global http_proxy :0 >/dev/null 2>&1 && echo "device proxy cleared"')
fi

# macOS: the network service behind the default route ("Wi-Fi", "Ethernet", ...).
mac_service() {
  local device
  device="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
  networksetup -listallhardwareports | awk -v dev="$device" '/^Hardware Port:/ {sub(/^Hardware Port: /, ""); port=$0} $2 == dev {print port; exit}'
}

if [ "$SYSTEM_PROXY" = 1 ]; then
  command -v networksetup >/dev/null 2>&1 || { echo "--system-proxy is macOS only." >&2; exit 2; }
  SERVICE="$(mac_service)"
  [ -n "$SERVICE" ] || { echo "Could not find the active network service." >&2; exit 1; }
  WAS_WEB="$(networksetup -getwebproxy "$SERVICE" | awk '/^Enabled:/ {print $2}')"
  WAS_SECURE="$(networksetup -getsecurewebproxy "$SERVICE" | awk '/^Enabled:/ {print $2}')"
  networksetup -setwebproxy "$SERVICE" 127.0.0.1 "$PORT"
  networksetup -setsecurewebproxy "$SERVICE" 127.0.0.1 "$PORT"
  echo "  Mac proxy for \"$SERVICE\" → 127.0.0.1:$PORT (restored on exit)"
  # Restore the previous on/off state; only an already-enabled proxy is left on.
  if [ "$WAS_WEB" != "Yes" ]; then CLEANUP+=('networksetup -setwebproxystate "$SERVICE" off'); fi
  if [ "$WAS_SECURE" != "Yes" ]; then CLEANUP+=('networksetup -setsecurewebproxystate "$SERVICE" off'); fi
  CLEANUP+=('echo "  Mac proxy restored"')
fi

opener() {
  if command -v open >/dev/null 2>&1; then open "$1"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1; fi
}

# Open the panel once it answers, in the background so the proxy output stays in this terminal.
open_when_ready() {
  for _ in $(seq 1 75); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$PANEL_PORT") 2>/dev/null; then opener "http://127.0.0.1:$PANEL_PORT"; return; fi
    sleep 0.2
  done
}

PANEL_URL="http://127.0.0.1:$PANEL_PORT"
echo
if [ "$HEADLESS" = 1 ]; then
  echo "  Device proxy : $HOST_IP:$PORT   (Android emulator: 10.0.2.2:$PORT)"
  echo "  Rules file   : $RULES   (headless: edit it, it reloads on save)"
  echo
  MITMDUMP="$(bin mitmdump)"; [ -n "$MITMDUMP" ] || { echo "mitmdump not found. Run: bash setup.sh" >&2; exit 1; }
  "$MITMDUMP" "${COMMON[@]}"
  exit $?
fi

export TAMPER_PANEL_PORT="$PANEL_PORT"
echo "  ┌──────────────────────────────────────────────────────────────"
echo "  │  Tamper panel   →  $PANEL_URL"
echo "  │  Device proxy   :  $HOST_IP:$PORT   (Android emulator: 10.0.2.2:$PORT)"
echo "  └──────────────────────────────────────────────────────────────"
echo "  Rules file     : $RULES"
if [ -n "$PROFILE" ]; then echo "  Profile        : $PROFILE"; fi
if [ -n "$LIBRARY" ]; then echo "  Tamper folders : ${LIBRARY//:/, }"; else echo "  Tamper folders : none (add one with --library ~/path/to/tampers)"; fi
if [ "$OPEN_BROWSER" = 1 ]; then open_when_ready & fi

if [ "$INSPECTOR" = 1 ]; then
  export TAMPER_WEB_URL="$WEB_URL"
  echo "  Raw inspector  : $WEB_URL   (optional, advanced)"
  echo "  Ctrl+C to stop."
  echo
  MITMWEB="$(bin mitmweb)"; [ -n "$MITMWEB" ] || { echo "mitmweb not found. Run: bash setup.sh" >&2; exit 1; }
  "$MITMWEB" "${COMMON[@]}" --web-host 127.0.0.1 --web-port "$WEB_PORT" \
    --set web_open_browser=false --set web_password="$WEB_TOKEN"
else
  echo "  Ctrl+C to stop."
  echo
  MITMDUMP="$(bin mitmdump)"; [ -n "$MITMDUMP" ] || { echo "mitmdump not found. Run: bash setup.sh" >&2; exit 1; }
  "$MITMDUMP" "${COMMON[@]}"
fi
