"""Tests for settings loading, precedence and redaction."""

import tomllib

import pytest

from fleet_usage.config import (
    CONFIG_ENV_VAR,
    REDACTED,
    ConfigError,
    load_settings,
    redacted_dict,
    render_settings_toml,
    resolve_settings_path,
    resolve_token,
)

MACHINE_ID = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'

FULL = """
schema_version = 1

[machine]
id = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'
label = 'fedora-mobile'

[github]
repository = 'octo/fleet-usage-data'
branch = 'main'
token_env = 'FLEET_USAGE_GITHUB_TOKEN'
api_base_url = 'https://api.github.com'

[collector]
command = ['bunx', 'ccusage@20.0.20']
version = '20.0.20'
agents = ['claude', 'codex']
timezone = 'Europe/Berlin'
timeout_seconds = 180
offline_pricing = true

[ledger]
freeze_window_days = 5

[sync]
interval_minutes = 60

[schedule]
backend = 'auto'

[report]
default_period = 'month'
stale_after_hours = 3
"""

VIEWER = """
schema_version = 1

[github]
repository = 'octo/fleet-usage-data'
"""


def write_settings(directory, body=FULL, name='settings.toml'):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding='utf-8')
    return path


def test_load_full_settings(tmp_path):
    path = write_settings(tmp_path)
    settings = load_settings(path)
    assert settings.machine is not None
    assert settings.machine.id == MACHINE_ID
    assert settings.collector is not None
    assert settings.collector.command == ['bunx', 'ccusage@20.0.20']
    assert settings.collector.timezone == 'Europe/Berlin'
    assert settings.github.repository == 'octo/fleet-usage-data'
    assert settings.ledger.freeze_window_days == 5
    assert settings.sync.interval_minutes == 60
    assert settings.schedule.backend == 'auto'
    assert settings.report.stale_after_hours == 3
    assert settings.source_path == path
    assert settings.viewer_only is False


def test_viewer_only_settings(tmp_path):
    path = write_settings(tmp_path, VIEWER)
    settings = load_settings(path)
    assert settings.machine is None
    assert settings.collector is None
    assert settings.viewer_only is True
    assert settings.timezone == 'Europe/Berlin'


def test_missing_file_is_actionable(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(tmp_path / 'nope.toml')
    message = str(excinfo.value)
    assert 'nope.toml' in message
    assert 'fleet-usage init' in message


def test_malformed_toml_is_actionable(tmp_path):
    path = write_settings(tmp_path, 'this is = not = toml\n')
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    assert 'not valid TOML' in str(excinfo.value)


def test_validation_error_names_the_key(tmp_path):
    path = write_settings(tmp_path, "[github]\nrepository = 'not-a-repo'\n")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    message = str(excinfo.value)
    assert 'github.repository' in message
    assert 'OWNER/NAME' in message


def test_unknown_key_is_rejected(tmp_path):
    path = write_settings(
        tmp_path,
        "[github]\nrepository = 'o/n'\ntyop = 1\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    message = str(excinfo.value)
    assert 'github.tyop' in message
    assert 'not permitted' in message


def test_unknown_timezone_is_rejected(tmp_path):
    body = FULL.replace("'Europe/Berlin'", "'Mars/Olympus'")
    path = write_settings(tmp_path, body)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    assert 'timezone' in str(excinfo.value)


@pytest.mark.parametrize(
    'machine_id',
    ['..', '.', 'a/b', 'has space', 'x' * 65, 'sn@pshot'],
)
def test_a_dangerous_machine_id_is_rejected(tmp_path, machine_id):
    # The id becomes a path segment below snapshots/ and machines/ in
    # the data repository.
    body = FULL.replace(f"'{MACHINE_ID}'", f"'{machine_id}'")
    path = write_settings(tmp_path, body)
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    assert 'machine.id' in str(excinfo.value)


@pytest.mark.parametrize(
    'machine_id', [MACHINE_ID, 'box-1', 'a.b_c', 'x' * 64]
)
def test_a_plain_machine_id_is_accepted(tmp_path, machine_id):
    body = FULL.replace(f"'{MACHINE_ID}'", f"'{machine_id}'")
    settings = load_settings(write_settings(tmp_path, body))
    assert settings.machine is not None
    assert settings.machine.id == machine_id


def test_settings_path_precedence(tmp_path, monkeypatch, app_paths):
    explicit = write_settings(tmp_path / 'explicit')
    from_env = write_settings(tmp_path / 'env')
    monkeypatch.setenv(CONFIG_ENV_VAR, str(from_env))
    assert resolve_settings_path(explicit) == explicit
    assert resolve_settings_path(None) == from_env
    monkeypatch.delenv(CONFIG_ENV_VAR)
    assert resolve_settings_path(None) == app_paths.settings_file


def test_process_env_beats_dotenv(tmp_path, monkeypatch):
    path = write_settings(tmp_path)
    (tmp_path / '.env').write_text(
        'FLEET_USAGE_GITHUB_TOKEN=from-dotenv\n', encoding='utf-8'
    )
    settings = load_settings(path)
    monkeypatch.delenv('FLEET_USAGE_GITHUB_TOKEN', raising=False)
    assert resolve_token(settings) == 'from-dotenv'
    monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', 'from-process')
    assert resolve_token(settings) == 'from-process'


def test_dotenv_is_read_next_to_settings_only(
    tmp_path, monkeypatch, app_paths
):
    config_dir = tmp_path / 'config'
    path = write_settings(config_dir)
    workdir = tmp_path / 'work'
    workdir.mkdir()
    (workdir / '.env').write_text(
        'FLEET_USAGE_GITHUB_TOKEN=from-cwd\n', encoding='utf-8'
    )
    monkeypatch.chdir(workdir)
    monkeypatch.delenv('FLEET_USAGE_GITHUB_TOKEN', raising=False)
    settings = load_settings(path)
    assert settings.env_values == {}
    assert resolve_token(settings) is None


def test_blank_token_counts_as_absent(tmp_path, monkeypatch):
    path = write_settings(tmp_path)
    monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', '   ')
    assert resolve_token(load_settings(path)) is None


def test_redaction_masks_token_keys(tmp_path):
    path = write_settings(tmp_path)
    (tmp_path / '.env').write_text(
        'FLEET_USAGE_GITHUB_TOKEN=super-secret\n', encoding='utf-8'
    )
    settings = load_settings(path)
    payload = redacted_dict(settings)
    assert payload['github']['token_env'] == REDACTED
    assert 'super-secret' not in repr(payload)
    assert 'source_path' not in payload
    assert 'env_values' not in payload
    assert payload['machine']['id'] == MACHINE_ID


def test_render_settings_toml_is_parsable():
    document = render_settings_toml(
        machine_id=MACHINE_ID,
        label='fedora-mobile',
        repository='octo/fleet-usage-data',
        collector_command=['bunx', 'ccusage@20.0.20'],
        collector_version='20.0.20',
        agents=['claude', 'codex'],
    )
    parsed = tomllib.loads(document)
    assert parsed['machine']['id'] == MACHINE_ID
    assert parsed['collector']['command'] == ['bunx', 'ccusage@20.0.20']
    assert parsed['report']['default_period'] == 'month'


def test_render_settings_toml_viewer_only():
    document = render_settings_toml(
        machine_id=None,
        label=None,
        repository='octo/fleet-usage-data',
        collector_command=None,
    )
    parsed = tomllib.loads(document)
    assert 'machine' not in parsed
    assert 'collector' not in parsed
    assert parsed['github']['repository'] == 'octo/fleet-usage-data'
