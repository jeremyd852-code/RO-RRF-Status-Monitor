"""有上限、可跨次啟動保存的未知資料待辦。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from catalog.schema import (
    TWRO_GAME_ID,
    UNKNOWN_JOURNAL_SCHEMA,
    now_iso,
    read_json_object,
    write_json_atomically,
)


VALID_KINDS = frozenset({"status", "item", "pet", "pet_food"})
MAX_VALUE_BY_KIND = {
    "status": 0xFFFF,
    "item": 0xFFFFFFFF,
    "pet": 0xFFFFFFFF,
    "pet_food": 0xFFFFFFFF,
}


@dataclass(frozen=True)
class UnknownRecord:
    kind: str
    value_id: int
    first_seen_at: str
    last_seen_at: str
    seen_count: int = 1
    source_headers: tuple[int, ...] = ()
    relations: tuple[str, ...] = ()
    last_attempt_signature: str = ""
    attempt_count: int = 0

    @property
    def key(self) -> tuple[str, int]:
        return self.kind, self.value_id

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.value_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "seen_count": self.seen_count,
            "source_headers": list(self.source_headers),
            "relations": list(self.relations),
            "last_attempt_signature": self.last_attempt_signature,
            "attempt_count": self.attempt_count,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "UnknownRecord":
        if not isinstance(payload, dict):
            raise ValueError("未知資料紀錄不是物件")
        kind = str(payload.get("kind", "")).strip()
        value_id = int(payload.get("id", -1))
        minimum = 0 if kind == "status" else 1
        if kind not in VALID_KINDS or not minimum <= value_id <= MAX_VALUE_BY_KIND[kind]:
            raise ValueError("未知資料類型或 ID 無效")
        return cls(
            kind=kind,
            value_id=value_id,
            first_seen_at=str(payload.get("first_seen_at", "")) or now_iso(),
            last_seen_at=str(payload.get("last_seen_at", "")) or now_iso(),
            seen_count=max(1, int(payload.get("seen_count", 1))),
            source_headers=tuple(
                sorted(
                    {
                        int(value)
                        for value in payload.get("source_headers", [])
                        if isinstance(value, int) and 0 <= value <= 0xFFFF
                    }
                )
            ),
            relations=tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in payload.get("relations", [])
                        if str(value).strip()
                    }
                )
            ),
            last_attempt_signature=str(payload.get("last_attempt_signature", "")),
            attempt_count=max(0, int(payload.get("attempt_count", 0))),
        )


class UnknownJournal:
    """只保存最小證據；不保存 RRF 路徑、人物名稱或人物 ID。"""

    def __init__(
        self,
        path: Path,
        *,
        max_records: int = 2000,
        batch_limit: int = 100,
    ) -> None:
        self.path = Path(path)
        self.max_records = max(1, int(max_records))
        self.batch_limit = max(1, min(int(batch_limit), self.max_records))
        self._lock = threading.RLock()
        self._records: dict[tuple[str, int], UnknownRecord] = {}
        self._dirty = False
        self.dropped_count = 0
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = read_json_object(self.path)
            if (
                int(payload.get("schema_version", 0)) != UNKNOWN_JOURNAL_SCHEMA
                or payload.get("game_id") != TWRO_GAME_ID
            ):
                raise ValueError("未知資料待辦格式不支援")
            records: dict[tuple[str, int], UnknownRecord] = {}
            raw_records = payload.get("records", [])
            if not isinstance(raw_records, list):
                raise ValueError("未知資料待辦 records 不是清單")
            for raw_record in raw_records:
                try:
                    record = UnknownRecord.from_payload(raw_record)
                except (TypeError, ValueError):
                    continue
                previous = records.get(record.key)
                if previous is None or record.last_seen_at >= previous.last_seen_at:
                    records[record.key] = record
            self._records = records
            self.dropped_count = max(0, int(payload.get("dropped_count", 0)))
            self._trim()
            self._dirty = False
        except (OSError, TypeError, ValueError):
            self.load_error = "未知資料待辦無法讀取，已使用空白待辦"
            self._records = {}

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def snapshot(self) -> tuple[UnknownRecord, ...]:
        with self._lock:
            return tuple(
                replace(record)
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (item.last_seen_at, item.kind, item.value_id),
                    reverse=True,
                )
            )

    def record(
        self,
        kind: str,
        value_id: int,
        *,
        source_header: int | None = None,
        relation: str = "",
        observed_at: str | None = None,
    ) -> bool:
        normalized_kind = str(kind).strip()
        normalized_id = int(value_id)
        minimum = 0 if normalized_kind == "status" else 1
        if (
            normalized_kind not in VALID_KINDS
            or not minimum <= normalized_id <= MAX_VALUE_BY_KIND[normalized_kind]
        ):
            return False
        observed = observed_at or now_iso()
        key = (normalized_kind, normalized_id)
        with self._lock:
            previous = self._records.get(key)
            headers = set(previous.source_headers if previous else ())
            if source_header is not None and 0 <= int(source_header) <= 0xFFFF:
                headers.add(int(source_header))
            relations = set(previous.relations if previous else ())
            normalized_relation = str(relation).strip()
            if normalized_relation:
                relations.add(normalized_relation)
            self._records[key] = UnknownRecord(
                kind=normalized_kind,
                value_id=normalized_id,
                first_seen_at=previous.first_seen_at if previous else observed,
                last_seen_at=observed,
                seen_count=(previous.seen_count + 1) if previous else 1,
                source_headers=tuple(sorted(headers)),
                relations=tuple(sorted(relations)),
                last_attempt_signature=previous.last_attempt_signature if previous else "",
                attempt_count=previous.attempt_count if previous else 0,
            )
            self._dirty = True
            self._trim()
            return previous is None

    def next_batch(
        self,
        *,
        client_signature: str = "",
        retry_attempted: bool = False,
        limit: int | None = None,
    ) -> tuple[UnknownRecord, ...]:
        safe_limit = max(1, min(int(limit or self.batch_limit), self.batch_limit))
        with self._lock:
            candidates = [
                record
                for record in self._records.values()
                if retry_attempted
                or not client_signature
                or record.last_attempt_signature != client_signature
            ]
            candidates.sort(
                key=lambda item: (
                    item.attempt_count,
                    -item.seen_count,
                    item.first_seen_at,
                    item.kind,
                    item.value_id,
                )
            )
            return tuple(replace(record) for record in candidates[:safe_limit])

    def mark_attempted(
        self,
        keys: Iterable[tuple[str, int]],
        *,
        client_signature: str,
    ) -> None:
        signature = str(client_signature)
        with self._lock:
            for key in keys:
                record = self._records.get((str(key[0]), int(key[1])))
                if record is None:
                    continue
                self._records[record.key] = replace(
                    record,
                    last_attempt_signature=signature,
                    attempt_count=record.attempt_count + 1,
                )
                self._dirty = True

    def resolve(self, keys: Iterable[tuple[str, int]]) -> int:
        removed = 0
        with self._lock:
            for kind, value_id in keys:
                if self._records.pop((str(kind), int(value_id)), None) is not None:
                    removed += 1
            if removed:
                self._dirty = True
        return removed

    def flush(self) -> bool:
        with self._lock:
            if not self._dirty and self.path.is_file():
                return False
            payload: dict[str, object] = {
                "schema_version": UNKNOWN_JOURNAL_SCHEMA,
                "game_id": TWRO_GAME_ID,
                "updated_at": now_iso(),
                "max_records": self.max_records,
                "dropped_count": self.dropped_count,
                "records": [
                    record.to_payload()
                    for record in sorted(
                        self._records.values(),
                        key=lambda item: (item.kind, item.value_id),
                    )
                ],
            }
            write_json_atomically(self.path, payload)
            self._dirty = False
            return True

    def _trim(self) -> None:
        excess = len(self._records) - self.max_records
        if excess <= 0:
            return
        # 優先淘汰很久沒再出現、已多次失敗的資料；保留近期且常見項目。
        ordered = sorted(
            self._records.values(),
            key=lambda item: (
                item.last_seen_at,
                -item.attempt_count,
                item.seen_count,
                item.kind,
                item.value_id,
            ),
        )
        for record in ordered[:excess]:
            self._records.pop(record.key, None)
            self.dropped_count += 1
        self._dirty = True
