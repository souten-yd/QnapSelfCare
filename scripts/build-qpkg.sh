#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo 'Usage: build-qpkg.sh <x86_64|arm_64> <output-directory>' >&2
  exit 2
fi
arch="$1"
output="$2"
case "$arch" in x86_64|arm_64) ;; *) echo "Unsupported arch: $arch" >&2; exit 2;; esac
root="$(cd "$(dirname "$0")/.." && pwd)"
command -v qbuild >/dev/null || { echo 'QDK qbuild is required' >&2; exit 1; }
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/shared" "$output"
cp "$root/qpkg/qpkg.cfg" "$work/qpkg.cfg"
cp "$root/qpkg/package_routines" "$work/package_routines"
cp "$root/qpkg/shared/selfcare-update" "$root/qpkg/shared/selfcare.sh" "$root/qpkg/shared/python3-path" "$root/qpkg/shared/PREVIEW.txt" "$root/updater.py" "$root/webapp.py" "$work/shared/"
cp -R "$root/web" "$work/shared/web"
cp "$root/storage.py" "$root/collectors.py" "$root/ble_protocol.py" "$root/ble_worker.py" "$root/ble_bridge.py" "$root/requirements-ble.txt" "$root/THIRD_PARTY.md" "$root/README.md" "$work/shared/"
cp -R "$root/licenses" "$root/bluetooth" "$root/docs" "$work/shared/"
mkdir -p "$work/shared/scripts"
cp "$root/scripts/install-ble.sh" "$work/shared/scripts/"
mkdir -p "$work/icons"
cp "$root/qpkg/icons/"*.png "$work/icons/"
chmod 755 "$work/shared/selfcare-update" "$work/shared/selfcare.sh" "$work/shared/python3-path"
. "$work/qpkg.cfg"
qbuild --root "$work" --build-arch "$arch" --build-dir "$work/build"
mapfile -t packages < <(find "$work/build" -maxdepth 1 -type f -name '*.qpkg')
if [[ ${#packages[@]} -ne 1 ]]; then
  echo 'Expected exactly one QPKG' >&2
  exit 1
fi
qbuild --query info "${packages[0]}"
cp "${packages[0]}" "$output/QnapSelfCare_${QPKG_VER}_${arch}.qpkg"
