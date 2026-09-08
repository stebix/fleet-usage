"""Tests for the snapshot agents hash."""

import datetime as dt
from decimal import Decimal

import pytest

from fleet_usage.models import AgentSnapshot, DayRecord, ModelRecord
from fleet_usage.snapshot import HASH_PREFIX, agents_hash, short_hash


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
        'codex': AgentSnapshot(status='error', message='boom'),
    }


def test_hash_shape():
    value = agents_hash(sample_agents())
    assert value.startswith(HASH_PREFIX)
    digest = value[len(HASH_PREFIX) :]
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


def test_hash_is_deterministic():
    assert agents_hash(sample_agents()) == agents_hash(sample_agents())


def test_hash_ignores_key_order():
    agents = sample_agents()
    reordered = {key: agents[key] for key in reversed(list(agents))}
    assert list(reordered) != list(agents)
    assert agents_hash(reordered) == agents_hash(agents)


def test_hash_changes_with_data():
    agents = sample_agents()
    mutated = sample_agents()
    mutated['claude'].days[0].input_tokens += 1
    assert agents_hash(mutated) != agents_hash(agents)


def test_empty_agents_hash_is_stable():
    assert agents_hash({}) == agents_hash({})


def test_short_hash():
    value = agents_hash(sample_agents())
    assert short_hash(value) == value[len(HASH_PREFIX) :][:8]
    assert len(short_hash(value)) == 8
    assert short_hash('abcdef0123456789') == 'abcdef01'


def test_short_hash_rejects_short_input():
    with pytest.raises(ValueError, match='too short'):
        short_hash('abc')
