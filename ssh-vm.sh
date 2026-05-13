#!/usr/bin/env bash
# ssh-vm.sh – interaktiver SSH-Login als 'agent'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_DIR="$SCRIPT_DIR/vm"
SSH_PORT="${SANDBOX_SSH_PORT:-2222}"

exec ssh \
    -i "$VM_DIR/ssh_key" \
    -o "UserKnownHostsFile=$VM_DIR/known_hosts" \
    -o "StrictHostKeyChecking=accept-new" \
    -o "LogLevel=ERROR" \
    -p "$SSH_PORT" \
    agent@localhost \
    "$@"
