"""右側狀態卡片的響應式排列計算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


CARD_MARGIN = 8
CARD_GAP_X = 4
CARD_GAP_Y = 3
CARD_MIN_CONTENT_WIDTH = 240
CARD_CONTENT_TOP = 37
CARD_BOTTOM_MARGIN = 7


@dataclass(frozen=True)
class CardPlacement:
    index: int
    column: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class CardLayout:
    placements: tuple[CardPlacement, ...]
    columns: int
    natural_height: int


def responsive_column_count(width: int, *, max_columns: int = 3) -> int:
    inner_width = max(1, int(width) - CARD_MARGIN * 2)
    possible = (inner_width + CARD_GAP_X) // (
        CARD_MIN_CONTENT_WIDTH + CARD_GAP_X
    )
    return max(1, min(max_columns, possible))


def arrange_card_heights(width: int, heights: Iterable[int]) -> CardLayout:
    """依最短欄向下堆疊；視窗拉寬時自然變成左右排列。"""

    card_heights = tuple(max(1, int(height)) for height in heights)
    columns = responsive_column_count(width)
    inner_width = max(1, int(width) - CARD_MARGIN * 2 - CARD_GAP_X * (columns - 1))
    card_width = max(1, inner_width // columns)
    column_bottoms = [CARD_CONTENT_TOP] * columns
    placements: list[CardPlacement] = []
    for index, height in enumerate(card_heights):
        column = min(range(columns), key=lambda candidate: (column_bottoms[candidate], candidate))
        x = CARD_MARGIN + column * (card_width + CARD_GAP_X)
        y = column_bottoms[column]
        placements.append(CardPlacement(index, column, x, y, card_width, height))
        column_bottoms[column] = y + height + CARD_GAP_Y
    content_bottom = max(column_bottoms, default=CARD_CONTENT_TOP)
    if card_heights:
        content_bottom -= CARD_GAP_Y
    return CardLayout(
        placements=tuple(placements),
        columns=columns,
        natural_height=max(CARD_CONTENT_TOP + CARD_BOTTOM_MARGIN, content_bottom + CARD_BOTTOM_MARGIN),
    )

