"""Console helpers shared by the command modules.

Everything a command prints goes through a :class:`rich.console.Console`
created here. Machine readable output is emitted through
:func:`print_data` which disables colour, markup and highlighting, so a
piped ``--format json`` never contains ANSI escape sequences.
"""

from typing import Any

from rich.console import Console

__all__ = ['err_console', 'out_console', 'print_data', 'print_error']


def out_console() -> Console:
    """Return the console used for regular output.

    Returns
    -------
    rich.console.Console
        A console writing to standard output.
    """
    return Console(highlight=False, soft_wrap=True)


def err_console() -> Console:
    """Return the console used for diagnostics.

    Returns
    -------
    rich.console.Console
        A console writing to standard error.
    """
    return Console(stderr=True, highlight=False, soft_wrap=True)


def print_data(payload: str) -> None:
    """Print machine readable text verbatim.

    Parameters
    ----------
    payload : str
        JSON, CSV or TOML text. It is written without colour, markup
        interpretation, highlighting or wrapping.
    """
    console = Console(
        highlight=False,
        soft_wrap=True,
        no_color=True,
        markup=False,
        emoji=False,
    )
    console.print(payload)


def print_error(message: str, *args: Any) -> None:
    """Print an error message to standard error.

    Parameters
    ----------
    message : str
        The message. Rich markup is disabled so that arbitrary text,
        such as a repository path in brackets, survives unchanged.
    *args : Any
        Additional lines printed after the message.
    """
    console = err_console()
    console.print('error:', style='bold red', end=' ')
    console.print(message, markup=False)
    for extra in args:
        console.print(str(extra), markup=False)
