"""Round trip and serialisation tests for the document models."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fleet_usage.models import (
    AgentSnapshot,
    Anomaly,
    CollectorInfo,
    DayRecord,
    FleetManifest,
    Ledger,
    LedgerAgent,
    LedgerDayRecord,
    MachineEntry,
    MergePolicy,
    ModelRecord,
    Snapshot,
    canonical_json,
)

MACHINE_ID = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'


def agents_from_fixture(path, limit=3):
    """Build the snapshot agents mapping from recorded ccusage output.

    The real collector arrives in a later phase; this helper performs the
    same mapping so that the model tests run against real numbers.
    """
    payload = json.loads(path.read_text(encoding='utf-8'))
    per_agent: dict[str, list[DayRecord]] = {}
    for row in payload['daily'][:limit]:
        for agent in row['agents']:
            models = [
                ModelRecord(
                    model=breakdown['modelName'],
                    input_tokens=breakdown['inputTokens'],
                    output_tokens=breakdown['outputTokens'],
                    cache_create_tokens=breakdown['cacheCreationTokens'],
                    cache_read_tokens=breakdown['cacheReadTokens'],
                    cost_usd=(
                        Decimal(str(breakdown['cost']))
                        if breakdown['cost']
                        else None
                    ),
                    cost_status=(
                        'estimated' if breakdown['cost'] else 'unknown'
                    ),
                )
                for breakdown in agent['modelBreakdowns']
            ]
            day = DayRecord(
                date=dt.date.fromisoformat(row['period']),
                input_tokens=agent['inputTokens'],
                output_tokens=agent['outputTokens'],
                cache_create_tokens=agent['cacheCreationTokens'],
                cache_read_tokens=agent['cacheReadTokens'],
                cost_usd=(
                    Decimal(str(agent['totalCost']))
                    if agent['totalCost']
                    else None
                ),
                cost_status=('estimated' if agent['totalCost'] else 'unknown'),
                models=models,
            )
            per_agent.setdefault(agent['agent'], []).append(day)
    return {
        name: AgentSnapshot(status='ok', days=days)
        for name, days in per_agent.items()
    }


@pytest.fixture
def fixture_agents(ccusage_fixture_dir):
    return agents_from_fixture(ccusage_fixture_dir / 'daily-by-agent.json')


def make_snapshot(agents):
    return Snapshot(
        machine_id=MACHINE_ID,
        label='fedora-mobile',
        collected_at=dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC),
        timezone='Europe/Berlin',
        collector=CollectorInfo(version='20.0.20'),
        agents_hash='sha256:' + '0' * 64,
        agents=agents,
    )


def test_snapshot_round_trip_from_fixture(fixture_agents):
    snapshot = make_snapshot(fixture_agents)
    payload = snapshot.model_dump(mode='json')
    restored = Snapshot.model_validate(payload)
    assert restored == snapshot
    assert restored.model_dump(mode='json') == payload


def test_snapshot_json_shape(fixture_agents):
    snapshot = make_snapshot(fixture_agents)
    payload = json.loads(snapshot.to_canonical_json())
    assert payload['schema_version'] == 1
    assert payload['collected_at'] == '2026-09-08T13:00:04Z'
    assert set(payload) == {
        'schema_version',
        'machine_id',
        'label',
        'collected_at',
        'timezone',
        'collector',
        'agents_hash',
        'agents',
    }
    day = payload['agents']['codex']['days'][0]
    assert set(day) == {
        'date',
        'input_tokens',
        'output_tokens',
        'cache_create_tokens',
        'cache_read_tokens',
        'cost_usd',
        'cost_status',
        'models',
    }
    assert isinstance(day['cost_usd'], str)
    assert day['cost_status'] == 'estimated'
    assert isinstance(day['models'][0]['cost_usd'], str)


def test_cost_is_exact_decimal_string():
    record = ModelRecord(model='m', cost_usd=Decimal('0.145127'))
    assert record.model_dump(mode='json')['cost_usd'] == '0.145127'
    restored = ModelRecord.model_validate(record.model_dump(mode='json'))
    assert restored.cost_usd == Decimal('0.145127')


def test_unknown_cost_serialises_to_null():
    record = DayRecord(date=dt.date(2026, 9, 7), cost_status='unknown')
    assert record.model_dump(mode='json')['cost_usd'] is None


def test_timestamps_are_normalised_to_utc_with_z():
    berlin = dt.timezone(dt.timedelta(hours=2))
    snapshot = make_snapshot({})
    shifted = snapshot.model_copy(
        update={
            'collected_at': dt.datetime(2026, 9, 8, 15, 0, 4, tzinfo=berlin)
        }
    )
    revalidated = Snapshot.model_validate(shifted.model_dump(mode='json'))
    assert revalidated.collected_at.tzinfo == dt.UTC
    dumped = revalidated.model_dump(mode='json')['collected_at']
    assert dumped == '2026-09-08T13:00:04Z'


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        Snapshot(
            machine_id=MACHINE_ID,
            label='x',
            collected_at=dt.datetime(2026, 9, 8, 13, 0, 4),
            timezone='Europe/Berlin',
            collector=CollectorInfo(version='20.0.20'),
            agents_hash='sha256:' + '0' * 64,
        )


def test_token_counts_must_be_non_negative():
    with pytest.raises(ValidationError):
        DayRecord(date=dt.date(2026, 9, 7), input_tokens=-1)


def test_days_and_models_are_sorted():
    agent = AgentSnapshot(
        status='ok',
        days=[
            DayRecord(date=dt.date(2026, 9, 8)),
            DayRecord(
                date=dt.date(2026, 9, 7),
                models=[
                    ModelRecord(model='zeta'),
                    ModelRecord(model='alpha'),
                ],
            ),
        ],
    )
    assert [day.date.day for day in agent.days] == [7, 8]
    assert [m.model for m in agent.days[0].models] == ['alpha', 'zeta']


def test_agents_are_sorted_by_name():
    snapshot = make_snapshot(
        {
            'codex': AgentSnapshot(status='ok'),
            'claude': AgentSnapshot(status='ok'),
        }
    )
    assert list(snapshot.agents) == ['claude', 'codex']


def test_errored_agent_carries_message_and_no_days():
    agent = AgentSnapshot(status='error', message='boom')
    assert agent.model_dump(mode='json') == {
        'status': 'error',
        'days': [],
        'message': 'boom',
    }
    with pytest.raises(ValidationError):
        AgentSnapshot(
            status='error', days=[DayRecord(date=dt.date(2026, 9, 7))]
        )


def make_ledger():
    return Ledger(
        machine_id=MACHINE_ID,
        label='fedora-mobile',
        merge_policy=MergePolicy(
            freeze_window_days=5, timezone='Europe/Berlin'
        ),
        last_run_at=dt.datetime(2026, 9, 8, 13, 0, 5, tzinfo=dt.UTC),
        applied_through=(
            f'snapshots/{MACHINE_ID}/2026-09/20260908T130004Z-deadbeef.json'
        ),
        last_agents_hash='sha256:' + 'a' * 64,
        agents={
            'claude': LedgerAgent(
                last_success_at=dt.datetime(
                    2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC
                ),
                days=[
                    LedgerDayRecord(
                        date=dt.date(2026, 9, 7),
                        input_tokens=1,
                        output_tokens=2,
                        cache_create_tokens=3,
                        cache_read_tokens=4,
                        cost_usd=Decimal('1.2345'),
                        cost_status='estimated',
                        source_collected_at=dt.datetime(
                            2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC
                        ),
                        settled=False,
                        models=[
                            ModelRecord(
                                model='claude-fable-5-1',
                                input_tokens=1,
                                output_tokens=2,
                                cache_create_tokens=3,
                                cache_read_tokens=4,
                                cost_usd=Decimal('1.2345'),
                                cost_status='estimated',
                            )
                        ],
                    )
                ],
            )
        },
        anomalies=[
            Anomaly(
                agent='claude',
                date=dt.date(2026, 9, 7),
                field='input_tokens',
                kept=10,
                observed=8,
                snapshot='snapshots/x/2026-09/a.json',
                kind='decrease_refused',
            )
        ],
    )


def test_ledger_round_trip():
    ledger = make_ledger()
    payload = ledger.model_dump(mode='json')
    assert Ledger.model_validate(payload) == ledger
    day = payload['agents']['claude']['days'][0]
    assert day['source_collected_at'] == '2026-09-08T13:00:04Z'
    assert day['settled'] is False
    assert day['cost_usd'] == '1.2345'
    assert payload['last_run_at'] == '2026-09-08T13:00:05Z'
    assert payload['anomalies'][0]['kind'] == 'decrease_refused'


def test_ledger_defaults_are_null():
    ledger = Ledger(
        machine_id=MACHINE_ID,
        label='x',
        merge_policy=MergePolicy(timezone='Europe/Berlin'),
    )
    payload = ledger.model_dump(mode='json')
    assert payload['last_run_at'] is None
    assert payload['applied_through'] is None
    assert payload['last_agents_hash'] is None
    assert payload['agents'] == {}
    assert payload['anomalies'] == []


def test_fleet_manifest_round_trip():
    manifest = FleetManifest(
        timezone='Europe/Berlin',
        freeze_window_days=5,
        machines=[MachineEntry(id=MACHINE_ID, label='fedora-mobile')],
    )
    payload = manifest.model_dump(mode='json')
    assert FleetManifest.model_validate(payload) == manifest
    assert payload['machines'][0]['label'] == 'fedora-mobile'


def test_canonical_json_is_sorted_and_compact():
    text = canonical_json({'b': 1, 'a': {'d': 2, 'c': 3}})
    assert text == '{"a":{"c":3,"d":2},"b":1}'


def test_pretty_json_ends_with_newline():
    ledger = make_ledger()
    assert ledger.to_pretty_json().endswith('}\n')
