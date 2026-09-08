"""systemd unit rendering, quoting and timer lifecycle."""

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from fleet_usage.scheduling.base import (
    ALLOWED_INTERVALS,
    LaunchSpec,
    RunResult,
    SchedulerError,
    oncalendar_expression,
)
from fleet_usage.scheduling.systemd import (
    SERVICE_NAME,
    TIMER_NAME,
    SystemdScheduler,
    render_environment,
    render_service,
    render_timer,
    unit_directory,
)

OFFSET = 7
AWKWARD = '/etc/fleet conf/set"tings 100%.toml'


def make_spec(tmp_path, interval=60, settings=None, extra_path_dirs=()):
    settings_path = (
        settings if settings is not None else str(tmp_path / 'settings.toml')
    )
    return LaunchSpec(
        executable=Path('/opt/bin/fleet-usage'),
        args=('--config', settings_path, 'publish', '--if-due'),
        settings_path=Path(settings_path),
        interval_minutes=interval,
        log_dir=tmp_path / 'logs',
        extra_path_dirs=tuple(extra_path_dirs),
    )


class FakeSystemctl:
    """Records ``systemctl`` and ``loginctl`` calls, replays results."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, argv, *, stdin=None):
        key = tuple(argv)
        self.calls.append(key)
        for prefix, result in self.results.items():
            if key[: len(prefix)] == prefix:
                return result
        return RunResult(returncode=0)

    @staticmethod
    def which(name):
        return f'/usr/bin/{name}'


def make_scheduler(tmp_path, fake=None, env=None, **spec_kwargs):
    fake = fake or FakeSystemctl()
    environment = {
        'XDG_CONFIG_HOME': str(tmp_path / 'config'),
        'XDG_RUNTIME_DIR': '/run/user/1000',
        'USER': 'stebix',
    }
    environment.update(env or {})
    return SystemdScheduler(
        make_spec(tmp_path, **spec_kwargs),
        runner=fake,
        which=fake.which,
        minute_offset=OFFSET,
        env=environment,
    )


# ------------------------------------------------------------- rendering


def test_unit_directory_honours_xdg(tmp_path):
    env = {'XDG_CONFIG_HOME': str(tmp_path / 'cfg')}
    assert unit_directory(env) == tmp_path / 'cfg' / 'systemd' / 'user'


def test_unit_directory_default(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert unit_directory({}) == tmp_path / '.config' / 'systemd' / 'user'


def test_service_unit_contents(tmp_path):
    text = render_service(make_spec(tmp_path))
    assert '[Service]' in text
    assert 'Type=oneshot' in text
    assert (
        f'ExecStart=/opt/bin/fleet-usage --config '
        f'{tmp_path / "settings.toml"} publish --if-due'
    ) in text
    log = tmp_path / 'logs' / 'systemd.log'
    assert f'StandardOutput=append:{log}' in text
    assert f'StandardError=append:{log}' in text
    assert text.endswith('\n')


def test_service_unit_quotes_spaces_quotes_and_percent(tmp_path):
    text = render_service(make_spec(tmp_path, settings=AWKWARD))
    exec_line = next(
        line for line in text.splitlines() if line.startswith('ExecStart=')
    )
    assert exec_line == (
        'ExecStart=/opt/bin/fleet-usage --config '
        '"/etc/fleet conf/set\\"tings 100%%.toml" publish --if-due'
    )


def test_timer_unit_contents(tmp_path):
    text = render_timer(make_spec(tmp_path, interval=120), OFFSET)
    assert 'OnCalendar=*-*-* 00/2:07:00' in text
    assert 'Persistent=true' in text
    assert 'RandomizedDelaySec=120' in text
    assert f'Unit={SERVICE_NAME}' in text
    assert '[Install]\nWantedBy=timers.target\n' in text


@pytest.mark.parametrize(
    ('interval', 'calendar'),
    [
        (15, '*-*-* *:07/15:00'),
        (60, '*-*-* *:07:00'),
        (1440, '*-*-* 03:07:00'),
    ],
)
def test_timer_calendar_table(tmp_path, interval, calendar):
    text = render_timer(make_spec(tmp_path, interval=interval), OFFSET)
    assert f'OnCalendar={calendar}\n' in text


# --------------------------------------------------------- collector PATH

BUN_DIR = '/home/me/.bun/bin'
BUN_PATH = (
    'Environment=PATH=/home/me/.bun/bin:/usr/local/sbin:/usr/local/bin:'
    '/usr/sbin:/usr/bin'
)


def test_no_environment_line_without_extra_directories(tmp_path):
    assert render_environment(make_spec(tmp_path)) == ''
    assert 'Environment=' not in render_service(make_spec(tmp_path))


def test_service_unit_carries_the_collector_directory(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=(BUN_DIR,))
    assert render_environment(spec) == f'{BUN_PATH}\n'
    text = render_service(spec)
    lines = text.splitlines()
    assert BUN_PATH in lines
    # The directive belongs to [Service] and precedes the command.
    assert lines.index('[Service]') < lines.index(BUN_PATH)
    assert lines.index(BUN_PATH) < lines.index(
        next(line for line in lines if line.startswith('ExecStart='))
    )


def test_a_standard_directory_is_never_repeated(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=('/usr/bin',))
    assert render_environment(spec) == (
        'Environment=PATH=/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin\n'
    )


def test_environment_line_quotes_a_directory_with_a_space(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=('/home/me/my tools/bin',))
    line = render_environment(spec).rstrip('\n')
    assert line.startswith('Environment="PATH=/home/me/my tools/bin:')
    assert line.endswith('/usr/bin"')


def test_installed_unit_file_holds_the_environment_line(tmp_path):
    scheduler = make_scheduler(tmp_path, extra_path_dirs=(BUN_DIR,))
    scheduler.install(60)
    assert BUN_PATH in scheduler.service_path.read_text()


# ------------------------------------------------------------- lifecycle


def test_install_writes_units_and_enables_the_timer(tmp_path):
    fake = FakeSystemctl()
    scheduler = make_scheduler(tmp_path, fake)
    report = scheduler.install(60)
    assert scheduler.service_path.is_file()
    assert scheduler.timer_path.is_file()
    assert (tmp_path / 'logs').is_dir()
    assert ('systemctl', '--user', 'daemon-reload') in fake.calls
    assert (
        'systemctl',
        '--user',
        'enable',
        '--now',
        TIMER_NAME,
    ) in fake.calls
    assert str(scheduler.timer_path) in report


def test_install_is_idempotent(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.install(60)
    first = scheduler.timer_path.read_text()
    scheduler.install(60)
    assert scheduler.timer_path.read_text() == first


def test_dry_run_writes_nothing(tmp_path):
    fake = FakeSystemctl()
    scheduler = make_scheduler(tmp_path, fake)
    report = scheduler.install(60, dry_run=True)
    assert report.startswith('dry run:')
    assert 'OnCalendar=*-*-* *:07:00' in report
    assert 'Type=oneshot' in report
    assert not scheduler.timer_path.exists()
    assert fake.calls == []


def test_install_reports_a_failing_systemctl(tmp_path):
    fake = FakeSystemctl(
        {
            ('systemctl', '--user', 'enable'): RunResult(
                returncode=1, stderr='Failed to enable unit'
            )
        }
    )
    scheduler = make_scheduler(tmp_path, fake)
    with pytest.raises(SchedulerError) as excinfo:
        scheduler.install(60)
    assert 'Failed to enable unit' in str(excinfo.value)


def test_status_when_nothing_is_installed(tmp_path):
    fake = FakeSystemctl(
        {('systemctl', '--user', 'show'): RunResult(0, 'ActiveState=inactive')}
    )
    scheduler = make_scheduler(tmp_path, fake)
    status = scheduler.status()
    assert status.installed is False
    assert status.backend == 'systemd'


def test_status_when_the_timer_is_active(tmp_path):
    fake = FakeSystemctl(
        {
            ('systemctl', '--user', 'show'): RunResult(
                0,
                'ActiveState=active\n'
                'NextElapseUSecRealtime=Mon 2026-09-08 14:07:00 CEST\n'
                'LastTriggerUSec=Mon 2026-09-08 13:07:00 CEST\n',
            ),
            ('systemctl', '--user', 'list-timers'): RunResult(
                0, 'NEXT  LEFT  LAST  PASSED  UNIT  ACTIVATES\n'
            ),
            ('loginctl',): RunResult(0, 'Linger=yes\n'),
        }
    )
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(120)
    status = scheduler.status()
    assert status.installed is True
    assert status.expression == '*-*-* 00/2:07:00'
    assert status.command.startswith('/opt/bin/fleet-usage')
    assert 'ActiveState: active' in status.detail
    assert 'NextElapseUSecRealtime' in status.detail
    assert status.notes == ()


def test_status_warns_when_linger_is_off(tmp_path):
    fake = FakeSystemctl(
        {
            ('systemctl', '--user', 'show'): RunResult(
                0, 'ActiveState=active'
            ),
            ('loginctl',): RunResult(0, 'Linger=no\n'),
        }
    )
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    status = scheduler.status()
    assert any('linger is not enabled' in note for note in status.notes)
    assert 'loginctl enable-linger stebix' in status.notes[0]
    assert 'note: linger is not enabled' in status.render()


def test_status_without_a_user_session(tmp_path):
    fake = FakeSystemctl()
    scheduler = SystemdScheduler(
        make_spec(tmp_path),
        runner=fake,
        which=lambda name: None,
        minute_offset=OFFSET,
        env={'XDG_CONFIG_HOME': str(tmp_path / 'config')},
    )
    assert scheduler.available() is False
    assert scheduler.status().installed is False


def test_available_needs_a_bus(tmp_path):
    fake = FakeSystemctl()
    scheduler = SystemdScheduler(
        make_spec(tmp_path),
        runner=fake,
        which=fake.which,
        env={'XDG_CONFIG_HOME': str(tmp_path)},
    )
    assert scheduler.available() is False
    with_bus = SystemdScheduler(
        make_spec(tmp_path),
        runner=fake,
        which=fake.which,
        env={
            'XDG_CONFIG_HOME': str(tmp_path),
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
        },
    )
    assert with_bus.available() is True


def test_uninstall_disables_and_removes(tmp_path):
    fake = FakeSystemctl()
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    report = scheduler.uninstall()
    assert not scheduler.timer_path.exists()
    assert not scheduler.service_path.exists()
    assert (
        'systemctl',
        '--user',
        'disable',
        '--now',
        TIMER_NAME,
    ) in fake.calls
    assert 'removed' in report


def test_uninstall_when_nothing_is_installed(tmp_path):
    scheduler = make_scheduler(tmp_path)
    assert 'nothing to do' in scheduler.uninstall()
    assert 'no unit files' in scheduler.uninstall(dry_run=True)


def test_uninstall_dry_run_keeps_the_files(tmp_path):
    scheduler = make_scheduler(tmp_path)
    scheduler.install(60)
    report = scheduler.uninstall(dry_run=True)
    assert report.startswith('dry run:')
    assert scheduler.timer_path.exists()


# ---------------------------------------------- verified against systemd

SYSTEMD_ANALYZE = shutil.which('systemd-analyze')
needs_systemd = pytest.mark.skipif(
    SYSTEMD_ANALYZE is None,
    reason='systemd-analyze is not installed',
)


@needs_systemd
@pytest.mark.parametrize('interval', ALLOWED_INTERVALS)
def test_oncalendar_is_accepted_by_systemd(interval):
    """systemd itself must understand every expression we generate."""
    expression = oncalendar_expression(interval, OFFSET)
    completed = subprocess.run(
        [SYSTEMD_ANALYZE, 'calendar', expression],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert f'Normalized form: {expression}' in completed.stdout


@needs_systemd
def test_units_are_accepted_by_systemd(tmp_path):
    """A settings path with a space, a quote and a percent still parses."""
    program = tmp_path / 'bin' / 'fleet-usage'
    program.parent.mkdir()
    program.write_text('#!/bin/sh\nexit 0\n')
    program.chmod(0o755)
    spec = dataclasses.replace(
        make_spec(
            tmp_path,
            interval=120,
            settings=AWKWARD,
            extra_path_dirs=('/home/me/my tools/bin',),
        ),
        executable=program,
    )
    units = tmp_path / 'units'
    units.mkdir()
    (units / SERVICE_NAME).write_text(render_service(spec))
    (units / TIMER_NAME).write_text(render_timer(spec, OFFSET))
    completed = subprocess.run(
        [
            SYSTEMD_ANALYZE,
            'verify',
            '--user',
            str(units / SERVICE_NAME),
            str(units / TIMER_NAME),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
