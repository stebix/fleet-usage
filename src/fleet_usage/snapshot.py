"""Construction, hashing and naming of immutable snapshots.

A snapshot is identified by the instant it was collected and by a short
hash of its payload, which makes its file name both chronological and
content addressed: two runs that observe the same usage at different
times produce different names, and sorting names sorts by time.

All timestamps in names and paths are UTC, so that machines in different
zones never disagree about the order of snapshots.
"""

import datetime as dt
import hashlib
import re
from collections.abc import Mapping

from fleet_usage.config import ConfigError, Settings
from fleet_usage.models import (
    AgentSnapshot,
    CollectorInfo,
    Snapshot,
    canonical_json_bytes,
)

__all__ = [
    'HASH_PREFIX',
    'NAME_TIMESTAMP_FORMAT',
    'SHORT_HASH_LENGTH',
    'SNAPSHOT_ROOT',
    'agents_hash',
    'build_snapshot',
    'parse_snapshot_name',
    'short_hash',
    'snapshot_file_name',
    'snapshot_remote_path',
]

HASH_PREFIX = 'sha256:'
SHORT_HASH_LENGTH = 8
SNAPSHOT_ROOT = 'snapshots'
NAME_TIMESTAMP_FORMAT = '%Y%m%dT%H%M%SZ'
_NAME_RE = re.compile(r'^(?P<stamp>\d{8}T\d{6}Z)-(?P<hash>[0-9a-f]{8})\.json$')


def agents_hash(agents: Mapping[str, AgentSnapshot]) -> str:
    """Hash the agents mapping of a snapshot.

    The hash is taken over the canonical JSON representation of the
    mapping, that is sorted keys, compact separators and ASCII escaping.
    It therefore only depends on the data, never on the order in which
    the collector happened to emit the agents.

    Parameters
    ----------
    agents : Mapping[str, AgentSnapshot]
        The ``agents`` object of a snapshot.

    Returns
    -------
    str
        ``'sha256:'`` followed by 64 lowercase hexadecimal characters.
    """
    digest = hashlib.sha256(canonical_json_bytes(agents)).hexdigest()
    return f'{HASH_PREFIX}{digest}'


def short_hash(value: str) -> str:
    """Return the first eight hexadecimal characters of a hash.

    Parameters
    ----------
    value : str
        A full hash, with or without the ``'sha256:'`` prefix.

    Returns
    -------
    str
        Eight lowercase hexadecimal characters, used in snapshot file
        names.

    Raises
    ------
    ValueError
        If ``value`` is shorter than eight hexadecimal characters.
    """
    digest = value
    if digest.startswith(HASH_PREFIX):
        digest = digest[len(HASH_PREFIX) :]
    if len(digest) < SHORT_HASH_LENGTH:
        msg = f'hash too short to abbreviate: {value!r}'
        raise ValueError(msg)
    return digest[:SHORT_HASH_LENGTH]


def build_snapshot(
    settings: Settings,
    agents: Mapping[str, AgentSnapshot],
    *,
    collector_version: str,
    collected_at: dt.datetime | None = None,
) -> Snapshot:
    """Assemble a snapshot from parsed collector output.

    Parameters
    ----------
    settings : Settings
        Loaded settings; the machine identity and the collector section
        are required, so a viewer-only installation cannot build a
        snapshot.
    agents : Mapping[str, AgentSnapshot]
        The parsed collector payload, keyed by agent name.
    collector_version : str
        Version reported by the collector for this run.
    collected_at : datetime.datetime or None, optional
        Collection instant; defaults to now in UTC. Sub-second
        precision is dropped so that the stored timestamp and the file
        name always describe the same instant.

    Returns
    -------
    Snapshot
        The assembled snapshot, including its agents hash.

    Raises
    ------
    ConfigError
        If the settings carry no machine identity or no collector
        section.
    """
    if settings.machine is None or settings.collector is None:
        msg = (
            'cannot build a snapshot without the [machine] and '
            '[collector] settings sections\n'
            "run 'fleet-usage init' without --viewer-only to add them"
        )
        raise ConfigError(msg)
    moment = dt.datetime.now(dt.UTC) if collected_at is None else collected_at
    if moment.tzinfo is None:
        msg = 'collected_at must be timezone aware'
        raise ValueError(msg)
    moment = moment.astimezone(dt.UTC).replace(microsecond=0)
    payload = dict(agents)
    collector = CollectorInfo(
        name='ccusage',
        version=collector_version,
        mode='auto',
        pricing='offline' if settings.collector.offline_pricing else 'online',
    )
    return Snapshot(
        machine_id=settings.machine.id,
        label=settings.machine.label,
        collected_at=moment,
        timezone=settings.collector.timezone,
        collector=collector,
        agents_hash=agents_hash(payload),
        agents=payload,
    )


def snapshot_file_name(snapshot: Snapshot) -> str:
    """Return the file name of ``snapshot``.

    Parameters
    ----------
    snapshot : Snapshot
        The snapshot to name.

    Returns
    -------
    str
        ``<YYYYMMDDTHHMMSSZ>-<hash8>.json`` in UTC. The same name is
        used in the local spool and in the data repository.
    """
    moment = snapshot.collected_at.astimezone(dt.UTC)
    stamp = moment.strftime(NAME_TIMESTAMP_FORMAT)
    return f'{stamp}-{short_hash(snapshot.agents_hash)}.json'


def snapshot_remote_path(snapshot: Snapshot) -> str:
    """Derive the remote path of ``snapshot`` in the data repository.

    Parameters
    ----------
    snapshot : Snapshot
        The snapshot to place.

    Returns
    -------
    str
        ``snapshots/<machine_id>/<YYYY-MM>/<stamp>-<hash8>.json``. The
        month directory keeps the trees small enough for the GitHub
        contents API to remain pleasant to page through.
    """
    moment = snapshot.collected_at.astimezone(dt.UTC)
    month = moment.strftime('%Y-%m')
    name = snapshot_file_name(snapshot)
    return f'{SNAPSHOT_ROOT}/{snapshot.machine_id}/{month}/{name}'


def parse_snapshot_name(name: str) -> tuple[dt.datetime, str]:
    """Split a snapshot file name into its instant and its short hash.

    Parameters
    ----------
    name : str
        A snapshot file name or a remote path ending in one.

    Returns
    -------
    tuple of (datetime.datetime, str)
        The collection instant in UTC and the eight character hash.

    Raises
    ------
    ValueError
        If ``name`` is not a snapshot name.
    """
    basename = name.rsplit('/', 1)[-1]
    match = _NAME_RE.match(basename)
    if match is None:
        msg = (
            f'not a snapshot file name: {name!r}\n'
            "expected '<YYYYMMDDTHHMMSSZ>-<hash8>.json'"
        )
        raise ValueError(msg)
    moment = dt.datetime.strptime(
        match.group('stamp'), NAME_TIMESTAMP_FORMAT
    ).replace(tzinfo=dt.UTC)
    return moment, match.group('hash')
