#!/usr/bin/env bash
# start-vm.sh – startet die Sandbox-VM im Hintergrund.
#
# Modi (über Env-Variable SANDBOX_MODE):
#   headless  (default) – kein Display, nur SSH
#   console             – seriell auf Terminal, Strg-A X zum Beenden
#   vnc                 – VNC auf localhost:5900 (für GUI-Schritt später)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VM_DIR="$SCRIPT_DIR/vm"
PID_FILE="$VM_DIR/qemu.pid"
MONITOR_SOCK="$VM_DIR/qemu-monitor.sock"
LOG_FILE="$VM_DIR/qemu.log"

MODE="${SANDBOX_MODE:-headless}"

# Ressourcen
MEM_MB="${SANDBOX_MEM_MB:-4096}"
CPUS="${SANDBOX_CPUS:-2}"

# Port-Forwarding: host:gast
SSH_PORT="${SANDBOX_SSH_PORT:-2222}"
VNC_PORT="${SANDBOX_VNC_PORT:-5900}"

log() { printf "\033[1;34m[start]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[start]\033[0m %s\n" "$*" >&2; }

# Schon laufend?
if [[ -f "$PID_FILE" ]]; then
    EXISTING_PID="$(cat "$PID_FILE")"
    if kill -0 "$EXISTING_PID" 2>/dev/null; then
        err "VM läuft bereits (PID $EXISTING_PID). Stop mit ./stop-vm.sh"
        exit 1
    else
        log "Stale PID-File gefunden, lösche."
        rm -f "$PID_FILE"
    fi
fi

# Voraussetzungen prüfen
for f in "$VM_DIR/disk.qcow2" "$VM_DIR/seed.iso" "$VM_DIR/edk2-code.fd" "$VM_DIR/edk2-vars.fd"; do
    if [[ ! -f "$f" ]]; then
        err "Fehlt: $f"
        err "Erst ./setup.sh ausführen."
        exit 1
    fi
done

log "Starte VM (mode=$MODE, mem=${MEM_MB}M, cpus=$CPUS)…"
log "  SSH:  localhost:${SSH_PORT}  →  guest:22"
[[ "$MODE" == "vnc" ]] && log "  VNC:  localhost:${VNC_PORT}  →  guest:5900"

# QEMU-Argumente zusammenbauen.
#
# Erklärungen:
#   -machine virt,...   : generische ARM-Maschine, kompatibel mit HVF
#   -accel hvf          : Apple Hypervisor.framework – native Geschwindigkeit
#   -cpu host           : CPU-Features 1:1 durchreichen (nur mit HVF)
#   -drive ...edk2-code : UEFI-Firmware (read-only)
#   -drive ...edk2-vars : UEFI NVRAM (per-VM Variablen, beschreibbar)
#   -drive ...disk      : Haupt-Disk (Overlay)
#   -drive ...seed.iso  : cloud-init Seed (read-only)
#   -netdev user,...    : User-Mode-Networking + Port-Forwards
#   -device virtio-net  : virtio NIC für Performance
#   -monitor unix:...   : QEMU Monitor über Unix-Socket – für graceful shutdown

QEMU_ARGS=(
    -name "sandbox-vm"
    -machine "virt,highmem=on,gic-version=3"
    -accel hvf
    -cpu host
    -smp "$CPUS"
    -m "$MEM_MB"

    # UEFI
    -drive "if=pflash,format=raw,readonly=on,file=$VM_DIR/edk2-code.fd"
    -drive "if=pflash,format=raw,file=$VM_DIR/edk2-vars.fd"

    # Disks
    -drive "if=virtio,format=qcow2,file=$VM_DIR/disk.qcow2"
    -drive "if=virtio,format=raw,file=$VM_DIR/seed.iso,readonly=on"

    # Netzwerk + Port-Forwarding
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:${VNC_PORT}-:5900"
    -device "virtio-net-pci,netdev=net0"

    # RNG (sonst hängt der Boot mit "waiting for entropy")
    -device "virtio-rng-pci"

    # Steuerung über Unix-Socket statt stdio (für daemonized run)
    -monitor "unix:${MONITOR_SOCK},server,nowait"

    # PID-File schreiben
    -pidfile "$PID_FILE"
)

case "$MODE" in
    headless)
        QEMU_ARGS+=(-display none -serial "file:$LOG_FILE")
        QEMU_ARGS+=(-daemonize)
        ;;
    console)
        QEMU_ARGS+=(-display none -serial mon:stdio)
        # nicht daemonized – wir wollen die Konsole sehen
        ;;
    vnc)
        # VNC-Display über die durchgereichte Port-Forward-Verbindung.
        # Innerhalb des Gasts muss x11vnc laufen (kommt später).
        # Hier nutzen wir QEMUs eigenes VNC nicht – wir wollen GUI im Gast sehen.
        QEMU_ARGS+=(-display none -serial "file:$LOG_FILE")
        QEMU_ARGS+=(-daemonize)
        ;;
    *)
        err "Unbekannter Mode: $MODE  (headless|console|vnc)"
        exit 1
        ;;
esac

# Logfile rotieren
[[ -f "$LOG_FILE" ]] && mv "$LOG_FILE" "$LOG_FILE.old"

# Los geht's
exec qemu-system-aarch64 "${QEMU_ARGS[@]}"
