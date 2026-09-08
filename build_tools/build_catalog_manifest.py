"""產生內建資料快照的完整性清單。"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from catalog.schema import build_catalog_manifest, write_json_atomically
from app.version import APP_VERSION


CATALOG_FILES = (
    "runtime_status_index.json",
    "status_catalog.json",
    "status_reviews.json",
    "client_catalog.json",
    "pet_catalog.json",
    "status_classification.json",
    "job_status_index.json",
    "job_skill_catalog.json",
    "可擴充資料庫.json",
    "EFSTIDs.lua",
    "stateiconinfo.lua",
)


def main() -> int:
    data_dir = ROOT / "data"
    payload = build_catalog_manifest(
        data_dir,
        app_version=APP_VERSION,
        filenames=CATALOG_FILES,
    )
    write_json_atomically(data_dir / "catalog_manifest.json", payload)
    counts = payload["counts"]
    print(
        "內建資料清單已產生："
        f"狀態 {counts['statuses']}、物品 {counts['items']}、"
        f"寵物 {counts['pets']}、寵物食物 {counts['pet_food_item_ids']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
