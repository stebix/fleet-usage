"""``fleet-usage show``: aggregate the fleet ledgers on read."""

import contextlib
import datetime as dt
import enum
import webbrowser
from typing import Annotated

import typer

from fleet_usage import reporting
from fleet_usage.commands._common import load_or_exit
from fleet_usage.config import Settings
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.fetch import (
    FetchAuthError,
    FleetData,
    NoDataError,
    RemoteDataError,
    build_client,
    fetch_fleet,
)
from fleet_usage.paths import get_paths
from fleet_usage.ui import out_console, print_data, print_error

__all__ = ['GroupBy', 'ShowFormat', 'repository_url', 'show_command']


class GroupBy(enum.StrEnum):
    """Grouping dimensions of the usage report."""

    MACHINE = 'machine'
    AGENT = 'agent'
    MODEL = 'model'
    DAY = 'day'
    MONTH = 'month'


class ShowFormat(enum.StrEnum):
    """Output formats of the usage report."""

    TABLE = 'table'
    JSON = 'json'
    CSV = 'csv'


def repository_url(settings: Settings) -> str:
    """Return the web address of the data repository.

    Parameters
    ----------
    settings : Settings
        Loaded settings.

    Returns
    -------
    str
        ``https://github.com/OWNER/NAME``.
    """
    return f'https://github.com/{settings.github.repository}'


def _open_web(settings: Settings) -> None:
    """Open the data repository in a browser and print the address.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    """
    url = repository_url(settings)
    out_console().print(url)
    # A machine without a usable browser is a normal case for this
    # tool; the address has already been printed, which is enough.
    with contextlib.suppress(webbrowser.Error):
        webbrowser.open(url)


def _load_fleet(
    settings: Settings,
    *,
    offline: bool,
) -> FleetData:
    """Fetch the fleet or terminate with the matching exit code.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    offline : bool
        Read the local cache only; the network is never touched.

    Returns
    -------
    FleetData
        Manifest, ledgers and freshness metadata.
    """
    paths = get_paths()
    try:
        client = None if offline else build_client(settings)
        return fetch_fleet(settings, paths, client, offline=offline)
    except FetchAuthError as exc:
        print_error(str(exc))
        exit_with(ExitCode.AUTH_FAILURE)
    except RemoteDataError as exc:
        print_error(str(exc))
        exit_with(ExitCode.REMOTE_CONFLICT)
    except NoDataError as exc:
        print_error(str(exc))
        exit_with(ExitCode.SPOOLED_NOT_UPLOADED)


def _resolve_range(
    settings: Settings,
    since: dt.datetime | None,
    until: dt.datetime | None,
    today: dt.date,
) -> tuple[dt.date | None, dt.date | None]:
    """Determine the reporting interval.

    An explicit bound always wins. When neither bound is given, the
    configured ``report.default_period`` decides, so that a bare
    ``fleet-usage show`` answers the question the operator asks most
    often without repeating options.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    since, until : datetime.datetime or None
        Values of ``--since`` and ``--until``.
    today : datetime.date
        The current day.

    Returns
    -------
    tuple
        ``(since, until)`` as dates; either may be ``None``.
    """
    if since is None and until is None:
        return reporting.default_range(settings.report.default_period, today)
    start = since.date() if since is not None else None
    end = until.date() if until is not None else None
    return start, end


def show_command(
    ctx: typer.Context,
    by: Annotated[
        GroupBy,
        typer.Option('--by', help='Grouping dimension.'),
    ] = GroupBy.MACHINE,
    since: Annotated[
        dt.datetime | None,
        typer.Option(
            '--since',
            help='First day to include (YYYY-MM-DD).',
            formats=['%Y-%m-%d'],
        ),
    ] = None,
    until: Annotated[
        dt.datetime | None,
        typer.Option(
            '--until',
            help='Last day to include (YYYY-MM-DD).',
            formats=['%Y-%m-%d'],
        ),
    ] = None,
    output_format: Annotated[
        ShowFormat,
        typer.Option('--format', help='Output format.'),
    ] = ShowFormat.TABLE,
    breakdown: Annotated[
        bool,
        typer.Option(
            '--breakdown/--flat',
            help='Break every group down by model. Ignored with --by model.',
        ),
    ] = True,
    offline: Annotated[
        bool,
        typer.Option('--offline', help='Use the cached ledgers only.'),
    ] = False,
    web: Annotated[
        bool,
        typer.Option('--web', help='Open the data repository README.'),
    ] = False,
) -> None:
    """Download the machine ledgers and print a usage report.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    by : GroupBy, optional
        Grouping dimension.
    since : datetime.datetime or None, optional
        First day to include.
    until : datetime.datetime or None, optional
        Last day to include.
    output_format : ShowFormat, optional
        Output format.
    breakdown : bool, optional
        Break every group down by model. On by default; ``--flat``
        reports the group totals alone.
    offline : bool, optional
        Do not contact GitHub.
    web : bool, optional
        Open the rendered README in a browser and do nothing else.
    """
    settings = load_or_exit(ctx)
    if web:
        _open_web(settings)
        return

    now = dt.datetime.now(dt.UTC)
    start, end = _resolve_range(settings, since, until, now.date())
    fleet = _load_fleet(settings, offline=offline)
    fleet = reporting.apply_corrections(fleet, {})

    group = str(by)
    rows: list[reporting.Row] | list[reporting.RowGroup]
    totals: reporting.Row | reporting.RowGroup
    # Grouping by model already puts one model per row, so a breakdown
    # would only repeat each row underneath itself.
    detailed = breakdown and by is not GroupBy.MODEL
    if detailed:
        rows = reporting.aggregate_grouped(
            fleet, by=by.value, since=start, until=end
        )
        totals = reporting.fleet_totals_grouped(fleet, since=start, until=end)
        total_row = totals.row
    else:
        rows = reporting.aggregate(fleet, by=by.value, since=start, until=end)
        totals = reporting.fleet_totals(fleet, since=start, until=end)
        total_row = totals
    statuses = reporting.machine_status(
        fleet, now, settings.report.stale_after_hours
    )
    summary = reporting.build_summary(fleet, statuses, total_row)

    if output_format is ShowFormat.JSON:
        print_data(
            reporting.render_json(
                rows,
                by=group,
                since=start,
                until=end,
                totals=totals,
                summary=summary,
                statuses=statuses,
            ).rstrip('\n')
        )
        return
    if output_format is ShowFormat.CSV:
        print_data(reporting.render_csv(rows, breakdown=detailed).rstrip('\n'))
        return
    reporting.render_table(
        rows,
        by=group,
        totals=totals,
        summary=summary,
        statuses=statuses,
        now=now,
    )
