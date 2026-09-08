"""Apply explicit, code-checked review decisions after every catalog update.

This module does not infer an effect from a skill, an item, or a UI category.
Load once per catalog generation; applying reviews performs no filesystem I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping, MutableMapping


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EFFECT_GROUPS = frozenset({"增益", "減益", "開關／特殊", "未分類"})


@dataclass(frozen=True)
class ReviewConflict:
    status_id: int | None
    reason: str
    expected_code: str = ""
    actual_code: str = ""


def load_efst_codes(data_dir: Path | None = None) -> dict[int, str]:
    """Read the bundled ID-to-code identity once, never on a display tick."""
    text = ((data_dir or DEFAULT_DATA_DIR) / "EFSTIDs.lua").read_text(encoding="utf-8-sig")
    result: dict[int, str] = {}
    ambiguous: set[int] = set()
    for code, raw_id in re.findall(r"\b(EFST_[A-Za-z0-9_]+)\s*=\s*(\d+)", text):
        status_id = int(raw_id)
        if not 0 <= status_id <= 0xFFFF:
            continue
        if status_id in result and result[status_id] != code:
            ambiguous.add(status_id)
        result[status_id] = code
    for status_id in ambiguous:
        result.pop(status_id, None)
    return result


class EffectReviews:
    def __init__(self, records: Mapping, code_map: Mapping[int, str],
                 load_conflicts: tuple[ReviewConflict, ...] = ()) -> None:
        self.records = dict(records)
        self.code_map = dict(code_map)
        self.load_conflicts = load_conflicts

    def apply(
        self,
        names: MutableMapping[int, str],
        groups: MutableMapping[int, str],
        metadata: MutableMapping | None = None,
        efst_codes: Mapping[int, str] | None = None,
    ) -> tuple[ReviewConflict, ...]:
        """Mutate only reviewed fields; an invalid row never blocks other IDs.

        Metadata may use integer or JSON string IDs. If an incoming metadata row
        declares another EFST code, neither its name nor its effect is overwritten.
        Callers retain/report the returned conflicts instead of silently applying
        a decision to a different state. Unreviewed/new IDs are left untouched.
        """
        codes = self.code_map if efst_codes is None else efst_codes
        conflicts = list(self.load_conflicts)
        for raw_id, review in self.records.items():
            try:
                status_id = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                conflicts.append(ReviewConflict(None, "invalid_status_id"))
                continue
            if not 0 <= status_id <= 0xFFFF or not isinstance(review, dict):
                conflicts.append(ReviewConflict(status_id, "invalid_review_record"))
                continue
            expected = str(review.get("efst_code", ""))
            actual = str(codes.get(status_id, ""))
            if not expected.startswith("EFST_") or expected != actual:
                conflicts.append(ReviewConflict(status_id, "efst_code_mismatch", expected, actual))
                continue
            key = status_id if metadata is None or status_id in metadata else str(status_id)
            current = metadata.get(key) if metadata is not None else None
            if isinstance(current, Mapping) and current.get("efst_code") not in (None, "", expected):
                conflicts.append(ReviewConflict(status_id, "metadata_code_mismatch", expected,
                                                str(current["efst_code"])))
                continue
            group = review.get("effect_group")
            if group is not None and (not isinstance(group, str) or group not in EFFECT_GROUPS):
                conflicts.append(ReviewConflict(status_id, "invalid_effect_group", expected, actual))
                continue
            invalid_fields = [field for field in ("display_name_override", "functional_category")
                              if field in review and not isinstance(review[field], str)]
            if invalid_fields:
                conflicts.append(ReviewConflict(status_id, "invalid_review_field", expected, actual))
                continue
            name = review.get("display_name_override", "").strip()
            if name:
                names[status_id] = name
            if group is not None:
                groups[status_id] = group
            if isinstance(current, Mapping):
                updated = dict(current)
                for field in ("functional_category", "effect_group", "effect_basis", "effect_note",
                              "raw_effect_group", "raw_title_color", "purpose_basis", "review_status"):
                    if field in review:
                        updated[field] = review[field]
                if name:
                    updated["name"] = updated["display_name_override"] = name
                    old_aliases = current.get("aliases", [])
                    reviewed_aliases = review.get("aliases", [])
                    updated["aliases"] = sorted({str(value) for value in
                        [*(old_aliases if isinstance(old_aliases, (list, tuple)) else []),
                         *(reviewed_aliases if isinstance(reviewed_aliases, (list, tuple)) else [])]
                        if isinstance(value, str) and value and value != name})
                metadata[key] = updated
        return tuple(conflicts)


def load_effect_reviews(data_dir: Path | None = None) -> EffectReviews:
    root = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
    codes = load_efst_codes(root)
    payload = json.loads((root / "status_reviews.json").read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("game_id") != "twro":
        raise ValueError("Expected version 1 TWRO status reviews")
    records = payload.get("statuses")
    if not isinstance(records, dict):
        raise ValueError("Expected status review records")
    return EffectReviews(records, codes)
