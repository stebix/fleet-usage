"""Merging of snapshots into the derived per-machine ledger.

A day record is settled once the snapshot that produced it was collected
at least ``freeze_window_days`` after the end of that day in the
reporting timezone; settled records are never replaced. The merge is
order independent: applying the same set of snapshots in any sequence
must produce the same day records, which is what makes it safe to fold
in whatever snapshots happen to be visible on the remote.

The rules, applied per agent with status ``'ok'`` and per date ``D`` of
a snapshot ``S`` collected at ``T``:

1. no record for ``D`` yet: insert it, whatever the age of ``S``;
2. an existing settled record whose source is newer than ``T``, where
   ``T`` is itself at or after the freeze boundary of ``D``: replace it,
   because among observations made after the boundary the earliest one
   saw the most data, the collector only ever losing history to
   retention;
3. an existing record with ``source_collected_at >= T`` that rule two
   does not cover: ignore ``S``, because it describes an older
   observation of the same day;
4. an existing settled record: refuse the replacement;
5. otherwise: replace the record and remember ``T`` as its source.

A replacement that lowers a token counter, and a refusal that would have
lowered one, are recorded in :attr:`Ledger.anomalies`. In both cases
``kept`` is the value that survived in the ledger and ``observed`` is
the value that lost; ``kind`` says which of the two won, so that an
operator can see where the collector disagreed with itself.

Anomalies are keyed by ``(agent, date, field, kind)``: a repeated
disagreement about the same counter replaces the previous entry instead
of appending to it, which keeps a ledger that is rewritten every hour
from growing without bound. The list is capped at
:data:`MAX_ANOMALIES` entries, the oldest dates being dropped first, and
is kept sorted so that a rebuild produces the same document as the
incremental merge.
"""

import datetime as dt
import zoneinfo
from collections.abc import Iterable, Sequence

from fleet_usage.models import (
    Anomaly,
    AnomalyKind,
    DayRecord,
    Ledger,
    LedgerAgent,
    LedgerDayRecord,
    MergePolicy,
    Snapshot,
)
from fleet_usage.snapshot import parse_snapshot_name

__all__ = [
    'MAX_ANOMALIES',
    'TOKEN_FIELDS',
    'apply_snapshot',
    'is_settled',
    'new_ledger',
    'rebuild_ledger',
    'settle_boundary',
]

TOKEN_FIELDS: tuple[str, ...] = (
    'input_tokens',
    'output_tokens',
    'cache_create_tokens',
    'cache_read_tokens',
)

#: Upper bound on :attr:`Ledger.anomalies`; the ledger is a report, not
#: an archive, and the snapshots keep the full history anyway.
MAX_ANOMALIES = 500


def new_ledger(machine_id: str, label: str, policy: MergePolicy) -> Ledger:
    """Create an empty ledger for one machine.

    Parameters
    ----------
    machine_id : str
        Identity of the machine.
    label : str
        Human readable machine name.
    policy : MergePolicy
        Freeze window and reporting timezone.

    Returns
    -------
    Ledger
        A ledger without agents, anomalies or applied snapshots.
    """
    return Ledger(
        machine_id=machine_id,
        label=label,
        merge_policy=policy.model_copy(deep=True),
    )


def settle_boundary(record_date: dt.date, policy: MergePolicy) -> dt.datetime:
    """Return the instant at which records for ``record_date`` freeze.

    The boundary is wall clock arithmetic in the reporting timezone:
    midnight at the start of the day after ``record_date``, moved
    forward by ``freeze_window_days`` calendar days. Adding calendar
    days rather than 24 hour blocks keeps the boundary at local midnight
    across a daylight saving transition.

    Parameters
    ----------
    record_date : datetime.date
        The calendar day of the record.
    policy : MergePolicy
        Freeze window and reporting timezone.

    Returns
    -------
    datetime.datetime
        The boundary as an aware UTC timestamp.

    Raises
    ------
    ValueError
        If the policy names an unknown timezone.
    """
    try:
        zone = zoneinfo.ZoneInfo(policy.timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        msg = f'unknown timezone {policy.timezone!r}'
        raise ValueError(msg) from exc
    boundary_day = record_date + dt.timedelta(
        days=1 + policy.freeze_window_days
    )
    local = dt.datetime.combine(boundary_day, dt.time.min, tzinfo=zone)
    return local.astimezone(dt.UTC)


def is_settled(
    record_date: dt.date,
    source_collected_at: dt.datetime,
    policy: MergePolicy,
) -> bool:
    """Decide whether a day record can still change.

    Parameters
    ----------
    record_date : datetime.date
        The calendar day of the record.
    source_collected_at : datetime.datetime
        When the snapshot behind the record was collected.
    policy : MergePolicy
        Freeze window and reporting timezone.

    Returns
    -------
    bool
        ``True`` when the record was collected at or after the freeze
        boundary and is therefore final.

    Raises
    ------
    ValueError
        If ``source_collected_at`` is naive or the policy names an
        unknown timezone.
    """
    if source_collected_at.tzinfo is None:
        msg = 'source_collected_at must be timezone aware'
        raise ValueError(msg)
    boundary = settle_boundary(record_date, policy)
    return source_collected_at.astimezone(dt.UTC) >= boundary


def _to_ledger_record(
    day: DayRecord, source_collected_at: dt.datetime, path: str
) -> LedgerDayRecord:
    """Turn a snapshot day into a ledger day.

    Parameters
    ----------
    day : DayRecord
        The record as reported by the collector.
    source_collected_at : datetime.datetime
        Collection instant of the snapshot it came from.
    path : str
        Remote path of that snapshot.

    Returns
    -------
    LedgerDayRecord
        The same numbers plus merge bookkeeping; ``settled`` is
        recomputed by the caller.
    """
    return LedgerDayRecord(
        source_collected_at=source_collected_at,
        source_snapshot=path,
        settled=False,
        **day.model_dump(),
    )


def _decreases(
    existing: LedgerDayRecord, candidate: DayRecord
) -> list[tuple[str, int, int]]:
    """List token counters that would shrink.

    Parameters
    ----------
    existing : LedgerDayRecord
        The record currently in the ledger.
    candidate : DayRecord
        The record offered by a newer snapshot.

    Returns
    -------
    list of (str, int, int)
        Field name, the value currently in the ledger and the value the
        snapshot reports, in a stable field order.
    """
    found = []
    for field in TOKEN_FIELDS:
        kept = int(getattr(existing, field))
        observed = int(getattr(candidate, field))
        if observed < kept:
            found.append((field, kept, observed))
    return found


def _increases(
    existing: LedgerDayRecord, candidate: DayRecord
) -> list[tuple[str, int, int]]:
    """List token counters that the ledger reports below the candidate.

    Used for the one case in which the record already in the ledger is
    the younger observation: the older one wins, so the ledger value is
    the one that loses.

    Parameters
    ----------
    existing : LedgerDayRecord
        The record currently in the ledger.
    candidate : DayRecord
        The record offered by an older snapshot.

    Returns
    -------
    list of (str, int, int)
        Field name, the value that is kept, which is the one the
        snapshot reports, and the value that loses, which is the one in
        the ledger, in a stable field order.
    """
    found = []
    for field in TOKEN_FIELDS:
        observed = int(getattr(existing, field))
        kept = int(getattr(candidate, field))
        if observed < kept:
            found.append((field, kept, observed))
    return found


def _merge_agent_days(
    entry: LedgerAgent,
    days: Sequence[DayRecord],
    collected_at: dt.datetime,
    agent: str,
    path: str,
    policy: MergePolicy,
) -> list[Anomaly]:
    """Fold the days of one agent into its ledger entry.

    Parameters
    ----------
    entry : LedgerAgent
        Ledger entry, updated in place.
    days : Sequence[DayRecord]
        Day records of the snapshot, for a single agent.
    collected_at : datetime.datetime
        Collection instant of the snapshot.
    agent : str
        Agent name, recorded in anomalies.
    path : str
        Remote path of the snapshot, recorded in anomalies.
    policy : MergePolicy
        Freeze window and reporting timezone.

    Returns
    -------
    list of Anomaly
        Anomalies produced by this agent, in date order.
    """
    anomalies: list[Anomaly] = []
    by_date = {record.date: record for record in entry.days}
    for day in sorted(days, key=lambda record: record.date):
        existing = by_date.get(day.date)
        if existing is None:
            by_date[day.date] = _to_ledger_record(day, collected_at, path)
            continue
        if collected_at <= existing.source_collected_at:
            if not _wins_over_a_later_observation(
                existing, collected_at, policy
            ):
                # The snapshot is not newer than what produced the
                # record and does not predate it inside the frozen
                # zone, so it carries nothing we do not already have.
                continue
            # Both observations are past the freeze boundary, where the
            # collector can only lose history: the earlier one, which is
            # this snapshot, is the more complete of the two.
            anomalies.extend(
                Anomaly(
                    agent=agent,
                    date=day.date,
                    field=field,
                    kept=kept,
                    observed=observed,
                    snapshot=existing.source_snapshot or path,
                    kind='decrease_refused',
                )
                for field, kept, observed in _increases(existing, day)
            )
            by_date[day.date] = _to_ledger_record(day, collected_at, path)
            continue
        settled = is_settled(
            existing.date, existing.source_collected_at, policy
        )
        shrinking = _decreases(existing, day)
        kind: AnomalyKind = (
            'decrease_refused' if settled else 'decrease_applied'
        )
        anomalies.extend(
            Anomaly(
                agent=agent,
                date=day.date,
                field=field,
                kept=kept,
                observed=observed,
                snapshot=path,
                kind=kind,
            )
            for field, kept, observed in shrinking
        )
        if settled:
            continue
        by_date[day.date] = _to_ledger_record(day, collected_at, path)
    entry.days = sorted(by_date.values(), key=lambda record: record.date)
    return anomalies


def _wins_over_a_later_observation(
    existing: LedgerDayRecord,
    collected_at: dt.datetime,
    policy: MergePolicy,
) -> bool:
    """Whether an older snapshot may replace a settled record.

    After the freeze boundary of a day the collector never gains data
    about it, it only loses it to retention, so the earliest observation
    made after that boundary is the most complete one. Applying that
    rule is what makes the merge order independent for a day that two
    late snapshots disagree about.

    Parameters
    ----------
    existing : LedgerDayRecord
        The record currently in the ledger.
    collected_at : datetime.datetime
        Collection instant of the snapshot being applied.
    policy : MergePolicy
        Freeze window and reporting timezone.

    Returns
    -------
    bool
        ``True`` when the snapshot is strictly older than the record,
        the record is settled and the snapshot was itself collected at
        or after the freeze boundary of the day.
    """
    return (
        collected_at < existing.source_collected_at
        and is_settled(existing.date, existing.source_collected_at, policy)
        and is_settled(existing.date, collected_at, policy)
    )


def _anomaly_key(anomaly: Anomaly) -> tuple[str, dt.date, str, str]:
    """Return the identity of an anomaly.

    Parameters
    ----------
    anomaly : Anomaly
        The entry to key.

    Returns
    -------
    tuple
        Agent, date, field and kind; two anomalies sharing this key
        describe the same disagreement and only the newer one is kept.
    """
    return (anomaly.agent, anomaly.date, anomaly.field, anomaly.kind)


def _sort_key(anomaly: Anomaly) -> tuple[dt.date, str, str, str]:
    """Return the display order of an anomaly.

    Parameters
    ----------
    anomaly : Anomaly
        The entry to order.

    Returns
    -------
    tuple
        Date, agent, field and kind, which is also the order in which
        entries are dropped once the list is full.
    """
    return (anomaly.date, anomaly.agent, anomaly.field, anomaly.kind)


def _merge_anomalies(
    existing: Sequence[Anomaly], found: Iterable[Anomaly]
) -> list[Anomaly]:
    """Fold new anomalies into the list a ledger already carries.

    A machine that publishes hourly sees the same disagreement again in
    every snapshot, so anomalies are keyed by
    ``(agent, date, field, kind)`` and a repeat replaces its
    predecessor, keeping the newest snapshot path and observed value but
    the value that was originally kept.

    Parameters
    ----------
    existing : Sequence[Anomaly]
        The anomalies already in the ledger.
    found : Iterable[Anomaly]
        The anomalies produced by the snapshot being applied.

    Returns
    -------
    list of Anomaly
        At most :data:`MAX_ANOMALIES` entries, the oldest dates dropped
        first, sorted by date, agent, field and kind.
    """
    merged: dict[tuple[str, dt.date, str, str], Anomaly] = {
        _anomaly_key(anomaly): anomaly for anomaly in existing
    }
    for anomaly in found:
        key = _anomaly_key(anomaly)
        previous = merged.get(key)
        merged[key] = (
            anomaly
            if previous is None
            else anomaly.model_copy(update={'kept': previous.kept})
        )
    ordered = sorted(merged.values(), key=_sort_key)
    return ordered[-MAX_ANOMALIES:]


def _resettle(ledger: Ledger) -> None:
    """Recompute the ``settled`` flag of every record.

    The flag depends on the current date only through the freeze
    boundary of each record, so it is derived rather than stored
    permanently; recomputing keeps it correct after a policy change.

    Parameters
    ----------
    ledger : Ledger
        Ledger to update in place.
    """
    for entry in ledger.agents.values():
        for record in entry.days:
            record.settled = is_settled(
                record.date, record.source_collected_at, ledger.merge_policy
            )


def _newest_applied(ledger: Ledger) -> dt.datetime | None:
    """Return the instant of the newest snapshot applied so far.

    Parameters
    ----------
    ledger : Ledger
        The ledger to inspect.

    Returns
    -------
    datetime.datetime or None
        The timestamp encoded in ``applied_through``, or ``None`` when
        no snapshot was applied yet or the name is not parseable.
    """
    if ledger.applied_through is None:
        return None
    try:
        moment, _ = parse_snapshot_name(ledger.applied_through)
    except ValueError:
        return None
    return moment


def apply_snapshot(ledger: Ledger, snapshot: Snapshot, path: str) -> Ledger:
    """Fold ``snapshot`` into ``ledger`` and return the result.

    The input ledger is never modified: merging is a pure function of
    the ledger and the snapshot, which makes a failed publish trivially
    recoverable and the merge easy to test.

    Parameters
    ----------
    ledger : Ledger
        The ledger to update.
    snapshot : Snapshot
        The snapshot to apply.
    path : str
        Remote path of the snapshot, recorded in anomalies and in
        ``applied_through``.

    Returns
    -------
    Ledger
        A new ledger with the snapshot applied.
    """
    updated = ledger.model_copy(deep=True)
    collected_at = snapshot.collected_at.astimezone(dt.UTC)
    previous_newest = _newest_applied(updated)
    for agent, payload in sorted(snapshot.agents.items()):
        entry = updated.agents.get(agent)
        if entry is None:
            entry = LedgerAgent()
            updated.agents[agent] = entry
        if payload.status != 'ok':
            entry.last_error = payload.message or (
                f'collector reported status {payload.status!r}'
            )
            continue
        updated.anomalies = _merge_anomalies(
            updated.anomalies,
            _merge_agent_days(
                entry,
                payload.days,
                collected_at,
                agent,
                path,
                updated.merge_policy,
            ),
        )
        if (
            entry.last_success_at is None
            or collected_at > entry.last_success_at
        ):
            entry.last_success_at = collected_at
    updated.agents = {
        key: updated.agents[key] for key in sorted(updated.agents)
    }
    if previous_newest is None or collected_at >= previous_newest:
        updated.last_agents_hash = snapshot.agents_hash
    if updated.applied_through is None or path > updated.applied_through:
        updated.applied_through = path
    _resettle(updated)
    return updated


def rebuild_ledger(
    machine_id: str,
    label: str,
    policy: MergePolicy,
    snapshots: Iterable[tuple[str, Snapshot]],
) -> Ledger:
    """Fold every snapshot into an empty ledger.

    Parameters
    ----------
    machine_id : str
        Machine the ledger belongs to.
    label : str
        Machine label.
    policy : MergePolicy
        Freeze window and reporting timezone.
    snapshots : Iterable[tuple[str, Snapshot]]
        Remote path and snapshot, in any order; they are applied in
        path order, which is chronological.

    Returns
    -------
    Ledger
        The rebuilt ledger.
    """
    ledger = new_ledger(machine_id, label, policy)
    for path, snapshot in sorted(snapshots, key=lambda item: item[0]):
        ledger = apply_snapshot(ledger, snapshot, path)
    return ledger
