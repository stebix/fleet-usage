"""The GitHub transport: media types, error mapping and retries."""

import base64
import json

import httpx
import pytest

from fleet_usage import __version__
from fleet_usage.github import (
    AuthError,
    GitHubClient,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RemoteConflictError,
    check_access,
)

pytestmark = pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False,
    can_send_already_matched_responses=True,
)

BASE = 'https://api.github.com'
REPO = 'octo/data'
CONTENTS = f'{BASE}/repos/{REPO}/contents'
TREES = f'{BASE}/repos/{REPO}/git/trees'


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps) -> GitHubClient:
    instance = GitHubClient(
        token='secret-token',
        repository=REPO,
        branch='main',
        api_base_url=BASE,
        sleep=sleeps.append,
    )
    yield instance
    instance.close()


def contents_payload(content: bytes, sha: str = 'abc123') -> dict:
    return {
        'name': 'file.json',
        'sha': sha,
        'size': len(content),
        'encoding': 'base64',
        'content': base64.b64encode(content).decode('ascii'),
    }


# -- construction ----------------------------------------------------


def test_rejects_a_malformed_repository():
    with pytest.raises(ValueError, match='OWNER/NAME'):
        GitHubClient(token='t', repository='no-slash')


def test_sends_the_documented_headers(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json?ref=main', json=contents_payload(b'{}')
    )
    client.get_file('a.json')
    request = httpx_mock.get_requests()[0]
    assert request.headers['authorization'] == 'Bearer secret-token'
    assert request.headers['accept'] == 'application/vnd.github+json'
    assert request.headers['x-github-api-version'] == '2022-11-28'
    assert request.headers['user-agent'] == f'fleet-usage/{__version__}'


# -- get_file --------------------------------------------------------


def test_get_file_decodes_base64_and_returns_the_sha(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m.json?ref=main',
        json=contents_payload(b'{"schema_version": 1}', sha='deadbeef'),
    )
    content, sha = client.get_file('machines/m.json')
    assert content == b'{"schema_version": 1}'
    assert sha == 'deadbeef'


def test_get_file_maps_404_to_not_found(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/machines/m.json?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )
    with pytest.raises(NotFoundError):
        client.get_file('machines/m.json')


def test_get_file_falls_back_to_the_raw_media_type(httpx_mock, client):
    payload = b'x' * 32
    httpx_mock.add_response(
        url=f'{CONTENTS}/big.json?ref=main',
        json={'sha': 'big-sha', 'encoding': 'none', 'content': ''},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/big.json?ref=main', content=payload
    )
    content, sha = client.get_file('big.json')
    assert content == payload
    assert sha == 'big-sha'
    accepts = [
        request.headers['accept'] for request in httpx_mock.get_requests()
    ]
    assert accepts == [
        'application/vnd.github+json',
        'application/vnd.github.raw+json',
    ]


def test_get_file_rejects_a_directory(httpx_mock, client):
    httpx_mock.add_response(url=f'{CONTENTS}/dir?ref=main', json=[])
    with pytest.raises(Exception, match='directory'):
        client.get_file('dir')


# -- put_file --------------------------------------------------------


def test_put_file_sends_base64_and_the_branch(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/snapshots/a.json',
        method='PUT',
        json={'content': {'sha': 'new-sha'}},
    )
    sha = client.put_file('snapshots/a.json', b'hello', 'msg', sha='old')
    assert sha == 'new-sha'
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert base64.b64decode(body['content']) == b'hello'
    assert body['branch'] == 'main'
    assert body['message'] == 'msg'
    assert body['sha'] == 'old'


def test_put_file_omits_the_sha_when_creating(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        json={'content': {'sha': 'new-sha'}},
    )
    client.put_file('a.json', b'hello', 'msg')
    body = json.loads(httpx_mock.get_requests()[0].content)
    assert 'sha' not in body


@pytest.mark.parametrize('status', [409, 422])
def test_put_file_maps_conflicts(httpx_mock, client, status):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=status,
        json={'message': 'sha does not match'},
    )
    with pytest.raises(RemoteConflictError, match='sha does not match'):
        client.put_file('a.json', b'hello', 'msg')
    assert len(httpx_mock.get_requests()) == 1


def test_put_file_maps_404_to_not_found(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=404,
        json={'message': 'Not Found'},
    )
    with pytest.raises(NotFoundError):
        client.put_file('a.json', b'hello', 'msg')


def test_401_is_an_auth_error_and_is_never_retried(httpx_mock, client, sleeps):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=401,
        json={'message': 'Bad credentials'},
    )
    with pytest.raises(AuthError, match='Bad credentials'):
        client.put_file('a.json', b'hello', 'msg')
    assert len(httpx_mock.get_requests()) == 1
    assert sleeps == []


def test_403_without_rate_limit_headers_is_an_auth_error(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=403,
        json={'message': 'Resource not accessible by personal access token'},
    )
    with pytest.raises(AuthError) as excinfo:
        client.put_file('a.json', b'hello', 'msg')
    assert not isinstance(excinfo.value, RateLimitError)


# -- retries ---------------------------------------------------------


def test_rate_limit_honours_retry_after(httpx_mock, client, sleeps):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=403,
        headers={'Retry-After': '7', 'x-ratelimit-remaining': '0'},
        json={'message': 'You have exceeded a secondary rate limit'},
    )
    with pytest.raises(RateLimitError) as excinfo:
        client.put_file('a.json', b'hello', 'msg')
    assert excinfo.value.retry_after == 7.0
    assert sleeps == [7.0, 7.0, 7.0, 7.0]
    assert len(httpx_mock.get_requests()) == 5


def test_rate_limit_then_success(httpx_mock, client, sleeps):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        status_code=429,
        headers={'Retry-After': '2'},
        json={'message': 'Too many requests'},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json',
        method='PUT',
        json={'content': {'sha': 'new-sha'}},
    )
    assert client.put_file('a.json', b'hello', 'msg') == 'new-sha'
    assert sleeps == [2.0]


def test_server_error_is_retried_then_succeeds(httpx_mock, client, sleeps):
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json?ref=main',
        status_code=502,
        json={'message': 'Bad gateway'},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/a.json?ref=main', json=contents_payload(b'ok')
    )
    content, _ = client.get_file('a.json')
    assert content == b'ok'
    assert len(sleeps) == 1


def test_network_errors_are_retried_then_reported(httpx_mock, client, sleeps):
    httpx_mock.add_exception(httpx.ConnectError('down'), is_reusable=True)
    with pytest.raises(NetworkError, match='down'):
        client.get_file('a.json')
    assert len(httpx_mock.get_requests()) == 5
    assert len(sleeps) == 4


def test_the_retry_budget_stops_the_loop(httpx_mock, sleeps):
    with GitHubClient(
        token='t',
        repository=REPO,
        api_base_url=BASE,
        sleep=sleeps.append,
        retry_budget=1.0,
    ) as bounded:
        httpx_mock.add_exception(httpx.ConnectError('down'), is_reusable=True)
        with pytest.raises(NetworkError):
            bounded.get_file('a.json')
    assert sum(sleeps) <= 1.0
    assert len(httpx_mock.get_requests()) < 5


# -- list_tree -------------------------------------------------------


def tree(paths: list[str], truncated: bool = False) -> dict:
    return {
        'sha': 'root',
        'truncated': truncated,
        'tree': [
            {'path': path, 'type': 'blob', 'sha': f'sha-{path}'}
            for path in paths
        ],
    }


def test_list_tree_filters_and_sorts(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1',
        json=tree(
            [
                'snapshots/m1/2026-09/b.json',
                'snapshots/m1/2026-09/a.json',
                'snapshots/m2/2026-09/c.json',
                'fleet.toml',
            ]
        ),
    )
    assert client.list_tree('snapshots/m1') == [
        'snapshots/m1/2026-09/a.json',
        'snapshots/m1/2026-09/b.json',
    ]


def test_list_tree_is_empty_for_a_repository_without_commits(
    httpx_mock, client
):
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1',
        status_code=409,
        json={'message': 'Git Repository is empty.'},
    )
    assert client.list_tree('snapshots/m1') == []


def test_list_tree_walks_subtrees_when_truncated(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1', json=tree([], truncated=True)
    )
    httpx_mock.add_response(
        url=f'{TREES}/main',
        json={
            'sha': 'root',
            'tree': [
                {'path': 'snapshots', 'type': 'tree', 'sha': 'tree-snapshots'},
                {'path': 'fleet.toml', 'type': 'blob', 'sha': 'x'},
            ],
        },
    )
    httpx_mock.add_response(
        url=f'{TREES}/tree-snapshots',
        json={
            'sha': 'tree-snapshots',
            'tree': [{'path': 'm1', 'type': 'tree', 'sha': 'tree-m1'}],
        },
    )
    httpx_mock.add_response(
        url=f'{TREES}/tree-m1?recursive=1',
        json={
            'sha': 'tree-m1',
            'truncated': False,
            'tree': [
                {'path': '2026-09/b.json', 'type': 'blob', 'sha': 'b'},
                {'path': '2026-09/a.json', 'type': 'blob', 'sha': 'a'},
                {'path': '2026-09', 'type': 'tree', 'sha': 't'},
            ],
        },
    )
    assert client.list_tree('snapshots/m1') == [
        'snapshots/m1/2026-09/a.json',
        'snapshots/m1/2026-09/b.json',
    ]


def test_list_tree_falls_back_to_the_contents_api(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1', json=tree([], truncated=True)
    )
    httpx_mock.add_response(
        url=f'{TREES}/main',
        json={
            'sha': 'root',
            'tree': [
                {'path': 'snapshots', 'type': 'tree', 'sha': 'tree-snapshots'}
            ],
        },
    )
    httpx_mock.add_response(
        url=f'{TREES}/tree-snapshots',
        json={
            'sha': 'tree-snapshots',
            'tree': [{'path': 'm1', 'type': 'tree', 'sha': 'tree-m1'}],
        },
    )
    httpx_mock.add_response(
        url=f'{TREES}/tree-m1?recursive=1',
        json={'sha': 'tree-m1', 'truncated': True, 'tree': []},
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/snapshots/m1?ref=main',
        json=[
            {'path': 'snapshots/m1/2026-09', 'type': 'dir'},
            {'path': 'snapshots/m1/README', 'type': 'file'},
        ],
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/snapshots/m1/2026-09?ref=main',
        json=[
            {'path': 'snapshots/m1/2026-09/a.json', 'type': 'file'},
            {'path': 'snapshots/m1/2026-09/b.json', 'type': 'file'},
        ],
    )
    assert client.list_tree('snapshots/m1') == [
        'snapshots/m1/2026-09/a.json',
        'snapshots/m1/2026-09/b.json',
        'snapshots/m1/README',
    ]


def test_list_tree_is_empty_for_an_unknown_prefix(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{TREES}/main?recursive=1', json=tree([], truncated=True)
    )
    httpx_mock.add_response(url=f'{TREES}/main', json={'tree': []})
    assert client.list_tree('snapshots/m1') == []


# -- repository ------------------------------------------------------


def test_create_repo_uses_the_user_endpoint_for_the_owner(httpx_mock, client):
    httpx_mock.add_response(url=f'{BASE}/user', json={'login': 'Octo'})
    httpx_mock.add_response(
        url=f'{BASE}/user/repos', method='POST', json={'full_name': REPO}
    )
    assert client.create_repo()['full_name'] == REPO
    body = json.loads(httpx_mock.get_requests()[1].content)
    assert body == {
        'name': 'data',
        'private': True,
        'auto_init': True,
        'description': 'fleet-usage data repository',
    }


def test_create_repo_uses_the_org_endpoint_for_another_owner(
    httpx_mock, client
):
    httpx_mock.add_response(url=f'{BASE}/user', json={'login': 'someone'})
    httpx_mock.add_response(
        url=f'{BASE}/orgs/octo/repos', method='POST', json={'full_name': REPO}
    )
    assert client.create_repo()['full_name'] == REPO


def test_repo_exists_is_false_on_404(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{BASE}/repos/{REPO}',
        status_code=404,
        json={'message': 'Not Found'},
    )
    assert client.repo_exists() is False


def test_check_access_reports_write_permission(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{BASE}/repos/{REPO}',
        json={
            'full_name': REPO,
            'private': True,
            'permissions': {'push': True, 'pull': True},
        },
    )
    report = check_access(client)
    assert report.exists and report.can_read and report.can_write
    assert 'read and write' in report.detail


def test_check_access_reports_read_only(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{BASE}/repos/{REPO}',
        json={'private': True, 'permissions': {'push': False, 'pull': True}},
    )
    report = check_access(client)
    assert report.can_read is True
    assert report.can_write is False


def test_check_access_reports_a_missing_repository(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{BASE}/repos/{REPO}',
        status_code=404,
        json={'message': 'Not Found'},
    )
    report = check_access(client)
    assert report.exists is False
    assert 'does not exist' in report.detail


def test_check_access_reports_a_bad_token(httpx_mock, client):
    httpx_mock.add_response(
        url=f'{BASE}/repos/{REPO}',
        status_code=401,
        json={'message': 'Bad credentials'},
    )
    report = check_access(client)
    assert report.can_read is False
    assert 'Bad credentials' in report.detail
