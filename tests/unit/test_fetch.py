"""Downloading, caching and offline behaviour of the fleet documents."""

import datetime as dt
import json
from pathlib import Path

import pytest

from fleet_usage import fetch
from fleet_usage.config import Settings
from fleet_usage.github import AuthError, GitHubError, NotFoundError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'ledgers'
M1 = '11111111-1111-4111-8111-111111111111'
M2 = '22222222-2222-4222-8222-222222222222'
M3 = '33333333-3333-4333-8333-333333333333'
M4 = '44444444-4444-4444-8444-444444444444'
NOW = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)


class FakeClient:
    """Serves recorded bytes and records every call."""

    def __init__(self, files=None, shas=None, tree=None, errors=None):
        self.files = dict(files or {})
        self.shas = dict(shas or {})
        self.tree = list(tree) if tree is not None else None
        self.errors = dict(errors or {})
        self.calls: list[str] = []

    def get_file(self, path):
        self.calls.append(path)
        if path in self.errors:
            raise self.errors[path]
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path], self.shas.get(path, f'sha-{path}')

    def list_tree(self, prefix):
        self.calls.append(f'tree:{prefix}')
        if self.tree is None:
            return [
                path for path in sorted(self.files) if path.startswith(prefix)
            ]
        return list(self.tree)


class ExplodingClient:
    """Any call is a test failure."""

    def get_file(self, path):
        raise AssertionError(f'get_file({path!r}) must not be called')

    def list_tree(self, prefix):
        raise AssertionError(f'list_tree({prefix!r}) must not be called')


def fixture_files() -> dict[str, bytes]:
    files = {'fleet.toml': (FIXTURES / 'fleet.toml').read_bytes()}
    for machine_id in (M1, M2, M3):
        path = FIXTURES / 'machines' / f'{machine_id}.json'
        files[f'machines/{machine_id}.json'] = path.read_bytes()
    return files


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({'github': {'repository': 'octo/data'}})


@pytest.fixture
def client() -> FakeClient:
    return FakeClient(fixture_files())


def cache_files(paths):
    return sorted(p.name for p in paths.ledger_cache_dir.iterdir())


# --- online ----------------------------------------------------------


def test_fetches_manifest_and_every_ledger(settings, app_paths, client):
    fleet = fetch.fetch_fleet(
        settings, app_paths, client, offline=False, now=NOW
    )
    assert [entry.id for entry in fleet.manifest.machines] == [M1, M2, M3, M4]
    assert set(fleet.ledgers) == {M1, M2, M3}
    assert fleet.never_reported == (M4,)
    assert fleet.from_cache is False
    assert fleet.fetched_at == NOW
    assert fleet.label_of(M4) == 'travel-laptop'
    assert 'fleet.toml' in client.calls
    assert f'machines/{M4}.json' in client.calls


def test_writes_the_cache_atomically(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    names = cache_files(app_paths)
    assert 'fleet.toml' in names
    assert f'{M1}.json' in names
    assert not [name for name in names if name.endswith('.tmp')]
    sidecar = json.loads(
        (app_paths.ledger_cache_dir / f'{M1}.json.meta.json').read_text()
    )
    assert sidecar['sha'] == f'sha-machines/{M1}.json'
    assert sidecar['fetched_at'] == '2026-09-05T12:00:00Z'
    assert sidecar['path'] == f'machines/{M1}.json'


def test_unchanged_sha_does_not_rewrite_the_cache(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    cached = app_paths.ledger_cache_dir / f'{M1}.json'
    before = cached.stat().st_ino
    later = NOW + dt.timedelta(hours=1)
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=later)
    assert cached.stat().st_ino == before
    sidecar = json.loads(
        (app_paths.ledger_cache_dir / f'{M1}.json.meta.json').read_text()
    )
    assert sidecar['fetched_at'] == '2026-09-05T12:00:00Z'


def test_changed_sha_replaces_the_cache(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    payload = json.loads(client.files[f'machines/{M1}.json'])
    payload['label'] = 'renamed'
    client.files[f'machines/{M1}.json'] = json.dumps(payload).encode()
    client.shas[f'machines/{M1}.json'] = 'sha-new'
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    cached = json.loads(
        (app_paths.ledger_cache_dir / f'{M1}.json').read_text()
    )
    assert cached['label'] == 'renamed'


def test_unregistered_ledgers_are_listed(settings, app_paths):
    stray = '99999999-9999-4999-8999-999999999999'
    files = fixture_files()
    files[f'machines/{stray}.json'] = files[f'machines/{M1}.json']
    client = FakeClient(files)
    fleet = fetch.fetch_fleet(
        settings, app_paths, client, offline=False, now=NOW
    )
    assert fleet.unregistered == (stray,)


def test_unregistered_discovery_tolerates_a_listing_failure(
    settings, app_paths
):
    class NoTree(FakeClient):
        def list_tree(self, prefix):
            raise GitHubError('trees API unavailable')

    fleet = fetch.fetch_fleet(
        settings, app_paths, NoTree(fixture_files()), offline=False, now=NOW
    )
    assert fleet.unregistered == ()
    assert len(fleet.ledgers) == 3


# --- failures --------------------------------------------------------


def test_network_failure_falls_back_to_the_cache(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    broken = FakeClient(
        fixture_files(),
        errors={
            path: GitHubError('connection reset') for path in fixture_files()
        },
    )
    later = NOW + dt.timedelta(hours=2)
    fleet = fetch.fetch_fleet(
        settings, app_paths, broken, offline=False, now=later
    )
    assert fleet.from_cache is True
    assert fleet.fetched_at == NOW
    assert set(fleet.ledgers) == {M1, M2, M3}


def test_network_failure_without_a_cache_is_an_error(settings, app_paths):
    broken = FakeClient(errors={'fleet.toml': GitHubError('offline')})
    with pytest.raises(fetch.NoDataError, match='no cached copy'):
        fetch.fetch_fleet(settings, app_paths, broken, offline=False, now=NOW)


def test_missing_manifest_is_an_error(settings, app_paths):
    with pytest.raises(fetch.NoDataError, match='repo init'):
        fetch.fetch_fleet(
            settings, app_paths, FakeClient(), offline=False, now=NOW
        )


def test_auth_failure_is_not_hidden_by_the_cache(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    denied = FakeClient(errors={'fleet.toml': AuthError('bad credentials')})
    with pytest.raises(fetch.FetchAuthError):
        fetch.fetch_fleet(settings, app_paths, denied, offline=False, now=NOW)


def test_invalid_remote_ledger_keeps_the_old_cache(
    settings, app_paths, client
):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    cached = app_paths.ledger_cache_dir / f'{M1}.json'
    good = cached.read_bytes()
    broken = FakeClient(fixture_files())
    broken.files[f'machines/{M1}.json'] = b'{"schema_version": '
    broken.shas[f'machines/{M1}.json'] = 'sha-broken'
    with pytest.raises(fetch.RemoteDataError, match='is invalid'):
        fetch.fetch_fleet(settings, app_paths, broken, offline=False, now=NOW)
    assert cached.read_bytes() == good
    assert not [
        name for name in cache_files(app_paths) if name.endswith('.tmp')
    ]


def test_invalid_remote_manifest_is_reported(settings, app_paths):
    client = FakeClient({'fleet.toml': b'not = [toml'})
    with pytest.raises(fetch.RemoteDataError, match=r'fleet\.toml'):
        fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)


def test_unusable_machine_id_is_rejected(settings, app_paths):
    manifest = (
        "schema_version = 1\ntimezone = 'Europe/Berlin'\n"
        "[[machines]]\nid = '../escape'\nlabel = 'evil'\n"
    )
    client = FakeClient({'fleet.toml': manifest.encode()})
    with pytest.raises(fetch.RemoteDataError, match='unusable machine id'):
        fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)


# --- offline ---------------------------------------------------------


def test_offline_never_touches_the_client(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    fleet = fetch.fetch_fleet(
        settings, app_paths, ExplodingClient(), offline=True, now=NOW
    )
    assert fleet.offline is True
    assert fleet.from_cache is True
    assert set(fleet.ledgers) == {M1, M2, M3}
    assert fleet.never_reported == (M4,)
    assert fleet.unregistered == ()


def test_offline_without_a_cache_explains_itself(settings, app_paths):
    with pytest.raises(fetch.NoDataError, match='no cached copy'):
        fetch.fetch_fleet(
            settings, app_paths, ExplodingClient(), offline=True, now=NOW
        )


def test_offline_accepts_no_client_at_all(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    fleet = fetch.fetch_fleet(settings, app_paths, None, offline=True, now=NOW)
    assert set(fleet.ledgers) == {M1, M2, M3}


def test_a_damaged_cache_sidecar_costs_only_the_metadata(
    settings, app_paths, client
):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    (app_paths.ledger_cache_dir / f'{M1}.json.meta.json').write_text('nope')
    fleet = fetch.fetch_fleet(settings, app_paths, None, offline=True, now=NOW)
    assert M1 in fleet.ledgers
    assert fleet.files[f'machines/{M1}.json'].sha is None


def test_a_damaged_cached_document_is_reported(settings, app_paths, client):
    fetch.fetch_fleet(settings, app_paths, client, offline=False, now=NOW)
    (app_paths.ledger_cache_dir / f'{M1}.json').write_text('{')
    with pytest.raises(fetch.RemoteDataError, match='cached copy'):
        fetch.fetch_fleet(settings, app_paths, None, offline=True, now=NOW)


# --- helpers ---------------------------------------------------------


def test_build_client_without_a_token(settings):
    with pytest.raises(fetch.FetchAuthError, match='--offline'):
        fetch.build_client(settings)


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('machines/abc.json', 'abc'),
        ('machines/', None),
        ('machines/nested/abc.json', None),
        ('snapshots/abc.json', None),
        ('machines/abc.txt', None),
    ],
)
def test_machine_id_from_path(path, expected):
    assert fetch.machine_id_from_path(path) == expected


def test_ledger_remote_path():
    assert fetch.ledger_remote_path('abc') == 'machines/abc.json'
