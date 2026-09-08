"""不依賴 Tk 的介面資料整理函式。

Tk 主執行緒只負責顯示結果；搜尋、摘要與幾何計算可獨立測試。
"""

from __future__ import annotations

from typing import Iterable

from monitor_core.catalog import StatusLibraryRecord, filter_status_records


def compact_status_summary(names: Iterable[str], total: int, limit: int = 3) -> str:
    labels = [str(name).strip() for name in names if str(name).strip()][: max(1, limit)]
    if total <= 0:
        return "尚未選擇監控狀態"
    if total > len(labels):
        labels.append(f"其他 +{total - len(labels)}")
    return f"已選 {total} 項｜" + "、".join(labels)


def shorten_text(text: str, max_characters: int) -> str:
    max_characters = max(4, int(max_characters))
    if len(text) <= max_characters:
        return text
    return text[: max_characters - 1].rstrip() + "…"


def remaining_seconds(milliseconds: int) -> int:
    """所有正剩餘時間向上取整；不把不足一秒顯示成已到期。"""
    return (max(0, int(milliseconds)) + 999) // 1000


def format_overlay_duration(milliseconds: int | None) -> str:
    """把卡片倒數顯示成秒、分秒或時分秒。"""

    if milliseconds is None:
        return "生效中"
    total_seconds = remaining_seconds(milliseconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}時{minutes:02d}分{seconds:02d}秒"
    if minutes:
        return f"{minutes}分{seconds:02d}秒"
    return f"{seconds}秒"


def calculate_auto_height_geometry(
    anchor_y: int,
    natural_height: int,
    work_top: int,
    work_bottom: int,
    *,
    minimum_height: int,
    top_margin: int,
    bottom_margin: int,
) -> tuple[int, int, int]:
    """先向下、再向上擴展；上下皆滿後回傳需要捲動的高度。"""

    max_height = max(
        minimum_height,
        work_bottom - work_top - top_margin - bottom_margin,
    )
    desired_height = max(minimum_height, int(natural_height))
    height = min(desired_height, max_height)
    safe_top = work_top + top_margin
    safe_bottom = work_bottom - bottom_margin
    anchor_y = max(safe_top, min(int(anchor_y), safe_bottom - minimum_height))
    y = anchor_y if anchor_y + height <= safe_bottom else max(safe_top, safe_bottom - height)
    return y, height, max(0, desired_height - height)


def overlay_natural_height(
    group_row_counts: Iterable[Iterable[int]],
    *,
    pet_visible: bool,
    minimum_height: int,
) -> int:
    height = 37
    people_count = 0
    for people in group_row_counts:
        for row_count in people:
            people_count += 1
            height += 26 + max(0, int(row_count)) * 21
    if pet_visible:
        height += 96
    if people_count == 0 and not pet_visible:
        height += 40
    return max(minimum_height, height + 7)
