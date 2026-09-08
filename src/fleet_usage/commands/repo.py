"""``fleet-usage repo``: prepare and join the private data repository.

Two operations live here. ``repo init`` creates the repository when
asked to and makes sure it carries the three scaffolding files:
``fleet.toml``, the README workflow and the dependency free render
script that the workflow calls. ``repo register`` adds the local machine
to ``fleet.toml``.

The manifest is read with :mod:`tomllib` and written with a small
renderer, because the file has a fixed, tiny shape and a TOML writer
dependency would buy nothing.
"""

import inspect
import json
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from fleet_usage import fetch, readme_render
from fleet_usage.commands._common import load_or_exit
from fleet_usage.config import Settings, resolve_token
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.github import (
    AuthError,
    GitHubClient,
    GitHubError,
    NotFoundError,
    RemoteConflictError,
)
from fleet_usage.models import FleetManifest, MachineEntry
from fleet_usage.templates import README_WORKFLOW, read_template
from fleet_usage.ui import out_console, print_error

__all__ = [
    'MANIFEST_PATH',
    'REGISTER_ATTEMPTS',
    'SCRIPT_PATH',
    'WORKFLOW_PATH',
    'app',
    'init_command',
    'parse_manifest',
    'register_command',
    'render_manifest',
    'render_script_source',
]

MANIFEST_PATH = 'fleet.toml'
WORKFLOW_PATH = '.github/workflows/readme.yml'
SCRIPT_PATH = 'scripts/render_readme.py'

#: A manifest write races with the other machines of the fleet, so a
#: stale blob SHA is retried rather than reported.
REGISTER_ATTEMPTS = 3

app = typer.Typer(
    name='repo',
    help='Prepare and join the private data repository.',
    no_args_is_help=True,
)


# -- manifest --------------------------------------------------------


def _toml_string(value: str) -> str:
    """Quote ``value`` as a TOML string.

    Parameters
    ----------
    value : str
        Any text.

    Returns
    -------
    str
        A literal string when the value allows it, and a basic string
        otherwise; :func:`json.dumps` produces exactly the escaping a
        TOML basic string uses for the characters that can occur here.
    """
    if "'" in value or '\n' in value or '\\' in value:
        return json.dumps(value)
    return f"'{value}'"


def render_manifest(manifest: FleetManifest) -> str:
    """Render ``fleet.toml``.

    Parameters
    ----------
    manifest : FleetManifest
        The manifest to serialise.

    Returns
    -------
    str
        The document, ending in a newline.
    """
    lines = [
        f'schema_version = {manifest.schema_version}',
        f'timezone = {_toml_string(manifest.timezone)}',
        f'freeze_window_days = {manifest.freeze_window_days}',
    ]
    for machine in manifest.machines:
        lines.extend(
            [
                '',
                '[[machines]]',
                f'id = {_toml_string(machine.id)}',
                f'label = {_toml_string(machine.label)}',
            ]
        )
    return '\n'.join(lines) + '\n'


def parse_manifest(text: str) -> FleetManifest:
    """Parse ``fleet.toml``.

    Parameters
    ----------
    text : str
        The document.

    Returns
    -------
    FleetManifest
        The parsed manifest.

    Raises
    ------
    ValueError
        If the document is not valid TOML or not a valid manifest.
    """
    try:
        return fetch.parse_manifest(text.encode('utf-8'))
    except ValueError as exc:
        msg = f'{MANIFEST_PATH}: {exc}'
        raise ValueError(msg) from exc


def render_script_source() -> str:
    """Return the source of the dependency free render script.

    The script copied into the data repository is the source text of
    :mod:`fleet_usage.readme_render`, which imports nothing from the
    package, so that the workflow can run it on a stock runner.

    Returns
    -------
    str
        The module source.
    """
    try:
        return (
            resources.files('fleet_usage')
            .joinpath('readme_render.py')
            .read_text(encoding='utf-8')
        )
    except (FileNotFoundError, OSError):  # pragma: no cover - packaging
        path = inspect.getsourcefile(readme_render)
        if path is None:
            msg = 'cannot locate the source of fleet_usage.readme_render'
            raise RuntimeError(msg) from None
        return Path(path).read_text(encoding='utf-8')


# -- shared plumbing -------------------------------------------------


def _client(settings: Settings, repository: str) -> GitHubClient:
    """Build a GitHub client for ``repository``.

    Parameters
    ----------
    settings : Settings
        Loaded settings, for the branch, the API base URL and the token.
    repository : str
        ``OWNER/NAME`` of the data repository.

    Returns
    -------
    GitHubClient
        The client. The command exits with a configuration error when
        the token is missing or the repository is malformed.
    """
    token = resolve_token(settings)
    if token is None:
        print_error(
            f'{settings.github.token_env} is not set',
            'put the GitHub token there or into the .env file next to '
            'settings.toml',
        )
        exit_with(ExitCode.CONFIG_ERROR)
    try:
        return GitHubClient(
            token=token,
            repository=repository,
            branch=settings.github.branch,
            api_base_url=settings.github.api_base_url,
        )
    except ValueError as exc:
        print_error(str(exc))
        exit_with(ExitCode.CONFIG_ERROR)


def _fail(exc: GitHubError) -> None:
    """Report a GitHub failure and exit with the matching code.

    Parameters
    ----------
    exc : GitHubError
        The failure. Permission problems become
        :attr:`~fleet_usage.exit_codes.ExitCode.AUTH_FAILURE`, remote
        state problems become
        :attr:`~fleet_usage.exit_codes.ExitCode.REMOTE_CONFLICT` and
        everything else, in particular transport failures, becomes the
        generic error code.
    """
    print_error(str(exc))
    if isinstance(exc, AuthError):
        exit_with(ExitCode.AUTH_FAILURE)
    if isinstance(exc, (NotFoundError, RemoteConflictError)):
        exit_with(ExitCode.REMOTE_CONFLICT)
    exit_with(ExitCode.ERROR)


def _ensure_file(
    client: GitHubClient,
    path: str,
    content: bytes,
    *,
    overwrite: bool,
) -> str:
    """Create a file, or refresh it when asked to.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    path : str
        Repository relative path.
    content : bytes
        The content the file should have.
    overwrite : bool
        Whether an existing file may be replaced.

    Returns
    -------
    str
        ``'created'``, ``'updated'`` or ``'kept'``.
    """
    try:
        current, sha = client.get_file(path)
    except NotFoundError:
        client.put_file(path, content, f'fleet-usage: add {path}')
        return 'created'
    if not overwrite or current == content:
        return 'kept'
    client.put_file(path, content, f'fleet-usage: update {path}', sha=sha)
    return 'updated'


def _require_manifest(client: GitHubClient) -> None:
    """Refuse to refresh the scaffolding of a foreign repository.

    ``--update`` overwrites generated files, so it must be certain that
    the repository really is a fleet data repository: a readable
    ``fleet.toml`` is the only evidence of that.

    Parameters
    ----------
    client : GitHubClient
        The transport.

    Raises
    ------
    GitHubError
        If the manifest cannot be fetched for a reason other than its
        absence.
    """
    try:
        content, _sha = client.get_file(MANIFEST_PATH)
    except NotFoundError:
        print_error(
            f'{client.repository} has no {MANIFEST_PATH}',
            'run the same command without --update to create the '
            'scaffolding first',
        )
        exit_with(ExitCode.REMOTE_CONFLICT)
    try:
        parse_manifest(content.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        print_error(
            f'{client.repository}: {exc}',
            'refusing to rewrite the scaffolding of a repository whose '
            f'{MANIFEST_PATH} cannot be read',
        )
        exit_with(ExitCode.REMOTE_CONFLICT)


# -- commands --------------------------------------------------------


@app.command('init')
def init_command(
    ctx: typer.Context,
    repo: Annotated[
        str,
        typer.Option(
            '--repo',
            help='Data repository as OWNER/NAME.',
            metavar='OWNER/NAME',
        ),
    ],
    create: Annotated[
        bool,
        typer.Option('--create', help='Create the repository if missing.'),
    ] = False,
    update: Annotated[
        bool,
        typer.Option('--update', help='Refresh the scaffolding files.'),
    ] = False,
) -> None:
    """Create or refresh the scaffolding of the data repository.

    ``fleet.toml`` is never overwritten: it is the shared state of the
    fleet and is only created when it is missing. ``--update`` refreshes
    the workflow and the render script, which are generated files, and
    is refused unless the repository already carries a readable
    ``fleet.toml``.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    repo : str
        Data repository.
    create : bool, optional
        Create the repository when it does not exist.
    update : bool, optional
        Overwrite the generated scaffolding files.
    """
    settings = load_or_exit(ctx)
    console = out_console()
    client = _client(settings, repo)
    manifest = FleetManifest(
        timezone=settings.timezone,
        freeze_window_days=settings.ledger.freeze_window_days,
        machines=[],
    )
    try:
        if create:
            if client.repo_exists():
                print_error(
                    f'{repo} already exists',
                    'drop --create to only write the scaffolding files',
                )
                exit_with(ExitCode.REMOTE_CONFLICT)
            client.create_repo(private=True)
            console.print(f'created private repository {repo}')
        elif not client.repo_exists():
            print_error(
                f'{repo} does not exist or the token cannot see it',
                'pass --create to create it',
            )
            exit_with(ExitCode.REMOTE_CONFLICT)
        if update:
            _require_manifest(client)
        wanted = [
            (MANIFEST_PATH, render_manifest(manifest).encode('utf-8'), False),
            (
                WORKFLOW_PATH,
                read_template(README_WORKFLOW).encode('utf-8'),
                update,
            ),
            (SCRIPT_PATH, render_script_source().encode('utf-8'), update),
        ]
        for path, content, overwrite in wanted:
            action = _ensure_file(client, path, content, overwrite=overwrite)
            console.print(f'{action} {path}')
    except GitHubError as exc:
        _fail(exc)
    finally:
        client.close()


@app.command('register')
def register_command(ctx: typer.Context) -> None:
    """Add this machine to ``fleet.toml`` in the data repository.

    The operation is idempotent: a machine that is already listed is
    left alone, except that a changed label is written through.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    """
    settings = load_or_exit(ctx)
    machine = settings.machine
    if machine is None:
        print_error(
            'this installation has no [machine] section',
            "run 'fleet-usage init' without --viewer-only first",
        )
        exit_with(ExitCode.CONFIG_ERROR)
    console = out_console()
    client = _client(settings, settings.github.repository)
    try:
        for attempt in range(REGISTER_ATTEMPTS):
            try:
                content, sha = client.get_file(MANIFEST_PATH)
            except NotFoundError:
                print_error(
                    f'{settings.github.repository} has no {MANIFEST_PATH}',
                    "run 'fleet-usage repo init --repo "
                    f"{settings.github.repository}' first",
                )
                exit_with(ExitCode.REMOTE_CONFLICT)
            try:
                manifest = parse_manifest(content.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                print_error(str(exc))
                exit_with(ExitCode.REMOTE_CONFLICT)
            known = {entry.id: entry for entry in manifest.machines}
            existing = known.get(machine.id)
            if existing is not None and existing.label == machine.label:
                console.print(f'{machine.label} is already registered')
                return
            if existing is None:
                manifest.machines.append(
                    MachineEntry(id=machine.id, label=machine.label)
                )
                action = 'registered'
            else:
                existing.label = machine.label
                action = 'relabelled'
            try:
                client.put_file(
                    MANIFEST_PATH,
                    render_manifest(manifest).encode('utf-8'),
                    f'fleet-usage: {action} {machine.label}',
                    sha=sha,
                )
            except RemoteConflictError:
                # Another machine registered between the GET and the
                # PUT, so the blob SHA is stale: read the manifest it
                # wrote and add this machine to that version instead.
                if attempt + 1 >= REGISTER_ATTEMPTS:
                    raise
                console.print(f'{MANIFEST_PATH} changed remotely, retrying')
                continue
            console.print(f'{action} {machine.label} ({machine.id})')
            return
    except GitHubError as exc:
        _fail(exc)
    finally:
        client.close()
