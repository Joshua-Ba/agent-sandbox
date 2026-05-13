#!/usr/bin/env bash
# stop-vm.sh – fährt die VM herunter.
# Versucht erst graceful (ACPI shutdown), dann hart.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_DIR="$SCRIPT_DIR/vm"
PID_FILE="$VM_DIR/qemu.pid"
MONITOR_SOCK="$VM_DIR/qemu-monitor.sock"

log() { printf "\033[1;34m[stop]\033[0m %s\n" "$*"; }

if [[ ! -f "$PID_FILE" ]]; then
    log "Keine laufende VM (kein PID-File)."
    exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    log "PID $PID läuft nicht mehr, räume auf."
    rm -f "$PID_FILE" "$MONITOR_SOCK"
    exit 0
fi

# Graceful shutdown via QEMU monitor (ACPI power button)
if [[ -S "$MONITOR_SOCK" ]]; then
    log "Sende ACPI shutdown…"
    # 'system_powerdown' simuliert Druck auf Power-Button.
    # Wir nutzen Python statt nc, da macOS' nc mit Unix-Sockets unrund läuft.
    python3 -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('$MONITOR_SOCK')
s.sendall(b'system_powerdown\n')
s.close()
" 2>/dev/null || true

    # Warten bis Prozess weg
    for i in $(seq 1 30); do
        if ! kill -0 "$PID" 2>/dev/null; then
            log "VM heruntergefahren."
            rm -f "$PID_FILE" "$MONITOR_SOCK"
            exit 0
        fi
        sleep 1
    done

    log "Graceful shutdown timed out, kille Prozess."
fi

# Hart killen
kill "$PID" 2>/dev/null || true
sleep 2
kill -9 "$PID" 2>/dev/null || true

rm -f "$PID_FILE" "$MONITOR_SOCK"
log "VM gestoppt."
