#!/usr/bin/env bash
# setup.sh – einmaliges Setup für die Sandbox-VM
# Idempotent: kann mehrfach ausgeführt werden, überspringt was schon da ist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VM_DIR="$SCRIPT_DIR/vm"
CLOUD_INIT_DIR="$SCRIPT_DIR/cloud-init"

# Debian 12 (bookworm) generic cloud image für arm64.
# "generic" weil es cloud-init unterstützt und KVM/HVF-tauglich ist.
DEBIAN_VERSION="12"
DEBIAN_IMAGE_NAME="debian-${DEBIAN_VERSION}-generic-arm64.qcow2"
DEBIAN_IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/${DEBIAN_IMAGE_NAME}"

DISK_SIZE="20G"

# --- Helfer -----------------------------------------------------------------

log() { printf "\033[1;34m[setup]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[setup]\033[0m %s\n" "$*" >&2; }

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "Benötigtes Programm fehlt: $1"
        err "Installiere mit: $2"
        exit 1
    fi
}

# --- Voraussetzungen prüfen -------------------------------------------------

log "Voraussetzungen prüfen…"
require_cmd qemu-system-aarch64 "brew install qemu"
require_cmd qemu-img            "brew install qemu"
require_cmd wget                "brew install wget"
require_cmd ssh-keygen          "Teil von macOS – sollte vorhanden sein"

# mkisofs (von cdrtools) ODER xorriso – wir bevorzugen mkisofs, fallen zurück
if command -v mkisofs >/dev/null 2>&1; then
    MKISO_CMD=(mkisofs)
elif command -v xorriso >/dev/null 2>&1; then
    MKISO_CMD=(xorriso -as mkisofs)
else
    err "Weder mkisofs (cdrtools) noch xorriso gefunden."
    err "Installiere mit: brew install cdrtools   ODER   brew install xorriso"
    exit 1
fi

mkdir -p "$VM_DIR"

# --- SSH-Keypair ------------------------------------------------------------

if [[ ! -f "$VM_DIR/ssh_key" ]]; then
    log "SSH-Keypair generieren…"
    ssh-keygen -t ed25519 -N "" -f "$VM_DIR/ssh_key" -C "sandbox-vm" >/dev/null
else
    log "SSH-Keypair existiert bereits, überspringen."
fi

SSH_PUBKEY="$(cat "$VM_DIR/ssh_key.pub")"

# --- Debian Cloud-Image holen ----------------------------------------------

BASE_IMAGE="$VM_DIR/debian-arm64.qcow2"

if [[ ! -f "$BASE_IMAGE" ]]; then
    log "Debian ${DEBIAN_VERSION} ARM64 Cloud-Image herunterladen (~ einige hundert MB)…"
    wget --show-progress -O "$BASE_IMAGE.tmp" "$DEBIAN_IMAGE_URL"
    mv "$BASE_IMAGE.tmp" "$BASE_IMAGE"
else
    log "Basis-Image existiert bereits, überspringen."
fi

# --- Overlay-Disk anlegen (qcow2 backed by base image) ---------------------
# Vorteil: Reset = Overlay löschen und neu anlegen. Basis bleibt unverändert.

OVERLAY_IMAGE="$VM_DIR/disk.qcow2"

if [[ ! -f "$OVERLAY_IMAGE" ]]; then
    log "Overlay-Disk anlegen (${DISK_SIZE}, sparse)…"
    qemu-img create -f qcow2 -F qcow2 -b "debian-arm64.qcow2" "$OVERLAY_IMAGE" "$DISK_SIZE"
else
    log "Overlay-Disk existiert bereits, überspringen."
fi

# --- cloud-init Seed-ISO bauen ---------------------------------------------
# user-data wird gerendert (SSH-Pubkey eingesetzt), dann zusammen mit
# meta-data als ISO mit Label "cidata" gepackt. So findet cloud-init es.

SEED_ISO="$VM_DIR/seed.iso"
RENDERED_USER_DATA="$VM_DIR/.user-data.rendered"

log "cloud-init Seed-ISO bauen…"

# Pubkey in user-data einsetzen.
# sed mit | als Trenner, weil Pubkey '/' enthalten kann.
sed "s|__SSH_PUBKEY__|${SSH_PUBKEY}|" "$CLOUD_INIT_DIR/user-data" > "$RENDERED_USER_DATA"

# ISO im NoCloud-Format bauen
"${MKISO_CMD[@]}" \
    -output "$SEED_ISO" \
    -volid cidata \
    -joliet -rock \
    -graft-points \
        "user-data=$RENDERED_USER_DATA" \
        "meta-data=$CLOUD_INIT_DIR/meta-data" \
    >/dev/null 2>&1

rm -f "$RENDERED_USER_DATA"

# --- UEFI-Firmware kopieren -------------------------------------------------
# ARM64-Gäste brauchen UEFI (kein BIOS). QEMU bringt EDK2 mit.
# Wir kopieren das vars-File pro VM, damit es beschreibbar ist.

EDK2_CODE_SRC="$(brew --prefix qemu 2>/dev/null)/share/qemu/edk2-aarch64-code.fd"
EDK2_VARS_SRC="$(brew --prefix qemu 2>/dev/null)/share/qemu/edk2-arm-vars.fd"

if [[ ! -f "$EDK2_CODE_SRC" ]]; then
    # Fallback: andere Pfade durchsuchen
    EDK2_CODE_SRC="$(find /opt/homebrew /usr/local -name 'edk2-aarch64-code.fd' 2>/dev/null | head -n1)"
    EDK2_VARS_SRC="$(find /opt/homebrew /usr/local -name 'edk2-arm-vars.fd' 2>/dev/null | head -n1)"
fi

if [[ -z "$EDK2_CODE_SRC" || ! -f "$EDK2_CODE_SRC" ]]; then
    err "edk2-aarch64-code.fd nicht gefunden. Ist qemu via brew installiert?"
    exit 1
fi

cp -f "$EDK2_CODE_SRC" "$VM_DIR/edk2-code.fd"
if [[ ! -f "$VM_DIR/edk2-vars.fd" ]]; then
    cp -f "$EDK2_VARS_SRC" "$VM_DIR/edk2-vars.fd"
fi

# --- Fertig -----------------------------------------------------------------

log "Setup fertig."
log "  Base image:    $BASE_IMAGE"
log "  Overlay disk:  $OVERLAY_IMAGE"
log "  Seed ISO:      $SEED_ISO"
log "  SSH key:       $VM_DIR/ssh_key"
log ""
log "Nächster Schritt: ./start-vm.sh"
log "Erster Boot dauert 1-2 Minuten (cloud-init Konfiguration)."
log "Dann: ./verify.sh"
