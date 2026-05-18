"""Unit-Tests für SandboxVM (Logik ohne echtes SSH).

Wir testen primär das Command-Wrapping (cwd/env) und CommandResult.
Die SSH/SFTP-Interaktion bleibt Integration-Tests vorbehalten – Mocking von
paramikos Transport wäre fragiler als Wert.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from agent_sandbox import CommandResult, SandboxConfig, SandboxVM


class TestCommandResult:
    def test_ok_when_exit_zero(self) -> None:
        r = CommandResult("ls", 0, "out", "", 0.1)
        assert r.ok is True

    def test_not_ok_when_exit_nonzero(self) -> None:
        r = CommandResult("ls", 1, "", "err", 0.1)
        assert r.ok is False

    def test_frozen(self) -> None:
        r = CommandResult("ls", 0, "", "", 0.1)
        with pytest.raises(AttributeError):
            r.exit_code = 99  # type: ignore[misc]


class TestWrapCommand:
    def test_plain_command_unchanged(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        assert vm._wrap_command("ls -la", cwd=None, env=None) == "ls -la"

    def test_cwd_prefix(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command("ls", cwd="/tmp", env=None)
        assert wrapped == "cd -- /tmp && ls"

    def test_cwd_with_spaces_gets_quoted(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command("ls", cwd="/path with spaces", env=None)
        assert wrapped == "cd -- '/path with spaces' && ls"

    def test_env_simple(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command("echo $FOO", cwd=None, env={"FOO": "bar"})
        assert wrapped == "export FOO=bar; echo $FOO"

    def test_env_values_quoted(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command("printenv", cwd=None, env={"MSG": "hello world"})
        assert wrapped == "export MSG='hello world'; printenv"

    def test_env_with_special_chars_quoted(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command(
            "true", cwd=None, env={"X": "a$b`c;d|e"}
        )
        # shlex.quote sollte alles einpacken
        assert "export X='a$b`c;d|e';" in wrapped

    def test_cwd_and_env_together(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        wrapped = vm._wrap_command(
            "make", cwd="/work", env={"DEBUG": "1"}
        )
        # env zuerst (export), dann cwd, dann command
        assert wrapped == "export DEBUG=1; cd -- /work && make"

    def test_invalid_env_key_with_equals(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        with pytest.raises(ValueError, match="Invalid env var name"):
            vm._wrap_command("true", cwd=None, env={"BAD=NAME": "value"})

    def test_invalid_env_key_with_space(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        with pytest.raises(ValueError, match="Invalid env var name"):
            vm._wrap_command("true", cwd=None, env={"BAD NAME": "value"})

    def test_invalid_env_key_empty(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        with pytest.raises(ValueError, match="Invalid env var name"):
            vm._wrap_command("true", cwd=None, env={"": "value"})

    def test_invalid_env_with_null(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        with pytest.raises(ValueError, match="Invalid env var value"):
            vm._wrap_command("true", cwd=None, env={"KEY": "val\0ue"})


class TestEnsureConnected:
    def test_raises_when_no_client(self, config: SandboxConfig) -> None:
        from agent_sandbox.errors import ConnectionError
        vm = SandboxVM(config)
        with pytest.raises(ConnectionError, match="Keine SSH-Verbindung"):
            vm._ensure_connected()

    def test_reconnects_when_transport_dead(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        # Fake-Client mit totem Transport
        dead_client = MagicMock()
        dead_client.get_transport.return_value.is_active.return_value = False
        vm._client = dead_client

        with patch.object(vm, "_connect") as mock_connect:
            # _connect setzt _client neu; wir simulieren das
            def reconnect_side_effect() -> None:
                vm._client = MagicMock()
                vm._client.get_transport.return_value.is_active.return_value = True
            mock_connect.side_effect = reconnect_side_effect

            vm._ensure_connected()
            mock_connect.assert_called_once()


class TestContextManager:
    def test_close_called_on_exit(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        with patch.object(vm, "start"), patch.object(vm, "close") as mock_close:
            with vm:
                pass
            mock_close.assert_called_once()

    def test_close_idempotent(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        vm.close()  # nichts zu schließen, sollte nicht werfen
        vm.close()  # nochmal, auch ok


class TestRunRaw:
    def test_returns_raw_bytes(self, config: SandboxConfig) -> None:
        vm = SandboxVM(config)
        # _exec_raw direkt mocken – wir testen nur das Wrapping
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with patch.object(
            vm, "_exec_raw", return_value=(png_bytes, b"", 0, 0.05)
        ):
            stdout, stderr, exit_code = vm.run_raw("scrot -o /dev/stdout")
        assert stdout == png_bytes
        assert stderr == b""
        assert exit_code == 0

    def test_check_raises_on_failure(self, config: SandboxConfig) -> None:
        from agent_sandbox import CommandError
        vm = SandboxVM(config)
        with patch.object(
            vm, "_exec_raw", return_value=(b"", b"boom", 1, 0.01)
        ), pytest.raises(CommandError) as exc_info:
            vm.run_raw("false", check=True)
        assert exc_info.value.exit_code == 1
        assert "boom" in exc_info.value.stderr


class TestScreenshot:
    def test_returns_pil_image_for_valid_png(self, config: SandboxConfig) -> None:
        from PIL import Image

        # Echtes kleines PNG generieren (32x32 rot)
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), color="red").save(buf, format="PNG")
        png_bytes = buf.getvalue()

        vm = SandboxVM(config)
        with patch.object(vm, "run_raw", return_value=(png_bytes, b"", 0)):
            img = vm.screenshot()

        assert img.size == (32, 32)
        assert img.mode == "RGB"

    def test_raises_on_empty_output(self, config: SandboxConfig) -> None:
        from agent_sandbox import ScreenshotError
        vm = SandboxVM(config)
        with (
            patch.object(vm, "run_raw", return_value=(b"", b"", 0)),
            pytest.raises(ScreenshotError, match="0 Bytes"),
        ):
            vm.screenshot()

    def test_raises_on_corrupt_png(self, config: SandboxConfig) -> None:
        from agent_sandbox import ScreenshotError
        vm = SandboxVM(config)
        # garbage statt PNG-Magic-Bytes
        with (
            patch.object(vm, "run_raw", return_value=(b"not a png", b"", 0)),
            pytest.raises(ScreenshotError, match="dekodiert"),
        ):
            vm.screenshot()