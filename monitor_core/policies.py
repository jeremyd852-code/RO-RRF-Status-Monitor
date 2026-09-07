"""1.6.0 人物監控與設定遷移規則。

本模組不匯入 Tkinter。人物類型或單一人物最後只能解析成 auto、custom、off
其中一種模式；舊人物列勾選與 ``target_ids`` 不再是執行權限。
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, Mapping
SETTINGS_SCHEMA_VERSION = 2
MODE_AUTO = 'auto'
MODE_CUSTOM = 'custom'
MODE_OFF = 'off'
MODE_INHERIT = 'inherit'
MONITOR_MODES = frozenset({MODE_AUTO, MODE_CUSTOM, MODE_OFF})
SCOPE_SELF = 'self'
SCOPE_PARTY = 'party'
SCOPE_SCREEN = 'screen'
SCOPE_OTHER = 'other'
SCOPE_CODES = (SCOPE_SELF, SCOPE_PARTY, SCOPE_SCREEN, SCOPE_OTHER)
SCOPE_LABELS = {SCOPE_SELF: '自己', SCOPE_PARTY: '隊伍成員', SCOPE_SCREEN: '畫面成員', SCOPE_OTHER: '其他目標'}
SCOPE_CODES_BY_LABEL = {label: code for code, label in SCOPE_LABELS.items()}
LEGACY_AUTHORITY_KEYS = ('target_ids', 'target_scope_enabled', 'target_scope_display_modes', 'target_scope_status_ids', 'target_display_mode_overrides', 'target_status_overrides')
DEFAULT_SCOPE_MODES = {SCOPE_SELF: MODE_AUTO, SCOPE_PARTY: MODE_AUTO, SCOPE_SCREEN: MODE_OFF, SCOPE_OTHER: MODE_OFF}
LEGACY_MODE_ALL = '全部狀態'
LEGACY_MODE_FOCUSED = '重點狀態'
LEGACY_MODE_OFF = '不監控'
LEGACY_MODE_INHERIT = '跟隨人物類型'
UI_MODE_AUTO = '自動監測'
UI_MODE_CUSTOM = '自訂監測'

def normalize_status_ids(values: object) -> frozenset[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    result: set[int] = set()
    for value in values:
        try:
            status_id = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= status_id <= 65535:
            result.add(status_id)
    return frozenset(result)

def scope_code(value: object) -> str:
    text = str(value or '').strip()
    if text in SCOPE_CODES:
        return text
    return SCOPE_CODES_BY_LABEL.get(text, SCOPE_OTHER)

def scope_label(value: object) -> str:
    return SCOPE_LABELS.get(scope_code(value), SCOPE_LABELS[SCOPE_OTHER])

def normalize_mode(value: object, fallback: str=MODE_OFF) -> str:
    text = str(value or '').strip()
    if text in MONITOR_MODES:
        return text
    return {LEGACY_MODE_ALL: MODE_AUTO, LEGACY_MODE_FOCUSED: MODE_CUSTOM, UI_MODE_AUTO: MODE_AUTO, UI_MODE_CUSTOM: MODE_CUSTOM, LEGACY_MODE_OFF: MODE_OFF}.get(text, fallback if fallback in MONITOR_MODES else MODE_OFF)

def legacy_mode(mode: str) -> str:
    return {MODE_AUTO: LEGACY_MODE_ALL, MODE_CUSTOM: LEGACY_MODE_FOCUSED, MODE_OFF: LEGACY_MODE_OFF}.get(mode, LEGACY_MODE_OFF)

@dataclass(frozen=True)
class ScopePolicy:
    mode: str
    custom_status_ids: frozenset[int] = frozenset()

@dataclass(frozen=True)
class EffectivePolicy:
    mode: str
    custom_status_ids: frozenset[int]
    source: str

    def displays(self, status_id: int) -> bool:
        if self.mode == MODE_OFF:
            return False
        if self.mode == MODE_AUTO:
            return True
        return int(status_id) in self.custom_status_ids

@dataclass(frozen=True)
class SettingsMigrationReport:
    source_schema: int
    target_schema: int
    migrated: bool
    ignored_target_id_rules: int
    dormant_status_count: int
    messages: tuple[str, ...]

class PolicyResolver:
    """解析單一目標最後生效的監控規則。"""

    def __init__(self, scope_policies: Mapping[str, ScopePolicy] | None=None, name_overrides: Mapping[str, ScopePolicy] | None=None, self_override: ScopePolicy | None=None, session_id_overrides: Mapping[int, ScopePolicy] | None=None) -> None:
        source = scope_policies or {}
        self.scope_policies = {code: source.get(code, ScopePolicy(DEFAULT_SCOPE_MODES[code])) if code in frozenset({'self', 'party'}) else ScopePolicy(MODE_OFF) for code in SCOPE_CODES}
        self.name_overrides = {str(name).strip().casefold(): policy for name, policy in (name_overrides or {}).items() if str(name).strip()}
        self.self_override = self_override
        self.session_id_overrides = {int(target_id): policy for target_id, policy in (session_id_overrides or {}).items() if int(target_id) > 0}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], *, session_id_overrides: Mapping[int, ScopePolicy] | None=None) -> 'PolicyResolver':
        scope_payload = settings.get('scope_policies', {})
        scope_policies: dict[str, ScopePolicy] = {}
        if isinstance(scope_payload, Mapping):
            for raw_scope, raw_policy in scope_payload.items():
                code = scope_code(raw_scope)
                scope_policies[code] = policy_from_payload(raw_policy, fallback=DEFAULT_SCOPE_MODES[code])
        name_overrides: dict[str, ScopePolicy] = {}
        self_override: ScopePolicy | None = None
        raw_overrides = settings.get('target_overrides', {})
        if isinstance(raw_overrides, Mapping):
            for raw_selector, raw_policy in raw_overrides.items():
                selector = str(raw_selector).strip()
                if not selector:
                    continue
                policy = policy_from_payload(raw_policy, fallback=MODE_OFF, allow_inherit=True)
                if policy.mode == MODE_INHERIT:
                    continue
                if selector.casefold() == SCOPE_SELF:
                    self_override = policy
                elif not selector.casefold().startswith('id:'):
                    name_overrides[selector] = policy
        return cls(scope_policies, name_overrides, self_override, session_id_overrides)

    def resolve(self, *, target_id: int, relation: str, target_name: str='') -> EffectivePolicy:
        code = scope_code(relation)
        if code not in frozenset({'self', 'party'}):
            return EffectivePolicy(MODE_OFF, frozenset(), '此版本不支援的目標範圍')
        target_id = int(target_id)
        if target_id in self.session_id_overrides:
            policy = self.session_id_overrides[target_id]
            return EffectivePolicy(policy.mode, policy.custom_status_ids, '本次人物覆寫')
        normalized_name = str(target_name or '').strip().casefold()
        if normalized_name and normalized_name in self.name_overrides:
            policy = self.name_overrides[normalized_name]
            return EffectivePolicy(policy.mode, policy.custom_status_ids, '人物名稱覆寫')
        if code == SCOPE_SELF and self.self_override is not None:
            policy = self.self_override
            return EffectivePolicy(policy.mode, policy.custom_status_ids, '自己覆寫')
        policy = self.scope_policies[code]
        return EffectivePolicy(policy.mode, policy.custom_status_ids, f'{SCOPE_LABELS[code]}規則')

def policy_from_payload(payload: object, *, fallback: str, allow_inherit: bool=False) -> ScopePolicy:
    if isinstance(payload, Mapping):
        raw_mode = payload.get('mode')
        raw_ids = payload.get('custom_status_ids', ())
    else:
        raw_mode = payload
        raw_ids = ()
    mode = str(raw_mode or '').strip()
    valid_modes = MONITOR_MODES | ({MODE_INHERIT} if allow_inherit else set())
    if mode not in valid_modes:
        mode = normalize_mode(raw_mode, fallback)
    return ScopePolicy(mode, normalize_status_ids(raw_ids))

def scope_policies_from_legacy(settings: Mapping[str, object]) -> dict[str, ScopePolicy]:
    raw_modes = settings.get('target_scope_display_modes', {})
    raw_lists = settings.get('target_scope_status_ids', {})
    raw_enabled = settings.get('target_scope_enabled', {})
    mode_map = raw_modes if isinstance(raw_modes, Mapping) else {}
    list_map = raw_lists if isinstance(raw_lists, Mapping) else {}
    enabled_map = raw_enabled if isinstance(raw_enabled, Mapping) else {}
    result: dict[str, ScopePolicy] = {}
    for code in SCOPE_CODES:
        label = SCOPE_LABELS[code]
        custom_ids = normalize_status_ids(list_map.get(label, ()))
        if label in mode_map:
            mode = normalize_mode(mode_map.get(label), DEFAULT_SCOPE_MODES[code])
            if code == SCOPE_PARTY and mode == MODE_CUSTOM and (not custom_ids):
                mode = MODE_AUTO
        elif label in enabled_map:
            mode = MODE_CUSTOM if bool(enabled_map.get(label)) else MODE_OFF
            if code in {SCOPE_SELF, SCOPE_PARTY} and bool(enabled_map.get(label)) and (not custom_ids):
                mode = MODE_AUTO
        else:
            mode = DEFAULT_SCOPE_MODES[code]
        result[code] = ScopePolicy(mode, custom_ids) if code in frozenset({'self', 'party'}) else ScopePolicy(MODE_OFF)
    return result

def migrate_settings_to_v2(saved: Mapping[str, object] | None) -> tuple[dict[str, object], SettingsMigrationReport]:
    """把 1.5.x 設定轉成 v2；不修改傳入物件。"""
    source: dict[str, object] = deepcopy(dict(saved or {}))
    try:
        source_schema = int(source.get('settings_schema_version', 1))
    except (TypeError, ValueError):
        source_schema = 1
    messages: list[str] = []
    if source_schema >= SETTINGS_SCHEMA_VERSION and isinstance(source.get('scope_policies'), Mapping):
        resolver = PolicyResolver.from_settings(source)
        source['scope_policies'] = serialize_scope_policies(resolver.scope_policies)
        source['settings_schema_version'] = SETTINGS_SCHEMA_VERSION
        for key in LEGACY_AUTHORITY_KEYS:
            source.pop(key, None)
        return (source, SettingsMigrationReport(source_schema=source_schema, target_schema=SETTINGS_SCHEMA_VERSION, migrated=False, ignored_target_id_rules=0, dormant_status_count=sum((len(policy.custom_status_ids) for policy in resolver.scope_policies.values())), messages=('設定已是 v2，完成格式驗證',)))
    policies = scope_policies_from_legacy(source)
    source['settings_schema_version'] = SETTINGS_SCHEMA_VERSION
    source['scope_policies'] = serialize_scope_policies(policies)
    ignored_id_rules = 0
    for key in ('target_display_mode_overrides', 'target_status_overrides'):
        payload = source.get(key, {})
        if isinstance(payload, Mapping):
            ignored_id_rules += len(payload)
    source['target_overrides'] = {}
    source['alert_policies'] = {'status_rules': deepcopy(source.get('status_alert_rules', {})), 'scope_enabled': deepcopy(source.get('target_sound_enabled', {})), 'target_overrides': {}}
    for key in LEGACY_AUTHORITY_KEYS:
        source.pop(key, None)
    dormant_count = sum((len(policy.custom_status_ids) for policy in policies.values()))
    if dormant_count:
        messages.append(f'保留 {dormant_count} 項自訂狀態作為待用清單')
    if ignored_id_rules:
        messages.append(f'{ignored_id_rules} 條舊人物 ID 規則未轉成長期權限')
    if not messages:
        messages.append('已套用 schema v2 安全預設')
    return (source, SettingsMigrationReport(source_schema=source_schema, target_schema=SETTINGS_SCHEMA_VERSION, migrated=True, ignored_target_id_rules=ignored_id_rules, dormant_status_count=dormant_count, messages=tuple(messages)))

def serialize_scope_policies(policies: Mapping[str, ScopePolicy]) -> dict[str, dict[str, object]]:
    return {code: {'mode': policies.get(code, ScopePolicy(DEFAULT_SCOPE_MODES[code])).mode if code in frozenset({'self', 'party'}) else MODE_OFF, 'custom_status_ids': sorted(policies.get(code, ScopePolicy(DEFAULT_SCOPE_MODES[code])).custom_status_ids)} for code in SCOPE_CODES}

def legacy_runtime_fields(settings: Mapping[str, object]) -> dict[str, object]:
    """提供舊 UI 的唯讀相容值；執行權限仍以 v2 policy 為準。"""
    resolver = PolicyResolver.from_settings(settings)
    modes: dict[str, str] = {}
    status_ids: dict[str, list[int]] = {}
    enabled: dict[str, bool] = {}
    for code in SCOPE_CODES:
        label = SCOPE_LABELS[code]
        policy = resolver.scope_policies[code]
        modes[label] = legacy_mode(policy.mode)
        status_ids[label] = sorted(policy.custom_status_ids)
        enabled[label] = policy.mode != MODE_OFF
    return {'target_scope_display_modes': modes, 'target_scope_status_ids': status_ids, 'target_scope_enabled': enabled, 'target_ids': [], 'target_display_mode_overrides': {}, 'target_status_overrides': {}}

def build_scope_policies(modes_by_label: Mapping[str, object], status_ids_by_label: Mapping[str, Iterable[int]]) -> dict[str, ScopePolicy]:
    result: dict[str, ScopePolicy] = {}
    for code in SCOPE_CODES:
        label = SCOPE_LABELS[code]
        result[code] = ScopePolicy(normalize_mode(modes_by_label.get(label), DEFAULT_SCOPE_MODES[code]) if code in frozenset({'self', 'party'}) else MODE_OFF, normalize_status_ids(status_ids_by_label.get(label, ())))
    return result
