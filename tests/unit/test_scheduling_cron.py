"""Crontab block management, idempotence and byte-for-byte preservation."""

import os
from pathlib import Path

import pytest

from fleet_usage.scheduling.base import LaunchSpec, RunResult, SchedulerError
from fleet_usage.scheduling.cron import (
    BEGIN_MARKER,
    END_MARKER,
    CronScheduler,
    env_prefix,
    find_block,
    render_block,
    replace_block,
    strip_block,
)

posix_only = pytest.mark.skipif(
    os.name == 'nt',
    reason='cron runs only on POSIX systems',
)


OFFSET = 7

# A realistic crontab: a shebang-less env block, comments, other jobs and
# no trailing newline on the last line.
EXISTING = (
    'SHELL=/bin/bash\n'
    'PATH=/usr/local/bin:/usr/bin:/bin\n'
    'MAILTO=""\n'
    '\n'
    '# nightly backup\n'
    '0 2 * * * /usr/local/bin/backup.sh --full\n'
    '\n'
    '*/5 * * * * /home/me/bin/ping-check\n'
    '@reboot /home/me/bin/warm-cache'
)


class FakeCrontab:
    """In-memory stand-in for the ``crontab`` program."""

    def __init__(self, initial=None, missing=False, list_fails=False):
        self.text = initial
        self.missing = missing
        self.list_fails = list_fails
        self.calls = []

    def __call__(self, argv, *, stdin=None):
        self.calls.append((tuple(argv), stdin))
        if tuple(argv) == ('crontab', '-l'):
            if self.list_fails:
                return RunResult(returncode=2, stderr='crontab: bad things')
            if self.text is None:
                return RunResult(
                    returncode=1,
                    stderr='no crontab for stebix\n',
                )
            return RunResult(returncode=0, stdout=self.text)
        if tuple(argv) == ('crontab', '-'):
            self.text = stdin
            return RunResult(returncode=0)
        raise AssertionError(f'unexpected call {argv}')

    def which(self, name):
        if name == 'crontab' and self.missing:
            return None
        return f'/usr/bin/{name}'


def make_spec(tmp_path, interval=60, settings=None, extra_path_dirs=()):
    settings_path = settings or (tmp_path / 'settings.toml')
    return LaunchSpec(
        executable=Path('/opt/bin/fleet-usage'),
        args=('--config', str(settings_path), 'publish', '--if-due'),
        settings_path=Path(settings_path),
        interval_minutes=interval,
        log_dir=tmp_path / 'logs',
        extra_path_dirs=tuple(extra_path_dirs),
    )


def make_scheduler(tmp_path, fake, **spec_kwargs):
    return CronScheduler(
        make_spec(tmp_path, **spec_kwargs),
        runner=fake,
        which=fake.which,
        minute_offset=OFFSET,
    )


# --------------------------------------------------------- pure functions


@posix_only
def test_render_block_shape(tmp_path):
    block = render_block(make_spec(tmp_path), OFFSET)
    lines = block.splitlines()
    assert lines[0] == BEGIN_MARKER
    assert lines[2] == END_MARKER
    assert block.endswith('\n')
    assert lines[1].startswith('7 * * * * /opt/bin/fleet-usage ')
    assert lines[1].endswith(f'>> {tmp_path / "logs" / "cron.log"} 2>&1')
    assert '--if-due' in lines[1]


def test_render_block_quotes_awkward_paths(tmp_path):
    spec = make_spec(tmp_path, settings="/etc/my conf/set'tings.toml")
    line = render_block(spec, OFFSET).splitlines()[1]
    assert "'/etc/my conf/set'\"'\"'tings.toml'" in line


def test_find_and_strip_on_a_crontab_without_a_block():
    assert find_block(EXISTING) is None
    assert strip_block(EXISTING) == EXISTING


def test_insert_preserves_everything_else(tmp_path):
    block = render_block(make_spec(tmp_path), OFFSET)
    updated = replace_block(EXISTING, block)
    # Only a terminating newline is added to the previous last line.
    assert updated == EXISTING + '\n' + block
    assert strip_block(updated) == EXISTING + '\n'
    assert find_block(updated) == block


def test_insert_into_an_empty_crontab(tmp_path):
    block = render_block(make_spec(tmp_path), OFFSET)
    assert replace_block('', block) == block


def test_replacement_happens_in_place(tmp_path):
    first = render_block(make_spec(tmp_path), OFFSET)
    middle = 'A=1\n' + first + '# tail comment\n0 4 * * * /bin/true\n'
    second = render_block(make_spec(tmp_path, interval=120), OFFSET)
    updated = replace_block(middle, second)
    assert (
        updated == 'A=1\n' + second + '# tail comment\n0 4 * * * /bin/true\n'
    )
    assert first not in updated


def test_strip_leaves_the_rest_byte_for_byte(tmp_path):
    block = render_block(make_spec(tmp_path), OFFSET)
    combined = EXISTING + '\n' + block
    assert strip_block(combined) == EXISTING + '\n'


def test_block_without_end_marker_is_not_recognised():
    broken = f'{BEGIN_MARKER}\n7 * * * * /bin/true\n'
    assert find_block(broken) is None
    assert strip_block(broken) == broken


# -------------------------------------------------------- collector PATH

BUN_DIR = '/home/me/.bun/bin'
BUN_PATH = 'env PATH=/home/me/.bun/bin:/usr/local/bin:/usr/bin:/bin'


def job_command(block):
    """Return the job line of ``block`` without its five time fields."""
    return block.splitlines()[1].split(None, 5)[5]


def test_no_env_prefix_without_extra_directories(tmp_path):
    assert env_prefix(make_spec(tmp_path)) == ''
    assert 'env PATH=' not in render_block(make_spec(tmp_path), OFFSET)


@posix_only
def test_job_carries_the_collector_directory(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=(BUN_DIR,))
    assert env_prefix(spec) == f'{BUN_PATH} '
    command = job_command(render_block(spec, OFFSET))
    assert command.startswith(f'{BUN_PATH} /opt/bin/fleet-usage ')
    assert command.endswith(f'>> {tmp_path / "logs" / "cron.log"} 2>&1')


def test_a_standard_directory_is_never_repeated(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=('/usr/bin',))
    assert env_prefix(spec) == 'env PATH=/usr/bin:/usr/local/bin:/bin '


def test_env_prefix_quotes_a_directory_with_a_space(tmp_path):
    spec = make_spec(tmp_path, extra_path_dirs=('/home/me/my tools/bin',))
    assert env_prefix(spec) == (
        "env 'PATH=/home/me/my tools/bin:/usr/local/bin:/usr/bin:/bin' "
    )


def test_the_crontab_never_gets_a_bare_path_assignment(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake, extra_path_dirs=(BUN_DIR,))
    scheduler.install(60)
    # A ``PATH=`` line in a crontab applies to every job below it, so the
    # managed block must never add one: the only one is the foreign line.
    assignments = [
        line for line in fake.text.splitlines() if line.startswith('PATH=')
    ]
    assert assignments == ['PATH=/usr/local/bin:/usr/bin:/bin']
    assert strip_block(fake.text) == EXISTING + '\n'
    assert BUN_PATH in find_block(fake.text)


# ------------------------------------------------------------- scheduler


def test_install_writes_the_expected_crontab(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    report = scheduler.install(60)
    assert 'added' in report
    assert fake.text == EXISTING + '\n' + render_block(
        make_spec(tmp_path), OFFSET
    )
    assert (tmp_path / 'logs').is_dir()


def test_install_is_idempotent(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    after_first = fake.text
    writes = sum(1 for call, _ in fake.calls if call == ('crontab', '-'))
    scheduler.install(60)
    assert fake.text == after_first
    # Nothing changed, so no second write happens.
    assert (
        sum(1 for call, _ in fake.calls if call == ('crontab', '-')) == writes
    )
    assert 'replaced' in scheduler.install(60)


def test_reinstall_with_a_new_interval_replaces_only_the_block(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    scheduler.install(120)
    assert strip_block(fake.text) == EXISTING + '\n'
    assert '7 */2 * * *' in find_block(fake.text)


def test_uninstall_leaves_the_rest_intact(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    report = scheduler.uninstall()
    assert 'removed' in report
    assert fake.text == EXISTING + '\n'


def test_uninstall_without_a_block_does_nothing(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    assert 'nothing to do' in scheduler.uninstall()
    assert fake.text == EXISTING
    assert not any(call == ('crontab', '-') for call, _ in fake.calls)


def test_no_crontab_for_user_counts_as_empty(tmp_path):
    fake = FakeCrontab(None)
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    assert fake.text == render_block(make_spec(tmp_path), OFFSET)


def test_a_real_crontab_error_is_reported(tmp_path):
    fake = FakeCrontab(EXISTING, list_fails=True)
    scheduler = make_scheduler(tmp_path, fake)
    with pytest.raises(SchedulerError) as excinfo:
        scheduler.install(60)
    assert 'cannot read the crontab' in str(excinfo.value)


def test_missing_crontab_binary(tmp_path):
    fake = FakeCrontab(EXISTING, missing=True)
    scheduler = make_scheduler(tmp_path, fake)
    assert scheduler.available() is False
    with pytest.raises(SchedulerError) as excinfo:
        scheduler.install(60)
    assert "no 'crontab' program on PATH" in str(excinfo.value)
    assert scheduler.status().installed is False


def test_dry_run_changes_nothing(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    report = scheduler.install(60, dry_run=True)
    assert report.startswith('dry run:')
    assert BEGIN_MARKER in report
    assert fake.text == EXISTING
    assert not (tmp_path / 'logs').exists()


def test_dry_run_uninstall_keeps_the_block(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    scheduler.install(60)
    before = fake.text
    assert scheduler.uninstall(dry_run=True).startswith('dry run:')
    assert fake.text == before


@posix_only
def test_status_reports_the_expression_and_command(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    assert scheduler.status().installed is False
    scheduler.install(120)
    status = scheduler.status()
    assert status.installed is True
    assert status.backend == 'cron'
    assert status.expression == '7 */2 * * *'
    assert status.command.startswith('/opt/bin/fleet-usage')
    assert 'cron.log' in status.render()


def test_install_rejects_an_unsupported_interval(tmp_path):
    fake = FakeCrontab(EXISTING)
    scheduler = make_scheduler(tmp_path, fake)
    with pytest.raises(SchedulerError):
        scheduler.install(90)
    assert fake.text == EXISTING
