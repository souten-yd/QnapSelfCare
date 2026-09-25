#!/bin/sh
# Explicit setup only; never invoked from service startup.
set -eu
qpkg_root=$(/sbin/getcfg QnapSelfCare Install_Path -d '' -f /etc/config/qpkg.conf)
[ -n "$qpkg_root" ] || { echo 'QnapSelfCare is not installed' >&2; exit 1; }
python=$(/bin/sh "$qpkg_root/python3-path")
data_dir=${SELFCARE_DATA_DIR:-/share/Container/QnapSelfCare}
[ -d "$data_dir" ] || { echo 'Start QnapSelfCare first' >&2; exit 1; }
"$python" -m pip install --target "$data_dir/python" -r "$qpkg_root/requirements-ble.txt"
printf '%s\n' 'BLE Python dependencies installed. Native mode also requires a running BlueZ system D-Bus service.'
