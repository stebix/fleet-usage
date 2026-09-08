"""Platform specific locations used by ``fleet-usage``.

All locations are derived from :mod:`platformdirs` with the application
name ``fleet-usage``. On Linux this honours the XDG base directory
variables; on macOS and Windows the platform conventions are used. Every
path below is distinct on every supported platform, even where the
platform collapses several base directories into one.
"""

import dataclasses
from pathlib import Path
from typing import Literal

from platformdirs import PlatformDirs

__all__ = ['APP_AUTHOR', 'APP_NAME', 'AppPaths', 'get_paths']

APP_NAME = 'fleet-usage'
APP_AUTHOR: Literal[False] = False


@dataclasses.dataclass(frozen=True, slots=True)
class AppPaths:
    """Filesystem locations owned by the application.

    Attributes
    ----------
    config_dir
        Directory holding ``settings.toml`` and ``.env``.
    settings_file
        The default settings file.
    env_file
        The ``.env`` file that is read next to the settings file.
    state_dir
        Mutable state that must survive reboots but is reproducible.
    spool_dir
        Snapshots that were built but not yet uploaded.
    lock_file
        Inter-process lock guarding a publish run.
    due_marker
        Small marker file recording the last successful publish.
    cache_dir
        Re-creatable downloads.
    ledger_cache_dir
        Cached copies of the remote per-machine ledgers.
    log_dir
        Rotating log files.
    """

    config_dir: Path
    settings_file: Path
    env_file: Path
    state_dir: Path
    spool_dir: Path
    lock_file: Path
    due_marker: Path
    cache_dir: Path
    ledger_cache_dir: Path
    log_dir: Path

    def as_dict(self) -> dict[str, Path]:
        """Return every path keyed by its field name.

        Returns
        -------
        dict of str to pathlib.Path
            Mapping from attribute name to location.
        """
        return dataclasses.asdict(self)


def get_paths() -> AppPaths:
    """Compute the application paths for the current platform.

    Returns
    -------
    AppPaths
        The resolved locations. Directories are not created here; the
        commands that write into them create them on demand.
    """
    dirs = PlatformDirs(appname=APP_NAME, appauthor=APP_AUTHOR, roaming=False)
    config_dir = Path(dirs.user_config_dir)
    state_dir = Path(dirs.user_state_dir)
    cache_dir = Path(dirs.user_cache_dir)
    log_dir = Path(dirs.user_log_dir)
    if state_dir == config_dir:
        # Windows and macOS collapse the config and state base
        # directories into one. Keep mutable state in a dedicated
        # subdirectory so that no two locations ever coincide.
        state_dir = state_dir / 'state'
    return AppPaths(
        config_dir=config_dir,
        settings_file=config_dir / 'settings.toml',
        env_file=config_dir / '.env',
        state_dir=state_dir,
        spool_dir=state_dir / 'spool',
        lock_file=state_dir / 'publish.lock',
        due_marker=state_dir / 'last-run.json',
        cache_dir=cache_dir,
        ledger_cache_dir=cache_dir / 'ledgers',
        log_dir=log_dir,
    )
