"""Render a README from the data repository contents.

This module is deliberately self contained: it uses the standard library
only and imports nothing from :mod:`fleet_usage`, because it is copied
verbatim into the data repository and executed there by a GitHub Actions
workflow that installs no dependencies. A test enforces that rule by
scanning the import statements of this file.

The numbers it produces must match
:func:`fleet_usage.reporting.aggregate` exactly, which is why costs are
:class:`decimal.Decimal` here as well and a record without a price is
never counted as zero.
"""

import argparse
import datetime as dt
import json
import sys
import tomllib
from collections.abc import Iterator
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, NamedTuple

__all__ = [
    'DEFAULT_STALE_AFTER_HOURS',
    'SUBCENT_COST',
    'UNKNOWN_COST',
    'Machine',
    'Totals',
    'daily_costs',
    'load_fleet',
    'main',
    'model_totals',
    'render_readme',
    'totals_for',
]

DEFAULT_STALE_AFTER_HOURS = 3
CHART_DAYS = 30
UNKNOWN_COST = '—'
UNKNOWN_MODEL = '(unknown)'
SUBCENT_COST = '&lt;0.01'
COST_PLACES = Decimal('0.01')
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MANIFEST_NAME = 'fleet.toml'
MACHINES_DIR = 'machines'


class Totals(NamedTuple):
    """Summed usage over a set of records.

    Attributes
    ----------
    input_tokens, output_tokens, cache_create_tokens, cache_read_tokens
        Token counters.
    cost_usd
        Sum of the known costs, or ``None`` when nothing was priced.
    cost_known
        ``False`` when at least one record carried no price.
    unknown_cost_records
        How many records carried no price.
    record_count
        How many records were summed.
    """

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
            Total number of tokens.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_create_tokens
            + self.cache_read_tokens
        )


class Machine(NamedTuple):
    """One machine of the fleet as seen from the data repository.

    Attributes
    ----------
    machine_id
        Id from the manifest.
    label
        Label from the manifest.
    ledger
        Parsed ``machines/<id>.json``, or ``None`` when the machine
        never published one.
    """

    machine_id: str
    label: str
    ledger: dict[str, Any] | None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a mapping, else an empty dict.

    Parameters
    ----------
    value : Any
        Anything read from JSON or TOML.

    Returns
    -------
    dict
        A mapping, possibly empty. Remote documents are untrusted, so
        every access goes through this guard instead of assuming a
        shape.
    """
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Return ``value`` when it is a list, else an empty list.

    Parameters
    ----------
    value : Any
        Anything read from JSON or TOML.

    Returns
    -------
    list
        A list, possibly empty.
    """
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int:
    """Coerce ``value`` into a non-negative integer.

    Parameters
    ----------
    value : Any
        Anything read from JSON.

    Returns
    -------
    int
        The value, or ``0`` when it is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _as_cost(record: dict[str, Any]) -> Decimal | None:
    """Extract the cost of a record.

    Parameters
    ----------
    record : dict
        A day or model record.

    Returns
    -------
    decimal.Decimal or None
        The amount, or ``None`` when the record is unpriced or
        malformed. ``cost_status == 'unknown'`` always wins over a
        present amount, mirroring the collector contract that an
        unpriced model is reported with a cost of zero.
    """
    if record.get('cost_status') == 'unknown':
        return None
    raw = record.get('cost_usd')
    if not isinstance(raw, str):
        return None
    try:
        return Decimal(raw)
    except ArithmeticError:
        return None


def _parse_date(value: Any) -> dt.date | None:
    """Parse an ISO date.

    Parameters
    ----------
    value : Any
        Anything read from JSON.

    Returns
    -------
    datetime.date or None
        The date, or ``None`` when it cannot be parsed.
    """
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp with a ``Z`` suffix.

    Parameters
    ----------
    value : Any
        Anything read from JSON.

    Returns
    -------
    datetime.datetime or None
        A timezone aware UTC timestamp, or ``None``.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def load_fleet(data_dir: Path) -> list[Machine]:
    """Read the manifest and every ledger it points at.

    Parameters
    ----------
    data_dir : pathlib.Path
        Directory holding ``fleet.toml`` and ``machines/*.json``.

    Returns
    -------
    list of Machine
        One entry per manifest machine, in manifest order. Ledgers that
        the manifest does not list are ignored on purpose: the manifest
        is the definition of the fleet.

    Raises
    ------
    FileNotFoundError
        If the manifest is missing.
    """
    with (data_dir / MANIFEST_NAME).open('rb') as stream:
        manifest = tomllib.load(stream)
    machines: list[Machine] = []
    for entry in _as_list(manifest.get('machines')):
        item = _as_dict(entry)
        machine_id = item.get('id')
        if not isinstance(machine_id, str) or not machine_id:
            continue
        label = item.get('label')
        path = data_dir / MACHINES_DIR / f'{machine_id}.json'
        ledger: dict[str, Any] | None = None
        if '/' not in machine_id and path.is_file():
            try:
                ledger = _as_dict(json.loads(path.read_text('utf-8')))
            except (OSError, ValueError):
                ledger = None
        machines.append(
            Machine(
                machine_id=machine_id,
                label=label if isinstance(label, str) else machine_id,
                ledger=ledger,
            )
        )
    return machines


def _iter_days(
    machines: list[Machine],
    since: dt.date | None,
    until: dt.date | None,
) -> Iterator[tuple[Machine, str, dict[str, Any]]]:
    """Yield every day record inside the reporting interval.

    Parameters
    ----------
    machines : list of Machine
        The fleet.
    since, until : datetime.date or None
        Inclusive bounds of the interval.

    Yields
    ------
    tuple
        Machine, agent name and the raw day record.
    """
    for machine in machines:
        if machine.ledger is None:
            continue
        agents = _as_dict(machine.ledger.get('agents'))
        for agent in sorted(agents):
            for entry in _as_list(_as_dict(agents[agent]).get('days')):
                record = _as_dict(entry)
                day = _parse_date(record.get('date'))
                if day is None:
                    continue
                if since is not None and day < since:
                    continue
                if until is not None and day > until:
                    continue
                yield machine, agent, record


def _sum_records(records: list[dict[str, Any]]) -> Totals:
    """Sum a list of day or model records.

    Parameters
    ----------
    records : list of dict
        Raw records.

    Returns
    -------
    Totals
        The summed counters; unknown costs are counted, never added as
        zero.
    """
    counters = [0, 0, 0, 0]
    cost: Decimal | None = None
    unknown = 0
    for record in records:
        counters[0] += _as_int(record.get('input_tokens'))
        counters[1] += _as_int(record.get('output_tokens'))
        counters[2] += _as_int(record.get('cache_create_tokens'))
        counters[3] += _as_int(record.get('cache_read_tokens'))
        amount = _as_cost(record)
        if amount is None:
            unknown += 1
            continue
        cost = amount if cost is None else cost + amount
    return Totals(
        input_tokens=counters[0],
        output_tokens=counters[1],
        cache_create_tokens=counters[2],
        cache_read_tokens=counters[3],
        cost_usd=cost,
        cost_known=unknown == 0,
        unknown_cost_records=unknown,
        record_count=len(records),
    )


def totals_for(
    machines: list[Machine],
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> Totals:
    """Total the whole fleet over an interval.

    Parameters
    ----------
    machines : list of Machine
        The fleet.
    since, until : datetime.date or None, optional
        Inclusive bounds.

    Returns
    -------
    Totals
        The fleet totals.
    """
    records = [record for _m, _a, record in _iter_days(machines, since, until)]
    return _sum_records(records)


def _model_records(day: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the model breakdown of a day record.

    Parameters
    ----------
    day : dict
        A raw day record.

    Returns
    -------
    list of dict
        The recorded models, or the day itself under
        :data:`UNKNOWN_MODEL` when no breakdown was recorded, so that
        model totals still add up to the fleet total.
    """
    models = [_as_dict(item) for item in _as_list(day.get('models'))]
    if models:
        return models
    fallback = dict(day)
    fallback['model'] = UNKNOWN_MODEL
    return [fallback]


def model_totals(
    machines: list[Machine],
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> list[tuple[str, Totals]]:
    """Total the fleet per model.

    Parameters
    ----------
    machines : list of Machine
        The fleet.
    since, until : datetime.date or None, optional
        Inclusive bounds.

    Returns
    -------
    list of (str, Totals)
        One entry per model, ordered by model name.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for _machine, _agent, day in _iter_days(machines, since, until):
        for record in _model_records(day):
            name = record.get('model')
            key = name if isinstance(name, str) and name else UNKNOWN_MODEL
            grouped.setdefault(key, []).append(record)
    return [(key, _sum_records(grouped[key])) for key in sorted(grouped)]


def daily_costs(
    machines: list[Machine],
    since: dt.date,
    until: dt.date,
) -> list[tuple[dt.date, Totals]]:
    """Total the fleet per calendar day.

    Parameters
    ----------
    machines : list of Machine
        The fleet.
    since, until : datetime.date
        Inclusive bounds.

    Returns
    -------
    list of (datetime.date, Totals)
        One entry per day that carries at least one record, ordered
        chronologically.
    """
    grouped: dict[dt.date, list[dict[str, Any]]] = {}
    for _machine, _agent, record in _iter_days(machines, since, until):
        day = _parse_date(record.get('date'))
        if day is not None:
            grouped.setdefault(day, []).append(record)
    return [(day, _sum_records(grouped[day])) for day in sorted(grouped)]


def _escape(text: str) -> str:
    """Escape untrusted text for a Markdown table cell.

    Machine labels, agent names and error messages come from other
    machines, so they are escaped rather than trusted.

    Parameters
    ----------
    text : str
        The raw text.

    Returns
    -------
    str
        Text that cannot break the table or inject markup.
    """
    out = text.replace('\\', '\\\\')
    for char in ('|', '`', '[', ']', '<', '>', '*', '_'):
        out = out.replace(char, '\\' + char)
    return ' '.join(out.split())


def _format_amount(amount: Decimal, floor: bool = False) -> str:
    """Render an amount in dollars and cents.

    This mirrors :func:`fleet_usage.reporting.format_amount`, which
    cannot be imported here, so that the README and the command line
    report the same figures.

    Parameters
    ----------
    amount : decimal.Decimal
        A non-negative amount in US dollars.
    floor : bool, optional
        Round down instead of to nearest, so that a lower bound is
        never rounded up into a claim the data does not support.

    Returns
    -------
    str
        For example ``'1,234.57'``. A positive amount that rounds away
        to nothing becomes :data:`SUBCENT_COST`, so a nearly free row
        is never mistaken for an unused one.
    """
    rounding = ROUND_FLOOR if floor else ROUND_HALF_EVEN
    quantized = amount.quantize(COST_PLACES, rounding=rounding)
    if quantized == 0 and amount > 0:
        return SUBCENT_COST
    return f'{quantized:,.2f}'


def _format_cost(totals: Totals) -> str:
    """Render a cost for a Markdown table.

    Parameters
    ----------
    totals : Totals
        Summed records.

    Returns
    -------
    str
        The exact amount, a lower bound when some records are unpriced,
        or :data:`UNKNOWN_COST` when none are priced.
    """
    if totals.cost_usd is None:
        return UNKNOWN_COST
    amount = _format_amount(totals.cost_usd, floor=not totals.cost_known)
    return amount if totals.cost_known else f'&ge; {amount}'


def _freshness(
    ledger: dict[str, Any] | None,
    now: dt.datetime,
    stale_after_hours: int,
) -> tuple[str, dt.datetime | None]:
    """Classify a machine as fresh, stale or never seen.

    Parameters
    ----------
    ledger : dict or None
        The parsed ledger, if any.
    now : datetime.datetime
        Reference time.
    stale_after_hours : int
        Age at which a machine becomes stale.

    Returns
    -------
    tuple of (str, datetime.datetime or None)
        The classification and the last run timestamp.
    """
    if ledger is None:
        return 'never', None
    last_run = _parse_timestamp(ledger.get('last_run_at'))
    if last_run is None:
        return 'never', None
    if now - last_run <= dt.timedelta(hours=stale_after_hours):
        return 'fresh', last_run
    return 'stale', last_run


def _machine_rows(
    machines: list[Machine],
    now: dt.datetime,
    stale_after_hours: int,
) -> list[str]:
    """Render the per machine table body.

    Parameters
    ----------
    machines : list of Machine
        The fleet.
    now : datetime.datetime
        Reference time.
    stale_after_hours : int
        Staleness threshold.

    Returns
    -------
    list of str
        Markdown table rows.
    """
    rows: list[str] = []
    for machine in machines:
        state, last_run = _freshness(machine.ledger, now, stale_after_hours)
        ledger = machine.ledger or {}
        errors: list[str] = []
        agents = _as_dict(ledger.get('agents'))
        for agent in sorted(agents):
            message = _as_dict(agents[agent]).get('last_error')
            if isinstance(message, str) and message:
                errors.append(f'{_escape(agent)}: {_escape(message)}')
        anomalies = len(_as_list(ledger.get('anomalies')))
        stamp = (
            last_run.strftime(TIMESTAMP_FORMAT)
            if last_run is not None
            else UNKNOWN_COST
        )
        detail = '<br>'.join(errors) if errors else 'none'
        rows.append(
            f'| {_escape(machine.label)} | {stamp} | {state} | '
            f'{detail} | {anomalies} |'
        )
    return rows


def _totals_rows(labelled: list[tuple[str, Totals]]) -> list[str]:
    """Render a totals table body.

    Parameters
    ----------
    labelled : list of (str, Totals)
        Row label and summed records.

    Returns
    -------
    list of str
        Markdown table rows.
    """
    rows: list[str] = []
    for label, totals in labelled:
        rows.append(
            f'| {_escape(label)} | {totals.input_tokens:,} | '
            f'{totals.output_tokens:,} | {totals.cache_create_tokens:,} | '
            f'{totals.cache_read_tokens:,} | {totals.total_tokens:,} | '
            f'{totals.record_count} | {_format_cost(totals)} |'
        )
    return rows


def _chart(points: list[tuple[dt.date, Totals]]) -> list[str]:
    """Render the daily cost chart as Mermaid.

    Parameters
    ----------
    points : list of (datetime.date, Totals)
        Daily totals of the charted window.

    Returns
    -------
    list of str
        Markdown lines, including the fenced Mermaid block and a note
        about the days that had to be omitted.
    """
    priced = [
        (day, totals.cost_usd)
        for day, totals in points
        if totals.cost_known and totals.cost_usd is not None
    ]
    skipped = len(points) - len(priced)
    if not priced:
        return [
            'No day in the last '
            f'{CHART_DAYS} days has a fully priced cost, so no chart is '
            'shown.',
        ]
    labels = ', '.join(f'"{day.isoformat()}"' for day, _cost in priced)
    values = ', '.join(f'{cost:.4f}' for _day, cost in priced)
    top = max(cost for _day, cost in priced)
    ceiling = f'{top:.4f}' if top > 0 else '1'
    lines = [
        '```mermaid',
        'xychart-beta',
        f'    title "Daily cost in USD, last {CHART_DAYS} days"',
        f'    x-axis [{labels}]',
        f'    y-axis "USD" 0 --> {ceiling}',
        f'    bar [{values}]',
        '```',
    ]
    if skipped:
        lines.append('')
        lines.append(
            f'{skipped} day(s) are omitted from the chart because at least '
            'one record of that day carries no price.'
        )
    return lines


def render_readme(
    data_dir: Path,
    now: dt.datetime | None = None,
    stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
) -> str:
    """Build the README markdown from a data repository checkout.

    Parameters
    ----------
    data_dir : pathlib.Path
        Directory holding ``fleet.toml`` and ``machines/*.json``.
    now : datetime.datetime or None, optional
        Reference time; defaults to the current UTC time. Passing it
        explicitly makes the output reproducible.
    stale_after_hours : int, optional
        Age at which a machine is reported as stale.

    Returns
    -------
    str
        The markdown document, ending in a newline.
    """
    reference = now if now is not None else dt.datetime.now(dt.UTC)
    reference = reference.astimezone(dt.UTC)
    today = reference.date()
    month_start = today.replace(day=1)
    window_start = today - dt.timedelta(days=CHART_DAYS - 1)
    machines = load_fleet(data_dir)

    month = totals_for(machines, month_start, today)
    window = totals_for(machines, window_start, today)

    lines: list[str] = [
        '# Fleet usage',
        '',
        f'Generated {reference.strftime(TIMESTAMP_FORMAT)} from '
        f'{len(machines)} registered machine(s).',
        '',
        '## Machines',
        '',
        '| machine | last run | freshness | agent errors | anomalies |',
        '| --- | --- | --- | --- | --- |',
    ]
    lines.extend(_machine_rows(machines, reference, stale_after_hours))
    lines.extend(
        [
            '',
            '## Totals',
            '',
            '| period | input | output | cache write | cache read | '
            'total tokens | records | cost usd |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
        ]
    )
    lines.extend(
        _totals_rows(
            [
                (f'{month_start.isoformat()} .. {today.isoformat()}', month),
                (f'last {CHART_DAYS} days', window),
            ]
        )
    )
    lines.extend(
        [
            '',
            f'`{UNKNOWN_COST}` means no record carried a price and '
            '`&ge;` means the amount is a lower bound, because at least '
            'one record carried none. Unknown costs are never counted as '
            'zero.',
            '',
            f'## Models this month ({month_start.strftime("%Y-%m")})',
            '',
            '| model | input | output | cache write | cache read | '
            'total tokens | records | cost usd |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
        ]
    )
    models = model_totals(machines, month_start, today)
    if models:
        lines.extend(_totals_rows(models))
    else:
        lines.append('| no usage recorded | 0 | 0 | 0 | 0 | 0 | 0 | — |')
    lines.extend(
        [
            '',
            f'## Daily cost, last {CHART_DAYS} days',
            '',
        ]
    )
    lines.extend(_chart(daily_costs(machines, window_start, today)))
    lines.append('')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    """Command line entry point of the generated render script.

    Parameters
    ----------
    argv : list of str or None, optional
        Argument vector; defaults to :data:`sys.argv`.

    Returns
    -------
    int
        Process exit code; ``0`` on success and ``1`` when the data
        directory does not hold a readable manifest.
    """
    parser = argparse.ArgumentParser(
        description='Render a fleet usage README from the data repository.'
    )
    parser.add_argument('--data-dir', type=Path, default=Path())
    parser.add_argument('--output', type=Path, default=Path('README.md'))
    parser.add_argument(
        '--now',
        default=None,
        help='ISO-8601 UTC reference time, for reproducible output.',
    )
    parser.add_argument(
        '--stale-after-hours',
        type=int,
        default=DEFAULT_STALE_AFTER_HOURS,
    )
    args = parser.parse_args(argv)
    now = _parse_timestamp(args.now) if args.now else None
    if args.now and now is None:
        print(f'error: cannot parse --now {args.now!r}', file=sys.stderr)
        return 1
    try:
        document = render_readme(
            args.data_dir, now, stale_after_hours=args.stale_after_hours
        )
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    args.output.write_text(document, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
