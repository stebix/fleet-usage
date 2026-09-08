"""Fixtures for the CLI integration tests."""

import pytest
from typer.testing import CliRunner

from fleet_usage.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def invoke(runner, app_paths, monkeypatch):
    """Invoke the CLI with every path redirected into tmp_path."""
    monkeypatch.setenv('NO_COLOR', '1')
    monkeypatch.setenv('TERM', 'dumb')

    def _invoke(*args, **kwargs):
        return runner.invoke(app, list(args), **kwargs)

    return _invoke
