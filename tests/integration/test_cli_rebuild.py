"""``fleet-usage rebuild`` end to end against a mocked GitHub.

The command folds the immutable snapshots back into a fresh ledger with
the real :func:`fleet_usage.ledger.rebuild_ledger`, so this exercises the
signature of that function as well as the transport.
"""

import base64
import datetime as dt
import json
import time
from decimal import Decimal

import pytest

from fleet_usage.exit_codes import ExitCode
from fleet_usage.models import (
    AgentSnapshot,
    CollectorInfo,
    DayRecord,
    Snapshot,
)
from fleet_usage.snapshot import agents_hash, snapshot_remote_path

pytestmark = pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False,
    can_send_already_matched_responses=True,
)

BASE = 'https://api.github.com'
CONTENTS = f'{BASE}/repos/octo/data/contents'
TREES = f'{BASE}/repos/octo/data/git/trees'

SETTINGS = """schema_version = 1

[machine]
id = 'm1'
label = 'box'

[github]
repository = 'octo/data'
branch = 'main'
token_env = 'FLEET_USAGE_GITHUB_TOKEN'
api_base_url = 'https://api.github.com'

[collector]
command = ['ccusage']
version = '20.0.20'
agents = ['claude']
timezone = 'Europe/Berlin'

[ledger]
freeze_window_days = 5
"""

MANIFEST = b"""schema_version = 1
timezone = 'Europe/Berlin'
freeze_window_days = 5

[[machines]]
id = 'm1'
label = 'box'
"""


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Keep the bounded retries instant."""
    monkeypatch.setattr(time, 'sleep', lambda _seconds: None)


@pytest.fixture
def configured(app_paths, monkeypatch):
    app_paths.config_dir.mkdir(parents=True, exist_ok=True)
    app_paths.settings_file.write_text(SETTINGS, encoding='utf-8')
    monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', 'secret-token')
    return app_paths


def contents_payload(content: bytes, sha: str = 'blob-sha') -> dict:
    return {
        'sha': sha,
        'encoding': 'base64',
        'content': base64.b64encode(content).decode('ascii'),
    }


def make_snapshot(hour: int, tokens: int) -> Snapshot:
    agents = {
        'claude': AgentSnapshot(
            status='ok',
            days=[
                DayRecord(
                    date=dt.date(2026, 9, 7),
                    input_tokens=tokens,
                    cost_usd=Decimal('0.5'),
                    cost_status='estimated',
                )
            ],
        )
    }
    return Snapshot(
        machine_id='m1',
        label='box',
        collected_at=dt.datetime(2026, 9, 8, hour, tzinfo=dt.UTC),
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )


def register_repository(httpx_mock, snapshots, ledger=None):
    if ledger is None:
        httpx_mock.add_response(
            url=f'{CONTENTS}/machines/m1.json?ref=main',
            status_code=404,
            json={'message': 'Not Found'},
        )
    else:
        httpx_mock.add_response(
            url=f'{CONTENTS}/machines/m1.json?ref=main',
            json=contents_payload(ledger, sha='ledger-sha'),
        )
    httpx_mock.add_response(
        url=f'{CONTENTS}/fleet.toml?ref=main', json=contents_payload(MANIFEST)
    )
    paths = [snapshot_remote_path(document) for document in snapshots]
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1',
        json={
            'truncated': False,
            'tree': [
                {'path': path, 'type': 'blob', 'sha': 'x'} for path in paths
            ],
        },
    )
    for path, document in zip(paths, snapshots, strict=True):
        httpx_mock.add_response(
            url=f'{CONTENTS}/{path}?ref=main',
            json=contents_payload(document.to_pretty_json().encode('utf-8')),
        )
    return paths


def test_rebuild_folds_every_snapshot_into_a_fresh_ledger(
    invoke, configured, httpx_mock
):
    snapshots = [make_snapshot(10, 100), make_snapshot(12, 150)]
    paths = register_repository(httpx_mock, snapshots)
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json',
        method='PUT',
        json={'content': {'sha': 'ledger-sha'}},
    )

    result = invoke('rebuild')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'snapshots applied: 2' in result.output
    put = next(
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    )
    ledger = json.loads(base64.b64decode(json.loads(put.content)['content']))
    assert ledger['machine_id'] == 'm1'
    assert ledger['label'] == 'box'
    assert ledger['applied_through'] == paths[-1]
    assert ledger['merge_policy'] == {
        'version': 1,
        'freeze_window_days': 5,
        'timezone': 'Europe/Berlin',
    }
    day = ledger['agents']['claude']['days'][0]
    assert day['input_tokens'] == 150
    assert json.loads(put.content).get('sha') is None


def test_rebuild_replaces_an_existing_ledger(invoke, configured, httpx_mock):
    stale = json.dumps(
        {
            'schema_version': 1,
            'machine_id': 'm1',
            'label': 'box',
            'merge_policy': {
                'version': 1,
                'freeze_window_days': 1,
                'timezone': 'UTC',
            },
            'agents': {},
        }
    ).encode('utf-8')
    register_repository(httpx_mock, [make_snapshot(10, 100)], ledger=stale)
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json',
        method='PUT',
        json={'content': {'sha': 'new-sha'}},
    )

    result = invoke('rebuild')

    assert result.exit_code == ExitCode.OK, result.output
    put = next(
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    )
    assert json.loads(put.content)['sha'] == 'ledger-sha'


def test_rebuild_dry_run_writes_nothing(invoke, configured, httpx_mock):
    register_repository(httpx_mock, [make_snapshot(10, 100)])

    result = invoke('rebuild', '--dry-run')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'dry run' in result.output
    assert [
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    ] == []


def test_rebuild_without_snapshots_is_refused(invoke, configured, httpx_mock):
    register_repository(httpx_mock, [])
    result = invoke('rebuild')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
