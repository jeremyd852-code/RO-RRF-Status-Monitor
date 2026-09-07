"""不可變執行資料快照與版本檢查。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from catalog.schema import CatalogManifest, TWRO_GAME_ID, read_json_object


def _freeze_int_text(values: Mapping[int, str]) -> Mapping[int, str]:
    return MappingProxyType(
        {
            int(key): str(value)
            for key, value in values.items()
            if str(value).strip()
        }
    )


def _technical_name(value: str) -> bool:
    text = str(value).strip().upper()
    return not text or text.startswith(("EFST_", "STATE_", "STATUS_", "SC_"))


@dataclass(frozen=True)
class RuntimeCatalogSnapshot:
    generation: int
    status_names: Mapping[int, str]
    status_groups: Mapping[int, str]
    item_names: Mapping[int, str]
    pet_names: Mapping[int, str]
    pet_food_item_ids: frozenset[int]
    source: str
    manifest_valid: bool = True
    manifest_errors: tuple[str, ...] = ()

    def readable_status_name(self, status_id: int) -> str | None:
        value = self.status_names.get(int(status_id))
        return None if value is None or _technical_name(value) else value


class CatalogRepository:
    def __init__(self, snapshot: RuntimeCatalogSnapshot) -> None:
        self._lock = threading.RLock()
        self._snapshot = snapshot

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "CatalogRepository":
        return cls(load_bundled_snapshot(data_dir))

    def snapshot(self) -> RuntimeCatalogSnapshot:
        with self._lock:
            current = self._snapshot
            return RuntimeCatalogSnapshot(
                generation=current.generation,
                status_names=current.status_names,
                status_groups=current.status_groups,
                item_names=current.item_names,
                pet_names=current.pet_names,
                pet_food_item_ids=current.pet_food_item_ids,
                source=current.source,
                manifest_valid=current.manifest_valid,
                manifest_errors=current.manifest_errors,
            )

    def replace(
        self,
        *,
        status_names: Mapping[int, str],
        status_groups: Mapping[int, str],
        item_names: Mapping[int, str],
        pet_names: Mapping[int, str],
        pet_food_item_ids: Iterable[int],
        source: str,
    ) -> RuntimeCatalogSnapshot:
        with self._lock:
            current = self._snapshot
            self._snapshot = RuntimeCatalogSnapshot(
                generation=current.generation + 1,
                status_names=_freeze_int_text(status_names),
                status_groups=_freeze_int_text(status_groups),
                item_names=_freeze_int_text(item_names),
                pet_names=_freeze_int_text(pet_names),
                pet_food_item_ids=frozenset(int(value) for value in pet_food_item_ids),
                source=str(source),
                manifest_valid=current.manifest_valid,
                manifest_errors=current.manifest_errors,
            )
            return self._snapshot

    def merge(
        self,
        *,
        status_names: Mapping[int, str] | None = None,
        status_groups: Mapping[int, str] | None = None,
        item_names: Mapping[int, str] | None = None,
        pet_names: Mapping[int, str] | None = None,
        pet_food_item_ids: Iterable[int] = (),
        source: str = "",
    ) -> RuntimeCatalogSnapshot:
        with self._lock:
            current = self._snapshot
            merged_status_names = dict(current.status_names)
            merged_status_groups = dict(current.status_groups)
            merged_item_names = dict(current.item_names)
            merged_pet_names = dict(current.pet_names)
            merged_food_ids = set(current.pet_food_item_ids)
            merged_status_names.update(status_names or {})
            merged_status_groups.update(status_groups or {})
            merged_item_names.update(item_names or {})
            merged_pet_names.update(pet_names or {})
            merged_food_ids.update(int(value) for value in pet_food_item_ids)
            return self.replace(
                status_names=merged_status_names,
                status_groups=merged_status_groups,
                item_names=merged_item_names,
                pet_names=merged_pet_names,
                pet_food_item_ids=merged_food_ids,
                source=source or current.source,
            )


def load_bundled_snapshot(data_dir: Path) -> RuntimeCatalogSnapshot:
    root = Path(data_dir)
    status_names: dict[int, str] = {}
    status_groups: dict[int, str] = {}
    item_names: dict[int, str] = {}
    pet_names: dict[int, str] = {}
    food_ids: set[int] = set()

    status_payload = read_json_object(root / "status_catalog.json")
    if status_payload.get("game_id") != TWRO_GAME_ID:
        raise ValueError("status_catalog 不是台版 RO")
    records = status_payload.get("records", {})
    if isinstance(records, dict):
        for raw_id, raw_record in records.items():
            if not isinstance(raw_record, dict):
                continue
            status_id = int(raw_id)
            name = str(raw_record.get("name", "")).strip()
            if name:
                status_names[status_id] = name
            group = str(raw_record.get("effect_group", "")).strip()
            if group:
                status_groups[status_id] = group

    client_payload = read_json_object(root / "client_catalog.json")
    if client_payload.get("game_id") != TWRO_GAME_ID:
        raise ValueError("client_catalog 不是台版 RO")
    for raw_id, raw_value in dict(client_payload.get("items", {})).items():
        value = raw_value.get("name", "") if isinstance(raw_value, dict) else raw_value
        if str(value).strip():
            item_names[int(raw_id)] = str(value).strip()
    for raw_id, raw_value in dict(client_payload.get("pets", {})).items():
        value = raw_value.get("name", "") if isinstance(raw_value, dict) else raw_value
        if str(value).strip():
            pet_names[int(raw_id)] = str(value).strip()
    food_ids.update(
        int(value)
        for value in client_payload.get("pet_food_item_ids", [])
        if isinstance(value, int) and 0 < value <= 0xFFFFFFFF
    )

    extended_path = root / "可擴充資料庫.json"
    if extended_path.is_file():
        try:
            extended = read_json_object(extended_path)
            if extended.get("game_id") == TWRO_GAME_ID:
                extended_records = extended.get("records", {})
                if isinstance(extended_records, dict):
                    for kind, target in (
                        ("statuses", status_names),
                        ("items", item_names),
                        ("pets", pet_names),
                    ):
                        raw_records = extended_records.get(kind, {})
                        if not isinstance(raw_records, dict):
                            continue
                        for raw_id, raw_record in raw_records.items():
                            if (
                                isinstance(raw_record, dict)
                                and raw_record.get("review_status") == "confirmed"
                                and str(raw_record.get("name", "")).strip()
                            ):
                                target[int(raw_id)] = str(raw_record["name"]).strip()
                    raw_food = extended_records.get("pet_food_item_ids", {})
                    if isinstance(raw_food, dict):
                        food_ids.update(
                            int(raw_id)
                            for raw_id, raw_record in raw_food.items()
                            if isinstance(raw_record, dict)
                            and raw_record.get("review_status") == "confirmed"
                        )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    manifest_valid = True
    manifest_errors: tuple[str, ...] = ()
    manifest_path = root / "catalog_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = CatalogManifest.from_payload(read_json_object(manifest_path))
            manifest_valid, manifest_errors = manifest.validate_files(root)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            manifest_valid = False
            manifest_errors = (str(exc),)

    return RuntimeCatalogSnapshot(
        generation=0,
        status_names=_freeze_int_text(status_names),
        status_groups=_freeze_int_text(status_groups),
        item_names=_freeze_int_text(item_names),
        pet_names=_freeze_int_text(pet_names),
        pet_food_item_ids=frozenset(food_ids),
        source="1.6.5 內建台版資料",
        manifest_valid=manifest_valid,
        manifest_errors=manifest_errors,
    )
