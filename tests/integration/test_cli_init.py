"""Tests for ``fleet-usage init``."""

import os
import tomllib

from fleet_usage.exit_codes import ExitCode


def read(path):
    return tomllib.loads(path.read_text(encoding='utf-8'))


def test_init_creates_settings_and_env(invoke, app_paths):
    result = invoke('init', '--label', 'lab-1', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.OK, result.output
    assert app_paths.settings_file.is_file()
    parsed = read(app_paths.settings_file)
    assert parsed['machine']['label'] == 'lab-1'
    assert parsed['github']['repository'] == 'octo/data'
    assert parsed['collector']['command']
    assert len(parsed['machine']['id']) == 36
    env_text = app_paths.env_file.read_text(encoding='utf-8')
    assert 'FLEET_USAGE_GITHUB_TOKEN=' in env_text


def test_env_file_is_private_on_posix(invoke, app_paths):
    invoke('init')
    if os.name != 'posix':
        return
    mode = app_paths.env_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_label_defaults_to_hostname(invoke, app_paths, monkeypatch):
    monkeypatch.setattr('socket.gethostname', lambda: 'my-host')
    invoke('init')
    assert read(app_paths.settings_file)['machine']['label'] == 'my-host'


def test_init_refuses_to_overwrite(invoke, app_paths):
    invoke('init', '--label', 'first')
    result = invoke('init', '--label', 'second')
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert 'already exists' in result.output
    assert read(app_paths.settings_file)['machine']['label'] == 'first'


def test_force_preserves_machine_id(invoke, app_paths):
    invoke('init', '--label', 'first')
    first = read(app_paths.settings_file)
    result = invoke('init', '--force', '--label', 'second')
    assert result.exit_code == ExitCode.OK, result.output
    second = read(app_paths.settings_file)
    assert second['machine']['id'] == first['machine']['id']
    assert second['machine']['label'] == 'second'


def test_force_keeps_existing_env_file(invoke, app_paths):
    invoke('init')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=real-token\n', encoding='utf-8'
    )
    result = invoke('init', '--force')
    assert result.exit_code == ExitCode.OK
    assert 'real-token' in app_paths.env_file.read_text(encoding='utf-8')


def test_init_is_idempotent_with_force(invoke, app_paths):
    invoke('init', '--label', 'lab', '--repo', 'octo/data')
    first = app_paths.settings_file.read_text(encoding='utf-8')
    invoke('init', '--force')
    assert app_paths.settings_file.read_text(encoding='utf-8') == first


def test_viewer_only_omits_machine_and_collector(invoke, app_paths):
    result = invoke('init', '--viewer-only', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.OK, result.output
    parsed = read(app_paths.settings_file)
    assert 'machine' not in parsed
    assert 'collector' not in parsed
    assert parsed['github']['repository'] == 'octo/data'


def test_init_honours_config_option(invoke, tmp_path, app_paths):
    target = tmp_path / 'custom' / 'settings.toml'
    result = invoke('--config', str(target), 'init', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.OK, result.output
    assert target.is_file()
    assert (target.parent / '.env').is_file()
    assert not app_paths.settings_file.exists()
