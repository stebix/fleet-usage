"""Windows Task Scheduler argument lists and XML, verified on Linux.

Nothing here executes ``schtasks``: every call goes through an injected
runner that records the argument vector, which is exactly what has to be
right on the machine that does run it.
"""

from pathlib import Path
from xml.etree import ElementTree

import pytest

from fleet_usage.scheduling.base import LaunchSpec, RunResult, SchedulerError
from fleet_usage.scheduling.windows import (
    TASK_NAME,
    WindowsScheduler,
    create_argv,
    delete_argv,
    fallback_create_argv,
    path_note,
    query_argv,
    render_task_xml,
    schedule_flags,
)

OFFSET = 7
NS = '{http://schemas.microsoft.com/windows/2004/02/mit/task}'
EXE = Path(r'C:\Users\me\AppData\Roaming\uv\tools\fleet-usage.exe')
SETTINGS = r'C:\Users\me\AppData\Roaming\fleet-usage\settings.toml'


def make_spec(
    interval=60, settings=SETTINGS, log_dir=None, extra_path_dirs=()
):
    return LaunchSpec(
        executable=EXE,
        args=('--config', settings, 'publish', '--if-due'),
        settings_path=Path(settings),
        interval_minutes=interval,
        log_dir=log_dir or Path(r'C:\Users\me\logs'),
        extra_path_dirs=tuple(extra_path_dirs),
    )


class FakeSchtasks:
    """Records ``schtasks`` invocations and replays canned results."""

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
        return f'C:\\Windows\\System32\\{name}'


def make_scheduler(tmp_path, fake=None, **kwargs):
    fake = fake or FakeSchtasks()
    options = {
        'platform': 'win32',
        'user': 'CORP\\me',
        'xml_dir': tmp_path / 'xml',
        'runner': fake,
        'which': fake.which,
        'minute_offset': OFFSET,
    }
    spec_interval = kwargs.pop('interval', 60)
    options.update(kwargs)
    return WindowsScheduler(make_spec(interval=spec_interval), **options), fake


# ------------------------------------------------------------- triggers

FLAG_TABLE = [
    (15, 'MINUTE', 15, '00:07'),
    (30, 'MINUTE', 30, '00:07'),
    (60, 'HOURLY', 1, '00:07'),
    (120, 'HOURLY', 2, '00:07'),
    (720, 'HOURLY', 12, '00:07'),
    (1440, 'DAILY', 1, '03:07'),
]


@pytest.mark.parametrize(('interval', 'kind', 'modifier', 'start'), FLAG_TABLE)
def test_schedule_flags_table(interval, kind, modifier, start):
    flags = schedule_flags(interval, OFFSET)
    assert (flags.kind, flags.modifier, flags.start_time) == (
        kind,
        modifier,
        start,
    )


def test_schedule_flags_reject_unsupported_intervals():
    with pytest.raises(SchedulerError):
        schedule_flags(90, OFFSET)


# ------------------------------------------------------------------ XML


def parse(document):
    return ElementTree.fromstring(document)


def test_task_xml_is_well_formed_and_carries_the_command():
    root = parse(render_task_xml(make_spec(), OFFSET, user='CORP\\me'))
    exec_node = root.find(f'{NS}Actions/{NS}Exec')
    assert exec_node.find(f'{NS}Command').text == str(EXE)
    assert exec_node.find(f'{NS}Arguments').text == (
        f'--config {SETTINGS} publish --if-due'
    )


def test_task_xml_quotes_a_settings_path_with_spaces():
    spaced = r'C:\Program Files\fleet usage\settings.toml'
    root = parse(
        render_task_xml(make_spec(settings=spaced), OFFSET, user='me')
    )
    arguments = root.find(f'{NS}Actions/{NS}Exec/{NS}Arguments').text
    assert arguments == f'--config "{spaced}" publish --if-due'


def test_task_xml_settings_cover_what_schtasks_flags_cannot():
    root = parse(render_task_xml(make_spec(), OFFSET, user='me'))
    settings = root.find(f'{NS}Settings')
    values = {
        node.tag.replace(NS, ''): node.text
        for node in settings
        if node.text is not None
    }
    assert values['StartWhenAvailable'] == 'true'
    assert values['MultipleInstancesPolicy'] == 'IgnoreNew'
    assert values['ExecutionTimeLimit'] == 'PT30M'
    assert values['WakeToRun'] == 'false'
    assert values['DisallowStartIfOnBatteries'] == 'false'


def test_task_xml_repetition_for_hourly_and_daily():
    hourly = parse(render_task_xml(make_spec(120), OFFSET, user='me'))
    trigger = hourly.find(f'{NS}Triggers/{NS}CalendarTrigger')
    assert trigger.find(f'{NS}StartBoundary').text == '2000-01-01T00:07:00'
    repetition = trigger.find(f'{NS}Repetition')
    assert repetition.find(f'{NS}Interval').text == 'PT2H'
    assert repetition.find(f'{NS}Duration').text == 'P1D'

    quarter = parse(render_task_xml(make_spec(15), OFFSET, user='me'))
    interval = quarter.find(
        f'{NS}Triggers/{NS}CalendarTrigger/{NS}Repetition/{NS}Interval'
    )
    assert interval.text == 'PT15M'

    daily = parse(render_task_xml(make_spec(1440), OFFSET, user='me'))
    trigger = daily.find(f'{NS}Triggers/{NS}CalendarTrigger')
    assert trigger.find(f'{NS}StartBoundary').text == '2000-01-01T03:07:00'
    assert trigger.find(f'{NS}Repetition') is None


@pytest.mark.parametrize(
    ('mode', 'logon_type'),
    [
        ('interactive', 'InteractiveToken'),
        ('s4u', 'S4U'),
        ('password', 'Password'),
    ],
)
def test_task_xml_logon_types(mode, logon_type):
    root = parse(
        render_task_xml(make_spec(), OFFSET, user='CORP\\me', logon_mode=mode)
    )
    principal = root.find(f'{NS}Principals/{NS}Principal')
    assert principal.find(f'{NS}LogonType').text == logon_type
    assert principal.find(f'{NS}UserId').text == 'CORP\\me'
    assert principal.find(f'{NS}RunLevel').text == 'LeastPrivilege'


def test_task_xml_rejects_an_unknown_logon_mode():
    with pytest.raises(SchedulerError):
        render_task_xml(make_spec(), OFFSET, user='me', logon_mode='kerberos')


def test_task_xml_escapes_markup_in_paths():
    spec = make_spec(settings=r'C:\a<b>&c\settings.toml')
    document = render_task_xml(spec, OFFSET, user='me')
    assert '<b>&c' not in document
    root = parse(document)
    arguments = root.find(f'{NS}Actions/{NS}Exec/{NS}Arguments').text
    assert r'C:\a<b>&c\settings.toml' in arguments


def test_task_xml_declares_utf16():
    document = render_task_xml(make_spec(), OFFSET, user='me')
    assert document.startswith('<?xml version="1.0" encoding="UTF-16"?>')


# -------------------------------------------------------- argument lists


def test_create_argv_uses_the_xml_definition():
    assert create_argv(Path(r'C:\tmp\FleetUsage.xml'), user='CORP\\me') == [
        'schtasks.exe',
        '/Create',
        '/TN',
        'FleetUsage',
        '/XML',
        r'C:\tmp\FleetUsage.xml',
        '/F',
    ]


def test_create_argv_logon_modes():
    path = Path(r'C:\tmp\FleetUsage.xml')
    s4u = create_argv(path, user='CORP\\me', logon_mode='s4u')
    assert s4u[-2:] == ['/RU', 'CORP\\me']
    password = create_argv(path, user='CORP\\me', logon_mode='password')
    assert password[-4:] == ['/RU', 'CORP\\me', '/RP', '*']
    # The password itself is never a command line argument.
    assert not any(item not in {'*'} and 'secret' in item for item in password)


def test_fallback_argv_carries_the_trigger_flags():
    argv = fallback_create_argv(make_spec(120), OFFSET, user='CORP\\me')
    assert argv[:4] == ['schtasks.exe', '/Create', '/TN', 'FleetUsage']
    assert argv[argv.index('/SC') + 1] == 'HOURLY'
    assert argv[argv.index('/MO') + 1] == '2'
    assert argv[argv.index('/ST') + 1] == '00:07'
    assert argv[argv.index('/RL') + 1] == 'LIMITED'
    assert '/IT' in argv
    task_run = argv[argv.index('/TR') + 1]
    assert task_run == f'"{EXE}" --config {SETTINGS} publish --if-due'


def test_fallback_argv_minute_and_daily():
    minute = fallback_create_argv(make_spec(15), OFFSET, user='me')
    assert minute[minute.index('/SC') + 1] == 'MINUTE'
    assert minute[minute.index('/MO') + 1] == '15'
    daily = fallback_create_argv(make_spec(1440), OFFSET, user='me')
    assert daily[daily.index('/SC') + 1] == 'DAILY'
    assert daily[daily.index('/ST') + 1] == '03:07'


def test_query_and_delete_argv():
    assert query_argv('XML') == [
        'schtasks.exe',
        '/Query',
        '/TN',
        'FleetUsage',
        '/XML',
    ]
    assert query_argv('LIST') == [
        'schtasks.exe',
        '/Query',
        '/TN',
        'FleetUsage',
        '/FO',
        'LIST',
        '/V',
    ]
    assert delete_argv() == [
        'schtasks.exe',
        '/Delete',
        '/TN',
        'FleetUsage',
        '/F',
    ]


# ------------------------------------------------------------- lifecycle


def test_install_registers_the_xml_task(tmp_path):
    scheduler, fake = make_scheduler(tmp_path)
    report = scheduler.install(60)
    assert TASK_NAME in report
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call[:5] == (
        'schtasks.exe',
        '/Create',
        '/TN',
        'FleetUsage',
        '/XML',
    )
    # The temporary definition is cleaned up after registration.
    assert not (tmp_path / 'xml' / 'FleetUsage.xml').exists()


def test_install_writes_the_definition_as_utf16(tmp_path):
    written = {}

    def runner(argv, *, stdin=None):
        path = Path(argv[argv.index('/XML') + 1])
        written['bytes'] = path.read_bytes()
        return RunResult(returncode=0)

    scheduler = WindowsScheduler(
        make_spec(),
        platform='win32',
        user='me',
        xml_dir=tmp_path / 'xml',
        runner=runner,
        which=lambda name: 'schtasks.exe',
        minute_offset=OFFSET,
    )
    scheduler.install(60)
    raw = written['bytes']
    assert raw[:2] in (b'\xff\xfe', b'\xfe\xff')
    assert '<Task version="1.4"' in raw.decode('utf-16')


def test_install_falls_back_when_the_xml_is_rejected(tmp_path):
    fake = FakeSchtasks(
        {
            ('schtasks.exe', '/Create', '/TN', 'FleetUsage', '/XML'): (
                RunResult(returncode=1, stderr='ERROR: Invalid XML')
            )
        }
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    report = scheduler.install(60)
    assert len(fake.calls) == 2
    assert '/SC' in fake.calls[1]
    assert 'without the XML definition' in report


def test_install_reports_a_total_failure(tmp_path):
    fake = FakeSchtasks(
        {('schtasks.exe',): RunResult(returncode=1, stderr='ERROR: Access')}
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    with pytest.raises(SchedulerError) as excinfo:
        scheduler.install(60)
    assert 'ERROR: Access' in str(excinfo.value)


def test_dry_run_runs_nothing(tmp_path):
    scheduler, fake = make_scheduler(tmp_path)
    report = scheduler.install(60, dry_run=True)
    assert fake.calls == []
    assert 'dry run' in report
    assert '<Task version="1.4"' in report
    assert not (tmp_path / 'xml').exists()


def test_status_when_the_task_is_missing(tmp_path):
    fake = FakeSchtasks(
        {
            ('schtasks.exe', '/Query'): RunResult(
                returncode=1, stderr='ERROR: The system cannot find the file'
            )
        }
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    status = scheduler.status()
    assert status.installed is False
    assert 'no scheduled task' in status.detail


def test_status_reads_the_registered_definition(tmp_path):
    document = render_task_xml(make_spec(), OFFSET, user='CORP\\me')
    fake = FakeSchtasks(
        {
            ('schtasks.exe', '/Query', '/TN', 'FleetUsage', '/XML'): (
                RunResult(returncode=0, stdout=document)
            ),
            ('schtasks.exe', '/Query', '/TN', 'FleetUsage', '/FO'): (
                RunResult(returncode=0, stdout='TaskName: \\FleetUsage\n')
            ),
        }
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    status = scheduler.status()
    assert status.installed is True
    assert status.expression == '2000-01-01T00:07:00'
    assert status.command == str(EXE)
    assert 'FleetUsage' in status.detail


def test_status_on_linux_is_not_installed(tmp_path):
    scheduler, fake = make_scheduler(tmp_path, platform='linux')
    status = scheduler.status()
    assert status.installed is False
    assert fake.calls == []
    assert scheduler.available() is False


def test_uninstall(tmp_path):
    scheduler, fake = make_scheduler(tmp_path)
    assert TASK_NAME in scheduler.uninstall()
    assert fake.calls[0] == tuple(delete_argv())


def test_uninstall_when_the_task_does_not_exist(tmp_path):
    fake = FakeSchtasks(
        {
            ('schtasks.exe', '/Delete'): RunResult(
                returncode=1,
                stderr='ERROR: The specified task name does not exist.',
            )
        }
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    assert 'nothing to do' in scheduler.uninstall()


def test_uninstall_reports_other_failures(tmp_path):
    fake = FakeSchtasks(
        {
            ('schtasks.exe', '/Delete'): RunResult(
                returncode=1, stderr='ERROR: Access is denied.'
            )
        }
    )
    scheduler, _ = make_scheduler(tmp_path, fake)
    with pytest.raises(SchedulerError):
        scheduler.uninstall()


def test_uninstall_dry_run(tmp_path):
    scheduler, fake = make_scheduler(tmp_path)
    assert scheduler.uninstall(dry_run=True).startswith('dry run:')
    assert fake.calls == []


# --------------------------------------------------------- collector PATH

BUN_DIR = r'C:\Users\me\.bun\bin'


def path_scheduler(tmp_path, logon_mode, fake=None):
    """A scheduler whose collector lives outside the system directories."""
    fake = fake or FakeSchtasks()
    scheduler = WindowsScheduler(
        make_spec(extra_path_dirs=(BUN_DIR,)),
        platform='win32',
        user='CORP\\me',
        logon_mode=logon_mode,
        xml_dir=tmp_path / 'xml',
        runner=fake,
        which=fake.which,
        minute_offset=OFFSET,
    )
    return scheduler, fake


def test_an_interactive_task_needs_no_path_note():
    assert path_note(make_spec(extra_path_dirs=(BUN_DIR,)), 'interactive') is (
        None
    )


@pytest.mark.parametrize('mode', ['s4u', 'password'])
def test_a_non_interactive_task_notes_the_path(mode):
    note = path_note(make_spec(extra_path_dirs=(BUN_DIR,)), mode)
    assert 'system or user PATH' in note
    assert BUN_DIR in note


def test_the_preview_carries_the_note(tmp_path):
    scheduler, _ = path_scheduler(tmp_path, 's4u')
    report = scheduler.install(60, dry_run=True)
    assert 'system or user PATH' in report
    assert BUN_DIR in report


def test_the_installation_report_carries_the_note(tmp_path):
    scheduler, _ = path_scheduler(tmp_path, 'password')
    assert 'system or user PATH' in scheduler.install(60)


def test_an_interactive_installation_stays_silent(tmp_path):
    scheduler, _ = path_scheduler(tmp_path, 'interactive')
    assert 'system or user PATH' not in scheduler.install(60, dry_run=True)


def test_the_task_definition_never_sets_a_path(tmp_path):
    scheduler, _ = path_scheduler(tmp_path, 's4u')
    document = scheduler.task_xml(60)
    assert 'PATH' not in document
