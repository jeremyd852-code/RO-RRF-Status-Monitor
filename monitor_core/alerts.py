"""狀態燈號與到期提醒規則。

只有已確認增益用綠／黃／紅表示剩餘時間。效果性質獨立於技能、物品
等來源分類；資料有效性又獨立於效果性質。
"""

from __future__ import annotations


EXPIRATION_LEVELS = frozenset({"yellow", "red"})


def effect_nature(group: str) -> str:
    if group == "增益":
        return "buff"
    if group == "減益":
        return "debuff"
    if group in {"開關／特殊", "特殊", "混合", "複合", "特殊／混合", "特殊／複合"}:
        return "special"
    return "unknown"


def effect_label(group: str) -> str:
    return {"buff": "增益", "debuff": "異常", "special": "特殊／複合", "unknown": "待確認"}[effect_nature(group)]


def visual_colors(level: str) -> tuple[str, str, str]:
    """點、淡底、深色文字；主表與浮動卡共用。"""
    return {
        "normal": ("#246B43", "#FFFFFF", "#263846"),
        "yellow": ("#765B00", "#FFF4B8", "#765B00"),
        "red": ("#972F32", "#FFE0E0", "#972F32"),
        "debuff": ("#623C83", "#F0E3FF", "#623C83"),
        "special": ("#326580", "#EAF2F8", "#326580"),
        "neutral": ("#59636B", "#EDF0F2", "#59636B"),
        "stale": ("#59636B", "#EDF0F2", "#59636B"),
    }.get(level, ("#59636B", "#EDF0F2", "#59636B"))


def expiration_level(remaining_ms: int, yellow_ms: int, red_ms: int) -> str:
    if remaining_ms <= red_ms:
        return "red"
    if remaining_ms <= yellow_ms:
        return "yellow"
    return "normal"


def status_visual_level(
    *,
    group: str,
    category: str,
    remaining_ms: int | None,
    yellow_ms: int,
    red_ms: int,
) -> str:
    """決定卡片與主表燈號；不改變立即提示的獨立規則。"""

    del category
    nature = effect_nature(group)
    if nature == "debuff":
        return "debuff"
    if nature == "special":
        return "special"
    if nature != "buff":
        return "neutral"
    if remaining_ms is None:
        return "normal"
    return expiration_level(remaining_ms, yellow_ms, red_ms)


def expiration_phase(level: str) -> str:
    return level if level in EXPIRATION_LEVELS else ""


def level_label(level: str) -> str:
    return {
        "red": "即將結束",
        "yellow": "即將到期",
        "debuff": "異常",
        "special": "特殊／複合",
        "neutral": "待確認",
        "stale": "上次資料",
    }.get(level, "")
