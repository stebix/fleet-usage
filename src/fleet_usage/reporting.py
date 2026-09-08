"""Aggregation and rendering of the fleet ledgers.

Aggregation happens on read: every machine ledger is downloaded by
:mod:`fleet_usage.fetch` and grouped here by machine, agent, model, day
or month. The functions in this module are pure; only
:func:`render_table` touches a console, and it writes nothing else.

Two rules shape the numbers.

* Money is :class:`decimal.Decimal`, never :class:`float`, and is only
  ever summed from records that actually carry a price.
* A record whose cost is unknown is never counted as zero. It is
  counted in ``unknown_cost_records`` and clears ``cost_known``, so the
  renderers can show ``—`` instead of a wrong total.
"""

import csv
import dataclasses
import datetime as dt
import io
import json
from collections.abc import Iterable, Iterator, Mapping
from decimal import Decimal
from typing import Any, Literal

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from fleet_usage.fetch import FleetData
from fleet_usage.models import DayRecord, LedgerDayRecord, ModelRecord
from fleet_usage.ui import out_console

__all__ = [
    'UNKNOWN_COST',
    'UNKNOWN_MODEL',
    'Freshness',
    'GroupKey',
    'MachineStatus',
    'Row',
    'StatusSummary',
    'aggregate',
    'apply_corrections',
    'build_summary',
    'default_range',
    'fleet_totals',
    'format_cost',
    'machine_status',
    'render_csv',
    'render_json',
    'render_table',
    'row_to_dict',
    'status_panel',
]

GroupKey = Literal['machine', 'agent', 'model', 'day', 'month']
Freshness = Literal['fresh', 'stale', 'never']

UNKNOWN_COST = '—'
UNKNOWN_MODEL = '(unknown)'
PARTIAL_PREFIX = '>= '

GROUP_LABELS: dict[str, tuple[str, str]] = {
    'machine': ('machine', 'label'),
    'agent': ('agent', 'agent'),
    'model': ('model', 'model'),
    'day': ('day', 'day'),
    'month': ('month', 'month'),
}

CSV_COLUMNS = (
    'key',
    'label',
    'input_tokens',
    'output_tokens',
    'cache_create_tokens',
    'cache_read_tokens',
    'total_tokens',
    'record_count',
    'cost_usd',
    'cost_known',
    'unknown_cost_records',
)
FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


@dataclasses.dataclass(frozen=True, slots=True)
class Row:
    """One aggregated group of usage records.

    Attributes
    ----------
    group
        The dimension this row was grouped by.
    key
        Stable identity of the group: a machine id, an agent name, a
        model name, an ISO date or a ``YYYY-MM`` month.
    label
        Human readable name of the group. Equal to ``key`` except for
        machines, where it is the manifest label.
    input_tokens, output_tokens, cache_create_tokens, cache_read_tokens
        Summed token counters.
    cost_usd
        Sum of the costs that are known, or ``None`` when no
        contributing record carried a price.
    cost_known
        ``False`` when at least one contributing record had an unknown
        cost, in which case ``cost_usd`` is a lower bound.
    unknown_cost_records
        How many contributing records had an unknown cost.
    record_count
        How many records were folded into this row.
    """

    group: str
    key: str
    label: str
    input_tokens: int
    output_tokens: int
    cache_create_tokens: int
    cache_read_tokens: int
    cost_usd: Decimal | None
    cost_known: bool
    unknown_cost_records: int
    record_count: int

    @property
    def total_tokens(self) -> int:
        """Sum of all four token counters.

        Returns
        -------
        int
            Total number of tokens in this group.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_create_tokens
            + self.cache_read_tokens
        )


@dataclasses.dataclass(frozen=True, slots=True)
class MachineStatus:
    """Reporting health of one machine in the manifest.

    Attributes
    ----------
    machine_id
        Machine id from ``fleet.toml``.
    label
        Manifest label.
    last_run_at
        When the machine last published, or ``None``.
    freshness
        ``'fresh'``, ``'stale'`` or ``'never'``.
    agent_errors
        Last error message per agent, for agents that recorded one.
    anomaly_count
        Number of anomalies in the machine ledger.
    from_cache
        Whether the ledger in use came from the local cache.
    """

    machine_id: str
    label: str
    last_run_at: dt.datetime | None
    freshness: Freshness
    agent_errors: dict[str, str]
    anomaly_count: int
    from_cache: bool


@dataclasses.dataclass(frozen=True, slots=True)
class StatusSummary:
    """Everything the panel above the table reports.

    Attributes
    ----------
    expected
        Machines listed in the manifest.
    reporting
        Machines whose ledger was read and is fresh.
    stale
        Machines whose ledger is older than the staleness threshold.
    never
        Machines that never published a ledger.
    unregistered
        Ledgers present in the repository but absent from the manifest.
    total_records
        Records that went into the report.
    unknown_cost_records
        How many of them carried no price.
    fetched_at
        Age of the oldest document in use.
    from_cache
        Whether any document came from the local cache.
    offline
        Whether the read avoided the network entirely.
    """

    expected: int
    reporting: int
    stale: int
    never: int
    unregistered: int
    total_records: int
    unknown_cost_records: int
    fetched_at: dt.datetime | None
    from_cache: bool
    offline: bool

    @property
    def cost_coverage(self) -> str:
        """Share of records that carry a known cost.

        Returns
        -------
        str
            For example ``'6/8 records priced'``.
        """
        priced = self.total_records - self.unknown_cost_records
        return f'{priced}/{self.total_records} records priced'

    def lines(self, now: dt.datetime | None = None) -> list[str]:
        """Render the summary as panel lines.

        Parameters
        ----------
        now : datetime.datetime or None, optional
            Reference time used to describe the data age.

        Returns
        -------
        list of str
            One line per topic, ready for a Rich panel.
        """
        machines = (
            f'machines: {self.expected} expected, '
            f'{self.reporting} reporting, {self.stale} stale, '
            f'{self.never} never reported'
        )
        if self.unregistered:
            machines += f', {self.unregistered} unregistered'
        source = 'offline (cache only)' if self.offline else 'github'
        if self.from_cache and not self.offline:
            source = 'github (cached copies in use)'
        return [
            machines,
            f'cost coverage: {self.cost_coverage}',
            f'data: {_age_label(self.fetched_at, now)} via {source}',
        ]


@dataclasses.dataclass(slots=True)
class _Bucket:
    """Mutable accumulator behind a :class:`Row`."""

    label: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_create_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: Decimal | None = None
    unknown_cost_records: int = 0
    record_count: int = 0

    def add(self, record: DayRecord | ModelRecord) -> None:
        """Fold one usage record into the accumulator.

        Parameters
        ----------
        record : DayRecord or ModelRecord
            The record to add. Its cost only contributes when it is
            actually known; an unknown cost is counted separately and
            never treated as zero.
        """
        self.input_tokens += record.input_tokens
        self.output_tokens += record.output_tokens
        self.cache_create_tokens += record.cache_create_tokens
        self.cache_read_tokens += record.cache_read_tokens
        self.record_count += 1
        if record.cost_usd is None or record.cost_status == 'unknown':
            self.unknown_cost_records += 1
            return
        current = self.cost_usd if self.cost_usd is not None else Decimal(0)
        self.cost_usd = current + record.cost_usd

    def to_row(self, group: str, key: str) -> Row:
        """Freeze the accumulator into a row.

        Parameters
        ----------
        group : str
            The grouping dimension.
        key : str
            Identity of the group.

        Returns
        -------
        Row
            The immutable result.
        """
        return Row(
            group=group,
            key=key,
            label=self.label,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_create_tokens=self.cache_create_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cost_usd=self.cost_usd,
            cost_known=self.unknown_cost_records == 0,
            unknown_cost_records=self.unknown_cost_records,
            record_count=self.record_count,
        )


def _in_range(
    day: dt.date, since: dt.date | None, until: dt.date | None
) -> bool:
    """Whether ``day`` falls inside the closed reporting interval.

    Parameters
    ----------
    day : datetime.date
        The day of a record.
    since : datetime.date or None
        First day to include, or ``None`` for no lower bound.
    until : datetime.date or None
        Last day to include, or ``None`` for no upper bound.

    Returns
    -------
    bool
        ``True`` when the day is inside the interval.
    """
    if since is not None and day < since:
        return False
    return not (until is not None and day > until)


def _iter_days(
    fleet: FleetData,
    since: dt.date | None,
    until: dt.date | None,
) -> Iterator[tuple[str, str, LedgerDayRecord]]:
    """Yield every day record inside the reporting interval.

    Parameters
    ----------
    fleet : FleetData
        The fetched fleet.
    since : datetime.date or None
        First day to include.
    until : datetime.date or None
        Last day to include.

    Yields
    ------
    tuple of (str, str, LedgerDayRecord)
        Machine id, agent name and the record.
    """
    for machine_id in sorted(fleet.ledgers):
        ledger = fleet.ledgers[machine_id]
        for agent in sorted(ledger.agents):
            for day in ledger.agents[agent].days:
                if _in_range(day.date, since, until):
                    yield machine_id, agent, day


def _group_of(
    by: GroupKey,
    fleet: FleetData,
    machine_id: str,
    agent: str,
    day: LedgerDayRecord,
) -> tuple[str, str]:
    """Return the key and label of the group a record belongs to.

    Parameters
    ----------
    by : GroupKey
        The grouping dimension. ``'model'`` is handled by the caller.
    fleet : FleetData
        The fetched fleet, used to resolve machine labels.
    machine_id : str
        Machine the record came from.
    agent : str
        Agent the record came from.
    day : LedgerDayRecord
        The record.

    Returns
    -------
    tuple of (str, str)
        Group key and display label.
    """
    if by == 'machine':
        return machine_id, fleet.label_of(machine_id)
    if by == 'agent':
        return agent, agent
    if by == 'month':
        month = day.date.strftime('%Y-%m')
        return month, month
    iso = day.date.isoformat()
    return iso, iso


def aggregate(
    fleet: FleetData,
    *,
    by: GroupKey = 'machine',
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> list[Row]:
    """Group and total the ledger day records.

    Groups without a single record inside the interval are omitted, so
    an empty result means "no usage in this period", not "no machines".
    Grouping by model uses the per-model breakdown of each day record;
    a record without a breakdown is attributed to
    :data:`UNKNOWN_MODEL` so that model totals still add up to the
    fleet total.

    Parameters
    ----------
    fleet : FleetData
        Manifest and ledgers as returned by
        :func:`fleet_usage.fetch.fetch_fleet`.
    by : {'machine', 'agent', 'model', 'day', 'month'}, optional
        Grouping dimension.
    since : datetime.date or None, optional
        First day to include.
    until : datetime.date or None, optional
        Last day to include.

    Returns
    -------
    list of Row
        One row per non-empty group, ordered by key for days, months,
        agents and models, and by label for machines.

    Raises
    ------
    ValueError
        If ``by`` is not a known dimension.
    """
    if by not in GROUP_LABELS:
        msg = f'unknown grouping {by!r}'
        raise ValueError(msg)
    buckets: dict[str, _Bucket] = {}
    for machine_id, agent, day in _iter_days(fleet, since, until):
        if by == 'model':
            for record in _model_records(day):
                bucket = buckets.setdefault(
                    record.model, _Bucket(label=record.model)
                )
                bucket.add(record)
            continue
        key, label = _group_of(by, fleet, machine_id, agent, day)
        bucket = buckets.setdefault(key, _Bucket(label=label))
        bucket.add(day)
    rows = [bucket.to_row(by, key) for key, bucket in buckets.items()]
    if by == 'machine':
        return sorted(rows, key=lambda row: (row.label, row.key))
    return sorted(rows, key=lambda row: row.key)


def _model_records(day: DayRecord) -> list[ModelRecord]:
    """Return the model breakdown of a day record.

    Parameters
    ----------
    day : DayRecord
        A day record, with or without a breakdown.

    Returns
    -------
    list of ModelRecord
        The recorded models, or a single synthetic record carrying the
        whole day under :data:`UNKNOWN_MODEL` when the collector did
        not report a breakdown.
    """
    if day.models:
        return day.models
    return [
        ModelRecord(
            model=UNKNOWN_MODEL,
            input_tokens=day.input_tokens,
            output_tokens=day.output_tokens,
            cache_create_tokens=day.cache_create_tokens,
            cache_read_tokens=day.cache_read_tokens,
            cost_usd=day.cost_usd,
            cost_status=day.cost_status,
        )
    ]


def fleet_totals(
    fleet: FleetData,
    *,
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> Row:
    """Total the whole fleet over the reporting interval.

    Parameters
    ----------
    fleet : FleetData
        The fetched fleet.
    since : datetime.date or None, optional
        First day to include.
    until : datetime.date or None, optional
        Last day to include.

    Returns
    -------
    Row
        A single row with ``group='fleet'`` and ``key='fleet'``.
    """
    bucket = _Bucket(label='fleet')
    for _machine_id, _agent, day in _iter_days(fleet, since, until):
        bucket.add(day)
    return bucket.to_row('fleet', 'fleet')


def machine_status(
    fleet: FleetData,
    now: dt.datetime,
    stale_after_hours: int,
) -> list[MachineStatus]:
    """Classify every manifest machine as fresh, stale or never seen.

    Parameters
    ----------
    fleet : FleetData
        The fetched fleet.
    now : datetime.datetime
        Timezone aware reference time.
    stale_after_hours : int
        A machine is stale once its last run is older than this.

    Returns
    -------
    list of MachineStatus
        One entry per manifest machine, ordered by label. Machines with
        a ledger but no ``last_run_at`` count as ``'never'``: a ledger
        without a run has never been published from a real collection.
    """
    threshold = dt.timedelta(hours=stale_after_hours)
    statuses: list[MachineStatus] = []
    for entry in fleet.manifest.machines:
        ledger = fleet.ledgers.get(entry.id)
        meta = fleet.files.get(f'machines/{entry.id}.json')
        last_run = ledger.last_run_at if ledger is not None else None
        if last_run is None:
            freshness: Freshness = 'never'
        elif now - last_run <= threshold:
            freshness = 'fresh'
        else:
            freshness = 'stale'
        errors: dict[str, str] = {}
        if ledger is not None:
            for name in sorted(ledger.agents):
                error = ledger.agents[name].last_error
                if error:
                    errors[name] = error
        statuses.append(
            MachineStatus(
                machine_id=entry.id,
                label=entry.label,
                last_run_at=last_run,
                freshness=freshness,
                agent_errors=errors,
                anomaly_count=len(ledger.anomalies) if ledger else 0,
                from_cache=meta.from_cache if meta is not None else False,
            )
        )
    return sorted(
        statuses, key=lambda status: (status.label, status.machine_id)
    )


def build_summary(
    fleet: FleetData,
    statuses: Iterable[MachineStatus],
    totals: Row,
) -> StatusSummary:
    """Assemble the status panel contents.

    Parameters
    ----------
    fleet : FleetData
        The fetched fleet.
    statuses : iterable of MachineStatus
        Output of :func:`machine_status`.
    totals : Row
        Output of :func:`fleet_totals` for the same interval as the
        table, used for the cost coverage line.

    Returns
    -------
    StatusSummary
        Counters and labels for the panel.
    """
    items = list(statuses)
    return StatusSummary(
        expected=len(fleet.manifest.machines),
        reporting=sum(1 for s in items if s.freshness == 'fresh'),
        stale=sum(1 for s in items if s.freshness == 'stale'),
        never=sum(1 for s in items if s.freshness == 'never'),
        unregistered=len(fleet.unregistered),
        total_records=totals.record_count,
        unknown_cost_records=totals.unknown_cost_records,
        fetched_at=fleet.fetched_at,
        from_cache=fleet.from_cache,
        offline=fleet.offline,
    )


def apply_corrections(
    fleet: FleetData,
    corrections: Mapping[str, Any],
) -> FleetData:
    """Apply manual corrections to the fetched fleet.

    This is a deliberate no-op placeholder. Manual corrections (for
    example a machine that double counted a day, or a price the
    collector could not know) will eventually live in a small TOML
    document in the data repository and be applied here, between
    fetching and aggregating, so that neither the immutable snapshots
    nor the derived ledgers have to be rewritten.

    Parameters
    ----------
    fleet : FleetData
        The fetched fleet.
    corrections : Mapping[str, Any]
        Parsed corrections document. Any content is accepted and
        ignored today.

    Returns
    -------
    FleetData
        ``fleet`` unchanged.
    """
    del corrections
    return fleet


def default_range(
    period: str,
    today: dt.date,
) -> tuple[dt.date | None, dt.date | None]:
    """Return the interval implied by ``report.default_period``.

    Parameters
    ----------
    period : str
        One of ``'day'``, ``'week'``, ``'month'`` or ``'all'``.
    today : datetime.date
        The current day in the reporting timezone.

    Returns
    -------
    tuple
        ``(since, until)``; either bound may be ``None`` for "open".
        ``'all'`` returns ``(None, None)``.
    """
    if period == 'day':
        return today, today
    if period == 'week':
        return today - dt.timedelta(days=6), today
    if period == 'month':
        return today.replace(day=1), today
    return None, None


def _age_label(
    fetched_at: dt.datetime | None,
    now: dt.datetime | None,
) -> str:
    """Describe how old the data in use is.

    Parameters
    ----------
    fetched_at : datetime.datetime or None
        When the oldest document in use was downloaded.
    now : datetime.datetime or None
        Reference time.

    Returns
    -------
    str
        For example ``'fetched 12 min ago'`` or ``'age unknown'``.
    """
    if fetched_at is None:
        return 'age unknown'
    if now is None:
        return f'fetched {fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")}'
    seconds = max(0.0, (now - fetched_at).total_seconds())
    if seconds < 90:
        return 'fetched just now'
    minutes = int(seconds // 60)
    if minutes < 90:
        return f'fetched {minutes} min ago'
    hours = int(seconds // 3600)
    if hours < 48:
        return f'fetched {hours} h ago'
    return f'fetched {int(seconds // 86400)} d ago'


def format_cost(row: Row) -> str:
    """Render the cost of a row for human consumption.

    Parameters
    ----------
    row : Row
        The aggregated group.

    Returns
    -------
    str
        The exact amount when every contributing record was priced, a
        ``'>= '`` prefixed lower bound when only some were, and
        :data:`UNKNOWN_COST` when none were. An unknown cost is never
        rendered as ``0``.
    """
    if row.cost_usd is None:
        return UNKNOWN_COST
    if row.cost_known:
        return str(row.cost_usd)
    return f'{PARTIAL_PREFIX}{row.cost_usd}'


def row_to_dict(row: Row) -> dict[str, Any]:
    """Convert a row into JSON-ready primitives.

    Parameters
    ----------
    row : Row
        The aggregated group.

    Returns
    -------
    dict
        Scalars only; the cost is a decimal string or ``None``, never a
        float, so that no precision is lost on the way out.
    """
    return {
        'group': row.group,
        'key': row.key,
        'label': row.label,
        'input_tokens': row.input_tokens,
        'output_tokens': row.output_tokens,
        'cache_create_tokens': row.cache_create_tokens,
        'cache_read_tokens': row.cache_read_tokens,
        'total_tokens': row.total_tokens,
        'record_count': row.record_count,
        'cost_usd': None if row.cost_usd is None else str(row.cost_usd),
        'cost_known': row.cost_known,
        'unknown_cost_records': row.unknown_cost_records,
    }


def _status_payload(summary: StatusSummary) -> dict[str, Any]:
    """Convert a status summary into JSON-ready primitives.

    Parameters
    ----------
    summary : StatusSummary
        The panel contents.

    Returns
    -------
    dict
        Scalars only; timestamps are ISO-8601 UTC strings.
    """
    fetched = summary.fetched_at
    return {
        'expected': summary.expected,
        'reporting': summary.reporting,
        'stale': summary.stale,
        'never': summary.never,
        'unregistered': summary.unregistered,
        'total_records': summary.total_records,
        'unknown_cost_records': summary.unknown_cost_records,
        'fetched_at': (
            None if fetched is None else fetched.strftime('%Y-%m-%dT%H:%M:%SZ')
        ),
        'from_cache': summary.from_cache,
        'offline': summary.offline,
    }


def _machine_payload(status: MachineStatus) -> dict[str, Any]:
    """Convert a machine status into JSON-ready primitives.

    Parameters
    ----------
    status : MachineStatus
        Health of one machine.

    Returns
    -------
    dict
        Scalars and one mapping of agent errors.
    """
    last_run = status.last_run_at
    return {
        'machine_id': status.machine_id,
        'label': status.label,
        'last_run_at': (
            None
            if last_run is None
            else last_run.strftime('%Y-%m-%dT%H:%M:%SZ')
        ),
        'freshness': status.freshness,
        'agent_errors': dict(status.agent_errors),
        'anomaly_count': status.anomaly_count,
        'from_cache': status.from_cache,
    }


def render_json(
    rows: Iterable[Row],
    *,
    by: str = 'machine',
    since: dt.date | None = None,
    until: dt.date | None = None,
    totals: Row | None = None,
    summary: StatusSummary | None = None,
    statuses: Iterable[MachineStatus] | None = None,
) -> str:
    """Render the aggregation as JSON.

    Parameters
    ----------
    rows : iterable of Row
        Output of :func:`aggregate`.
    by : str, optional
        The grouping dimension, echoed in the document.
    since, until : datetime.date or None, optional
        The reporting interval, echoed as ISO dates.
    totals : Row or None, optional
        Fleet totals for the same interval.
    summary : StatusSummary or None, optional
        Status counters.
    statuses : iterable of MachineStatus or None, optional
        Per machine health.

    Returns
    -------
    str
        JSON text with sorted keys and no ANSI escape sequences,
        ending in a newline. Row order follows ``rows``.
    """
    payload: dict[str, Any] = {
        'group_by': by,
        'since': None if since is None else since.isoformat(),
        'until': None if until is None else until.isoformat(),
        'rows': [row_to_dict(row) for row in rows],
    }
    if totals is not None:
        payload['totals'] = row_to_dict(totals)
    if summary is not None:
        payload['status'] = _status_payload(summary)
    if statuses is not None:
        payload['machines'] = [_machine_payload(s) for s in statuses]
    return json.dumps(payload, indent=2, sort_keys=True) + '\n'


def _csv_cell(value: object) -> str:
    """Render one CSV cell, defusing spreadsheet formulas.

    A cell that a spreadsheet would evaluate as a formula is prefixed
    with a single quote. Machine labels come from a shared repository,
    so they are treated as untrusted input.

    Parameters
    ----------
    value : object
        The cell value.

    Returns
    -------
    str
        The escaped text.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return f"'{text}"
    return text


def render_csv(rows: Iterable[Row]) -> str:
    """Render the aggregation as CSV.

    Parameters
    ----------
    rows : iterable of Row
        Output of :func:`aggregate`.

    Returns
    -------
    str
        CSV text with a header row, LF line endings and no ANSI
        escape sequences.
    """
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        payload = row_to_dict(row)
        writer.writerow([_csv_cell(payload[name]) for name in CSV_COLUMNS])
    return stream.getvalue()


def _status_style(freshness: Freshness) -> str:
    """Return the Rich style for a freshness label.

    Parameters
    ----------
    freshness : Freshness
        The classification.

    Returns
    -------
    str
        A Rich style name.
    """
    return {'fresh': 'green', 'stale': 'yellow', 'never': 'red'}[freshness]


def status_panel(
    summary: StatusSummary,
    statuses: Iterable[MachineStatus] = (),
    now: dt.datetime | None = None,
) -> Panel:
    """Build the panel printed above the report table.

    Parameters
    ----------
    summary : StatusSummary
        Counters and labels.
    statuses : iterable of MachineStatus, optional
        Per machine health; machines that are not fresh and agents with
        errors are called out by name.
    now : datetime.datetime or None, optional
        Reference time for the data age.

    Returns
    -------
    rich.panel.Panel
        The renderable panel.
    """
    lines = list(summary.lines(now))
    problems: list[str] = []
    for status in statuses:
        if status.freshness != 'fresh':
            style = _status_style(status.freshness)
            label = escape(status.label)
            problems.append(f'[{style}]{label}: {status.freshness}[/]')
        for agent, error in status.agent_errors.items():
            detail = f'{escape(status.label)}/{escape(agent)}'
            problems.append(f'[red]{detail}: {escape(error)}[/]')
    if problems:
        lines.append('attention: ' + '; '.join(problems))
    return Panel('\n'.join(lines), title='fleet status', expand=False)


def render_table(
    rows: Iterable[Row],
    *,
    by: str = 'machine',
    totals: Row | None = None,
    summary: StatusSummary | None = None,
    statuses: Iterable[MachineStatus] = (),
    now: dt.datetime | None = None,
    console: Console | None = None,
) -> None:
    """Print the aggregation as a Rich table.

    Parameters
    ----------
    rows : iterable of Row
        Output of :func:`aggregate`.
    by : str, optional
        The grouping dimension; it names the first column.
    totals : Row or None, optional
        Fleet totals, printed as a final row.
    summary : StatusSummary or None, optional
        When given, a status panel is printed above the table.
    statuses : iterable of MachineStatus, optional
        Per machine health for the panel.
    now : datetime.datetime or None, optional
        Reference time for the data age.
    console : rich.console.Console or None, optional
        Destination; defaults to the standard output console.
    """
    target = console if console is not None else out_console()
    materialised = list(rows)
    if summary is not None:
        target.print(status_panel(summary, statuses, now))
    heading = GROUP_LABELS.get(by, (by, by))[0]
    table = Table(highlight=False)
    table.add_column(heading, overflow='fold')
    table.add_column('input', justify='right')
    table.add_column('output', justify='right')
    table.add_column('cache w', justify='right')
    table.add_column('cache r', justify='right')
    table.add_column('total', justify='right')
    table.add_column('records', justify='right')
    table.add_column('cost usd', justify='right')
    for row in materialised:
        table.add_row(*_table_cells(row))
    if totals is not None:
        table.add_section()
        table.add_row(*_table_cells(totals, name='fleet total'))
    target.print(table)
    if not materialised:
        target.print('no usage records in the selected period')
    unpriced = sum(row.unknown_cost_records for row in materialised)
    if unpriced:
        target.print(
            f'{UNKNOWN_COST} = no priced record; '
            f'{PARTIAL_PREFIX.strip()} = lower bound '
            f'({unpriced} record(s) without a price)',
            style='dim',
        )


def _table_cells(row: Row, name: str | None = None) -> list[str]:
    """Render one table row.

    Parameters
    ----------
    row : Row
        The aggregated group.
    name : str or None, optional
        Override for the first cell.

    Returns
    -------
    list of str
        The cells, with thousands separators on the counters. Markup is
        escaped by Rich only for styles, so the label is printed
        verbatim through a plain string.
    """
    return [
        name if name is not None else escape(row.label),
        f'{row.input_tokens:,}',
        f'{row.output_tokens:,}',
        f'{row.cache_create_tokens:,}',
        f'{row.cache_read_tokens:,}',
        f'{row.total_tokens:,}',
        f'{row.record_count:,}',
        format_cost(row),
    ]
