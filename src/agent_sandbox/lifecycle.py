"""VM-Lifecycle: Start/Stop/Status.

Wir rufen die bestehenden Bash-Skripte auf statt QEMU selbst zu starten.
Vorteile:
  - eine Quelle der Wahrheit für die QEMU-Argumente (das Skript)
  - Bash-Skripte können auch ohne Python benutzt werden
  - keine Duplikation von Pfaden/Flags zwischen Shell und Python
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time

from .config import SandboxConfig
from .errors import SandboxTimeoutError, VMLifecycleError

logger = logging.getLogger(__name__)


def _port_open(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """Schneller TCP-Reachability-Check ohne SSH-Handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def is_running(config: SandboxConfig) -> bool:
    """True wenn der SSH-Port der VM TCP-erreichbar ist.

    Beachte: das heißt nicht dass sshd bereits handshake-fähig ist – nur dass
    QEMU läuft und Port-Forwarding aktiv ist. Für sshd-Bereitschaft den
    Boot-Wait in SandboxVM nutzen.
    """
    return _port_open(config.ssh_host, config.ssh_port)


def start_vm(config: SandboxConfig) -> None:
    """Startet die VM via ./start-vm.sh. Idempotent: tut nichts wenn schon läuft."""
    if is_running(config):
        logger.debug("VM läuft bereits (Port %d offen)", config.ssh_port)
        return

    script = config.script("start-vm.sh")
    if not script.is_file():
        raise VMLifecycleError(f"Skript nicht gefunden: {script}")

    logger.info("Starte VM über %s", script)
    result = subprocess.run(
        [str(script)],
        cwd=config.repo_root,
        capture_output=True,
        text=True,
        timeout=30,  # start-vm.sh sollte sofort zurückkehren (daemonized)
    )
    if result.returncode != 0:
        raise VMLifecycleError(
            f"start-vm.sh fehlgeschlagen (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def stop_vm(config: SandboxConfig) -> None:
    """Fährt die VM via ./stop-vm.sh herunter."""
    script = config.script("stop-vm.sh")
    if not script.is_file():
        raise VMLifecycleError(f"Skript nicht gefunden: {script}")

    logger.info("Stoppe VM über %s", script)
    result = subprocess.run(
        [str(script)],
        cwd=config.repo_root,
        capture_output=True,
        text=True,
        timeout=60,  # graceful shutdown kann etwas dauern
    )
    if result.returncode != 0:
        # Nicht-fatal: stop-vm.sh kann auch zurückkehren nachdem es hart gekillt hat
        logger.warning(
            "stop-vm.sh exit %d:\nstdout: %s\nstderr: %s",
            result.returncode, result.stdout, result.stderr,
        )


def wait_for_port(config: SandboxConfig, timeout_s: float | None = None) -> float:
    """Wartet bis SSH-Port erreichbar ist. Gibt vergangene Sekunden zurück.

    Wirft SandboxTimeoutError nach Timeout.
    """
    timeout = timeout_s if timeout_s is not None else config.boot_timeout_s
    start = time.monotonic()
    interval = config.connect_retry_interval_s

    while True:
        elapsed = time.monotonic() - start
        if _port_open(config.ssh_host, config.ssh_port, timeout_s=1.0):
            logger.info("SSH-Port erreichbar nach %.1fs", elapsed)
            return elapsed
        if elapsed > timeout:
            raise SandboxTimeoutError(
                f"VM nicht innerhalb von {timeout:.0f}s erreichbar "
                f"({config.ssh_host}:{config.ssh_port})"
            )
        time.sleep(interval)
