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

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar

import paramiko

# typing.Self gibt es erst ab Python 3.11. Wir nutzen typing_extensions als
# Backport, damit der Code auf 3.10+ läuft (wie in pyproject.toml deklariert).
from typing_extensions import Self

from .config import SandboxConfig
from .errors import (
    CommandError,
    CommandTimeoutError,
    ConnectionError,
    FileTransferError,
    SandboxTimeoutError,
    ScreenshotError,
)
from .lifecycle import is_running, start_vm, stop_vm, wait_for_port

# Type-only import: macht Pillow nur für mypy/IDE sichtbar, nicht zur Laufzeit.
# Pillow ist eine optionale Dependency – nur screenshot() braucht es.
if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

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
            stdout/stderr sind als UTF-8 dekodierte Strings. Für binäre
            Ausgaben siehe run_raw().
        """
        stdout_bytes, stderr_bytes, exit_code, duration = self._exec_raw(
            command, timeout_s=timeout_s, cwd=cwd, env=env
        )
        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_s=duration,
        )
        if check and not result.ok:
            raise CommandError(command, exit_code, result.stdout, result.stderr)
        return result

    def run_raw(
        self,
        command: str,
        *,
        timeout_s: float | None = None,
        check: bool = False,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bytes, bytes, int]:
        """Wie run(), aber gibt stdout/stderr als rohe Bytes zurück.

        Nützlich für Befehle die binäre Daten ausgeben (Screenshots, Binaries,
        komprimierte Streams). stderr-Bytes werden für Fehlermeldungen
        UTF-8-dekodiert (mit Replace), falls check=True und exit_code != 0.

        Returns:
            (stdout_bytes, stderr_bytes, exit_code)
        """
        stdout_bytes, stderr_bytes, exit_code, _ = self._exec_raw(
            command, timeout_s=timeout_s, cwd=cwd, env=env
        )
        if check and exit_code != 0:
            raise CommandError(
                command,
                exit_code,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        return stdout_bytes, stderr_bytes, exit_code

    def _exec_raw(
        self,
        command: str,
        *,
        timeout_s: float | None,
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> tuple[bytes, bytes, int, float]:
        """Interne Kern-Routine: führt Befehl aus und gibt rohe Bytes zurück.

        Returns:
            (stdout_bytes, stderr_bytes, exit_code, duration_s)
        """
        client = self._ensure_connected()
        timeout = timeout_s if timeout_s is not None else self.config.command_timeout_s

        wrapped = self._wrap_command(command, cwd=cwd, env=env)

        logger.debug("run: %s", wrapped)
        start_time = time.monotonic()

        try:
            stdin, stdout, stderr = client.exec_command(wrapped, timeout=timeout)
            stdin.close()
            stdout_bytes = stdout.read()
            stderr_bytes = stderr.read()
            exit_code = stdout.channel.recv_exit_status()
        except TimeoutError as e:  # paramiko wirft socket.timeout (Alias)
            raise CommandTimeoutError(
                f"Befehl überschritt Timeout von {timeout}s: {command!r}"
            ) from e

        duration = time.monotonic() - start_time
        return stdout_bytes, stderr_bytes, exit_code, duration

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

    # ------------------------------------------------------------------ display

    def screenshot(self, *, display: str = ":1") -> PILImage:
        """Macht ein Screenshot des Gast-Desktops und gibt es als PIL.Image zurück.

        Implementation: ruft `scrot` im Gast auf, schreibt PNG nach stdout,
        liest die Bytes über SSH zurück und dekodiert sie mit Pillow.
        Keine temp-Datei im Gast nötig.

        Args:
            display: X-Display (default ":1", wo unser Xvfb läuft).

        Returns:
            PIL.Image.Image – kann direkt .save(), .resize(), .tobytes() etc.

        Raises:
            CommandError: scrot scheiterte (z.B. weil Display nicht da ist).
            ScreenshotError: scrot lief durch, aber PNG ließ sich nicht parsen.
        """
        # Pillow erst hier importieren, damit das Package auch ohne Pillow
        # importierbar bleibt (für Tests die screenshot nicht brauchen).
        try:
            from PIL import Image
        except ImportError as e:
            raise ScreenshotError(
                "Pillow ist nicht installiert. pip install pillow"
            ) from e

        # scrot -o /dev/stdout: PNG auf stdout, Overwrite egal (Stream).
        # DISPLAY explizit per env: scrot braucht das, sonst sucht es :0
        # (was nicht existiert, weil unser Xvfb auf :1 läuft).
        stdout_bytes, stderr_bytes, _exit_code = self.run_raw(
            "scrot -o /dev/stdout",
            env={"DISPLAY": display},
            check=True,
        )

        # scrot schreibt manchmal informative Meldungen auf stderr ("Saving
        # screenshot to..."). Wir loggen das nur auf debug, nicht warnen.
        if stderr_bytes:
            logger.debug("scrot stderr: %s", stderr_bytes.decode("utf-8", errors="replace"))

        if not stdout_bytes:
            raise ScreenshotError("scrot lieferte 0 Bytes")

        try:
            img = Image.open(io.BytesIO(stdout_bytes))
            # .open() ist lazy – ein vollständiger load() jetzt deckt korrupte
            # PNGs sofort hier auf, nicht erst bei der nächsten Operation.
            img.load()
        except Exception as e:
            raise ScreenshotError(
                f"PNG-Daten von scrot konnten nicht dekodiert werden: {e}"
            ) from e

        return img

    # ------------------------------------------------------------------ input

    # xdotool kennt Buttons als Zahlen. Wir bieten lesbare Namen an, lassen
    # aber auch die Zahl direkt zu falls jemand was Exotisches braucht.
    _BUTTON_MAP: ClassVar[dict[str, int]] = {
        "left": 1,
        "middle": 2,
        "right": 3,
        "scroll_up": 4,
        "scroll_down": 5,
        "scroll_left": 6,
        "scroll_right": 7,
    }

    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        display: str = ":1",
    ) -> None:
        """Klick an Position (x, y) im Gast-Display.

        Args:
            x, y: Bildschirm-Koordinaten in Pixeln (0,0 = oben links).
            button: "left", "middle", "right", oder ein numerischer xdotool-Code.
            display: X-Display (default :1).

        Bewegt den Cursor erst zur Position, dann klickt. Im Gegensatz zu
        einem reinen `xdotool click N`, das an der aktuellen Mausposition
        klicken würde, ist das deterministisch.
        """
        btn = self._resolve_button(button)
        self._xdotool(
            f"mousemove --sync {int(x)} {int(y)} click {btn}",
            display=display,
        )

    def double_click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        display: str = ":1",
    ) -> None:
        """Doppelklick an Position (x, y).

        Wir nutzen xdotools `--repeat 2`, das kürzere Intervalle als zwei
        separate Aufrufe garantiert – Apps unterscheiden Doppelklick vom
        zweifachen Einzelklick anhand der Verzögerung.
        """
        btn = self._resolve_button(button)
        self._xdotool(
            f"mousemove --sync {int(x)} {int(y)} click --repeat 2 {btn}",
            display=display,
        )

    def move_mouse(self, x: int, y: int, *, display: str = ":1") -> None:
        """Bewegt den Mauszeiger ohne zu klicken."""
        self._xdotool(
            f"mousemove --sync {int(x)} {int(y)}",
            display=display,
        )

    def scroll(
        self,
        x: int,
        y: int,
        *,
        direction: str = "down",
        amount: int = 3,
        display: str = ":1",
    ) -> None:
        """Scrollen an Position (x, y).

        Args:
            direction: "up", "down", "left", "right".
            amount: Anzahl der "Scroll-Ticks" (1 Tick ≈ 3 Zeilen in den
                meisten Apps; experimentell anpassen).
        """
        if amount < 1:
            raise ValueError(f"scroll amount muss >= 1 sein, war {amount}")
        if direction not in ("up", "down", "left", "right"):
            raise ValueError(
                f"direction muss up/down/left/right sein, war {direction!r}"
            )
        btn = self._BUTTON_MAP[f"scroll_{direction}"]
        self._xdotool(
            f"mousemove --sync {int(x)} {int(y)} click --repeat {int(amount)} {btn}",
            display=display,
        )

    def type_text(
        self,
        text: str,
        *,
        delay_ms: int = 12,
        display: str = ":1",
    ) -> None:
        """Tippt einen String als wäre er auf der Tastatur eingegeben.

        Für einzelne Tasten oder Hotkeys (Strg+C etc.) siehe key().

        Args:
            delay_ms: Verzögerung zwischen den Zeichen. Default 12ms ist
                xdotool-Default. Manche Apps verlieren Zeichen bei 0ms,
                deshalb nicht weiter runtersetzen.
        """
        if delay_ms < 0:
            raise ValueError("delay_ms muss >= 0 sein")
        # `--` damit xdotool den Text nicht als Flags missversteht
        # (z.B. ein String der mit '-' anfängt).
        # shlex.quote für das eigentliche Text-Argument.
        from shlex import quote
        self._xdotool(
            f"type --delay {int(delay_ms)} -- {quote(text)}",
            display=display,
        )

    def key(self, keysym: str, *, display: str = ":1") -> None:
        """Drückt eine einzelne Taste oder Tastenkombination.

        Args:
            keysym: X-Keysym-Name oder Kombination. Beispiele:
                "Return", "Escape", "Tab", "BackSpace", "Delete",
                "ctrl+c", "ctrl+shift+t", "alt+F4",
                "Page_Down", "Home", "F1".

        Die volle Liste findet sich in /usr/include/X11/keysymdef.h oder
        unter `man 7 xkeyboard-config`.
        """
        if not keysym or any(c.isspace() for c in keysym):
            # `key foo bar` würde zwei Tasten drücken; das war nicht gemeint.
            # Wer das wirklich will, ruft key() mehrfach.
            raise ValueError(
                f"keysym darf keine Leerzeichen enthalten: {keysym!r}"
            )
        from shlex import quote
        self._xdotool(f"key -- {quote(keysym)}", display=display)

    @classmethod
    def _resolve_button(cls, button: str | int) -> int:
        """Übersetzt "left"/"right"/... zu xdotool-Button-Codes."""
        if isinstance(button, int):
            return button
        try:
            return cls._BUTTON_MAP[button]
        except KeyError:
            raise ValueError(
                f"Unbekannter Button: {button!r}. "
                f"Erlaubt: {list(cls._BUTTON_MAP)}"
            ) from None

    def _xdotool(self, args: str, *, display: str) -> None:
        """Führt `xdotool <args>` mit DISPLAY=:1 aus und prüft Erfolg.

        Bewusst eng gehalten – nicht öffentlich, damit wir später leicht auf
        eine andere Backend-Implementation wechseln können (z.B. wayland's
        ydotool oder direkter VNC-Input).
        """
        self.run(f"xdotool {args}", env={"DISPLAY": display}, check=True)

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