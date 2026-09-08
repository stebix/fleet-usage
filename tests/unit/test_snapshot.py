"""Tests for building, naming and placing snapshots."""

import datetime as dt
import re
import zoneinfo
from decimal import Decimal

import pytest

from fleet_usage.config import (
    CollectorSettings,
    ConfigError,
    GitHubSettings,
    MachineSettings,
    Settings,
)
from fleet_usage.models import AgentSnapshot, DayRecord, ModelRecord
from fleet_usage.snapshot import (
    HASH_PREFIX,
    agents_hash,
    build_snapshot,
    parse_snapshot_name,
    short_hash,
    snapshot_file_name,
    snapshot_remote_path,
)

MACHINE_ID = '3f7c2f66-3d0f-4a1e-9a5a-1f4e4f0a1d11'
BERLIN = zoneinfo.ZoneInfo('Europe/Berlin')
NAME_RE = re.compile(r'^\d{8}T\d{6}Z-[0-9a-f]{8}\.json$')


def make_settings(*, viewer_only=False, offline_pricing=True):
    collector = CollectorSettings(
        command=['bunx', 'ccusage@20.0.20'],
        version='20.0.20',
        agents=['claude', 'codex'],
        timezone='Europe/Berlin',
        offline_pricing=offline_pricing,
    )
    return Settings(
        machine=(
            None
            if viewer_only
            else MachineSettings(id=MACHINE_ID, label='fedora-mobile')
        ),
        github=GitHubSettings(repository='owner/fleet-usage-data'),
        collector=None if viewer_only else collector,
    )


def sample_agents():
    return {
        'claude': AgentSnapshot(
            status='ok',
            days=[
                DayRecord(
                    date=dt.date(2026, 9, 7),
                    input_tokens=1,
                    output_tokens=2,
                    cache_create_tokens=3,
                    cache_read_tokens=4,
                    cost_usd=Decimal('1.2345'),
                    cost_status='estimated',
                    models=[
                        ModelRecord(
                            model='claude-fable-5-1',
                            input_tokens=1,
                            output_tokens=2,
                            cost_usd=Decimal('1.2345'),
                            cost_status='estimated',
                        )
                    ],
                )
            ],
        ),
        'codex': AgentSnapshot(status='ok', days=[]),
    }


MOMENT = dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC)


def build(agents=None, collected_at=MOMENT, **kwargs):
    return build_snapshot(
        make_settings(**kwargs),
        sample_agents() if agents is None else agents,
        collector_version='20.0.20',
        collected_at=collected_at,
    )


# --------------------------------------------------------------- content


def test_build_snapshot_fills_identity_and_collector():
    snapshot = build()
    assert snapshot.schema_version == 1
    assert snapshot.machine_id == MACHINE_ID
    assert snapshot.label == 'fedora-mobile'
    assert snapshot.timezone == 'Europe/Berlin'
    assert snapshot.collector.name == 'ccusage'
    assert snapshot.collector.version == '20.0.20'
    assert snapshot.collector.mode == 'auto'
    assert snapshot.collector.pricing == 'offline'
    assert snapshot.collected_at == MOMENT


def test_build_snapshot_records_online_pricing():
    assert build(offline_pricing=False).collector.pricing == 'online'


def test_build_snapshot_sets_agents_hash():
    snapshot = build()
    assert snapshot.agents_hash == agents_hash(sample_agents())
    assert snapshot.agents_hash.startswith(HASH_PREFIX)


def test_build_snapshot_keeps_agents():
    snapshot = build()
    assert list(snapshot.agents) == ['claude', 'codex']
    assert snapshot.agents['claude'].days[0].cost_usd == Decimal('1.2345')


def test_build_snapshot_defaults_to_now():
    before = dt.datetime.now(dt.UTC).replace(microsecond=0)
    snapshot = build(collected_at=None)
    after = dt.datetime.now(dt.UTC)
    assert before <= snapshot.collected_at <= after
    assert snapshot.collected_at.microsecond == 0


def test_build_snapshot_drops_sub_second_precision():
    moment = dt.datetime(2026, 9, 8, 13, 0, 4, 987654, tzinfo=dt.UTC)
    snapshot = build(collected_at=moment)
    assert snapshot.collected_at == MOMENT
    assert snapshot_file_name(snapshot).startswith('20260908T130004Z-')


def test_build_snapshot_rejects_naive_timestamps():
    with pytest.raises(ValueError, match='timezone aware'):
        build(collected_at=dt.datetime(2026, 9, 8, 13, 0, 4))


def test_build_snapshot_requires_machine_and_collector():
    with pytest.raises(ConfigError, match=r'\[machine\]'):
        build(viewer_only=True)


def test_build_snapshot_does_not_alias_the_input_mapping():
    agents = sample_agents()
    snapshot = build(agents)
    agents['gemini'] = AgentSnapshot(status='ok', days=[])
    assert 'gemini' not in snapshot.agents


# ------------------------------------------------------------------ hash


def test_agents_hash_is_independent_of_key_order():
    agents = sample_agents()
    reversed_agents = {key: agents[key] for key in reversed(list(agents))}
    assert list(reversed_agents) != list(agents)
    first = build(agents)
    second = build(reversed_agents)
    assert first.agents_hash == second.agents_hash
    assert snapshot_remote_path(first) == snapshot_remote_path(second)


def test_hash_changes_when_data_changes():
    agents = sample_agents()
    other = sample_agents()
    other['claude'].days[0].input_tokens += 1
    assert build(agents).agents_hash != build(other).agents_hash


def test_file_name_uses_the_short_hash():
    snapshot = build()
    expected = short_hash(snapshot.agents_hash)
    assert snapshot_file_name(snapshot) == f'20260908T130004Z-{expected}.json'


# ------------------------------------------------------------------ path


def test_remote_path_format():
    snapshot = build()
    path = snapshot_remote_path(snapshot)
    prefix = f'snapshots/{MACHINE_ID}/2026-09/'
    assert path.startswith(prefix)
    name = path[len(prefix) :]
    assert NAME_RE.match(name)
    assert name == snapshot_file_name(snapshot)


def test_remote_path_is_utc_not_local():
    # 00:30 Berlin on the first of January is still December in UTC, so
    # both the month directory and the name must shift back.
    local = dt.datetime(2026, 1, 1, 0, 30, 0, tzinfo=BERLIN)
    snapshot = build(collected_at=local)
    path = snapshot_remote_path(snapshot)
    assert '/2025-12/' in path
    assert snapshot_file_name(snapshot).startswith('20251231T233000Z-')


def test_remote_paths_sort_chronologically():
    moments = [
        dt.datetime(2026, 9, 8, 13, 0, 4, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 8, 14, 0, 4, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 1, 0, 0, 0, tzinfo=dt.UTC),
    ]
    paths = [snapshot_remote_path(build(collected_at=m)) for m in moments]
    assert paths == sorted(paths)


# ------------------------------------------------------------ name parse


def test_parse_snapshot_name_round_trip():
    snapshot = build()
    moment, digest = parse_snapshot_name(snapshot_file_name(snapshot))
    assert moment == snapshot.collected_at
    assert digest == short_hash(snapshot.agents_hash)


def test_parse_snapshot_name_accepts_a_full_path():
    snapshot = build()
    moment, digest = parse_snapshot_name(snapshot_remote_path(snapshot))
    assert moment == MOMENT
    assert moment.tzinfo is not None
    assert digest == short_hash(snapshot.agents_hash)


@pytest.mark.parametrize(
    'name',
    [
        '',
        'snapshot.json',
        '20260908T130004Z.json',
        '20260908T130004Z-abcd.json',
        '20260908T130004Z-ABCDEF12.json',
        '20260908T130004-abcdef12.json',
        '20260908T130004Z-abcdef12.txt',
        '20260908T130004Z-abcdef12.json.tmp',
    ],
)
def test_parse_snapshot_name_rejects_other_names(name):
    with pytest.raises(ValueError, match='not a snapshot file name'):
        parse_snapshot_name(name)
