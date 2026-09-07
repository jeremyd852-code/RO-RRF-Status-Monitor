"""台版 RRF 人物狀態旗標（0x0229）解析。

封包格式由台版實錄與 RO 封包結構交叉確認：
header(2) + AID(4) + bodyState(2) + healthState(2) + effectState(4) + PK(1)。

bodyState 與 healthState 對應的 EFST ID 直接採用台版主程式
EFSTIDs.lua 內的 BODYSTATE／HEALTHSTATE 常數。effectState 同時包含
騎乘、隱身、獵鷹等外觀／姿態旗標，不當成異常狀態，以免錯誤提示。
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


ACTOR_STATE_HEADER = 0x0229
ACTOR_STATE_PACKET_SIZE = 15

# bodyState 是單選值；順序與台版 EFST_BODYSTATE_* 常數一致。
BODY_STATE_STATUS_IDS: dict[int, int] = {
    1: 875,  # EFST_BODYSTATE_STONECURSE
    2: 876,  # EFST_BODYSTATE_FREEZING
    3: 877,  # EFST_BODYSTATE_STUN
    4: 878,  # EFST_BODYSTATE_SLEEP
    5: 879,  # EFST_BODYSTATE_UNDEAD
    6: 880,  # EFST_BODYSTATE_STONECURSE_ING
    7: 881,  # EFST_BODYSTATE_BURNNING（沿用客戶端拼字）
    8: 882,  # EFST_BODYSTATE_IMPRISON
}

# healthState 是可複選 bit mask；順序與台版 EFST_HEALTHSTATE_* 常數一致。
HEALTH_STATE_STATUS_IDS: dict[int, int] = {
    0x0001: 883,  # POISON
    0x0002: 884,  # CURSE
    0x0004: 885,  # SILENCE
    0x0008: 886,  # CONFUSION
    0x0010: 887,  # BLIND
    0x0020: 888,  # ANGELUS
    0x0040: 889,  # BLOODING（沿用客戶端拼字）
    0x0080: 890,  # HEAVYPOISON
    0x0100: 891,  # FEAR
}

ACTOR_STATE_STATUS_NAMES: dict[int, str] = {
    875: "石化",
    876: "冰凍",
    877: "暈眩",
    878: "睡眠",
    879: "不死狀態",
    880: "石化進行中",
    881: "著火",
    882: "隔離",
    883: "中毒",
    884: "詛咒",
    885: "沉默",
    886: "混亂",
    887: "黑暗",
    888: "天使之障壁",
    889: "出血",
    890: "致命中毒",
    891: "恐怖",
}


@dataclass(frozen=True)
class ActorStateSnapshot:
    target_id: int
    body_state: int
    health_state: int
    effect_state: int
    pk_mode: int
    status_ids: frozenset[int]
    unknown_body_state: int | None = None
    unknown_health_bits: int = 0


def decode_actor_state_packet(data: bytes) -> ActorStateSnapshot | None:
    """解碼一筆 0x0229；格式或 header 不符時回傳 ``None``。"""

    if len(data) < ACTOR_STATE_PACKET_SIZE:
        return None
    header, target_id, body_state, health_state, effect_state, pk_mode = struct.unpack_from(
        "<HIHHIB", data, 0
    )
    if header != ACTOR_STATE_HEADER or target_id <= 0:
        return None

    status_ids: set[int] = set()
    unknown_body_state: int | None = None
    if body_state:
        status_id = BODY_STATE_STATUS_IDS.get(body_state)
        if status_id is None:
            unknown_body_state = body_state
        else:
            status_ids.add(status_id)

    known_health_mask = 0
    for bit, status_id in HEALTH_STATE_STATUS_IDS.items():
        known_health_mask |= bit
        if health_state & bit:
            status_ids.add(status_id)

    return ActorStateSnapshot(
        target_id=target_id,
        body_state=body_state,
        health_state=health_state,
        effect_state=effect_state,
        pk_mode=pk_mode,
        status_ids=frozenset(status_ids),
        unknown_body_state=unknown_body_state,
        unknown_health_bits=health_state & ~known_health_mask,
    )
