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
data_dir=${SELFCARE_DATA_DIR:-/share/Container/QnapSelfCare}
logfile="$data_dir/logs/selfcare.log"

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
        # QTS can start QPKGs before storage volumes, shared-folder aliases
        # and the Python QPKG have finished initializing after a NAS reboot.
        # Never create /share/Container on an unmounted backing volume.
        startup_wait=${SELFCARE_STARTUP_WAIT_SECONDS:-90}
        case "$startup_wait" in *[!0-9]*|'') startup_wait=90;; esac
        [ "$startup_wait" -le 180 ] || startup_wait=180
        elapsed=0
        python=
        while :; do
            if [ -d "$(dirname "$data_dir")" ] && [ -d /share/Container ]; then
                mkdir -p "$data_dir/logs" "$data_dir/data" "$data_dir/config"
                if python=$(/bin/sh "$root/python3-path" 2>> "$logfile"); then
                    break
                fi
            fi
            if [ "$elapsed" -ge "$startup_wait" ]; then
                echo "QnapSelfCare startup timed out after ${elapsed}s waiting for Container share and Python; check QTS volume and Python3 QPKG" >&2
                exit 1
            fi
            sleep 2
            elapsed=$((elapsed + 2))
        done
        rm -f "$root/admin-token"
        "$python" "$root/webapp.py" --lan --port 17863 --data-dir "$data_dir" < /dev/null > "$logfile" 2>&1 &
        echo $! > "$pidfile"
        sleep 1
        running || { rm -f "$pidfile"; echo "Web UI failed to start; check $logfile" >&2; exit 1; }
        ;;
    stop)
        if running; then
            kill "$(cat "$pidfile")"
            count=0
            while running; do
                count=$((count + 1))
                [ "$count" -lt 15 ] || { echo 'Web service did not stop; refusing overlapping restart' >&2; exit 1; }
                sleep 1
            done
        fi
        rm -f "$pidfile"
        ;;
    restart)
        "$0" stop
        "$0" start
        ;;
    *) echo "Usage: $0 {start|stop|restart}" >&2; exit 2;;
esac
