"""Unit-Tests für lifecycle.py.

Wir mocken socket und subprocess. is_running()/start_vm()/stop_vm() sollen
sich deterministisch verhalten.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from agent_sandbox import SandboxConfig, SandboxTimeoutError, VMLifecycleError
from agent_sandbox.lifecycle import is_running, start_vm, stop_vm, wait_for_port


class TestIsRunning:
    def test_port_open(self, config: SandboxConfig) -> None:
        with patch("agent_sandbox.lifecycle.socket.create_connection"):
            assert is_running(config) is True

    def test_port_closed(self, config: SandboxConfig) -> None:
        with patch(
            "agent_sandbox.lifecycle.socket.create_connection",
            side_effect=ConnectionRefusedError(),
        ):
            assert is_running(config) is False

    def test_port_timeout(self, config: SandboxConfig) -> None:
        with patch(
            "agent_sandbox.lifecycle.socket.create_connection",
            side_effect=TimeoutError(),
        ):
            assert is_running(config) is False


class TestStartVm:
    def test_skips_if_already_running(self, config: SandboxConfig) -> None:
        with (
            patch("agent_sandbox.lifecycle.is_running", return_value=True),
            patch("agent_sandbox.lifecycle.subprocess.run") as mock_run,
        ):
            start_vm(config)
            mock_run.assert_not_called()

    def test_calls_script_when_not_running(self, config: SandboxConfig) -> None:
        # Skript muss existieren (sonst FileNotFoundError vor subprocess.run)
        config.script("start-vm.sh").parent.mkdir(parents=True, exist_ok=True)
        # script() prüft is_file – wir mocken den Pfad-Check
        with (
            patch("agent_sandbox.lifecycle.is_running", return_value=False),
            patch.object(type(config.script("start-vm.sh")), "is_file", return_value=True),
            patch(
                "agent_sandbox.lifecycle.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as mock_run,
        ):
            start_vm(config)
            mock_run.assert_called_once()

    def test_raises_on_script_failure(self, config: SandboxConfig) -> None:
        with (
            patch("agent_sandbox.lifecycle.is_running", return_value=False),
            patch.object(type(config.script("start-vm.sh")), "is_file", return_value=True),
            patch(
                "agent_sandbox.lifecycle.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "boom"),
            ),pytest.raises(VMLifecycleError, match="exit 1")
        ):
            start_vm(config)

    def test_raises_when_script_missing(self, config: SandboxConfig, tmp_path) -> None:
        # Config mit nicht-existierendem Repo
        bad_config = SandboxConfig(repo_root=tmp_path)
        with (
            patch("agent_sandbox.lifecycle.is_running", return_value=False),
            pytest.raises(VMLifecycleError, match="nicht gefunden"),
        ):
            start_vm(bad_config)


class TestStopVm:
    def test_logs_warning_on_nonzero_exit(self, config: SandboxConfig, caplog) -> None:
        with (
            patch.object(type(config.script("stop-vm.sh")), "is_file", return_value=True),
            patch(
                "agent_sandbox.lifecycle.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "stale pid"),
            ),
        ):
            stop_vm(config)  # nicht-fatal
        assert any("exit 1" in r.message for r in caplog.records)


class TestWaitForPort:
    def test_returns_immediately_when_open(self, config: SandboxConfig) -> None:
        with patch("agent_sandbox.lifecycle._port_open", return_value=True):
            elapsed = wait_for_port(config)
        assert elapsed < 0.1

    def test_raises_on_timeout(self, config: SandboxConfig) -> None:
        # config.boot_timeout_s = 2.0 in der Fixture
        with (
            patch("agent_sandbox.lifecycle._port_open", return_value=False),
            pytest.raises(SandboxTimeoutError, match="nicht innerhalb"),
        ):
            wait_for_port(config, timeout_s=0.2)
