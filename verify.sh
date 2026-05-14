#!/usr/bin/env bash
# verify.sh – wartet bis SSH antwortet und führt einen Testbefehl aus.
# Erster Boot dauert wegen cloud-init 1-2 Minuten.
#
# Optionen:
#   --wait-cloud-init   zusätzlich warten bis cloud-init durch ist
#                       (Marker-File /var/log/sandbox-ready im Gast).
#                       Ohne das Flag wird nur ein Hinweis ausgegeben,
#                       falls cloud-init noch läuft – kein Fehler.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_DIR="$SCRIPT_DIR/vm"
SSH_PORT="${SANDBOX_SSH_PORT:-2222}"

TIMEOUT_S="${SANDBOX_VERIFY_TIMEOUT:-180}"
CLOUD_INIT_TIMEOUT_S="${SANDBOX_CLOUD_INIT_TIMEOUT:-300}"

WAIT_CLOUD_INIT=0
for arg in "$@"; do
    case "$arg" in
        --wait-cloud-init) WAIT_CLOUD_INIT=1 ;;
        -h|--help)
            cat <<EOF
Usage: $0 [--wait-cloud-init]

  --wait-cloud-init   Warte bis cloud-init im Gast fertig ist (bis zu ${CLOUD_INIT_TIMEOUT_S}s).
                      Ohne das Flag wird nur SSH-Erreichbarkeit geprüft.

Env-Variablen:
  SANDBOX_SSH_PORT             default: 2222
  SANDBOX_VERIFY_TIMEOUT       default: 180  (SSH-Wartezeit in s)
  SANDBOX_CLOUD_INIT_TIMEOUT   default: 300  (cloud-init-Wartezeit in s)
EOF
            exit 0
            ;;
        *)
            printf "Unbekanntes Argument: %s\n" "$arg" >&2
            exit 1
            ;;
    esac
done

log() { printf "\033[1;34m[verify]\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m[verify]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[verify]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[verify]\033[0m %s\n" "$*" >&2; }

# --- 1) Warten auf SSH ------------------------------------------------------

log "Warte auf SSH (bis zu ${TIMEOUT_S}s)…"

START="$(date +%s)"
while true; do
    NOW="$(date +%s)"
    ELAPSED=$((NOW - START))
    if (( ELAPSED > TIMEOUT_S )); then
        err "Timeout. VM-Log: $VM_DIR/qemu.log"
        exit 1
    fi

    if ssh \
        -i "$VM_DIR/ssh_key" \
        -o "UserKnownHostsFile=$VM_DIR/known_hosts" \
        -o "StrictHostKeyChecking=accept-new" \
        -o "ConnectTimeout=3" \
        -o "LogLevel=ERROR" \
        -o "BatchMode=yes" \
        -p "$SSH_PORT" \
        agent@localhost "true" 2>/dev/null
    then
        break
    fi

    printf "."
    sleep 3
done
echo

ok "SSH erreichbar nach ${ELAPSED}s."

# --- 2) Optional: warten auf cloud-init ------------------------------------

# Helfer: prüft im Gast ob cloud-init durch ist.
# Wir nutzen primär das offizielle `cloud-init status --wait`-Verhalten via
# Marker-File. Das Marker-File schreibt unser runcmd-Block am Ende von cloud-init.
guest_cloud_init_done() {
    ssh \
        -i "$VM_DIR/ssh_key" \
        -o "UserKnownHostsFile=$VM_DIR/known_hosts" \
        -o "StrictHostKeyChecking=accept-new" \
        -o "ConnectTimeout=3" \
        -o "LogLevel=ERROR" \
        -o "BatchMode=yes" \
        -p "$SSH_PORT" \
        agent@localhost \
        "test -f /var/log/sandbox-ready" 2>/dev/null
}

if (( WAIT_CLOUD_INIT == 1 )); then
    log "Warte auf cloud-init (bis zu ${CLOUD_INIT_TIMEOUT_S}s)…"
    CI_START="$(date +%s)"
    while true; do
        if guest_cloud_init_done; then
            CI_NOW="$(date +%s)"
            CI_ELAPSED=$((CI_NOW - CI_START))
            echo
            ok "cloud-init fertig nach ${CI_ELAPSED}s."
            break
        fi

        CI_NOW="$(date +%s)"
        CI_ELAPSED=$((CI_NOW - CI_START))
        if (( CI_ELAPSED > CLOUD_INIT_TIMEOUT_S )); then
            echo
            err "cloud-init nicht innerhalb ${CLOUD_INIT_TIMEOUT_S}s fertig."
            err "Im Gast prüfen mit:  ./ssh-vm.sh sudo cloud-init status --long"
            exit 1
        fi

        printf "."
        sleep 5
    done
fi

# --- 3) Gast-Info sammeln ---------------------------------------------------

log "Sammle Gast-Info…"
./ssh-vm.sh bash -s <<'REMOTE'
echo "----- Identität -----"
echo "hostname: $(hostname)"
echo "user:     $(whoami)"
echo "uid:      $(id -u)"
echo "----- System -----"
echo "kernel:   $(uname -r)"
echo "arch:     $(uname -m)"
echo "----- Ressourcen -----"
echo "cpus:     $(nproc)"
echo "mem:      $(awk '/MemTotal/ {printf "%.1f GB\n", $2/1024/1024}' /proc/meminfo)"
echo "disk:     $(df -h / | awk 'NR==2 {print $2 " total, " $4 " free"}')"
echo "----- cloud-init -----"
if [[ -f /var/log/sandbox-ready ]]; then
    echo "ready:    yes"
else
    echo "ready:    no  (cloud-init läuft noch – mit --wait-cloud-init darauf warten)"
fi
echo "----- GUI-Stack -----"
# active/inactive/failed/not-found – wir wollen wissen ob die Units überhaupt
# da sind (cloud-init könnte sie noch nicht installiert haben) und ob sie laufen.
for unit in sandbox-xvfb sandbox-xfce sandbox-vnc; do
    state="$(systemctl is-active "$unit.service" 2>/dev/null || true)"
    if [[ -z "$state" ]]; then state="not-found"; fi
    printf "%-14s %s\n" "$unit:" "$state"
done
echo "----- Python -----"
python3 --version
REMOTE

# Wenn der User nicht explizit gewartet hat: Hinweis ausgeben falls noch nicht ready.
if (( WAIT_CLOUD_INIT == 0 )); then
    if ! guest_cloud_init_done; then
        warn "cloud-init läuft noch im Hintergrund. Für Vollständigkeit:"
        warn "  ./verify.sh --wait-cloud-init"
    fi
fi

ok "Verifikation erfolgreich. VM ist einsatzbereit."