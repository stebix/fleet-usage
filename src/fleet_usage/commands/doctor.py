"""``fleet-usage doctor``: check that this installation can work."""

import dataclasses
import datetime as dt
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.table import Table

from fleet_usage.commands._common import config_path_of
from fleet_usage.config import (
    ConfigError,
    Settings,
    load_settings,
    resolve_token,
)
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.fetch import FileSource, build_client, ledger_remote_path
from fleet_usage.github import GitHubError, NotFoundError, check_access
from fleet_usage.models import Ledger
from fleet_usage.paths import AppPaths, get_paths
from fleet_usage.scheduling.base import is_standard_path_dir
from fleet_usage.scheduling.systemd import SERVICE_NAME, unit_directory
from fleet_usage.scheduling.windows import inherits_directory
from fleet_usage.ui import out_console, print_error

__all__ = [
    'Check',
    'Status',
    'doctor_command',
    'fetch_ledger',
    'run_checks',
]

Status = str
OK = 'ok'
WARN = 'warn'
FAIL = 'fail'
SKIP = 'skip'

_STYLES = {
    OK: 'green',
    WARN: 'yellow',
    FAIL: 'bold red',
    SKIP: 'dim',
}
VERSION_TIMEOUT_SECONDS = 30
PLACEHOLDER_TOKEN = 'replace-me'


@dataclasses.dataclass(frozen=True, slots=True)
class Check:
    """Outcome of a single diagnostic.

    Attributes
    ----------
    name
        Short identifier of the diagnostic.
    status
        One of ``'ok'``, ``'warn'``, ``'fail'`` or ``'skip'``.
    detail
        Human readable explanation.
    """

    name: str
    status: Status
    detail: str


def _check_paths(paths: AppPaths) -> list[Check]:
    """Verify that every directory the tool writes to is usable.

    Parameters
    ----------
    paths : AppPaths
        Resolved application paths.

    Returns
    -------
    list of Check
        One entry per directory.
    """
    directories = {
        'config dir': paths.config_dir,
        'state dir': paths.state_dir,
        'spool dir': paths.spool_dir,
        'cache dir': paths.ledger_cache_dir,
        'log dir': paths.log_dir,
    }
    checks: list[Check] = []
    for name, directory in directories.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError as exc:
            checks.append(Check(name, FAIL, f'{directory}: {exc}'))
        else:
            checks.append(Check(name, OK, str(directory)))
    return checks


def _collector_version(command: list[str]) -> tuple[bool, str]:
    """Run ``<collector> --version`` and return its last output line.

    Parameters
    ----------
    command : list of str
        Argument vector that launches the collector.

    Returns
    -------
    tuple of (bool, str)
        Whether the call succeeded and the reported version or the
        error text. The command is never run through a shell.
    """
    try:
        completed = subprocess.run(
            [*command, '--version'],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, 'timed out'
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return False, detail or f'exit code {completed.returncode}'
    lines = [line.strip() for line in completed.stdout.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return False, 'no version output'
    return True, lines[-1]


def _installed_service_text() -> str | None:
    """Return the text of the installed systemd service unit.

    Returns
    -------
    str or None
        The unit file contents, or ``None`` when no user unit was
        written. Reading one file is cheap enough for a diagnostic;
        interrogating every backend is not.
    """
    try:
        return (unit_directory() / SERVICE_NAME).read_text(encoding='utf-8')
    except OSError:
        return None


def _check_collector_path(executable: Path) -> Check | None:
    """Check that a scheduled run would also find the collector.

    A POSIX scheduler starts its jobs with a short ``PATH``
    (:data:`~fleet_usage.scheduling.base.STANDARD_PATH_DIRS`), so a
    collector installed under the home directory works interactively and
    is invisible to the timer. ``schedule install`` writes the directory
    into the job; this check reports when it did not.

    Windows carries nothing, because there is nothing to carry: a task
    inherits the ``PATH`` the registry persists for the machine and the
    account. The question there is whether the collector directory is on
    that ``PATH``, and no reinstall of the job can change the answer.

    Parameters
    ----------
    executable : pathlib.Path
        The resolved collector program.

    Returns
    -------
    Check or None
        ``None`` when the collector lives in a standard directory and
        nothing has to be carried, otherwise a check describing whether
        a scheduled run would find it.
    """
    directory = executable.parent
    if is_standard_path_dir(directory):
        return None
    name = 'collector PATH'
    if sys.platform == 'win32':
        check = _inherited_path_check(name, directory)
    else:
        check = _carried_path_check(name, directory)
    return check


def _inherited_path_check(name: str, directory: Path) -> Check:
    """Judge a directory against the ``PATH`` a Windows task inherits.

    Parameters
    ----------
    name : str
        Name the check is reported under.
    directory : pathlib.Path
        Directory the collector was resolved from.

    Returns
    -------
    Check
        Whether the registry persists the directory for a scheduled run.
    """
    if inherits_directory(directory):
        return Check(name, OK, f'a scheduled task inherits {directory}')
    return Check(
        name,
        WARN,
        f'{directory} is on the PATH of this shell only, so a '
        'scheduled task cannot find the collector; add it to the '
        'PATH of your account under "Edit environment variables '
        'for your account"',
    )


def _carried_path_check(name: str, directory: Path) -> Check:
    """Judge a directory against the ``PATH`` a POSIX job carries.

    Parameters
    ----------
    name : str
        Name the check is reported under.
    directory : pathlib.Path
        Directory the collector was resolved from.

    Returns
    -------
    Check
        Whether the installed unit carries the directory.
    """
    unit = _installed_service_text()
    if unit is not None and str(directory) in unit:
        return Check(name, OK, f'the scheduled job carries {directory}')
    return Check(
        name,
        WARN,
        f'{directory} is not on the PATH of a scheduled run; run '
        "'fleet-usage schedule install' so the job carries it",
    )


def _check_collector(settings: Settings) -> list[Check]:
    """Verify that the configured collector can be executed.

    Parameters
    ----------
    settings : Settings
        Loaded settings.

    Returns
    -------
    list of Check
        Resolution of the executable, the directory it was resolved
        from and, if it resolves, the version it reports compared
        against the configured one.
    """
    collector = settings.collector
    if collector is None:
        return [Check('collector', SKIP, 'viewer-only install, no collector')]
    executable = shutil.which(collector.command[0])
    if executable is None:
        return [
            Check(
                'collector',
                FAIL,
                f'{collector.command[0]!r} not found on PATH',
            )
        ]
    program = Path(executable)
    checks = [Check('collector', OK, f'{program} (from {program.parent})')]
    path_check = _check_collector_path(program)
    if path_check is not None:
        checks.append(path_check)
    succeeded, reported = _collector_version(collector.command)
    if not succeeded:
        checks.append(Check('collector version', FAIL, reported))
        return checks
    expected = collector.version
    if expected is None:
        checks.append(Check('collector version', OK, f'{reported} (unpinned)'))
    elif expected in reported:
        checks.append(Check('collector version', OK, reported))
    else:
        checks.append(
            Check(
                'collector version',
                WARN,
                f'reports {reported!r}, settings pin {expected!r}',
            )
        )
    return checks


def fetch_ledger(client: FileSource, machine_id: str) -> Ledger | None:
    """Download the ledger of one machine, tolerating its absence.

    Parameters
    ----------
    client : FileSource
        A client bound to the data repository.
    machine_id : str
        Machine whose ledger is wanted.

    Returns
    -------
    Ledger or None
        The parsed ledger, or ``None`` when it does not exist, cannot
        be reached or cannot be parsed. A diagnostic never fails
        because of a document it only wanted to look at.
    """
    try:
        content, _sha = client.get_file(ledger_remote_path(machine_id))
    except NotFoundError:
        return None
    except GitHubError:
        return None
    try:
        return Ledger.model_validate_json(content)
    except ValidationError:
        return None


def _check_clock(
    client: FileSource, machine_id: str, now: dt.datetime
) -> Check:
    """Compare the local clock against the last published run.

    A clock that lags behind produces snapshot names that sort before
    ``applied_through``, so the ledger silently stops growing. That is
    invisible in every other check, hence this one.

    Parameters
    ----------
    client : FileSource
        A client bound to the data repository.
    machine_id : str
        Machine whose ledger carries the reference timestamp.
    now : datetime.datetime
        The current instant according to this machine.

    Returns
    -------
    Check
        ``WARN`` when the local clock is behind the last published run,
        ``OK`` when it is not and ``SKIP`` when there is nothing to
        compare against.
    """
    name = 'clock'
    ledger = fetch_ledger(client, machine_id)
    if ledger is None or ledger.last_run_at is None:
        return Check(name, SKIP, 'no published run to compare against')
    last = ledger.last_run_at
    if now.astimezone(dt.UTC) < last:
        return Check(
            name,
            WARN,
            'local clock is behind the last published run; snapshots '
            'may sort before applied_through, run '
            '`fleet-usage rebuild` after fixing the clock '
            f'(last run {last:%Y-%m-%dT%H:%M:%SZ})',
        )
    return Check(name, OK, f'last published run {last:%Y-%m-%dT%H:%M:%SZ}')


def _check_github(settings: Settings, token: str | None) -> list[Check]:
    """Probe the data repository with the configured token.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    token : str or None
        The resolved token, if any.

    Returns
    -------
    list of Check
        Reachability: ``OK`` when the token can write, ``WARN`` when it
        can only read (enough for a viewer), ``FAIL`` when the
        repository is unreachable and ``SKIP`` without a usable token.
        A publisher whose token can read is also checked against the
        clock of its own ledger.
    """
    name = 'github reachability'
    if token is None or token == PLACEHOLDER_TOKEN:
        return [Check(name, SKIP, 'no usable token')]
    client = build_client(settings)
    try:
        report = check_access(client)
        if not report.can_read:
            return [Check(name, FAIL, report.detail)]
        status = OK if report.can_write or settings.viewer_only else WARN
        checks = [Check(name, status, report.detail)]
        machine = settings.machine
        if machine is not None:
            checks.append(
                _check_clock(client, machine.id, dt.datetime.now(dt.UTC))
            )
        return checks
    finally:
        client.close()


def run_checks(config_path: Path | None) -> list[Check]:
    """Run every diagnostic.

    Parameters
    ----------
    config_path : pathlib.Path or None
        Explicit settings path from ``--config``.

    Returns
    -------
    list of Check
        The results in display order.
    """
    paths = get_paths()
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        return [Check('settings', FAIL, str(exc).splitlines()[0])]

    checks = [Check('settings', OK, str(settings.source_path))]
    mode = 'viewer-only' if settings.viewer_only else 'publisher'
    checks.append(Check('mode', OK, mode))

    token = resolve_token(settings)
    name = settings.github.token_env
    if token is None:
        checks.append(Check('github token', FAIL, f'{name} is not set'))
    elif token == PLACEHOLDER_TOKEN:
        checks.append(
            Check(
                'github token',
                WARN,
                f'{name} still holds the placeholder from init',
            )
        )
    else:
        checks.append(Check('github token', OK, f'{name} is set'))

    checks.extend(_check_github(settings, token))
    checks.extend(_check_paths(paths))
    checks.extend(_check_collector(settings))
    return checks


def doctor_command(ctx: typer.Context) -> None:
    """Report whether this installation is ready to run.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    """
    checks = run_checks(config_path_of(ctx))
    table = Table(title='fleet-usage doctor', highlight=False)
    table.add_column('check')
    table.add_column('status')
    table.add_column('detail', overflow='fold')
    for check in checks:
        table.add_row(
            check.name,
            f'[{_STYLES[check.status]}]{check.status}[/]',
            check.detail,
        )
    out_console().print(table)
    failures = [check for check in checks if check.status == FAIL]
    if failures:
        print_error(f'{len(failures)} check(s) failed')
        exit_with(ExitCode.CONFIG_ERROR)
