"""``fleet-usage schedule``: install the recurring publish job.

The command layer stays thin: it resolves the settings, turns them into a
:class:`~fleet_usage.scheduling.base.LaunchSpec`, picks a backend and
prints what the backend reports. Two module level seams --
:data:`RUNNER` and :data:`EXECUTABLE` -- let the integration tests drive
every backend without touching the machine they run on.
"""

import enum
from pathlib import Path
from typing import Annotated, cast

import typer

from fleet_usage.commands._common import config_path_of, load_or_exit
from fleet_usage.config import Settings
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.paths import get_paths
from fleet_usage.scheduling.base import (
    LaunchSpec,
    Runner,
    Scheduler,
    SchedulerError,
    build_launch_spec,
    checkout_root,
    select_backend,
    subprocess_runner,
)
from fleet_usage.scheduling.windows import (
    LogonMode as WindowsLogonMode,
)
from fleet_usage.scheduling.windows import (
    WindowsScheduler,
)
from fleet_usage.ui import out_console, print_error

__all__ = [
    'EXECUTABLE',
    'RUNNER',
    'Backend',
    'LogonMode',
    'app',
    'install_command',
    'status_command',
    'uninstall_command',
]

DEV_CHECKOUT_WARNING = (
    'warning: the scheduled command is the console script of a '
    'development checkout; it stops working as soon as the virtual '
    "environment is rebuilt or the checkout moves ('uv tool install "
    "{checkout}' installs a stable one)"
)

#: Subprocess seam; the tests replace it with a recording fake.
RUNNER: Runner = subprocess_runner
#: Executable seam; when set it bypasses the console script lookup.
EXECUTABLE: Path | None = None

app = typer.Typer(
    name='schedule',
    help='Install, inspect and remove the scheduled publish job.',
    no_args_is_help=True,
)


class Backend(enum.StrEnum):
    """Supported scheduler backends."""

    AUTO = 'auto'
    CRON = 'cron'
    SYSTEMD = 'systemd'
    WINDOWS = 'windows'


class LogonMode(enum.StrEnum):
    """How a Windows scheduled task authenticates."""

    INTERACTIVE = 'interactive'
    S4U = 's4u'
    PASSWORD = 'password'


BackendOption = Annotated[
    Backend,
    typer.Option('--backend', help='Scheduler backend to use.'),
]
DryRunOption = Annotated[
    bool,
    typer.Option('--dry-run', help='Print what would be done.'),
]
IntervalOption = Annotated[
    int | None,
    typer.Option(
        '--interval',
        help='Minutes between runs; defaults to [sync].interval_minutes.',
        metavar='MINUTES',
    ),
]
LogonModeOption = Annotated[
    LogonMode,
    typer.Option(
        '--logon-mode',
        help='Windows only: how the scheduled task authenticates.',
    ),
]
ForceOption = Annotated[
    bool,
    typer.Option(
        '--force',
        help='Install even when the collector cannot be resolved on PATH.',
    ),
]
AllowDevCheckoutOption = Annotated[
    bool,
    typer.Option(
        '--allow-dev-checkout',
        help='Allow scheduling the console script of a development checkout.',
    ),
]


def _fail(exc: SchedulerError) -> None:
    """Report a scheduler failure and exit with a configuration error.

    Parameters
    ----------
    exc : SchedulerError
        The failure to report.
    """
    print_error(str(exc))
    exit_with(ExitCode.CONFIG_ERROR)


def _require_publisher(settings: Settings) -> None:
    """Refuse to schedule a viewer-only installation.

    A viewer-only install has no machine identity and no collector, so
    the job it would run could only fail. Failing here, once, beats a
    scheduler that reports an error every hour.

    Parameters
    ----------
    settings : Settings
        The loaded settings.
    """
    if not settings.viewer_only:
        return
    print_error(
        'this installation is viewer-only and cannot publish',
        'add a [machine] and a [collector] section to '
        f'{settings.source_path}, or re-run: fleet-usage init',
    )
    exit_with(ExitCode.CONFIG_ERROR)


def _warn_about_a_dev_checkout(spec: LaunchSpec) -> None:
    """Print a warning when the job runs out of a development checkout.

    Parameters
    ----------
    spec : LaunchSpec
        The job description.
    """
    checkout = checkout_root(spec.executable)
    if checkout is not None:
        out_console().print(
            DEV_CHECKOUT_WARNING.format(checkout=checkout), markup=False
        )


def _launch_spec(
    ctx: typer.Context,
    settings: Settings,
    interval_minutes: int,
    *,
    force: bool = False,
    allow_dev_checkout: bool = False,
) -> LaunchSpec:
    """Build the launch specification for this installation.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    settings : Settings
        The loaded settings.
    interval_minutes : int
        Requested interval.
    force : bool, optional
        Install even when the collector cannot be resolved.
    allow_dev_checkout : bool, optional
        Accept the console script of a development checkout.

    Returns
    -------
    LaunchSpec
        The job description.
    """
    settings_path = settings.source_path or config_path_of(ctx)
    if settings_path is None:  # pragma: no cover - defensive
        settings_path = get_paths().settings_file
    collector = settings.collector
    try:
        return build_launch_spec(
            settings_path=settings_path,
            log_dir=get_paths().log_dir,
            interval_minutes=interval_minutes,
            executable=EXECUTABLE,
            allow_dev_checkout=allow_dev_checkout,
            collector_command=None if collector is None else collector.command,
            force=force,
        )
    except SchedulerError as exc:
        _fail(exc)
        raise  # pragma: no cover - _fail never returns


def _backend(
    backend: Backend,
    spec: LaunchSpec,
    logon_mode: LogonMode = LogonMode.INTERACTIVE,
) -> Scheduler:
    """Select the scheduler backend.

    Parameters
    ----------
    backend : Backend
        Requested backend or ``auto``.
    spec : LaunchSpec
        The job description.
    logon_mode : LogonMode, optional
        Windows logon mode, applied when the Windows backend is chosen.

    Returns
    -------
    Scheduler
        A ready to use backend.
    """
    try:
        chosen = select_backend(str(backend), spec=spec, runner=RUNNER)
    except SchedulerError as exc:
        _fail(exc)
        raise  # pragma: no cover - _fail never returns
    if isinstance(chosen, WindowsScheduler):
        chosen.logon_mode = cast('WindowsLogonMode', str(logon_mode))
    return chosen


def _resolve_interval(
    settings: Settings,
    interval: int | None,
) -> int:
    """Return the interval to install.

    Parameters
    ----------
    settings : Settings
        The loaded settings.
    interval : int or None
        Value of ``--interval``.

    Returns
    -------
    int
        The requested interval, or the configured one.
    """
    return settings.sync.interval_minutes if interval is None else interval


@app.command('install')
def install_command(
    ctx: typer.Context,
    interval: IntervalOption = None,
    dry_run: DryRunOption = False,
    backend: BackendOption = Backend.AUTO,
    logon_mode: LogonModeOption = LogonMode.INTERACTIVE,
    force: ForceOption = False,
    allow_dev_checkout: AllowDevCheckoutOption = False,
) -> None:
    """Install the scheduled publish job.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    interval : int or None, optional
        Minutes between runs.
    dry_run : bool, optional
        Only print the planned changes.
    backend : Backend, optional
        Scheduler backend.
    logon_mode : LogonMode, optional
        Windows only: how the scheduled task authenticates.
    force : bool, optional
        Install even when the collector cannot be resolved on the
        ``PATH``.
    allow_dev_checkout : bool, optional
        Accept the console script of a development checkout.
    """
    settings = load_or_exit(ctx)
    _require_publisher(settings)
    minutes = _resolve_interval(settings, interval)
    spec = _launch_spec(
        ctx,
        settings,
        minutes,
        force=force,
        allow_dev_checkout=allow_dev_checkout,
    )
    scheduler = _backend(backend, spec, logon_mode)
    console = out_console()
    try:
        report = scheduler.install(minutes, dry_run=dry_run)
    except SchedulerError as exc:
        _fail(exc)
        return
    _warn_about_a_dev_checkout(spec)
    console.print(report)
    if dry_run:
        return
    try:
        console.print(scheduler.status().render())
    except SchedulerError as exc:
        _fail(exc)


@app.command('status')
def status_command(
    ctx: typer.Context,
    backend: BackendOption = Backend.AUTO,
) -> None:
    """Report the state of the scheduled publish job.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    backend : Backend, optional
        Scheduler backend.
    """
    settings = load_or_exit(ctx)
    minutes = settings.sync.interval_minutes
    try:
        spec = build_launch_spec(
            settings_path=settings.source_path or get_paths().settings_file,
            log_dir=get_paths().log_dir,
            interval_minutes=minutes,
            executable=EXECUTABLE,
            allow_dev_checkout=True,
            force=True,
        )
    except SchedulerError:
        # Status must work even where the executable cannot be located;
        # the spec is only used to name paths in the report.
        spec = LaunchSpec(
            executable=Path('fleet-usage'),
            args=(),
            settings_path=settings.source_path or get_paths().settings_file,
            interval_minutes=60,
            log_dir=get_paths().log_dir,
        )
    scheduler = _backend(backend, spec)
    try:
        status = scheduler.status()
    except SchedulerError as exc:
        _fail(exc)
        return
    _warn_about_a_dev_checkout(spec)
    out_console().print(status.render())


@app.command('uninstall')
def uninstall_command(
    ctx: typer.Context,
    dry_run: DryRunOption = False,
    backend: BackendOption = Backend.AUTO,
) -> None:
    """Remove the scheduled publish job.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    dry_run : bool, optional
        Only print the planned changes.
    backend : Backend, optional
        Scheduler backend.
    """
    settings = load_or_exit(ctx)
    spec = LaunchSpec(
        executable=EXECUTABLE or Path('fleet-usage'),
        args=(),
        settings_path=settings.source_path or get_paths().settings_file,
        interval_minutes=settings.sync.interval_minutes,
        log_dir=get_paths().log_dir,
    )
    scheduler = _backend(backend, spec)
    try:
        report = scheduler.uninstall(dry_run=dry_run)
    except SchedulerError as exc:
        _fail(exc)
        return
    out_console().print(report)
