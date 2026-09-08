"""``fleet-usage config``: inspect the effective configuration."""

import enum
import json
from typing import Annotated, Any

import typer

from fleet_usage.commands._common import load_or_exit
from fleet_usage.config import redacted_dict
from fleet_usage.ui import out_console, print_data

__all__ = ['ConfigFormat', 'app', 'show_command']

app = typer.Typer(
    name='config',
    help='Inspect the effective configuration.',
    no_args_is_help=True,
)


class ConfigFormat(enum.StrEnum):
    """Rendering styles of ``config show``."""

    TOML = 'toml'
    JSON = 'json'


def _format_value(value: Any) -> str:
    """Render a scalar or list the way ``settings.toml`` spells it.

    Parameters
    ----------
    value : Any
        A scalar or a list of scalars.

    Returns
    -------
    str
        TOML-ish text: single quoted strings, lowercase booleans.
    """
    if isinstance(value, str):
        return "'" + value.replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return '[' + ', '.join(_format_value(item) for item in value) + ']'
    return json.dumps(value)


def _render_toml(payload: dict[str, Any], prefix: str = '') -> list[str]:
    """Render a redacted settings dict as TOML-ish text.

    The output is meant to be read, not parsed back: it mirrors the
    layout of ``settings.toml`` so that an operator can compare the two
    at a glance.

    Parameters
    ----------
    payload : dict
        Redacted settings.
    prefix : str, optional
        Current section prefix.

    Returns
    -------
    list of str
        Lines of the rendered document.
    """
    scalars: list[str] = []
    tables: list[str] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            name = f'{prefix}{key}'
            tables.append('')
            tables.append(f'[{name}]')
            tables.extend(_render_toml(value, prefix=f'{name}.'))
        else:
            scalars.append(f'{key} = {_format_value(value)}')
    return scalars + tables


@app.command('show')
def show_command(
    ctx: typer.Context,
    output_format: Annotated[
        ConfigFormat,
        typer.Option('--format', help='Output format.'),
    ] = ConfigFormat.TOML,
) -> None:
    """Print the effective settings with secrets masked.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.
    output_format : ConfigFormat, optional
        ``'toml'`` for a human readable rendering, ``'json'`` for a
        machine readable one.
    """
    settings = load_or_exit(ctx)
    payload = redacted_dict(settings)
    if output_format is ConfigFormat.JSON:
        print_data(json.dumps(payload, indent=2, sort_keys=True))
        return
    console = out_console()
    if settings.source_path is not None:
        console.print(f'# {settings.source_path}')
    print_data('\n'.join(_render_toml(payload)).strip())
