"""把內建 Lua 狀態資料轉成監控器啟動用的小型索引。"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rrf_monitor
from catalog.schema import now_iso, write_json_atomically


def main() -> int:
    destination = ROOT / "data" / "runtime_status_index.json"
    names, groups, _source = rrf_monitor._load_twro_status_names(
        prefer_runtime_index=False
    )
    names.update(rrf_monitor.ACTOR_STATE_STATUS_NAMES)
    groups.update(
        {status_id: "減益" for status_id in rrf_monitor.ACTOR_STATE_STATUS_NAMES}
    )
    groups[888] = "增益"
    names.update(rrf_monitor.CLIENT_STATUS_DISPLAY_NAMES)
    names.update(
        {
            status_id: str(record.get("name", "")).strip()
            for status_id, record in rrf_monitor.RUNTIME_STATUS_METADATA.items()
            if str(record.get("name", "")).strip()
        }
    )
    names.update(rrf_monitor.EXTENDED_EFST_NAMES)
    categories = dict(rrf_monitor.CLIENT_STATUS_LIBRARY_CATEGORIES)
    categories.update(rrf_monitor.EXTENDED_STATUS_LIBRARY_CATEGORIES)
    groups.update(
        {
            status_id: (
                "增益"
                if category == "BUFF"
                else "減益"
                if category == "DEBUFF"
                else groups.get(status_id, "未分類")
            )
            for status_id, category in categories.items()
        }
    )
    names.update(
        {
            status_id: str(record["display_name_override"]).strip()
            for status_id, record in rrf_monitor.RUNTIME_STATUS_METADATA.items()
            if str(record.get("display_name_override", "")).strip()
        }
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "game_id": "twro",
        "app_version": "1.6.5",
        "built_at": now_iso(),
        "names": {
            str(status_id): name
            for status_id, name in sorted(names.items())
        },
        "groups": {
            str(status_id): group
            for status_id, group in sorted(groups.items())
        },
    }
    write_json_atomically(destination, payload)
    print(
        f"執行索引已產生：名稱 {len(names)}、分類 {len(groups)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
