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
cp "$root/qpkg/shared/selfcare-update" "$root/qpkg/shared/PREVIEW.txt" "$root/updater.py" "$work/shared/"
chmod 755 "$work/shared/selfcare-update"
qbuild --root "$work" --build-arch "$arch" --build-dir "$work/build"
mapfile -t packages < <(find "$work/build" -maxdepth 1 -type f -name '*.qpkg')
if [[ ${#packages[@]} -ne 1 ]]; then
  echo 'Expected exactly one QPKG' >&2
  exit 1
fi
qbuild --query info "${packages[0]}"
cp "${packages[0]}" "$output/QnapSelfCare_0.1.0_${arch}.qpkg"
