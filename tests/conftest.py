"""Pytest-Fixtures.

Wir trennen Unit- und Integration-Tests:
  - Unit:    laufen immer, mocken paramiko/subprocess
  - Integration: laufen nur mit `pytest -m integration`, brauchen echte VM
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_sandbox import SandboxConfig


@pytest.fixture(scope="module")
def repo_root() -> Path:
    """Pfad zum Repo (Tests laufen vom Repo aus)."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def config(repo_root: Path, tmp_path: Path) -> SandboxConfig:
    """Default-Config für Tests. Nutzt das echte Repo aber Temp-Keys."""
    # Wir nutzen einen Fake-Keypath; Unit-Tests sollen Connect mocken,
    # also reicht es dass der Pfad nicht existiert (oder existieren wir
    # legen ein leeres File an wenn der Test es braucht).
    return SandboxConfig(
        repo_root=repo_root,
        ssh_key_path=tmp_path / "fake_key",
        boot_timeout_s=2.0,  # Unit-Tests sollen schnell timeoutten
        cloud_init_timeout_s=2.0,
        command_timeout_s=2.0,
        connect_retry_interval_s=0.05,
    )


@pytest.fixture(scope="module")
def integration_config(repo_root: Path) -> SandboxConfig:
    """Real-Config für Integration-Tests. Nutzt echten Key vom Repo."""
    return SandboxConfig(
        repo_root=repo_root,
        # Default ssh_key_path = vm/ssh_key wird genutzt
        auto_start=False,  # Integration-Tests gehen davon aus dass VM läuft
    )
