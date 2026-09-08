"""``fleet-usage init``: create the local configuration."""

import os
import socket
import tomllib
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from fleet_usage.commands._common import config_path_of
from fleet_usage.config import (
    DEFAULT_TIMEZONE,
    DEFAULT_TOKEN_ENV,
    render_env_template,
    render_settings_toml,
    resolve_settings_path,
)
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.ui import out_console, print_error

__all__ = ['init_command']

PLACEHOLDER_REPOSITORY = 'OWNER/fleet-usage-data'
DEFAULT_COLLECTOR_COMMAND = ['bunx', 'ccusage@20.0.20']
DEFAULT_COLLECTOR_VERSION = '20.0.20'
DEFAULT_AGENTS = ['claude', 'codex']
ENV_FILE_MODE = 0o600


def _read_existing(path: Path) -> dict[str, Any]:
    """Parse an existing settings file, tolerating damage.

    Parameters
    ----------
    path : pathlib.Path
        The settings file.

    Returns
    -------
    dict
        The parsed document, or an empty dict when the file is missing
        or unparsable. A broken file must not stop ``init --force`` from
        repairing it, but a readable machine id is still worth keeping.
    """
    if not path.is_file():
        return {}
    try:
        with path.open('rb') as stream:
            data: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a table from a parsed TOML document.

    Parameters
    ----------
    data : dict
        Parsed document.
    name : str
        Table name.

    Returns
    -------
    dict
        The table, or an empty dict when it is absent or not a table.
    """
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _write_env_file(path: Path, token_env: str) -> bool:
    """Create the ``.env`` placeholder if it does not exist yet.

    An existing file is never rewritten, not even with ``--force``: it
    is the one file that holds a real secret, and re-running ``init``
    must not destroy a working token.

    Parameters
    ----------
    path : pathlib.Path
        Location of the ``.env`` file.
    token_env : str
        Name of the token variable.

    Returns
    -------
    bool
        ``True`` when the file was created.
    """
    if path.exists():
        return False
    path.write_text(render_env_template(token_env), encoding='utf-8')
    if os.name == 'posix':
        path.chmod(ENV_FILE_MODE)
    return True


def init_command(
    ctx: typer.Context,
    label: Annotated[
        str | None,
        typer.Option('--label', help='Human readable name of this machine.'),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            '--repo',
            help='Private data repository as OWNER/NAME.',
            metavar='OWNER/NAME',
        ),
    ] = None,
    viewer_only: Annotated[
        bool,
        typer.Option(
            '--viewer-only',
            help='Configure a read-only installation without a collector.',
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            '--force',
            help='Overwrite an existing settings file.',
        ),
    ] = False,
) -> None:
    """Create ``settings.toml`` and a placeholder ``.env``.

    Re-running the command is safe: without ``--force`` it refuses to
    touch an existing settings file, and with ``--force`` it keeps the
    machine identity that is already recorded, because that identity
    names the machine's data in the remote repository.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    label : str or None, optional
        Machine label; defaults to the host name.
    repo : str or None, optional
        Data repository.
    viewer_only : bool, optional
        Omit the machine identity and the collector section.
    force : bool, optional
        Allow overwriting an existing settings file.
    """
    settings_path = resolve_settings_path(config_path_of(ctx))
    existing = _read_existing(settings_path)
    if settings_path.exists() and not force:
        print_error(
            f'{settings_path} already exists',
            'pass --force to rewrite it; the machine id is preserved',
        )
        exit_with(ExitCode.CONFIG_ERROR)

    machine = _section(existing, 'machine')
    github = _section(existing, 'github')
    collector = _section(existing, 'collector')

    machine_id = str(machine.get('id') or uuid.uuid4())
    machine_label = label or str(machine.get('label') or socket.gethostname())
    repository = repo or str(
        github.get('repository') or PLACEHOLDER_REPOSITORY
    )
    token_env = str(github.get('token_env') or DEFAULT_TOKEN_ENV)

    command = collector.get('command') or DEFAULT_COLLECTOR_COMMAND
    version = collector.get('version') or DEFAULT_COLLECTOR_VERSION
    agents = collector.get('agents') or DEFAULT_AGENTS
    timezone = str(collector.get('timezone') or DEFAULT_TIMEZONE)

    document = render_settings_toml(
        machine_id=None if viewer_only else machine_id,
        label=None if viewer_only else machine_label,
        repository=repository,
        token_env=token_env,
        collector_command=None if viewer_only else list(command),
        collector_version=None if viewer_only else str(version),
        agents=None if viewer_only else list(agents),
        timezone=timezone,
    )
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(document, encoding='utf-8')
        created_env = _write_env_file(settings_path.parent / '.env', token_env)
    except OSError as exc:
        print_error(f'cannot write configuration: {exc}')
        exit_with(ExitCode.CONFIG_ERROR)

    console = out_console()
    console.print(f'wrote {settings_path}')
    env_path = settings_path.parent / '.env'
    if created_env:
        console.print(f'wrote {env_path}')
    else:
        console.print(f'kept existing {env_path}')
    if viewer_only:
        console.print('mode: viewer-only (publishing is disabled)')
    else:
        console.print(f'machine id: {machine_id}')
        console.print(f'label: {machine_label}')
    if repository == PLACEHOLDER_REPOSITORY:
        console.print(
            'next: set [github].repository to your private data repository'
        )
    console.print(f'next: put your GitHub token into {env_path}')
