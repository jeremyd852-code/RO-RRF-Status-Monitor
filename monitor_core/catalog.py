"""台版狀態資料、搜尋索引與分頁工具。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


TWRO_GAME_ID = "twro"
CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StatusLibraryRecord:
    status_id: int
    source_header: int
    name: str
    category: str
    jobs: tuple[str, ...]
    effect_group: str
    consumable_subcategory: str
    skills: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    functional_category: str = ""
    source_tags: tuple[str, ...] = ()
    search_text: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # 搜尋文字只在資料快照建立時整理一次，不在每次輸入時重新拼接。
        normalized = (
            f"{self.name} {self.status_id} 0x{self.status_id:04X} "
            f"{self.category} {' '.join(self.jobs)} {' '.join(self.skills)} "
            f"{self.consumable_subcategory} {' '.join(self.aliases)} "
            f"{self.functional_category} {' '.join(self.source_tags)}"
        ).casefold()
        object.__setattr__(self, "search_text", normalized)


@dataclass(frozen=True)
class StatusPage:
    ids: tuple[int, ...]
    page_index: int
    page_count: int
    total_count: int


def paginate_status_ids(
    status_ids: Iterable[int],
    page_index: int,
    page_size: int,
) -> StatusPage:
    ordered = tuple(status_ids)
    page_size = max(1, int(page_size))
    total_count = len(ordered)
    page_count = max(1, (total_count + page_size - 1) // page_size)
    safe_index = max(0, min(int(page_index), page_count - 1))
    start = safe_index * page_size
    return StatusPage(
        ids=ordered[start : start + page_size],
        page_index=safe_index,
        page_count=page_count,
        total_count=total_count,
    )


class StatusSearchIndex:
    """不可變狀態索引；分類切換只組合集合，不重新解析資料檔。"""

    def __init__(self, records: Iterable[StatusLibraryRecord] = ()) -> None:
        record_map = {record.status_id: record for record in records}
        self.records: Mapping[int, StatusLibraryRecord] = record_map
        self.all_ids = frozenset(record_map)
        by_category: dict[str, set[int]] = defaultdict(set)
        by_job: dict[str, set[int]] = defaultdict(set)
        by_effect: dict[str, set[int]] = defaultdict(set)
        by_consumable: dict[str, set[int]] = defaultdict(set)
        by_functional: dict[str, set[int]] = defaultdict(set)
        for record in record_map.values():
            by_category[record.category].add(record.status_id)
            by_effect[record.effect_group].add(record.status_id)
            if record.consumable_subcategory:
                by_consumable[record.consumable_subcategory].add(record.status_id)
            if record.functional_category:
                by_functional[record.functional_category].add(record.status_id)
            for job in record.jobs:
                by_job[job].add(record.status_id)
        self.by_category = {key: frozenset(value) for key, value in by_category.items()}
        self.by_job = {key: frozenset(value) for key, value in by_job.items()}
        self.by_effect = {key: frozenset(value) for key, value in by_effect.items()}
        self.by_consumable = {
            key: frozenset(value) for key, value in by_consumable.items()
        }
        self.by_functional = {
            key: frozenset(value) for key, value in by_functional.items()
        }

    def filter(
        self,
        *,
        query: str,
        mode: str,
        category: str,
        job: str,
        effect: str,
        consumable_subcategory: str,
        selected_ids: set[int],
        functional_category: str = "全部用途",
    ) -> tuple[int, ...]:
        normalized_query = query.strip().casefold()
        candidates = set(self.all_ids)
        if mode == "職業技能":
            if job == "全部職業":
                candidates = {
                    status_id
                    for status_id in candidates
                    if self.records[status_id].jobs
                }
            else:
                candidates.intersection_update(self.by_job.get(job, ()))
            expected_group = {
                "BUFF": "增益",
                "DEBUFF": "減益",
                "開關／特殊": "開關／特殊",
                "未分類": "未分類",
            }.get(effect)
            if expected_group is not None:
                candidates.intersection_update(self.by_effect.get(expected_group, ()))
        elif mode == "已勾選":
            candidates.intersection_update(selected_ids)
        elif mode == "常用狀態":
            candidates.intersection_update(selected_ids)
        elif not normalized_query:
            if category == "經驗／掉寶":
                candidates.intersection_update(
                    set(self.by_category.get("經驗", ()))
                    | set(self.by_category.get("掉寶", ()))
                )
            elif category == "消耗品":
                consumable_ids: set[int] = set()
                for indexed_ids in self.by_consumable.values():
                    consumable_ids.update(indexed_ids)
                candidates.intersection_update(consumable_ids)
            elif category != "全部分類":
                candidates.intersection_update(self.by_category.get(category, ()))
            if category == "消耗品" and consumable_subcategory != "全部消耗品":
                candidates.intersection_update(
                    self.by_consumable.get(consumable_subcategory, ())
                )
        if normalized_query:
            candidates = {
                status_id
                for status_id in candidates
                if normalized_query in self.records[status_id].search_text
            }
        if functional_category != "全部用途":
            candidates.intersection_update(
                self.by_functional.get(functional_category, ())
            )
        return tuple(
            sorted(
                candidates,
                key=lambda status_id: (
                    self.records[status_id].category,
                    self.records[status_id].name.casefold(),
                    status_id,
                ),
            )
        )


def filter_status_records(
    records: Iterable[StatusLibraryRecord],
    *,
    query: str,
    mode: str,
    category: str,
    job: str,
    effect: str,
    consumable_subcategory: str,
    selected_ids: set[int],
    functional_category: str = "全部用途",
) -> tuple[int, ...]:
    """相容入口；單次使用可直接建立輕量索引。"""

    return StatusSearchIndex(records).filter(
        query=query,
        mode=mode,
        category=category,
        job=job,
        effect=effect,
        consumable_subcategory=consumable_subcategory,
        selected_ids=selected_ids,
        functional_category=functional_category,
    )


def detect_ro_client(ro_dir: Path) -> str:
    """辨識台版 RO 主程式；樂園資料絕不視為可校對來源。"""

    try:
        if not ro_dir.is_dir():
            return "missing"
        names = {path.name.casefold() for path in ro_dir.iterdir()}
    except OSError:
        return "missing"
    if "roztw.ini" in names or "ragnarokzero.exe" in names:
        return "rozero"
    if "ro1tw.ini" in names and "data.grf" in names:
        return TWRO_GAME_ID
    return "unknown"


def twro_install_message(ro_dir: Path) -> tuple[bool, str]:
    kind = detect_ro_client(ro_dir)
    if kind == TWRO_GAME_ID:
        return True, "已確認台版 RO"
    if kind == "rozero":
        return False, "這是 RO 樂園資料夾，不能用於台版資料校對"
    if kind == "missing":
        return False, "找不到資料夾"
    return False, "找不到台版 RO 識別檔 RO1TW.ini"


def bundled_catalog_is_twro(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("game_id") == TWRO_GAME_ID
