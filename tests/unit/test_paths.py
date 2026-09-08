"""Tests for the platform path layout."""

from pathlib import Path

from fleet_usage import paths as paths_module


def test_paths_are_all_distinct(app_paths):
    values = list(app_paths.as_dict().values())
    assert len(set(values)) == len(values)


def test_expected_layout(app_paths):
    assert app_paths.settings_file.parent == app_paths.config_dir
    assert app_paths.settings_file.name == 'settings.toml'
    assert app_paths.env_file.name == '.env'
    assert app_paths.spool_dir.parent == app_paths.state_dir
    assert app_paths.lock_file.parent == app_paths.state_dir
    assert app_paths.due_marker.parent == app_paths.state_dir
    assert app_paths.ledger_cache_dir.parent == app_paths.cache_dir


def test_all_paths_are_absolute(app_paths):
    for value in app_paths.as_dict().values():
        assert isinstance(value, Path)
        assert value.is_absolute()


def test_real_platformdirs_layout_is_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'c'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 's'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'k'))
    values = list(paths_module.get_paths().as_dict().values())
    assert len(set(values)) == len(values)
    assert all('fleet-usage' in str(value) for value in values)


def test_state_dir_is_separated_when_bases_collide(monkeypatch, tmp_path):
    class Collapsed:
        user_config_dir = str(tmp_path / 'app')
        user_state_dir = str(tmp_path / 'app')
        user_cache_dir = str(tmp_path / 'app' / 'Cache')
        user_log_dir = str(tmp_path / 'app' / 'Logs')

    monkeypatch.setattr(
        paths_module, 'PlatformDirs', lambda **kwargs: Collapsed()
    )
    paths = paths_module.get_paths()
    assert paths.state_dir != paths.config_dir
    values = list(paths.as_dict().values())
    assert len(set(values)) == len(values)
