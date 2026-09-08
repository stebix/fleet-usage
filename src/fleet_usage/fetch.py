"""Downloading and caching of the remote fleet documents.

Reading the fleet means downloading ``fleet.toml`` plus one
``machines/<id>.json`` ledger per registered machine. Every document is
mirrored into the local cache directory so that ``--offline`` works and
so that a transient network failure degrades into slightly stale data
instead of an error.

Three rules keep the cache trustworthy.

* A document replaces its cached copy only after it has been parsed
  successfully. Invalid remote data therefore never destroys a good
  cache; it raises :class:`RemoteDataError` instead.
* Cache files are written through a temporary file and
  :func:`os.replace`, so a reader never observes a partial document.
* The blob SHA reported by the transport is remembered next to the
  document and compared on the next fetch. An unchanged SHA skips the
  rewrite entirely, which is the poor man's ``If-None-Match``.

With ``offline=True`` no method of the client is called at all.
"""

import dataclasses
import datetime as dt
import json
import os
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from fleet_usage.config import Settings, resolve_token
from fleet_usage.github import (
    AuthError,
    GitHubClient,
    GitHubError,
    NotFoundError,
)
from fleet_usage.models import FleetManifest, Ledger
from fleet_usage.paths import AppPaths

__all__ = [
    'MACHINES_PREFIX',
    'MANIFEST_PATH',
    'FetchAuthError',
    'FetchError',
    'FileMeta',
    'FileSource',
    'FleetData',
    'NoDataError',
    'RemoteDataError',
    'build_client',
    'fetch_fleet',
    'ledger_remote_path',
    'machine_id_from_path',
    'parse_ledger',
    'parse_manifest',
]

MANIFEST_PATH = 'fleet.toml'
MACHINES_PREFIX = 'machines/'
CACHE_SUFFIX = '.meta.json'


class FetchError(Exception):
    """Base class for every failure while reading the fleet."""


class RemoteDataError(FetchError):
    """A remote document exists but cannot be parsed."""


class NoDataError(FetchError):
    """Nothing could be read: no network and no usable cache."""


class FetchAuthError(FetchError):
    """The token is missing, expired or lacks the required scope."""


class FileSource(Protocol):
    """The part of :class:`~fleet_usage.github.GitHubClient` used here.

    Declaring the dependency structurally keeps the read path testable
    with a small fake and documents that reading never writes.
    """

    def get_file(self, path: str) -> tuple[bytes, str]:
        """Fetch a file and its blob SHA.

        Parameters
        ----------
        path : str
            Repository relative path.

        Returns
        -------
        tuple of (bytes, str)
            File content and blob SHA.
        """
        ...  # pragma: no cover - protocol declaration

    def list_tree(self, prefix: str) -> list[str]:
        """List every blob path below ``prefix``.

        Parameters
        ----------
        prefix : str
            Repository relative directory.

        Returns
        -------
        list of str
            Blob paths.
        """
        ...  # pragma: no cover - protocol declaration


@dataclasses.dataclass(frozen=True, slots=True)
class FileMeta:
    """Where one document came from and how old it is.

    Attributes
    ----------
    path
        Repository relative path of the document.
    fetched_at
        When the copy that is being used was downloaded.
    from_cache
        ``True`` when the network was unavailable or not used and the
        content comes from the local cache.
    sha
        Blob SHA of the copy, when it is known.
    """

    path: str
    fetched_at: dt.datetime | None
    from_cache: bool
    sha: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class FleetData:
    """Everything ``show`` needs to render a report.

    Attributes
    ----------
    manifest
        Contents of ``fleet.toml``.
    ledgers
        Ledger per machine id; machines that never reported are absent.
    files
        Freshness metadata keyed by repository relative path.
    never_reported
        Ids of manifest machines without a ledger in the repository.
    unregistered
        Ids of ledgers found in ``machines/`` that the manifest does not
        list. Only populated when the fleet was read online.
    offline
        Whether the read was performed without any network access.
    """

    manifest: FleetManifest
    ledgers: dict[str, Ledger]
    files: dict[str, FileMeta]
    never_reported: tuple[str, ...] = ()
    unregistered: tuple[str, ...] = ()
    offline: bool = False

    @property
    def from_cache(self) -> bool:
        """Whether any document was served from the local cache.

        Returns
        -------
        bool
            ``True`` when at least one document is a cached copy.
        """
        return any(meta.from_cache for meta in self.files.values())

    @property
    def fetched_at(self) -> dt.datetime | None:
        """Age of the oldest document in use.

        Returns
        -------
        datetime.datetime or None
            The earliest download timestamp, or ``None`` when no
            document carries one.
        """
        stamps = [
            meta.fetched_at
            for meta in self.files.values()
            if meta.fetched_at is not None
        ]
        return min(stamps) if stamps else None

    def label_of(self, machine_id: str) -> str:
        """Return the display label of a machine.

        Parameters
        ----------
        machine_id : str
            Machine id, registered or not.

        Returns
        -------
        str
            The manifest label, the ledger label or the id itself, in
            that order of preference.
        """
        for entry in self.manifest.machines:
            if entry.id == machine_id:
                return entry.label
        ledger = self.ledgers.get(machine_id)
        if ledger is not None:
            return ledger.label
        return machine_id


def ledger_remote_path(machine_id: str) -> str:
    """Return the repository path of a machine ledger.

    Parameters
    ----------
    machine_id : str
        Machine id.

    Returns
    -------
    str
        ``machines/<machine_id>.json``.
    """
    return f'{MACHINES_PREFIX}{machine_id}.json'


def machine_id_from_path(path: str) -> str | None:
    """Extract the machine id from a ledger path.

    Parameters
    ----------
    path : str
        Repository relative path, for example ``machines/abc.json``.

    Returns
    -------
    str or None
        The id, or ``None`` when ``path`` is not a ledger path.
    """
    if not path.startswith(MACHINES_PREFIX) or not path.endswith('.json'):
        return None
    name = path[len(MACHINES_PREFIX) : -len('.json')]
    if not name or '/' in name:
        return None
    return name


def _is_safe_name(name: str) -> bool:
    """Whether a machine id may be used as a cache file name.

    Ids arrive from a remote document, so they are checked before they
    reach the filesystem.

    Parameters
    ----------
    name : str
        Candidate machine id.

    Returns
    -------
    bool
        ``True`` when the id contains no path separator and no dot
        segment.
    """
    if not name or name in {'.', '..'}:
        return False
    forbidden = {'/', '\\', os.sep, os.altsep or '/'}
    return not any(char in name for char in forbidden)


@dataclasses.dataclass(frozen=True, slots=True)
class _Cached:
    """A document that was found in the local cache."""

    content: bytes
    sha: str | None
    fetched_at: dt.datetime | None


def _sidecar(cache_file: Path) -> Path:
    """Return the metadata file belonging to a cache entry.

    Parameters
    ----------
    cache_file : pathlib.Path
        The cached document.

    Returns
    -------
    pathlib.Path
        Path of the sidecar holding the SHA and the fetch time.
    """
    return cache_file.with_name(cache_file.name + CACHE_SUFFIX)


def _parse_timestamp(value: object) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp from a sidecar.

    Parameters
    ----------
    value : object
        The raw value read from JSON.

    Returns
    -------
    datetime.datetime or None
        A timezone aware timestamp, or ``None`` when the value is
        missing or malformed.
    """
    if not isinstance(value, str):
        return None
    text = value.replace('Z', '+00:00')
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _read_cache(cache_file: Path) -> _Cached | None:
    """Read a cached document and its sidecar.

    Parameters
    ----------
    cache_file : pathlib.Path
        The cached document.

    Returns
    -------
    _Cached or None
        The cached bytes with whatever metadata could be recovered, or
        ``None`` when there is no cached copy. A damaged sidecar only
        costs the metadata, never the document.
    """
    try:
        content = cache_file.read_bytes()
    except OSError:
        return None
    sha: str | None = None
    fetched_at: dt.datetime | None = None
    try:
        raw = json.loads(_sidecar(cache_file).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        value = raw.get('sha')
        sha = value if isinstance(value, str) else None
        fetched_at = _parse_timestamp(raw.get('fetched_at'))
    return _Cached(content=content, sha=sha, fetched_at=fetched_at)


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write ``payload`` to ``target`` without ever truncating it.

    Parameters
    ----------
    target : pathlib.Path
        Destination file.
    payload : bytes
        Content to store.

    Raises
    ------
    OSError
        If the directory cannot be created or the file cannot be
        written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f'{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        tmp.write_bytes(payload)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _write_cache(
    cache_file: Path,
    content: bytes,
    sha: str | None,
    fetched_at: dt.datetime,
    remote_path: str,
) -> None:
    """Store a validated document in the cache.

    Parameters
    ----------
    cache_file : pathlib.Path
        Destination inside the cache directory.
    content : bytes
        The verbatim remote document.
    sha : str or None
        Blob SHA reported by the transport.
    fetched_at : datetime.datetime
        Download timestamp.
    remote_path : str
        Repository relative path, recorded for debugging.

    Notes
    -----
    Cache writes are best effort: a read-only cache directory must not
    break a report, so :class:`OSError` is swallowed.
    """
    meta = {
        'path': remote_path,
        'sha': sha,
        'fetched_at': fetched_at.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    payload = json.dumps(meta, sort_keys=True, indent=2) + '\n'
    try:
        _atomic_write(cache_file, content)
        _atomic_write(_sidecar(cache_file), payload.encode('utf-8'))
    except OSError:
        return


def parse_manifest(content: bytes) -> FleetManifest:
    """Parse ``fleet.toml``.

    Parameters
    ----------
    content : bytes
        The raw document.

    Returns
    -------
    FleetManifest
        The validated manifest.

    Raises
    ------
    ValueError
        If the document is not TOML or does not match the schema.
    """
    try:
        raw = tomllib.loads(content.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f'not valid TOML: {exc}'
        raise ValueError(msg) from exc
    try:
        return FleetManifest.model_validate(raw)
    except ValidationError as exc:
        msg = f'not a fleet manifest: {exc.error_count()} problem(s)'
        raise ValueError(msg) from exc


def parse_ledger(content: bytes) -> Ledger:
    """Parse a machine ledger.

    Parameters
    ----------
    content : bytes
        The raw document.

    Returns
    -------
    Ledger
        The validated ledger.

    Raises
    ------
    ValueError
        If the document is not JSON or does not match the schema.
    """
    try:
        return Ledger.model_validate_json(content)
    except ValidationError as exc:
        msg = f'not a ledger: {exc.error_count()} problem(s)'
        raise ValueError(msg) from exc


def _from_cache[T](
    cached: _Cached,
    remote_path: str,
    parse: Callable[[bytes], T],
) -> tuple[T, FileMeta]:
    """Turn a cached document into a parsed value.

    Parameters
    ----------
    cached : _Cached
        The cached bytes and metadata.
    remote_path : str
        Repository relative path, used in error messages.
    parse : callable
        Parser for the document family.

    Returns
    -------
    tuple
        The parsed value and its freshness metadata.

    Raises
    ------
    RemoteDataError
        If the cached copy itself is unusable.
    """
    try:
        value = parse(cached.content)
    except ValueError as exc:
        msg = f'cached copy of {remote_path} is unusable: {exc}'
        raise RemoteDataError(msg) from exc
    meta = FileMeta(
        path=remote_path,
        fetched_at=cached.fetched_at,
        from_cache=True,
        sha=cached.sha,
    )
    return value, meta


def _fetch_document[T](
    client: FileSource | None,
    remote_path: str,
    cache_file: Path,
    parse: Callable[[bytes], T],
    *,
    offline: bool,
    now: dt.datetime,
) -> tuple[T, FileMeta] | None:
    """Fetch one document, falling back to the cache when needed.

    Parameters
    ----------
    client : FileSource or None
        Transport; ignored and never called when ``offline`` is set.
    remote_path : str
        Repository relative path.
    cache_file : pathlib.Path
        Where the document is mirrored locally.
    parse : callable
        Parser and validator for the document family.
    offline : bool
        Read the cache only.
    now : datetime.datetime
        Timestamp recorded for a successful download.

    Returns
    -------
    tuple or None
        The parsed value with its metadata, or ``None`` when the
        document does not exist remotely and is not cached.

    Raises
    ------
    FetchAuthError
        If the transport rejects the token.
    NoDataError
        If the network is unavailable and nothing is cached.
    RemoteDataError
        If the document exists but cannot be parsed.
    """
    cached = _read_cache(cache_file)
    if offline or client is None:
        if cached is None:
            return None
        return _from_cache(cached, remote_path, parse)
    try:
        content, sha = client.get_file(remote_path)
    except AuthError as exc:
        raise FetchAuthError(str(exc)) from exc
    except NotFoundError:
        return None
    except (GitHubError, OSError) as exc:
        if cached is None:
            msg = (
                f'cannot download {remote_path}: {exc}\n'
                'no cached copy is available'
            )
            raise NoDataError(msg) from exc
        return _from_cache(cached, remote_path, parse)
    try:
        value = parse(content)
    except ValueError as exc:
        msg = f'{remote_path} in the data repository is invalid: {exc}'
        raise RemoteDataError(msg) from exc
    if cached is None or cached.sha != sha or cached.content != content:
        _write_cache(cache_file, content, sha, now, remote_path)
    meta = FileMeta(
        path=remote_path, fetched_at=now, from_cache=False, sha=sha
    )
    return value, meta


def build_client(settings: Settings) -> GitHubClient:
    """Create a GitHub client from the settings.

    Parameters
    ----------
    settings : Settings
        Loaded settings.

    Returns
    -------
    GitHubClient
        A configured client. It satisfies :class:`FileSource`, so the
        read path can be exercised with a fake instead.

    Raises
    ------
    FetchAuthError
        If no token is configured.
    """
    token = resolve_token(settings)
    if token is None:
        msg = (
            f'{settings.github.token_env} is not set\n'
            'put a GitHub token into the .env file next to settings.toml, '
            'or pass --offline to use the cached ledgers'
        )
        raise FetchAuthError(msg)
    return GitHubClient(
        repository=settings.github.repository,
        token=token,
        branch=settings.github.branch,
        api_base_url=settings.github.api_base_url,
    )


def _discover_unregistered(
    client: FileSource,
    known: set[str],
) -> tuple[str, ...]:
    """List ledgers that the manifest does not mention.

    Parameters
    ----------
    client : FileSource
        Transport.
    known : set of str
        Machine ids from the manifest.

    Returns
    -------
    tuple of str
        Ids of unregistered ledgers, sorted. Listing failures are
        ignored: an unregistered machine is a hint, not a result.
    """
    try:
        paths = client.list_tree(MACHINES_PREFIX)
    except (GitHubError, OSError):
        return ()
    found: set[str] = set()
    for path in paths:
        machine_id = machine_id_from_path(path)
        if machine_id is not None and machine_id not in known:
            found.add(machine_id)
    return tuple(sorted(found))


def fetch_fleet(
    settings: Settings,
    paths: AppPaths,
    client: FileSource | None,
    *,
    offline: bool = False,
    now: dt.datetime | None = None,
) -> FleetData:
    """Download the manifest and every machine ledger.

    Parameters
    ----------
    settings : Settings
        Loaded settings; only the repository coordinates are used, for
        error messages.
    paths : AppPaths
        Application paths; the cache lives in
        :attr:`~fleet_usage.paths.AppPaths.ledger_cache_dir`.
    client : FileSource or None
        Transport. It is never called when ``offline`` is set, so
        ``None`` is a valid argument for an offline read.
    offline : bool, optional
        Read the local cache only.
    now : datetime.datetime or None, optional
        Timestamp recorded for downloads; defaults to the current time.

    Returns
    -------
    FleetData
        Manifest, ledgers and per-document freshness metadata.

    Raises
    ------
    FetchAuthError
        If the transport rejects the token.
    NoDataError
        If neither the network nor the cache yields a manifest.
    RemoteDataError
        If the manifest or a ledger exists but cannot be parsed.
    """
    stamp = now if now is not None else dt.datetime.now(dt.UTC)
    cache_dir = paths.ledger_cache_dir
    manifest_result = _fetch_document(
        client,
        MANIFEST_PATH,
        cache_dir / MANIFEST_PATH,
        parse_manifest,
        offline=offline,
        now=stamp,
    )
    if manifest_result is None:
        repository = settings.github.repository
        if offline or client is None:
            msg = (
                f'no cached copy of {MANIFEST_PATH} in {cache_dir}\n'
                'run without --offline once to populate the cache'
            )
        else:
            msg = (
                f'{repository} has no {MANIFEST_PATH}\n'
                "run 'fleet-usage repo init' to create it"
            )
        raise NoDataError(msg)
    manifest, manifest_meta = manifest_result

    files: dict[str, FileMeta] = {MANIFEST_PATH: manifest_meta}
    ledgers: dict[str, Ledger] = {}
    never: list[str] = []
    for entry in manifest.machines:
        remote_path = ledger_remote_path(entry.id)
        if not _is_safe_name(entry.id):
            msg = f'{MANIFEST_PATH} lists an unusable machine id {entry.id!r}'
            raise RemoteDataError(msg)
        result = _fetch_document(
            client,
            remote_path,
            cache_dir / f'{entry.id}.json',
            parse_ledger,
            offline=offline,
            now=stamp,
        )
        if result is None:
            never.append(entry.id)
            continue
        ledger, meta = result
        ledgers[entry.id] = ledger
        files[remote_path] = meta

    unregistered: tuple[str, ...] = ()
    if not offline and client is not None:
        known = {entry.id for entry in manifest.machines}
        unregistered = _discover_unregistered(client, known)

    return FleetData(
        manifest=manifest,
        ledgers=ledgers,
        files=files,
        never_reported=tuple(never),
        unregistered=unregistered,
        offline=offline,
    )
