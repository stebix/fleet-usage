"""The exit code contract is part of the public interface."""

import pytest
import typer

from fleet_usage.exit_codes import ExitCode, exit_with


def test_documented_values():
    assert ExitCode.OK == 0
    assert ExitCode.ERROR == 1
    assert ExitCode.CONFIG_ERROR == 2
    assert ExitCode.COLLECTION_FAILURE == 3
    assert ExitCode.SPOOLED_NOT_UPLOADED == 4
    assert ExitCode.REMOTE_CONFLICT == 5
    assert ExitCode.AUTH_FAILURE == 6


def test_exit_with_raises_typer_exit():
    with pytest.raises(typer.Exit) as excinfo:
        exit_with(ExitCode.REMOTE_CONFLICT)
    assert excinfo.value.exit_code == 5
