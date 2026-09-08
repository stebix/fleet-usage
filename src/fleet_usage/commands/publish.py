"""``fleet-usage publish``: collect, upload and merge.

The command itself contains no policy: it loads the settings, hands the
work to :func:`fleet_usage.publisher.publish` and renders the result.
Which failure becomes which exit code is decided in the publisher, with
one exception: a local filesystem failure, for instance a spool
directory that is not writable, is reported here as a plain error
instead of a traceback.
"""

from typing import Annotated

import typer
from rich.table import Table

from fleet_usage import publisher
from fleet_usage.commands._common import load_or_exit
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.logging_setup import configure_logging
from fleet_usage.models import Snapshot
from fleet_usage.paths import AppPaths, get_paths
from fleet_usage.ui import out_console, print_error

__all__ = ['dry_run_table', 'publish_command']


def dry_run_table(snapshot: Snapshot) -> Table:
    """Build the summary table shown by ``--dry-run``.

    Parameters
    ----------
    snapshot : Snapshot
        The snapshot that was collected but not written anywhere.

    Returns
    -------
    rich.table.Table
        One row per agent, plus the collection timestamp in the title.
    """
    table = Table(
        title=f'collected at {snapshot.collected_at:%Y-%m-%dT%H:%M:%SZ}',
        highlight=False,
    )
    table.add_column('agent')
    table.add_column('status')
    table.add_column('days', justify='right')
    table.add_column('latest')
    table.add_column('tokens', justify='right')
    table.add_column('cost (USD)', justify='right')
    for row in publisher.summarize(snapshot):
        cost = f'{row.cost_usd}'
        if row.unknown_costs:
            cost = f'{cost} (+{row.unknown_costs} unknown)'
        table.add_row(
            row.agent,
            row.status,
            str(row.days),
            '' if row.latest_date is None else row.latest_date.isoformat(),
            f'{row.tokens:,}',
            cost,
        )
    return table


def _local_failure(exc: OSError, paths: AppPaths) -> str:
    """Describe a local filesystem failure in one line.

    Parameters
    ----------
    exc : OSError
        The failure raised by the spool, the lock or the cache.
    paths : AppPaths
        Resolved application paths, used when the exception does not
        name a file itself.

    Returns
    -------
    str
        A message naming the path that could not be used.
    """
    where = exc.filename or str(paths.state_dir)
    reason = exc.strerror or str(exc)
    return f'cannot write {where}: {reason}'


def publish_command(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            '--dry-run',
            help='Collect and print a summary without writing anywhere.',
        ),
    ] = False,
    if_due: Annotated[
        bool,
        typer.Option(
            '--if-due',
            help='Do nothing when the last run is younger than the '
            'configured sync interval.',
        ),
    ] = False,
) -> None:
    """Collect usage, upload a snapshot and update the ledger.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    dry_run : bool, optional
        Skip every write.
    if_due : bool, optional
        Skip the run when it is not due yet.
    """
    settings = load_or_exit(ctx)
    paths = get_paths()
    configure_logging(paths.log_dir)
    try:
        result = publisher.publish(
            settings, paths, dry_run=dry_run, if_due=if_due
        )
    except OSError as exc:
        print_error(_local_failure(exc, paths))
        exit_with(ExitCode.ERROR)
    console = out_console()
    if result.snapshot is not None and dry_run:
        console.print(dry_run_table(result.snapshot))
    if result.exit_code is ExitCode.OK:
        if result.message:
            console.print(result.message)
    else:
        print_error(result.message)
    exit_with(result.exit_code)
