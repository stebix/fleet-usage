"""``fleet-usage repo`` end to end against a mocked GitHub."""

import ast
import base64
import json
import sys
import time
import tomllib

import pytest

from fleet_usage.commands import repo as repo_cmd
from fleet_usage.exit_codes import ExitCode
from fleet_usage.models import FleetManifest, MachineEntry

pytestmark = pytest.mark.httpx_mock(
    assert_all_responses_were_requested=False,
    can_send_already_matched_responses=True,
)

BASE = 'https://api.github.com'
REPO_URL = f'{BASE}/repos/octo/data'
CONTENTS = f'{REPO_URL}/contents'

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
freeze_window_days = 7
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


def missing(httpx_mock, path: str) -> None:
    httpx_mock.add_response(
        url=f'{CONTENTS}/{path}?ref=main',
        status_code=404,
        json={'message': 'Not Found'},
    )


def accepts_put(httpx_mock, path: str) -> None:
    httpx_mock.add_response(
        url=f'{CONTENTS}/{path}',
        method='PUT',
        json={'content': {'sha': f'sha-{path}'}},
    )


def put_bodies(httpx_mock) -> dict[str, bytes]:
    bodies = {}
    for request in httpx_mock.get_requests():
        if request.method != 'PUT':
            continue
        path = request.url.path.split('/contents/')[-1]
        body = json.loads(request.content)
        bodies[path] = base64.b64decode(body['content'])
    return bodies


# -- the manifest writer ---------------------------------------------


def test_the_manifest_round_trips():
    manifest = FleetManifest(
        timezone='Europe/Berlin',
        freeze_window_days=7,
        machines=[
            MachineEntry(id='m1', label='box'),
            MachineEntry(id='m2', label="jan's laptop"),
        ],
    )
    text = repo_cmd.render_manifest(manifest)
    assert tomllib.loads(text) == {
        'schema_version': 1,
        'timezone': 'Europe/Berlin',
        'freeze_window_days': 7,
        'machines': [
            {'id': 'm1', 'label': 'box'},
            {'id': 'm2', 'label': "jan's laptop"},
        ],
    }
    assert repo_cmd.parse_manifest(text) == manifest


def test_a_broken_manifest_is_rejected():
    with pytest.raises(ValueError, match='not valid TOML'):
        repo_cmd.parse_manifest('= nonsense')


def test_the_render_script_is_dependency_free():
    source = repo_cmd.render_script_source()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
    assert imported
    assert imported <= sys.stdlib_module_names
    assert 'def render_readme' in source


# -- repo init -------------------------------------------------------


def test_init_writes_the_scaffolding(invoke, configured, httpx_mock):
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    for path in (
        repo_cmd.MANIFEST_PATH,
        repo_cmd.WORKFLOW_PATH,
        repo_cmd.SCRIPT_PATH,
    ):
        missing(httpx_mock, path)
        accepts_put(httpx_mock, path)

    result = invoke('repo', 'init', '--repo', 'octo/data')

    assert result.exit_code == ExitCode.OK, result.output
    bodies = put_bodies(httpx_mock)
    assert set(bodies) == {
        repo_cmd.MANIFEST_PATH,
        repo_cmd.WORKFLOW_PATH,
        repo_cmd.SCRIPT_PATH,
    }
    manifest = tomllib.loads(bodies[repo_cmd.MANIFEST_PATH].decode('utf-8'))
    assert manifest == {
        'schema_version': 1,
        'timezone': 'Europe/Berlin',
        'freeze_window_days': 7,
    }
    workflow = bodies[repo_cmd.WORKFLOW_PATH].decode('utf-8')
    # The runner has no 'python' on PATH, only 'python3'.
    assert 'python3 scripts/render_readme.py' in workflow
    assert "cron: '17 */4 * * *'" in workflow
    assert 'contents: write' in workflow
    assert 'cancel-in-progress: true' in workflow
    assert b'def render_readme' in bodies[repo_cmd.SCRIPT_PATH]


def test_init_keeps_an_existing_manifest_and_workflow(
    invoke, configured, httpx_mock
):
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(b"schema_version = 1\ntimezone = 'UTC'\n"),
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.WORKFLOW_PATH}?ref=main',
        json=contents_payload(b'name: stale\n'),
    )
    missing(httpx_mock, repo_cmd.SCRIPT_PATH)
    accepts_put(httpx_mock, repo_cmd.SCRIPT_PATH)

    result = invoke('repo', 'init', '--repo', 'octo/data')

    assert result.exit_code == ExitCode.OK, result.output
    assert set(put_bodies(httpx_mock)) == {repo_cmd.SCRIPT_PATH}
    assert f'kept {repo_cmd.MANIFEST_PATH}' in result.output


def test_update_refreshes_the_workflow_but_not_the_manifest(
    invoke, configured, httpx_mock
):
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(b"schema_version = 1\ntimezone = 'UTC'\n"),
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.WORKFLOW_PATH}?ref=main',
        json=contents_payload(b'name: stale\n', sha='workflow-sha'),
    )
    accepts_put(httpx_mock, repo_cmd.WORKFLOW_PATH)
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.SCRIPT_PATH}?ref=main',
        json=contents_payload(b'print("old")\n', sha='script-sha'),
    )
    accepts_put(httpx_mock, repo_cmd.SCRIPT_PATH)

    result = invoke('repo', 'init', '--repo', 'octo/data', '--update')

    assert result.exit_code == ExitCode.OK, result.output
    bodies = put_bodies(httpx_mock)
    assert set(bodies) == {repo_cmd.WORKFLOW_PATH, repo_cmd.SCRIPT_PATH}
    shas = {
        request.url.path.split('/contents/')[-1]: json.loads(
            request.content
        ).get('sha')
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    }
    assert shas[repo_cmd.WORKFLOW_PATH] == 'workflow-sha'


def test_update_refuses_a_repository_without_a_manifest(
    invoke, configured, httpx_mock
):
    # --update rewrites generated files, so it must be sure that the
    # repository really is a fleet data repository.
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    missing(httpx_mock, repo_cmd.MANIFEST_PATH)

    result = invoke('repo', 'init', '--repo', 'octo/data', '--update')

    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert repo_cmd.MANIFEST_PATH in result.output
    assert put_bodies(httpx_mock) == {}


def test_update_refuses_an_unparsable_manifest(invoke, configured, httpx_mock):
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(b'= nonsense'),
    )

    result = invoke('repo', 'init', '--repo', 'octo/data', '--update')

    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert put_bodies(httpx_mock) == {}


def test_create_makes_a_private_repository(invoke, configured, httpx_mock):
    httpx_mock.add_response(
        url=REPO_URL, status_code=404, json={'message': 'Not Found'}
    )
    httpx_mock.add_response(url=f'{BASE}/user', json={'login': 'octo'})
    httpx_mock.add_response(
        url=f'{BASE}/user/repos',
        method='POST',
        json={'full_name': 'octo/data'},
    )
    for path in (
        repo_cmd.MANIFEST_PATH,
        repo_cmd.WORKFLOW_PATH,
        repo_cmd.SCRIPT_PATH,
    ):
        missing(httpx_mock, path)
        accepts_put(httpx_mock, path)

    result = invoke('repo', 'init', '--repo', 'octo/data', '--create')

    assert result.exit_code == ExitCode.OK, result.output
    creates = [
        request
        for request in httpx_mock.get_requests()
        if request.method == 'POST'
    ]
    assert json.loads(creates[0].content)['private'] is True
    assert 'created private repository octo/data' in result.output


def test_create_refuses_an_existing_repository(invoke, configured, httpx_mock):
    httpx_mock.add_response(url=REPO_URL, json={'full_name': 'octo/data'})
    result = invoke('repo', 'init', '--repo', 'octo/data', '--create')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert 'already exists' in result.output


def test_init_without_create_needs_the_repository(
    invoke, configured, httpx_mock
):
    httpx_mock.add_response(
        url=REPO_URL, status_code=404, json={'message': 'Not Found'}
    )
    result = invoke('repo', 'init', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert '--create' in result.output


def test_init_reports_a_bad_token(invoke, configured, httpx_mock):
    httpx_mock.add_response(
        url=REPO_URL, status_code=401, json={'message': 'Bad credentials'}
    )
    result = invoke('repo', 'init', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.AUTH_FAILURE, result.output


def test_init_needs_a_token(invoke, app_paths, monkeypatch):
    app_paths.config_dir.mkdir(parents=True, exist_ok=True)
    app_paths.settings_file.write_text(SETTINGS, encoding='utf-8')
    monkeypatch.delenv('FLEET_USAGE_GITHUB_TOKEN', raising=False)
    result = invoke('repo', 'init', '--repo', 'octo/data')
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert 'FLEET_USAGE_GITHUB_TOKEN' in result.output


# -- repo register ---------------------------------------------------


def test_register_appends_the_machine(invoke, configured, httpx_mock):
    existing = FleetManifest(timezone='Europe/Berlin', freeze_window_days=7)
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(existing).encode('utf-8'),
            sha='manifest-sha',
        ),
    )
    accepts_put(httpx_mock, repo_cmd.MANIFEST_PATH)

    result = invoke('repo', 'register')

    assert result.exit_code == ExitCode.OK, result.output
    body = put_bodies(httpx_mock)[repo_cmd.MANIFEST_PATH]
    assert tomllib.loads(body.decode('utf-8'))['machines'] == [
        {'id': 'm1', 'label': 'box'}
    ]
    put = next(
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    )
    assert json.loads(put.content)['sha'] == 'manifest-sha'


def test_register_is_idempotent(invoke, configured, httpx_mock):
    existing = FleetManifest(
        timezone='Europe/Berlin',
        freeze_window_days=7,
        machines=[MachineEntry(id='m1', label='box')],
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(existing).encode('utf-8')
        ),
    )

    result = invoke('repo', 'register')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'already registered' in result.output
    assert put_bodies(httpx_mock) == {}


def test_register_writes_a_changed_label(invoke, configured, httpx_mock):
    existing = FleetManifest(
        timezone='Europe/Berlin',
        machines=[MachineEntry(id='m1', label='old-name')],
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(existing).encode('utf-8')
        ),
    )
    accepts_put(httpx_mock, repo_cmd.MANIFEST_PATH)

    result = invoke('repo', 'register')

    assert result.exit_code == ExitCode.OK, result.output
    body = put_bodies(httpx_mock)[repo_cmd.MANIFEST_PATH]
    assert tomllib.loads(body.decode('utf-8'))['machines'] == [
        {'id': 'm1', 'label': 'box'}
    ]


def test_register_retries_a_stale_manifest_sha(invoke, configured, httpx_mock):
    # Another machine registered between the GET and the PUT, so the
    # blob SHA is stale; the manifest has to be read again.
    existing = FleetManifest(timezone='Europe/Berlin', freeze_window_days=7)
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(existing).encode('utf-8'),
            sha='first-sha',
        ),
        is_reusable=False,
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}',
        method='PUT',
        status_code=409,
        json={'message': 'is at 0000 but expected 1111'},
        is_reusable=False,
    )
    other = FleetManifest(
        timezone='Europe/Berlin',
        freeze_window_days=7,
        machines=[MachineEntry(id='m2', label='other')],
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(other).encode('utf-8'),
            sha='second-sha',
        ),
    )
    accepts_put(httpx_mock, repo_cmd.MANIFEST_PATH)

    result = invoke('repo', 'register')

    assert result.exit_code == ExitCode.OK, result.output
    body = put_bodies(httpx_mock)[repo_cmd.MANIFEST_PATH]
    assert tomllib.loads(body.decode('utf-8'))['machines'] == [
        {'id': 'm2', 'label': 'other'},
        {'id': 'm1', 'label': 'box'},
    ]
    puts = [
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    ]
    assert [json.loads(request.content)['sha'] for request in puts] == [
        'first-sha',
        'second-sha',
    ]


def test_register_gives_up_after_three_conflicts(
    invoke, configured, httpx_mock
):
    existing = FleetManifest(timezone='Europe/Berlin', freeze_window_days=7)
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(
            repo_cmd.render_manifest(existing).encode('utf-8')
        ),
    )
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}',
        method='PUT',
        status_code=409,
        json={'message': 'is at 0000 but expected 1111'},
    )

    result = invoke('repo', 'register')

    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    puts = [
        request
        for request in httpx_mock.get_requests()
        if request.method == 'PUT'
    ]
    assert len(puts) == repo_cmd.REGISTER_ATTEMPTS


def test_register_needs_a_manifest(invoke, configured, httpx_mock):
    missing(httpx_mock, repo_cmd.MANIFEST_PATH)
    result = invoke('repo', 'register')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output
    assert 'repo init' in result.output


def test_register_rejects_a_broken_manifest(invoke, configured, httpx_mock):
    httpx_mock.add_response(
        url=f'{CONTENTS}/{repo_cmd.MANIFEST_PATH}?ref=main',
        json=contents_payload(b'= nonsense'),
    )
    result = invoke('repo', 'register')
    assert result.exit_code == ExitCode.REMOTE_CONFLICT, result.output


def test_register_needs_a_machine_section(invoke, app_paths, monkeypatch):
    app_paths.config_dir.mkdir(parents=True, exist_ok=True)
    app_paths.settings_file.write_text(
        "schema_version = 1\n\n[github]\nrepository = 'octo/data'\n",
        encoding='utf-8',
    )
    monkeypatch.setenv('FLEET_USAGE_GITHUB_TOKEN', 'secret-token')
    result = invoke('repo', 'register')
    assert result.exit_code == ExitCode.CONFIG_ERROR, result.output
    assert '[machine]' in result.output
