"""The publish sequence.

Under a file lock, the remote ledger is fetched, a snapshot is collected
and spooled, the spool is drained to the remote, and every snapshot
after ``applied_through`` is merged back into the ledger before it is
written with its blob SHA.

The sequence is written so that every interruption is recoverable on the
next run:

* a snapshot that was built but not uploaded stays in the spool,
* a snapshot that was uploaded but whose upload response was lost is
  recognised on the next attempt by comparing the remote document,
* a snapshot that was uploaded before the ledger could be written is
  found again by listing the remote snapshots after ``applied_through``.

This module is also the single place that decides which failure becomes
which exit code; see :func:`exit_code_for`.
"""

import dataclasses
import datetime as dt
import decimal
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import filelock
from pydantic import ValidationError

from fleet_usage import snapshot as snapshot_module
from fleet_usage import spool
from fleet_usage.collector import (
    CollectorError,
    collector_version,
    parse_daily_by_agent,
    run_collector,
)
from fleet_usage.config import Settings, resolve_token
from fleet_usage.exit_codes import ExitCode
from fleet_usage.fetch import ledger_remote_path
from fleet_usage.github import (
    AuthError,
    GitHubClient,
    GitHubError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    RemoteConflictError,
)
from fleet_usage.ledger import apply_snapshot, new_ledger
from fleet_usage.models import Ledger, LedgerAgent, MergePolicy, Snapshot
from fleet_usage.paths import AppPaths

__all__ = [
    'LEDGER_ATTEMPTS',
    'MIN_UPLOAD_INTERVAL_SECONDS',
    'UPLOAD_ATTEMPTS',
    'AgentSummary',
    'ClientFactory',
    'PublishResult',
    'collect_snapshot',
    'default_client_factory',
    'exit_code_for',
    'is_due',
    'ledger_remote_path',
    'publish',
    'summarize',
    'write_due_marker',
]

_LOGGER = logging.getLogger(__name__)

#: One create per second keeps the run below the 60 writes per minute
#: that the Contents API tolerates for a single repository.
MIN_UPLOAD_INTERVAL_SECONDS = 1.0
UPLOAD_ATTEMPTS = 3
LEDGER_ATTEMPTS = 3
VERIFY_BACKOFF_SECONDS = 2.0

ClientFactory = Callable[[Settings, str], GitHubClient]


@dataclasses.dataclass(frozen=True, slots=True)
class AgentSummary:
    """One row of the ``--dry-run`` summary table.

    Attributes
    ----------
    agent
        Agent name.
    status
        ``'ok'`` or ``'error'``.
    days
        Number of day records in the snapshot.
    latest_date
        Most recent day, or ``None`` when there is none.
    tokens
        Sum of all token counters.
    cost_usd
        Sum of the known costs.
    unknown_costs
        Number of day records whose cost is unknown.
    """

    agent: str
    status: str
    days: int
    latest_date: dt.date | None
    tokens: int
    cost_usd: decimal.Decimal
    unknown_costs: int


@dataclasses.dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of one publish cycle.

    Attributes
    ----------
    exit_code
        The exit code the command must return.
    message
        One line summary for the operator.
    snapshot
        The snapshot that was collected, if any.
    spooled
        Whether a new snapshot was written to the spool.
    deduplicated
        Whether collection produced a snapshot identical to the one the
        ledger already knows, so that nothing was spooled.
    uploaded
        Remote paths confirmed present during this run.
    pending
        Number of files still in the spool afterwards.
    applied
        Remote snapshot paths merged into the ledger.
    ledger_written
        Whether the ledger was written back.
    skipped_remote
        Remote paths that could not be read as snapshots and were
        skipped rather than applied.
    skipped
        ``None``, ``'locked'`` or ``'not-due'``.
    """

    exit_code: ExitCode
    message: str = ''
    snapshot: Snapshot | None = None
    spooled: bool = False
    deduplicated: bool = False
    uploaded: tuple[str, ...] = ()
    pending: int = 0
    applied: tuple[str, ...] = ()
    ledger_written: bool = False
    skipped_remote: tuple[str, ...] = ()
    skipped: str | None = None


def snapshot_prefix(machine_id: str) -> str:
    """Return the remote directory holding a machine's snapshots.

    Parameters
    ----------
    machine_id : str
        Identifier of the machine.

    Returns
    -------
    str
        ``snapshots/<machine_id>``.
    """
    return f'snapshots/{machine_id}'


def default_client_factory(settings: Settings, token: str) -> GitHubClient:
    """Build a GitHub client from the settings.

    Parameters
    ----------
    settings : Settings
        Loaded settings.
    token : str
        Resolved GitHub token.

    Returns
    -------
    GitHubClient
        A client bound to the configured repository and branch.
    """
    return GitHubClient(
        token=token,
        repository=settings.github.repository,
        branch=settings.github.branch,
        api_base_url=settings.github.api_base_url,
    )


def collect_snapshot(settings: Settings, *, now: dt.datetime) -> Snapshot:
    """Run the collector and assemble a snapshot.

    Parameters
    ----------
    settings : Settings
        Loaded settings; the machine and collector sections must be
        present.
    now : datetime.datetime
        Collection timestamp recorded in the snapshot.

    Returns
    -------
    Snapshot
        The assembled snapshot.

    Raises
    ------
    CollectorError
        If the collector cannot be run or its output cannot be parsed.
    """
    collector = settings.collector
    machine = settings.machine
    if collector is None or machine is None:  # pragma: no cover - guarded
        msg = 'collect_snapshot requires a publisher installation'
        raise CollectorError(msg)
    payload = run_collector(collector)
    agents = parse_daily_by_agent(payload, collector.agents)
    version = collector.version or collector_version(collector)
    return snapshot_module.build_snapshot(
        settings,
        agents,
        collector_version=version,
        collected_at=now,
    )


def summarize(snapshot: Snapshot) -> list[AgentSummary]:
    """Summarise a snapshot for the dry run table.

    Parameters
    ----------
    snapshot : Snapshot
        The collected snapshot.

    Returns
    -------
    list of AgentSummary
        One row per agent, ordered by agent name.
    """
    rows: list[AgentSummary] = []
    for agent in sorted(snapshot.agents):
        payload = snapshot.agents[agent]
        tokens = sum(day.total_tokens for day in payload.days)
        cost = decimal.Decimal(0)
        unknown = 0
        for day in payload.days:
            if day.cost_usd is None:
                unknown += 1
            else:
                cost += day.cost_usd
        latest = max((day.date for day in payload.days), default=None)
        rows.append(
            AgentSummary(
                agent=agent,
                status=payload.status,
                days=len(payload.days),
                latest_date=latest,
                tokens=tokens,
                cost_usd=cost,
                unknown_costs=unknown,
            )
        )
    return rows


def exit_code_for(
    *,
    auth_error: AuthError | None,
    conflict: RemoteConflictError | None,
    pending: int,
    network_error: GitHubError | None = None,
    collection_error: CollectorError | None,
) -> ExitCode:
    """Map the failures of one run onto a single exit code.

    A run can fail in several ways at once, for instance when the
    collector is broken and the network is down as well. The most
    specific and most actionable condition wins, in this order:
    authentication, remote conflict, undelivered spool, collection.

    Parameters
    ----------
    auth_error : AuthError or None
        A permission failure, if one occurred.
    conflict : RemoteConflictError or None
        A remote conflict, if one occurred.
    pending : int
        Files left in the spool.
    network_error : GitHubError or None, optional
        A transport failure that left work undone, for instance a
        ledger that could not be written.
    collection_error : CollectorError or None
        A collector failure, if one occurred.

    Returns
    -------
    ExitCode
        The exit code for the process.
    """
    if auth_error is not None:
        return ExitCode.AUTH_FAILURE
    if conflict is not None:
        return ExitCode.REMOTE_CONFLICT
    if pending or network_error is not None:
        return ExitCode.SPOOLED_NOT_UPLOADED
    if collection_error is not None:
        return ExitCode.COLLECTION_FAILURE
    return ExitCode.OK


# -- due marker ------------------------------------------------------


def read_due_marker(marker: Path) -> dt.datetime | None:
    """Read the timestamp of the last completed run.

    Parameters
    ----------
    marker : pathlib.Path
        The marker file in the state directory.

    Returns
    -------
    datetime.datetime or None
        The recorded instant, or ``None`` when the marker is missing or
        unreadable. A damaged marker never blocks a run.
    """
    try:
        payload = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get('last_run_at')
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def write_due_marker(marker: Path, now: dt.datetime) -> None:
    """Record the instant of a completed run.

    The file is written through a temporary file plus an atomic replace,
    so that a crash never leaves a half written marker behind.

    Parameters
    ----------
    marker : pathlib.Path
        The marker file in the state directory.
    now : datetime.datetime
        Timestamp to record.
    """
    payload = json.dumps(
        {'last_run_at': now.astimezone(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}
    )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=marker.parent, prefix=marker.name, suffix='.tmp'
        )
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(payload)
        Path(temporary).replace(marker)
    except OSError as exc:
        _LOGGER.warning('cannot write the due marker %s: %s', marker, exc)


def is_due(marker: Path, now: dt.datetime, interval_minutes: int) -> bool:
    """Whether a run is due according to the local marker.

    Parameters
    ----------
    marker : pathlib.Path
        The marker file in the state directory.
    now : datetime.datetime
        Current instant.
    interval_minutes : int
        Configured publish interval.

    Returns
    -------
    bool
        ``True`` when there is no marker or it is older than the
        interval.
    """
    last = read_due_marker(marker)
    if last is None:
        return True
    age = now.astimezone(dt.UTC) - last
    return age >= dt.timedelta(minutes=interval_minutes)


# -- helpers ---------------------------------------------------------


def _same_document(left: bytes, right: bytes) -> bool:
    """Compare two documents, tolerating insignificant JSON differences.

    Parameters
    ----------
    left : bytes
        One document.
    right : bytes
        The other document.

    Returns
    -------
    bool
        ``True`` when the bytes are equal or both decode to the same
        JSON value.
    """
    if left == right:
        return True
    try:
        return bool(json.loads(left) == json.loads(right))
    except ValueError:
        return False


def _quarantine(path: Path) -> None:
    """Move an unreadable spool file aside.

    Parameters
    ----------
    path : pathlib.Path
        The offending spool file. It is renamed rather than deleted so
        that the data can still be inspected, and so that it no longer
        blocks the drain.
    """
    target = path.with_suffix(path.suffix + '.invalid')
    try:
        path.replace(target)
    except OSError as exc:  # pragma: no cover - filesystem specific
        _LOGGER.warning('cannot quarantine %s: %s', path, exc)


def _newest_spooled_hash(spool_dir: Path) -> str | None:
    """Return the short agents hash of the newest spooled snapshot.

    The spool is drained in name order and a name carries the short
    hash of the payload, so the last file is the most recent thing this
    machine observed and its hash can be compared without reading the
    document.

    Parameters
    ----------
    spool_dir : pathlib.Path
        The spool directory.

    Returns
    -------
    str or None
        Eight hexadecimal characters, or ``None`` when the spool is
        empty or its newest name is not a snapshot name.
    """
    files = spool.list_spool(spool_dir)
    if not files:
        return None
    try:
        _moment, digest = snapshot_module.parse_snapshot_name(files[-1].name)
    except ValueError:
        return None
    return digest


def _upload_one(
    client: GitHubClient,
    remote_path: str,
    content: bytes,
    *,
    sleep: Callable[[float], None],
) -> None:
    """Upload one snapshot and confirm that it is present remotely.

    Any failure of the ``PUT`` is followed by a ``GET`` of the same
    path, because a lost response is indistinguishable from a rejected
    request: if the remote document is identical the create did happen
    and the file may be dropped from the spool.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    remote_path : str
        Path of the snapshot in the data repository.
    content : bytes
        The snapshot document.
    sleep : callable
        Injected sleep used between the bounded retries.

    Raises
    ------
    AuthError
        If the token may not write; never retried.
    RemoteConflictError
        If a different document already occupies the path.
    GitHubError
        If the upload could not be confirmed.
    """
    message = f'fleet-usage: add {remote_path}'
    last: GitHubError | None = None
    for attempt in range(UPLOAD_ATTEMPTS):
        try:
            client.put_file(remote_path, content, message)
        except RateLimitError as exc:
            last = exc
        except AuthError:
            raise
        except GitHubError as exc:
            last = exc
        else:
            return
        _LOGGER.info('verifying %s after %s', remote_path, last)
        try:
            remote_content, _ = client.get_file(remote_path)
        except NotFoundError:
            # The create really did not happen; retry it.
            if attempt + 1 >= UPLOAD_ATTEMPTS:
                break
            sleep(VERIFY_BACKOFF_SECONDS * (attempt + 1))
            continue
        except RateLimitError as exc:
            # A rate limit shares its status code with a permission
            # failure but says nothing about the token, so it must be
            # reported as a transport problem, never as an auth error.
            last = exc
            break
        except AuthError:
            raise
        except GitHubError as exc:
            last = exc
            break
        if _same_document(remote_content, content):
            _LOGGER.info('%s was already present remotely', remote_path)
            return
        msg = (
            f'{remote_path} exists remotely with different content; '
            'refusing to overwrite an immutable snapshot'
        )
        raise RemoteConflictError(msg, path=remote_path)
    raise (
        last
        if last is not None
        else NetworkError(f'could not confirm {remote_path}', path=remote_path)
    )


@dataclasses.dataclass(slots=True)
class _DrainOutcome:
    """Result of draining the spool.

    Attributes
    ----------
    uploaded
        Remote paths confirmed present.
    documents
        The snapshots behind those paths, so that the ledger merge does
        not download what this run just uploaded.
    pending
        Files still in the spool.
    auth_error
        A permission failure that stopped the drain.
    conflict
        A conflicting remote document that stopped the drain.
    network_error
        A transport failure that stopped the drain.
    """

    uploaded: list[str] = dataclasses.field(default_factory=list)
    documents: dict[str, Snapshot] = dataclasses.field(default_factory=dict)
    pending: int = 0
    auth_error: AuthError | None = None
    conflict: RemoteConflictError | None = None
    network_error: GitHubError | None = None


def _drain(
    client: GitHubClient,
    spool_dir: Path,
    *,
    sleep: Callable[[float], None],
) -> _DrainOutcome:
    """Upload every spooled snapshot in name order.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    spool_dir : pathlib.Path
        The spool directory.
    sleep : callable
        Injected sleep, used for the throttle and the retries.

    Returns
    -------
    _DrainOutcome
        What was uploaded and what stopped the drain, if anything.
    """
    outcome = _DrainOutcome()
    files = spool.list_spool(spool_dir)
    for index, path in enumerate(files):
        try:
            content = path.read_bytes()
            document = Snapshot.model_validate_json(content)
        except OSError as exc:
            _LOGGER.error('cannot read spooled %s: %s', path, exc)
            continue
        except ValidationError as exc:
            _LOGGER.error('spooled %s is not a snapshot: %s', path, exc)
            _quarantine(path)
            continue
        remote_path = snapshot_module.snapshot_remote_path(document)
        if index:
            sleep(MIN_UPLOAD_INTERVAL_SECONDS)
        try:
            _upload_one(client, remote_path, content, sleep=sleep)
        except RemoteConflictError as exc:
            _LOGGER.error('%s', exc)
            outcome.conflict = exc
            break
        except RateLimitError as exc:
            _LOGGER.warning('%s stays in the spool: %s', path, exc)
            outcome.network_error = exc
            break
        except AuthError as exc:
            _LOGGER.error('%s', exc)
            outcome.auth_error = exc
            break
        except GitHubError as exc:
            _LOGGER.warning('%s stays in the spool: %s', path, exc)
            outcome.network_error = exc
            break
        outcome.uploaded.append(remote_path)
        outcome.documents[remote_path] = document
        spool.remove(path)
    outcome.pending = len(spool.list_spool(spool_dir))
    return outcome


def _empty_ledger(settings: Settings, machine_id: str, label: str) -> Ledger:
    """Build the ledger of a machine that has never published.

    Parameters
    ----------
    settings : Settings
        Loaded settings, for the merge policy.
    machine_id : str
        Machine identifier.
    label : str
        Machine label.

    Returns
    -------
    Ledger
        An empty ledger.
    """
    policy = MergePolicy(
        freeze_window_days=settings.ledger.freeze_window_days,
        timezone=settings.timezone,
    )
    return new_ledger(machine_id, label, policy)


def _fetch_ledger(
    client: GitHubClient,
    settings: Settings,
    machine_id: str,
    label: str,
) -> tuple[Ledger, str | None]:
    """Fetch the remote ledger and its blob SHA.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    settings : Settings
        Loaded settings.
    machine_id : str
        Machine identifier.
    label : str
        Machine label.

    Returns
    -------
    tuple of (Ledger, str or None)
        The ledger and its blob SHA; an empty ledger and ``None`` when
        the machine has not published before.

    Raises
    ------
    RemoteConflictError
        If the remote document is not a valid ledger.
    """
    path = ledger_remote_path(machine_id)
    try:
        content, sha = client.get_file(path)
    except NotFoundError:
        _LOGGER.info('no remote ledger at %s yet', path)
        return _empty_ledger(settings, machine_id, label), None
    try:
        ledger = Ledger.model_validate_json(content)
    except ValidationError as exc:
        msg = f'the remote ledger {path} is not valid: {exc}'
        raise RemoteConflictError(msg, path=path) from exc
    ledger.label = label
    ledger.merge_policy = MergePolicy(
        version=ledger.merge_policy.version,
        freeze_window_days=settings.ledger.freeze_window_days,
        timezone=settings.timezone,
    )
    return ledger, sha


def _apply_remote_snapshots(
    client: GitHubClient,
    ledger: Ledger,
    machine_id: str,
    cache: dict[str, Snapshot],
) -> tuple[Ledger, list[str], list[str]]:
    """Merge every remote snapshot after ``applied_through``.

    :func:`fleet_usage.ledger.apply_snapshot` is a pure function, so the
    merged ledger is threaded through the loop and returned rather than
    updated in place.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    ledger : Ledger
        The ledger to start from.
    machine_id : str
        Machine identifier.
    cache : dict of str to Snapshot
        Snapshots already downloaded during this run; extended here so
        that a ledger conflict retry costs no extra downloads.

    Returns
    -------
    tuple of (Ledger, list of str, list of str)
        The merged ledger, the paths that were applied, in order, and
        the paths that could not be read.

    Notes
    -----
    A remote document that is not a snapshot is reported and skipped
    rather than raised: it is immutable, so failing on it would block
    every future run of this machine forever. ``applied_through`` moves
    past it so that it is not downloaded again either.
    """
    prefix = snapshot_prefix(machine_id)
    remote_paths = [
        path
        for path in client.list_tree(prefix)
        if path.endswith('.json')
        and (ledger.applied_through is None or path > ledger.applied_through)
    ]
    merged = ledger
    applied: list[str] = []
    skipped: list[str] = []
    for path in sorted(remote_paths):
        document = cache.get(path)
        if document is None:
            content, _ = client.get_file(path)
            try:
                document = Snapshot.model_validate_json(content)
            except ValidationError as exc:
                _LOGGER.warning(
                    'the remote document %s is not a snapshot, skipping '
                    'it: %s',
                    path,
                    exc,
                )
                skipped.append(path)
                merged = _advance_applied_through(merged, path)
                continue
            cache[path] = document
        merged = apply_snapshot(merged, document, path)
        applied.append(path)
    return merged, applied, skipped


def _advance_applied_through(ledger: Ledger, path: str) -> Ledger:
    """Move ``applied_through`` past a snapshot that was not applied.

    Parameters
    ----------
    ledger : Ledger
        The ledger to advance; it is not modified.
    path : str
        Remote path that was skipped.

    Returns
    -------
    Ledger
        A ledger whose ``applied_through`` is at least ``path``.
    """
    if ledger.applied_through is not None and path <= ledger.applied_through:
        return ledger
    return ledger.model_copy(update={'applied_through': path})


def _record_agent_states(
    ledger: Ledger,
    snapshot: Snapshot | None,
    collection_error: CollectorError | None,
    agents: list[str],
) -> None:
    """Update the per agent error fields of the ledger.

    Parameters
    ----------
    ledger : Ledger
        The ledger to update in place.
    snapshot : Snapshot or None
        The snapshot of this run, if collection succeeded.
    collection_error : CollectorError or None
        The collector failure, if collection failed.
    agents : list of str
        Configured agents, used when the whole collection failed.
    """
    if collection_error is not None:
        for agent in agents:
            entry = ledger.agents.setdefault(agent, LedgerAgent())
            entry.last_error = str(collection_error)
        return
    if snapshot is None:
        return
    for name, payload in snapshot.agents.items():
        entry = ledger.agents.setdefault(name, LedgerAgent())
        if payload.status == 'ok':
            entry.last_error = None
        else:
            entry.last_error = payload.message or 'collection failed'


def _write_ledger(
    client: GitHubClient,
    settings: Settings,
    machine_id: str,
    label: str,
    *,
    ledger: Ledger,
    sha: str | None,
    snapshot: Snapshot | None,
    collection_error: CollectorError | None,
    now: dt.datetime,
    cache: dict[str, Snapshot],
) -> tuple[list[str], list[str], bool]:
    """Apply the remote snapshots and write the ledger back.

    Steps five and six of the publish sequence are retried together:
    a conflicting write means somebody else changed the ledger, so the
    remote state must be read again before the merge is redone.

    Parameters
    ----------
    client : GitHubClient
        The transport.
    settings : Settings
        Loaded settings.
    machine_id : str
        Machine identifier.
    label : str
        Machine label.
    ledger : Ledger
        The ledger fetched at the start of the run.
    sha : str or None
        Blob SHA of that ledger.
    snapshot : Snapshot or None
        The snapshot of this run.
    collection_error : CollectorError or None
        The collector failure, if any.
    now : datetime.datetime
        Timestamp recorded as ``last_run_at``.
    cache : dict of str to Snapshot
        Downloaded snapshots, reused across attempts.

    Returns
    -------
    tuple of (list of str, list of str, bool)
        The applied paths, the unreadable paths that were skipped and
        whether the ledger was written.

    Raises
    ------
    RemoteConflictError
        If the ledger still conflicts after the bounded retries.
    AuthError
        If the token may not write.
    GitHubError
        On any other transport failure.
    """
    agents = list(settings.collector.agents) if settings.collector else []
    current, current_sha = ledger, sha
    for attempt in range(LEDGER_ATTEMPTS):
        if attempt:
            current, current_sha = _fetch_ledger(
                client, settings, machine_id, label
            )
        current, applied, skipped = _apply_remote_snapshots(
            client, current, machine_id, cache
        )
        _record_agent_states(current, snapshot, collection_error, agents)
        current.last_run_at = now
        payload = current.to_pretty_json().encode('utf-8')
        path = ledger_remote_path(machine_id)
        try:
            client.put_file(
                path,
                payload,
                f'fleet-usage: update {path}',
                sha=current_sha,
            )
        except RemoteConflictError:
            if attempt + 1 >= LEDGER_ATTEMPTS:
                raise
            _LOGGER.info('ledger %s changed remotely, retrying', path)
            continue
        return applied, skipped, True
    raise AssertionError('unreachable')  # pragma: no cover


# -- the sequence ----------------------------------------------------


def publish(
    settings: Settings,
    paths: AppPaths,
    *,
    dry_run: bool = False,
    if_due: bool = False,
    now: dt.datetime | None = None,
    client_factory: ClientFactory | None = None,
    sleep: Callable[[float], None] | None = None,
) -> PublishResult:
    """Run one publish cycle.

    Parameters
    ----------
    settings : Settings
        Loaded settings; a viewer-only installation is rejected.
    paths : AppPaths
        Resolved application paths.
    dry_run : bool, optional
        Collect and summarise without writing the spool, the remote or
        the due marker.
    if_due : bool, optional
        Return immediately when the previous run is younger than the
        configured interval.
    now : datetime.datetime or None, optional
        Current instant; injected by the tests.
    client_factory : callable or None, optional
        Builds the GitHub client from the settings and the token.
    sleep : callable or None, optional
        Injected sleep for the upload throttle; defaults to
        :func:`time.sleep`.

    Returns
    -------
    PublishResult
        The outcome, including the exit code for the process.
    """
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    machine = settings.machine
    if machine is None or settings.collector is None:
        return PublishResult(
            exit_code=ExitCode.CONFIG_ERROR,
            message=(
                'publish needs the [machine] and [collector] sections; '
                "this installation is viewer-only (run 'fleet-usage init')"
            ),
        )
    if if_due and not is_due(
        paths.due_marker, moment, settings.sync.interval_minutes
    ):
        return PublishResult(
            exit_code=ExitCode.OK,
            message='not due yet',
            skipped='not-due',
        )

    paths.state_dir.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(str(paths.lock_file), timeout=0)
    try:
        lock.acquire()
    except filelock.Timeout:
        _LOGGER.info('another publish run holds %s', paths.lock_file)
        return PublishResult(
            exit_code=ExitCode.OK,
            message='another fleet-usage publish is already running',
            skipped='locked',
        )
    try:
        return _run(
            settings,
            paths,
            dry_run=dry_run,
            now=moment,
            client_factory=client_factory or default_client_factory,
            sleep=time.sleep if sleep is None else sleep,
        )
    finally:
        lock.release()


def _run(
    settings: Settings,
    paths: AppPaths,
    *,
    dry_run: bool,
    now: dt.datetime,
    client_factory: ClientFactory,
    sleep: Callable[[float], None],
) -> PublishResult:
    """Execute the locked part of the publish sequence.

    Parameters
    ----------
    settings : Settings
        Loaded settings with machine and collector present.
    paths : AppPaths
        Resolved application paths.
    dry_run : bool
        Whether every write is skipped.
    now : datetime.datetime
        Current instant in UTC.
    client_factory : callable
        Builds the GitHub client.
    sleep : callable
        Injected sleep.

    Returns
    -------
    PublishResult
        The outcome of the run.
    """
    machine = settings.machine
    assert machine is not None
    if dry_run:
        try:
            preview = collect_snapshot(settings, now=now)
        except CollectorError as exc:
            return PublishResult(
                exit_code=ExitCode.COLLECTION_FAILURE, message=str(exc)
            )
        return PublishResult(
            exit_code=ExitCode.OK,
            message='dry run: nothing was written',
            snapshot=preview,
        )

    token = resolve_token(settings)
    if token is None:
        return PublishResult(
            exit_code=ExitCode.CONFIG_ERROR,
            message=(
                f'{settings.github.token_env} is not set; put the GitHub '
                'token there or into the .env file next to settings.toml'
            ),
        )

    auth_error: AuthError | None = None
    conflict: RemoteConflictError | None = None
    collection_error: CollectorError | None = None
    document: Snapshot | None = None
    spooled = False
    deduplicated = False
    uploaded: list[str] = []
    applied: list[str] = []
    skipped_remote: list[str] = []
    ledger_written = False
    pending = 0
    network_error: GitHubError | None = None

    client = client_factory(settings, token)
    try:
        try:
            ledger, sha = _fetch_ledger(
                client, settings, machine.id, machine.label
            )
        except RemoteConflictError as exc:
            return PublishResult(
                exit_code=ExitCode.REMOTE_CONFLICT, message=str(exc)
            )
        except RateLimitError as exc:
            pending = len(spool.list_spool(paths.spool_dir))
            return PublishResult(
                exit_code=ExitCode.SPOOLED_NOT_UPLOADED,
                message=f'cannot reach GitHub: {exc}',
                pending=pending,
            )
        except AuthError as exc:
            return PublishResult(
                exit_code=ExitCode.AUTH_FAILURE, message=str(exc)
            )
        except GitHubError as exc:
            pending = len(spool.list_spool(paths.spool_dir))
            return PublishResult(
                exit_code=ExitCode.SPOOLED_NOT_UPLOADED,
                message=f'cannot reach GitHub: {exc}',
                pending=pending,
            )

        try:
            document = collect_snapshot(settings, now=now)
        except CollectorError as exc:
            _LOGGER.error('collection failed: %s', exc)
            collection_error = exc

        cache: dict[str, Snapshot] = {}
        if document is not None:
            spooled_hash = _newest_spooled_hash(paths.spool_dir)
            digest = snapshot_module.short_hash(document.agents_hash)
            if document.agents_hash == ledger.last_agents_hash:
                _LOGGER.info('collection is unchanged, nothing to spool')
                deduplicated = True
            elif spooled_hash == digest:
                # The remote is behind, so the ledger cannot vouch for
                # this payload, but the spool can: a machine that stays
                # offline must not accumulate one identical snapshot
                # per run.
                _LOGGER.info(
                    'collection equals the newest spooled snapshot, '
                    'nothing to spool'
                )
                deduplicated = True
            else:
                spool.write_snapshot(paths.spool_dir, document)
                spooled = True

        drained = _drain(client, paths.spool_dir, sleep=sleep)
        uploaded = drained.uploaded
        pending = drained.pending
        auth_error = drained.auth_error
        conflict = drained.conflict
        network_error = drained.network_error
        cache.update(drained.documents)

        if auth_error is None and conflict is None:
            try:
                applied, skipped_remote, ledger_written = _write_ledger(
                    client,
                    settings,
                    machine.id,
                    machine.label,
                    ledger=ledger,
                    sha=sha,
                    snapshot=document,
                    collection_error=collection_error,
                    now=now,
                    cache=cache,
                )
            except RemoteConflictError as exc:
                _LOGGER.error('%s', exc)
                conflict = exc
            except RateLimitError as exc:
                _LOGGER.warning('cannot write the ledger: %s', exc)
                network_error = exc
            except AuthError as exc:
                _LOGGER.error('%s', exc)
                auth_error = exc
            except GitHubError as exc:
                _LOGGER.warning('cannot write the ledger: %s', exc)
                network_error = exc
    finally:
        client.close()
        write_due_marker(paths.due_marker, now)

    code = exit_code_for(
        auth_error=auth_error,
        conflict=conflict,
        pending=pending,
        network_error=network_error,
        collection_error=collection_error,
    )
    return PublishResult(
        exit_code=code,
        message=_message(
            code,
            uploaded=uploaded,
            pending=pending,
            applied=applied,
            skipped_remote=skipped_remote,
            deduplicated=deduplicated,
            auth_error=auth_error,
            conflict=conflict,
            network_error=network_error,
            collection_error=collection_error,
        ),
        snapshot=document,
        spooled=spooled,
        deduplicated=deduplicated,
        uploaded=tuple(uploaded),
        pending=pending,
        applied=tuple(applied),
        ledger_written=ledger_written,
        skipped_remote=tuple(skipped_remote),
    )


def _message(
    code: ExitCode,
    *,
    uploaded: list[str],
    pending: int,
    applied: list[str],
    skipped_remote: list[str],
    deduplicated: bool,
    auth_error: AuthError | None,
    conflict: RemoteConflictError | None,
    network_error: GitHubError | None,
    collection_error: CollectorError | None,
) -> str:
    """Compose the one line summary of a run.

    Parameters
    ----------
    code : ExitCode
        The exit code that was determined.
    uploaded : list of str
        Confirmed remote paths.
    pending : int
        Files left in the spool.
    applied : list of str
        Snapshots merged into the ledger.
    skipped_remote : list of str
        Remote documents that could not be read as snapshots.
    deduplicated : bool
        Whether the collection was identical to the last one.
    auth_error : AuthError or None
        The permission failure, if any.
    conflict : RemoteConflictError or None
        The conflict, if any.
    network_error : GitHubError or None
        The transport failure, if any.
    collection_error : CollectorError or None
        The collector failure, if any.

    Returns
    -------
    str
        A message written for a human operator.
    """
    if code is ExitCode.AUTH_FAILURE:
        return str(auth_error)
    if code is ExitCode.REMOTE_CONFLICT:
        return str(conflict)
    if code is ExitCode.SPOOLED_NOT_UPLOADED:
        if not pending:
            return f'cannot reach GitHub: {network_error}'
        noun = 'snapshot' if pending == 1 else 'snapshots'
        return (
            f'{pending} {noun} stay in the spool and will be uploaded '
            f'on the next run ({network_error})'
            if network_error is not None
            else f'{pending} {noun} stay in the spool and will be '
            'uploaded on the next run'
        )
    if code is ExitCode.COLLECTION_FAILURE:
        return f'collection failed: {collection_error}'
    parts = [f'uploaded {len(uploaded)}', f'applied {len(applied)}']
    if deduplicated:
        parts.append('collection unchanged')
    if skipped_remote:
        noun = 'document' if len(skipped_remote) == 1 else 'documents'
        parts.append(
            f'skipped {len(skipped_remote)} unreadable remote {noun} '
            f'({", ".join(sorted(skipped_remote))})'
        )
    return ', '.join(parts)
