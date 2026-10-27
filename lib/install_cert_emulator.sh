#!/usr/bin/env bash
# Install the mitmproxy CA so an Android EMULATOR trusts intercepted HTTPS.
#
# This is the equivalent of trusting the Charles root cert, but as a SYSTEM cert so
# every app (not just opt-in debug builds) accepts it. Works on a writable-system
# emulator image (e.g. "Google APIs", NOT "Google Play"). For a physical device or a
# Play-store image you instead need a debug build whose network-security-config
# trusts user CAs (see README).
#
#   ./proxy.sh cert-emulator [-s <emulator-serial>]
set -euo pipefail

SERIAL=()
if [ "${1:-}" = "-s" ]; then SERIAL=(-s "$2"); fi

CA="${MITMPROXY_CA:-$HOME/.mitmproxy/mitmproxy-ca-cert.pem}"
if [ ! -f "$CA" ]; then
  echo "mitmproxy CA not found at $CA." >&2
  echo "Run 'mitmdump' once (it generates the CA under ~/.mitmproxy/), then retry." >&2
  exit 1
fi

# Android system certs are stored as <subject_hash_old>.0 in PEM form.
HASH="$(openssl x509 -inform PEM -subject_hash_old -in "$CA" | head -1)"
TMP="$(mktemp -d)"
cp "$CA" "$TMP/$HASH.0"

echo "Gaining writable /system on the emulator..."
adb ${SERIAL[@]+"${SERIAL[@]}"} root
# Newer images need verity disabled before remount succeeds.
if ! adb ${SERIAL[@]+"${SERIAL[@]}"} remount >/dev/null 2>&1; then
  adb ${SERIAL[@]+"${SERIAL[@]}"} disable-verity || true
  adb ${SERIAL[@]+"${SERIAL[@]}"} reboot
  adb ${SERIAL[@]+"${SERIAL[@]}"} wait-for-device
  adb ${SERIAL[@]+"${SERIAL[@]}"} root
  adb ${SERIAL[@]+"${SERIAL[@]}"} remount
fi

echo "Pushing CA as system cert $HASH.0 ..."
adb ${SERIAL[@]+"${SERIAL[@]}"} push "$TMP/$HASH.0" /system/etc/security/cacerts/
adb ${SERIAL[@]+"${SERIAL[@]}"} shell chmod 644 "/system/etc/security/cacerts/$HASH.0"
adb ${SERIAL[@]+"${SERIAL[@]}"} reboot

echo "Installed. The emulator is rebooting; mitmproxy HTTPS will be trusted system-wide."
