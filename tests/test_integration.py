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
