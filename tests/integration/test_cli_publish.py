"""``fleet-usage publish`` end to end against a mocked GitHub."""

import base64
import datetime as dt
import json
import time
from decimal import Decimal

import pytest

from fleet_usage import publisher
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
NOW = dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC)

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

[sync]
interval_minutes = 60
"""


def make_snapshot() -> Snapshot:
    agents = {
        'claude': AgentSnapshot(
            status='ok',
            days=[
                DayRecord(
                    date=dt.date(2026, 9, 7),
                    input_tokens=100,
                    output_tokens=20,
                    cost_usd=Decimal('1.2345'),
                    cost_status='estimated',
                )
            ],
        )
    }
    return Snapshot(
        machine_id='m1',
        label='box',
        collected_at=NOW,
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )


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


@pytest.fixture
def collected(monkeypatch) -> Snapshot:
    document = make_snapshot()

    def _collect(settings, *, now):
        return document

    monkeypatch.setattr(publisher, 'collect_snapshot', _collect)
    return document


def contents_payload(content: bytes, sha: str = 'blob-sha') -> dict:
    return {
        'sha': sha,
        'encoding': 'base64',
        'content': base64.b64encode(content).decode('ascii'),
    }


def test_dry_run_prints_a_summary_and_writes_nothing(
    invoke, configured, collected, httpx_mock
):
    result = invoke('publish', '--dry-run')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'claude' in result.output
    assert '2026-09-07' in result.output
    assert httpx_mock.get_requests() == []
    assert not configured.due_marker.exists()
    assert list(configured.spool_dir.glob('*.json')) == []


def test_publish_uploads_the_snapshot_and_the_ledger(
    invoke, configured, collected, httpx_mock
):
    remote = snapshot_remote_path(collected)
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{remote}',
        method='PUT',
        json={'content': {'sha': 'snap-sha'}},
    )
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1',
        json={
            'truncated': False,
            'tree': [{'path': remote, 'type': 'blob', 'sha': 'x'}],
        },
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{remote}?ref=main',
        json=contents_payload(collected.to_pretty_json().encode('utf-8')),
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json',
        method='PUT',
        json={'content': {'sha': 'ledger-sha'}},
    )

    result = invoke('publish')

    assert result.exit_code == ExitCode.OK, result.output
    puts = [
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    ]
    assert [request.url.path.split('/contents/')[-1] for request in puts] == [
        remote,
        'machines/m1.json',
    ]
    encoded = json.loads(puts[1].content)['content']
    ledger = json.loads(base64.b64decode(encoded))
    assert ledger['applied_through'] == remote
    assert ledger['agents']['claude']['days'][0]['input_tokens'] == 100
    assert configured.due_marker.exists()
    assert list(configured.spool_dir.glob('*.json')) == []


def test_a_bad_token_exits_six(invoke, configured, collected, httpx_mock):
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json?ref=main',
        status_code=401,
        json={'message': 'Bad credentials'},
    )
    result = invoke('publish')
    assert result.exit_code == ExitCode.AUTH_FAILURE, result.output
    assert 'Bad credentials' in result.output


def test_a_network_failure_keeps_the_spool_and_exits_four(
    invoke, configured, collected, httpx_mock
):
    remote = snapshot_remote_path(collected)
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{remote}',
        method='PUT',
        status_code=500,
        json={'message': 'oops'},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{remote}?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1',
        json={'truncated': False, 'tree': []},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json',
        method='PUT',
        json={'content': {'sha': 'ledger-sha'}},
    )

    result = invoke('publish')

    assert result.exit_code == ExitCode.SPOOLED_NOT_UPLOADED, result.output
    assert '1 snapshot' in result.output
    assert len(list(configured.spool_dir.glob('*.json'))) == 1


def test_if_due_does_nothing_right_after_a_run(
    invoke, configured, collected, httpx_mock, monkeypatch
):
    publisher.write_due_marker(configured.due_marker, dt.datetime.now(dt.UTC))
    result = invoke('publish', '--if-due')
    assert result.exit_code == ExitCode.OK, result.output
    assert httpx_mock.get_requests() == []


def test_a_viewer_only_install_exits_two(invoke, app_paths, monkeypatch):
    app_paths.config_dir.mkdir(parents=True, exist_ok=True)
    app_paths.settings_file.write_text(
        "schema_version = 1\n\n[github]\nrepository = 'octo/data'\n",
        encoding='utf-8',
    )
    monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', 'secret-token')
    result = invoke('publish')
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'viewer-only' in result.output


def test_a_missing_settings_file_exits_two(invoke, app_paths):
    result = invoke('publish')
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output


def test_an_unwritable_spool_is_reported_without_a_traceback(
    invoke, configured, collected, httpx_mock
):
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m1.json?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )
    configured.spool_dir.mkdir(parents=True, exist_ok=True)
    configured.spool_dir.chmod(0o500)
    try:
        result = invoke('publish')
    finally:
        configured.spool_dir.chmod(0o700)

    assert result.exit_code == ExitCode.ERROR, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert str(configured.spool_dir) in result.output
    assert 'cannot write' in result.output
