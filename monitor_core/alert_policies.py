"""1.6.0 提示規則解析器；不依賴 Tkinter。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
from monitor_core.policies import scope_code
ALERT_RULE_KEYS = ('apply', 'yellow', 'red')

def _rule_payload(payload: object) -> dict[str, bool]:
    if not isinstance(payload, Mapping):
        return {}
    return {key: bool(payload[key]) for key in ALERT_RULE_KEYS if key in payload}

def _status_rules(payload: object) -> dict[int, dict[str, bool]]:
    if not isinstance(payload, Mapping):
        return {}
    result: dict[int, dict[str, bool]] = {}
    for raw_status_id, raw_rule in payload.items():
        try:
            status_id = int(raw_status_id)
        except (TypeError, ValueError):
            continue
        if 0 <= status_id <= 65535:
            result[status_id] = _rule_payload(raw_rule)
    return result

@dataclass(frozen=True)
class AlertDecision:
    enabled: bool
    source: str

class AlertPolicyResolver:
    """解析個別人物、人物類型與狀態預設的提示結果。"""

    def __init__(self, *, global_enabled: Mapping[str, bool] | None=None, status_rules: Mapping[int, Mapping[str, bool]] | None=None, scope_enabled: Mapping[str, bool] | None=None, scope_status_rules: Mapping[str, Mapping[int, Mapping[str, bool]]] | None=None, target_overrides: Mapping[str, object] | None=None) -> None:
        self.global_enabled = {key: bool((global_enabled or {}).get(key, True)) for key in ALERT_RULE_KEYS}
        self.status_rules = {int(status_id): _rule_payload(rule) for status_id, rule in (status_rules or {}).items()}
        self.scope_enabled = {scope_code(scope): bool(enabled) for scope, enabled in (scope_enabled or {}).items()}
        self.scope_status_rules = {scope_code(scope): {int(status_id): _rule_payload(rule) for status_id, rule in rules.items()} for scope, rules in (scope_status_rules or {}).items()}
        self.target_overrides: dict[str, dict[str, object]] = {}
        for raw_name, raw_override in (target_overrides or {}).items():
            name = str(raw_name).strip().casefold()
            if not name or not isinstance(raw_override, Mapping):
                continue
            enabled = raw_override.get('enabled')
            self.target_overrides[name] = {'enabled': enabled if isinstance(enabled, bool) else None, 'status_rules': _status_rules(raw_override.get('status_rules', {}))}

    def resolve(self, *, rule_key: str, status_id: int, relation: str, target_name: str='', default_enabled: bool=False) -> AlertDecision:
        code = scope_code(relation)
        if code not in frozenset({'self', 'party'}):
            return AlertDecision(False, '此版本不支援的目標範圍')
        if rule_key not in ALERT_RULE_KEYS:
            return AlertDecision(False, '無效提示類型')
        if not self.global_enabled[rule_key]:
            return AlertDecision(False, '提示音總開關')
        normalized_name = str(target_name or '').strip().casefold()
        target_rule = self.target_overrides.get(normalized_name, {})
        target_status_rules = target_rule.get('status_rules', {})
        if isinstance(target_status_rules, Mapping):
            exact = target_status_rules.get(int(status_id), {})
            if isinstance(exact, Mapping) and rule_key in exact:
                return AlertDecision(bool(exact[rule_key]), '個別人物＋狀態')
        target_enabled = target_rule.get('enabled')
        if isinstance(target_enabled, bool) and (not target_enabled):
            return AlertDecision(False, '個別人物靜音')
        scope_rules = self.scope_status_rules.get(code, {})
        scope_rule = scope_rules.get(int(status_id), {})
        if rule_key in scope_rule:
            return AlertDecision(bool(scope_rule[rule_key]), '人物類型＋狀態')
        if target_enabled is not True and (not self.scope_enabled.get(code, False)):
            return AlertDecision(False, '人物類型靜音')
        status_rule = self.status_rules.get(int(status_id), {})
        if rule_key in status_rule:
            return AlertDecision(bool(status_rule[rule_key]), '狀態提示規則')
        return AlertDecision(bool(default_enabled), '安全預設')
