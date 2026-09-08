"""Scheduler backend based on the user crontab.

The crontab is a shared, hand edited file: other jobs, environment
assignments and comments live in it and must survive untouched. This
backend therefore owns exactly one delimited block and treats everything
outside it as opaque bytes -- it is copied through unchanged, in place,
including a missing trailing newline on the last foreign line.
"""

from fleet_usage.scheduling.base import (
    CRON_PATH_DIRS,
    LaunchSpec,
    Scheduler,
    SchedulerError,
    ScheduleStatus,
    cron_expression,
    cron_quote,
    path_value,
)

__all__ = [
    'BEGIN_MARKER',
    'END_MARKER',
    'CronScheduler',
    'env_prefix',
    'find_block',
    'render_block',
    'replace_block',
    'strip_block',
]

BEGIN_MARKER = '# BEGIN fleet-usage (managed; do not edit)'
END_MARKER = '# END fleet-usage'
LOG_NAME = 'cron.log'
_EMPTY_MESSAGES = ('no crontab for', 'no crontab')


def _split(text: str) -> list[str]:
    """Split ``text`` into lines, keeping their line endings.

    Parameters
    ----------
    text : str
        Crontab contents.

    Returns
    -------
    list of str
        Lines including their terminators, so that joining them
        reproduces the input byte for byte.
    """
    return text.splitlines(keepends=True)


def _block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Locate the managed block inside ``lines``.

    Parameters
    ----------
    lines : list of str
        Crontab lines with terminators.

    Returns
    -------
    tuple of int or None
        Half-open ``(start, stop)`` index range of the block, or
        ``None`` when no complete block is present.
    """
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == BEGIN_MARKER:
            start = index
        elif stripped == END_MARKER and start is not None:
            return (start, index + 1)
    return None


def find_block(text: str) -> str | None:
    """Return the managed block of a crontab, if it has one.

    Parameters
    ----------
    text : str
        Crontab contents.

    Returns
    -------
    str or None
        The block including both markers, or ``None``.
    """
    lines = _split(text)
    bounds = _block_bounds(lines)
    if bounds is None:
        return None
    start, stop = bounds
    return ''.join(lines[start:stop])


def strip_block(text: str) -> str:
    """Return ``text`` without the managed block.

    Parameters
    ----------
    text : str
        Crontab contents.

    Returns
    -------
    str
        Everything outside the block, unchanged.
    """
    lines = _split(text)
    bounds = _block_bounds(lines)
    if bounds is None:
        return text
    start, stop = bounds
    return ''.join(lines[:start] + lines[stop:])


def replace_block(text: str, block: str) -> str:
    """Insert or replace the managed block in ``text``.

    An existing block is replaced where it stands, so that a reinstall
    never reorders the file. A new block is appended at the end, after
    making sure the previous last line is terminated -- cron rejects a
    file whose final line has no newline.

    Parameters
    ----------
    text : str
        Current crontab contents.
    block : str
        The rendered block, ending in a newline.

    Returns
    -------
    str
        The new crontab contents.
    """
    lines = _split(text)
    bounds = _block_bounds(lines)
    if bounds is not None:
        start, stop = bounds
        return ''.join(lines[:start]) + block + ''.join(lines[stop:])
    prefix = text
    if prefix and not prefix.endswith('\n'):
        prefix += '\n'
    return prefix + block


def env_prefix(spec: LaunchSpec) -> str:
    """Render the ``env`` prefix that widens the ``PATH`` of the job.

    cron starts a job with a ``PATH`` of ``/usr/bin:/bin``, so a
    collector installed under the home directory is invisible to it. The
    directory is prepended through an explicit ``env`` invocation rather
    than through a ``PATH=`` line in the crontab: such a line applies to
    every job below it in the file, and this backend must not change how
    foreign jobs run.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.

    Returns
    -------
    str
        ``'env PATH=... '`` including the trailing space, or an empty
        string when the job needs nothing beyond the default
        directories.
    """
    if not spec.extra_path_dirs:
        return ''
    value = path_value(spec.extra_path_dirs, CRON_PATH_DIRS)
    return f'env {cron_quote(f"PATH={value}")} '


def render_block(spec: LaunchSpec, offset: int) -> str:
    """Render the managed crontab block for ``spec``.

    Parameters
    ----------
    spec : LaunchSpec
        The job to schedule.
    offset : int
        Minute within the hour at which this machine runs.

    Returns
    -------
    str
        The two markers with the job line between them, ending in a
        newline.

    Raises
    ------
    SchedulerError
        If the interval cannot be expressed in cron.
    """
    expression = cron_expression(spec.interval_minutes, offset)
    log_file = cron_quote(str(spec.log_dir / LOG_NAME))
    command = f'{env_prefix(spec)}{spec.shell_command()}'
    job = f'{expression} {command} >> {log_file} 2>&1'
    return f'{BEGIN_MARKER}\n{job}\n{END_MARKER}\n'


def _job_line(block: str) -> str | None:
    """Return the job line of a rendered block.

    Parameters
    ----------
    block : str
        A managed block.

    Returns
    -------
    str or None
        The first line that is neither marker nor blank.
    """
    for line in block.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            return stripped
    return None


class CronScheduler(Scheduler):
    """Manage a ``crontab`` entry that runs ``publish --if-due``."""

    name = 'cron'

    def available(self) -> bool:
        """Whether ``crontab`` is on the PATH.

        Returns
        -------
        bool
            ``True`` when the binary exists.
        """
        return self.which('crontab') is not None

    def _require_crontab(self) -> None:
        """Fail with advice when ``crontab`` is missing.

        Raises
        ------
        SchedulerError
            If no ``crontab`` program is on the PATH.
        """
        if self.available():
            return
        msg = (
            "no 'crontab' program on PATH\n"
            'install a cron implementation (cronie, vixie-cron, ...) or '
            'choose another backend with --backend'
        )
        raise SchedulerError(msg)

    def read_crontab(self) -> str:
        """Return the current user crontab.

        Returns
        -------
        str
            The crontab contents; an empty string when the user has no
            crontab yet.

        Raises
        ------
        SchedulerError
            If ``crontab -l`` fails for any other reason.
        """
        self._require_crontab()
        result = self.run(['crontab', '-l'])
        if result.ok:
            return result.stdout
        text = (result.stderr + result.stdout).lower()
        if any(marker in text for marker in _EMPTY_MESSAGES):
            return ''
        msg = f'cannot read the crontab: {result.message()}'
        raise SchedulerError(msg)

    def write_crontab(self, text: str) -> None:
        """Replace the user crontab with ``text``.

        Parameters
        ----------
        text : str
            The complete new crontab.

        Raises
        ------
        SchedulerError
            If ``crontab -`` rejects the input.
        """
        self._require_crontab()
        result = self.run(['crontab', '-'], stdin=text)
        if not result.ok:
            msg = f'cannot write the crontab: {result.message()}'
            raise SchedulerError(msg)

    def install(self, interval_minutes: int, dry_run: bool = False) -> str:
        """Add or replace the managed crontab block.

        Parameters
        ----------
        interval_minutes : int
            Desired interval between runs.
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of the planned or performed change.

        Raises
        ------
        SchedulerError
            If cron is unavailable or refuses the new crontab.
        """
        spec = self._spec_for(interval_minutes)
        block = render_block(spec, self.offset)
        current = self.read_crontab()
        updated = replace_block(current, block)
        present = find_block(current) is not None
        verb, done = ('replace', 'replaced') if present else ('add', 'added')
        if dry_run:
            return (
                f'dry run: would {verb} the managed block in the user '
                f'crontab:\n{block}'
                f'output would be appended to {spec.log_dir / LOG_NAME}'
            )
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        if updated != current:
            self.write_crontab(updated)
        return (
            f'{done} the managed block in the user crontab:\n{block}'
            f'output is appended to {spec.log_dir / LOG_NAME}'
        )

    def status(self) -> ScheduleStatus:
        """Report whether the managed crontab block exists.

        Returns
        -------
        ScheduleStatus
            The observed state.
        """
        if not self.available():
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail="no 'crontab' program on PATH",
            )
        current = self.read_crontab()
        block = find_block(current)
        if block is None:
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail="run 'fleet-usage schedule install' to add it",
            )
        line = _job_line(block)
        if line is None:
            return ScheduleStatus(
                installed=False,
                backend=self.name,
                detail='the managed block is present but holds no job',
            )
        fields = line.split(None, 5)
        expression = ' '.join(fields[:5])
        command = fields[5] if len(fields) > 5 else ''
        return ScheduleStatus(
            installed=True,
            backend=self.name,
            detail=f'log file: {self.spec.log_dir / LOG_NAME}',
            expression=expression,
            command=command,
        )

    def uninstall(self, dry_run: bool = False) -> str:
        """Remove the managed crontab block, leaving the rest intact.

        Parameters
        ----------
        dry_run : bool, optional
            Only describe what would happen.

        Returns
        -------
        str
            A description of the planned or performed change.

        Raises
        ------
        SchedulerError
            If cron is unavailable or refuses the new crontab.
        """
        current = self.read_crontab()
        if find_block(current) is None:
            return 'no managed block in the user crontab; nothing to do'
        updated = strip_block(current)
        if dry_run:
            return 'dry run: would remove the managed block from the crontab'
        self.write_crontab(updated)
        return 'removed the managed block from the user crontab'
