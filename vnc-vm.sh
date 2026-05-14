#!/usr/bin/env bash
# vnc-vm.sh – öffnet den GUI-Desktop der Sandbox in einem VNC-Viewer.
#
# Wir rufen den Viewer per CLI mit Server-Adresse als Argument auf.
# Vorteile gegenüber `open vnc://...`:
#   - kein "vnc://"-URL-Scheme nötig – macOS Screen Sharing wird NICHT
#     parallel mitgeöffnet
#   - keine Connect-Dialog-Bestätigung – TigerVNC verbindet direkt
#
# Auswahl-Logik (in dieser Reihenfolge):
#   1. `vncviewer` im PATH (TigerVNC via Homebrew formula oder Linux)
#   2. TigerVNC.app im /Applications-Ordner (Cask-Installation)
#   3. Fallback: vnc://-URL via `open`
#
# Voraussetzung: VM läuft und cloud-init ist durch.
#   ./verify.sh --wait-cloud-init

set -euo pipefail

# Explizit 127.0.0.1 statt "localhost" – auf macOS resolven viele Tools
# "localhost" zuerst zu ::1 (IPv6), QEMUs hostfwd lauscht aber nur auf IPv4.
VNC_HOST="127.0.0.1"
VNC_PORT="${SANDBOX_VNC_PORT:-5900}"
VNC_TARGET="${VNC_HOST}:${VNC_PORT}"

log() { printf "\033[1;34m[vnc]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[vnc]\033[0m %s\n" "$*"; }

# 1) TigerVNC im PATH
if command -v vncviewer >/dev/null 2>&1; then
    log "Starte TigerVNC: vncviewer ${VNC_TARGET}"
    exec vncviewer "${VNC_TARGET}"
fi

# 2) TigerVNC.app im /Applications-Ordner
# Direkter Aufruf des Binaries im App-Bundle – kein vnc:// dabei, also wird
# macOS Screen Sharing nicht angetriggert. Argument ist die Server-Adresse,
# damit verbindet TigerVNC direkt ohne Connect-Dialog.
#
# Das Binary im Bundle heißt `vncviewer` (nicht "TigerVNC Viewer").
TIGER_BIN=""
for app in "/Applications/TigerVNC.app" "/Applications/TigerVNC Viewer.app"; do
    for bin_name in "vncviewer" "TigerVNC Viewer"; do
        candidate="$app/Contents/MacOS/$bin_name"
        if [[ -x "$candidate" ]]; then
            TIGER_BIN="$candidate"
            break 2
        fi
    done
done

if [[ -n "$TIGER_BIN" ]]; then
    log "Starte TigerVNC mit ${VNC_TARGET}"
    # nohup + Background, sonst blockiert das Shell-Skript bis der Viewer endet
    # (und Strg-C würde ihn killen).
    nohup "$TIGER_BIN" "${VNC_TARGET}" >/dev/null 2>&1 &
    disown
    exit 0
fi

# 3) Fallback: vnc:// URL – funktioniert mit macOS Screen Sharing oft NICHT
warn "TigerVNC nicht gefunden. Versuche vnc://-URL (oft inkompatibel)."
warn "Empfohlen: brew install --cask tigervnc"
warn ""
exec open "vnc://${VNC_TARGET}"