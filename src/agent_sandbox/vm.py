"""Die Haupt-API: SandboxVM.

Designziele:
  - Ein SSH-Channel pro Befehl (paramiko macht das so), aber EIN Transport
    pro Instanz. Spart Reconnect-Kosten zwischen run()-Aufrufen.
  - run() ist sync, gibt ein strukturiertes CommandResult zurück.
  - Sauberer Context-Manager: with SandboxVM() as vm: ...
  - File-Transfer über SFTP (Paramikos SFTPClient), nicht scp/cat-Hacks.

Was wir absichtlich NICHT machen:
  - Eigenen Async-Layer einziehen – passiert später falls nötig
  - Long-lived shell sessions (paramiko.invoke_shell) – jeder Befehl ist
    unabhängig. Wenn der Agent Stateful-Shell braucht, baut der Orchestrator
    das oben drauf (z.B. mit `bash -c` und expliziter env).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

# typing.Self gibt es erst ab Python 3.11. Wir nutzen typing_extensions als
# Backport, damit der Code auf 3.10+ läuft (wie in pyproject.toml deklariert).
from typing_extensions import Self

import paramiko

from .config import SandboxConfig
from .errors import (
    CommandError,
    CommandTimeoutError,
    ConnectionError,
    FileTransferError,
    SandboxTimeoutError,
)
from .lifecycle import is_running, start_vm, stop_vm, wait_for_port

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Ergebnis eines run()-Aufrufs.

    duration_s hilft Agenten zu lernen welche Operationen teuer sind.
    """

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SandboxVM:
    """Schnittstelle zur Sandbox-VM.

    Beispiel::

        with SandboxVM() as vm:
            result = vm.run("uname -a")
            print(result.stdout)

            vm.put_file("local.txt", "/home/agent/remote.txt")
            vm.get_file("/etc/os-release", "os-release")
    """

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Schließe SSH; VM selbst lassen wir am Leben.
        # Begründung: Lifecycle der VM ≠ Lifecycle des Wrappers.
        # Wer die VM stoppen will, ruft explizit shutdown_vm() oder stop-vm.sh.
        self.close()

    def start(self) -> None:
        """Stellt sicher dass die VM läuft und eine SSH-Session offen ist.

        Startet die VM falls config.auto_start gesetzt ist und sie nicht läuft.
        Wartet auf SSH-Port, baut Verbindung auf, optional: wartet auf
        cloud-init readiness.
        """
        if not is_running(self.config) and self.config.auto_start:
            logger.info("VM läuft nicht, starte sie")
            start_vm(self.config)

        wait_for_port(self.config)
        self._connect()

        if self.config.wait_for_cloud_init:
            self._wait_for_cloud_init()

    def close(self) -> None:
        """Schließt SSH-Verbindung. Lässt die VM weiterlaufen."""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception as e:
                logger.debug("SFTP close error: %s", e)
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                logger.debug("SSH close error: %s", e)
            self._client = None

    def shutdown_vm(self) -> None:
        """Fährt die VM herunter (graceful via stop-vm.sh)."""
        self.close()
        stop_vm(self.config)

    # ------------------------------------------------------------------ commands

    def run(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        check: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Führt einen Befehl im Gast aus und gibt das Ergebnis zurück.

        Args:
            command: Befehlszeile (wird von /bin/sh -c interpretiert).
            timeout_s: Override für config.command_timeout_s.
            check: Wenn True, wirft CommandError bei exit_code != 0.
            cwd: Working directory im Gast.
            env: Zusätzliche Environment-Variablen (sicher quoted).

        Returns:
            CommandResult mit stdout/stderr/exit_code/duration_s.
        """
        client = self._ensure_connected()
        timeout = timeout_s if timeout_s is not None else self.config.command_timeout_s

        wrapped = self._wrap_command(command, cwd=cwd, env=env)

        logger.debug("run: %s", wrapped)
        start_time = time.monotonic()

        try:
            stdin, stdout, stderr = client.exec_command(wrapped, timeout=timeout)
            stdin.close()
            # exit_status blockiert bis Befehl fertig ist; das Timeout aus
            # exec_command greift auf Channel-Reads, also lesen wir explizit:
            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        except TimeoutError as e:  # paramiko wirft socket.timeout (Alias)
            raise CommandTimeoutError(
                f"Befehl überschritt Timeout von {timeout}s: {command!r}"
            ) from e

        duration = time.monotonic() - start_time
        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_s=duration,
        )

        if check and not result.ok:
            raise CommandError(command, exit_code, stdout_text, stderr_text)

        return result

    def _wrap_command(
        self,
        command: str,
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> str:
        """Wickelt einen Befehl mit cwd/env in ein sicheres Shell-Konstrukt.

        Wir setzen Environment-Variablen als `export K=V`-Statements VOR dem
        Befehl in derselben Subshell. Hintergrund: ein vorangestelltes
        `env K=V command` würde nicht funktionieren, weil die SSH-Login-Shell
        `$K` im command-String expandiert BEVOR env startet – $K ist da noch
        nicht gesetzt, also wird es zum leeren String.

        Mit `export` in derselben Shell sind die Variablen für den Rest der
        Befehlszeile bereits gesetzt, also expandiert die Shell sie korrekt.

        cwd kommt als `cd -- DIR &&` davor. POSIX-Shell wird vorausgesetzt
        (Debian: dash als /bin/sh).
        """
        from shlex import quote

        parts: list[str] = []
        if env:
            for key, value in env.items():
                # Validierung: keys müssen valide Shell-Identifier sein,
                # NUL ist in beiden Feldern verboten.
                if not key or "=" in key or "\0" in key or any(
                    c.isspace() for c in key
                ):
                    raise ValueError(f"Invalid env var name: {key!r}")
                if "\0" in value:
                    raise ValueError(f"Invalid env var value: {value!r}")
                parts.append(f"export {key}={quote(value)};")
        if cwd is not None:
            parts.append(f"cd -- {quote(cwd)} &&")
        # Originalbefehl unquoted anhängen – wird von der Remote-Shell
        # interpretiert. Das ist Absicht: der Caller soll Shell-Syntax
        # nutzen können (pipes, redirects, &&).
        parts.append(command)
        return " ".join(parts)

    # ------------------------------------------------------------------ files

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        """Kopiert local_path vom Host nach remote_path im Gast."""
        sftp = self._ensure_sftp()
        local = Path(local_path)
        if not local.is_file():
            raise FileTransferError(f"Lokale Datei nicht gefunden: {local}")
        try:
            sftp.put(str(local), remote_path)
        except OSError as e:
            raise FileTransferError(
                f"put_file({local} -> {remote_path}) fehlgeschlagen: {e}"
            ) from e

    def get_file(self, remote_path: str, local_path: str | Path) -> None:
        """Kopiert remote_path aus dem Gast nach local_path auf dem Host."""
        sftp = self._ensure_sftp()
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            sftp.get(remote_path, str(local))
        except OSError as e:
            raise FileTransferError(
                f"get_file({remote_path} -> {local}) fehlgeschlagen: {e}"
            ) from e

    def write_text(self, remote_path: str, content: str, *, mode: int = 0o644) -> None:
        """Schreibt Text direkt in eine Remote-Datei. Bequemer als put_file
        für kurze Inhalte (Configs, Scripts).
        """
        sftp = self._ensure_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content)
            sftp.chmod(remote_path, mode)
        except OSError as e:
            raise FileTransferError(
                f"write_text({remote_path}) fehlgeschlagen: {e}"
            ) from e

    def read_text(self, remote_path: str) -> str:
        """Liest eine Remote-Datei als Text."""
        sftp = self._ensure_sftp()
        try:
            with sftp.open(remote_path, "r") as f:
                return f.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise FileTransferError(
                f"read_text({remote_path}) fehlgeschlagen: {e}"
            ) from e

    # ------------------------------------------------------------------ internals

    def _connect(self) -> None:
        """Baut SSH-Verbindung auf, mit Retry-Loop für Race-Conditions."""
        key_path = self.config.absolute_ssh_key_path()
        if not key_path.is_file():
            raise ConnectionError(
                f"SSH-Key nicht gefunden: {key_path}. Erst ./setup.sh ausführen."
            )

        # known_hosts ignorieren – das Repo hat seinen eigenen Hostkey, und
        # bei Reset wechselt er. Wir prüfen Hosts also nicht.
        # Für ein lokales Loopback-Setup ist das akzeptabel.
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = paramiko.Ed25519Key.from_private_key_file(str(key_path))

        deadline = time.monotonic() + self.config.boot_timeout_s
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client.connect(
                    hostname=self.config.ssh_host,
                    port=self.config.ssh_port,
                    username=self.config.ssh_user,
                    pkey=pkey,
                    timeout=5,
                    auth_timeout=5,
                    banner_timeout=5,
                    allow_agent=False,
                    look_for_keys=False,
                )
                self._client = client
                logger.info(
                    "SSH connected to %s@%s:%d",
                    self.config.ssh_user, self.config.ssh_host, self.config.ssh_port,
                )
                return
            except (paramiko.SSHException, OSError) as e:
                last_exc = e
                time.sleep(self.config.connect_retry_interval_s)

        raise ConnectionError(
            f"SSH-Verbindung scheiterte nach {self.config.boot_timeout_s}s: {last_exc}"
        ) from last_exc

    def _ensure_connected(self) -> paramiko.SSHClient:
        if self._client is None:
            raise ConnectionError("Keine SSH-Verbindung. Erst .start() aufrufen.")
        # Transport-Liveness prüfen; reconnect wenn weg
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            logger.info("SSH-Transport tot, reconnect")
            self._client = None
            self._sftp = None
            self._connect()
            assert self._client is not None
        return self._client

    def _ensure_sftp(self) -> paramiko.SFTPClient:
        client = self._ensure_connected()
        if self._sftp is None:
            self._sftp = client.open_sftp()
        return self._sftp

    def _wait_for_cloud_init(self) -> None:
        """Pollt /var/log/sandbox-ready bis es existiert (Marker aus user-data)."""
        deadline = time.monotonic() + self.config.cloud_init_timeout_s
        while time.monotonic() < deadline:
            result = self.run(
                "test -f /var/log/sandbox-ready",
                timeout_s=10,
                check=False,
            )
            if result.ok:
                logger.info("cloud-init fertig")
                return
            time.sleep(3)
        raise SandboxTimeoutError(
            f"cloud-init nicht innerhalb von {self.config.cloud_init_timeout_s}s fertig"
        )