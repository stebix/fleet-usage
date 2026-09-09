"""``fleet-usage show`` end to end, with a fake transport."""

import csv
import io
import json
from pathlib import Path

import pytest

from fleet_usage.commands import show as show_module
from fleet_usage.exit_codes import ExitCode
from fleet_usage.github import GitHubError, NotFoundError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'ledgers'
M1 = '11111111-1111-4111-8111-111111111111'
M2 = '22222222-2222-4222-8222-222222222222'
M3 = '33333333-3333-4333-8333-333333333333'
M4 = '44444444-4444-4444-8444-444444444444'
SETTINGS = """schema_version = 1

[machine]
id = '{machine_id}'
label = 'fedora-mobile'

[github]
repository = 'octo/fleet-usage-data'
branch = 'main'
token_env = 'FLEET_USAGE_GITHUB_TOKEN'

[report]
default_period = '{period}'
stale_after_hours = 3
"""


class FakeClient:
    def __init__(self, files=None, errors=None):
        self.files = dict(files or {})
        self.errors = dict(errors or {})
        self.calls: list[str] = []

    def get_file(self, path):
        self.calls.append(path)
        if path in self.errors:
            raise self.errors[path]
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path], f'sha-{path}'

    def list_tree(self, prefix):
        self.calls.append(f'tree:{prefix}')
        return [path for path in sorted(self.files) if path.startswith(prefix)]


def fixture_files():
    files = {'fleet.toml': (FIXTURES / 'fleet.toml').read_bytes()}
    for machine_id in (M1, M2, M3):
        path = FIXTURES / 'machines' / f'{machine_id}.json'
        files[f'machines/{machine_id}.json'] = path.read_bytes()
    return files


@pytest.fixture
def configure(app_paths, monkeypatch):
    def _configure(period='month', client=None, token='ghp_test'):
        app_paths.config_dir.mkdir(parents=True, exist_ok=True)
        app_paths.settings_file.write_text(
            SETTINGS.format(machine_id=M1, period=period), encoding='utf-8'
        )
        if token is not None:
            monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', token)
        fake = client if client is not None else FakeClient(fixture_files())
        monkeypatch.setattr(show_module, 'build_client', lambda settings: fake)
        return fake

    return _configure


def payload_of(result):
    return json.loads(result.output)


# --- formats ---------------------------------------------------------


def test_table_report(invoke, configure):
    configure()
    result = invoke('show', '--since', '2026-09-01', '--until', '2026-09-05')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'fleet status' in result.output
    assert '4 expected' in result.output
    assert 'fedora-mobile' in result.output
    assert 'travel-laptop: never' in result.output
    assert '12,532' in result.output


def test_json_report(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--by',
        'agent',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'json',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert '\x1b[' not in result.output
    payload = payload_of(result)
    assert payload['group_by'] == 'agent'
    assert [row['key'] for row in payload['rows']] == ['claude', 'codex']
    assert payload['rows'][0]['cost_usd'] == '9.3000'
    assert payload['rows'][0]['cost_known'] is False
    assert payload['totals']['record_count'] == 8
    assert payload['status']['expected'] == 4
    assert payload['status']['never'] == 1
    assert len(payload['machines']) == 4


def test_csv_report(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--by',
        'model',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'csv',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert '\x1b[' not in result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    assert rows[0][0] == 'key'
    assert [row[0] for row in rows[1:] if row] == [
        'claude-fable-5-1',
        'claude-sonnet-4-6',
        'gpt-5-codex',
    ]


def test_the_table_breaks_down_by_model_by_default(
    invoke, configure, monkeypatch
):
    configure()
    monkeypatch.setenv('COLUMNS', '120')
    result = invoke('show', '--since', '2026-09-01', '--until', '2026-09-05')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'fedora-mobile' in result.output
    assert '├─ claude-fable-5-1' in result.output
    assert '└─ gpt-5-codex' in result.output
    assert '>= 8.75' in result.output  # the fleet total for that model
    assert '3.6000' not in result.output  # cropped to cents


def test_breakdown_spelled_out_matches_the_default(
    invoke, configure, monkeypatch
):
    configure()
    monkeypatch.setenv('COLUMNS', '120')
    args = ('show', '--since', '2026-09-01', '--until', '2026-09-05')
    assert invoke(*args, '--breakdown').output == invoke(*args).output


def test_flat_drops_the_model_sub_rows(invoke, configure, monkeypatch):
    configure()
    monkeypatch.setenv('COLUMNS', '120')
    result = invoke(
        'show', '--flat', '--since', '2026-09-01', '--until', '2026-09-05'
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert 'fedora-mobile' in result.output
    assert 'claude-fable-5-1' not in result.output
    assert '12,532' in result.output


def test_flat_drops_the_models_from_json(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--flat',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'json',
    )
    assert result.exit_code == ExitCode.OK, result.output
    payload = payload_of(result)
    assert all('models' not in row for row in payload['rows'])
    assert 'models' not in payload['totals']


def test_flat_drops_the_model_column_from_csv(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--flat',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'csv',
    )
    assert result.exit_code == ExitCode.OK, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    assert rows[0] == list(show_module.reporting.CSV_COLUMNS)
    assert [row[0] for row in rows[1:] if row] == [M1, M3, M2]


def test_table_costs_are_cropped_to_cents(invoke, configure, monkeypatch):
    configure()
    monkeypatch.setenv('COLUMNS', '120')
    result = invoke('show', '--since', '2026-09-01', '--until', '2026-09-05')
    assert result.exit_code == ExitCode.OK, result.output
    assert '5.85' in result.output
    assert '5.8500' not in result.output
    assert '>= 9.45' in result.output


def test_json_nests_models_by_default(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--by',
        'agent',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'json',
    )
    assert result.exit_code == ExitCode.OK, result.output
    payload = payload_of(result)
    claude = payload['rows'][0]
    assert claude['key'] == 'claude'
    assert [model['key'] for model in claude['models']] == [
        'claude-fable-5-1',
        'claude-sonnet-4-6',
    ]
    assert payload['totals']['models'][0]['cost_usd'] == '8.7500'


def test_csv_carries_a_model_column_by_default(invoke, configure):
    configure()
    result = invoke(
        'show',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'csv',
    )
    assert result.exit_code == ExitCode.OK, result.output
    rows = list(csv.reader(io.StringIO(result.output)))
    assert rows[0][-1] == 'model'
    assert rows[1][-1] == ''
    assert rows[2][-1] == 'claude-fable-5-1'
    assert rows[2][0] == M1


def test_breakdown_is_ignored_when_grouping_by_model(invoke, configure):
    configure()
    args = (
        'show',
        '--by',
        'model',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'csv',
    )
    detailed = invoke(*args)
    assert detailed.exit_code == ExitCode.OK, detailed.output
    assert invoke(*args, '--flat').output == detailed.output
    rows = list(csv.reader(io.StringIO(detailed.output)))
    assert rows[0] == list(show_module.reporting.CSV_COLUMNS)


@pytest.mark.parametrize('by', ['machine', 'agent', 'model', 'day', 'month'])
def test_every_grouping_works(invoke, configure, by):
    configure()
    result = invoke(
        'show',
        '--by',
        by,
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'json',
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert payload_of(result)['group_by'] == by


# --- period defaulting -----------------------------------------------


def test_default_period_month_bounds_the_report(invoke, configure):
    configure(period='month')
    result = invoke('show', '--format', 'json')
    assert result.exit_code == ExitCode.OK, result.output
    payload = payload_of(result)
    assert payload['since'].endswith('-01')
    assert payload['until'] is not None


def test_default_period_all_leaves_the_report_open(invoke, configure):
    configure(period='all')
    result = invoke('show', '--format', 'json')
    assert result.exit_code == ExitCode.OK, result.output
    payload = payload_of(result)
    assert payload['since'] is None
    assert payload['until'] is None
    assert payload['totals']['record_count'] == 8


def test_an_explicit_bound_wins_over_the_default(invoke, configure):
    configure(period='month')
    result = invoke('show', '--since', '2026-09-03', '--format', 'json')
    payload = payload_of(result)
    assert payload['since'] == '2026-09-03'
    assert payload['until'] is None
    assert payload['totals']['record_count'] == 2


# --- offline and the cache -------------------------------------------


def test_offline_uses_the_cache_and_no_network(invoke, configure):
    client = configure()
    assert invoke('show', '--format', 'json').exit_code == ExitCode.OK
    client.calls.clear()

    class Exploding(FakeClient):
        def get_file(self, path):
            raise AssertionError('offline must not fetch')

    configure(client=Exploding())
    result = invoke(
        'show',
        '--offline',
        '--since',
        '2026-09-01',
        '--until',
        '2026-09-05',
        '--format',
        'json',
    )
    assert result.exit_code == ExitCode.OK, result.output
    payload = payload_of(result)
    assert payload['status']['offline'] is True
    assert payload['status']['from_cache'] is True
    assert payload['totals']['record_count'] == 8


def test_offline_without_a_cache_fails_with_the_network_code(
    invoke, configure
):
    configure()
    result = invoke('show', '--offline')
    assert result.exit_code == ExitCode.SPOOLED_NOT_UPLOADED, result.output
    assert 'no cached copy' in result.output


def test_network_failure_without_a_cache_fails(invoke, configure):
    configure(
        client=FakeClient(errors={'fleet.toml': GitHubError('no route')})
    )
    result = invoke('show')
    assert result.exit_code == ExitCode.SPOOLED_NOT_UPLOADED, result.output


def test_invalid_remote_ledger_fails_with_the_remote_code(invoke, configure):
    files = fixture_files()
    files[f'machines/{M2}.json'] = b'{"schema_version": 1'
    configure(client=FakeClient(files))
    result = invoke('show')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert 'invalid' in result.output


def test_missing_token_fails_with_the_auth_code(
    invoke, app_paths, monkeypatch
):
    app_paths.config_dir.mkdir(parents=True, exist_ok=True)
    app_paths.settings_file.write_text(
        SETTINGS.format(machine_id=M1, period='month'), encoding='utf-8'
    )
    monkeypatch.delenv('FLEET_USAGE_GITHUB_TOKEN', raising=False)
    result = invoke('show')
    assert result.exit_code == ExitCode.AUTH_FAILURE, result.output
    assert '--offline' in result.output


def test_missing_settings_is_a_config_error(invoke):
    result = invoke('show')
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output


# --- --web -----------------------------------------------------------


def test_web_prints_the_url_and_does_nothing_else(
    invoke, configure, monkeypatch
):
    client = configure()
    opened: list[str] = []
    monkeypatch.setattr(
        show_module.webbrowser, 'open', lambda url: opened.append(url)
    )
    result = invoke('show', '--web')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'https://github.com/octo/fleet-usage-data' in result.output
    assert opened == ['https://github.com/octo/fleet-usage-data']
    assert client.calls == []


def test_web_survives_a_machine_without_a_browser(
    invoke, configure, monkeypatch
):
    configure()

    def boom(url):
        raise show_module.webbrowser.Error('no browser')

    monkeypatch.setattr(show_module.webbrowser, 'open', boom)
    result = invoke('show', '--web')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'https://github.com/' in result.output
