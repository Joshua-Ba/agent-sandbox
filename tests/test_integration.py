"""Integration-Tests gegen eine echte, laufende VM.

Voraussetzungen:
  - VM läuft (./start-vm.sh)
  - VM ist erreichbar auf 127.0.0.1:2222

Aufruf:
  pytest -m integration
  pytest -m integration -v -s    # mit Output

Diese Tests sind langsam (Sekunden) und können fehlschlagen wenn die VM
nicht bereit ist – daher opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_sandbox import (
    CommandError,
    SandboxConfig,
    SandboxVM,
)
from agent_sandbox.lifecycle import is_running

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def vm(integration_config: SandboxConfig) -> SandboxVM:
    """Wiederverwendete VM-Session über alle Tests im Modul (spart Reconnects)."""
    if not is_running(integration_config):
        pytest.skip("VM läuft nicht. Erst ./start-vm.sh ausführen.")

    with SandboxVM(integration_config) as v:
        yield v


class TestBasicCommands:
    def test_run_returns_stdout(self, vm: SandboxVM) -> None:
        result = vm.run("echo hello")
        assert result.ok
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_run_captures_stderr(self, vm: SandboxVM) -> None:
        result = vm.run("echo oops >&2; echo ok")
        assert result.ok
        assert result.stdout.strip() == "ok"
        assert result.stderr.strip() == "oops"

    def test_run_nonzero_exit(self, vm: SandboxVM) -> None:
        result = vm.run("false")
        assert not result.ok
        assert result.exit_code == 1

    def test_check_raises_on_failure(self, vm: SandboxVM) -> None:
        with pytest.raises(CommandError) as exc_info:
            vm.run("ls /does/not/exist", check=True)
        assert exc_info.value.exit_code != 0

    def test_check_passes_on_success(self, vm: SandboxVM) -> None:
        result = vm.run("true", check=True)
        assert result.ok

    def test_duration_is_measured(self, vm: SandboxVM) -> None:
        result = vm.run("sleep 0.5")
        assert 0.4 < result.duration_s < 2.0


class TestCwdAndEnv:
    def test_cwd(self, vm: SandboxVM) -> None:
        result = vm.run("pwd", cwd="/tmp")
        assert result.stdout.strip() == "/tmp"

    def test_env(self, vm: SandboxVM) -> None:
        result = vm.run("echo $MY_VAR", env={"MY_VAR": "agent-sandbox"})
        assert result.stdout.strip() == "agent-sandbox"

    def test_env_with_spaces(self, vm: SandboxVM) -> None:
        result = vm.run("echo \"$MSG\"", env={"MSG": "hello world"})
        assert result.stdout.strip() == "hello world"


class TestFileTransfer:
    def test_put_and_get(self, vm: SandboxVM, tmp_path: Path) -> None:
        local_in = tmp_path / "input.txt"
        local_in.write_text("ping\n")

        vm.put_file(local_in, "/tmp/sandbox_test_input.txt")

        local_out = tmp_path / "output.txt"
        vm.get_file("/tmp/sandbox_test_input.txt", local_out)

        assert local_out.read_text() == "ping\n"

        # Aufräumen im Gast
        vm.run("rm -f /tmp/sandbox_test_input.txt")

    def test_write_text_and_read_text(self, vm: SandboxVM) -> None:
        content = "hello\nfrom sandbox\n"
        vm.write_text("/tmp/sandbox_test_wt.txt", content)
        assert vm.read_text("/tmp/sandbox_test_wt.txt") == content
        vm.run("rm -f /tmp/sandbox_test_wt.txt")

    def test_write_text_sets_mode(self, vm: SandboxVM) -> None:
        vm.write_text("/tmp/sandbox_test_mode.sh", "#!/bin/sh\necho hi\n", mode=0o755)
        result = vm.run("/tmp/sandbox_test_mode.sh")
        assert result.stdout.strip() == "hi"
        vm.run("rm -f /tmp/sandbox_test_mode.sh")


class TestSandboxIdentity:
    def test_running_as_agent_user(self, vm: SandboxVM) -> None:
        result = vm.run("whoami")
        assert result.stdout.strip() == "agent"

    def test_is_arm64(self, vm: SandboxVM) -> None:
        result = vm.run("uname -m")
        assert result.stdout.strip() == "aarch64"

    def test_has_python3(self, vm: SandboxVM) -> None:
        result = vm.run("python3 --version", check=True)
        assert result.stdout.startswith("Python 3")


class TestScreenshotIntegration:
    """Erfordert dass cloud-init durch ist und der GUI-Stack läuft."""

    def test_screenshot_returns_image_with_expected_dimensions(
        self, vm: SandboxVM
    ) -> None:
        # Verify dass scrot überhaupt da ist – sonst überspringen mit klarer
        # Botschaft statt CommandError
        check = vm.run("command -v scrot")
        if not check.ok:
            pytest.skip("scrot nicht installiert (alte VM? neu provisionieren)")

        img = vm.screenshot()
        # Default Xvfb-Auflösung aus sandbox-xvfb.service
        assert img.size == (1280, 800)
        # RGB oder RGBA, je nach scrot-Version
        assert img.mode in ("RGB", "RGBA")

    def test_screenshot_returns_png_decodable_by_pillow(
        self, vm: SandboxVM
    ) -> None:
        check = vm.run("command -v scrot")
        if not check.ok:
            pytest.skip("scrot nicht installiert")

        img = vm.screenshot()
        # Re-encode als PNG → wieder dekodieren, prüft konsistente Daten
        import io as _io

        from PIL import Image

        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        roundtripped = Image.open(_io.BytesIO(buf.getvalue()))
        assert roundtripped.size == img.size


class TestInputIntegration:
    """Maus/Tastatur-Tests gegen echtes Xvfb + xdotool.

    Maus testen wir via xdotools eigenem getmouselocation – nach move_mouse
    muss der Cursor exakt auf der angegebenen Position sein. Das ist eine
    direkte, deterministische Verifikation.

    Tastatur können wir nicht so leicht roundtrip-testen ohne eine App zum
    Reinschreiben. Wir prüfen nur dass die Befehle ohne Fehler durchlaufen.
    """

    def _xdotool_available(self, vm: SandboxVM) -> bool:
        return vm.run("command -v xdotool").ok

    def _mouse_position(self, vm: SandboxVM) -> tuple[int, int]:
        """Holt aktuelle Mausposition über xdotools getmouselocation.

        Output-Format: 'x:640 y:400 screen:0 window:1234567'
        """
        result = vm.run(
            "xdotool getmouselocation",
            env={"DISPLAY": ":1"},
            check=True,
        )
        fields = dict(p.split(":", 1) for p in result.stdout.split())
        return int(fields["x"]), int(fields["y"])

    def test_move_mouse_sets_position(self, vm: SandboxVM) -> None:
        if not self._xdotool_available(vm):
            pytest.skip("xdotool nicht installiert")

        vm.move_mouse(100, 200)
        assert self._mouse_position(vm) == (100, 200)

        vm.move_mouse(640, 400)
        assert self._mouse_position(vm) == (640, 400)

    def test_click_moves_to_position_first(self, vm: SandboxVM) -> None:
        if not self._xdotool_available(vm):
            pytest.skip("xdotool nicht installiert")

        vm.move_mouse(0, 0)
        vm.click(500, 300)
        assert self._mouse_position(vm) == (500, 300)

    def test_type_text_runs_without_error(self, vm: SandboxVM) -> None:
        if not self._xdotool_available(vm):
            pytest.skip("xdotool nicht installiert")
        vm.type_text("hello world")
        vm.type_text("--with leading dashes")
        vm.type_text("$HOME `not expanded` ; safe")

    def test_key_runs_without_error(self, vm: SandboxVM) -> None:
        if not self._xdotool_available(vm):
            pytest.skip("xdotool nicht installiert")
        vm.key("Return")
        vm.key("Escape")
        vm.key("ctrl+a")

    def test_scroll_runs_without_error(self, vm: SandboxVM) -> None:
        if not self._xdotool_available(vm):
            pytest.skip("xdotool nicht installiert")
        vm.scroll(640, 400, direction="down", amount=2)
        vm.scroll(640, 400, direction="up", amount=1)