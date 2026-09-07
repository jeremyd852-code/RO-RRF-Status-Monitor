"""資料格式、校驗與安全寫入工具。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping


TWRO_GAME_ID = "twro"
CATALOG_MANIFEST_SCHEMA = 1
UNKNOWN_JOURNAL_SCHEMA = 1


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json_object(path: Path) -> dict[str, object]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 最外層必須是物件：{path}")
    return payload


def write_json_atomically(
    path: Path,
    payload: Mapping[str, object],
    *,
    backup: bool = False,
) -> Path | None:
    """驗證 JSON 後原子替換；需要時只保存一份上一版備份。"""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    # 在碰觸正式檔前先確定內容可重新讀回。
    verified = json.loads(serialized)
    if not isinstance(verified, dict):
        raise ValueError("JSON 驗證失敗")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path: Path | None = Path(temporary_name)
    backup_path = destination.with_name(destination.name + ".bak") if backup else None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 再從實際暫存檔讀一次，避免磁碟上留下截斷 JSON。
        read_json_object(temporary_path)
        if backup and destination.is_file():
            shutil.copy2(destination, backup_path)
        os.replace(temporary_path, destination)
        temporary_path = None
        return backup_path if backup_path and backup_path.is_file() else None
    finally:
        if temporary_path is not None and temporary_path.is_file():
            try:
                temporary_path.unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class FileSignature:
    name: str
    size: int
    mtime_ns: int

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


def collect_file_signatures(
    paths: Iterable[Path],
    *,
    base_dir: Path | None = None,
) -> tuple[FileSignature, ...]:
    result: list[FileSignature] = []
    base = Path(base_dir).resolve() if base_dir is not None else None
    for raw_path in paths:
        path = Path(raw_path)
        try:
            info = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        try:
            name = str(path.resolve().relative_to(base)) if base is not None else path.name
        except ValueError:
            name = path.name
        result.append(FileSignature(name, int(info.st_size), int(info.st_mtime_ns)))
    return tuple(sorted(result, key=lambda item: item.name.casefold()))


@dataclass(frozen=True)
class CatalogManifest:
    schema_version: int
    game_id: str
    app_version: str
    built_at: str
    files: Mapping[str, str]
    counts: Mapping[str, int]

    @classmethod
    def from_payload(cls, payload: object) -> "CatalogManifest":
        if not isinstance(payload, dict):
            raise ValueError("catalog manifest 不是物件")
        schema_version = int(payload.get("schema_version", 0))
        game_id = str(payload.get("game_id", ""))
        if schema_version != CATALOG_MANIFEST_SCHEMA:
            raise ValueError("catalog manifest 版本不支援")
        if game_id != TWRO_GAME_ID:
            raise ValueError("catalog manifest 不是台版 RO")
        raw_files = payload.get("files", {})
        raw_counts = payload.get("counts", {})
        if not isinstance(raw_files, dict) or not isinstance(raw_counts, dict):
            raise ValueError("catalog manifest 欄位錯誤")
        files = {
            str(name): str(value).lower()
            for name, value in raw_files.items()
            if str(name) and len(str(value)) == 64
        }
        counts = {
            str(name): max(0, int(value))
            for name, value in raw_counts.items()
        }
        return cls(
            schema_version,
            game_id,
            str(payload.get("app_version", "")),
            str(payload.get("built_at", "")),
            files,
            counts,
        )

    def validate_files(self, data_dir: Path) -> tuple[bool, tuple[str, ...]]:
        errors: list[str] = []
        root = Path(data_dir)
        for relative_name, expected_hash in self.files.items():
            path = root / relative_name
            if not path.is_file():
                errors.append(f"缺少 {relative_name}")
                continue
            try:
                actual_hash = file_sha256(path)
            except OSError as exc:
                errors.append(f"無法讀取 {relative_name}：{exc}")
                continue
            if actual_hash.casefold() != expected_hash.casefold():
                errors.append(f"{relative_name} 校驗不符")
        return not errors, tuple(errors)


def catalog_counts(data_dir: Path) -> dict[str, int]:
    root = Path(data_dir)
    counts = {
        "statuses": 0,
        "items": 0,
        "pets": 0,
        "pet_food_item_ids": 0,
    }
    try:
        statuses = read_json_object(root / "status_catalog.json")
        records = statuses.get("records", {})
        counts["statuses"] = len(records) if isinstance(records, dict) else 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        clients = read_json_object(root / "client_catalog.json")
        for key in ("items", "pets"):
            values = clients.get(key, {})
            counts[key] = len(values) if isinstance(values, dict) else 0
        values = clients.get("pet_food_item_ids", [])
        counts["pet_food_item_ids"] = len(values) if isinstance(values, list) else 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return counts


def build_catalog_manifest(
    data_dir: Path,
    *,
    app_version: str,
    filenames: Iterable[str],
) -> dict[str, object]:
    root = Path(data_dir)
    file_hashes: dict[str, str] = {}
    for name in filenames:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(path)
        file_hashes[name] = file_sha256(path)
    return {
        "schema_version": CATALOG_MANIFEST_SCHEMA,
        "game_id": TWRO_GAME_ID,
        "app_version": str(app_version),
        "built_at": now_iso(),
        "files": file_hashes,
        "counts": catalog_counts(root),
    }
