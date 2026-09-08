"""Aggregation, status classification and rendering."""

import csv
import datetime as dt
import io
import json
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest
from rich.console import Console

from fleet_usage import reporting
from fleet_usage.fetch import FileMeta, FleetData
from fleet_usage.models import FleetManifest, Ledger

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'ledgers'
M1 = '11111111-1111-4111-8111-111111111111'
M2 = '22222222-2222-4222-8222-222222222222'
M3 = '33333333-3333-4333-8333-333333333333'
M4 = '44444444-4444-4444-8444-444444444444'

FETCHED_AT = dt.datetime(2026, 9, 5, 12, 30, tzinfo=dt.UTC)
FULL = (dt.date(2026, 9, 1), dt.date(2026, 9, 5))


def load_manifest(directory: Path = FIXTURES) -> FleetManifest:
    return FleetManifest.model_validate(
        tomllib.loads((directory / 'fleet.toml').read_text(encoding='utf-8'))
    )


def load_fleet(directory: Path = FIXTURES, **kwargs) -> FleetData:
    manifest = load_manifest(directory)
    ledgers: dict[str, Ledger] = {}
    files = {
        'fleet.toml': FileMeta('fleet.toml', FETCHED_AT, False, 'sha-fleet')
    }
    never: list[str] = []
    for entry in manifest.machines:
        path = directory / 'machines' / f'{entry.id}.json'
        if not path.is_file():
            never.append(entry.id)
            continue
        ledgers[entry.id] = Ledger.model_validate_json(path.read_bytes())
        remote = f'machines/{entry.id}.json'
        files[remote] = FileMeta(remote, FETCHED_AT, False, f'sha-{entry.id}')
    return FleetData(
        manifest=manifest,
        ledgers=ledgers,
        files=files,
        never_reported=tuple(never),
        **kwargs,
    )


@pytest.fixture
def fleet() -> FleetData:
    return load_fleet()


def row_by(rows, key):
    matches = [row for row in rows if row.key == key]
    assert len(matches) == 1, f'{key} not found in {[r.key for r in rows]}'
    return matches[0]


def tokens(row):
    return (
        row.input_tokens,
        row.output_tokens,
        row.cache_create_tokens,
        row.cache_read_tokens,
    )


# --- aggregation -----------------------------------------------------


def test_by_machine_totals(fleet):
    rows = reporting.aggregate(
        fleet, by='machine', since=FULL[0], until=FULL[1]
    )
    assert [row.key for row in rows] == [M1, M3, M2]  # ordered by label

    one = row_by(rows, M1)
    assert tokens(one) == (4400, 870, 300, 1400)
    assert one.total_tokens == 6970
    assert one.record_count == 4
    assert one.cost_usd == Decimal('3.6000')
    assert one.cost_known is False
    assert one.unknown_cost_records == 1
    assert one.label == 'fedora-mobile'

    two = row_by(rows, M2)
    assert tokens(two) == (4350, 870, 110, 220)
    assert two.total_tokens == 5550
    assert two.cost_usd == Decimal('5.8500')
    assert two.cost_known is True
    assert two.record_count == 3

    three = row_by(rows, M3)
    assert tokens(three) == (10, 2, 0, 0)
    assert three.cost_usd is None
    assert three.cost_known is False
    assert three.record_count == 1


def test_by_agent_totals(fleet):
    rows = reporting.aggregate(fleet, by='agent', since=FULL[0], until=FULL[1])
    assert [row.key for row in rows] == ['claude', 'codex']

    claude = row_by(rows, 'claude')
    assert tokens(claude) == (7810, 1562, 410, 1620)
    assert claude.total_tokens == 11402
    assert claude.cost_usd == Decimal('9.3000')
    assert claude.cost_known is False
    assert claude.record_count == 5

    codex = row_by(rows, 'codex')
    assert tokens(codex) == (950, 180, 0, 0)
    assert codex.cost_usd == Decimal('0.1500')
    assert codex.cost_known is False
    assert codex.unknown_cost_records == 1
    assert codex.record_count == 3


def test_by_model_totals(fleet):
    rows = reporting.aggregate(fleet, by='model', since=FULL[0], until=FULL[1])
    assert [row.key for row in rows] == [
        'claude-fable-5-1',
        'claude-sonnet-4-6',
        'gpt-5-codex',
    ]

    fable = row_by(rows, 'claude-fable-5-1')
    assert tokens(fable) == (7010, 1402, 400, 1600)
    assert fable.cost_usd == Decimal('8.7500')
    assert fable.cost_known is False
    assert fable.record_count == 4

    sonnet = row_by(rows, 'claude-sonnet-4-6')
    assert tokens(sonnet) == (800, 160, 10, 20)
    assert sonnet.cost_usd == Decimal('0.5500')
    assert sonnet.cost_known is True

    codex = row_by(rows, 'gpt-5-codex')
    assert tokens(codex) == (950, 180, 0, 0)
    assert codex.cost_usd == Decimal('0.1500')
    assert codex.cost_known is False


def test_by_day_totals(fleet):
    rows = reporting.aggregate(fleet, by='day', since=FULL[0], until=FULL[1])
    assert [row.key for row in rows] == [
        '2026-09-01',
        '2026-09-02',
        '2026-09-03',
        '2026-09-04',
    ]
    first = row_by(rows, '2026-09-01')
    assert tokens(first) == (2310, 452, 300, 400)
    assert first.cost_usd == Decimal('1.5000')
    assert first.cost_known is False
    assert first.unknown_cost_records == 2

    second = row_by(rows, '2026-09-02')
    assert tokens(second) == (2350, 470, 10, 1020)
    assert second.cost_usd == Decimal('2.3500')
    assert second.cost_known is True
    assert second.record_count == 3


def test_by_month_matches_fleet_totals(fleet):
    rows = reporting.aggregate(fleet, by='month', since=FULL[0], until=FULL[1])
    assert [row.key for row in rows] == ['2026-09']
    month = rows[0]
    assert tokens(month) == (8760, 1742, 410, 1620)
    assert month.total_tokens == 12532
    assert month.cost_usd == Decimal('9.4500')
    assert month.cost_known is False
    assert month.record_count == 8

    totals = reporting.fleet_totals(fleet, since=FULL[0], until=FULL[1])
    assert tokens(totals) == tokens(month)
    assert totals.cost_usd == month.cost_usd
    assert totals.record_count == month.record_count


def test_groupings_agree_on_the_total(fleet):
    reference = reporting.fleet_totals(fleet, since=FULL[0], until=FULL[1])
    for by in ('machine', 'agent', 'model', 'day', 'month'):
        rows = reporting.aggregate(fleet, by=by, since=FULL[0], until=FULL[1])
        assert sum(row.total_tokens for row in rows) == reference.total_tokens
        assert sum(row.record_count for row in rows) >= reference.record_count


def test_filters_narrow_the_result(fleet):
    rows = reporting.aggregate(
        fleet,
        by='machine',
        since=dt.date(2026, 9, 2),
        until=dt.date(2026, 9, 3),
    )
    assert [row.key for row in rows] == [M1, M2]
    one = row_by(rows, M1)
    assert tokens(one) == (2100, 420, 0, 1000)
    assert one.cost_usd == Decimal('2.1000')
    assert one.cost_known is True
    two = row_by(rows, M2)
    assert tokens(two) == (350, 70, 10, 20)
    assert two.cost_usd == Decimal('0.3500')
    assert two.record_count == 2


def test_open_bounds(fleet):
    left = reporting.aggregate(fleet, by='day', until=dt.date(2026, 9, 1))
    assert [row.key for row in left] == ['2026-09-01']
    right = reporting.aggregate(fleet, by='day', since=dt.date(2026, 9, 4))
    assert [row.key for row in right] == ['2026-09-04']
    everything = reporting.aggregate(fleet, by='day')
    assert len(everything) == 4


def test_empty_period_yields_no_rows(fleet):
    rows = reporting.aggregate(
        fleet,
        by='machine',
        since=dt.date(2026, 10, 1),
        until=dt.date(2026, 10, 31),
    )
    assert rows == []
    totals = reporting.fleet_totals(
        fleet, since=dt.date(2026, 10, 1), until=dt.date(2026, 10, 31)
    )
    assert totals.record_count == 0
    assert totals.cost_usd is None
    assert totals.cost_known is True


def test_unknown_cost_is_never_summed_as_zero(fleet):
    rows = reporting.aggregate(
        fleet, by='machine', since=FULL[0], until=FULL[1]
    )
    three = row_by(rows, M3)
    assert three.cost_usd is None
    assert reporting.format_cost(three) == reporting.UNKNOWN_COST
    one = row_by(rows, M1)
    assert reporting.format_cost(one) == '>= 3.6000'
    two = row_by(rows, M2)
    assert reporting.format_cost(two) == '5.8500'


def test_day_without_model_breakdown_is_attributed(tmp_path):
    payload = json.loads(
        (FIXTURES / 'machines' / f'{M3}.json').read_text(encoding='utf-8')
    )
    payload['agents']['claude']['days'][0]['models'] = []
    directory = tmp_path / 'data'
    (directory / 'machines').mkdir(parents=True)
    (directory / 'fleet.toml').write_text(
        (FIXTURES / 'fleet.toml').read_text(encoding='utf-8'), encoding='utf-8'
    )
    (directory / 'machines' / f'{M3}.json').write_text(
        json.dumps(payload), encoding='utf-8'
    )
    fleet = load_fleet(directory)
    rows = reporting.aggregate(fleet, by='model')
    assert reporting.UNKNOWN_MODEL in {row.key for row in rows}
    unknown = row_by(rows, reporting.UNKNOWN_MODEL)
    assert unknown.input_tokens == 10


def test_unknown_grouping_is_rejected(fleet):
    with pytest.raises(ValueError, match='unknown grouping'):
        reporting.aggregate(fleet, by='week')


def test_apply_corrections_is_a_no_op(fleet):
    assert reporting.apply_corrections(fleet, {'anything': 1}) is fleet


# --- status ----------------------------------------------------------


def test_machine_status_classifies_fresh_stale_and_never(fleet):
    now = dt.datetime(2026, 9, 5, 13, 0, tzinfo=dt.UTC)
    statuses = reporting.machine_status(fleet, now, stale_after_hours=3)
    by_id = {status.machine_id: status for status in statuses}
    assert len(statuses) == 4
    assert by_id[M1].freshness == 'fresh'
    assert by_id[M2].freshness == 'fresh'
    assert by_id[M3].freshness == 'stale'
    assert by_id[M4].freshness == 'never'
    assert by_id[M4].last_run_at is None
    assert by_id[M4].anomaly_count == 0
    assert by_id[M3].anomaly_count == 1
    assert by_id[M2].agent_errors == {
        'codex': 'ccusage exited with code 1: codex data unreadable'
    }
    assert by_id[M1].agent_errors == {}


def test_machine_status_threshold_is_inclusive(fleet):
    now = dt.datetime(2026, 9, 5, 15, 0, tzinfo=dt.UTC)
    statuses = reporting.machine_status(fleet, now, stale_after_hours=3)
    by_id = {status.machine_id: status for status in statuses}
    assert by_id[M1].freshness == 'fresh'
    assert by_id[M2].freshness == 'stale'


def test_summary_counts_machines_and_cost_coverage(fleet):
    now = dt.datetime(2026, 9, 5, 13, 0, tzinfo=dt.UTC)
    statuses = reporting.machine_status(fleet, now, 3)
    totals = reporting.fleet_totals(fleet, since=FULL[0], until=FULL[1])
    summary = reporting.build_summary(fleet, statuses, totals)
    assert summary.expected == 4
    assert summary.reporting == 2
    assert summary.stale == 1
    assert summary.never == 1
    assert summary.total_records == 8
    assert summary.unknown_cost_records == 2
    assert summary.cost_coverage == '6/8 records priced'
    lines = summary.lines(now)
    assert '4 expected' in lines[0]
    assert 'never reported' in lines[0]
    assert 'fetched 30 min ago' in lines[2]


def test_summary_reports_cache_use():
    fleet = load_fleet(offline=True)
    now = dt.datetime(2026, 9, 5, 13, 0, tzinfo=dt.UTC)
    statuses = reporting.machine_status(fleet, now, 3)
    totals = reporting.fleet_totals(fleet)
    summary = reporting.build_summary(fleet, statuses, totals)
    assert summary.offline is True
    assert 'offline (cache only)' in summary.lines(now)[2]


# --- default period --------------------------------------------------


@pytest.mark.parametrize(
    ('period', 'expected'),
    [
        ('day', (dt.date(2026, 9, 8), dt.date(2026, 9, 8))),
        ('week', (dt.date(2026, 9, 2), dt.date(2026, 9, 8))),
        ('month', (dt.date(2026, 9, 1), dt.date(2026, 9, 8))),
        ('all', (None, None)),
    ],
)
def test_default_range(period, expected):
    assert reporting.default_range(period, dt.date(2026, 9, 8)) == expected


# --- rendering -------------------------------------------------------


def test_render_json_is_stable_and_free_of_ansi(fleet):
    rows = reporting.aggregate(
        fleet, by='machine', since=FULL[0], until=FULL[1]
    )
    totals = reporting.fleet_totals(fleet, since=FULL[0], until=FULL[1])
    text = reporting.render_json(
        rows, by='machine', since=FULL[0], until=FULL[1], totals=totals
    )
    assert '\x1b[' not in text
    assert text == reporting.render_json(
        rows, by='machine', since=FULL[0], until=FULL[1], totals=totals
    )
    payload = json.loads(text)
    assert payload['group_by'] == 'machine'
    assert payload['since'] == '2026-09-01'
    assert payload['until'] == '2026-09-05'
    assert [row['key'] for row in payload['rows']] == [M1, M3, M2]
    assert payload['rows'][0]['cost_usd'] == '3.6000'
    assert payload['rows'][0]['cost_known'] is False
    assert payload['rows'][1]['cost_usd'] is None
    assert payload['totals']['total_tokens'] == 12532
    keys = list(payload['rows'][0])
    assert keys == sorted(keys)


def test_render_json_includes_status_and_machines(fleet):
    now = dt.datetime(2026, 9, 5, 13, 0, tzinfo=dt.UTC)
    statuses = reporting.machine_status(fleet, now, 3)
    totals = reporting.fleet_totals(fleet)
    summary = reporting.build_summary(fleet, statuses, totals)
    payload = json.loads(
        reporting.render_json(
            [], by='machine', summary=summary, statuses=statuses
        )
    )
    assert payload['status']['never'] == 1
    assert payload['status']['fetched_at'] == '2026-09-05T12:30:00Z'
    assert payload['machines'][0]['freshness'] in {
        'fresh',
        'stale',
        'never',
    }
    assert payload['machines'][0]['label'] == 'fedora-mobile'


def test_render_csv_header_and_values(fleet):
    rows = reporting.aggregate(
        fleet, by='machine', since=FULL[0], until=FULL[1]
    )
    text = reporting.render_csv(rows)
    assert '\x1b[' not in text
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == list(reporting.CSV_COLUMNS)
    assert parsed[1][0] == M1
    assert parsed[1][parsed[0].index('cost_usd')] == '3.6000'
    assert parsed[1][parsed[0].index('cost_known')] == 'false'
    assert parsed[2][parsed[0].index('cost_usd')] == ''


def test_render_csv_defuses_formula_injection():
    row = reporting.Row(
        group='machine',
        key='=cmd|calc',
        label='@SUM(A1:A2)',
        input_tokens=1,
        output_tokens=0,
        cache_create_tokens=0,
        cache_read_tokens=0,
        cost_usd=Decimal('1'),
        cost_known=True,
        unknown_cost_records=0,
        record_count=1,
    )
    parsed = list(csv.reader(io.StringIO(reporting.render_csv([row]))))
    assert parsed[1][0] == "'=cmd|calc"
    assert parsed[1][1] == "'@SUM(A1:A2)"


@pytest.mark.parametrize('cell', ['=1+1', '+1', '-1', '@x', '\tx', '\rx'])
def test_render_csv_quotes_every_dangerous_prefix(cell):
    row = reporting.Row(
        group='machine',
        key=cell,
        label=cell,
        input_tokens=0,
        output_tokens=0,
        cache_create_tokens=0,
        cache_read_tokens=0,
        cost_usd=None,
        cost_known=False,
        unknown_cost_records=1,
        record_count=1,
    )
    text = reporting.render_csv([row])
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[1][0].startswith("'")


def test_render_table_prints_panel_and_rows(fleet):
    now = dt.datetime(2026, 9, 5, 13, 0, tzinfo=dt.UTC)
    rows = reporting.aggregate(
        fleet, by='machine', since=FULL[0], until=FULL[1]
    )
    totals = reporting.fleet_totals(fleet, since=FULL[0], until=FULL[1])
    statuses = reporting.machine_status(fleet, now, 3)
    summary = reporting.build_summary(fleet, statuses, totals)
    console = Console(
        file=io.StringIO(), width=200, no_color=True, highlight=False
    )
    reporting.render_table(
        rows,
        by='machine',
        totals=totals,
        summary=summary,
        statuses=statuses,
        now=now,
        console=console,
    )
    text = console.file.getvalue()
    assert 'fleet status' in text
    assert '4 expected' in text
    assert 'fedora-mobile' in text
    assert 'nuc | attic' in text
    assert 'travel-laptop: never' in text
    assert 'fleet total' in text
    assert '12,532' in text
    assert reporting.UNKNOWN_COST in text


def test_render_table_reports_an_empty_period(fleet):
    console = Console(file=io.StringIO(), width=120, no_color=True)
    reporting.render_table([], by='day', console=console)
    assert 'no usage records' in console.file.getvalue()
