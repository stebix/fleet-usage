"""Local spool of snapshots that are built but not yet uploaded.

Snapshots are written with a temporary file plus an atomic replace so
that a crash never leaves a half written document behind, and they are
drained in file name order, which is chronological by construction.

A crash between the write and the replace leaves a ``.tmp`` file, which
is never listed and is deleted once it is older than
:data:`TMP_MAX_AGE_SECONDS`; a run in progress therefore keeps its own
temporary file, while abandoned ones eventually disappear.
"""

import os
import time
from pathlib import Path

from fleet_usage.models import Snapshot
from fleet_usage.snapshot import snapshot_file_name

__all__ = [
    'SNAPSHOT_SUFFIX',
    'TMP_MAX_AGE_SECONDS',
    'TMP_SUFFIX',
    'list_spool',
    'read_snapshot',
    'remove',
    'write_snapshot',
]

SNAPSHOT_SUFFIX = '.json'
TMP_SUFFIX = '.tmp'
TMP_MAX_AGE_SECONDS = 3600


def write_snapshot(spool_dir: Path, snapshot: Snapshot) -> Path:
    """Write ``snapshot`` into the spool directory atomically.

    The document is first written to a sibling ``.tmp`` file, flushed to
    the storage device and only then moved into place with
    :func:`os.replace`, which is atomic on every supported platform. A
    reader therefore never observes a partially written snapshot.

    Parameters
    ----------
    spool_dir : pathlib.Path
        The spool directory; created when missing.
    snapshot : Snapshot
        The snapshot to persist.

    Returns
    -------
    pathlib.Path
        Path of the spooled file.
    """
    spool_dir.mkdir(parents=True, exist_ok=True)
    target = spool_dir / snapshot_file_name(snapshot)
    temporary = target.with_name(target.name + TMP_SUFFIX)
    payload = snapshot.to_pretty_json()
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    return target


def _clean_stale_temporaries(spool_dir: Path) -> None:
    """Delete abandoned ``.tmp`` files from a crashed run.

    Parameters
    ----------
    spool_dir : pathlib.Path
        The spool directory.
    """
    deadline = time.time() - TMP_MAX_AGE_SECONDS
    for candidate in spool_dir.glob(f'*{TMP_SUFFIX}'):
        try:
            if candidate.stat().st_mtime < deadline:
                candidate.unlink()
        except OSError:
            # A concurrent publish may have replaced or removed the
            # file already; that is precisely the outcome we want.
            continue


def list_spool(spool_dir: Path) -> list[Path]:
    """List spooled snapshots in upload order.

    Parameters
    ----------
    spool_dir : pathlib.Path
        The spool directory; a missing directory is not an error.

    Returns
    -------
    list of pathlib.Path
        Complete snapshot files sorted by name, which is chronological.
        Temporary files are never included.
    """
    if not spool_dir.is_dir():
        return []
    _clean_stale_temporaries(spool_dir)
    entries = [
        path
        for path in spool_dir.iterdir()
        if path.is_file() and path.name.endswith(SNAPSHOT_SUFFIX)
    ]
    return sorted(entries, key=lambda path: path.name)


def read_snapshot(path: Path) -> Snapshot:
    """Load a spooled snapshot.

    Parameters
    ----------
    path : pathlib.Path
        A file written by :func:`write_snapshot`.

    Returns
    -------
    Snapshot
        The parsed snapshot.

    Raises
    ------
    pydantic.ValidationError
        If the file is not a valid snapshot document.
    OSError
        If the file cannot be read.
    """
    return Snapshot.model_validate_json(path.read_text(encoding='utf-8'))


def remove(path: Path) -> None:
    """Delete a spooled snapshot.

    Called only once the snapshot is confirmed to be present on the
    remote, so that a failed upload is retried instead of being lost.

    Parameters
    ----------
    path : pathlib.Path
        The spooled file; a missing file is not an error.
    """
    path.unlink(missing_ok=True)
