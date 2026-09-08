"""Settings model, loading, redaction and the ``init`` template.

Settings live in ``settings.toml`` inside the platform config directory.
The file is located with an explicit precedence:

1. the ``--config`` command line option,
2. the ``FLEET_USAGE_CONFIG`` environment variable,
3. the platform default from :mod:`fleet_usage.paths`.

A ``.env`` file is read only from the directory of the resolved settings
file, never from the current working directory, and never overrides a
variable that is already present in the process environment.
"""

import os
import re
import tomllib
import zoneinfo
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from fleet_usage.paths import get_paths

__all__ = [
    'CONFIG_ENV_VAR',
    'REDACTED',
    'CollectorSettings',
    'ConfigError',
    'GitHubSettings',
    'LedgerSettings',
    'MachineSettings',
    'ReportSettings',
    'ScheduleSettings',
    'Settings',
    'SyncSettings',
    'load_env_values',
    'load_settings',
    'redacted_dict',
    'render_env_template',
    'render_settings_toml',
    'resolve_settings_path',
    'resolve_token',
]

CONFIG_ENV_VAR = 'FLEET_USAGE_CONFIG'
DEFAULT_TOKEN_ENV = 'FLEET_USAGE_GITHUB_TOKEN'
DEFAULT_API_BASE_URL = 'https://api.github.com'
DEFAULT_TIMEZONE = 'Europe/Berlin'
REDACTED = '***'
SECRET_KEY_MARKERS = ('token', 'secret')
_REPOSITORY_RE = re.compile(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$')
_MACHINE_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


class ConfigError(Exception):
    """Raised when settings are missing, malformed or incomplete.

    The message is written for a human operator and always names the
    file involved plus the action that fixes the problem. Commands turn
    it into exit code :attr:`~fleet_usage.exit_codes.ExitCode.CONFIG_ERROR`.
    """


class _Section(BaseModel):
    """Base class for settings sections; unknown keys are rejected."""

    model_config = ConfigDict(extra='forbid')


def _validate_timezone(value: str) -> str:
    """Check that ``value`` names an IANA timezone.

    Parameters
    ----------
    value : str
        Timezone name, for example ``'Europe/Berlin'``.

    Returns
    -------
    str
        The unchanged value.

    Raises
    ------
    ValueError
        If the timezone database does not know the name.
    """
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        msg = f'unknown timezone {value!r}'
        raise ValueError(msg) from exc
    return value


class MachineSettings(_Section):
    """Identity of the machine that publishes snapshots."""

    id: str
    label: str

    @field_validator('id', 'label')
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Reject empty identifiers and labels.

        Parameters
        ----------
        value : str
            The configured value.

        Returns
        -------
        str
            The stripped value.

        Raises
        ------
        ValueError
            If the value is blank.
        """
        stripped = value.strip()
        if not stripped:
            msg = 'must not be empty'
            raise ValueError(msg)
        return stripped

    @field_validator('id')
    @classmethod
    def _safe_id(cls, value: str) -> str:
        """Reject identifiers that cannot be used in a remote path.

        The identifier becomes a path segment below ``snapshots/`` and
        ``machines/`` in the data repository, so anything a path could
        reinterpret, in particular a separator or a relative component,
        has to be refused before it reaches the remote.

        Parameters
        ----------
        value : str
            The configured identifier, already stripped.

        Returns
        -------
        str
            The unchanged identifier.

        Raises
        ------
        ValueError
            If the identifier is not one to 64 characters of letters,
            digits, dot, underscore or hyphen, or is a relative path
            component.
        """
        if value in {'.', '..'} or not _MACHINE_ID_RE.match(value):
            msg = (
                'must be 1 to 64 characters of letters, digits, '
                f"'.', '_' or '-' and not '.' or '..', got {value!r}"
            )
            raise ValueError(msg)
        return value


class GitHubSettings(_Section):
    """Coordinates of the private data repository."""

    repository: str
    branch: str = 'main'
    token_env: str = DEFAULT_TOKEN_ENV
    api_base_url: str = DEFAULT_API_BASE_URL

    @field_validator('repository')
    @classmethod
    def _check_repository(cls, value: str) -> str:
        """Validate the ``OWNER/NAME`` shape.

        Parameters
        ----------
        value : str
            The configured repository.

        Returns
        -------
        str
            The unchanged value.

        Raises
        ------
        ValueError
            If the value is not ``OWNER/NAME``.
        """
        if not _REPOSITORY_RE.match(value):
            msg = f"expected 'OWNER/NAME', got {value!r}"
            raise ValueError(msg)
        return value


class CollectorSettings(_Section):
    """How to invoke ``ccusage`` and what to expect from it."""

    command: list[str] = Field(min_length=1)
    version: str | None = None
    agents: list[str] = Field(default_factory=list)
    timezone: str = DEFAULT_TIMEZONE
    timeout_seconds: int = Field(default=180, gt=0)
    offline_pricing: bool = True

    @field_validator('timezone')
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """Validate the reporting timezone.

        Parameters
        ----------
        value : str
            IANA timezone name.

        Returns
        -------
        str
            The unchanged value.
        """
        return _validate_timezone(value)


class LedgerSettings(_Section):
    """Merge behaviour of the per-machine ledger."""

    freeze_window_days: int = Field(default=5, ge=0)


class SyncSettings(_Section):
    """How often a machine is expected to publish."""

    interval_minutes: int = Field(default=60, gt=0)


class ScheduleSettings(_Section):
    """Which scheduler backend to install."""

    backend: Literal['auto', 'cron', 'systemd', 'windows'] = 'auto'


class ReportSettings(_Section):
    """Defaults for ``fleet-usage show``."""

    default_period: Literal['day', 'week', 'month', 'all'] = 'month'
    stale_after_hours: int = Field(default=3, gt=0)


class Settings(_Section):
    """The complete contents of ``settings.toml``.

    ``machine`` and ``collector`` are optional so that a viewer-only
    installation can read the fleet without being able to publish.
    """

    schema_version: int = 1
    machine: MachineSettings | None = None
    github: GitHubSettings
    collector: CollectorSettings | None = None
    ledger: LedgerSettings = Field(default_factory=LedgerSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)

    source_path: Path | None = Field(default=None, exclude=True)
    env_values: dict[str, str] = Field(default_factory=dict, exclude=True)

    @property
    def viewer_only(self) -> bool:
        """Whether this installation can only read the fleet.

        Returns
        -------
        bool
            ``True`` when either the machine identity or the collector
            configuration is absent.
        """
        return self.machine is None or self.collector is None

    @property
    def timezone(self) -> str:
        """Reporting timezone of this installation.

        Returns
        -------
        str
            The collector timezone, or the default when no collector is
            configured.
        """
        if self.collector is not None:
            return self.collector.timezone
        return DEFAULT_TIMEZONE


def resolve_settings_path(
    explicit: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path:
    """Locate the settings file.

    Parameters
    ----------
    explicit : pathlib.Path or None, optional
        Value of the ``--config`` option.
    env : dict of str to str or None, optional
        Environment to inspect; defaults to :data:`os.environ`.

    Returns
    -------
    pathlib.Path
        The settings path with the documented precedence applied.
    """
    if explicit is not None:
        return explicit.expanduser()
    environ = os.environ if env is None else env
    from_env = environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return get_paths().settings_file


def load_env_values(settings_path: Path) -> dict[str, str]:
    """Read the ``.env`` file that sits next to the settings file.

    The current working directory is never consulted: a scheduled run
    starts in an arbitrary directory and picking up a stray ``.env``
    there would be both surprising and unsafe.

    Parameters
    ----------
    settings_path : pathlib.Path
        Path of the settings file, existing or not.

    Returns
    -------
    dict of str to str
        Variables defined in the file; empty when there is no file.
    """
    env_file = settings_path.parent / '.env'
    if not env_file.is_file():
        return {}
    values = dotenv_values(env_file)
    return {key: value for key, value in values.items() if value is not None}


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate the settings.

    Parameters
    ----------
    path : pathlib.Path or None, optional
        Explicit settings path, usually from ``--config``.

    Returns
    -------
    Settings
        The validated settings, carrying the resolved source path and
        the values of the neighbouring ``.env`` file.

    Raises
    ------
    ConfigError
        If the file is missing, is not valid TOML or does not satisfy
        the schema. The message names the file and the fix.
    """
    settings_path = resolve_settings_path(path)
    if not settings_path.is_file():
        msg = (
            f'no settings file at {settings_path}\n'
            "run 'fleet-usage init' to create one, or point --config / "
            f'{CONFIG_ENV_VAR} at an existing file'
        )
        raise ConfigError(msg)
    try:
        with settings_path.open('rb') as stream:
            raw: dict[str, Any] = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        msg = f'{settings_path} is not valid TOML: {exc}'
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f'cannot read {settings_path}: {exc}'
        raise ConfigError(msg) from exc
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        msg = f'{settings_path} is invalid:\n{_format_errors(exc)}'
        raise ConfigError(msg) from exc
    settings.source_path = settings_path
    settings.env_values = load_env_values(settings_path)
    return settings


def _format_errors(exc: ValidationError) -> str:
    """Turn a pydantic error into operator readable lines.

    Parameters
    ----------
    exc : pydantic.ValidationError
        The validation failure.

    Returns
    -------
    str
        One ``key: message`` line per problem.
    """
    lines = []
    for error in exc.errors():
        location = '.'.join(str(part) for part in error['loc']) or '<root>'
        lines.append(f'  {location}: {error["msg"]}')
    return '\n'.join(lines)


def resolve_token(settings: Settings) -> str | None:
    """Return the GitHub token for ``settings``.

    The process environment wins over the ``.env`` file, so that a
    scheduler or a secret manager can always override a file on disk.

    Parameters
    ----------
    settings : Settings
        Loaded settings.

    Returns
    -------
    str or None
        The token, or ``None`` when neither source defines it.
    """
    name = settings.github.token_env
    value = os.environ.get(name)
    if value is None:
        value = settings.env_values.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _is_secret_key(key: str) -> bool:
    """Whether a settings key must be masked.

    Parameters
    ----------
    key : str
        A settings key.

    Returns
    -------
    bool
        ``True`` when the key mentions a token or a secret.
    """
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def _redact(value: Any) -> Any:
    """Recursively mask secret-looking keys.

    Parameters
    ----------
    value : Any
        A nested structure of dicts, lists and scalars.

    Returns
    -------
    Any
        The same structure with masked values.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redacted_dict(settings: Settings) -> dict[str, Any]:
    """Return the settings as a plain dict safe for display.

    Any value whose key mentions a token or a secret is replaced by
    :data:`REDACTED`. The resolved token itself is never part of the
    result, because it is not a settings field to begin with.

    Parameters
    ----------
    settings : Settings
        Loaded settings.

    Returns
    -------
    dict
        JSON-ready, redacted representation.
    """
    payload = settings.model_dump(mode='json', exclude_none=False)
    redacted = _redact(payload)
    return cast('dict[str, Any]', redacted)


def _toml_string(value: str) -> str:
    """Quote ``value`` as a single quoted TOML string.

    Parameters
    ----------
    value : str
        Any text without single quotes, newlines or control characters.

    Returns
    -------
    str
        The quoted literal.

    Raises
    ------
    ValueError
        If the value cannot be represented as a literal string.
    """
    if "'" in value or '\n' in value:
        msg = f'cannot render {value!r} as a TOML literal string'
        raise ValueError(msg)
    return f"'{value}'"


def _toml_array(values: list[str]) -> str:
    """Render a list of strings as a TOML array.

    Parameters
    ----------
    values : list of str
        The items.

    Returns
    -------
    str
        For example ``['bunx', 'ccusage@20.0.20']``.
    """
    return '[' + ', '.join(_toml_string(item) for item in values) + ']'


def render_settings_toml(
    *,
    machine_id: str | None,
    label: str | None,
    repository: str,
    branch: str = 'main',
    token_env: str = DEFAULT_TOKEN_ENV,
    api_base_url: str = DEFAULT_API_BASE_URL,
    collector_command: list[str] | None = None,
    collector_version: str | None = None,
    agents: list[str] | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    timeout_seconds: int = 180,
    offline_pricing: bool = True,
    freeze_window_days: int = 5,
    interval_minutes: int = 60,
    schedule_backend: str = 'auto',
    default_period: str = 'month',
    stale_after_hours: int = 3,
) -> str:
    """Render a ``settings.toml`` document.

    A hand written template is used on purpose: the file is meant to be
    edited by a human, so comments and section order matter more than
    round-tripping, and no TOML writer dependency is needed.

    Parameters
    ----------
    machine_id : str or None
        Machine UUID; omit the ``[machine]`` section when ``None``.
    label : str or None
        Human readable machine name.
    repository : str
        ``OWNER/NAME`` of the private data repository.
    branch : str, optional
        Branch to write to.
    token_env : str, optional
        Name of the environment variable holding the GitHub token.
    api_base_url : str, optional
        GitHub API base URL.
    collector_command : list of str or None, optional
        Argument vector that launches ``ccusage``; omit the
        ``[collector]`` section when ``None``.
    collector_version : str or None, optional
        Expected collector version.
    agents : list of str or None, optional
        Agents to record.
    timezone : str, optional
        Reporting timezone.
    timeout_seconds : int, optional
        Collector timeout.
    offline_pricing : bool, optional
        Whether to pass ``--offline``.
    freeze_window_days : int, optional
        Days after which a day record is settled.
    interval_minutes : int, optional
        Expected publish interval.
    schedule_backend : str, optional
        Scheduler backend.
    default_period : str, optional
        Default reporting period.
    stale_after_hours : int, optional
        Age after which a machine counts as stale.

    Returns
    -------
    str
        The rendered document, ending in a newline.
    """
    blocks: list[str] = ['schema_version = 1\n']
    if machine_id is not None and label is not None:
        blocks.append(
            '[machine]\n'
            f'id = {_toml_string(machine_id)}\n'
            f'label = {_toml_string(label)}\n'
        )
    blocks.append(
        '[github]\n'
        f'repository = {_toml_string(repository)}\n'
        f'branch = {_toml_string(branch)}\n'
        f'token_env = {_toml_string(token_env)}\n'
        f'api_base_url = {_toml_string(api_base_url)}\n'
    )
    if collector_command is not None:
        version_line = (
            f'version = {_toml_string(collector_version)}\n'
            if collector_version is not None
            else ''
        )
        blocks.append(
            '[collector]\n'
            f'command = {_toml_array(collector_command)}\n'
            f'{version_line}'
            f'agents = {_toml_array(agents or [])}\n'
            f'timezone = {_toml_string(timezone)}\n'
            f'timeout_seconds = {timeout_seconds}\n'
            f'offline_pricing = {str(offline_pricing).lower()}\n'
        )
    blocks.append(f'[ledger]\nfreeze_window_days = {freeze_window_days}\n')
    blocks.append(f'[sync]\ninterval_minutes = {interval_minutes}\n')
    blocks.append(f'[schedule]\nbackend = {_toml_string(schedule_backend)}\n')
    blocks.append(
        '[report]\n'
        f'default_period = {_toml_string(default_period)}\n'
        f'stale_after_hours = {stale_after_hours}\n'
    )
    return '\n'.join(blocks)


def render_env_template(token_env: str = DEFAULT_TOKEN_ENV) -> str:
    """Render the placeholder ``.env`` file.

    Parameters
    ----------
    token_env : str, optional
        Name of the variable that must hold the GitHub token.

    Returns
    -------
    str
        The file contents, ending in a newline.
    """
    return (
        '# GitHub token with access to the private data repository.\n'
        '# Fine-grained token, contents: read and write.\n'
        f'{token_env}=replace-me\n'
    )
