"""Command line entry point.

This module only wires the pieces together: it creates the Typer
application, defines the global options and registers the command
modules from :mod:`fleet_usage.commands`. No behaviour lives here.
"""

import dataclasses
from pathlib import Path
from typing import Annotated

import typer

from fleet_usage import __version__
from fleet_usage.commands import (
    config_cmd,
    doctor,
    init,
    publish,
    rebuild,
    repo,
    schedule,
    show,
)

__all__ = ['AppContext', 'app', 'main']


@dataclasses.dataclass(frozen=True, slots=True)
class AppContext:
    """Global options shared by every command.

    Attributes
    ----------
    config_path
        Value of ``--config``, or ``None`` to use the documented
        precedence in :mod:`fleet_usage.config`.
    """

    config_path: Path | None = None


app = typer.Typer(
    name='fleet-usage',
    help=(
        'Collect, publish and aggregate AI coding agent usage across a '
        'fleet of personal machines.'
    ),
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(config_cmd.app, name='config')
app.add_typer(repo.app, name='repo')
app.add_typer(schedule.app, name='schedule')
app.command('init')(init.init_command)
app.command('doctor')(doctor.doctor_command)
app.command('publish')(publish.publish_command)
app.command('show')(show.show_command)
app.command('rebuild')(rebuild.rebuild_command)


def _version_callback(value: bool) -> None:
    """Print the package version and exit.

    Parameters
    ----------
    value : bool
        Whether ``--version`` was given.

    Raises
    ------
    typer.Exit
        When the option was given.
    """
    if value:
        typer.echo(f'fleet-usage {__version__}')
        raise typer.Exit(0)


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            '--config',
            help='Path to settings.toml (overrides FLEET_USAGE_CONFIG).',
            metavar='PATH',
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            '--version',
            help='Show the version and exit.',
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    """Store the global options for the invoked command.

    Parameters
    ----------
    ctx : typer.Context
        Click context of the invocation.
    config : pathlib.Path or None, optional
        Explicit settings path.
    version : bool, optional
        Handled by an eager callback.
    """
    del version
    ctx.obj = AppContext(config_path=config)


def main() -> None:
    """Run the command line application."""
    app()
