"""狀態燈號與到期提醒規則。

一般增益用綠／黃／紅表示剩餘時間；異常狀態固定使用紫色，避免把
「中了異常」誤讀成「增益快結束」。未知分類則使用藍灰色。
"""

from __future__ import annotations


EXPIRATION_LEVELS = frozenset({"yellow", "red"})


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
    remaining_ms: int,
    yellow_ms: int,
    red_ms: int,
) -> str:
    """決定卡片與主表燈號；不改變立即提示的獨立規則。"""

    if group == "減益" or category == "DEBUFF":
        return "debuff"
    if group in {"未分類", "伺服器狀態"} and category not in {
        "BUFF",
        "技能",
        "消耗品",
        "經驗",
        "掉寶",
    }:
        return "neutral"
    return expiration_level(remaining_ms, yellow_ms, red_ms)


def expiration_phase(level: str) -> str:
    return level if level in EXPIRATION_LEVELS else ""


def level_label(level: str) -> str:
    return {
        "red": "紅燈",
        "yellow": "黃燈",
        "debuff": "異常",
        "neutral": "未分類",
    }.get(level, "")

