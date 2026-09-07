"""Validated recording-start data for the recording owner's own pet only."""
from __future__ import annotations

from dataclasses import dataclass
import struct


@dataclass(frozen=True)
class ReplayPetSnapshot:
    owner_id: int
    pet_gid: int
    pet_id: int
    pet_name: str
    level: int
    satiety: int
    intimacy: int

    def is_valid(self) -> bool:
        ranges = ((self.owner_id, 1, 0xFFFFFFFE), (self.pet_gid, 1, 0xFFFFFFFE),
                  (self.pet_id, 1, 0xFFFF), (self.level, 1, 0xFFFF),
                  (self.satiety, 0, 100), (self.intimacy, 0, 1000))
        return (all(isinstance(value, int) and not isinstance(value, bool)
                    and lower <= value <= upper for value, lower, upper in ranges)
                and isinstance(self.pet_name, str) and bool(self.pet_name.strip()))


def decode_pet_snapshot(chunks: list[tuple[int, bytes]], owner_id: int | None) -> ReplayPetSnapshot | None:
    """Decode container 9's 53xx fields; never infer a pet from dummy 100s.

    All five recorded numeric fields are four-byte little-endian values. A missing,
    duplicate, truncated or sentinel field rejects the entire snapshot. Container
    9 belongs to the recording owner; visible-entity data is not an ownership source.
    """
    required = {5301, 5303, 5305, 5306, 5307, 5308}
    fields: dict[int, bytes] = {}
    for chunk_id, payload in chunks:
        if chunk_id in required:
            if chunk_id in fields or len(payload) != (32 if chunk_id == 5303 else 4):
                return None
            fields[chunk_id] = payload
    if fields.keys() != required:
        return None
    try:
        name = fields[5303].split(b"\0", 1)[0].decode("cp950").strip()
    except UnicodeDecodeError:
        return None
    values = {key: struct.unpack("<I", fields[key])[0] for key in required if key != 5303}
    result = ReplayPetSnapshot(owner_id, values[5301], values[5305], name,
                               values[5306], values[5307], values[5308])
    return result if result.is_valid() else None
