"""Interval tables, quoting, executable lookup and backend selection."""

from pathlib import Path

import pytest

from fleet_usage.scheduling import base
from fleet_usage.scheduling.base import (
    ALLOWED_INTERVALS,
    LaunchSpec,
    RunResult,
    SchedulerError,
    build_launch_spec,
    cron_expression,
    cron_quote,
    machine_minute_offset,
    oncalendar_expression,
    resolve_executable,
    select_backend,
    systemd_quote,
    validate_interval,
)
from fleet_usage.scheduling.cron import CronScheduler
from fleet_usage.scheduling.systemd import SystemdScheduler
from fleet_usage.scheduling.windows import WindowsScheduler

OFFSET = 7


def make_spec(**overrides):
    defaults = {
        'executable': Path('/opt/bin/fleet-usage'),
        'args': (
            '--config',
            '/etc/fleet/settings.toml',
            'publish',
            '--if-due',
        ),
        'settings_path': Path('/etc/fleet/settings.toml'),
        'interval_minutes': 60,
        'log_dir': Path('/var/log/fleet'),
    }
    defaults.update(overrides)
    return LaunchSpec(**defaults)


def fake_runner(results=None, calls=None):
    """Return a runner that replays ``results`` and records calls."""
    table = results or {}
    log = calls if calls is not None else []

    def run(argv, *, stdin=None):
        log.append((tuple(argv), stdin))
        for prefix, result in table.items():
            if tuple(argv)[: len(prefix)] == prefix:
                return result
        return RunResult(returncode=0)

    run.calls = log
    return run


# --------------------------------------------------------------- intervals


@pytest.mark.parametrize('minutes', ALLOWED_INTERVALS)
def test_allowed_intervals_pass(minutes):
    assert validate_interval(minutes) == minutes


@pytest.mark.parametrize('minutes', [0, 1, 5, 7, 45, 90, 100, 720 + 1, 2880])
def test_rejected_intervals(minutes):
    with pytest.raises(SchedulerError) as excinfo:
        validate_interval(minutes)
    message = str(excinfo.value)
    assert str(minutes) in message
    assert '15, 30, 60, 120, 180, 240, 360, 720, 1440' in message


CRON_TABLE = [
    (15, '7,22,37,52 * * * *'),
    (30, '7,37 * * * *'),
    (60, '7 * * * *'),
    (120, '7 */2 * * *'),
    (180, '7 */3 * * *'),
    (240, '7 */4 * * *'),
    (360, '7 */6 * * *'),
    (720, '7 */12 * * *'),
    (1440, '7 3 * * *'),
]


@pytest.mark.parametrize(('minutes', 'expected'), CRON_TABLE)
def test_cron_expression_table(minutes, expected):
    assert cron_expression(minutes, OFFSET) == expected


ONCALENDAR_TABLE = [
    (15, '*-*-* *:07/15:00'),
    (30, '*-*-* *:07/30:00'),
    (60, '*-*-* *:07:00'),
    (120, '*-*-* 00/2:07:00'),
    (180, '*-*-* 00/3:07:00'),
    (240, '*-*-* 00/4:07:00'),
    (360, '*-*-* 00/6:07:00'),
    (720, '*-*-* 00/12:07:00'),
    (1440, '*-*-* 03:07:00'),
]


@pytest.mark.parametrize(('minutes', 'expected'), ONCALENDAR_TABLE)
def test_oncalendar_table(minutes, expected):
    assert oncalendar_expression(minutes, OFFSET) == expected


def test_sub_hour_offset_is_folded_into_the_interval():
    # An offset of 47 must not produce a minute beyond the first period.
    assert cron_expression(15, 47) == '2,17,32,47 * * * *'
    assert oncalendar_expression(15, 47) == '*-*-* *:02/15:00'


@pytest.mark.parametrize('minutes', ALLOWED_INTERVALS)
def test_cron_and_oncalendar_never_raise_for_allowed(minutes):
    assert cron_expression(minutes, 0)
    assert oncalendar_expression(minutes, 0)


def test_minute_offset_is_stable_and_in_range():
    first = machine_minute_offset('machine-a')
    assert first == machine_minute_offset('machine-a')
    assert 0 <= first < 60
    assert machine_minute_offset('machine-b') != first


def test_minute_offset_without_seed_is_stable():
    assert machine_minute_offset() == machine_minute_offset()


# ----------------------------------------------------------------- quoting


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('/plain/path', '/plain/path'),
        ('/with space/x', "'/with space/x'"),
        ("/it's/here", "'/it'\"'\"'s/here'"),
        ('/100%/x', r'/100\%/x'),
    ],
)
def test_cron_quote(value, expected):
    assert cron_quote(value) == expected


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('/plain/path', '/plain/path'),
        ('/with space/x', '"/with space/x"'),
        ('/it"s/here', '"/it\\"s/here"'),
        ('/100%/x', '/100%%/x'),
        ('/back\\slash', '"/back\\\\slash"'),
    ],
)
def test_systemd_quote(value, expected):
    assert systemd_quote(value) == expected


def test_launch_spec_command_renderings():
    spec = make_spec(
        executable=Path('/opt/my bin/fleet-usage'),
        args=('--config', "/etc/o'dd/settings.toml", 'publish', '--if-due'),
    )
    shell = spec.shell_command()
    assert shell.startswith("'/opt/my bin/fleet-usage'")
    assert 'publish --if-due' in shell
    unit = spec.systemd_command()
    assert unit.startswith('"/opt/my bin/fleet-usage"')
    assert spec.argv[0] == '/opt/my bin/fleet-usage'


def test_windows_arguments_quote_spaces():
    spec = make_spec(
        args=('--config', r'C:\Program Files\fleet\settings.toml', 'publish')
    )
    assert '"C:\\Program Files\\fleet\\settings.toml"' in (
        spec.windows_arguments()
    )


# ------------------------------------------------------ executable lookup


def test_prefers_console_script_next_to_the_interpreter(tmp_path):
    prefix = tmp_path / 'venv' / 'bin'
    prefix.mkdir(parents=True)
    script = prefix / 'fleet-usage'
    script.write_text('#!/bin/sh\n')
    other = tmp_path / 'elsewhere' / 'fleet-usage'
    other.parent.mkdir(parents=True)
    other.write_text('#!/bin/sh\n')
    found = resolve_executable(
        exec_prefix=prefix,
        argv0=str(other),
        which=lambda name: str(other),
        is_windows=False,
        temp_roots=[],
    )
    assert found == script.resolve()


def test_falls_back_to_argv0_then_path(tmp_path):
    argv0 = tmp_path / 'a' / 'fleet-usage'
    argv0.parent.mkdir(parents=True)
    argv0.write_text('')
    found = resolve_executable(
        exec_prefix=tmp_path / 'missing',
        argv0=str(argv0),
        which=lambda name: None,
        is_windows=False,
        temp_roots=[],
    )
    assert found == argv0.resolve()

    on_path = tmp_path / 'b' / 'fleet-usage'
    on_path.parent.mkdir(parents=True)
    on_path.write_text('')
    found = resolve_executable(
        exec_prefix=tmp_path / 'missing',
        argv0='',
        which=lambda name: str(on_path),
        is_windows=False,
        temp_roots=[],
    )
    assert found == on_path.resolve()


def test_refuses_an_executable_in_a_temporary_directory(tmp_path):
    script = tmp_path / 'bin' / 'fleet-usage'
    script.parent.mkdir(parents=True)
    script.write_text('')
    with pytest.raises(SchedulerError) as excinfo:
        resolve_executable(
            exec_prefix=script.parent,
            argv0='',
            which=lambda name: None,
            is_windows=False,
            temp_roots=[tmp_path],
        )
    assert 'temporary directory' in str(excinfo.value)


def test_refuses_when_nothing_can_be_found(tmp_path):
    with pytest.raises(SchedulerError) as excinfo:
        resolve_executable(
            exec_prefix=tmp_path / 'nope',
            argv0='',
            which=lambda name: None,
            is_windows=False,
            temp_roots=[],
        )
    assert 'cannot locate' in str(excinfo.value)


def test_windows_prefers_the_exe(tmp_path):
    exe = tmp_path / 'fleet-usage.exe'
    exe.write_text('')
    plain = tmp_path / 'fleet-usage'
    plain.write_text('')
    found = resolve_executable(
        exec_prefix=tmp_path,
        argv0='',
        which=lambda name: None,
        is_windows=True,
        temp_roots=[],
    )
    assert found == exe.resolve()


def test_build_launch_spec_shapes_the_arguments(tmp_path):
    settings = tmp_path / 'settings.toml'
    settings.write_text('')
    program = tmp_path / 'fleet-usage'
    program.write_text('')
    spec = build_launch_spec(
        settings_path=settings,
        log_dir=tmp_path / 'logs',
        interval_minutes=60,
        executable=program,
    )
    assert spec.args == (
        '--config',
        str(settings),
        'publish',
        '--if-due',
    )
    assert spec.executable == program
    assert spec.interval_minutes == 60


def test_build_launch_spec_validates_the_interval(tmp_path):
    with pytest.raises(SchedulerError):
        build_launch_spec(
            settings_path=tmp_path / 'settings.toml',
            log_dir=tmp_path,
            interval_minutes=90,
            executable=tmp_path / 'fleet-usage',
        )


# ----------------------------------------------------- backend selection

LINUX_ENV = {'XDG_RUNTIME_DIR': '/run/user/1000'}


def test_auto_picks_windows_on_win32():
    chosen = select_backend(
        'auto',
        spec=make_spec(),
        platform='win32',
        which=lambda name: None,
        env={},
    )
    assert isinstance(chosen, WindowsScheduler)


def test_auto_picks_systemd_when_the_user_bus_answers():
    runner = fake_runner()
    chosen = select_backend(
        'auto',
        spec=make_spec(),
        platform='linux',
        runner=runner,
        which=lambda name: f'/usr/bin/{name}',
        env=LINUX_ENV,
    )
    assert isinstance(chosen, SystemdScheduler)
    assert runner.calls[0][0] == (
        'systemctl',
        '--user',
        'show',
        '--property=Version',
    )


def test_auto_falls_back_to_cron_without_a_user_bus():
    chosen = select_backend(
        'auto',
        spec=make_spec(),
        platform='linux',
        runner=fake_runner(),
        which=lambda name: f'/usr/bin/{name}',
        env={},
    )
    assert isinstance(chosen, CronScheduler)


def test_auto_falls_back_to_cron_when_systemctl_fails():
    runner = fake_runner({('systemctl',): RunResult(returncode=1, stderr='x')})
    chosen = select_backend(
        'auto',
        spec=make_spec(),
        platform='linux',
        runner=runner,
        which=lambda name: f'/usr/bin/{name}',
        env=LINUX_ENV,
    )
    assert isinstance(chosen, CronScheduler)


def test_auto_falls_back_to_cron_without_systemctl():
    chosen = select_backend(
        'auto',
        spec=make_spec(),
        platform='linux',
        runner=fake_runner(),
        which=lambda name: None if name == 'systemctl' else '/usr/bin/crontab',
        env=LINUX_ENV,
    )
    assert isinstance(chosen, CronScheduler)


def test_auto_reports_when_nothing_is_available():
    with pytest.raises(SchedulerError) as excinfo:
        select_backend(
            'auto',
            spec=make_spec(),
            platform='linux',
            runner=fake_runner(),
            which=lambda name: None,
            env={},
        )
    assert 'no supported scheduler found' in str(excinfo.value)


def test_explicit_backend_that_is_unusable_is_refused():
    with pytest.raises(SchedulerError) as excinfo:
        select_backend(
            'cron',
            spec=make_spec(),
            platform='linux',
            runner=fake_runner(),
            which=lambda name: None,
            env={},
        )
    assert 'not usable' in str(excinfo.value)


def test_unknown_backend_name():
    with pytest.raises(SchedulerError) as excinfo:
        select_backend('launchd', spec=make_spec(), platform='linux')
    assert 'unknown scheduler backend' in str(excinfo.value)


def test_explicit_windows_backend_is_refused_on_linux():
    with pytest.raises(SchedulerError):
        select_backend(
            'windows',
            spec=make_spec(),
            platform='linux',
            which=lambda name: '/usr/bin/schtasks.exe',
            env={},
        )


def test_subprocess_runner_reports_a_missing_program():
    with pytest.raises(SchedulerError):
        base.subprocess_runner(['/nonexistent/program-xyz'])


def test_subprocess_runner_captures_output():
    result = base.subprocess_runner(['cat'], stdin='hello')
    assert result.ok
    assert result.stdout == 'hello'
