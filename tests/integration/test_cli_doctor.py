"""Tests for ``fleet-usage doctor``."""

import datetime as dt

import pytest

from fleet_usage.exit_codes import ExitCode
from fleet_usage.github import AccessReport
from fleet_usage.models import Ledger, MergePolicy


@pytest.fixture(autouse=True)
def _offline_github(monkeypatch):
    """Answer every remote probe locally so no test touches GitHub."""
    report = AccessReport(
        repository='octo/data',
        exists=True,
        can_read=True,
        can_write=True,
        detail='read/write access confirmed',
    )
    monkeypatch.setattr(
        'fleet_usage.commands.doctor.check_access', lambda client: report
    )
    monkeypatch.setattr(
        'fleet_usage.commands.doctor.fetch_ledger',
        lambda client, machine_id: None,
    )


def published_ledger(last_run_at: dt.datetime) -> Ledger:
    """A ledger whose last run happened at ``last_run_at``."""
    return Ledger(
        machine_id='m1',
        label='box',
        merge_policy=MergePolicy(
            freeze_window_days=5, timezone='Europe/Berlin'
        ),
        last_run_at=last_run_at,
    )


def test_doctor_without_settings(invoke):
    result = invoke('doctor')
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert 'settings' in result.output


def test_doctor_reports_missing_token_and_collector(invoke, monkeypatch):
    monkeypatch.setattr('shutil.which', lambda name: None)
    invoke('init', '--repo', 'octo/data')
    result = invoke('doctor')
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert 'fail' in result.output
    assert 'no usable token' in result.output


def test_doctor_passes_with_token_and_collector(
    invoke, app_paths, monkeypatch, tmp_path
):
    invoke('init', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=token\n', encoding='utf-8'
    )
    monkeypatch.setattr('shutil.which', lambda name: str(tmp_path / 'bunx'))

    class Completed:
        returncode = 0
        stdout = 'ccusage 20.0.20\n'
        stderr = ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: Completed())
    result = invoke('doctor')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'publisher' in result.output
    assert 'fail' not in result.output


def test_doctor_warns_on_version_mismatch(
    invoke, app_paths, monkeypatch, tmp_path
):
    invoke('init', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=token\n', encoding='utf-8'
    )
    monkeypatch.setattr('shutil.which', lambda name: str(tmp_path / 'bunx'))

    class Completed:
        returncode = 0
        stdout = 'ccusage 19.0.0\n'
        stderr = ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: Completed())
    result = invoke('doctor')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'warn' in result.output


def test_doctor_reports_viewer_only(invoke, app_paths, monkeypatch):
    invoke('init', '--viewer-only', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=token\n', encoding='utf-8'
    )
    result = invoke('doctor')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'viewer-only' in result.output


def test_doctor_warns_about_placeholder_token(
    invoke, app_paths, monkeypatch, tmp_path
):
    invoke('init', '--repo', 'octo/data')
    monkeypatch.setattr('shutil.which', lambda name: str(tmp_path / 'bunx'))

    class Completed:
        returncode = 0
        stdout = 'ccusage 20.0.20\n'
        stderr = ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: Completed())
    result = invoke('doctor')
    assert result.exit_code == ExitCode.OK, result.output
    assert 'placeholder' in result.output


def test_doctor_warns_when_the_local_clock_is_behind(
    invoke, app_paths, monkeypatch, tmp_path
):
    invoke('init', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=token\n', encoding='utf-8'
    )
    monkeypatch.setattr('shutil.which', lambda name: str(tmp_path / 'bunx'))

    class Completed:
        returncode = 0
        stdout = 'ccusage 20.0.20\n'
        stderr = ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: Completed())
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=2)
    monkeypatch.setattr(
        'fleet_usage.commands.doctor.fetch_ledger',
        lambda client, machine_id: published_ledger(future),
    )

    result = invoke('doctor')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'warn' in result.output
    assert 'local clock is behind' in result.output
    assert 'rebuild' in result.output


def test_doctor_is_happy_with_a_clock_that_is_ahead(
    invoke, app_paths, monkeypatch, tmp_path
):
    invoke('init', '--repo', 'octo/data')
    app_paths.env_file.write_text(
        'FLEET_USAGE_GITHUB_TOKEN=token\n', encoding='utf-8'
    )
    monkeypatch.setattr('shutil.which', lambda name: str(tmp_path / 'bunx'))

    class Completed:
        returncode = 0
        stdout = 'ccusage 20.0.20\n'
        stderr = ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: Completed())
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    monkeypatch.setattr(
        'fleet_usage.commands.doctor.fetch_ledger',
        lambda client, machine_id: published_ledger(past),
    )

    result = invoke('doctor')

    assert result.exit_code == ExitCode.OK, result.output
    assert 'local clock is behind' not in result.output
    assert 'last published run' in result.output
