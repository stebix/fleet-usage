"""Scheduler backend based on the Windows Task Scheduler.

``schtasks.exe`` is driven exclusively through argument lists -- never a
shell string -- so a settings path containing spaces or quotes stays
intact. Its command line flags cannot express everything the job needs
(catching up a missed start, refusing to pile up instances, running on
battery), therefore the task is registered from a generated XML
definition and the flag form is kept only as a fallback.
"""

import dataclasses
import getpass
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from fleet_usage.scheduling.base import (
    LaunchSpec,
    Runner,
    Scheduler,
    SchedulerError,
    ScheduleStatus,
    WhichCallable,
    default_which,
    subprocess_runner,
    validate_interval,
)

__all__ = [
    'EXECUTION_TIME_LIMIT',
    'TASK_NAME',
    'LogonMode',
    'WindowsScheduler',
    'create_argv',
    'delete_argv',
    'fallback_create_argv',
    'path_note',
    'query_argv',
    'render_task_xml',
    'schedule_flags',
]

TASK_NAME = 'FleetUsage'
SCHTASKS = 'schtasks.exe'
EXECUTION_TIME_LIMIT = 'PT30M'
REPETITION_DURATION = 'P1D'
DAILY_HOUR = 3
_START_DATE = '2000-01-01'

LogonMode = Literal['interactive', 's4u', 'password']
_LOGON_TYPES: dict[str, str] = {
    'interactive': 'InteractiveToken',
    's4u': 'S4U',
    'password': 'Password',
}


@dataclasses.dataclass(frozen=True, slots=True)
class ScheduleFlags:
    """The ``schtasks`` trigger flags matching an interval.

    Attributes
    ----------
    kind
        ``'MINUTE'``, ``'HOURLY'`` or ``'DAILY'``.
    modifier
        Value of ``/MO``.
    start_time
        ``HH:MM`` at which the first run happens.
    """

    kind: str
    modifier: int
    start_time: str


def schedule_flags(interval_minutes: int, offset: int) -> ScheduleFlags:
    """Map an interval onto ``schtasks`` trigger flags.

    Parameters
    ----------
    interval_minutes : int
        One of the supported intervals.
    offset : int
        Minute within the hour at which this machine runs.

    Returns
    -------
    ScheduleFlags
        The trigger description.

    Raises
    ------
    SchedulerError
        If the interval is unsupported.
    """
    interval = validate_interval(interval_minutes)
    minute = offset % 60
    if interval < 60:
        start = offset % interval
        return ScheduleFlags('MINUTE', interval, f'00:{start:02d}')
    if interval == 1440:
        return ScheduleFlags('DAILY', 1, f'{DAILY_HOUR:02d}:{minute:02d}')
    return ScheduleFlags('HOURLY', interval // 60, f'00:{minute:02d}')


def _iso_duration(minutes: int) -> str:
    """Render an interval as an ISO 8601 duration.

    Parameters
    ----------
    minutes : int
        Interval in minutes.

    Returns
    -------
    str
        For example ``'PT15M'`` or ``'PT2H'``.
    """
    if minutes % 60:
        return f'PT{minutes}M'
    return f'PT{minutes // 60}H'


def render_task_xml(
    spec: LaunchSpec,
    offset: int,
    *,
    user: str,
    logon_mode: LogonMode = 'interactive',
) -> str:
    """Render the Task Scheduler XML definition of the job.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.
    offset : int
        Minute within the hour at which this machine runs.
    user : str
        Account the task runs as.
    logon_mode : {'interactive', 's4u', 'password'}, optional
        How the task authenticates.

    Returns
    -------
    str
        The XML document, ready to be written as UTF-16.

    Raises
    ------
    SchedulerError
        If the interval or the logon mode is unsupported.
    """
    flags = schedule_flags(spec.interval_minutes, offset)
    logon_type = _LOGON_TYPES.get(logon_mode)
    if logon_type is None:
        known = ', '.join(sorted(_LOGON_TYPES))
        msg = f'unknown logon mode {logon_mode!r}; known: {known}'
        raise SchedulerError(msg)
    boundary = f'{_START_DATE}T{flags.start_time}:00'
    if flags.kind == 'DAILY':
        repetition = ''
    else:
        interval = _iso_duration(spec.interval_minutes)
        repetition = (
            '      <Repetition>\n'
            f'        <Interval>{interval}</Interval>\n'
            f'        <Duration>{REPETITION_DURATION}</Duration>\n'
            '        <StopAtDurationEnd>false</StopAtDurationEnd>\n'
            '      </Repetition>\n'
        )
    command = escape(str(spec.executable))
    arguments = escape(spec.windows_arguments())
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.4" xmlns="http://schemas.microsoft.com/'
        'windows/2004/02/mit/task">\n'
        '  <RegistrationInfo>\n'
        '    <Description>Collect and publish AI coding agent usage'
        '</Description>\n'
        f'    <URI>\\{TASK_NAME}</URI>\n'
        '  </RegistrationInfo>\n'
        '  <Triggers>\n'
        '    <CalendarTrigger>\n'
        f'      <StartBoundary>{boundary}</StartBoundary>\n'
        '      <Enabled>true</Enabled>\n'
        '      <ScheduleByDay>\n'
        '        <DaysInterval>1</DaysInterval>\n'
        '      </ScheduleByDay>\n'
        f'{repetition}'
        '    </CalendarTrigger>\n'
        '  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        f'      <UserId>{escape(user)}</UserId>\n'
        f'      <LogonType>{logon_type}</LogonType>\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false'
        '</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false'
        '</RunOnlyIfNetworkAvailable>\n'
        '    <IdleSettings>\n'
        '      <StopOnIdleEnd>false</StopOnIdleEnd>\n'
        '      <RestartOnIdle>false</RestartOnIdle>\n'
        '    </IdleSettings>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>false</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <WakeToRun>false</WakeToRun>\n'
        f'    <ExecutionTimeLimit>{EXECUTION_TIME_LIMIT}'
        '</ExecutionTimeLimit>\n'
        '    <Priority>7</Priority>\n'
        '  </Settings>\n'
        '  <Actions Context="Author">\n'
        '    <Exec>\n'
        f'      <Command>{command}</Command>\n'
        f'      <Arguments>{arguments}</Arguments>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )


def create_argv(
    xml_path: Path,
    *,
    user: str,
    logon_mode: LogonMode = 'interactive',
) -> list[str]:
    """Return the ``schtasks`` command that registers the XML task.

    Parameters
    ----------
    xml_path : pathlib.Path
        The generated task definition.
    user : str
        Account the task runs as.
    logon_mode : {'interactive', 's4u', 'password'}, optional
        How the task authenticates. In ``'password'`` mode ``/RP *``
        makes ``schtasks`` prompt for the password itself; the password
        never travels through this process.

    Returns
    -------
    list of str
        The argument vector, never a shell string.
    """
    argv = [
        SCHTASKS,
        '/Create',
        '/TN',
        TASK_NAME,
        '/XML',
        str(xml_path),
        '/F',
    ]
    if logon_mode == 's4u':
        argv.extend(['/RU', user])
    elif logon_mode == 'password':
        argv.extend(['/RU', user, '/RP', '*'])
    return argv


def fallback_create_argv(
    spec: LaunchSpec,
    offset: int,
    *,
    user: str,
    logon_mode: LogonMode = 'interactive',
) -> list[str]:
    """Return the flag based ``schtasks`` creation command.

    This form cannot express ``StartWhenAvailable`` and friends, so it
    is only used when registering the XML definition fails.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.
    offset : int
        Minute within the hour at which this machine runs.
    user : str
        Account the task runs as.
    logon_mode : {'interactive', 's4u', 'password'}, optional
        How the task authenticates.

    Returns
    -------
    list of str
        The argument vector.

    Raises
    ------
    SchedulerError
        If the interval is unsupported.
    """
    flags = schedule_flags(spec.interval_minutes, offset)
    task_run = f'"{spec.executable}" {spec.windows_arguments()}'
    argv = [
        SCHTASKS,
        '/Create',
        '/TN',
        TASK_NAME,
        '/TR',
        task_run,
        '/SC',
        flags.kind,
        '/MO',
        str(flags.modifier),
        '/ST',
        flags.start_time,
        '/RL',
        'LIMITED',
        '/F',
    ]
    if logon_mode == 'password':
        argv.extend(['/RU', user, '/RP', '*'])
    elif logon_mode == 's4u':
        argv.extend(['/RU', user, '/NP'])
    else:
        argv.extend(['/RU', user, '/IT'])
    return argv


def path_note(spec: LaunchSpec, logon_mode: LogonMode) -> str | None:
    """Return the ``PATH`` caveat of a non-interactive logon mode.

    A task registered with an interactive token inherits the ``PATH`` of
    the account that owns it, so nothing has to be carried along the way
    the POSIX backends carry it. ``S4U`` and ``Password`` tasks start
    outside a logon session, where only the system and user ``PATH`` from
    the registry apply, and a collector installed by a per-user package
    manager may be missing from both.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.
    logon_mode : {'interactive', 's4u', 'password'}
        How the task authenticates.

    Returns
    -------
    str or None
        A single line of advice, or ``None`` in interactive mode.
    """
    if logon_mode == 'interactive':
        return None
    where = ''
    if spec.extra_path_dirs:
        where = f'; here it resolved from {spec.extra_path_dirs[0]}'
    return (
        f'note: a task in {logon_mode} mode does not inherit an '
        'interactive PATH, so the collector must be on the system or '
        f'user PATH{where}'
    )


def query_argv(fmt: str = 'XML') -> list[str]:
    """Return the ``schtasks`` command that inspects the task.

    Parameters
    ----------
    fmt : str, optional
        ``'XML'`` for the full definition, ``'LIST'`` for the verbose
        human readable form.

    Returns
    -------
    list of str
        The argument vector.
    """
    if fmt == 'XML':
        return [SCHTASKS, '/Query', '/TN', TASK_NAME, '/XML']
    return [SCHTASKS, '/Query', '/TN', TASK_NAME, '/FO', 'LIST', '/V']


def delete_argv() -> list[str]:
    """Return the ``schtasks`` command that removes the task.

    Returns
    -------
    list of str
        The argument vector.
    """
    return [SCHTASKS, '/Delete', '/TN', TASK_NAME, '/F']


class WindowsScheduler(Scheduler):
    """Manage the ``FleetUsage`` task in the Windows Task Scheduler."""

    name = 'windows'

    def __init__(
        self,
        spec: LaunchSpec,
        *,
        platform: str = 'win32',
        user: str | None = None,
        logon_mode: LogonMode = 'interactive',
        xml_dir: Path | None = None,
        runner: Runner = subprocess_runner,
        which: WhichCallable = default_which,
        minute_offset: int | None = None,
    ) -> None:
        """Store the job specification and the Windows specific options.

        Parameters
        ----------
        spec : LaunchSpec
            The job to schedule.
        platform : str, optional
            Platform string; the backend is only usable on ``'win32'``.
        user : str or None, optional
            Account the task runs as; the current user by default.
        logon_mode : {'interactive', 's4u', 'password'}, optional
            How the task authenticates.
        xml_dir : pathlib.Path or None, optional
            Directory the generated XML is written to. It is a
            short-lived file that only ``schtasks`` reads.
        runner : Runner, optional
            Subprocess seam.
        which : callable, optional
            ``PATH`` lookup seam.
        minute_offset : int or None, optional
            Minute within the hour.
        """
        super().__init__(
            spec,
            runner=runner,
            which=which,
            minute_offset=minute_offset,
        )
        self.platform = platform
        self.user = user if user is not None else _current_user()
        self.logon_mode: LogonMode = logon_mode
        self.xml_dir = xml_dir

    def available(self) -> bool:
        """Whether the Windows Task Scheduler can be used.

        Returns
        -------
        bool
            ``True`` only on Windows, and only when ``schtasks`` exists.
        """
        if self.platform != 'win32':
            return False
        return (
            self.which(SCHTASKS) is not None
            or self.which('schtasks') is not None
        )

    def task_xml(self, interval_minutes: int) -> str:
        """Render the task definition for an interval.

        Parameters
        ----------
        interval_minutes : int
            Desired interval between runs.

        Returns
        -------
        str
            The XML document.

        Raises
        ------
        SchedulerError
            If the interval or logon mode is unsupported.
        """
        spec = self._spec_for(interval_minutes)
        return render_task_xml(
            spec,
            self.offset,
            user=self.user,
            logon_mode=self.logon_mode,
        )

    def _write_xml(self, document: str) -> Path:
        """Write the task definition where ``schtasks`` can read it.

        Parameters
        ----------
        document : str
            The XML text.

        Returns
        -------
        pathlib.Path
            The file, encoded as UTF-16 with a byte order mark, which is
            what ``schtasks /XML`` expects.

        Raises
        ------
        SchedulerError
            If the file cannot be written.
        """
        directory = self.xml_dir
        if directory is None:
            directory = Path(tempfile.gettempdir())
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{TASK_NAME}.xml'
            path.write_text(document, encoding='utf-16')
        except OSError as exc:
            msg = f'cannot write the task definition: {exc}'
            raise SchedulerError(msg) from exc
        return path

    def install(self, interval_minutes: int, dry_run: bool = False) -> str:
        """Register the scheduled task.

        Parameters
        ----------
        interval_minutes : int
            Desired interval between runs.
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of the planned or performed change.

        Raises
        ------
        SchedulerError
            If ``schtasks`` refuses both the XML and the flag form.
        """
        spec = self._spec_for(interval_minutes)
        document = self.task_xml(interval_minutes)
        if dry_run:
            argv = create_argv(
                Path(f'<generated>/{TASK_NAME}.xml'),
                user=self.user,
                logon_mode=self.logon_mode,
            )
            note = path_note(spec, self.logon_mode)
            suffix = f'{note}\n' if note is not None else ''
            return (
                f'dry run: would register the task {TASK_NAME} with\n'
                f'{" ".join(argv)}\n'
                f'--- {TASK_NAME}.xml\n{document}{suffix}'
            )
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        xml_path = self._write_xml(document)
        argv = create_argv(
            xml_path,
            user=self.user,
            logon_mode=self.logon_mode,
        )
        note = path_note(spec, self.logon_mode)
        suffix = f'\n{note}' if note is not None else ''
        result = self.run(argv)
        if result.ok:
            xml_path.unlink(missing_ok=True)
            return f'registered the scheduled task {TASK_NAME}{suffix}'
        fallback = fallback_create_argv(
            spec,
            self.offset,
            user=self.user,
            logon_mode=self.logon_mode,
        )
        second = self.run(fallback)
        xml_path.unlink(missing_ok=True)
        if second.ok:
            return (
                f'registered the scheduled task {TASK_NAME} without the '
                'XML definition; missed runs will not be caught up\n'
                f'(XML registration failed: {result.message()}){suffix}'
            )
        msg = (
            f'cannot register the scheduled task {TASK_NAME}: '
            f'{result.message() or second.message()}'
        )
        raise SchedulerError(msg)

    def status(self) -> ScheduleStatus:
        """Report whether the task is registered.

        Returns
        -------
        ScheduleStatus
            The observed state.
        """
        if self.platform != 'win32':
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail='the Windows Task Scheduler exists only on Windows',
            )
        definition = self.run(query_argv('XML'))
        if not definition.ok:
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail=(
                    f'no scheduled task named {TASK_NAME}; run '
                    "'fleet-usage schedule install' to create it"
                ),
            )
        verbose = self.run(query_argv('LIST'))
        detail = verbose.stdout.strip() if verbose.ok else ''
        return ScheduleStatus(
            installed=True,
            backend=self.name,
            detail=detail,
            expression=_xml_value(definition.stdout, 'StartBoundary'),
            command=_xml_value(definition.stdout, 'Command'),
        )

    def uninstall(self, dry_run: bool = False) -> str:
        """Delete the scheduled task.

        Parameters
        ----------
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of the planned or performed change.

        Raises
        ------
        SchedulerError
            If ``schtasks`` fails for a reason other than a missing
            task.
        """
        argv = delete_argv()
        if dry_run:
            return f'dry run: would run {" ".join(argv)}'
        result = self.run(argv)
        if result.ok:
            return f'deleted the scheduled task {TASK_NAME}'
        text = result.message().lower()
        if 'does not exist' in text or 'cannot find' in text:
            return f'no scheduled task named {TASK_NAME}; nothing to do'
        msg = (
            f'cannot delete the scheduled task {TASK_NAME}: {result.message()}'
        )
        raise SchedulerError(msg)


def _current_user() -> str:
    r"""Return the account the task should run as.

    Returns
    -------
    str
        ``DOMAIN\\user`` when the environment knows a domain, otherwise
        the bare user name.
    """
    try:
        name = getpass.getuser()
    except OSError:  # pragma: no cover - defensive
        name = os.environ.get('USERNAME', 'SYSTEM')
    domain = os.environ.get('USERDOMAIN')
    if domain:
        return f'{domain}\\{name}'
    return name


def _xml_value(document: str, tag: str) -> str | None:
    """Extract the text of the first ``tag`` element.

    A regular expression is enough here: the input is the output of
    ``schtasks /Query /XML``, whose shape is fixed, and pulling in an XML
    parser to read one element would only add failure modes.

    Parameters
    ----------
    document : str
        The XML text.
    tag : str
        Element name without angle brackets.

    Returns
    -------
    str or None
        The element text, or ``None`` when it is absent.
    """
    match = re.search(rf'<{tag}>(.*?)</{tag}>', document, re.DOTALL)
    if match is None:
        return None
    return match.group(1).strip()
