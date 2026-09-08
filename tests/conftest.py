"""Shared fixtures for the fleet-usage test suite."""

import dataclasses
from pathlib import Path

import pytest

from fleet_usage import paths as paths_module
from fleet_usage.paths import AppPaths

FIXTURE_DIR = Path(__file__).parent / 'fixtures'
CCUSAGE_FIXTURES = FIXTURE_DIR / 'ccusage-20.0.20'


@dataclasses.dataclass(frozen=True)
class FakePlatformDirs:
    """Stand-in for platformdirs.PlatformDirs rooted at a temp dir."""

    root: Path

    @property
    def user_config_dir(self) -> str:
        return str(self.root / 'config')

    @property
    def user_state_dir(self) -> str:
        return str(self.root / 'state')

    @property
    def user_cache_dir(self) -> str:
        return str(self.root / 'cache')

    @property
    def user_log_dir(self) -> str:
        return str(self.root / 'log')


@pytest.fixture
def app_paths(tmp_path, monkeypatch) -> AppPaths:
    """Redirect every application path into a temporary directory."""
    root = tmp_path / 'appdirs'

    def factory(**kwargs):
        return FakePlatformDirs(root=root)

    monkeypatch.setattr(paths_module, 'PlatformDirs', factory)
    monkeypatch.delenv('FLEET_USAGE_CONFIG', raising=False)
    monkeypatch.delenv('FLEET_USAGE_GITHUB_TOKEN', raising=False)
    return paths_module.get_paths()


@pytest.fixture
def ccusage_fixture_dir() -> Path:
    """Directory holding the recorded ccusage 20.0.20 output."""
    return CCUSAGE_FIXTURES
