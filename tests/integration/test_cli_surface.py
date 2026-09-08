"""The full command surface exists and every help page renders."""

import pytest

from fleet_usage import __version__
from fleet_usage.exit_codes import ExitCode

HELP_TARGETS = [
    ('--help',),
    ('init', '--help'),
    ('doctor', '--help'),
    ('config', '--help'),
    ('config', 'show', '--help'),
    ('repo', '--help'),
    ('publish', '--help'),
    ('show', '--help'),
    ('rebuild', '--help'),
    ('schedule', '--help'),
]


@pytest.mark.parametrize('args', HELP_TARGETS, ids=lambda a: ' '.join(a))
def test_help_works(invoke, args):
    result = invoke(*args)
    assert result.exit_code == ExitCode.OK, result.output


def test_version_option(invoke):
    result = invoke('--version')
    assert result.exit_code == ExitCode.OK
    assert __version__ in result.output
