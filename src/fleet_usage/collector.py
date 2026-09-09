"""Execution and parsing of the ``ccusage`` collector.

The unified invocation ``ccusage daily --json --by-agent --offline
-z <tz>`` returns every agent in one document. Unpriced models are
reported with a cost of ``0.0`` rather than ``null``, so a zero cost
combined with a positive token count must be recorded as an unknown
cost. The command vector always comes from the settings and is never
run through a shell.

Parsing is strict and all-or-nothing: a document that does not match the
expected shape raises :class:`CollectorError` instead of yielding a
partially populated result, because a silently truncated snapshot would
be merged into the ledger and could freeze wrong numbers forever.
"""

import datetime as dt
import json
import math
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fleet_usage.config import CollectorSettings
from fleet_usage.models import (
    AgentSnapshot,
    CostStatus,
    DayRecord,
    ModelRecord,
)

__all__ = [
    'STDERR_TAIL_LINES',
    'CollectorError',
    'build_command',
    'collector_version',
    'load_fixture',
    'parse_daily_by_agent',
    'run_collector',
]

STDERR_TAIL_LINES = 20
_TOKEN_FIELDS: tuple[tuple[str, str], ...] = (
    ('input_tokens', 'inputTokens'),
    ('output_tokens', 'outputTokens'),
    ('cache_create_tokens', 'cacheCreationTokens'),
    ('cache_read_tokens', 'cacheReadTokens'),
)
_VERSION_RE = re.compile(r'^v?(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)$')


COST_QUANTUM = Decimal('0.000001')
"""Costs are kept to micro-dollars; finer digits are float noise."""


class CollectorError(Exception):
    """Raised when the collector cannot be run or parsed."""


def build_command(settings: CollectorSettings) -> list[str]:
    """Build the argument vector for a collection run.

    Parameters
    ----------
    settings : CollectorSettings
        Collector configuration.

    Returns
    -------
    list of str
        The full argument vector, for example ``['bunx',
        'ccusage@20.0.20', 'daily', '--json', '--by-agent', '-z',
        'Europe/Berlin', '--offline']``.
    """
    command = [
        *settings.command,
        'daily',
        '--json',
        '--by-agent',
        '-z',
        settings.timezone,
    ]
    if settings.offline_pricing:
        command.append('--offline')
    return command


def _tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    """Return the last ``lines`` non-empty lines of ``text``.

    Parameters
    ----------
    text : str
        Captured output.
    lines : int, optional
        Maximum number of lines to keep.

    Returns
    -------
    str
        The tail, or ``'<no output>'`` when nothing was captured.
    """
    kept = [line for line in text.splitlines() if line.strip()]
    if not kept:
        return '<no output>'
    return '\n'.join(kept[-lines:])


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run ``command`` without a shell and return the finished process.

    The resolved path is handed to :func:`subprocess.run` as
    ``executable`` while ``command`` stays the argument vector, so the
    program still sees the configured name as ``argv[0]``. Multi-call
    binaries dispatch on that name: ``bunx`` is a copy of ``bun`` that
    only runs a package when it is called ``bunx``. On Windows
    :func:`shutil.which` reports the extension in the case of ``PATHEXT``
    rather than the case on disk, so passing its result as ``argv[0]``
    would turn ``bunx`` into ``bunx.EXE``, and ``bun`` would fall back to
    running the package specifier as a script file.

    Parameters
    ----------
    command : list of str
        Argument vector; the first element is resolved on ``PATH``.
    timeout : int
        Seconds after which the collector is killed.

    Returns
    -------
    subprocess.CompletedProcess
        The finished process with decoded output.

    Raises
    ------
    CollectorError
        If the executable cannot be found, the process cannot be
        started or it exceeds ``timeout``.
    """
    executable = shutil.which(command[0])
    if executable is None:
        msg = (
            f'collector executable {command[0]!r} not found on PATH\n'
            "check the 'command' entry of the [collector] settings "
            'section and make sure the program is installed'
        )
        raise CollectorError(msg)
    try:
        return subprocess.run(
            command,
            executable=executable,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = (
            f'collector timed out after {timeout}s: '
            f'{" ".join(command)}\n'
            "raise 'timeout_seconds' in the [collector] settings section"
        )
        raise CollectorError(msg) from exc
    except OSError as exc:
        msg = f'cannot run collector {" ".join(command)}: {exc}'
        raise CollectorError(msg) from exc


def _check_returncode(
    process: subprocess.CompletedProcess[str], command: list[str]
) -> None:
    """Raise when the collector exited with a non-zero status.

    Parameters
    ----------
    process : subprocess.CompletedProcess
        The finished process.
    command : list of str
        The argument vector, used in the message.

    Raises
    ------
    CollectorError
        If the exit status is not zero.
    """
    if process.returncode == 0:
        return
    msg = (
        f'collector exited with status {process.returncode}: '
        f'{" ".join(command)}\n'
        f'last {STDERR_TAIL_LINES} stderr lines:\n{_tail(process.stderr)}'
    )
    raise CollectorError(msg)


def run_collector(settings: CollectorSettings) -> dict[str, Any]:
    """Run the collector and return its parsed JSON document.

    Parameters
    ----------
    settings : CollectorSettings
        Collector configuration.

    Returns
    -------
    dict
        The parsed ``ccusage`` output.

    Raises
    ------
    CollectorError
        If the executable is missing, the run times out, the exit
        status is non-zero or the output is not a JSON object.
    """
    command = build_command(settings)
    process = _run(command, settings.timeout_seconds)
    _check_returncode(process, command)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        msg = (
            f'collector did not produce valid JSON: {exc}\n'
            f'command: {" ".join(command)}\n'
            f'first 200 characters of stdout: {process.stdout[:200]!r}'
        )
        raise CollectorError(msg) from exc
    if not isinstance(payload, dict):
        msg = (
            'collector produced a JSON '
            f'{type(payload).__name__}, expected an object\n'
            f'command: {" ".join(command)}'
        )
        raise CollectorError(msg)
    return payload


def collector_version(settings: CollectorSettings) -> str:
    """Return the version reported by the collector.

    Parameters
    ----------
    settings : CollectorSettings
        Collector configuration; only ``command`` and
        ``timeout_seconds`` are used.

    Returns
    -------
    str
        The bare version, for example ``'20.0.20'``.

    Raises
    ------
    CollectorError
        If the collector cannot be run or its output does not end in a
        line of the form ``ccusage X.Y.Z``.
    """
    command = [*settings.command, '--version']
    process = _run(command, settings.timeout_seconds)
    _check_returncode(process, command)
    lines = [line.strip() for line in process.stdout.splitlines()]
    non_empty = [line for line in lines if line]
    if not non_empty:
        msg = (
            f'collector printed no version: {" ".join(command)}\n'
            f'stderr:\n{_tail(process.stderr)}'
        )
        raise CollectorError(msg)
    candidate = non_empty[-1]
    if candidate.lower().startswith('ccusage'):
        candidate = candidate[len('ccusage') :].strip()
    match = _VERSION_RE.match(candidate)
    if match is None:
        msg = (
            'cannot parse the collector version from '
            f'{non_empty[-1]!r}\nexpected a line like "ccusage 20.0.20"'
        )
        raise CollectorError(msg)
    return match.group(1)


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    """Return ``value`` as a mapping or raise.

    Parameters
    ----------
    value : Any
        Candidate value from the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    Mapping[str, Any]
        The mapping.

    Raises
    ------
    CollectorError
        If ``value`` is not a JSON object.
    """
    if not isinstance(value, dict):
        msg = (
            f'unexpected collector output: {what} is a '
            f'{type(value).__name__}, expected an object'
        )
        raise CollectorError(msg)
    return value


def _require_list(value: Any, what: str) -> Sequence[Any]:
    """Return ``value`` as a list or raise.

    Parameters
    ----------
    value : Any
        Candidate value from the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    Sequence[Any]
        The list.

    Raises
    ------
    CollectorError
        If ``value`` is not a JSON array.
    """
    if not isinstance(value, list):
        msg = (
            f'unexpected collector output: {what} is a '
            f'{type(value).__name__}, expected an array'
        )
        raise CollectorError(msg)
    return value


def _require_str(value: Any, what: str) -> str:
    """Return ``value`` as a non-empty string or raise.

    Parameters
    ----------
    value : Any
        Candidate value from the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    str
        The string.

    Raises
    ------
    CollectorError
        If ``value`` is not a non-empty string.
    """
    if not isinstance(value, str) or not value.strip():
        msg = (
            f'unexpected collector output: {what} is {value!r}, '
            'expected a non-empty string'
        )
        raise CollectorError(msg)
    return value


def _require_date(value: Any, what: str) -> dt.date:
    """Parse an ISO ``YYYY-MM-DD`` value or raise.

    Parameters
    ----------
    value : Any
        Candidate value from the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    datetime.date
        The parsed day.

    Raises
    ------
    CollectorError
        If ``value`` is not an ISO calendar date.
    """
    text = _require_str(value, what)
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        msg = (
            f'unexpected collector output: {what} is {value!r}, '
            "expected a date of the form 'YYYY-MM-DD'"
        )
        raise CollectorError(msg) from exc


def _require_tokens(source: Mapping[str, Any], key: str, what: str) -> int:
    """Read a non-negative integer token counter or raise.

    Parameters
    ----------
    source : Mapping[str, Any]
        Object carrying the counter.
    key : str
        Name of the counter in the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    int
        The counter.

    Raises
    ------
    CollectorError
        If the counter is missing, not an integer or negative.
    """
    if key not in source:
        msg = f'unexpected collector output: {what} has no {key!r} field'
        raise CollectorError(msg)
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        msg = (
            f'unexpected collector output: {what}.{key} is {value!r}, '
            'expected an integer token count'
        )
        raise CollectorError(msg)
    if value < 0:
        msg = (
            f'unexpected collector output: {what}.{key} is {value}, '
            'token counts must not be negative'
        )
        raise CollectorError(msg)
    return value


def _require_cost(source: Mapping[str, Any], key: str, what: str) -> float:
    """Read a finite, non-negative cost or raise.

    Parameters
    ----------
    source : Mapping[str, Any]
        Object carrying the cost.
    key : str
        Name of the cost field in the collector document.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    float
        The cost in US dollars.

    Raises
    ------
    CollectorError
        If the cost is missing, not a number, not finite or negative.
    """
    if key not in source:
        msg = f'unexpected collector output: {what} has no {key!r} field'
        raise CollectorError(msg)
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = (
            f'unexpected collector output: {what}.{key} is {value!r}, '
            'expected a number'
        )
        raise CollectorError(msg)
    number = float(value)
    if not math.isfinite(number):
        msg = (
            f'unexpected collector output: {what}.{key} is {value!r}, '
            'expected a finite number'
        )
        raise CollectorError(msg)
    if number < 0:
        msg = (
            f'unexpected collector output: {what}.{key} is {value!r}, '
            'costs must not be negative'
        )
        raise CollectorError(msg)
    return number


def _classify_cost(
    cost: float, total_tokens: int, what: str
) -> tuple[Decimal | None, CostStatus]:
    """Turn a reported cost into an amount and a status.

    ``ccusage`` reports unpriced models with a cost of ``0.0`` instead of
    ``null``, so a zero cost next to a positive token count means the
    price is unknown rather than free.

    Parameters
    ----------
    cost : float
        The reported cost.
    total_tokens : int
        Sum of the four token counters of the same record.
    what : str
        Human readable description used in the error message.

    Returns
    -------
    tuple of (decimal.Decimal or None, str)
        The exact amount and its status.

    Raises
    ------
    CollectorError
        If the float cannot be converted to a decimal.
    """
    if cost > 0:
        try:
            amount = Decimal(str(cost)).quantize(
                COST_QUANTUM, rounding=ROUND_HALF_EVEN
            )
            return amount, 'estimated'
        except InvalidOperation as exc:  # pragma: no cover - guarded above
            msg = f'unexpected collector output: {what} cost {cost!r}'
            raise CollectorError(msg) from exc
    if total_tokens > 0:
        return None, 'unknown'
    return Decimal('0'), 'estimated'


def _parse_model(entry: Any, what: str) -> ModelRecord:
    """Convert one ``modelBreakdowns`` entry into a model record.

    Parameters
    ----------
    entry : Any
        Candidate entry from the collector document.
    what : str
        Human readable description used in error messages.

    Returns
    -------
    ModelRecord
        The parsed record.

    Raises
    ------
    CollectorError
        If the entry does not have the expected shape.
    """
    breakdown = _require_mapping(entry, what)
    name = _require_str(breakdown.get('modelName'), f'{what}.modelName')
    counts = {
        field: _require_tokens(breakdown, key, what)
        for field, key in _TOKEN_FIELDS
    }
    cost = _require_cost(breakdown, 'cost', what)
    amount, status = _classify_cost(cost, sum(counts.values()), what)
    return ModelRecord(
        model=name, cost_usd=amount, cost_status=status, **counts
    )


def _parse_agent_day(
    entry: Mapping[str, Any], day: dt.date, what: str
) -> DayRecord:
    """Convert one ``agents`` entry of one row into a day record.

    Parameters
    ----------
    entry : Mapping[str, Any]
        The per agent object of a daily row.
    day : datetime.date
        The calendar day of the row.
    what : str
        Human readable description used in error messages.

    Returns
    -------
    DayRecord
        The parsed record with sorted models.

    Raises
    ------
    CollectorError
        If the entry does not have the expected shape.
    """
    counts = {
        field: _require_tokens(entry, key, what)
        for field, key in _TOKEN_FIELDS
    }
    raw_models = _require_list(
        entry.get('modelBreakdowns', []), f'{what}.modelBreakdowns'
    )
    models = [
        _parse_model(item, f'{what}.modelBreakdowns[{index}]')
        for index, item in enumerate(raw_models)
    ]
    total = _require_cost(entry, 'totalCost', what)
    amount: Decimal | None
    status: CostStatus
    if not models:
        amount, status = _classify_cost(total, sum(counts.values()), what)
    elif any(model.cost_status == 'unknown' for model in models):
        amount, status = None, 'unknown'
    else:
        known = [
            model.cost_usd for model in models if model.cost_usd is not None
        ]
        amount, status = sum(known, Decimal('0')), 'estimated'
    return DayRecord(
        date=day,
        cost_usd=amount,
        cost_status=status,
        models=models,
        **counts,
    )


def _parse_rows(
    rows: Sequence[Any],
) -> dict[str, list[DayRecord]]:
    """Walk ``daily[*].agents[*]`` and group day records by agent.

    Parameters
    ----------
    rows : Sequence[Any]
        The ``daily`` array of the collector document.

    Returns
    -------
    dict of str to list of DayRecord
        One entry per agent seen in the output.

    Raises
    ------
    CollectorError
        If a row lacks ``agents`` (the collector was called without
        ``--by-agent``) or any value has an unexpected shape.
    """
    per_agent: dict[str, list[DayRecord]] = {}
    seen: set[tuple[str, dt.date]] = set()
    for index, item in enumerate(rows):
        what = f'daily[{index}]'
        row = _require_mapping(item, what)
        day = _require_date(row.get('period'), f'{what}.period')
        if 'agents' not in row:
            msg = (
                f'{what} has no "agents" field: the collector must be '
                'run with --by-agent so that usage can be attributed '
                'to individual agents\n'
                'a collector version without --by-agent support cannot '
                'be used by fleet-usage'
            )
            raise CollectorError(msg)
        entries = _require_list(row['agents'], f'{what}.agents')
        for position, raw_agent in enumerate(entries):
            agent_what = f'{what}.agents[{position}]'
            agent = _require_mapping(raw_agent, agent_what)
            name = _require_str(agent.get('agent'), f'{agent_what}.agent')
            if (name, day) in seen:
                msg = (
                    'unexpected collector output: agent '
                    f'{name!r} appears twice for {day.isoformat()}'
                )
                raise CollectorError(msg)
            seen.add((name, day))
            record = _parse_agent_day(agent, day, agent_what)
            per_agent.setdefault(name, []).append(record)
    return per_agent


def parse_daily_by_agent(
    payload: Mapping[str, Any],
    configured_agents: Sequence[str],
) -> dict[str, AgentSnapshot]:
    """Convert collector output into the ``agents`` mapping.

    Parameters
    ----------
    payload : Mapping[str, Any]
        Parsed ``ccusage daily --json --by-agent`` document.
    configured_agents : Sequence[str]
        Agents named in the settings. Agents that are configured but
        absent from the output get an empty, successful entry; agents
        present but not configured are kept as well.

    Returns
    -------
    dict of str to AgentSnapshot
        The snapshot payload, ordered by agent name. Either the whole
        document is converted or nothing is returned.

    Raises
    ------
    CollectorError
        If the document does not have the expected shape.
    """
    document = _require_mapping(payload, 'collector output')
    if 'daily' not in document:
        msg = (
            'unexpected collector output: no "daily" key\n'
            "expected the result of 'ccusage daily --json --by-agent'"
        )
        raise CollectorError(msg)
    rows = _require_list(document['daily'], 'daily')
    per_agent = _parse_rows(rows)
    names = set(per_agent) | {name for name in configured_agents if name}
    return {
        name: AgentSnapshot(status='ok', days=per_agent.get(name, []))
        for name in sorted(names)
    }


def load_fixture(path: Path) -> dict[str, Any]:
    """Load a recorded collector document from disk.

    Parameters
    ----------
    path : pathlib.Path
        JSON file produced by ``ccusage``.

    Returns
    -------
    dict
        The parsed document.

    Raises
    ------
    CollectorError
        If the file cannot be read or is not a JSON object.
    """
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        msg = f'cannot read collector output {path}: {exc}'
        raise CollectorError(msg) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f'{path} is not valid JSON: {exc}'
        raise CollectorError(msg) from exc
    if not isinstance(payload, dict):
        msg = f'{path} is a JSON {type(payload).__name__}, expected an object'
        raise CollectorError(msg)
    return payload
