"""不可變的監控畫面快照。"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Iterable

from monitor_core.policies import MODE_AUTO, MODE_CUSTOM, PolicyResolver


@dataclass(frozen=True)
class StatusObservation:
    target_id: int
    status_id: int
    active: bool
    remaining_ms: int | None
    total_ms: int | None
    event_timeline_ms: int
    source_header: int

    @property
    def key(self) -> tuple[int, int]:
        return self.status_id, self.target_id


@dataclass(frozen=True)
class DisplayStatus:
    observation: StatusObservation
    relation: str
    target_name: str
    policy_mode: str
    policy_source: str


@dataclass(frozen=True)
class MonitorSnapshot:
    version: int
    created_monotonic: float
    statuses: tuple[DisplayStatus, ...]
    parsed_status_count: int
    observation_count: int
    policy_retained_count: int
    excluded_internal_count: int
    excluded_unreadable_count: int = 0

    @property
    def keys(self) -> frozenset[tuple[int, int]]:
        return frozenset(item.observation.key for item in self.statuses)


def build_monitor_snapshot(
    observations: Iterable[StatusObservation],
    *,
    resolver: PolicyResolver,
    relation_for: Callable[[int], str],
    name_for: Callable[[int], str | None],
    version: int,
    internal_status_ids: frozenset[int] = frozenset(),
    readable_status_ids: frozenset[int] | None = None,
) -> MonitorSnapshot:
    source = tuple(observations)
    displayed: list[DisplayStatus] = []
    retained = 0
    excluded_internal = 0
    excluded_unreadable = 0
    for observation in source:
        relation = relation_for(observation.target_id)
        target_name = name_for(observation.target_id) or ""
        policy = resolver.resolve(
            target_id=observation.target_id,
            relation=relation,
            target_name=target_name,
        )
        if policy.mode not in {MODE_AUTO, MODE_CUSTOM}:
            continue
        retained += 1
        if observation.status_id in internal_status_ids:
            excluded_internal += 1
            continue
        if readable_status_ids is not None and observation.status_id not in readable_status_ids:
            excluded_unreadable += 1
            continue
        if not policy.displays(observation.status_id):
            continue
        displayed.append(
            DisplayStatus(
                observation=observation,
                relation=relation,
                target_name=target_name,
                policy_mode=policy.mode,
                policy_source=policy.source,
            )
        )
    displayed.sort(
        key=lambda item: (
            item.relation,
            item.observation.target_id,
            item.observation.status_id,
        )
    )
    return MonitorSnapshot(
        version=max(0, int(version)),
        created_monotonic=monotonic(),
        statuses=tuple(displayed),
        parsed_status_count=len(source),
        observation_count=len(source),
        policy_retained_count=retained,
        excluded_internal_count=excluded_internal,
        excluded_unreadable_count=excluded_unreadable,
    )
