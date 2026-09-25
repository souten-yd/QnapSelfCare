#!/bin/sh
set -eu
mkdir -p /run/dbus
dbus-daemon --system --fork --nopidfile
/usr/libexec/bluetooth/bluetoothd --nodetach &
exec python /app/ble_bridge.py
