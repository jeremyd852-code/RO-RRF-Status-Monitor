"""Read-only Chinese pet species and client-declared food lookups."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class PetCatalogEntry:
    species_id: int
    name_zh: str | None
    food_item_id: int | None
    food_name_zh: str | None
    name_source: str | None
    food_source: str | None
    food_name_source: str | None

    @property
    def missing_fields(self) -> frozenset[str]:
        return frozenset(field for field in ('name_zh', 'food_item_id', 'food_name_zh')
                         if getattr(self, field) is None)


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value)
    else:
        return None
    return number if 0 < number <= 0xFFFFFFFF else None


def _chinese_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or not re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]', value):
        return None
    return value if not any(ord(char) < 32 for char in value) else None


def _source(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _catalog_path(path: Path | str | None) -> Path:
    if path is not None:
        candidate = Path(path)
        return candidate / 'pet_catalog.json' if candidate.is_dir() else candidate
    candidate = Path(__file__).resolve().parents[1] / 'data' / 'pet_catalog.json'
    if candidate.is_file() or not getattr(sys, 'frozen', False):
        return candidate
    # Portable packages keep data beside the EXE; modules are in _internal.
    return Path(sys.executable).resolve().parent / 'data' / 'pet_catalog.json'


class PetCatalog:
    def __init__(self, data_root: Path | str | None = None) -> None:
        self.path = _catalog_path(data_root)
        entries: dict[int, PetCatalogEntry] = {}
        self.error: str | None = None
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if not isinstance(payload, dict) or payload.get('schema_version') != 1 or payload.get('game_id') != 'twro':
                raise ValueError('Unsupported pet catalog schema or game')
            raw_entries = payload.get('pets')
            if not isinstance(raw_entries, dict):
                raise ValueError('Pet entries must be an object')
            for raw_id, raw in raw_entries.items():
                species_id = _positive_id(raw_id)
                if species_id is None or not isinstance(raw, dict):
                    continue
                name = _chinese_name(raw.get('name_zh'))
                food_id = _positive_id(raw.get('food_item_id'))
                food_name = _chinese_name(raw.get('food_name_zh')) if food_id is not None else None
                entries[species_id] = PetCatalogEntry(
                    species_id, name, food_id, food_name,
                    _source(raw.get('name_source')) if name else None,
                    _source(raw.get('food_source')) if food_id else None,
                    _source(raw.get('food_name_source')) if food_name else None,
                )
        except (OSError, UnicodeError, ValueError, TypeError):
            # Missing data never prevents the monitor from starting.
            self.error = '寵物對照資料無法載入'
        self.entries: Mapping[int, PetCatalogEntry] = MappingProxyType(entries)

    def lookup(self, species_id: object) -> PetCatalogEntry | None:
        return self.entries.get(_positive_id(species_id))

    def __len__(self) -> int:
        return len(self.entries)


@lru_cache(maxsize=8)
def load_pet_catalog(path: Path | str | None = None) -> PetCatalog:
    """Load once; path may name pet_catalog.json or its data directory."""
    return PetCatalog(path)


def get_pet_info(species_id: object, path: Path | str | None = None) -> PetCatalogEntry | None:
    """Return species facts, never a nickname or a guessed food association."""
    return load_pet_catalog(path).lookup(species_id)
