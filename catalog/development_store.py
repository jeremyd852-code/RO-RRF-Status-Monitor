"""可擴充資料庫安全合併。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from catalog.schema import now_iso, read_json_object, write_json_atomically

@dataclass(frozen=True)
class DevelopmentMergeResult:
    added_statuses: int = 0
    added_items: int = 0
    confirmed_existing: int = 0
    conflicts: int = 0

def _readable_name(value: object) -> str:
    name = str(value or '').strip()
    upper = name.upper()
    if not name or '�' in name or upper.startswith(('EFST_', 'STATE_', 'STATUS_', 'SC_')):
        return ''
    return name

def merge_exact_client_names(path: Path, *, status_names: Mapping[int, str] | None=None, item_names: Mapping[int, str] | None=None, source_label: str='RO 主程式精確 ID 對應', client_signature: str='') -> DevelopmentMergeResult:
    """只合併官方可讀的精確 ID；衝突不覆蓋。"""
    destination = Path(path)
    payload = read_json_object(destination)
    records = payload.get('records', {})
    if not isinstance(records, dict):
        raise ValueError('資料庫缺少 records')
    status_records = records.setdefault('statuses', {})
    item_records = records.setdefault('items', {})
    if not isinstance(status_records, dict) or not isinstance(item_records, dict):
        raise ValueError('資料庫資料類型錯誤')
    added_statuses = 0
    added_items = 0
    confirmed_existing = 0
    conflicts = 0
    today = now_iso()[:10]

    def merge_group(target: dict[str, object], values: Mapping[int, str], *, default_category: str, minimum_id: int, maximum_id: int) -> tuple[int, int, int]:
        added = 0
        confirmed = 0
        local_conflicts = 0
        for raw_id, raw_name in sorted(values.items()):
            value_id = int(raw_id)
            name = _readable_name(raw_name)
            if not minimum_id <= value_id <= maximum_id or not name:
                continue
            key = str(value_id)
            existing = target.get(key)
            if isinstance(existing, dict):
                existing_name = _readable_name(existing.get('name'))
                if existing_name and existing_name != name:
                    local_conflicts += 1
                    continue
                sources = {str(value) for value in existing.get('sources', []) if str(value).strip()}
                sources.add(source_label)
                observed = {str(value) for value in existing.get('observed_names', []) if str(value).strip()}
                observed.add(name)
                existing.update({'id': value_id, 'name': name, 'confirmed': True, 'confirmed_at': existing.get('confirmed_at') or today, 'review_status': 'confirmed', 'verification': 'confirmed_client_exact', 'sources': sorted(sources), 'observed_names': sorted(observed)})
                confirmed += 1
                continue
            target[key] = {'id': value_id, 'name': name, 'category': default_category, 'confirmed': True, 'confirmed_at': today, 'review_status': 'confirmed', 'verification': 'confirmed_client_exact', 'observed_names': [name], 'sources': [source_label]}
            added += 1
        return (added, confirmed, local_conflicts)
    added_statuses, confirmed_statuses, status_conflicts = merge_group(status_records, status_names or {}, default_category='其他', minimum_id=0, maximum_id=65535)
    added_items, confirmed_items, item_conflicts = merge_group(item_records, item_names or {}, default_category='其他', minimum_id=1, maximum_id=4294967295)
    confirmed_existing = confirmed_statuses + confirmed_items
    conflicts = status_conflicts + item_conflicts
    if added_statuses or added_items or confirmed_existing:
        payload['updated_at'] = now_iso()
        if client_signature:
            payload['last_client_signature'] = str(client_signature)
        write_json_atomically(destination, payload, backup=True)
    return DevelopmentMergeResult(added_statuses, added_items, confirmed_existing, conflicts)
