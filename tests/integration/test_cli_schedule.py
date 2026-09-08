"""``fleet-usage schedule`` end to end, with a fake scheduler runner."""

from pathlib import Path

import pytest

from fleet_usage.commands import schedule as schedule_cmd
from fleet_usage.exit_codes import ExitCode
from fleet_usage.scheduling.base import RunResult
from fleet_usage.scheduling.cron import BEGIN_MARKER, find_block

SETTINGS = """schema_version = 1

[machine]
id = '11111111-2222-3333-4444-555555555555'
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

VIEWER_SETTINGS = """schema_version = 1

[github]
repository = 'octo/fleet-usage-data'

[sync]
interval_minutes = 60
"""


class FakeCron:
    """A ``crontab`` that lives in memory and records every call."""

    def __init__(self):
        self.text = None
        self.calls = []

    def __call__(self, argv, *, stdin=None):
        key = tuple(argv)
        self.calls.append((key, stdin))
        if key == ('crontab', '-l'):
            if self.text is None:
                return RunResult(returncode=1, stderr='no crontab for tester')
            return RunResult(returncode=0, stdout=self.text)
        if key == ('crontab', '-'):
            self.text = stdin
            return RunResult(returncode=0)
        return RunResult(returncode=0)


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / 'settings.toml'
    path.write_text(SETTINGS, encoding='utf-8')
    return path


@pytest.fixture
def viewer_settings_file(tmp_path):
    path = tmp_path / 'viewer.toml'
    path.write_text(VIEWER_SETTINGS, encoding='utf-8')
    return path


@pytest.fixture
def fake_cron(tmp_path, monkeypatch):
    """Point the schedule command at a fake crontab and executable."""
    program = tmp_path / 'opt' / 'bin' / 'fleet-usage'
    program.parent.mkdir(parents=True)
    program.write_text('#!/bin/sh\n')
    fake = FakeCron()
    monkeypatch.setattr(schedule_cmd, 'RUNNER', fake)
    monkeypatch.setattr(schedule_cmd, 'EXECUTABLE', program)
    monkeypatch.setattr(
        'shutil.which',
        lambda name, *args, **kwargs: (
            '/usr/bin/crontab' if name == 'crontab' else None
        ),
    )
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    fake.program = program
    return fake


def test_install_writes_the_managed_block(
    invoke, app_paths, settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert fake_cron.text is not None
    block = find_block(fake_cron.text)
    assert block is not None
    job = block.splitlines()[1]
    assert job.startswith('7 * * * * ') or ' * * * * ' in job
    assert str(fake_cron.program) in job
    assert f'--config {settings_file}' in job
    assert 'publish --if-due' in job
    assert 'cron.log 2>&1' in job
    # The report and the resulting status are both printed.
    assert BEGIN_MARKER in result.output
    assert 'backend: cron' in result.output
    assert 'state: installed' in result.output


def test_install_honours_the_interval_option(
    invoke, app_paths, settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
        '--interval',
        '120',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert '*/2 * * *' in find_block(fake_cron.text)


def test_install_rejects_an_unschedulable_interval(
    invoke, app_paths, settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
        '--interval',
        '90',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'cannot be scheduled' in result.output
    assert '15, 30, 60' in result.output
    assert fake_cron.text is None


def test_install_is_idempotent(invoke, app_paths, settings_file, fake_cron):
    first = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert first.exit_code == ExitCode.OK, first.output
    after = fake_cron.text
    second = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert second.exit_code == ExitCode.OK, second.output
    assert fake_cron.text == after


def test_dry_run_touches_nothing(invoke, app_paths, settings_file, fake_cron):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
        '--dry-run',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert 'dry run' in result.output
    assert BEGIN_MARKER in result.output
    assert fake_cron.text is None
    assert not any(call == ('crontab', '-') for call, _ in fake_cron.calls)


def test_status_before_and_after_install(
    invoke, app_paths, settings_file, fake_cron
):
    before = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'status',
        '--backend',
        'cron',
    )
    assert before.exit_code == ExitCode.OK, before.output
    assert 'state: not installed' in before.output

    invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    after = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'status',
        '--backend',
        'cron',
    )
    assert after.exit_code == ExitCode.OK, after.output
    assert 'state: installed' in after.output
    assert 'schedule: ' in after.output


def test_uninstall_removes_the_block(
    invoke, app_paths, settings_file, fake_cron
):
    invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'uninstall',
        '--backend',
        'cron',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert find_block(fake_cron.text) is None
    assert 'removed' in result.output


def test_uninstall_dry_run_keeps_the_block(
    invoke, app_paths, settings_file, fake_cron
):
    invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    before = fake_cron.text
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'uninstall',
        '--backend',
        'cron',
        '--dry-run',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert 'dry run' in result.output
    assert fake_cron.text == before


def test_viewer_only_install_is_refused(
    invoke, app_paths, viewer_settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(viewer_settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'viewer-only' in result.output
    assert fake_cron.text is None


def test_missing_settings_is_a_config_error(invoke, app_paths, tmp_path):
    result = invoke(
        '--config',
        str(tmp_path / 'nope.toml'),
        'schedule',
        'install',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output


def test_temporary_executable_is_refused(
    invoke, app_paths, settings_file, tmp_path, monkeypatch
):
    program = tmp_path / 'ephemeral' / 'fleet-usage'
    program.parent.mkdir(parents=True)
    program.write_text('')
    fake = FakeCron()
    monkeypatch.setattr(schedule_cmd, 'RUNNER', fake)
    monkeypatch.setattr(schedule_cmd, 'EXECUTABLE', None)
    monkeypatch.setattr('sys.executable', str(program.parent / 'python'))
    monkeypatch.setattr('shutil.which', lambda name, *args, **kwargs: None)
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'temporary directory' in result.output
    assert fake.text is None


def test_unresolvable_executable_is_refused(
    invoke, app_paths, settings_file, tmp_path, monkeypatch
):
    fake = FakeCron()
    monkeypatch.setattr(schedule_cmd, 'RUNNER', fake)
    monkeypatch.setattr(schedule_cmd, 'EXECUTABLE', None)
    monkeypatch.setattr(
        'sys.executable', str(tmp_path / 'no-such-venv' / 'python')
    )
    monkeypatch.setattr('sys.argv', ['pytest'])
    monkeypatch.setattr('shutil.which', lambda name, *a, **k: None)
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'cannot locate' in result.output


def test_no_supported_backend(
    invoke, app_paths, settings_file, tmp_path, monkeypatch
):
    program = tmp_path / 'opt' / 'fleet-usage'
    program.parent.mkdir(parents=True)
    program.write_text('')
    monkeypatch.setattr(schedule_cmd, 'RUNNER', FakeCron())
    monkeypatch.setattr(schedule_cmd, 'EXECUTABLE', program)
    monkeypatch.setattr('shutil.which', lambda name, *a, **k: None)
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'no supported scheduler found' in result.output


def test_explicit_windows_backend_is_refused_on_linux(
    invoke, app_paths, settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'windows',
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'not usable' in result.output


def test_logon_mode_option_is_accepted(
    invoke, app_paths, settings_file, fake_cron
):
    result = invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
        '--logon-mode',
        's4u',
        '--dry-run',
    )
    assert result.exit_code == ExitCode.OK, result.output


HELP_TARGETS = [
    ('schedule', '--help'),
    ('schedule', 'install', '--help'),
    ('schedule', 'status', '--help'),
    ('schedule', 'uninstall', '--help'),
]


@pytest.mark.parametrize('args', HELP_TARGETS, ids=lambda a: ' '.join(a))
def test_help(invoke, args):
    result = invoke(*args)
    assert result.exit_code == ExitCode.OK, result.output


def test_log_directory_is_created_under_the_app_paths(
    invoke, app_paths, settings_file, fake_cron
):
    invoke(
        '--config',
        str(settings_file),
        'schedule',
        'install',
        '--backend',
        'cron',
    )
    assert Path(app_paths.log_dir).is_dir()
    assert str(app_paths.log_dir) in fake_cron.text
