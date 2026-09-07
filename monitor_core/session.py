"""人物狀態觀察庫與工作階段。

未知身分的狀態依狀態生命週期保存，不用固定 60 秒刪除仍有效的 BUFF。
"""
from __future__ import annotations
from collections import OrderedDict
from dataclasses import replace
from threading import Event, Lock
from typing import Callable, Iterable
from monitor_core.policies import PolicyResolver, scope_label
from monitor_core.snapshots import MonitorSnapshot, StatusObservation, build_monitor_snapshot

class ObservationStore:

    def __init__(self, *, max_observations: int=4096, max_quarantine: int=512) -> None:
        self.max_observations = max(1, int(max_observations))
        self.max_quarantine = max(1, int(max_quarantine))
        self._active: OrderedDict[tuple[int, int], StatusObservation] = OrderedDict()
        self._quarantine: OrderedDict[tuple[int, int], StatusObservation] = OrderedDict()
        self._allowed_target_ids: frozenset[int] = frozenset()

    def set_allowed_target_ids(self, target_ids: Iterable[int]) -> None:
        """Receive the session's trusted identity set, never user policy rules."""
        self._allowed_target_ids = frozenset((int(value) for value in target_ids if int(value) > 0))
        self._quarantine.clear()
        for key in list(self._active):
            if key[1] not in self._allowed_target_ids:
                self._active.pop(key, None)

    def _target_allowed(self, target_id: int) -> bool:
        return target_id in self._allowed_target_ids

    def clear(self) -> None:
        self._active.clear()
        self._quarantine.clear()

    def replace(self, observations: Iterable[StatusObservation], *, identity_confirmed: Callable[[int], bool] | None=None) -> None:
        """同步解析器的有上限快照，包含供主表檢視的近期結束狀態。"""
        active: OrderedDict[tuple[int, int], StatusObservation] = OrderedDict()
        quarantine: OrderedDict[tuple[int, int], StatusObservation] = OrderedDict()
        for observation in observations:
            if not self._target_allowed(observation.target_id):
                continue
            confirmed = identity_confirmed is None or identity_confirmed(observation.target_id)
            if not confirmed:
                continue
            target = active if confirmed else quarantine
            target[observation.key] = observation
        self._active = active
        self._quarantine = quarantine
        self._trim(self._active, self.max_observations)
        self._trim(self._quarantine, self.max_quarantine)

    def observe(self, observation: StatusObservation, *, identity_confirmed: bool) -> None:
        if not self._target_allowed(observation.target_id) or not identity_confirmed:
            self._active.pop(observation.key, None)
            self._quarantine.pop(observation.key, None)
            return
        target = self._active if identity_confirmed else self._quarantine
        other = self._quarantine if identity_confirmed else self._active
        if not observation.active or (observation.remaining_ms is not None and observation.remaining_ms <= 0):
            target.pop(observation.key, None)
            other.pop(observation.key, None)
            return
        other.pop(observation.key, None)
        target.pop(observation.key, None)
        target[observation.key] = observation
        self._trim(target, self.max_observations if identity_confirmed else self.max_quarantine)

    def promote_target(self, target_id: int) -> int:
        return 0

    def remove_target(self, target_id: int) -> None:
        for mapping in (self._active, self._quarantine):
            for key in [key for key in mapping if key[1] == int(target_id)]:
                mapping.pop(key, None)

    def observations(self, *, include_quarantine: bool=True) -> tuple[StatusObservation, ...]:
        values = [value for value in self._active.values() if self._target_allowed(value.target_id)]
        if include_quarantine and False:
            values.extend(self._quarantine.values())
        return tuple((replace(value) for value in values))

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def quarantine_count(self) -> int:
        return len(self._quarantine)

    @staticmethod
    def _trim(mapping: OrderedDict, limit: int) -> None:
        while len(mapping) > limit:
            mapping.popitem(last=False)

class MonitorSession:

    def __init__(self, resolver: PolicyResolver | None=None) -> None:
        self._lock = Lock()
        self.resolver = resolver or PolicyResolver()
        self.store = ObservationStore()
        self.version = 0
        self.active_event = Event()
        self.reset_event = Event()
        self.wake_event = Event()
        self._alive = True
        self._trusted_self_id: int | None = None
        self._trusted_relations: dict[int, str] = {}

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._alive

    @property
    def active(self) -> bool:
        return self.alive and self.active_event.is_set()

    def start_monitoring(self) -> bool:
        """啟動工作階段並喚醒唯一監控執行緒。"""
        with self._lock:
            if not self._alive or self.active_event.is_set():
                return False
            self.active_event.set()
            self.reset_event.set()
            self.wake_event.set()
            return True

    def stop_monitoring(self) -> bool:
        """停止讀取並要求下一次啟動重建解析狀態。"""
        with self._lock:
            was_active = self.active_event.is_set()
            self.active_event.clear()
            self.reset_event.set()
            self.wake_event.set()
            return was_active

    def request_reset(self) -> None:
        self.reset_event.set()
        self.wake_event.set()

    def consume_reset_request(self) -> bool:
        if not self.reset_event.is_set():
            return False
        self.reset_event.clear()
        return True

    def shutdown(self) -> None:
        with self._lock:
            self._alive = False
            self.active_event.clear()
            self.reset_event.set()
            self.wake_event.set()

    def reset(self) -> None:
        with self._lock:
            self.store.clear()
            self._trusted_self_id = None
            self._trusted_relations.clear()
            self.store.set_allowed_target_ids(())
            self.version += 1

    def set_trusted_targets(self, *, self_id: int | None=None, party_ids: Iterable[int]=(), own_pet_ids: Iterable[int]=()) -> frozenset[int]:
        """Replace current replay identities; return IDs whose old state is revoked.

        Only the trusted replay identity/party/ownership tracker may supply this
        set. A name, screen appearance, or an imported setting is not evidence.
        The caller must remove the returned IDs from its alert/history queues;
        this session owns observations only. A self-ID change revokes all old
        identities even when the next character shares some of their party.
        """
        resolved_self = int(self_id) if self_id is not None and int(self_id) > 0 else None
        relations: dict[int, str] = {}
        if resolved_self is not None:
            relations = {int(value): 'party' for value in party_ids if int(value) > 0}
            relations.update({int(value): 'self' for value in own_pet_ids if int(value) > 0})
            relations[resolved_self] = 'self'
        with self._lock:
            if resolved_self == self._trusted_self_id and relations == self._trusted_relations:
                return frozenset()
            if resolved_self != self._trusted_self_id:
                revoked = frozenset(self._trusted_relations)
                self.store.clear()
            else:
                revoked = frozenset((target_id for target_id, relation in self._trusted_relations.items() if relations.get(target_id) != relation))
            self._trusted_self_id = resolved_self
            self._trusted_relations = relations
            self.store.set_allowed_target_ids(relations)
            for target_id in revoked:
                self.store.remove_target(target_id)
            self.version += 1
            return revoked

    def set_resolver(self, resolver: PolicyResolver) -> None:
        with self._lock:
            self.resolver = resolver
            self.version += 1

    def replace_observations(self, observations: Iterable[StatusObservation], *, identity_confirmed: Callable[[int], bool] | None=None) -> None:
        with self._lock:
            self.store.replace(observations, identity_confirmed=identity_confirmed)
            self.version += 1

    def snapshot(self, *, relation_for: Callable[[int], str], name_for: Callable[[int], str | None], internal_status_ids: frozenset[int]=frozenset(), readable_status_ids: frozenset[int] | None=None) -> MonitorSnapshot:
        with self._lock:
            observations = self.store.observations()
            resolver = self.resolver
            version = self.version
            trusted_relations = dict(self._trusted_relations)
        observations = tuple((value for value in observations if value.target_id in trusted_relations))
        relation_for = lambda target_id: scope_label(trusted_relations.get(target_id, 'other'))
        snapshot = build_monitor_snapshot(observations, resolver=resolver, relation_for=relation_for, name_for=name_for, version=version, internal_status_ids=internal_status_ids, readable_status_ids=readable_status_ids)
        with self._lock:
            if self.version != version:
                return build_monitor_snapshot((), resolver=self.resolver, relation_for=relation_for, name_for=name_for, version=self.version)
        return snapshot
