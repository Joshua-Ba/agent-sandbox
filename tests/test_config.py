"""Unit-Tests für SandboxConfig."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_sandbox import SandboxConfig
from agent_sandbox.config import _find_repo_root


class TestFindRepoRoot:
    def test_finds_root_from_inside_repo(self, tmp_path: Path) -> None:
        # Fake-Repo bauen
        (tmp_path / "setup.sh").touch()
        (tmp_path / "start-vm.sh").touch()
        deep = tmp_path / "src" / "agent_sandbox"
        deep.mkdir(parents=True)
        nested_file = deep / "config.py"
        nested_file.touch()

        assert _find_repo_root(nested_file) == tmp_path

    def test_raises_when_not_in_repo(self, tmp_path: Path) -> None:
        lonely_file = tmp_path / "lonely.py"
        lonely_file.touch()
        with pytest.raises(FileNotFoundError, match="Repo-Root"):
            _find_repo_root(lonely_file)


class TestSandboxConfig:
    def test_defaults(self, repo_root: Path) -> None:
        config = SandboxConfig(repo_root=repo_root)
        assert config.ssh_host == "127.0.0.1"
        assert config.ssh_port == 2222
        assert config.ssh_user == "agent"
        assert config.auto_start is True
        assert config.wait_for_cloud_init is False

    def test_absolute_ssh_key_path_relative(self, repo_root: Path) -> None:
        config = SandboxConfig(repo_root=repo_root, ssh_key_path=Path("vm/ssh_key"))
        assert config.absolute_ssh_key_path() == repo_root / "vm" / "ssh_key"

    def test_absolute_ssh_key_path_already_absolute(self, repo_root: Path) -> None:
        absolute = Path("/tmp/some_key")
        config = SandboxConfig(repo_root=repo_root, ssh_key_path=absolute)
        assert config.absolute_ssh_key_path() == absolute

    def test_script_path(self, repo_root: Path) -> None:
        config = SandboxConfig(repo_root=repo_root)
        assert config.script("start-vm.sh") == repo_root / "start-vm.sh"

    def test_is_frozen(self, repo_root: Path) -> None:
        config = SandboxConfig(repo_root=repo_root)
        with pytest.raises(AttributeError):
            config.ssh_port = 9999  # type: ignore[misc]
