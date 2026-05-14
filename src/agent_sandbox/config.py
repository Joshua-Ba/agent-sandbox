"""Konfiguration und Pfade.

Wir trennen Konfiguration in eine eigene Datei, damit Tests sie leicht
patchen können (z.B. um einen Temp-Pfad statt $repo/vm zu nutzen) und damit
die Default-Werte an einer Stelle versammelt sind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _find_repo_root(start: Path | None = None) -> Path:
    """Sucht aufwärts nach dem Repo-Root (erkennbar an `setup.sh` + `vm/`-Konvention).

    Hintergrund: Das Package soll funktionieren, egal ob es per `pip install -e .`
    installiert ist oder direkt aus dem Repo importiert wird. Wir wollen die
    Bash-Skripte und das `vm/`-Verzeichnis auf dem Filesystem finden.
    """
    start = start or Path(__file__).resolve()
    for parent in [start, *start.parents]:
        if (parent / "setup.sh").is_file() and (parent / "start-vm.sh").is_file():
            return parent
    raise FileNotFoundError(
        "Konnte Repo-Root nicht finden (suche nach setup.sh und start-vm.sh). "
        "Setze SandboxConfig.repo_root explizit."
    )


@dataclass(frozen=True)
class SandboxConfig:
    """Konfiguration einer SandboxVM-Instanz.

    Alle Felder haben sinnvolle Defaults. Felder die du wahrscheinlich
    überschreiben willst:
      - ssh_port: wenn 2222 belegt ist
      - command_timeout_s: bei Befehlen die länger laufen dürfen
    """

    # Lokalisierung des Repos (für die Bash-Skripte und vm/-Pfad)
    repo_root: Path = field(default_factory=_find_repo_root)

    # SSH-Verbindung
    ssh_host: str = "127.0.0.1"
    ssh_port: int = 2222
    ssh_user: str = "agent"
    # Pfad zum Private-Key, relativ zu repo_root falls nicht absolut
    ssh_key_path: Path = Path("vm/ssh_key")

    # Wartezeiten
    boot_timeout_s: float = 180.0
    cloud_init_timeout_s: float = 300.0
    command_timeout_s: float = 60.0
    connect_retry_interval_s: float = 2.0

    # Verhalten
    auto_start: bool = True  # start_vm() im __enter__ wenn nicht erreichbar
    wait_for_cloud_init: bool = False  # bei start() auf cloud-init warten

    def absolute_ssh_key_path(self) -> Path:
        if self.ssh_key_path.is_absolute():
            return self.ssh_key_path
        return self.repo_root / self.ssh_key_path

    def script(self, name: str) -> Path:
        """Pfad zu einem Bash-Skript im Repo (setup.sh, start-vm.sh, ...)."""
        return self.repo_root / name
