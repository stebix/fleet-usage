"""Scheduler backend based on a systemd user timer.

A user timer is the right tool on a modern Linux desktop: it survives
suspend (``Persistent=true`` catches up a missed run), it spreads the
load across the fleet (``RandomizedDelaySec=``) and its output ends up
both in a log file and in the journal. The one sharp edge is lingering:
without ``loginctl enable-linger`` the user manager stops at logout, so
the status output says so explicitly.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from fleet_usage.scheduling.base import (
    SYSTEMD_PATH_DIRS,
    LaunchSpec,
    Runner,
    Scheduler,
    SchedulerError,
    ScheduleStatus,
    WhichCallable,
    default_which,
    oncalendar_expression,
    path_value,
    subprocess_runner,
    systemd_quote,
)

__all__ = [
    'LOG_NAME',
    'SERVICE_NAME',
    'TIMER_NAME',
    'SystemdScheduler',
    'render_environment',
    'render_service',
    'render_timer',
    'unit_directory',
]

SERVICE_NAME = 'fleet-usage.service'
TIMER_NAME = 'fleet-usage.timer'
LOG_NAME = 'systemd.log'
RANDOMIZED_DELAY_SECONDS = 120
_SHOW_PROPERTIES = 'ActiveState,NextElapseUSecRealtime,LastTriggerUSec'


def unit_directory(env: Mapping[str, str] | None = None) -> Path:
    """Return the directory holding the user unit files.

    Parameters
    ----------
    env : mapping of str to str or None, optional
        Environment to inspect; defaults to :data:`os.environ`.

    Returns
    -------
    pathlib.Path
        ``$XDG_CONFIG_HOME/systemd/user``, defaulting to
        ``~/.config/systemd/user``.
    """
    environ = os.environ if env is None else env
    base = environ.get('XDG_CONFIG_HOME')
    root = Path(base) if base else Path.home() / '.config'
    return root / 'systemd' / 'user'


def render_environment(spec: LaunchSpec) -> str:
    """Render the ``Environment=`` directives of the service.

    The systemd user manager starts a service with a ``PATH`` of
    ``/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`` only, so a
    collector installed under the home directory is invisible to it. The
    directory resolved at install time is prepended, which keeps the
    scheduled run equivalent to the interactive one.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.

    Returns
    -------
    str
        A ``PATH`` assignment line, or an empty string when the job
        needs nothing beyond the default directories.
    """
    if not spec.extra_path_dirs:
        return ''
    value = path_value(spec.extra_path_dirs, SYSTEMD_PATH_DIRS)
    return f'Environment={systemd_quote(f"PATH={value}")}\n'


def render_service(spec: LaunchSpec) -> str:
    """Render the ``fleet-usage.service`` unit.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.

    Returns
    -------
    str
        The complete unit file, ending in a newline.
    """
    log_file = spec.log_dir / LOG_NAME
    append = f'append:{log_file}'
    return (
        '[Unit]\n'
        'Description=Collect and publish AI coding agent usage\n'
        'After=network-online.target\n'
        'Wants=network-online.target\n'
        '\n'
        '[Service]\n'
        'Type=oneshot\n'
        f'{render_environment(spec)}'
        f'ExecStart={spec.systemd_command()}\n'
        f'StandardOutput={append}\n'
        f'StandardError={append}\n'
        'TimeoutStartSec=1800\n'
    )


def render_timer(spec: LaunchSpec, offset: int) -> str:
    """Render the ``fleet-usage.timer`` unit.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.
    offset : int
        Minute within the hour at which this machine runs.

    Returns
    -------
    str
        The complete unit file, ending in a newline.

    Raises
    ------
    SchedulerError
        If the interval cannot be expressed as a calendar event.
    """
    calendar = oncalendar_expression(spec.interval_minutes, offset)
    minutes = spec.interval_minutes
    return (
        '[Unit]\n'
        f'Description=Run fleet-usage publish every {minutes} minutes\n'
        '\n'
        '[Timer]\n'
        f'OnCalendar={calendar}\n'
        'Persistent=true\n'
        f'RandomizedDelaySec={RANDOMIZED_DELAY_SECONDS}\n'
        'AccuracySec=1min\n'
        f'Unit={SERVICE_NAME}\n'
        '\n'
        '[Install]\n'
        'WantedBy=timers.target\n'
    )


def _parse_show(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` key/value output.

    Parameters
    ----------
    text : str
        Output of ``systemctl show -p ...``.

    Returns
    -------
    dict of str to str
        The properties; values may be empty.
    """
    properties: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        if separator:
            properties[key.strip()] = value.strip()
    return properties


class SystemdScheduler(Scheduler):
    """Manage the ``fleet-usage`` user service and timer."""

    name = 'systemd'

    def __init__(
        self,
        spec: LaunchSpec,
        *,
        runner: Runner = subprocess_runner,
        which: WhichCallable = default_which,
        minute_offset: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Store the job specification, the seams and the environment.

        Parameters
        ----------
        spec : LaunchSpec
            The job to schedule.
        runner : Runner, optional
            Subprocess seam.
        which : callable, optional
            ``PATH`` lookup seam.
        minute_offset : int or None, optional
            Minute within the hour.
        env : mapping of str to str or None, optional
            Environment used to locate the unit directory and to detect
            a user bus.
        """
        super().__init__(
            spec,
            runner=runner,
            which=which,
            minute_offset=minute_offset,
        )
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.unit_dir = unit_directory(self.env)

    @property
    def service_path(self) -> Path:
        """Location of the service unit file.

        Returns
        -------
        pathlib.Path
            Path of ``fleet-usage.service``.
        """
        return self.unit_dir / SERVICE_NAME

    @property
    def timer_path(self) -> Path:
        """Location of the timer unit file.

        Returns
        -------
        pathlib.Path
            Path of ``fleet-usage.timer``.
        """
        return self.unit_dir / TIMER_NAME

    def available(self) -> bool:
        """Whether a systemd user session can be addressed.

        Returns
        -------
        bool
            ``True`` when ``systemctl`` exists and the environment
            points at a user bus.
        """
        if self.which('systemctl') is None:
            return False
        return bool(
            self.env.get('XDG_RUNTIME_DIR')
            or self.env.get('DBUS_SESSION_BUS_ADDRESS')
        )

    def _require(self, args: list[str]) -> None:
        """Run a ``systemctl --user`` command and demand success.

        Parameters
        ----------
        args : list of str
            Arguments after ``--user``.

        Raises
        ------
        SchedulerError
            If the command fails.
        """
        result = self.run(['systemctl', '--user', *args])
        if not result.ok:
            joined = ' '.join(['systemctl', '--user', *args])
            msg = f'{joined} failed: {result.message()}'
            raise SchedulerError(msg)

    def install(self, interval_minutes: int, dry_run: bool = False) -> str:
        """Write the unit files and enable the timer.

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
            If systemd refuses the units.
        """
        spec = self._spec_for(interval_minutes)
        service = render_service(spec)
        timer = render_timer(spec, self.offset)
        preview = (
            f'--- {self.service_path}\n{service}'
            f'--- {self.timer_path}\n{timer}'
            'then: systemctl --user daemon-reload\n'
            f'      systemctl --user enable --now {TIMER_NAME}\n'
        )
        if dry_run:
            return f'dry run: would write\n{preview}'
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.unit_dir.mkdir(parents=True, exist_ok=True)
            self.service_path.write_text(service, encoding='utf-8')
            self.timer_path.write_text(timer, encoding='utf-8')
        except OSError as exc:
            msg = f'cannot write the unit files: {exc}'
            raise SchedulerError(msg) from exc
        self._require(['daemon-reload'])
        self._require(['enable', '--now', TIMER_NAME])
        return (
            f'wrote {self.service_path}\n'
            f'wrote {self.timer_path}\n'
            f'enabled and started {TIMER_NAME}'
        )

    def _linger_note(self) -> str | None:
        """Return a warning when the user session does not linger.

        Returns
        -------
        str or None
            Advice for server users, or ``None`` when lingering is on
            or cannot be determined.
        """
        user = self.env.get('USER') or self.env.get('USERNAME')
        if not user or self.which('loginctl') is None:
            return None
        result = self.run(['loginctl', 'show-user', user, '-p', 'Linger'])
        if not result.ok:
            return None
        value = _parse_show(result.stdout).get('Linger', '')
        if value.lower() == 'yes':
            return None
        return (
            'linger is not enabled for this user, so the timer stops at '
            f"logout; run 'loginctl enable-linger {user}' to keep it "
            'running'
        )

    def status(self) -> ScheduleStatus:
        """Report the state of the timer.

        Returns
        -------
        ScheduleStatus
            The observed state.
        """
        if not self.available():
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail='no systemd user session on this machine',
            )
        units_present = self.timer_path.is_file()
        show = self.run(
            [
                'systemctl',
                '--user',
                'show',
                TIMER_NAME,
                '-p',
                _SHOW_PROPERTIES,
            ]
        )
        properties = _parse_show(show.stdout) if show.ok else {}
        active = properties.get('ActiveState', 'unknown')
        listing = self.run(
            [
                'systemctl',
                '--user',
                'list-timers',
                '--all',
                '--no-pager',
                TIMER_NAME,
            ]
        )
        detail_lines = [
            f'unit file: {self.timer_path}',
            f'ActiveState: {active}',
        ]
        for key in ('NextElapseUSecRealtime', 'LastTriggerUSec'):
            value = properties.get(key)
            if value:
                detail_lines.append(f'{key}: {value}')
        if listing.ok and listing.stdout.strip():
            detail_lines.extend(listing.stdout.strip().splitlines())
        notes = []
        note = self._linger_note()
        if note is not None:
            notes.append(note)
        expression = None
        command = None
        if units_present:
            expression = _directive(self.timer_path, 'OnCalendar=')
            command = _directive(self.service_path, 'ExecStart=')
        installed = units_present and active == 'active'
        if units_present and not installed:
            detail_lines.append(
                'the unit files exist but the timer is not active; run '
                f"'systemctl --user enable --now {TIMER_NAME}'"
            )
        return ScheduleStatus(
            installed=installed,
            backend=self.name,
            detail='\n'.join(detail_lines),
            expression=expression,
            command=command,
            notes=tuple(notes),
        )

    def uninstall(self, dry_run: bool = False) -> str:
        """Disable the timer and delete both unit files.

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
            If a unit file cannot be deleted.
        """
        present = self.timer_path.is_file() or self.service_path.is_file()
        if dry_run:
            if not present:
                return 'dry run: no unit files to remove'
            return (
                f'dry run: would run systemctl --user disable --now '
                f'{TIMER_NAME}\n'
                f'and delete {self.timer_path} and {self.service_path}'
            )
        if not present:
            return 'no fleet-usage unit files found; nothing to do'
        self.run(['systemctl', '--user', 'disable', '--now', TIMER_NAME])
        removed = []
        try:
            for path in (self.timer_path, self.service_path):
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
        except OSError as exc:
            msg = f'cannot delete the unit files: {exc}'
            raise SchedulerError(msg) from exc
        self.run(['systemctl', '--user', 'daemon-reload'])
        listed = '\n'.join(f'removed {item}' for item in removed)
        return f'disabled {TIMER_NAME}\n{listed}'


def _directive(path: Path, prefix: str) -> str | None:
    """Return the value of a unit file directive.

    Parameters
    ----------
    path : pathlib.Path
        A unit file.
    prefix : str
        Directive prefix including the ``=``.

    Returns
    -------
    str or None
        The value, or ``None`` when the file or directive is absent.
    """
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None
