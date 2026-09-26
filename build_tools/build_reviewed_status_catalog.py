"""Materialize code-checked review metadata before runtime-index generation."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from catalog.effect_reviews import load_effect_reviews
from catalog.schema import json_sha256, now_iso, write_json_atomically


def build_reviewed_catalog(payload: dict, reviews) -> dict:
    result = deepcopy(payload)
    if result.get('game_id') != 'twro' or not isinstance(result.get('records'), dict):
        raise ValueError('Expected TWRO status catalog records')
    records = result['records']
    names = {int(k): v['name'] for k, v in records.items() if isinstance(v, dict) and v.get('name')}
    groups = {int(k): v.get('effect_group', '未分類') for k, v in records.items() if isinstance(v, dict)}
    conflicts = reviews.apply(names, groups, records)
    if conflicts:
        raise ValueError('Status review identity conflict: ' + repr(conflicts))
    for key, record in records.items():
        sid = int(key)
        record.setdefault('id', sid)
        record.setdefault('name', names.get(sid, reviews.code_map.get(sid, '')))
        record.setdefault('effect_group', groups.get(sid, '未分類'))
        record.setdefault('functional_category', '其他／待確認')
        # Missing raw metadata is not evidence of duration, item or skill origin.
        # Such reviewed names are available as advanced selections.
        record.setdefault('impact_class', '進階可追蹤')
        record.setdefault('source_tags', [])
        record.setdefault('skill_names', [])
        record.setdefault('item_names', [])
        record.setdefault('has_skill_link', False)
        record.setdefault('has_time_limit', False)
    result['records'] = dict(sorted(records.items(), key=lambda x: int(x[0])))
    result['statistics'] = {
        **result.get('statistics', {}), 'record_count': len(records),
        'functional_category_counts': dict(sorted(Counter(r['functional_category'] for r in records.values()).items())),
        'effect_group_counts': dict(sorted(Counter(r['effect_group'] for r in records.values()).items())),
    }
    result['review_source'] = 'status_reviews.json'
    result['reviewed_on'] = '2026-09-27'
    for field in ('built_at', 'source_signature', 'source_signature_scope'):
        result.pop(field, None)
    result['source_signature'] = 'sha256:' + json_sha256(result)
    result['source_signature_scope'] = '加入建置中繼資料前的已覆核台版狀態目錄'
    result['built_at'] = now_iso()
    return result


def main():
    data = ROOT / 'data'
    payload = json.loads((data / 'status_catalog.json').read_text(encoding='utf-8-sig'))
    result = build_reviewed_catalog(payload, load_effect_reviews(data))
    write_json_atomically(data / 'status_catalog.json', result)
    print(f"狀態目錄已覆核：{len(result['records'])} 筆")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
