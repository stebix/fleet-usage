"""The publish sequence: ordering, recovery and exit codes."""

import datetime as dt
import json
from decimal import Decimal

import filelock
import pytest

from fleet_usage import publisher, spool
from fleet_usage.collector import CollectorError
from fleet_usage.config import Settings
from fleet_usage.exit_codes import ExitCode
from fleet_usage.github import (
    AuthError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RemoteConflictError,
)
from fleet_usage.models import (
    AgentSnapshot,
    CollectorInfo,
    DayRecord,
    Ledger,
    LedgerAgent,
    MergePolicy,
    Snapshot,
)
from fleet_usage.snapshot import agents_hash, snapshot_remote_path

TOKEN_ENV = 'FLEET_USAGE_GITHUB_TOKEN'
NOW = dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC)


# -- doubles ---------------------------------------------------------


class FakeGitHub:
    """In-memory stand-in for :class:`fleet_usage.github.GitHubClient`."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.puts = []
        self.gets = []
        self.trees = []
        self.put_effects = {}
        self.closed = False

    def get_file(self, path):
        self.gets.append(path)
        if path not in self.files:
            raise NotFoundError(f'not found ({path})', path=path)
        return self.files[path], f'sha-{path}'

    def put_file(self, path, content, message, sha=None):
        queue = self.put_effects.get(path)
        effect = queue.pop(0) if queue else None
        if effect == 'lost-response':
            self.files[path] = content
            raise NetworkError('connection reset after the write')
        if isinstance(effect, Exception):
            raise effect
        self.files[path] = content
        self.puts.append((path, content, sha))
        return f'sha-{path}'

    def list_tree(self, prefix):
        self.trees.append(prefix)
        marker = f'{prefix.rstrip("/")}/'
        return sorted(p for p in self.files if p.startswith(marker))

    def close(self):
        self.closed = True


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def settings(app_paths, monkeypatch) -> Settings:
    monkeypatch.setenv(TOKEN_ENV, 'secret-token')
    return Settings.model_validate(
        {
            'schema_version': 1,
            'machine': {'id': 'm1', 'label': 'box'},
            'github': {'repository': 'octo/data', 'branch': 'main'},
            'collector': {
                'command': ['ccusage'],
                'version': '20.0.20',
                'agents': ['claude', 'codex'],
                'timezone': 'Europe/Berlin',
            },
            'ledger': {'freeze_window_days': 5},
            'sync': {'interval_minutes': 60},
        }
    )


def make_snapshot(
    *,
    collected_at=NOW,
    tokens=10,
    machine_id='m1',
    label='box',
) -> Snapshot:
    agents = {
        'claude': AgentSnapshot(
            status='ok',
            days=[
                DayRecord(
                    date=dt.date(2026, 9, 7),
                    input_tokens=tokens,
                    output_tokens=2,
                    cost_usd=Decimal('1.2345'),
                    cost_status='estimated',
                )
            ],
        ),
        'codex': AgentSnapshot(status='ok', days=[]),
    }
    return Snapshot(
        machine_id=machine_id,
        label=label,
        collected_at=collected_at,
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )


def fake_collect(document):
    def _collect(settings, *, now):
        return document

    return _collect


def failing_collect(message='ccusage exploded'):
    def _collect(settings, *, now):
        raise CollectorError(message)

    return _collect


def run(settings, app_paths, client, sleeps, **kwargs):
    return publisher.publish(
        settings,
        app_paths,
        now=kwargs.pop('now', NOW),
        client_factory=lambda _settings, _token: client,
        sleep=sleeps.append,
        **kwargs,
    )


def stored_ledger(client) -> Ledger:
    return Ledger.model_validate_json(client.files['machines/m1.json'])


# -- happy paths -----------------------------------------------------


def test_spools_uploads_and_writes_the_ledger(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps)

    remote = snapshot_remote_path(document)
    assert result.exit_code is ExitCode.OK
    assert result.uploaded == (remote,)
    assert result.applied == (remote,)
    assert result.ledger_written is True
    assert spool.list_spool(app_paths.spool_dir) == []
    ledger = stored_ledger(client)
    assert ledger.applied_through == remote
    assert ledger.last_agents_hash == document.agents_hash
    assert ledger.last_run_at == NOW
    assert [day.date for day in ledger.agents['claude'].days] == [
        dt.date(2026, 9, 7)
    ]
    assert client.closed is True


def test_identical_collection_is_not_spooled(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    ledger = Ledger(
        machine_id='m1',
        label='box',
        merge_policy=MergePolicy(freeze_window_days=5, timezone='UTC'),
        last_agents_hash=document.agents_hash,
    )
    client = FakeGitHub(
        {'machines/m1.json': ledger.to_pretty_json().encode('utf-8')}
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.deduplicated is True
    assert result.spooled is False
    assert result.uploaded == ()
    assert spool.list_spool(app_paths.spool_dir) == []
    assert [path for path, _c, _s in client.puts] == ['machines/m1.json']


def test_the_merge_policy_follows_the_local_settings(
    settings, app_paths, monkeypatch, sleeps
):
    ledger = Ledger(
        machine_id='m1',
        label='old-label',
        merge_policy=MergePolicy(freeze_window_days=1, timezone='UTC'),
    )
    client = FakeGitHub(
        {'machines/m1.json': ledger.to_pretty_json().encode('utf-8')}
    )
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    run(settings, app_paths, client, sleeps)

    stored = stored_ledger(client)
    assert stored.label == 'box'
    assert stored.merge_policy.freeze_window_days == 5
    assert stored.merge_policy.timezone == 'Europe/Berlin'


def test_uploads_are_throttled(settings, app_paths, monkeypatch, sleeps):
    early = make_snapshot(collected_at=NOW - dt.timedelta(hours=1), tokens=5)
    spool.write_snapshot(app_paths.spool_dir, early)
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps)

    assert len(result.uploaded) == 2
    assert sleeps == [publisher.MIN_UPLOAD_INTERVAL_SECONDS]
    assert result.uploaded == tuple(sorted(result.uploaded))


# -- recovery --------------------------------------------------------


def test_a_lost_upload_response_is_acknowledged(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub()
    client.put_effects[remote] = ['lost-response']
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.uploaded == (remote,)
    assert spool.list_spool(app_paths.spool_dir) == []
    assert remote in client.gets


def test_a_snapshot_uploaded_before_a_crash_is_applied_later(
    settings, app_paths, monkeypatch, sleeps
):
    orphan = make_snapshot(collected_at=NOW - dt.timedelta(hours=1), tokens=7)
    orphan_path = snapshot_remote_path(orphan)
    ledger = Ledger(
        machine_id='m1',
        label='box',
        merge_policy=MergePolicy(freeze_window_days=5, timezone='UTC'),
        last_agents_hash=orphan.agents_hash,
    )
    client = FakeGitHub(
        {
            orphan_path: orphan.to_pretty_json().encode('utf-8'),
            'machines/m1.json': ledger.to_pretty_json().encode('utf-8'),
        }
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(orphan))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.deduplicated is True
    assert result.applied == (orphan_path,)
    stored = stored_ledger(client)
    assert stored.applied_through == orphan_path
    assert stored.agents['claude'].days[0].input_tokens == 7


def test_snapshots_before_applied_through_are_not_downloaded_again(
    settings, app_paths, monkeypatch, sleeps
):
    old = make_snapshot(collected_at=NOW - dt.timedelta(days=1), tokens=3)
    old_path = snapshot_remote_path(old)
    ledger = Ledger(
        machine_id='m1',
        label='box',
        merge_policy=MergePolicy(freeze_window_days=5, timezone='UTC'),
        applied_through=old_path,
        last_agents_hash=old.agents_hash,
    )
    client = FakeGitHub(
        {
            old_path: old.to_pretty_json().encode('utf-8'),
            'machines/m1.json': ledger.to_pretty_json().encode('utf-8'),
        }
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(old))

    result = run(settings, app_paths, client, sleeps)

    assert result.applied == ()
    assert old_path not in client.gets


# -- failures --------------------------------------------------------


def test_collection_failure_still_drains_the_spool(
    settings, app_paths, monkeypatch, sleeps
):
    pending = make_snapshot(collected_at=NOW - dt.timedelta(hours=1))
    spool.write_snapshot(app_paths.spool_dir, pending)
    monkeypatch.setattr(publisher, 'collect_snapshot', failing_collect())
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.COLLECTION_FAILURE
    assert result.uploaded == (snapshot_remote_path(pending),)
    assert spool.list_spool(app_paths.spool_dir) == []
    ledger = stored_ledger(client)
    assert ledger.agents['claude'].last_error == 'ccusage exploded'
    assert ledger.agents['codex'].last_error == 'ccusage exploded'
    assert ledger.last_run_at == NOW


def test_a_network_failure_leaves_the_spool_and_exits_four(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub()
    client.put_effects[remote] = [NetworkError('no route to host')] * 4
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert result.pending == 1
    assert '1 snapshot' in result.message
    assert len(spool.list_spool(app_paths.spool_dir)) == 1


def test_a_different_remote_document_is_a_conflict(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    other = make_snapshot(tokens=999).to_pretty_json().encode('utf-8')
    client = FakeGitHub({remote: other})
    client.put_effects[remote] = [
        RemoteConflictError('422 already exists', path=remote)
    ]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.REMOTE_CONFLICT
    assert 'different content' in result.message
    assert len(spool.list_spool(app_paths.spool_dir)) == 1


def test_an_identical_remote_document_ends_the_upload(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub({remote: document.to_pretty_json().encode('utf-8')})
    client.put_effects[remote] = [
        RemoteConflictError('422 already exists', path=remote)
    ]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.uploaded == (remote,)
    assert spool.list_spool(app_paths.spool_dir) == []


def test_a_missing_remote_file_retries_the_upload(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub()
    client.put_effects[remote] = [NetworkError('reset')]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.uploaded == (remote,)
    assert publisher.VERIFY_BACKOFF_SECONDS in sleeps


def test_an_auth_failure_exits_six(settings, app_paths, monkeypatch, sleeps):
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub()
    client.put_effects[remote] = [AuthError('Bad credentials')]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.AUTH_FAILURE
    assert 'Bad credentials' in result.message
    assert len(spool.list_spool(app_paths.spool_dir)) == 1
    assert 'machines/m1.json' not in [path for path, _c, _s in client.puts]


def test_a_rate_limited_snapshot_upload_stays_spooled(
    settings, app_paths, monkeypatch, sleeps
):
    # A secondary rate limit answers with 403, the status code of a
    # permission failure, but the token is fine and the snapshot must
    # simply be retried on the next run.
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub()
    client.put_effects[remote] = [
        RateLimitError('secondary rate limit', status_code=403, path=remote)
        for _ in range(4)
    ]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert result.pending == 1
    assert result.uploaded == ()
    assert len(spool.list_spool(app_paths.spool_dir)) == 1
    # Nothing was applied, so the ledger cannot claim the snapshot.
    assert stored_ledger(client).applied_through is None


def test_a_rate_limited_ledger_write_is_not_an_auth_failure(
    settings, app_paths, monkeypatch, sleeps
):
    document = make_snapshot()
    client = FakeGitHub()
    client.put_effects['machines/m1.json'] = [
        RateLimitError('secondary rate limit', status_code=403)
        for _ in range(4)
    ]
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert result.ledger_written is False
    assert 'machines/m1.json' not in client.files
    assert result.uploaded == (snapshot_remote_path(document),)


def test_an_unreadable_remote_snapshot_is_skipped_not_fatal(
    settings, app_paths, monkeypatch, sleeps
):
    # A snapshot is immutable, so a document that cannot be parsed would
    # block every future run if it were treated as a conflict.
    garbage = 'snapshots/m1/2026-09/20260908T110000Z-00000000.json'
    document = make_snapshot()
    remote = snapshot_remote_path(document)
    client = FakeGitHub({garbage: b'{"nonsense": true}'})
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.applied == (remote,)
    assert result.skipped_remote == (garbage,)
    assert result.ledger_written is True
    assert garbage in result.message
    stored = stored_ledger(client)
    assert stored.applied_through == remote
    assert stored.agents['claude'].days[0].input_tokens == 10


def test_a_skipped_remote_snapshot_is_not_retried_forever(
    settings, app_paths, monkeypatch, sleeps
):
    garbage = 'snapshots/m1/2026-10/20261001T110000Z-00000000.json'
    document = make_snapshot()
    client = FakeGitHub({garbage: b'not json at all'})
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    run(settings, app_paths, client, sleeps)

    assert stored_ledger(client).applied_through == garbage
    before = client.gets.count(garbage)
    run(settings, app_paths, client, sleeps)
    assert client.gets.count(garbage) == before


def test_an_offline_run_spools_an_unchanged_collection_only_once(
    settings, app_paths, monkeypatch, sleeps
):
    class Offline(FakeGitHub):
        def put_file(self, path, content, message, sha=None):
            raise NetworkError('no route to host', path=path)

    client = Offline()
    document = make_snapshot()
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))
    first = run(settings, app_paths, client, sleeps)
    later = make_snapshot(collected_at=NOW + dt.timedelta(hours=1))
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(later))
    second = run(
        settings, app_paths, client, sleeps, now=NOW + dt.timedelta(hours=1)
    )

    assert first.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert second.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert second.spooled is False
    assert second.deduplicated is True
    assert len(spool.list_spool(app_paths.spool_dir)) == 1


def test_an_offline_run_spools_a_changed_collection(
    settings, app_paths, monkeypatch, sleeps
):
    class Offline(FakeGitHub):
        def put_file(self, path, content, message, sha=None):
            raise NetworkError('no route to host', path=path)

    client = Offline()
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )
    run(settings, app_paths, client, sleeps)
    changed = make_snapshot(
        collected_at=NOW + dt.timedelta(hours=1), tokens=99
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(changed))
    second = run(
        settings, app_paths, client, sleeps, now=NOW + dt.timedelta(hours=1)
    )

    assert second.spooled is True
    assert second.deduplicated is False
    assert len(spool.list_spool(app_paths.spool_dir)) == 2


def test_an_unreadable_remote_ledger_exits_five(
    settings, app_paths, monkeypatch, sleeps
):
    client = FakeGitHub({'machines/m1.json': b'{"schema_version": "no"}'})
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.REMOTE_CONFLICT
    assert 'not valid' in result.message


def test_a_ledger_conflict_is_retried(
    settings, app_paths, monkeypatch, sleeps
):
    client = FakeGitHub()
    client.put_effects['machines/m1.json'] = [
        RemoteConflictError('sha mismatch')
    ]
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert result.ledger_written is True
    assert client.gets.count('machines/m1.json') >= 2


def test_a_persistent_ledger_conflict_exits_five(
    settings, app_paths, monkeypatch, sleeps
):
    client = FakeGitHub()
    client.put_effects['machines/m1.json'] = [
        RemoteConflictError('sha mismatch') for _ in range(5)
    ]
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.REMOTE_CONFLICT
    assert result.ledger_written is False


def test_a_corrupt_spool_file_is_quarantined(
    settings, app_paths, monkeypatch, sleeps
):
    app_paths.spool_dir.mkdir(parents=True, exist_ok=True)
    broken = app_paths.spool_dir / '20260101T000000Z-deadbeef.json'
    broken.write_text('{"nonsense": true}', encoding='utf-8')
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    assert not broken.exists()
    assert (app_paths.spool_dir / f'{broken.name}.invalid').exists()


# -- guards ----------------------------------------------------------


def test_a_second_instance_exits_zero(
    settings, app_paths, monkeypatch, sleeps
):
    calls = []

    def _collect(settings, *, now):
        calls.append(now)
        return make_snapshot()

    monkeypatch.setattr(publisher, 'collect_snapshot', _collect)
    app_paths.state_dir.mkdir(parents=True, exist_ok=True)
    other = filelock.FileLock(str(app_paths.lock_file), timeout=0)
    other.acquire()
    try:
        result = run(settings, app_paths, FakeGitHub(), sleeps)
    finally:
        other.release()

    assert result.exit_code is ExitCode.OK
    assert result.skipped == 'locked'
    assert calls == []


def test_if_due_skips_a_recent_run(settings, app_paths, monkeypatch, sleeps):
    publisher.write_due_marker(
        app_paths.due_marker, NOW - dt.timedelta(minutes=5)
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', failing_collect())

    result = run(settings, app_paths, FakeGitHub(), sleeps, if_due=True)

    assert result.exit_code is ExitCode.OK
    assert result.skipped == 'not-due'


def test_if_due_runs_once_the_interval_has_passed(
    settings, app_paths, monkeypatch, sleeps
):
    publisher.write_due_marker(
        app_paths.due_marker, NOW - dt.timedelta(minutes=61)
    )
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    result = run(settings, app_paths, FakeGitHub(), sleeps, if_due=True)

    assert result.skipped is None
    assert result.exit_code is ExitCode.OK
    payload = json.loads(app_paths.due_marker.read_text(encoding='utf-8'))
    assert payload['last_run_at'] == '2026-09-08T13:00:04Z'


def test_if_due_tolerates_scheduler_jitter(app_paths):
    # A systemd timer with RandomizedDelaySec can fire 59 minutes after
    # the previous run; that run must not be skipped.
    publisher.write_due_marker(
        app_paths.due_marker, NOW - dt.timedelta(minutes=59)
    )
    assert publisher.is_due(app_paths.due_marker, NOW, 60) is True

    publisher.write_due_marker(
        app_paths.due_marker, NOW - dt.timedelta(minutes=54)
    )
    assert publisher.is_due(app_paths.due_marker, NOW, 60) is False


@pytest.mark.parametrize(
    ('interval', 'expected_minutes'),
    [(60, 55), (120, 115), (30, 25), (10, 7.5), (4, 3)],
)
def test_due_threshold_caps_the_slack(interval, expected_minutes):
    assert publisher.due_threshold(interval) == dt.timedelta(
        minutes=expected_minutes
    )


def test_a_damaged_marker_never_blocks_a_run(app_paths):
    app_paths.state_dir.mkdir(parents=True, exist_ok=True)
    app_paths.due_marker.write_text('not json', encoding='utf-8')
    assert publisher.is_due(app_paths.due_marker, NOW, 60) is True


def test_dry_run_writes_nothing(settings, app_paths, monkeypatch, sleeps):
    document = make_snapshot()
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps, dry_run=True)

    assert result.exit_code is ExitCode.OK
    assert result.snapshot is document
    assert client.files == {}
    assert client.puts == []
    assert spool.list_spool(app_paths.spool_dir) == []
    assert not app_paths.due_marker.exists()


def test_dry_run_reports_a_collection_failure(
    settings, app_paths, monkeypatch, sleeps
):
    monkeypatch.setattr(publisher, 'collect_snapshot', failing_collect())
    result = run(settings, app_paths, FakeGitHub(), sleeps, dry_run=True)
    assert result.exit_code is ExitCode.COLLECTION_FAILURE


def test_a_viewer_only_install_is_a_config_error(app_paths, sleeps):
    viewer = Settings.model_validate(
        {'schema_version': 1, 'github': {'repository': 'octo/data'}}
    )
    result = run(viewer, app_paths, FakeGitHub(), sleeps)
    assert result.exit_code is ExitCode.CONFIG_ERROR
    assert 'viewer-only' in result.message


def test_a_missing_token_is_a_config_error(
    settings, app_paths, monkeypatch, sleeps
):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )
    result = run(settings, app_paths, FakeGitHub(), sleeps)
    assert result.exit_code is ExitCode.CONFIG_ERROR
    assert TOKEN_ENV in result.message


# -- pure helpers ----------------------------------------------------


def test_exit_codes_are_ordered_by_severity():
    conflict = RemoteConflictError('conflict')
    auth = AuthError('auth')
    collection = CollectorError('collection')
    assert (
        publisher.exit_code_for(
            auth_error=auth,
            conflict=conflict,
            pending=3,
            collection_error=collection,
        )
        is ExitCode.AUTH_FAILURE
    )
    assert (
        publisher.exit_code_for(
            auth_error=None,
            conflict=conflict,
            pending=3,
            collection_error=collection,
        )
        is ExitCode.REMOTE_CONFLICT
    )
    assert (
        publisher.exit_code_for(
            auth_error=None,
            conflict=None,
            pending=3,
            collection_error=collection,
        )
        is ExitCode.SPOOLED_NOT_UPLOADED
    )
    assert (
        publisher.exit_code_for(
            auth_error=None,
            conflict=None,
            pending=0,
            collection_error=collection,
        )
        is ExitCode.COLLECTION_FAILURE
    )
    assert (
        publisher.exit_code_for(
            auth_error=None, conflict=None, pending=0, collection_error=None
        )
        is ExitCode.OK
    )


def test_summarize_counts_days_tokens_and_cost():
    document = make_snapshot()
    rows = {row.agent: row for row in publisher.summarize(document)}
    assert rows['claude'].days == 1
    assert rows['claude'].tokens == 12
    assert rows['claude'].cost_usd == Decimal('1.2345')
    assert rows['claude'].latest_date == dt.date(2026, 9, 7)
    assert rows['codex'].days == 0
    assert rows['codex'].latest_date is None


def test_summarize_marks_unknown_costs():
    agents = {
        'claude': AgentSnapshot(
            status='ok',
            days=[DayRecord(date=dt.date(2026, 9, 7), input_tokens=5)],
        )
    }
    document = Snapshot(
        machine_id='m1',
        label='box',
        collected_at=NOW,
        timezone='UTC',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )
    assert publisher.summarize(document)[0].unknown_costs == 1


def test_remote_paths():
    assert publisher.ledger_remote_path('m1') == 'machines/m1.json'
    assert publisher.snapshot_prefix('m1') == 'snapshots/m1'


def test_collect_snapshot_wires_the_collector(settings, monkeypatch):
    payload = {'daily': []}
    monkeypatch.setattr(publisher, 'run_collector', lambda collector: payload)
    monkeypatch.setattr(
        publisher,
        'parse_daily_by_agent',
        lambda document, agents: {
            name: AgentSnapshot(status='ok', days=[]) for name in agents
        },
    )
    document = publisher.collect_snapshot(settings, now=NOW)
    assert document.machine_id == 'm1'
    assert document.label == 'box'
    assert document.collected_at == NOW
    assert document.collector.version == '20.0.20'
    assert sorted(document.agents) == ['claude', 'codex']
    assert document.agents_hash == agents_hash(document.agents)


def test_error_agents_are_recorded_in_the_ledger(
    settings, app_paths, monkeypatch, sleeps
):
    agents = {
        'claude': AgentSnapshot(status='ok', days=[]),
        'codex': AgentSnapshot(status='error', message='no data directory'),
    }
    document = Snapshot(
        machine_id='m1',
        label='box',
        collected_at=NOW,
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))
    client = FakeGitHub()

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.OK
    ledger = stored_ledger(client)
    assert ledger.agents['codex'].last_error == 'no data directory'
    assert ledger.agents['claude'].last_error is None


def test_a_previous_error_is_cleared_on_success(
    settings, app_paths, monkeypatch, sleeps
):
    previous = Ledger(
        machine_id='m1',
        label='box',
        merge_policy=MergePolicy(freeze_window_days=5, timezone='UTC'),
        agents={'claude': LedgerAgent(last_error='boom')},
    )
    client = FakeGitHub(
        {'machines/m1.json': previous.to_pretty_json().encode('utf-8')}
    )
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    run(settings, app_paths, client, sleeps)

    assert stored_ledger(client).agents['claude'].last_error is None


def test_an_unreachable_ledger_fetch_still_spools_the_collection(
    settings, app_paths, monkeypatch, sleeps
):
    class Unreachable(FakeGitHub):
        def get_file(self, path):
            raise NetworkError('connection refused', path=path)

        def put_file(self, path, content, message, sha=None):
            raise NetworkError('connection refused', path=path)

    client = Unreachable()
    document = make_snapshot()
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(document))

    first = run(settings, app_paths, client, sleeps)
    later = make_snapshot(collected_at=NOW + dt.timedelta(hours=1))
    monkeypatch.setattr(publisher, 'collect_snapshot', fake_collect(later))
    second = run(
        settings, app_paths, client, sleeps, now=NOW + dt.timedelta(hours=1)
    )

    assert first.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert first.spooled is True
    assert first.pending == 1
    assert '1 snapshot' in first.message
    assert second.exit_code is ExitCode.SPOOLED_NOT_UPLOADED
    assert second.spooled is False
    assert second.deduplicated is True
    assert len(spool.list_spool(app_paths.spool_dir)) == 1


def test_a_rejected_token_on_the_ledger_fetch_still_spools(
    settings, app_paths, monkeypatch, sleeps
):
    class Rejected(FakeGitHub):
        def get_file(self, path):
            raise AuthError('401 bad credentials', path=path)

    client = Rejected()
    monkeypatch.setattr(
        publisher, 'collect_snapshot', fake_collect(make_snapshot())
    )

    result = run(settings, app_paths, client, sleeps)

    assert result.exit_code is ExitCode.AUTH_FAILURE
    assert result.spooled is True
    assert len(spool.list_spool(app_paths.spool_dir)) == 1
