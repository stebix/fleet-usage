"""Common interface and shared helpers of every scheduler backend.

A backend turns one :class:`LaunchSpec` -- an absolute executable, its
arguments, and where to write logs -- into a recurring job managed by the
operating system. Everything that talks to the outside world goes through
two injectable seams: a :class:`Runner` for subprocesses and a ``which``
callable for PATH lookups. That keeps the Windows backend fully testable
on Linux and keeps the cron and systemd backends testable without ever
installing a real schedule.
"""

import abc
import dataclasses
import hashlib
import os
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

__all__ = [
    'ALLOWED_INTERVALS',
    'DAILY_HOUR',
    'EXECUTABLE_NAME',
    'LaunchSpec',
    'RunResult',
    'Runner',
    'ScheduleStatus',
    'Scheduler',
    'SchedulerError',
    'WhichCallable',
    'build_launch_spec',
    'cron_expression',
    'cron_quote',
    'default_which',
    'machine_minute_offset',
    'oncalendar_expression',
    'resolve_executable',
    'select_backend',
    'subprocess_runner',
    'systemd_quote',
    'validate_interval',
]

EXECUTABLE_NAME = 'fleet-usage'
ALLOWED_INTERVALS = (15, 30, 60, 120, 180, 240, 360, 720, 1440)
DAILY_HOUR = 3
_MACHINE_ID_FILES = (
    Path('/etc/machine-id'),
    Path('/var/lib/dbus/machine-id'),
)
_SHELL_SAFE = frozenset(string.ascii_letters + string.digits + '@%+=:,./-_')


class SchedulerError(Exception):
    """Raised when a schedule cannot be installed, read or removed.

    The message is written for a human operator: it names the backend,
    what failed and, where possible, the command that would fix it.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of one subprocess invocation.

    Attributes
    ----------
    returncode
        Exit status of the process.
    stdout
        Captured standard output, decoded as text.
    stderr
        Captured standard error, decoded as text.
    """

    returncode: int
    stdout: str = ''
    stderr: str = ''

    @property
    def ok(self) -> bool:
        """Whether the process succeeded.

        Returns
        -------
        bool
            ``True`` when the exit status is zero.
        """
        return self.returncode == 0

    def message(self) -> str:
        """Return the most useful diagnostic text of the process.

        Returns
        -------
        str
            ``stderr`` when it carries anything, otherwise ``stdout``.
        """
        return self.stderr.strip() or self.stdout.strip()


class Runner(Protocol):
    """Callable that executes an argument vector without a shell."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
    ) -> RunResult:
        """Run ``argv`` and capture its output.

        Parameters
        ----------
        argv : sequence of str
            Program and arguments. Never a shell string.
        stdin : str or None, optional
            Text fed to the process on standard input.

        Returns
        -------
        RunResult
            Exit status and captured output.
        """
        ...


WhichCallable = Callable[[str], str | None]


def default_which(name: str) -> str | None:
    """Look ``name`` up on the ``PATH``.

    This thin wrapper exists so that the lookup happens at call time
    rather than at import time: binding :func:`shutil.which` itself as a
    default argument would freeze it and make the behaviour impossible
    to substitute in a test.

    Parameters
    ----------
    name : str
        Program name.

    Returns
    -------
    str or None
        Absolute path of the program, or ``None``.
    """
    return shutil.which(name)


def subprocess_runner(
    argv: Sequence[str],
    *,
    stdin: str | None = None,
) -> RunResult:
    """Run ``argv`` with :mod:`subprocess`, never through a shell.

    Parameters
    ----------
    argv : sequence of str
        Program and arguments.
    stdin : str or None, optional
        Text written to the child's standard input.

    Returns
    -------
    RunResult
        Exit status and captured output.

    Raises
    ------
    SchedulerError
        If the program does not exist or cannot be executed.
    """
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except OSError as exc:
        msg = f'cannot run {argv[0]!r}: {exc}'
        raise SchedulerError(msg) from exc
    return RunResult(
        returncode=completed.returncode,
        stdout=completed.stdout or '',
        stderr=completed.stderr or '',
    )


def validate_interval(minutes: int) -> int:
    """Check that ``minutes`` can be expressed as a repeating schedule.

    Only intervals that divide an hour or a day evenly are accepted.
    Anything else (90 minutes, say) cannot be written as a cron
    expression that repeats at a constant distance, and a schedule that
    silently drifts is worse than a rejected one.

    Parameters
    ----------
    minutes : int
        Requested interval.

    Returns
    -------
    int
        The unchanged interval.

    Raises
    ------
    SchedulerError
        If the interval is not one of :data:`ALLOWED_INTERVALS`.
    """
    if minutes in ALLOWED_INTERVALS:
        return minutes
    allowed = ', '.join(str(item) for item in ALLOWED_INTERVALS)
    msg = (
        f'interval of {minutes} minutes cannot be scheduled\n'
        f'supported intervals (minutes): {allowed}\n'
        'pick one of them with --interval, or change '
        '[sync].interval_minutes'
    )
    raise SchedulerError(msg)


def _machine_seed() -> str:
    """Return a stable per-machine string used to spread the load.

    Returns
    -------
    str
        The systemd/D-Bus machine id when readable, otherwise the
        hardware address reported by :func:`uuid.getnode`.
    """
    for candidate in _MACHINE_ID_FILES:
        try:
            text = candidate.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    return f'{uuid.getnode():x}'


def machine_minute_offset(seed: str | None = None) -> int:
    """Return the minute within the hour at which this machine runs.

    Every machine in the fleet talks to the same GitHub repository, so
    firing them all at ``:00`` would bunch the API calls together. The
    offset is derived from the machine id, which makes it stable across
    reinstalls but different between machines.

    Parameters
    ----------
    seed : str or None, optional
        Explicit seed; the machine id is used when omitted.

    Returns
    -------
    int
        A minute in ``range(60)``.
    """
    raw = _machine_seed() if seed is None else seed
    digest = hashlib.sha256(raw.encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big') % 60


def _minutes_within_hour(interval: int, offset: int) -> list[int]:
    """Return the minutes at which a sub-hourly schedule fires.

    Parameters
    ----------
    interval : int
        Interval in minutes, a divisor of 60.
    offset : int
        Minute offset of this machine.

    Returns
    -------
    list of int
        Ascending minutes, for example ``[7, 22, 37, 52]``.
    """
    start = offset % interval
    return list(range(start, 60, interval))


def cron_expression(interval_minutes: int, offset: int) -> str:
    """Render the cron time specification for an interval.

    Parameters
    ----------
    interval_minutes : int
        One of :data:`ALLOWED_INTERVALS`.
    offset : int
        Minute offset of this machine.

    Returns
    -------
    str
        The five leading fields of a crontab line, for example
        ``'7 */2 * * *'``. Sub-hourly intervals use an explicit minute
        list rather than a step, because plain POSIX cron implementations
        do not understand ``*/n``.

    Raises
    ------
    SchedulerError
        If the interval is not supported.
    """
    interval = validate_interval(interval_minutes)
    if interval < 60:
        minutes = ','.join(
            str(item) for item in _minutes_within_hour(interval, offset)
        )
        return f'{minutes} * * * *'
    minute = offset % 60
    if interval == 60:
        return f'{minute} * * * *'
    if interval == 1440:
        return f'{minute} {DAILY_HOUR} * * *'
    hours = interval // 60
    return f'{minute} */{hours} * * *'


def oncalendar_expression(interval_minutes: int, offset: int) -> str:
    """Render the systemd ``OnCalendar=`` value for an interval.

    Parameters
    ----------
    interval_minutes : int
        One of :data:`ALLOWED_INTERVALS`.
    offset : int
        Minute offset of this machine.

    Returns
    -------
    str
        A calendar expression, for example ``'*-*-* 00/2:07:00'``.

    Raises
    ------
    SchedulerError
        If the interval is not supported.
    """
    interval = validate_interval(interval_minutes)
    minute = offset % 60
    if interval < 60:
        start = offset % interval
        return f'*-*-* *:{start:02d}/{interval}:00'
    if interval == 60:
        return f'*-*-* *:{minute:02d}:00'
    if interval == 1440:
        return f'*-*-* {DAILY_HOUR:02d}:{minute:02d}:00'
    hours = interval // 60
    return f'*-*-* 00/{hours}:{minute:02d}:00'


def cron_quote(value: str) -> str:
    """Quote one argument for a crontab command line.

    ``%`` is special to cron -- everything after an unescaped ``%``
    becomes standard input for the job -- so it is escaped even inside
    quotes, which :func:`shlex.quote` does not do.

    Parameters
    ----------
    value : str
        The argument.

    Returns
    -------
    str
        A safely quoted and percent-escaped argument.
    """
    return shlex.quote(value).replace('%', r'\%')


def systemd_quote(value: str) -> str:
    """Quote one argument for a systemd unit file directive.

    systemd applies its own unquoting to ``ExecStart=`` and expands
    ``%`` specifiers, so both quoting and percent escaping differ from a
    POSIX shell.

    Parameters
    ----------
    value : str
        The argument.

    Returns
    -------
    str
        The argument, double quoted when it needs to be.
    """
    escaped = value.replace('%', '%%')
    if escaped and all(char in _SHELL_SAFE for char in escaped):
        return escaped
    inner = escaped.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{inner}"'


@dataclasses.dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything a backend needs to describe the recurring job.

    Attributes
    ----------
    executable
        Absolute path of the installed ``fleet-usage`` program.
    args
        Arguments passed to it, already including ``--config``.
    settings_path
        Absolute path of the settings file the job must read.
    interval_minutes
        How often the job should run.
    log_dir
        Directory the backend redirects the job output into.
    """

    executable: Path
    args: tuple[str, ...]
    settings_path: Path
    interval_minutes: int
    log_dir: Path

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the full argument vector of the job.

        Returns
        -------
        tuple of str
            The executable followed by its arguments.
        """
        return (str(self.executable), *self.args)

    def shell_command(self) -> str:
        """Return the job as a single POSIX shell command.

        Returns
        -------
        str
            Every element quoted, safe to place in a crontab line.
        """
        return ' '.join(cron_quote(item) for item in self.argv)

    def systemd_command(self) -> str:
        """Return the job as a systemd ``ExecStart=`` value.

        Returns
        -------
        str
            Every element quoted according to systemd's rules.
        """
        return ' '.join(systemd_quote(item) for item in self.argv)

    def windows_arguments(self) -> str:
        """Return the arguments as one Windows command line string.

        Returns
        -------
        str
            The arguments (without the executable) quoted the way the
            Windows C runtime parses them.
        """
        return subprocess.list2cmdline(list(self.args))


def _executable_names(is_windows: bool) -> tuple[str, ...]:
    """Return the file names the console script can have.

    Parameters
    ----------
    is_windows : bool
        Whether the target platform is Windows.

    Returns
    -------
    tuple of str
        Candidate file names in preference order.
    """
    if is_windows:
        return (f'{EXECUTABLE_NAME}.exe', EXECUTABLE_NAME)
    return (EXECUTABLE_NAME,)


def _temp_roots(override: Sequence[Path] | None = None) -> list[Path]:
    """Return directories a scheduled command must never live in.

    Parameters
    ----------
    override : sequence of pathlib.Path or None, optional
        Explicit roots, replacing the platform temporary directory. The
        tests pass their own, because pytest's ``tmp_path`` itself lives
        inside the temporary directory this check exists to reject.

    Returns
    -------
    list of pathlib.Path
        Resolved temporary directory roots.
    """
    roots = (
        [Path(tempfile.gettempdir())] if override is None else list(override)
    )
    resolved = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    return resolved


def _is_temporary(path: Path, roots: Sequence[Path]) -> bool:
    """Whether ``path`` lives inside one of ``roots``.

    Parameters
    ----------
    path : pathlib.Path
        An absolute, resolved path.
    roots : sequence of pathlib.Path
        Temporary directory roots.

    Returns
    -------
    bool
        ``True`` when the path is inside a temporary directory.
    """
    return any(path == root or root in path.parents for root in roots)


def resolve_executable(
    *,
    argv0: str | None = None,
    exec_prefix: Path | None = None,
    which: WhichCallable = default_which,
    is_windows: bool | None = None,
    temp_roots: Sequence[Path] | None = None,
) -> Path:
    """Locate the installed ``fleet-usage`` executable.

    A scheduler entry outlives the shell that created it, so it must
    name an absolute, permanent path. The console script next to the
    running interpreter is preferred because it is the one that belongs
    to this installation; ``sys.argv[0]`` and the ``PATH`` are only
    fallbacks.

    Parameters
    ----------
    argv0 : str or None, optional
        Value of ``sys.argv[0]``; read from :mod:`sys` when omitted.
    exec_prefix : pathlib.Path or None, optional
        Directory holding the console scripts; defaults to the parent of
        ``sys.executable``.
    which : callable, optional
        ``PATH`` lookup, injected by the tests.
    is_windows : bool or None, optional
        Whether the target platform is Windows.
    temp_roots : sequence of pathlib.Path or None, optional
        Temporary roots to refuse, replacing the platform default.

    Returns
    -------
    pathlib.Path
        The absolute path of the executable.

    Raises
    ------
    SchedulerError
        If no executable can be found, or the one found sits inside a
        temporary directory and would disappear.
    """
    windows = sys.platform == 'win32' if is_windows is None else is_windows
    names = _executable_names(windows)
    prefix = (
        Path(sys.executable).parent if exec_prefix is None else exec_prefix
    )
    candidates: list[Path] = []
    for name in names:
        candidates.append(prefix / name)
    raw_argv0 = sys.argv[0] if argv0 is None else argv0
    if raw_argv0:
        argv0_path = Path(raw_argv0)
        if argv0_path.name in names:
            candidates.append(argv0_path)
    for name in names:
        found = which(name)
        if found:
            candidates.append(Path(found))

    roots = _temp_roots(temp_roots)
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if not resolved.is_file():
            continue
        if _is_temporary(resolved, roots):
            msg = (
                f'refusing to schedule {resolved}: it is inside a '
                'temporary directory and will not survive a reboot\n'
                f'install fleet-usage permanently (for example with '
                f"'uv tool install fleet-usage') and try again"
            )
            raise SchedulerError(msg)
        return resolved
    msg = (
        f'cannot locate the {EXECUTABLE_NAME!r} executable\n'
        'a scheduled job needs an absolute path to it; install the '
        'package so that the console script exists, for example with '
        "'uv tool install fleet-usage'"
    )
    raise SchedulerError(msg)


def build_launch_spec(
    *,
    settings_path: Path,
    log_dir: Path,
    interval_minutes: int,
    executable: Path | None = None,
    argv0: str | None = None,
    exec_prefix: Path | None = None,
    which: WhichCallable = default_which,
    is_windows: bool | None = None,
    temp_roots: Sequence[Path] | None = None,
) -> LaunchSpec:
    """Assemble the :class:`LaunchSpec` for this installation.

    Parameters
    ----------
    settings_path : pathlib.Path
        Path of ``settings.toml``; it is made absolute.
    log_dir : pathlib.Path
        Directory for the job output.
    interval_minutes : int
        Requested interval; validated here so that every backend can
        rely on it.
    executable : pathlib.Path or None, optional
        Explicit executable path, bypassing the search.
    argv0 : str or None, optional
        Forwarded to :func:`resolve_executable`.
    exec_prefix : pathlib.Path or None, optional
        Forwarded to :func:`resolve_executable`.
    which : callable, optional
        Forwarded to :func:`resolve_executable`.
    is_windows : bool or None, optional
        Forwarded to :func:`resolve_executable`.
    temp_roots : sequence of pathlib.Path or None, optional
        Forwarded to :func:`resolve_executable`.

    Returns
    -------
    LaunchSpec
        The specification of the recurring job.

    Raises
    ------
    SchedulerError
        If the interval is unsupported or no executable is found.
    """
    interval = validate_interval(interval_minutes)
    program = (
        resolve_executable(
            argv0=argv0,
            exec_prefix=exec_prefix,
            which=which,
            is_windows=is_windows,
            temp_roots=temp_roots,
        )
        if executable is None
        else executable
    )
    absolute_settings = settings_path.expanduser().absolute()
    args = ('--config', str(absolute_settings), 'publish', '--if-due')
    return LaunchSpec(
        executable=program,
        args=args,
        settings_path=absolute_settings,
        interval_minutes=interval,
        log_dir=log_dir.expanduser().absolute(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ScheduleStatus:
    """State of the scheduled job.

    Attributes
    ----------
    installed
        Whether the job exists.
    backend
        Name of the backend that reported the state.
    detail
        Human readable description, for example the next run time.
    expression
        The backend specific time specification, when one is installed.
    command
        The command the scheduler is configured to run.
    notes
        Additional warnings, such as a missing systemd linger setting.
    """

    installed: bool
    backend: str
    detail: str
    expression: str | None = None
    command: str | None = None
    notes: tuple[str, ...] = ()

    def render(self) -> str:
        """Render the status as human readable lines.

        Returns
        -------
        str
            One ``key: value`` line per known fact.
        """
        state = 'installed' if self.installed else 'not installed'
        lines = [f'backend: {self.backend}', f'state: {state}']
        if self.expression:
            lines.append(f'schedule: {self.expression}')
        if self.command:
            lines.append(f'command: {self.command}')
        if self.detail:
            lines.extend(f'  {line}' for line in self.detail.splitlines())
        lines.extend(f'note: {note}' for note in self.notes)
        return '\n'.join(lines)


class Scheduler(abc.ABC):
    """Install, inspect and remove the recurring publish job."""

    name: str

    def __init__(
        self,
        spec: LaunchSpec,
        *,
        runner: Runner = subprocess_runner,
        which: WhichCallable = default_which,
        minute_offset: int | None = None,
    ) -> None:
        """Store the job specification and the injectable seams.

        Parameters
        ----------
        spec : LaunchSpec
            The job to schedule.
        runner : Runner, optional
            Subprocess seam.
        which : callable, optional
            ``PATH`` lookup seam.
        minute_offset : int or None, optional
            Minute within the hour; derived from the machine id when
            omitted.
        """
        self.spec = spec
        self.run = runner
        self.which = which
        self.offset = (
            machine_minute_offset() if minute_offset is None else minute_offset
        )

    def _spec_for(self, interval_minutes: int) -> LaunchSpec:
        """Return the spec with ``interval_minutes`` applied.

        Parameters
        ----------
        interval_minutes : int
            Requested interval.

        Returns
        -------
        LaunchSpec
            A copy carrying the validated interval.

        Raises
        ------
        SchedulerError
            If the interval is unsupported.
        """
        interval = validate_interval(interval_minutes)
        return dataclasses.replace(self.spec, interval_minutes=interval)

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this backend can be used on this machine.

        Returns
        -------
        bool
            ``True`` when the required tooling is present.
        """

    @abc.abstractmethod
    def install(self, interval_minutes: int, dry_run: bool = False) -> str:
        """Install the recurring job.

        Parameters
        ----------
        interval_minutes : int
            Desired interval between runs.
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of what was done.
        """

    @abc.abstractmethod
    def status(self) -> ScheduleStatus:
        """Report the current state of the job.

        Returns
        -------
        ScheduleStatus
            The observed state.
        """

    @abc.abstractmethod
    def uninstall(self, dry_run: bool = False) -> str:
        """Remove the recurring job.

        Parameters
        ----------
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of what was done.
        """


def _user_bus_present(env: Mapping[str, str]) -> bool:
    """Whether the environment points at a systemd user bus.

    Parameters
    ----------
    env : mapping of str to str
        The environment to inspect.

    Returns
    -------
    bool
        ``True`` when a user session bus is reachable.
    """
    return bool(
        env.get('XDG_RUNTIME_DIR') or env.get('DBUS_SESSION_BUS_ADDRESS')
    )


def select_backend(
    name: str = 'auto',
    *,
    spec: LaunchSpec,
    platform: str | None = None,
    runner: Runner = subprocess_runner,
    which: WhichCallable = default_which,
    env: Mapping[str, str] | None = None,
    minute_offset: int | None = None,
) -> Scheduler:
    """Return the scheduler backend to use.

    Parameters
    ----------
    name : str, optional
        ``'auto'``, ``'cron'``, ``'systemd'`` or ``'windows'``.
    spec : LaunchSpec
        The job to schedule.
    platform : str or None, optional
        Platform string, defaulting to :data:`sys.platform`.
    runner : Runner, optional
        Subprocess seam.
    which : callable, optional
        ``PATH`` lookup seam.
    env : mapping of str to str or None, optional
        Environment used to detect a systemd user bus.
    minute_offset : int or None, optional
        Minute within the hour.

    Returns
    -------
    Scheduler
        A ready to use backend.

    Raises
    ------
    SchedulerError
        If the requested backend is unknown or unusable, or if automatic
        selection finds no supported scheduler.
    """
    # Imported here: the concrete backends import this module.
    from fleet_usage.scheduling.cron import CronScheduler
    from fleet_usage.scheduling.systemd import SystemdScheduler
    from fleet_usage.scheduling.windows import WindowsScheduler

    system = sys.platform if platform is None else platform
    environ: Mapping[str, str] = os.environ if env is None else env

    def make_cron() -> Scheduler:
        return CronScheduler(
            spec,
            runner=runner,
            which=which,
            minute_offset=minute_offset,
        )

    def make_systemd() -> Scheduler:
        return SystemdScheduler(
            spec,
            runner=runner,
            which=which,
            minute_offset=minute_offset,
            env=environ,
        )

    def make_windows() -> Scheduler:
        return WindowsScheduler(
            spec,
            runner=runner,
            which=which,
            minute_offset=minute_offset,
            platform=system,
        )

    factories: dict[str, Callable[[], Scheduler]] = {
        'cron': make_cron,
        'systemd': make_systemd,
        'windows': make_windows,
    }
    if name != 'auto':
        factory = factories.get(name)
        if factory is None:
            known = ', '.join(sorted(factories))
            msg = f'unknown scheduler backend {name!r}; known: {known}'
            raise SchedulerError(msg)
        backend = factory()
        if not backend.available():
            msg = (
                f'the {name} backend is not usable on this machine\n'
                f'{_unavailable_hint(name)}'
            )
            raise SchedulerError(msg)
        return backend

    if system == 'win32':
        return make_windows()
    if _user_bus_present(environ) and which('systemctl') is not None:
        probe = runner(['systemctl', '--user', 'show', '--property=Version'])
        if probe.ok:
            return make_systemd()
    if which('crontab') is not None:
        return make_cron()
    msg = (
        'no supported scheduler found on this machine\n'
        'install a systemd user session or a cron implementation, or '
        'run fleet-usage publish from your own scheduler'
    )
    raise SchedulerError(msg)


def _unavailable_hint(name: str) -> str:
    """Return advice for an explicitly requested, unusable backend.

    Parameters
    ----------
    name : str
        Backend name.

    Returns
    -------
    str
        A single line of advice.
    """
    hints = {
        'cron': "no 'crontab' program on PATH",
        'systemd': (
            "no 'systemctl' program on PATH, or no systemd user session "
            '(XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS are unset)'
        ),
        'windows': 'the Windows Task Scheduler exists only on Windows',
    }
    return hints.get(name, 'the required tooling is missing')
