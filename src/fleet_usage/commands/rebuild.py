"""``fleet-usage rebuild``: fold all snapshots into a fresh ledger."""

import datetime as dt
from typing import Annotated

import typer
from pydantic import ValidationError

from fleet_usage import ledger as ledger_module
from fleet_usage.commands._common import load_or_exit
from fleet_usage.config import Settings
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.fetch import (
    MANIFEST_PATH,
    FetchAuthError,
    build_client,
    ledger_remote_path,
    parse_manifest,
)
from fleet_usage.github import (
    AuthError,
    GitHubClient,
    GitHubError,
    NotFoundError,
)
from fleet_usage.models import Ledger, MergePolicy, Snapshot
from fleet_usage.ui import out_console, print_error

__all__ = ['rebuild_command', 'snapshot_prefix']


def snapshot_prefix(machine_id: str) -> str:
    """Return the repository prefix holding a machine's snapshots.

    Parameters
    ----------
    machine_id : str
        Machine id.

    Returns
    -------
    str
        ``snapshots/<machine_id>/``.
    """
    return f'snapshots/{machine_id}/'


def _resolve_machine(settings: Settings, machine: str | None) -> str:
    """Determine which machine to rebuild.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    machine : str or None
        Value of ``--machine``.

    Returns
    -------
    str
        The machine id.
    """
    if machine:
        return machine
    if settings.machine is None:
        print_error(
            'this installation has no [machine] section',
            'pass --machine ID to rebuild another machine',
        )
        exit_with(ExitCode.CONFIG_ERROR)
    return settings.machine.id


def _existing_ledger(
    client: GitHubClient,
    path: str,
) -> tuple[str | None, Ledger | None]:
    """Fetch the blob SHA and content of the ledger being replaced.

    Parameters
    ----------
    client : GitHubClient
        Transport.
    path : str
        Repository path of the ledger.

    Returns
    -------
    tuple of (str or None, Ledger or None)
        Blob SHA and parsed ledger. The SHA is ``None`` when the ledger
        does not exist yet, which is the normal case for a first
        rebuild; the ledger is ``None`` in addition when it is corrupt,
        which is exactly what a rebuild repairs.
    """
    try:
        content, sha = client.get_file(path)
    except NotFoundError:
        return None, None
    try:
        return sha, Ledger.model_validate_json(content)
    except ValidationError:
        return sha, None


def _merge_policy(
    client: GitHubClient,
    settings: Settings,
    existing: Ledger | None,
) -> MergePolicy:
    """Determine the merge policy the rebuilt ledger must follow.

    The fleet manifest wins: the freeze window and the reporting
    timezone are fleet-wide decisions, and a rebuild is the moment at
    which a machine adopts a changed policy. The ledger being replaced
    and the local settings are fallbacks for an unreadable manifest.

    Parameters
    ----------
    client : GitHubClient
        Transport.
    settings : Settings
        Loaded settings.
    existing : Ledger or None
        The ledger being replaced, when it could be parsed.

    Returns
    -------
    MergePolicy
        The policy to rebuild with.
    """
    try:
        content, _sha = client.get_file(MANIFEST_PATH)
        manifest = parse_manifest(content)
    except (NotFoundError, ValueError):
        manifest = None
    if manifest is not None:
        return MergePolicy(
            freeze_window_days=manifest.freeze_window_days,
            timezone=manifest.timezone,
        )
    if existing is not None:
        return existing.merge_policy
    return MergePolicy(
        freeze_window_days=settings.ledger.freeze_window_days,
        timezone=settings.timezone,
    )


def _load_snapshots(
    client: GitHubClient,
    machine_id: str,
) -> list[tuple[str, Snapshot]]:
    """Download every snapshot of a machine.

    Parameters
    ----------
    client : GitHubClient
        Transport.
    machine_id : str
        Machine to rebuild.

    Returns
    -------
    list of (str, Snapshot)
        Remote path and parsed snapshot, in path order.
    """
    prefix = snapshot_prefix(machine_id)
    paths = sorted(
        path
        for path in client.list_tree(prefix)
        if path.startswith(prefix) and path.endswith('.json')
    )
    snapshots: list[tuple[str, Snapshot]] = []
    for path in paths:
        content, _sha = client.get_file(path)
        try:
            snapshots.append((path, Snapshot.model_validate_json(content)))
        except ValidationError as exc:
            print_error(
                f'{path} is not a valid snapshot',
                f'{exc.error_count()} problem(s); refusing to rebuild from '
                'incomplete data',
            )
            exit_with(ExitCode.REMOTE_CONFLICT)
    return snapshots


def _resolve_label(
    settings: Settings,
    machine_id: str,
    existing: Ledger | None,
    snapshots: list[tuple[str, Snapshot]],
) -> str:
    """Determine the label of the rebuilt ledger.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    machine_id : str
        Machine being rebuilt.
    existing : Ledger or None
        The ledger being replaced, when it could be parsed.
    snapshots : list of (str, Snapshot)
        The snapshots being folded, in path order.

    Returns
    -------
    str
        The label of the local machine when it is the one being
        rebuilt, else the label recorded in the newest snapshot, the
        previous ledger or, as a last resort, the machine id.
    """
    machine = settings.machine
    if machine is not None and machine.id == machine_id:
        return machine.label
    if snapshots:
        return snapshots[-1][1].label
    if existing is not None:
        return existing.label
    return machine_id


def _summarise(ledger: Ledger) -> tuple[int, int]:
    """Count the day records and distinct days of a ledger.

    Parameters
    ----------
    ledger : Ledger
        The rebuilt ledger.

    Returns
    -------
    tuple of (int, int)
        Number of records and number of distinct calendar days.
    """
    days: set[dt.date] = set()
    records = 0
    for agent in ledger.agents.values():
        for day in agent.days:
            days.add(day.date)
            records += 1
    return records, len(days)


def rebuild_command(
    ctx: typer.Context,
    machine: Annotated[
        str | None,
        typer.Option(
            '--machine',
            help='Machine id to rebuild; defaults to this machine.',
            metavar='ID',
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option('--dry-run', help='Print the result without writing.'),
    ] = False,
) -> None:
    """Rebuild a machine ledger from its immutable snapshots.

    The snapshots are the source of truth; the ledger is derived. A
    rebuild therefore fixes any ledger that drifted, was corrupted or
    was written by an older merge policy, without touching a single
    snapshot.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    machine : str or None, optional
        Machine to rebuild.
    dry_run : bool, optional
        Report what would be written and write nothing.
    """
    settings = load_or_exit(ctx)
    machine_id = _resolve_machine(settings, machine)
    remote_path = ledger_remote_path(machine_id)
    console = out_console()
    try:
        client = build_client(settings)
        sha, existing = _existing_ledger(client, remote_path)
        policy = _merge_policy(client, settings, existing)
        snapshots = _load_snapshots(client, machine_id)
    except FetchAuthError as exc:
        print_error(str(exc))
        exit_with(ExitCode.AUTH_FAILURE)
    except AuthError as exc:
        print_error(str(exc))
        exit_with(ExitCode.AUTH_FAILURE)
    except (GitHubError, OSError) as exc:
        print_error(f'cannot read {settings.github.repository}: {exc}')
        exit_with(ExitCode.SPOOLED_NOT_UPLOADED)

    if not snapshots:
        print_error(
            f'no snapshots below {snapshot_prefix(machine_id)}',
            'nothing to rebuild from',
        )
        exit_with(ExitCode.REMOTE_CONFLICT)

    label = _resolve_label(settings, machine_id, existing, snapshots)
    rebuilt = ledger_module.rebuild_ledger(
        machine_id, label, policy, snapshots
    )
    records, days = _summarise(rebuilt)
    console.print(f'machine: {label} ({machine_id})')
    console.print(f'snapshots applied: {len(snapshots)}')
    console.print(f'day records: {records} over {days} day(s)')
    console.print(f'anomalies: {len(rebuilt.anomalies)}')

    if dry_run:
        console.print(f'dry run: {remote_path} not written')
        return

    payload = rebuilt.to_pretty_json().encode('utf-8')
    message = f'rebuild ledger for {machine_id}'
    try:
        client.put_file(remote_path, payload, message, sha)
    except AuthError as exc:
        print_error(str(exc))
        exit_with(ExitCode.AUTH_FAILURE)
    except (GitHubError, OSError) as exc:
        print_error(f'cannot write {remote_path}: {exc}')
        exit_with(ExitCode.SPOOLED_NOT_UPLOADED)
    console.print(f'wrote {remote_path}')
