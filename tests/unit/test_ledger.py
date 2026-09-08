"""Tests for settling and merging snapshots into the ledger."""

import datetime as dt
import itertools
import zoneinfo
from decimal import Decimal

import pytest

from fleet_usage.ledger import (
    MAX_ANOMALIES,
    apply_snapshot,
    is_settled,
    new_ledger,
    rebuild_ledger,
    settle_boundary,
)
from fleet_usage.models import (
    AgentSnapshot,
    CollectorInfo,
    DayRecord,
    MergePolicy,
    ModelRecord,
    Snapshot,
)
from fleet_usage.snapshot import agents_hash, snapshot_remote_path

MACHINE_ID = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'
LABEL = 'fedora-mobile'
BERLIN = zoneinfo.ZoneInfo('Europe/Berlin')
POLICY = MergePolicy(version=1, freeze_window_days=5, timezone='Europe/Berlin')


def policy(freeze_window_days=5, timezone='Europe/Berlin'):
    return MergePolicy(
        version=1,
        freeze_window_days=freeze_window_days,
        timezone=timezone,
    )


def day(date, tokens, *, output=0, cache_create=0, cache_read=0, cost='1.5'):
    """Build a snapshot day record with a single model."""
    return DayRecord(
        date=date,
        input_tokens=tokens,
        output_tokens=output,
        cache_create_tokens=cache_create,
        cache_read_tokens=cache_read,
        cost_usd=Decimal(cost),
        cost_status='estimated',
        models=[
            ModelRecord(
                model='model-a',
                input_tokens=tokens,
                output_tokens=output,
                cache_create_tokens=cache_create,
                cache_read_tokens=cache_read,
                cost_usd=Decimal(cost),
                cost_status='estimated',
            )
        ],
    )


def snapshot(collected_at, agents):
    """Build a snapshot from a mapping of agent name to AgentSnapshot."""
    return Snapshot(
        machine_id=MACHINE_ID,
        label=LABEL,
        collected_at=collected_at,
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash=agents_hash(agents),
        agents=agents,
    )


def ok_snapshot(collected_at, days, agent='claude'):
    return snapshot(
        collected_at, {agent: AgentSnapshot(status='ok', days=days)}
    )


def utc(year, month, dayofmonth, hour=0, minute=0, second=0):
    return dt.datetime(
        year, month, dayofmonth, hour, minute, second, tzinfo=dt.UTC
    )


def empty():
    return new_ledger(MACHINE_ID, LABEL, POLICY)


def apply(ledger, snap):
    return apply_snapshot(ledger, snap, snapshot_remote_path(snap))


def records(ledger, agent='claude'):
    return {record.date: record for record in ledger.agents[agent].days}


# --------------------------------------------------------------- settling


def test_settle_boundary_is_local_midnight_after_the_window():
    boundary = settle_boundary(dt.date(2026, 9, 7), POLICY)
    assert boundary == utc(2026, 9, 12, 22)
    assert boundary.astimezone(BERLIN) == dt.datetime(
        2026, 9, 13, tzinfo=BERLIN
    )


def test_zero_window_settles_at_the_end_of_the_day():
    boundary = settle_boundary(dt.date(2026, 9, 7), policy(0))
    assert boundary == utc(2026, 9, 7, 22)


def test_is_settled_is_exact_at_the_boundary():
    date = dt.date(2026, 9, 7)
    boundary = settle_boundary(date, POLICY)
    assert not is_settled(date, boundary - dt.timedelta(seconds=1), POLICY)
    assert is_settled(date, boundary, POLICY)
    assert is_settled(date, boundary + dt.timedelta(seconds=1), POLICY)


def test_is_settled_accepts_any_input_timezone():
    date = dt.date(2026, 9, 7)
    boundary = settle_boundary(date, POLICY)
    assert is_settled(date, boundary.astimezone(BERLIN), POLICY)


def test_is_settled_rejects_naive_timestamps():
    with pytest.raises(ValueError, match='timezone aware'):
        is_settled(dt.date(2026, 9, 7), dt.datetime(2026, 9, 20), POLICY)


def test_unknown_timezone_is_reported():
    broken = MergePolicy(freeze_window_days=5, timezone='Europe/Berlin')
    object.__setattr__(broken, 'timezone', 'Mars/Olympus')
    with pytest.raises(ValueError, match='unknown timezone'):
        settle_boundary(dt.date(2026, 9, 7), broken)


def test_boundary_across_the_march_transition():
    # Berlin switches to summer time on 2026-03-29. Local midnight on
    # 2026-03-30 is 22:00 UTC, not 23:00 UTC, so the freeze window must
    # be counted in calendar days rather than in 24 hour blocks.
    date = dt.date(2026, 3, 28)
    boundary = settle_boundary(date, policy(1))
    assert boundary == utc(2026, 3, 29, 22)
    assert boundary.astimezone(BERLIN).hour == 0
    assert not is_settled(date, utc(2026, 3, 29, 21, 59, 59), policy(1))
    assert is_settled(date, boundary, policy(1))


def test_boundary_before_the_march_transition_is_still_winter_time():
    boundary = settle_boundary(dt.date(2026, 3, 27), policy(0))
    assert boundary == utc(2026, 3, 27, 23)


def test_boundary_across_the_october_transition():
    # Berlin returns to winter time on 2026-10-25, so local midnight on
    # 2026-10-26 is 23:00 UTC.
    date = dt.date(2026, 10, 24)
    boundary = settle_boundary(date, policy(1))
    assert boundary == utc(2026, 10, 25, 23)
    assert boundary.astimezone(BERLIN).hour == 0
    assert not is_settled(date, utc(2026, 10, 25, 22, 59, 59), policy(1))
    assert is_settled(date, boundary, policy(1))


def test_boundary_before_the_october_transition_is_still_summer_time():
    boundary = settle_boundary(dt.date(2026, 10, 23), policy(0))
    assert boundary == utc(2026, 10, 23, 22)


def test_timezone_matters():
    date = dt.date(2026, 9, 7)
    berlin = settle_boundary(date, policy(0, 'Europe/Berlin'))
    tokyo = settle_boundary(date, policy(0, 'Asia/Tokyo'))
    assert tokyo < berlin
    assert tokyo == utc(2026, 9, 7, 15)


# ------------------------------------------------------------- insertion


def test_insert_into_an_empty_ledger():
    ledger = apply(
        empty(),
        ok_snapshot(utc(2026, 9, 8, 10), [day(dt.date(2026, 9, 8), 100)]),
    )
    record = records(ledger)[dt.date(2026, 9, 8)]
    assert record.input_tokens == 100
    assert record.source_collected_at == utc(2026, 9, 8, 10)
    assert record.settled is False
    assert record.models[0].model == 'model-a'
    assert record.cost_usd == Decimal('1.5')


def test_a_very_old_snapshot_still_inserts_a_missing_day():
    # Rule 2 of the merge: a day that is not in the ledger is inserted
    # whatever the age of the snapshot, so a machine that was offline
    # for a week does not lose its history.
    snap = ok_snapshot(utc(2026, 9, 30, 10), [day(dt.date(2026, 9, 1), 42)])
    ledger = apply(empty(), snap)
    record = records(ledger)[dt.date(2026, 9, 1)]
    assert record.input_tokens == 42
    assert record.settled is True


def test_apply_does_not_mutate_the_input_ledger():
    before = apply(
        empty(),
        ok_snapshot(utc(2026, 9, 8, 10), [day(dt.date(2026, 9, 8), 100)]),
    )
    frozen = before.to_canonical_json()
    apply(
        before,
        ok_snapshot(utc(2026, 9, 8, 11), [day(dt.date(2026, 9, 8), 200)]),
    )
    assert before.to_canonical_json() == frozen


def test_applying_the_same_snapshot_twice_changes_nothing():
    snap = ok_snapshot(
        utc(2026, 9, 8, 10),
        [day(dt.date(2026, 9, 7), 100), day(dt.date(2026, 9, 8), 50)],
    )
    once = apply(empty(), snap)
    twice = apply(once, snap)
    assert twice.to_canonical_json() == once.to_canonical_json()


def test_days_and_models_stay_sorted():
    days = [
        day(dt.date(2026, 9, 9), 1),
        day(dt.date(2026, 9, 7), 2),
        day(dt.date(2026, 9, 8), 3),
    ]
    days[0].models = [
        ModelRecord(model='zeta', input_tokens=1),
        ModelRecord(model='alpha', input_tokens=1),
    ]
    ledger = apply(empty(), ok_snapshot(utc(2026, 9, 10, 10), days))
    entry = ledger.agents['claude']
    assert [record.date for record in entry.days] == sorted(
        record.date for record in entry.days
    )
    names = [model.model for model in entry.days[-1].models]
    assert names == ['alpha', 'zeta']


def test_agents_are_merged_independently():
    first = snapshot(
        utc(2026, 9, 8, 10),
        {
            'claude': AgentSnapshot(
                status='ok', days=[day(dt.date(2026, 9, 8), 100)]
            ),
            'codex': AgentSnapshot(
                status='ok', days=[day(dt.date(2026, 9, 8), 5)]
            ),
        },
    )
    second = ok_snapshot(
        utc(2026, 9, 8, 11), [day(dt.date(2026, 9, 8), 300)], agent='claude'
    )
    ledger = apply(apply(empty(), first), second)
    assert records(ledger, 'claude')[dt.date(2026, 9, 8)].input_tokens == 300
    assert records(ledger, 'codex')[dt.date(2026, 9, 8)].input_tokens == 5
    assert list(ledger.agents) == ['claude', 'codex']


# ----------------------------------------------------------- replacement


def test_monday_morning_is_corrected_by_the_sunday_snapshot():
    # The Monday run only sees the usage up to that moment; the run of
    # the following Sunday reports the complete day, is past the freeze
    # boundary and therefore both corrects and settles the record.
    monday = dt.date(2026, 9, 7)
    partial = ok_snapshot(utc(2026, 9, 7, 7), [day(monday, 100)])
    complete = ok_snapshot(utc(2026, 9, 13, 10), [day(monday, 150)])
    ledger = apply(apply(empty(), partial), complete)
    record = records(ledger)[monday]
    assert record.input_tokens == 150
    assert record.settled is True
    assert record.source_collected_at == utc(2026, 9, 13, 10)
    assert ledger.anomalies == []


def test_the_monday_case_is_order_independent():
    monday = dt.date(2026, 9, 7)
    partial = ok_snapshot(utc(2026, 9, 7, 7), [day(monday, 100)])
    complete = ok_snapshot(utc(2026, 9, 13, 10), [day(monday, 150)])
    forwards = apply(apply(empty(), partial), complete)
    backwards = apply(apply(empty(), complete), partial)
    assert records(backwards)[monday].input_tokens == 150
    assert records(backwards)[monday].settled is True
    assert backwards.anomalies == []
    assert (
        records(backwards)[monday].source_collected_at
        == records(forwards)[monday].source_collected_at
    )


def test_an_older_snapshot_never_overwrites_a_newer_record():
    date = dt.date(2026, 9, 8)
    newer = ok_snapshot(utc(2026, 9, 8, 18), [day(date, 900)])
    older = ok_snapshot(utc(2026, 9, 8, 9), [day(date, 100)])
    ledger = apply(apply(empty(), newer), older)
    record = records(ledger)[date]
    assert record.input_tokens == 900
    assert record.source_collected_at == utc(2026, 9, 8, 18)
    assert ledger.anomalies == []
    assert ledger.applied_through == snapshot_remote_path(newer)
    assert ledger.last_agents_hash == newer.agents_hash


def test_dates_absent_from_a_snapshot_are_kept():
    first = ok_snapshot(
        utc(2026, 9, 8, 10),
        [day(dt.date(2026, 9, 7), 100), day(dt.date(2026, 9, 8), 20)],
    )
    second = ok_snapshot(utc(2026, 9, 8, 12), [day(dt.date(2026, 9, 8), 30)])
    ledger = apply(apply(empty(), first), second)
    assert sorted(records(ledger)) == [
        dt.date(2026, 9, 7),
        dt.date(2026, 9, 8),
    ]
    assert records(ledger)[dt.date(2026, 9, 7)].input_tokens == 100
    assert records(ledger)[dt.date(2026, 9, 8)].input_tokens == 30


def test_settled_records_are_never_replaced():
    date = dt.date(2026, 9, 1)
    settled = ok_snapshot(utc(2026, 9, 7, 10), [day(date, 500)])
    later = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 900)])
    ledger = apply(apply(empty(), settled), later)
    record = records(ledger)[date]
    assert record.settled is True
    assert record.input_tokens == 500
    assert record.source_collected_at == utc(2026, 9, 7, 10)


# ------------------------------------------------------------- anomalies


def test_a_decrease_below_the_freeze_window_is_applied_and_logged():
    date = dt.date(2026, 9, 8)
    first = ok_snapshot(utc(2026, 9, 8, 12), [day(date, 500)])
    second = ok_snapshot(utc(2026, 9, 8, 18), [day(date, 400)])
    ledger = apply(apply(empty(), first), second)
    record = records(ledger)[date]
    assert record.input_tokens == 400
    assert record.settled is False
    assert len(ledger.anomalies) == 1
    anomaly = ledger.anomalies[0]
    assert anomaly.kind == 'decrease_applied'
    assert anomaly.agent == 'claude'
    assert anomaly.date == date
    assert anomaly.field == 'input_tokens'
    assert anomaly.kept == 500
    assert anomaly.observed == 400
    assert anomaly.snapshot == snapshot_remote_path(second)


def test_a_decrease_of_a_settled_record_is_refused_and_logged():
    date = dt.date(2026, 9, 1)
    first = ok_snapshot(utc(2026, 9, 7, 10), [day(date, 500)])
    second = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 400)])
    ledger = apply(apply(empty(), first), second)
    record = records(ledger)[date]
    assert record.input_tokens == 500
    assert record.settled is True
    assert len(ledger.anomalies) == 1
    anomaly = ledger.anomalies[0]
    assert anomaly.kind == 'decrease_refused'
    assert anomaly.kept == 500
    assert anomaly.observed == 400
    assert anomaly.snapshot == snapshot_remote_path(second)


def test_every_shrinking_field_is_reported():
    date = dt.date(2026, 9, 8)
    first = ok_snapshot(
        utc(2026, 9, 8, 12),
        [day(date, 500, output=40, cache_create=30, cache_read=20)],
    )
    second = ok_snapshot(
        utc(2026, 9, 8, 18),
        [day(date, 500, output=39, cache_create=30, cache_read=19)],
    )
    ledger = apply(apply(empty(), first), second)
    # Anomalies are sorted by date, agent, field and kind so that a
    # rebuild renders them exactly like the incremental merge.
    assert [anomaly.field for anomaly in ledger.anomalies] == [
        'cache_read_tokens',
        'output_tokens',
    ]
    assert all(
        anomaly.kind == 'decrease_applied' for anomaly in ledger.anomalies
    )


def test_growth_produces_no_anomaly():
    date = dt.date(2026, 9, 8)
    first = ok_snapshot(utc(2026, 9, 8, 12), [day(date, 500)])
    second = ok_snapshot(utc(2026, 9, 8, 18), [day(date, 501)])
    ledger = apply(apply(empty(), first), second)
    assert ledger.anomalies == []
    assert records(ledger)[date].input_tokens == 501


def test_an_older_snapshot_with_lower_numbers_is_not_an_anomaly():
    date = dt.date(2026, 9, 8)
    newer = ok_snapshot(utc(2026, 9, 8, 18), [day(date, 900)])
    older = ok_snapshot(utc(2026, 9, 8, 9), [day(date, 100)])
    ledger = apply(apply(empty(), newer), older)
    assert ledger.anomalies == []


# ---------------------------------------------------------------- errors


def test_an_errored_agent_only_sets_the_last_error():
    date = dt.date(2026, 9, 8)
    good = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 100)])
    broken = snapshot(
        utc(2026, 9, 8, 11),
        {'claude': AgentSnapshot(status='error', message='ccusage exploded')},
    )
    ledger = apply(apply(empty(), good), broken)
    entry = ledger.agents['claude']
    assert entry.last_error == 'ccusage exploded'
    assert entry.last_success_at == utc(2026, 9, 8, 10)
    assert [record.input_tokens for record in entry.days] == [100]
    assert ledger.anomalies == []


def test_an_errored_agent_that_is_new_gets_an_empty_entry():
    broken = snapshot(
        utc(2026, 9, 8, 11),
        {'codex': AgentSnapshot(status='error', message='no such agent')},
    )
    entry = apply(empty(), broken).agents['codex']
    assert entry.days == []
    assert entry.last_error == 'no such agent'
    assert entry.last_success_at is None


def test_last_success_at_tracks_the_newest_successful_snapshot():
    date = dt.date(2026, 9, 8)
    first = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 100)])
    second = ok_snapshot(utc(2026, 9, 8, 16), [day(date, 200)])
    ledger = apply(apply(empty(), first), second)
    assert ledger.agents['claude'].last_success_at == utc(2026, 9, 8, 16)
    ledger = apply(ledger, first)
    assert ledger.agents['claude'].last_success_at == utc(2026, 9, 8, 16)


def test_an_empty_successful_agent_is_recorded():
    ledger = apply(empty(), ok_snapshot(utc(2026, 9, 8, 10), []))
    entry = ledger.agents['claude']
    assert entry.days == []
    assert entry.last_success_at == utc(2026, 9, 8, 10)


# ------------------------------------------------------------ bookkeeping


def test_applied_through_and_hash_follow_the_newest_snapshot():
    first = ok_snapshot(utc(2026, 9, 8, 10), [day(dt.date(2026, 9, 8), 1)])
    second = ok_snapshot(utc(2026, 9, 8, 16), [day(dt.date(2026, 9, 8), 2)])
    ledger = apply(apply(empty(), first), second)
    assert ledger.applied_through == snapshot_remote_path(second)
    assert ledger.last_agents_hash == second.agents_hash


def test_applied_through_never_moves_backwards():
    first = ok_snapshot(utc(2026, 9, 8, 10), [day(dt.date(2026, 9, 8), 1)])
    second = ok_snapshot(utc(2026, 9, 8, 16), [day(dt.date(2026, 9, 8), 2)])
    ledger = apply(apply(apply(empty(), first), second), first)
    assert ledger.applied_through == snapshot_remote_path(second)
    assert ledger.last_agents_hash == second.agents_hash


def test_last_run_at_is_left_to_the_publisher():
    ledger = apply(empty(), ok_snapshot(utc(2026, 9, 8, 10), []))
    assert ledger.last_run_at is None
    assert ledger.merge_policy == POLICY


# ----------------------------------------------------------- permutations


def permutation_snapshots():
    return [
        ok_snapshot(
            utc(2026, 9, 1, 10),
            [day(dt.date(2026, 9, 1), 100)],
        ),
        ok_snapshot(
            utc(2026, 9, 2, 10),
            [day(dt.date(2026, 9, 1), 150), day(dt.date(2026, 9, 2), 20)],
        ),
        ok_snapshot(
            utc(2026, 9, 3, 10),
            [day(dt.date(2026, 9, 2), 30), day(dt.date(2026, 9, 3), 5)],
        ),
        ok_snapshot(
            utc(2026, 9, 10, 10),
            [day(dt.date(2026, 9, 3), 7), day(dt.date(2026, 9, 9), 1)],
        ),
        # A second look at 2026-09-03 from after its freeze boundary:
        # whichever of the two late snapshots is applied first, the
        # earlier one has to win.
        ok_snapshot(
            utc(2026, 9, 11, 10),
            [day(dt.date(2026, 9, 3), 7), day(dt.date(2026, 9, 9), 1)],
        ),
    ]


def fold(snapshots):
    ledger = empty()
    for snap in snapshots:
        ledger = apply(ledger, snap)
    return ledger


def comparable(ledger):
    return ledger.model_copy(
        update={'applied_through': None, 'last_agents_hash': None}
    ).to_canonical_json()


def test_every_permutation_produces_the_same_ledger():
    snapshots = permutation_snapshots()
    newest = max(snapshots, key=lambda snap: snap.collected_at)
    expected = comparable(fold(snapshots))
    seen = 0
    for order in itertools.permutations(snapshots):
        ledger = fold(order)
        assert comparable(ledger) == expected
        assert ledger.applied_through == snapshot_remote_path(newest)
        assert ledger.last_agents_hash == newest.agents_hash
        seen += 1
    assert seen == 120


def test_the_permutation_result_is_the_expected_one():
    ledger = fold(permutation_snapshots())
    assert {
        date: record.input_tokens for date, record in records(ledger).items()
    } == {
        dt.date(2026, 9, 1): 150,
        dt.date(2026, 9, 2): 30,
        dt.date(2026, 9, 3): 7,
        dt.date(2026, 9, 9): 1,
    }
    assert ledger.anomalies == []
    # Only 2026-09-03 was last observed after its freeze boundary; the
    # other days were last seen by a snapshot inside the window.
    assert [record.settled for record in ledger.agents['claude'].days] == [
        False,
        False,
        True,
        False,
    ]
    # 2026-09-03 keeps the earlier of the two post-boundary looks.
    assert records(ledger)[dt.date(2026, 9, 3)].source_collected_at == utc(
        2026, 9, 10, 10
    )


# ------------------------------------------------- post-boundary order


def late_pair():
    """Two observations of one day, both past its freeze boundary."""
    date = dt.date(2026, 9, 1)
    early = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 500, output=40)])
    late = ok_snapshot(utc(2026, 9, 9, 10), [day(date, 400, output=40)])
    return date, early, late


def test_the_earliest_observation_after_the_boundary_wins():
    # Past the boundary the collector only loses history to retention,
    # so the first look at a settled day is the complete one.
    date, early, late = late_pair()
    ledger = apply(apply(empty(), late), early)
    record = records(ledger)[date]
    assert record.input_tokens == 500
    assert record.settled is True
    assert record.source_collected_at == utc(2026, 9, 8, 10)
    assert len(ledger.anomalies) == 1
    anomaly = ledger.anomalies[0]
    assert anomaly.kind == 'decrease_refused'
    assert anomaly.kept == 500
    assert anomaly.observed == 400
    assert anomaly.snapshot == snapshot_remote_path(late)


def test_two_post_boundary_observations_are_order_independent():
    _date, early, late = late_pair()
    forwards = apply(apply(empty(), early), late)
    backwards = apply(apply(empty(), late), early)
    assert comparable(backwards) == comparable(forwards)


def test_a_pre_boundary_snapshot_never_reopens_a_settled_record():
    # The rule is limited to observations made after the boundary; an
    # older, partial look at the day must stay ignored.
    date = dt.date(2026, 9, 1)
    partial = ok_snapshot(utc(2026, 9, 1, 8), [day(date, 100)])
    complete = ok_snapshot(utc(2026, 9, 8, 10), [day(date, 500)])
    ledger = apply(apply(empty(), complete), partial)
    record = records(ledger)[date]
    assert record.input_tokens == 500
    assert record.source_collected_at == utc(2026, 9, 8, 10)
    assert ledger.anomalies == []


# ------------------------------------------------------ anomaly volume


def test_an_hourly_repeat_of_one_disagreement_stays_one_anomaly():
    # Once retention prunes a settled day, every following snapshot
    # reports the same shrinkage. The ledger is rewritten hourly, so
    # appending would grow it without bound.
    date = dt.date(2026, 9, 1)
    ledger = apply(
        empty(),
        ok_snapshot(utc(2026, 9, 8, 0), [day(date, 500, output=40)]),
    )
    after_two = None
    last = None
    for hour in range(1, 25):
        moment = utc(2026, 9, 8, 0) + dt.timedelta(hours=hour)
        last = ok_snapshot(moment, [day(date, 400, output=30)])
        ledger = apply(ledger, last)
        if hour == 2:
            after_two = ledger.to_canonical_json()

    assert [anomaly.field for anomaly in ledger.anomalies] == [
        'input_tokens',
        'output_tokens',
    ]
    assert {anomaly.kind for anomaly in ledger.anomalies} == {
        'decrease_refused'
    }
    # The value that was kept is the original one; the snapshot named is
    # the most recent one that disagreed with it.
    assert [anomaly.kept for anomaly in ledger.anomalies] == [500, 40]
    assert {anomaly.snapshot for anomaly in ledger.anomalies} == {
        snapshot_remote_path(last)
    }
    assert after_two is not None
    assert len(ledger.to_canonical_json()) == len(after_two)


def test_the_anomaly_list_is_capped_and_drops_the_oldest_days():
    dates = [dt.date(2026, 1, 1) + dt.timedelta(days=n) for n in range(600)]
    first = ok_snapshot(
        utc(2027, 6, 1, 10), [day(date, 500) for date in dates]
    )
    second = ok_snapshot(
        utc(2027, 6, 2, 10), [day(date, 400) for date in dates]
    )
    ledger = apply(apply(empty(), first), second)
    anomalies = ledger.anomalies
    assert len(anomalies) == MAX_ANOMALIES
    assert [anomaly.date for anomaly in anomalies] == sorted(
        anomaly.date for anomaly in anomalies
    )
    assert anomalies[0].date == dates[600 - MAX_ANOMALIES]
    assert anomalies[-1].date == dates[-1]


# --------------------------------------------------------------- rebuild


def paths_and_snapshots(snapshots):
    return [(snapshot_remote_path(snap), snap) for snap in snapshots]


def test_rebuild_matches_the_incremental_ledger():
    snapshots = permutation_snapshots()
    incremental = fold(snapshots)
    rebuilt = rebuild_ledger(
        MACHINE_ID, LABEL, POLICY, paths_and_snapshots(snapshots)
    )
    assert rebuilt.to_canonical_json() == incremental.to_canonical_json()


def test_rebuild_is_independent_of_the_input_order():
    snapshots = permutation_snapshots()
    expected = rebuild_ledger(
        MACHINE_ID, LABEL, POLICY, paths_and_snapshots(snapshots)
    ).to_canonical_json()
    for order in itertools.permutations(snapshots):
        rebuilt = rebuild_ledger(
            MACHINE_ID, LABEL, POLICY, paths_and_snapshots(order)
        )
        assert rebuilt.to_canonical_json() == expected


def test_rebuild_of_nothing_is_an_empty_ledger():
    rebuilt = rebuild_ledger(MACHINE_ID, LABEL, POLICY, [])
    assert rebuilt.to_canonical_json() == empty().to_canonical_json()
    assert rebuilt.agents == {}
    assert rebuilt.applied_through is None


def test_new_ledger_copies_the_policy():
    given = policy(3, 'Asia/Tokyo')
    ledger = new_ledger(MACHINE_ID, LABEL, given)
    assert ledger.merge_policy == given
    assert ledger.merge_policy is not given
    assert ledger.machine_id == MACHINE_ID
    assert ledger.label == LABEL
