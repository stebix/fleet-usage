"""Tests for ``fleet-usage config show``."""

import json
import re

from fleet_usage.config import REDACTED
from fleet_usage.exit_codes import ExitCode

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def test_config_show_toml(invoke):
    invoke('init', '--label', 'lab-1', '--repo', 'octo/data')
    result = invoke('config', 'show')
    assert result.exit_code == ExitCode.OK, result.output
    assert '[github]' in result.output
    assert 'octo/data' in result.output
    assert REDACTED in result.output


def test_config_show_json_is_plain_and_redacted(invoke, app_paths):
    invoke('init', '--label', 'lab-1', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=super-secret\n', encoding='utf-8'
    )
    result = invoke('config', 'show', '--format', 'json')
    assert result.exit_code == ExitCode.OK, result.output
    assert ANSI.search(result.output) is None
    payload = json.loads(result.output)
    assert payload['github']['repository'] == 'octo/data'
    assert payload['github']['token_env'] == REDACTED
    assert 'super-secret' not in result.output
    assert 'source_path' not in payload


def test_config_show_without_settings(invoke):
    result = invoke('config', 'show')
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert 'fleet-usage init' in result.output


def test_config_show_rejects_unknown_format(invoke):
    invoke('init')
    result = invoke('config', 'show', '--format', 'yaml')
    assert result.exit_code != ExitCode.OK


def test_config_show_toml_uses_single_quotes(invoke):
    invoke('init', '--label', 'lab-1', '--repo', 'octo/data')
    result = invoke('config', 'show')
    assert "repository = 'octo/data'" in result.output
    assert 'offline_pricing = false' in result.output
