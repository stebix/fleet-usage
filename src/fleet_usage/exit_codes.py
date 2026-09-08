"""Process exit codes used by every ``fleet-usage`` command.

The numeric values are part of the public contract of the CLI: schedulers
and wrapper scripts branch on them, therefore they must never change.
"""

import enum
from typing import NoReturn

import typer

__all__ = ['ExitCode', 'exit_with']


class ExitCode(enum.IntEnum):
    """Exit codes returned by the ``fleet-usage`` executable.

    Attributes
    ----------
    OK
        Everything succeeded.
    ERROR
        Generic failure that has no dedicated code, for example a command
        that is not implemented yet.
    CONFIG_ERROR
        Settings are missing, malformed or incomplete for the requested
        operation.
    COLLECTION_FAILURE
        The collector (``ccusage``) could not be run or its output could
        not be parsed.
    SPOOLED_NOT_UPLOADED
        A snapshot was written to the local spool but could not be
        uploaded, usually because of a network problem.
    REMOTE_CONFLICT
        The remote repository holds conflicting or invalid data.
    AUTH_FAILURE
        The GitHub token is missing, expired or lacks permissions.
    """

    OK = 0
    ERROR = 1
    CONFIG_ERROR = 2
    COLLECTION_FAILURE = 3
    SPOOLED_NOT_UPLOADED = 4
    REMOTE_CONFLICT = 5
    AUTH_FAILURE = 6


def exit_with(code: ExitCode) -> NoReturn:
    """Terminate the current command with ``code``.

    Parameters
    ----------
    code : ExitCode
        The exit code to hand back to the operating system.

    Raises
    ------
    typer.Exit
        Always. Typer converts the exception into a process exit code,
        which keeps the behaviour testable with ``CliRunner``.
    """
    raise typer.Exit(int(code))
