"""Helpers shared by the command modules."""

from pathlib import Path

import typer

from fleet_usage.config import ConfigError, Settings, load_settings
from fleet_usage.exit_codes import ExitCode, exit_with
from fleet_usage.ui import print_error

__all__ = ['config_path_of', 'load_or_exit', 'not_implemented']


def config_path_of(ctx: typer.Context) -> Path | None:
    """Return the ``--config`` value stored by the root callback.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.

    Returns
    -------
    pathlib.Path or None
        The explicit settings path, if one was given.
    """
    obj = ctx.obj
    return getattr(obj, 'config_path', None)


def load_or_exit(ctx: typer.Context) -> Settings:
    """Load the settings or terminate with a configuration error.

    Parameters
    ----------
    ctx : typer.Context
        Context of the running command.

    Returns
    -------
    Settings
        The validated settings.
    """
    try:
        return load_settings(config_path_of(ctx))
    except ConfigError as exc:
        print_error(str(exc))
        exit_with(ExitCode.CONFIG_ERROR)


def not_implemented(name: str) -> None:
    """Report an unimplemented command and exit.

    Parameters
    ----------
    name : str
        Full command name, for example ``'repo init'``.
    """
    print_error(f'{name}: not implemented yet')
    exit_with(ExitCode.ERROR)
