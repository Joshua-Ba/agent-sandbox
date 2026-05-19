#!/usr/bin/env bash
# reset-vm.sh – Komplett-Reset und Neuaufsetzen der Sandbox-VM.
#
# Was passiert (in dieser Reihenfolge):
#   1. VM stoppen (falls läuft)
#   2. setup.sh – cloud-init Seed-ISO neu bauen (mit aktueller user-data)
#   3. Overlay-Disk vm/disk.qcow2 wegwerfen, neu anlegen
#   4. known_hosts wegwerfen (Gast generiert neuen SSH-Hostkey)
#   5. start-vm.sh
#   6. verify.sh --wait-cloud-init
#
# Was NICHT passiert (absichtlich):
#   - Basis-Image vm/debian-arm64.qcow2 wird NICHT neu geladen
#     (wäre 400+ MB Download für nichts)
#   - SSH-Key vm/ssh_key wird NICHT neu generiert
#     (würde unnötig den existierenden Provisioning-State verändern)
#
# Falls du wirklich alles plattmachen willst (inkl. Basis-Image und SSH-Key):
#   rm -rf vm/   &&   ./reset-vm.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VM_DIR="$SCRIPT_DIR/vm"
DISK_SIZE="20G"

log()  { printf "\033[1;34m[reset]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[reset]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[reset]\033[0m %s\n" "$*" >&2; }

# Sauberes Fehler-Reporting: zeigt an welcher Stelle es geknallt hat
fail_step() {
    err "Reset abgebrochen in Schritt: $1"
    err "Logs prüfen, dann manuell weitermachen oder reset-vm.sh erneut versuchen."
    exit 1
}

# --- 1) VM stoppen ----------------------------------------------------------
log "1/6 VM stoppen (falls läuft)…"
./stop-vm.sh || fail_step "stop-vm.sh"

# --- 2) setup.sh: Seed-ISO neu bauen ---------------------------------------
log "2/6 setup.sh – cloud-init Seed-ISO neu bauen…"
./setup.sh || fail_step "setup.sh"

# --- 3) Overlay-Disk zurücksetzen ------------------------------------------
# setup.sh würde die Overlay-Disk NICHT neu anlegen wenn sie schon existiert
# (idempotent). Wir wollen aber bewusst frisch starten.
log "3/6 Overlay-Disk zurücksetzen…"
if [[ ! -f "$VM_DIR/debian-arm64.qcow2" ]]; then
    err "Basis-Image fehlt: $VM_DIR/debian-arm64.qcow2"
    err "setup.sh sollte es heruntergeladen haben – ist da etwas schief?"
    exit 1
fi
rm -f "$VM_DIR/disk.qcow2"
qemu-img create -f qcow2 -F qcow2 \
    -b "debian-arm64.qcow2" \
    "$VM_DIR/disk.qcow2" "$DISK_SIZE" >/dev/null \
    || fail_step "qemu-img create (Overlay-Disk)"

# --- 4) known_hosts wegwerfen ----------------------------------------------
# Der neu provisionierte Gast generiert beim ersten Boot einen neuen SSH-
# Hostkey. Ohne dieses rm würde ssh über einen veränderten Hostkey klagen.
log "4/6 known_hosts wegwerfen…"
rm -f "$VM_DIR/known_hosts"

# --- 5) VM starten ----------------------------------------------------------
log "5/6 VM starten…"
./start-vm.sh || fail_step "start-vm.sh"

# --- 6) Verify (inkl. cloud-init Wait) -------------------------------------
log "6/6 Verifikation (wartet auf SSH und cloud-init, kann mehrere Minuten dauern)…"
./verify.sh --wait-cloud-init || fail_step "verify.sh --wait-cloud-init"

ok "Reset komplett. VM ist frisch provisioniert und einsatzbereit."