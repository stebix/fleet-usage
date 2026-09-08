"""Pydantic models for every JSON document exchanged by the fleet.

Three document families are described here.

* Snapshots (:class:`Snapshot`) are immutable, one per collection run.
* Ledgers (:class:`Ledger`) are derived per machine and overwritten.
* The fleet manifest (:class:`FleetManifest`) lives in the data
  repository root and lists the participating machines.

All models serialise deterministically: timestamps become ISO-8601 UTC
strings with a ``Z`` suffix, monetary amounts become decimal strings or
``null`` and token counts are non-negative integers. Use
:func:`canonical_json` whenever a byte-stable representation is needed,
for example for hashing.
"""

import datetime as dt
import json
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

__all__ = [
    'AgentSnapshot',
    'Anomaly',
    'CollectorInfo',
    'CostStatus',
    'DayRecord',
    'FleetManifest',
    'Ledger',
    'LedgerAgent',
    'LedgerDayRecord',
    'MachineEntry',
    'MergePolicy',
    'ModelRecord',
    'Snapshot',
    'canonical_json',
    'canonical_json_bytes',
    'to_canonical_obj',
]

CostStatus = Literal['estimated', 'unknown']
AgentStatus = Literal['ok', 'error']
AnomalyKind = Literal['decrease_applied', 'decrease_refused']

NonNegativeInt = Annotated[int, Field(ge=0)]

SCHEMA_VERSION = 1
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


def _to_utc(value: dt.datetime) -> dt.datetime:
    """Return ``value`` as a timezone aware UTC timestamp.

    Parameters
    ----------
    value : datetime.datetime
        Timestamp to normalise. Naive values are rejected because an
        implicit timezone would silently corrupt the merge order.

    Returns
    -------
    datetime.datetime
        The same instant expressed in UTC.

    Raises
    ------
    ValueError
        If ``value`` carries no timezone information.
    """
    if value.tzinfo is None:
        msg = 'timestamps must be timezone aware'
        raise ValueError(msg)
    return value.astimezone(dt.UTC)


def format_timestamp(value: dt.datetime) -> str:
    """Render ``value`` as an ISO-8601 UTC string with a ``Z`` suffix.

    Parameters
    ----------
    value : datetime.datetime
        Timezone aware timestamp.

    Returns
    -------
    str
        For example ``'2026-09-08T13:00:04Z'``. Sub-second precision is
        deliberately dropped: snapshots are taken hourly at best and a
        stable, human readable representation is worth more than
        microseconds.
    """
    return _to_utc(value).strftime(TIMESTAMP_FORMAT)


def to_canonical_obj(value: Any) -> Any:
    """Convert ``value`` into JSON-ready primitives.

    Parameters
    ----------
    value : Any
        A model, a mapping of models, a sequence of models or any object
        that :mod:`json` already understands.

    Returns
    -------
    Any
        Nested lists, dicts and scalars only.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(k): to_canonical_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_canonical_obj(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to canonical JSON.

    Canonical means sorted keys, no insignificant whitespace and ASCII
    escaping, so that equal documents always produce equal strings.

    Parameters
    ----------
    value : Any
        Model, mapping, sequence or scalar.

    Returns
    -------
    str
        The canonical JSON representation.
    """
    return json.dumps(
        to_canonical_obj(value),
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return :func:`canonical_json` encoded as UTF-8.

    Parameters
    ----------
    value : Any
        Model, mapping, sequence or scalar.

    Returns
    -------
    bytes
        UTF-8 encoded canonical JSON.
    """
    return canonical_json(value).encode('utf-8')


class FleetModel(BaseModel):
    """Base class adding canonical dumping to every document model."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    def to_canonical_json(self) -> str:
        """Return the canonical JSON representation of this model.

        Returns
        -------
        str
            Sorted keys, compact separators, ASCII only.
        """
        return canonical_json(self)

    def to_pretty_json(self) -> str:
        """Return an indented JSON representation for files and logs.

        Returns
        -------
        str
            Two space indentation with sorted keys and a trailing
            newline, which keeps diffs in the data repository readable.
        """
        payload = json.dumps(
            self.model_dump(mode='json'),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        return payload + '\n'


class _TokenCounts(FleetModel):
    """Token counters and cost shared by day and model records."""

    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cache_create_tokens: NonNegativeInt = 0
    cache_read_tokens: NonNegativeInt = 0
    cost_usd: Decimal | None = None
    cost_status: CostStatus = 'unknown'

    @field_serializer('cost_usd')
    def _serialize_cost(self, value: Decimal | None) -> str | None:
        """Serialise the cost as a decimal string.

        Parameters
        ----------
        value : decimal.Decimal or None
            The amount in US dollars.

        Returns
        -------
        str or None
            The exact decimal text, never a float.
        """
        if value is None:
            return None
        return str(value)

    @property
    def total_tokens(self) -> int:
        """Sum of all four token counters.

        Returns
        -------
        int
            Total number of tokens.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_create_tokens
            + self.cache_read_tokens
        )


class ModelRecord(_TokenCounts):
    """Usage of a single model on a single day."""

    model: str


class DayRecord(_TokenCounts):
    """Usage of one agent on one calendar day in the reporting zone."""

    date: dt.date
    models: list[ModelRecord] = Field(default_factory=list)

    @field_validator('models')
    @classmethod
    def _sort_models(cls, value: list[ModelRecord]) -> list[ModelRecord]:
        """Keep model records sorted by model name.

        Parameters
        ----------
        value : list of ModelRecord
            Records in arbitrary order.

        Returns
        -------
        list of ModelRecord
            The same records ordered by ``model``.
        """
        return sorted(value, key=lambda record: record.model)


class LedgerDayRecord(DayRecord):
    """A day record enriched with merge bookkeeping.

    ``source_snapshot`` names the snapshot the numbers came from. It is
    optional because ledgers written before it existed do not carry it;
    the merge falls back to the path of the snapshot being applied.
    """

    source_collected_at: dt.datetime
    source_snapshot: str | None = None
    settled: bool = False

    @field_validator('source_collected_at')
    @classmethod
    def _normalize(cls, value: dt.datetime) -> dt.datetime:
        """Normalise the source timestamp to UTC.

        Parameters
        ----------
        value : datetime.datetime
            Timezone aware timestamp.

        Returns
        -------
        datetime.datetime
            The same instant in UTC.
        """
        return _to_utc(value)

    @field_serializer('source_collected_at')
    def _serialize_source(self, value: dt.datetime) -> str:
        """Serialise the source timestamp.

        Parameters
        ----------
        value : datetime.datetime
            Timezone aware timestamp.

        Returns
        -------
        str
            ISO-8601 UTC with a ``Z`` suffix.
        """
        return format_timestamp(value)


class CollectorInfo(FleetModel):
    """Provenance of the data inside a snapshot."""

    name: str = 'ccusage'
    version: str
    mode: str = 'auto'
    pricing: str = 'offline'


class AgentSnapshot(FleetModel):
    """Per agent payload of a snapshot.

    A failed agent carries ``status='error'`` plus a ``message`` and no
    days; a successful agent carries ``status='ok'`` and possibly an
    empty ``days`` list when the agent was simply not used.
    """

    status: AgentStatus
    days: list[DayRecord] = Field(default_factory=list)
    message: str | None = None

    @field_validator('days')
    @classmethod
    def _sort_days(cls, value: list[DayRecord]) -> list[DayRecord]:
        """Keep day records sorted by date.

        Parameters
        ----------
        value : list of DayRecord
            Records in arbitrary order.

        Returns
        -------
        list of DayRecord
            The same records ordered by ``date``.
        """
        return sorted(value, key=lambda record: record.date)

    @model_validator(mode='after')
    def _check_consistency(self) -> Self:
        """Reject payloads that mix an error status with data.

        Returns
        -------
        AgentSnapshot
            The validated model.

        Raises
        ------
        ValueError
            If an errored agent also reports days.
        """
        if self.status == 'error' and self.days:
            msg = "agents with status 'error' must not carry days"
            raise ValueError(msg)
        return self


class Snapshot(FleetModel):
    """An immutable record of one collection run on one machine."""

    schema_version: int = SCHEMA_VERSION
    machine_id: str
    label: str
    collected_at: dt.datetime
    timezone: str
    collector: CollectorInfo
    agents_hash: str
    agents: dict[str, AgentSnapshot] = Field(default_factory=dict)

    @field_validator('collected_at')
    @classmethod
    def _normalize(cls, value: dt.datetime) -> dt.datetime:
        """Normalise the collection timestamp to UTC.

        Parameters
        ----------
        value : datetime.datetime
            Timezone aware timestamp.

        Returns
        -------
        datetime.datetime
            The same instant in UTC.
        """
        return _to_utc(value)

    @field_validator('agents')
    @classmethod
    def _sort_agents(
        cls, value: dict[str, AgentSnapshot]
    ) -> dict[str, AgentSnapshot]:
        """Keep agents ordered by name.

        Parameters
        ----------
        value : dict of str to AgentSnapshot
            Agents in arbitrary order.

        Returns
        -------
        dict of str to AgentSnapshot
            The same mapping with sorted keys.
        """
        return {key: value[key] for key in sorted(value)}

    @field_serializer('collected_at')
    def _serialize_collected_at(self, value: dt.datetime) -> str:
        """Serialise the collection timestamp.

        Parameters
        ----------
        value : datetime.datetime
            Timezone aware timestamp.

        Returns
        -------
        str
            ISO-8601 UTC with a ``Z`` suffix.
        """
        return format_timestamp(value)


class MergePolicy(FleetModel):
    """Parameters that decide when a ledger record becomes settled."""

    version: int = 1
    freeze_window_days: NonNegativeInt = 5
    timezone: str


class Anomaly(FleetModel):
    """A refused or applied decrease of a token counter."""

    agent: str
    date: dt.date
    field: str
    kept: int
    observed: int
    snapshot: str
    kind: AnomalyKind


class LedgerAgent(FleetModel):
    """Per agent section of a machine ledger."""

    last_success_at: dt.datetime | None = None
    last_error: str | None = None
    days: list[LedgerDayRecord] = Field(default_factory=list)

    @field_validator('last_success_at')
    @classmethod
    def _normalize(cls, value: dt.datetime | None) -> dt.datetime | None:
        """Normalise the success timestamp to UTC.

        Parameters
        ----------
        value : datetime.datetime or None
            Timezone aware timestamp, if any.

        Returns
        -------
        datetime.datetime or None
            The same instant in UTC.
        """
        return None if value is None else _to_utc(value)

    @field_validator('days')
    @classmethod
    def _sort_days(cls, value: list[LedgerDayRecord]) -> list[LedgerDayRecord]:
        """Keep day records sorted by date.

        Parameters
        ----------
        value : list of LedgerDayRecord
            Records in arbitrary order.

        Returns
        -------
        list of LedgerDayRecord
            The same records ordered by ``date``.
        """
        return sorted(value, key=lambda record: record.date)

    @field_serializer('last_success_at')
    def _serialize_success(self, value: dt.datetime | None) -> str | None:
        """Serialise the success timestamp.

        Parameters
        ----------
        value : datetime.datetime or None
            Timezone aware timestamp, if any.

        Returns
        -------
        str or None
            ISO-8601 UTC with a ``Z`` suffix.
        """
        return None if value is None else format_timestamp(value)


class Ledger(FleetModel):
    """Derived, mutable state of a single machine."""

    schema_version: int = SCHEMA_VERSION
    machine_id: str
    label: str
    merge_policy: MergePolicy
    last_run_at: dt.datetime | None = None
    applied_through: str | None = None
    last_agents_hash: str | None = None
    agents: dict[str, LedgerAgent] = Field(default_factory=dict)
    anomalies: list[Anomaly] = Field(default_factory=list)

    @field_validator('last_run_at')
    @classmethod
    def _normalize(cls, value: dt.datetime | None) -> dt.datetime | None:
        """Normalise the run timestamp to UTC.

        Parameters
        ----------
        value : datetime.datetime or None
            Timezone aware timestamp, if any.

        Returns
        -------
        datetime.datetime or None
            The same instant in UTC.
        """
        return None if value is None else _to_utc(value)

    @field_validator('agents')
    @classmethod
    def _sort_agents(
        cls, value: dict[str, LedgerAgent]
    ) -> dict[str, LedgerAgent]:
        """Keep agents ordered by name.

        Parameters
        ----------
        value : dict of str to LedgerAgent
            Agents in arbitrary order.

        Returns
        -------
        dict of str to LedgerAgent
            The same mapping with sorted keys.
        """
        return {key: value[key] for key in sorted(value)}

    @field_serializer('last_run_at')
    def _serialize_last_run(self, value: dt.datetime | None) -> str | None:
        """Serialise the run timestamp.

        Parameters
        ----------
        value : datetime.datetime or None
            Timezone aware timestamp, if any.

        Returns
        -------
        str or None
            ISO-8601 UTC with a ``Z`` suffix.
        """
        return None if value is None else format_timestamp(value)


class MachineEntry(FleetModel):
    """One machine listed in the fleet manifest."""

    id: str
    label: str


class FleetManifest(FleetModel):
    """Contents of ``fleet.toml`` in the data repository root."""

    schema_version: int = SCHEMA_VERSION
    timezone: str
    freeze_window_days: NonNegativeInt = 5
    machines: list[MachineEntry] = Field(default_factory=list)
