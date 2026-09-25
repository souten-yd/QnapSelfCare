#!/bin/sh
set -eu
name=QnapSelfCare
config=/etc/config/qpkg.conf
if [ -x /sbin/getcfg ] && [ -f "$config" ]; then
    root=$(/sbin/getcfg "$name" Install_Path -d "" -f "$config")
else
    root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
[ -n "$root" ] || { echo 'QPKG installation path missing' >&2; exit 1; }
pidfile="$root/selfcare.pid"

running() {
    [ -f "$pidfile" ] || return 1
    pid=$(cat "$pidfile")
    case "$pid" in *[!0-9]*|'') return 1;; esac
    [ -r "/proc/$pid/cmdline" ] || return 1
    command_line=$(tr '\000' ' ' < "/proc/$pid/cmdline")
    case "$command_line" in *"$root/webapp.py"*) kill -0 "$pid" 2>/dev/null;; *) return 1;; esac
}

case "${1:-}" in
    start)
        if [ -x /sbin/getcfg ] && [ -f "$config" ]; then
            enabled=$(/sbin/getcfg "$name" Enable -u -d FALSE -f "$config")
            [ "$enabled" = TRUE ] || { echo "$name is disabled" >&2; exit 1; }
        fi
        running && exit 0
        umask 077
        python=$(/bin/sh "$root/python3-path" 2>> "$root/selfcare.log") || {
            echo 'Python 3.8+ not found; check selfcare.log' >&2
            exit 1
        }
        rm -f "$root/admin-token"
        nohup "$python" "$root/webapp.py" --lan --port 17863 > "$root/selfcare.log" 2>&1 &
        echo $! > "$pidfile"
        sleep 1
        running || { rm -f "$pidfile"; echo 'Web UI failed to start; check selfcare.log' >&2; exit 1; }
        ;;
    stop)
        if running; then kill "$(cat "$pidfile")"; fi
        rm -f "$pidfile"
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    *) echo "Usage: $0 {start|stop|restart}" >&2; exit 2;;
esac
