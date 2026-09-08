"""The dependency-free README renderer and its parity with reporting."""

import ast
import datetime as dt
import sys
from pathlib import Path

import pytest
from test_reporting import load_fleet  # same directory

from fleet_usage import readme_render, reporting

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'ledgers'
MODULE_PATH = Path(readme_render.__file__)
NOW = dt.datetime(2026, 9, 8, 6, 0, tzinfo=dt.UTC)
MONTH = (dt.date(2026, 9, 1), dt.date(2026, 9, 8))
M3 = '33333333-3333-4333-8333-333333333333'


def imported_modules() -> set[str]:
    tree = ast.parse(MODULE_PATH.read_text(encoding='utf-8'))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add('<relative>')
            elif node.module:
                names.add(node.module.split('.')[0])
    return names


# --- the standard library only ---------------------------------------


def test_readme_render_imports_nothing_but_the_standard_library():
    modules = imported_modules()
    assert modules, 'no imports found; the scan is broken'
    assert 'fleet_usage' not in modules
    assert '<relative>' not in modules
    third_party = modules - set(sys.stdlib_module_names)
    assert third_party == set()


def test_readme_render_can_run_outside_the_package(tmp_path):
    copied = tmp_path / 'render_readme.py'
    copied.write_text(
        MODULE_PATH.read_text(encoding='utf-8'), encoding='utf-8'
    )
    namespace: dict[str, object] = {'__name__': 'render_readme'}
    exec(compile(copied.read_text(), str(copied), 'exec'), namespace)
    assert callable(namespace['render_readme'])


# --- content ---------------------------------------------------------


@pytest.fixture
def document() -> str:
    return readme_render.render_readme(FIXTURES, NOW)


def test_document_structure(document):
    assert document.startswith('# Fleet usage\n')
    assert 'Generated 2026-09-08T06:00:00Z from 4 registered machine(s).' in (
        document
    )
    assert '## Machines' in document
    assert '## Totals' in document
    assert '## Models this month (2026-09)' in document
    assert '## Daily cost, last 30 days' in document
    assert document.endswith('\n')


def machine_row(document, label):
    prefix = f'| {readme_render._escape(label)} |'
    rows = [line for line in document.splitlines() if line.startswith(prefix)]
    assert len(rows) == 1, f'{label} not found once'
    return rows[0]


def test_machine_table_reports_freshness_errors_and_anomalies(document):
    fedora = machine_row(document, 'fedora-mobile')
    assert '| stale |' in fedora
    assert '2026-09-05T12:00:00Z' in fedora
    assert fedora.strip().endswith('| none | 0 |')

    assert '| never |' in machine_row(document, 'travel-laptop')

    workstation = machine_row(document, 'workstation')
    assert 'ccusage exited with code 1' in workstation
    assert 'codex' in workstation

    attic = machine_row(document, 'nuc | attic')
    assert '2026-09-01T06:00:00Z' in attic
    assert attic.strip().endswith('| 1 |')


def test_untrusted_labels_are_escaped(document):
    assert r'nuc \| attic' in document
    assert '| nuc | attic |' not in document


def test_model_table_lists_every_model(document):
    assert '| claude-fable-5-1 |' in document
    assert '| claude-sonnet-4-6 |' in document
    assert '| gpt-5-codex |' in document


def test_chart_is_mermaid_and_skips_unpriced_days(document):
    assert '```mermaid' in document
    assert 'xychart-beta' in document
    assert '"2026-09-02"' in document
    assert '"2026-09-01"' not in document
    assert 'omitted from the chart' in document


def test_chart_falls_back_when_nothing_is_priced(tmp_path):
    machines = tmp_path / 'machines'
    machines.mkdir()
    (tmp_path / 'fleet.toml').write_text(
        (FIXTURES / 'fleet.toml').read_text(encoding='utf-8'), encoding='utf-8'
    )
    (machines / f'{M3}.json').write_text(
        (FIXTURES / 'machines' / f'{M3}.json').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    document = readme_render.render_readme(tmp_path, NOW)
    assert '```mermaid' not in document
    assert 'no chart is shown' in document


def test_only_manifest_machines_are_reported(tmp_path):
    stray = '99999999-9999-4999-8999-999999999999'
    machines = tmp_path / 'machines'
    machines.mkdir()
    (tmp_path / 'fleet.toml').write_text(
        (FIXTURES / 'fleet.toml').read_text(encoding='utf-8'), encoding='utf-8'
    )
    for source in (FIXTURES / 'machines').iterdir():
        (machines / source.name).write_text(
            source.read_text(encoding='utf-8'), encoding='utf-8'
        )
    (machines / f'{stray}.json').write_text(
        (FIXTURES / 'machines' / f'{M3}.json').read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    document = readme_render.render_readme(tmp_path, NOW)
    assert stray not in document
    assert readme_render.render_readme(FIXTURES, NOW) == document


# --- parity with fleet_usage.reporting -------------------------------


def test_totals_match_reporting():
    machines = readme_render.load_fleet(FIXTURES)
    mine = readme_render.totals_for(machines, *MONTH)
    theirs = reporting.fleet_totals(
        load_fleet(), since=MONTH[0], until=MONTH[1]
    )
    assert mine.input_tokens == theirs.input_tokens
    assert mine.output_tokens == theirs.output_tokens
    assert mine.cache_create_tokens == theirs.cache_create_tokens
    assert mine.cache_read_tokens == theirs.cache_read_tokens
    assert mine.total_tokens == theirs.total_tokens
    assert mine.record_count == theirs.record_count
    assert mine.cost_usd == theirs.cost_usd
    assert mine.cost_known == theirs.cost_known
    assert mine.unknown_cost_records == theirs.unknown_cost_records


def test_model_totals_match_reporting():
    machines = readme_render.load_fleet(FIXTURES)
    mine = readme_render.model_totals(machines, *MONTH)
    theirs = reporting.aggregate(
        load_fleet(), by='model', since=MONTH[0], until=MONTH[1]
    )
    assert [name for name, _ in mine] == [row.key for row in theirs]
    for (_name, totals), row in zip(mine, theirs, strict=True):
        assert totals.total_tokens == row.total_tokens
        assert totals.cost_usd == row.cost_usd
        assert totals.cost_known == row.cost_known
        assert totals.record_count == row.record_count


def test_daily_costs_match_reporting():
    machines = readme_render.load_fleet(FIXTURES)
    mine = readme_render.daily_costs(machines, *MONTH)
    theirs = reporting.aggregate(
        load_fleet(), by='day', since=MONTH[0], until=MONTH[1]
    )
    assert [day.isoformat() for day, _ in mine] == [row.key for row in theirs]
    for (_day, totals), row in zip(mine, theirs, strict=True):
        assert totals.cost_usd == row.cost_usd
        assert totals.cost_known == row.cost_known


def test_freshness_matches_reporting():
    machines = readme_render.load_fleet(FIXTURES)
    document = readme_render.render_readme(FIXTURES, NOW)
    statuses = reporting.machine_status(load_fleet(), NOW, 3)
    assert len(statuses) == len(machines)
    for status in statuses:
        assert f'| {status.freshness} |' in machine_row(document, status.label)


# --- command line ----------------------------------------------------


def test_main_writes_the_document(tmp_path):
    output = tmp_path / 'README.md'
    code = readme_render.main(
        [
            '--data-dir',
            str(FIXTURES),
            '--output',
            str(output),
            '--now',
            '2026-09-08T06:00:00Z',
        ]
    )
    assert code == 0
    assert output.read_text(encoding='utf-8') == readme_render.render_readme(
        FIXTURES, NOW
    )


def test_main_rejects_an_unparsable_now(tmp_path, capsys):
    code = readme_render.main(
        [
            '--data-dir',
            str(FIXTURES),
            '--output',
            str(tmp_path / 'R.md'),
            '--now',
            'yesterday',
        ]
    )
    assert code == 1
    assert 'cannot parse --now' in capsys.readouterr().err


def test_main_reports_a_missing_manifest(tmp_path, capsys):
    code = readme_render.main(
        ['--data-dir', str(tmp_path), '--output', str(tmp_path / 'R.md')]
    )
    assert code == 1
    assert 'error:' in capsys.readouterr().err
