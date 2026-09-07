"""可獨立測試的關閉決策。"""

from __future__ import annotations

from enum import Enum


class CloseAction(str, Enum):
    CLOSE_NOW = "close_now"
    CHOOSE_UNKNOWN = "choose_unknown"
    WAIT_FOR_ACTIVE_WORK = "wait_for_active_work"
    SHOW_EXISTING_PROGRESS = "show_existing_progress"


def decide_close_action(
    *,
    close_finalized: bool,
    closing_after_work: bool,
    catalog_work_active: bool,
    unknown_count: int,
) -> CloseAction:
    if close_finalized:
        return CloseAction.CLOSE_NOW
    if closing_after_work:
        return CloseAction.SHOW_EXISTING_PROGRESS
    if catalog_work_active:
        return CloseAction.WAIT_FOR_ACTIVE_WORK
    if max(0, int(unknown_count)):
        return CloseAction.CHOOSE_UNKNOWN
    return CloseAction.CLOSE_NOW
