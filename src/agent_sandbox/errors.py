"""Exceptions für agent_sandbox.

Eine kleine, klare Hierarchie:

  SandboxError                  – Basis
    ├── VMLifecycleError        – Start/Stop, QEMU-Probleme
    ├── ConnectionError         – SSH-Verbindung verlor/scheiterte
    ├── CommandError            – run() Befehl scheiterte (mit Exit-Code != 0)
    ├── CommandTimeoutError     – run() lief in Timeout
    ├── FileTransferError       – put_file / get_file
    └── SandboxTimeoutError     – allgemeines Timeout (Boot, cloud-init)
"""

from __future__ import annotations


class SandboxError(Exception):
    """Basis aller Sandbox-Fehler."""


class VMLifecycleError(SandboxError):
    """Fehler beim Starten/Stoppen der VM."""


class ConnectionError(SandboxError):
    """SSH-Verbindung konnte nicht aufgebaut oder ist abgebrochen."""


class CommandError(SandboxError):
    """Ein Befehl im Gast endete mit nicht-null Exit-Code (wenn check=True)."""

    def __init__(self, command: str, exit_code: int, stdout: str, stderr: str):
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command failed (exit {exit_code}): {command!r}\n"
            f"stderr: {stderr.strip()[:500]}"
        )


class CommandTimeoutError(SandboxError):
    """Ein Befehl lief in den Timeout."""


class FileTransferError(SandboxError):
    """put_file/get_file scheiterte."""


class SandboxTimeoutError(SandboxError):
    """Generisches Timeout (Boot, cloud-init readiness, etc.)."""
