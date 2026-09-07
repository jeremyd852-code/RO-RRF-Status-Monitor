"""RO RRF 即時狀態監控器（唯讀）。

只讀取 Ragnarok Replay File（.rrf），不修改遊戲、不注入程序，
也不會送出滑鼠或鍵盤輸入。
"""
from __future__ import annotations
import base64
import ctypes
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import threading
import time
import traceback
import zlib
from collections import Counter, deque
from functools import wraps
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from ctypes import wintypes
_TCL_DLL_HANDLE = None

def windows_short_path(path: str) -> str:
    """把含中文字的路徑轉成 Tcl 可穩定讀取的 Windows 短路徑。"""
    if os.name != 'nt':
        return path
    try:
        get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short_path_name.restype = ctypes.c_uint32
        required = get_short_path_name(path, None, 0)
        if not required:
            return path
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_short_path_name(path, buffer, len(buffer))
        return buffer.value if written else path
    except (AttributeError, OSError, TypeError):
        return path

def ascii_tcl_runtime_paths(tcl_library: str, tk_library: str) -> tuple[str, str]:
    """讓 Tcl/Tk 在含中文的安裝路徑下仍能穩定讀取腳本。"""
    candidates = ((windows_short_path(tcl_library), windows_short_path(tk_library)), (tcl_library, tk_library))
    for tcl_candidate, tk_candidate in candidates:
        try:
            tcl_candidate.encode('ascii')
            tk_candidate.encode('ascii')
        except UnicodeEncodeError:
            continue
        if os.path.isfile(os.path.join(tcl_candidate, 'init.tcl')) and os.path.isfile(os.path.join(tk_candidate, 'pkgIndex.tcl')):
            return (os.path.normpath(tcl_candidate), os.path.normpath(tk_candidate))
    try:
        key_parts: list[str] = []
        for source_path in (os.path.join(tcl_library, 'init.tcl'), os.path.join(tcl_library, 'encoding', 'cp950.enc'), os.path.join(tk_library, 'pkgIndex.tcl'), os.path.join(tk_library, 'tk.tcl')):
            info = os.stat(source_path)
            key_parts.append(f'{os.path.normcase(os.path.abspath(source_path))}|{info.st_size}|{info.st_mtime_ns}')
        runtime_key = hashlib.sha256('\n'.join(key_parts).encode('utf-8')).hexdigest()[:16]
        temp_root = windows_short_path(tempfile.gettempdir())
        temp_root.encode('ascii')
        cache_parent = os.path.join(temp_root, 'RO-RRF-Status-Monitor')
        cache_root = os.path.join(cache_parent, f'tcltk-v2-{runtime_key}')
        cached_tcl = os.path.join(cache_root, 'tcl')
        cached_tk = os.path.join(cache_root, 'tk')
        complete_marker = os.path.join(cache_root, '.complete')
        cache_ready = all((os.path.isfile(path) for path in (os.path.join(cached_tcl, 'init.tcl'), os.path.join(cached_tcl, 'encoding', 'cp950.enc'), os.path.join(cached_tk, 'pkgIndex.tcl'), os.path.join(cached_tk, 'tk.tcl'), complete_marker)))
        if not cache_ready:
            os.makedirs(cache_parent, exist_ok=True)
            if os.path.isdir(cache_root):
                shutil.rmtree(cache_root, ignore_errors=True)
            staging_root = tempfile.mkdtemp(prefix=f'tcltk-v2-{runtime_key}-', dir=cache_parent)
            try:
                shutil.copytree(tcl_library, os.path.join(staging_root, 'tcl'))
                shutil.copytree(tk_library, os.path.join(staging_root, 'tk'))
                with open(os.path.join(staging_root, '.complete'), 'w', encoding='ascii') as handle:
                    handle.write(runtime_key + '\n')
                try:
                    os.replace(staging_root, cache_root)
                    staging_root = ''
                except FileExistsError:
                    pass
            finally:
                if staging_root and os.path.isdir(staging_root):
                    shutil.rmtree(staging_root, ignore_errors=True)
            cached_tcl = os.path.join(cache_root, 'tcl')
            cached_tk = os.path.join(cache_root, 'tk')
        return (cached_tcl, cached_tk)
    except (OSError, UnicodeEncodeError):
        return (os.path.normpath(tcl_library), os.path.normpath(tk_library))

def configure_tk_environment() -> None:
    """在匯入 Tk 前定位 Tcl/Tk；同時支援原始碼與資料夾版 EXE。"""
    roots = {os.path.dirname(os.path.abspath(sys.executable)), os.path.dirname(os.path.abspath(__file__)), os.path.abspath(getattr(sys, '_MEIPASS', '')) if getattr(sys, '_MEIPASS', '') else '', os.path.abspath(getattr(sys, 'prefix', '')) if getattr(sys, 'prefix', '') else ''}
    for root in roots:
        if not root:
            continue
        for tcl_library, tk_library in ((os.path.join(root, '_tcl_data'), os.path.join(root, '_tk_data')), (os.path.join(root, '_internal', '_tcl_data'), os.path.join(root, '_internal', '_tk_data'))):
            if os.path.isfile(os.path.join(tcl_library, 'init.tcl')) and os.path.isfile(os.path.join(tk_library, 'pkgIndex.tcl')):
                resolved_tcl, resolved_tk = ascii_tcl_runtime_paths(tcl_library, tk_library)
                os.environ['TCL_LIBRARY'] = resolved_tcl
                os.environ['TK_LIBRARY'] = resolved_tk
                return
        for tcl_root in (os.path.join(root, 'tcl'), os.path.join(root, '_internal', 'tcl'), os.path.join(root, 'lib')):
            tcl_library = os.path.join(tcl_root, 'tcl8.6')
            tk_library = os.path.join(tcl_root, 'tk8.6')
            if os.path.isfile(os.path.join(tcl_library, 'init.tcl')) and os.path.isfile(os.path.join(tk_library, 'pkgIndex.tcl')):
                resolved_tcl, resolved_tk = ascii_tcl_runtime_paths(tcl_library, tk_library)
                os.environ['TCL_LIBRARY'] = resolved_tcl
                os.environ['TK_LIBRARY'] = resolved_tk
                return

def initialize_tcl_executable() -> None:
    """先提供 Tcl 完整執行檔路徑，避免 Windows 把現有腳本誤判為遺失。"""
    global _TCL_DLL_HANDLE
    if os.name != 'nt':
        return
    os.environ.pop('TCL_LIBRARY', None)
    os.environ.pop('TK_LIBRARY', None)
    runtime_root = os.path.abspath(getattr(sys, '_MEIPASS', ''))
    dll_candidates = [os.path.join(runtime_root, 'tcl86t.dll') if runtime_root else '', os.path.join(os.path.dirname(os.path.abspath(sys.executable)), '_internal', 'tcl86t.dll'), os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'tcl86t.dll'), os.path.join(os.path.abspath(getattr(sys, 'base_prefix', '')), 'DLLs', 'tcl86t.dll')]
    tcl_dll_path = next((candidate for candidate in dll_candidates if candidate and os.path.isfile(candidate)), None)
    if tcl_dll_path is None:
        return
    executable_hint = windows_short_path(os.path.abspath(sys.executable))
    try:
        executable_bytes = executable_hint.replace('\\', '/').encode('ascii')
    except UnicodeEncodeError:
        system_root = os.environ.get('SystemRoot', 'C:\\Windows')
        executable_bytes = os.path.join(system_root, 'System32', 'cmd.exe').replace('\\', '/').encode('ascii', errors='ignore')
    try:
        _TCL_DLL_HANDLE = ctypes.WinDLL(tcl_dll_path)
        find_executable = _TCL_DLL_HANDLE.Tcl_FindExecutable
        find_executable.argtypes = [ctypes.c_char_p]
        find_executable.restype = None
        find_executable(executable_bytes)
    except (AttributeError, OSError):
        return
initialize_tcl_executable()
configure_tk_environment()
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import BinaryIO, Callable, Iterable
import tkinter as tk
from monitor_ui.view_models import calculate_auto_height_geometry, compact_status_summary, format_overlay_duration, shorten_text
from monitor_ui.card_layout import arrange_card_heights
from monitor_core.catalog import TWRO_GAME_ID, StatusLibraryRecord, StatusSearchIndex, bundled_catalog_is_twro, detect_ro_client, filter_status_records, paginate_status_ids, twro_install_message
from monitor_core.alerts import expiration_level, expiration_phase, level_label, status_visual_level
from monitor_core.alert_policies import AlertPolicyResolver
from monitor_core.actor_state import ACTOR_STATE_HEADER, ACTOR_STATE_STATUS_NAMES, decode_actor_state_packet
from monitor_core.policies import MODE_AUTO, MODE_CUSTOM, MODE_OFF, PolicyResolver, ScopePolicy, build_scope_policies, legacy_mode, legacy_runtime_fields, migrate_settings_to_v2, normalize_mode, scope_code, serialize_scope_policies
from monitor_core.session import MonitorSession
from monitor_core.capabilities import ALLOWED_TARGET_SCOPES, local_data_filename
from monitor_core.snapshots import StatusObservation
from monitor_core.replay_discovery import LiveReplayDiscovery, ReplayFileSample
from app.bootstrap import StartupTimeline
from app.lifecycle import CloseAction, decide_close_action
from catalog.schema import CatalogManifest, json_sha256, read_json_object, write_json_atomically as write_catalog_json_atomically
from catalog.sync_worker import CatalogWorkerCancelled, CatalogWorkerClient, CatalogWorkerProgress, commit_catalog_cache_files
from catalog.unknown_journal import UnknownJournal
from catalog.development_store import merge_exact_client_names
from catalog.pets import get_pet_info
from monitor_core.pet_alerts import CRITICAL_SATIETY_THRESHOLD, PetAlertController
from monitor_core.pet_snapshot import ReplayPetSnapshot, decode_pet_snapshot
from monitor_ui.pet_overlay import PetOverlay
try:
    import winsound
except ImportError:
    winsound = None

def application_dir() -> Path:
    """回傳程式實際所在資料夾；支援原始碼與 PyInstaller 資料夾版 EXE。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
APPLICATION_DIR = application_dir()
BUILD_CHANNEL = 'release'
DEVELOPMENT_BUILD = False

def scoped_settings(settings: dict) -> dict:
    """匯入、儲存與預設值都遵守允許的監控範圍。"""
    result = dict(settings)
    # 移除過時設定欄位。
    result.pop('edition', None)
    result['self_target_id'] = None
    result['self_target_name'] = ''
    for key in ('scope_policies', 'target_scope_enabled', 'target_scope_display_modes', 'target_sound_enabled', 'target_scope_status_ids'):
        if isinstance(result.get(key), dict):
            result[key] = {scope: value for scope, value in result[key].items() if scope_code(scope) in frozenset({'self', 'party'})}
    alerts = result.get('alert_policies')
    if isinstance(alerts, dict):
        alerts = dict(alerts)
        for key in ('scope_enabled', 'scope_status_rules', 'scope_rules'):
            if isinstance(alerts.get(key), dict):
                alerts[key] = {scope: value for scope, value in alerts[key].items() if scope_code(scope) in frozenset({'self', 'party'})}
        result['alert_policies'] = alerts
    return result

def serialized_scoped_monitor(function):
    """監控交付鎖讓停止／重掃與封包、快照、提醒依序完成。"""

    @wraps(function)
    def guarded(self, *args, **kwargs):
        lock = self.__dict__.setdefault('_scoped_monitor_lock', threading.RLock())
        with lock:
            return function(self, *args, **kwargs)
    return guarded

def import_settings_to_config(source_path: Path, destination_path: Path) -> tuple[dict[str, object], object, Path | None]:
    """驗證並匯入舊設定；目的檔以備份與原子替換保護。"""
    with Path(source_path).open('r', encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError('設定檔最外層必須是 JSON 物件')
    migrated, report = migrate_settings_to_v2(payload)
    migrated = scoped_settings(migrated)
    serialized = json.dumps(migrated, ensure_ascii=False, indent=2)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if destination.exists():
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = destination.with_name(f'{destination.stem}-before-import-{timestamp}{destination.suffix}.bak')
        suffix = 1
        while backup_path.exists():
            backup_path = destination.with_name(f'{destination.stem}-before-import-{timestamp}-{suffix}{destination.suffix}.bak')
            suffix += 1
        backup_path.write_bytes(destination.read_bytes())
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{destination.stem}-import-', suffix='.tmp', dir=str(destination.parent))
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(serialized)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return (migrated, report, backup_path)
DEFAULT_REPLAY_DIR = Path('C:\\Program Files (x86)\\Gravity\\RagnarokOnline\\Replay')
DEFAULT_RO_DIR = Path('C:\\Program Files (x86)\\Gravity\\RagnarokOnline')
FOCUS_STATUS_ID = 78
PET_PROPERTY_HEADER = 418
PET_FEED_HEADER = 419
PET_STATE_HEADER = 420
SELF_ID_HEADER = 643
PET_STATE_FIELDS = {1: '親密度', 2: '飽食度'}
SOUND_MODES = ('導航人聲', '系統提示音', '較明顯警告音', '自訂 WAV')
MIN_PYTHON_VERSION = (3, 10)
APPLY_ALERT_COOLDOWN_SECONDS = 2.0
APPLY_ALERT_GLOBAL_COOLDOWN_SECONDS = 1.0
MAX_PENDING_APPLY_EVENTS = 256
MAX_FINISHED_STATES = 1024
PROCESS_PACKET_BATCH_SIZE = 512
PROCESS_BATCH_TIME_BUDGET_SECONDS = 0.025
MAX_ACTIVE_STATES = 4096
MAX_QUARANTINED_STATES = 512
MAX_TRACKED_TARGETS = 4096
MAX_UNKNOWN_IDS = 2000
UNKNOWN_MEMORY_BATCH_LIMIT = 100
MAX_UI_CALLBACK_QUEUE = 256
UI_CALLBACK_MAX_PER_TICK = 50
UI_CALLBACK_TIME_BUDGET_MS = 12
REPLAY_DISCOVERY_INTERVAL_SECONDS = 3.0
REPLAY_ACTIVE_GRACE_SECONDS = 20.0
UNKNOWN_STATUS_INDEX_RETRY_DELAY_MS = 300000
UNKNOWN_STATUS_INDEX_MAX_ATTEMPTS = 2
MONITOR_IDLE_INTERVAL_MAX_SECONDS = 1.5
TARGET_OPTIONS_REFRESH_INTERVAL_SECONDS = 1.0
SETTINGS_SAVE_DEBOUNCE_MS = 600
STATUS_SEARCH_DEBOUNCE_MS = 180
OVERLAY_DEFAULT_WIDTH = 260
OVERLAY_DEFAULT_HEIGHT = 520
OVERLAY_MIN_WIDTH = 220
OVERLAY_MIN_HEIGHT = 150
OVERLAY_DEFAULT_FONT_SIZE = 9
OVERLAY_MIN_FONT_SIZE = 8
OVERLAY_MAX_FONT_SIZE = 14
OVERLAY_DEFAULT_OPACITY = 0.9
OVERLAY_MIN_OPACITY = 0.55
OVERLAY_MAX_OPACITY = 1.0
OVERLAY_RIGHT_MARGIN = 16
OVERLAY_TOP_FRACTION = 0.22
OVERLAY_SAFE_TOP_MARGIN = 12
OVERLAY_SAFE_BOTTOM_MARGIN = 16
OVERLAY_RESIZE_BORDER = 7
OVERLAY_DRAG_INTERVAL_MS = 33
OVERLAY_RESIZE_RENDER_INTERVAL_MS = 66
OVERLAY_TRANSPARENT = '#010203'
OVERLAY_BG = '#FFFFFF'
OVERLAY_SUMMARY_BG = '#DCEAF5'
OVERLAY_CARD_BG = '#FFFFFF'
OVERLAY_PET_BG = '#FFFFFF'
OVERLAY_BORDER = '#A8BAC9'
OVERLAY_TITLE_TEXT = '#2E4D66'
OVERLAY_TEXT = '#263846'
OVERLAY_MUTED_TEXT = '#526675'
OVERLAY_SEPARATOR = '#DFE7ED'
PET_ALERT_DEFAULT_THRESHOLD = 25
UI_REFRESH_INTERVAL_MS = 1000
UI_CALLBACK_INTERVAL_MS = 100
ALERT_RULE_KEYS = ('apply', 'yellow', 'red')
ALERT_RULE_LABELS = {'apply': '立即', 'yellow': '黃燈', 'red': '紅燈'}
STATUS_GROUP_ORDER = ('主要監控', '增益', '減益', '開關／特殊', '伺服器狀態', '未分類')
STATUS_GROUP_BY_COLOR = {'COLOR_TITLE_BUFF': '增益', 'COLOR_TITLE_DEBUFF': '減益', 'COLOR_TITLE_TOGGLE': '開關／特殊'}
STATUS_LIBRARY_CATEGORY_ORDER = ('技能', '消耗品', 'BUFF', 'DEBUFF', '掉寶', '經驗', '其他')
STATUS_LIBRARY_INDEX_MODES = ('分類', '職業技能', '已勾選', '常用狀態')
STATUS_LIBRARY_PLAYER_ENTRIES = ('已選擇', '職業技能', '消耗品', '異常狀態', '其他／搜尋')
CONSUMABLE_SUBCATEGORY_ORDER = ('全部消耗品', '經驗／掉寶', '料理／能力值', '攻擊／魔法', '攻速／移速', '防禦／抗性', 'HP／SP／恢復', '技能卷軸', '毒藥／負面效果', '特殊效果／變身', '其他／待確認')
CONSUMABLE_SUBCATEGORY_ALIASES = {'料理': '料理／能力值', '素質料理': '料理／能力值', '補品': 'HP／SP／恢復', '藥水／補品': 'HP／SP／恢復', 'HP／SP／持續恢復': 'HP／SP／恢復', '卷軸／書籍': '技能卷軸', '糖／特殊食品': '特殊效果／變身', '食品／飲料': '特殊效果／變身', '特殊／變身': '特殊效果／變身', '戰鬥藥': '攻擊／魔法', '其他持續效果': '其他／待確認', '待確認消耗品': '其他／待確認'}
JOB_EFFECT_FILTER_ORDER = ('全部效果', 'BUFF', 'DEBUFF', '開關／特殊', '未分類')
STATUS_FUNCTION_FILTER_ORDER = ('全部用途', '經驗／掉寶／收益', '負面／異常狀態', '能力值', '攻擊／傷害／命中', '防禦／減傷／迴避', '屬性／抗性', '移動／行動／施法', '恢復／資源', '死亡／保護', '冷卻／再使用', '姿態／開關／特殊', '職業技能／其他', '消耗品／料理', '消耗品／補品', '消耗品／卷軸／書籍', '消耗品／糖／特殊食品', '消耗品／食品／飲料', '消耗品／其他持續效果', '其他增益', '其他／待確認')
STATUS_LIBRARY_PAGE_SIZE = 60
STATUS_LIBRARY_NAME_HINTS = {'消耗品': ('料理', '食物', '食品', '藥水', '藥劑', '生命水', '魔力水', '集中力', '覺醒', '菠色克', '維他命', '濃縮', '蜂蜜', '葡萄', '蘋果', '香蕉', '肉', '魚', '麵包', '蛋糕', '餅乾', '熱狗', '便當', '料理', '卷軸', '書', '補品', '研磨', '糖', 'scroll', 'book', 'potion', 'food', 'meal', 'candy'), '掉寶': ('掉寶', '掉落', 'drop'), '經驗': ('經驗', 'experience')}
TARGET_SCOPE_ORDER = ('自己', '隊伍成員')
TARGET_SCOPE_DISPLAY_NAMES = {'自己': '自己', '隊伍成員': '隊伍', '畫面成員': '畫面', '其他目標': '其他'}
TARGET_VIEW_MODES = ('全部人物', '最近出現')
TARGET_RECENT_WINDOW_MS = 30000
TARGET_SCOPE_DEFAULTS = {'自己': True, '隊伍成員': True, '畫面成員': False, '其他目標': False}
TARGET_DISPLAY_MODE_ALL = '全部狀態'
TARGET_DISPLAY_MODE_FOCUSED = '重點狀態'
TARGET_DISPLAY_MODE_OFF = '不監控'
TARGET_DISPLAY_MODE_INHERIT = '跟隨人物類型'
TARGET_DISPLAY_MODES = (TARGET_DISPLAY_MODE_ALL, TARGET_DISPLAY_MODE_FOCUSED, TARGET_DISPLAY_MODE_OFF)
TARGET_DISPLAY_MODE_DEFAULTS = {'自己': TARGET_DISPLAY_MODE_ALL, '隊伍成員': TARGET_DISPLAY_MODE_ALL, '畫面成員': TARGET_DISPLAY_MODE_OFF, '其他目標': TARGET_DISPLAY_MODE_OFF}
TARGET_SOUND_DEFAULTS = {'自己': True, '隊伍成員': False, '畫面成員': False, '其他目標': False}
CORE_MONITORING_ONLY_DEFAULT = False
CORE_TARGET_SCOPE_DEFAULTS = {'自己': True, '隊伍成員': False, '畫面成員': False, '其他目標': False}
DEFAULT_CONFIG = {'settings_schema_version': 2, 'scope_policies': {'self': {'mode': 'auto', 'custom_status_ids': [FOCUS_STATUS_ID]}, 'party': {'mode': 'auto', 'custom_status_ids': []}, 'screen': {'mode': 'off', 'custom_status_ids': []}, 'other': {'mode': 'off', 'custom_status_ids': []}}, 'target_overrides': {}, 'alert_policies': {}, 'replay_dir': str(DEFAULT_REPLAY_DIR), 'ro_install_dir': str(DEFAULT_RO_DIR), 'auto_client_data': False, 'selected_file': '', 'auto_latest': True, 'alert_status_ids': [FOCUS_STATUS_ID], 'show_all_statuses': False, 'alert_seconds': 15, 'yellow_seconds': 30, 'red_seconds': 15, 'yellow_sound_enabled': True, 'red_sound_enabled': True, 'apply_sound_enabled': True, 'status_alert_rules': {}, 'target_status_overrides': {}, 'target_display_mode_overrides': {}, 'show_technical_columns': False, 'overlay_width': OVERLAY_DEFAULT_WIDTH, 'overlay_height': OVERLAY_DEFAULT_HEIGHT, 'overlay_x': None, 'overlay_y': None, 'overlay_auto_height': True, 'overlay_locked': False, 'overlay_opacity': OVERLAY_DEFAULT_OPACITY, 'overlay_font_size': OVERLAY_DEFAULT_FONT_SIZE, 'tree_column_widths': {}, 'sound_mode': SOUND_MODES[0], 'sound_file': '', 'overlay_enabled': True, 'sync_offset_seconds': 5, 'auto_sync': True, 'poll_seconds': 0.5, 'core_monitoring_only': CORE_MONITORING_ONLY_DEFAULT, 'target_scope_enabled': dict(TARGET_SCOPE_DEFAULTS), 'target_scope_display_modes': dict(TARGET_DISPLAY_MODE_DEFAULTS), 'target_sound_enabled': dict(TARGET_SOUND_DEFAULTS), 'auto_resolve_unknown': True, 'target_scope_status_ids': {'自己': [FOCUS_STATUS_ID], '隊伍成員': [], '畫面成員': [], '其他目標': []}, 'self_target_id': None, 'self_target_name': '', 'target_view': TARGET_VIEW_MODES[0], 'pet_monitor_enabled': True, 'pet_selected_id': None, 'pet_alert_enabled': True, 'pet_alert_threshold': PET_ALERT_DEFAULT_THRESHOLD, 'pet_overlay_enabled': True, 'pet_overlay_x': None, 'pet_overlay_y': None, 'pet_overlay_width': 280, 'pet_overlay_font_size': 12, 'pet_overlay_opacity': 0.9, 'pet_overlay_locked': False}
DEFAULT_CONFIG = scoped_settings(DEFAULT_CONFIG)

def resolved_target_display_modes(saved_modes: object, saved_scopes: object, *, core_monitoring_only: bool, migrate_legacy: bool=False) -> dict[str, str]:
    """讀取人物顯示模式；舊設定依原開關安全遷移為重點／不監控。"""
    mode_source = saved_modes if isinstance(saved_modes, dict) else {}
    scope_source = saved_scopes if isinstance(saved_scopes, dict) else {}
    result: dict[str, str] = {}
    for scope in TARGET_SCOPE_ORDER:
        if core_monitoring_only and scope != '自己':
            result[scope] = TARGET_DISPLAY_MODE_OFF
            continue
        raw_mode = mode_source.get(scope)
        if raw_mode in (*TARGET_DISPLAY_MODES, '自動監測', '自訂監測'):
            result[scope] = legacy_mode(normalize_mode(raw_mode, MODE_OFF))
            continue
        if migrate_legacy:
            enabled = bool(scope_source.get(scope, TARGET_SCOPE_DEFAULTS[scope]))
            result[scope] = TARGET_DISPLAY_MODE_FOCUSED if enabled else TARGET_DISPLAY_MODE_OFF
        else:
            result[scope] = TARGET_DISPLAY_MODE_DEFAULTS[scope]
    if core_monitoring_only:
        result['自己'] = TARGET_DISPLAY_MODE_ALL
    return result

def resolved_target_scope_settings(saved_scopes: object, *, core_monitoring_only: bool) -> dict[str, bool]:
    """套用新安裝預設，同時保留舊設定中已明確保存的選擇。"""
    defaults = CORE_TARGET_SCOPE_DEFAULTS if core_monitoring_only else TARGET_SCOPE_DEFAULTS
    source = saved_scopes if isinstance(saved_scopes, dict) else {}
    if core_monitoring_only:
        return {scope: scope == '自己' for scope in TARGET_SCOPE_ORDER}
    return {scope: bool(source.get(scope, defaults[scope])) for scope in TARGET_SCOPE_ORDER}

def resolved_target_sound_settings(saved_scopes: object, *, core_monitoring_only: bool) -> dict[str, bool]:
    """提示音採安全預設；公開核心模式永遠只有自己可以發聲。"""
    source = saved_scopes if isinstance(saved_scopes, dict) else {}
    if core_monitoring_only:
        return {scope: scope == '自己' for scope in TARGET_SCOPE_ORDER}
    return {scope: bool(source.get(scope, TARGET_SOUND_DEFAULTS[scope])) for scope in TARGET_SCOPE_ORDER}

def normalized_status_ids(values: object) -> set[int]:
    """把設定檔中的狀態 ID 整理成安全的整數集合。"""
    if not isinstance(values, (list, tuple, set)):
        return set()
    result: set[int] = set()
    for raw_value in values:
        if isinstance(raw_value, bool):
            continue
        try:
            status_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if 0 <= status_id <= 65535:
            result.add(status_id)
    return result

def resolved_target_scope_status_ids(saved_statuses: object, fallback_statuses: object, scope_settings: dict[str, bool], *, core_monitoring_only: bool) -> dict[str, set[int]]:
    """讀取人物類型狀態清單；舊版全域清單會複製到原本啟用的類型。"""
    source = saved_statuses if isinstance(saved_statuses, dict) else None
    legacy_ids = normalized_status_ids(fallback_statuses)
    result: dict[str, set[int]] = {}
    for scope in TARGET_SCOPE_ORDER:
        if core_monitoring_only and scope != '自己':
            result[scope] = set()
        elif source is not None:
            result[scope] = normalized_status_ids(source.get(scope, []))
        elif scope_settings.get(scope, False):
            result[scope] = set(legacy_ids)
        else:
            result[scope] = set()
    return result

def _extract_lua_brace_block(text: str, start: int) -> str:
    """取出 Lua 表格區塊；字串內的括號不算表格括號。"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ''

def _read_text_with_fallback(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ('utf-8', 'cp950', 'big5'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')

def _load_twro_status_names(*, prefer_runtime_index: bool=True) -> tuple[dict[int, str], dict[int, str], str]:
    """優先讀取打包前產生的精簡索引；缺檔時才回退解析 Lua。"""
    if prefer_runtime_index:
        runtime_path = APPLICATION_DIR / 'data' / 'runtime_status_index.json'
        try:
            payload = json.loads(runtime_path.read_text(encoding='utf-8'))
            if isinstance(payload, dict) and payload.get('schema_version') == 1 and (payload.get('game_id') == TWRO_GAME_ID):
                names = {int(raw_id): str(value).strip() for raw_id, value in dict(payload.get('names', {})).items() if str(value).strip()}
                groups = {int(raw_id): str(value).strip() for raw_id, value in dict(payload.get('groups', {})).items() if str(value).strip()}
                if names:
                    return (names, groups, '內建精簡狀態索引')
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    data_dirs = (APPLICATION_DIR / 'data',)
    for data_dir in data_dirs:
        efst_path = data_dir / 'EFSTIDs.lua'
        icon_path = data_dir / 'stateiconinfo.lua'
        if not efst_path.exists() or not icon_path.exists():
            continue
        try:
            efst_text = _read_text_with_fallback(efst_path)
            icon_text = _read_text_with_fallback(icon_path)
            efst_ids = {int(value): f'EFST_{name}' for name, value in re.findall('\\bEFST_([A-Za-z0-9_]+)\\s*=\\s*(\\d+)', efst_text)}
            descriptions: dict[str, list[str]] = {}
            categories: dict[str, str] = {}
            entry_pattern = re.compile('StateIconList\\[EFST_IDs\\.(EFST_[A-Za-z0-9_]+)\\]\\s*=\\s*\\{')
            for match in entry_pattern.finditer(icon_text):
                entry_block = _extract_lua_brace_block(icon_text, match.end() - 1)
                desc_match = re.search('descript\\s*=\\s*\\{', entry_block)
                if not desc_match:
                    continue
                desc_block = _extract_lua_brace_block(entry_block, desc_match.end() - 1)
                descriptions[match.group(1)] = [item.replace('\\n', '\n') for item in re.findall('\\{\\s*"([^"]*)"', desc_block)]
                color_match = re.search('\\b(COLOR_TITLE_BUFF|COLOR_TITLE_DEBUFF|COLOR_TITLE_TOGGLE)\\b', desc_block)
                if color_match:
                    categories[match.group(1)] = STATUS_GROUP_BY_COLOR[color_match.group(1)]
            display_names: dict[int, str] = {}
            display_groups: dict[int, str] = {}
            for status_id, efst_name in efst_ids.items():
                title = next((item.strip() for item in descriptions.get(efst_name, []) if item.strip() and item.strip() != '%s'), '')
                display_names[status_id] = title or efst_name
                if efst_name in categories:
                    display_groups[status_id] = categories[efst_name]
            if display_names:
                return (display_names, display_groups, '內建 Lua 狀態資料（備援）')
        except (OSError, UnicodeError, ValueError):
            continue
    return ({FOCUS_STATUS_ID: '經驗值倍增'}, {FOCUS_STATUS_ID: '主要監控'}, '內建焦點狀態')
EFST_NAMES, EFST_GROUPS, STATUS_DATA_SOURCE = _load_twro_status_names()
EFST_NAMES.update(ACTOR_STATE_STATUS_NAMES)
EFST_GROUPS.update({status_id: '減益' for status_id in ACTOR_STATE_STATUS_NAMES})
EFST_GROUPS[888] = '增益'
BUNDLED_EFST_NAMES = dict(EFST_NAMES)
BUNDLED_EFST_GROUPS = dict(EFST_GROUPS)
BUNDLED_STATUS_DATA_SOURCE = STATUS_DATA_SOURCE
EXTENDED_STATUS_CATEGORY_MAP = {'技能': '技能', '消耗品': '消耗品', 'BUFF': 'BUFF', 'DEBUFF': 'DEBUFF', '掉寶': '掉寶', '經驗': '經驗', '其他': '其他'}

def _load_confirmed_status_catalog() -> tuple[dict[int, str], dict[int, str], str]:
    """載入與程式放在一起的已確認擴充狀態資料。"""
    path = APPLICATION_DIR / 'data' / '可擴充資料庫.json'
    if not path.is_file():
        return ({}, {}, '未找到可擴充狀態資料')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return ({}, {}, '可擴充狀態資料讀取失敗')
    if not isinstance(payload, dict) or payload.get('schema_version') != 1 or (not bundled_catalog_is_twro(payload)):
        return ({}, {}, '可擴充狀態資料格式不支援')
    records = payload.get('records', {})
    status_records = records.get('statuses', {}) if isinstance(records, dict) else {}
    if not isinstance(status_records, dict):
        return ({}, {}, '可擴充狀態資料格式不支援')
    names: dict[int, str] = {}
    categories: dict[int, str] = {}
    for raw_id, record in status_records.items():
        if not isinstance(record, dict) or record.get('review_status') != 'confirmed':
            continue
        try:
            status_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(record.get('name', '')).strip()
        category = str(record.get('category', '')).strip()
        if not 0 <= status_id <= 65535 or not name or '�' in name:
            continue
        category_name = EXTENDED_STATUS_CATEGORY_MAP.get(category)
        if category_name is None:
            continue
        names[status_id] = name
        categories[status_id] = category_name
    return (names, categories, f'已載入可擴充狀態 {len(names)} 項')
EXTENDED_EFST_NAMES, EXTENDED_STATUS_LIBRARY_CATEGORIES, EXTENDED_STATUS_DATA_REPORT = _load_confirmed_status_catalog()

def _load_bundled_status_classification() -> tuple[dict[int, str], dict[int, str], dict[int, str], str]:
    """載入開發階段已由 RO 主程式核對的少量狀態分類。"""
    path = APPLICATION_DIR / 'data' / 'status_classification.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('schema_version') != 1 or (not bundled_catalog_is_twro(payload)):
            raise ValueError('schema_version')
        raw_records = payload.get('records', {})
        if not isinstance(raw_records, dict):
            raise ValueError('records')
        categories: dict[int, str] = {}
        subcategories: dict[int, str] = {}
        display_names: dict[int, str] = {}
        for raw_id, raw_record in raw_records.items():
            if not isinstance(raw_record, dict):
                continue
            if raw_record.get('review_status') != 'confirmed_from_client_data':
                continue
            try:
                status_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            category = EXTENDED_STATUS_CATEGORY_MAP.get(str(raw_record.get('category', '')).strip())
            subcategory = str(raw_record.get('consumable_subcategory', '')).strip()
            subcategory = CONSUMABLE_SUBCATEGORY_ALIASES.get(subcategory, subcategory)
            display_name = str(raw_record.get('name', '')).strip()
            if category not in STATUS_LIBRARY_CATEGORY_ORDER:
                continue
            categories[status_id] = category
            if display_name and '�' not in display_name:
                display_names[status_id] = display_name
            if category == '消耗品' and subcategory in CONSUMABLE_SUBCATEGORY_ORDER:
                subcategories[status_id] = subcategory
        return (display_names, categories, subcategories, f'已載入 RO 狀態分類 {len(categories)} 項')
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ({}, {}, {}, '內建 RO 狀態分類不可用')
CLIENT_STATUS_DISPLAY_NAMES, CLIENT_STATUS_LIBRARY_CATEGORIES, BUNDLED_CONSUMABLE_SUBCATEGORIES, BUNDLED_STATUS_CLASSIFICATION_REPORT = _load_bundled_status_classification()

def _load_bundled_runtime_status_catalog() -> tuple[dict[int, dict[str, object]], str, frozenset[int]]:
    """載入台版 RO 的精簡用途／來源索引；不含說明全文。"""
    path = APPLICATION_DIR / 'data' / 'status_catalog.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('schema_version') != 1 or (not bundled_catalog_is_twro(payload)):
            raise ValueError('schema')
        raw_records = payload.get('records', {})
        if not isinstance(raw_records, dict):
            raise ValueError('records')
        records: dict[int, dict[str, object]] = {}
        for raw_id, raw_record in raw_records.items():
            if not isinstance(raw_record, dict):
                continue
            try:
                status_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = str(raw_record.get('name', '')).strip()
            if not 0 <= status_id <= 65535 or not name or '�' in name:
                continue
            records[status_id] = {'name': name, 'has_skill_link': raw_record.get('has_skill_link') is True, 'display_name_override': str(raw_record.get('display_name_override', '')).strip(), 'aliases': tuple((str(value).strip() for value in raw_record.get('aliases', []) if str(value).strip())), 'functional_category': str(raw_record.get('functional_category', '其他／待確認')).strip(), 'item_names': tuple((str(value).strip() for value in raw_record.get('item_names', []) if str(value).strip())), 'source_tags': tuple((str(value).strip() for value in raw_record.get('source_tags', []) if str(value).strip()))}
        skill_status_ids = frozenset((value for value in payload.get('skill_status_ids', []) if isinstance(value, int) and 0 <= value <= 65535)) | frozenset((status_id for status_id, record in records.items() if record.get('has_skill_link') is True))
        return (records, f'已載入台版狀態用途索引 {len(records)} 項', skill_status_ids)
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return ({}, '台版狀態用途索引不可用', frozenset())
RUNTIME_STATUS_METADATA, RUNTIME_STATUS_CATALOG_REPORT, BUNDLED_SKILL_STATUS_IDS = _load_bundled_runtime_status_catalog()
BUNDLED_EFST_NAMES.update(CLIENT_STATUS_DISPLAY_NAMES)
BUNDLED_EFST_NAMES.update({status_id: str(record.get('name', '')).strip() for status_id, record in RUNTIME_STATUS_METADATA.items() if str(record.get('name', '')).strip()})
BUNDLED_EFST_NAMES.update(EXTENDED_EFST_NAMES)
REVIEWED_STATUS_DISPLAY_NAMES = {status_id: str(record['display_name_override']) for status_id, record in RUNTIME_STATUS_METADATA.items() if record.get('display_name_override')}
BUNDLED_EFST_NAMES.update(REVIEWED_STATUS_DISPLAY_NAMES)
BUNDLED_STATUS_LIBRARY_CATEGORIES = dict(CLIENT_STATUS_LIBRARY_CATEGORIES)
BUNDLED_STATUS_LIBRARY_CATEGORIES.update(EXTENDED_STATUS_LIBRARY_CATEGORIES)
BUNDLED_EFST_GROUPS.update({status_id: '增益' if category == 'BUFF' else '減益' for status_id, category in BUNDLED_STATUS_LIBRARY_CATEGORIES.items() if category in {'BUFF', 'DEBUFF'}})
STATUS_LIBRARY_CATEGORIES = BUNDLED_STATUS_LIBRARY_CATEGORIES

@dataclass(frozen=True)
class StatusDataResult:
    names: dict[int, str]
    groups: dict[int, str]
    source: str
    report: str
    signatures: list[dict[str, object]]

@dataclass(frozen=True)
class ClientCatalogResult:
    """物品／寵物的名稱與識別資料；不包含任何 RRF 或個人設定。"""
    item_names: dict[int, str]
    pet_names: dict[int, str]
    pet_food_item_ids: set[int]
    source: str
    report: str
    signatures: list[dict[str, object]]
    complete: bool = False

@dataclass(frozen=True)
class JobSkillCatalogResult:
    """隨程式攜帶的 RO 職業／技能索引；不在即時監控時掃描 GRF。"""
    jobs: tuple[dict[str, object], ...]
    status_links: dict[int, tuple[dict[str, object], ...]]
    source: str
    report: str

def _load_bundled_job_skill_catalog() -> JobSkillCatalogResult:
    """讀取由官方 RO data.grf 產生的職業技能索引。"""
    compact_path = APPLICATION_DIR / 'data' / 'job_status_index.json'
    full_path = APPLICATION_DIR / 'data' / 'job_skill_catalog.json'
    path = compact_path if compact_path.is_file() else full_path
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or payload.get('schema_version') not in (1, 2) or (not bundled_catalog_is_twro(payload)):
            raise ValueError('schema_version')
        schema_version = int(payload.get('schema_version', 1))
        raw_jobs = payload.get('jobs', [])
        if not isinstance(raw_jobs, list):
            raise ValueError('jobs')
        jobs: list[dict[str, object]] = []
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                continue
            try:
                job_id = int(raw_job.get('id'))
            except (TypeError, ValueError):
                continue
            name = str(raw_job.get('name_zh_tw') or raw_job.get('name', '')).strip()
            code = str(raw_job.get('code', '')).strip()
            raw_skills = raw_job.get('skills', [])
            if not name or not code or (not isinstance(raw_skills, list)):
                continue
            skills: list[dict[str, object]] = []
            for raw_skill in raw_skills:
                if not isinstance(raw_skill, dict):
                    continue
                try:
                    skill_id = int(raw_skill.get('id'))
                except (TypeError, ValueError):
                    continue
                skill_code = str(raw_skill.get('code', '')).strip()
                if skill_id <= 0 or not skill_code:
                    continue
                skill_name = str(raw_skill.get('name_zh_tw', '')).strip()
                status_ids = []
                for raw_status_id in raw_skill.get('status_ids', []):
                    try:
                        status_id = int(raw_status_id)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= status_id <= 65535:
                        status_ids.append(status_id)
                skills.append({'id': skill_id, 'code': skill_code, 'name': skill_name, 'inherited': bool(raw_skill.get('inherited', False)), 'status_ids': sorted(set(status_ids))})
            jobs.append({'id': job_id, 'name': name, 'code': code, 'parent_ids': tuple((int(value) for value in raw_job.get('parent_ids', []) if isinstance(value, (int, float)))), 'inherited_skill_ids': tuple((int(value) for value in raw_job.get('inherited_skill_ids', []) if isinstance(value, (int, float)))), 'skills': skills})
        status_links: dict[int, tuple[dict[str, object], ...]] = {}
        raw_links = payload.get('status_links', {})
        if isinstance(raw_links, dict):
            for raw_status_id, raw_records in raw_links.items():
                try:
                    status_id = int(raw_status_id)
                except (TypeError, ValueError):
                    continue
                if not 0 <= status_id <= 65535 or not isinstance(raw_records, list):
                    continue
                records: list[dict[str, object]] = []
                for raw_record in raw_records:
                    if not isinstance(raw_record, dict):
                        continue
                    job_name = str(raw_record.get('job_name', '')).strip()
                    skill_code = str(raw_record.get('skill_code', '')).strip()
                    skill_name = str(raw_record.get('skill_name', '')).strip()
                    if job_name and skill_code:
                        try:
                            job_id = int(raw_record.get('job_id', 0))
                            skill_id = int(raw_record.get('skill_id', 0))
                        except (TypeError, ValueError):
                            continue
                        records.append({'job_id': job_id, 'job_name': job_name, 'job_code': str(raw_record.get('job_code', '')), 'skill_id': skill_id, 'skill_code': skill_code, 'skill_name': skill_name, 'inherited': bool(raw_record.get('inherited', False)), 'mapping': str(raw_record.get('mapping', '')), 'needs_rrf_verification': bool(raw_record.get('needs_rrf_verification', True))})
                if records:
                    status_links[status_id] = tuple(records)
        if not jobs:
            raise ValueError('empty catalog')
        stats = payload.get('statistics', {})
        job_count = int(stats.get('job_count', len(jobs))) if isinstance(stats, dict) else len(jobs)
        skill_count = int(stats.get('unique_skill_count', stats.get('skill_count', 0))) if isinstance(stats, dict) else 0
        link_count = sum((len(records) for records in status_links.values()))
        return JobSkillCatalogResult(tuple(jobs), status_links, str(path), f'內建 RO 職業技能索引 v{schema_version}：{job_count} 個職業、{skill_count} 個技能、{link_count} 個職業狀態連結')
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return JobSkillCatalogResult(tuple(), {}, '內建職業技能索引不可用', '找不到內建 RO 職業技能索引；狀態仍可依原分類選取')
_BUNDLED_JOB_SKILL_CATALOG: JobSkillCatalogResult | None = None

def bundled_job_skill_catalog() -> JobSkillCatalogResult:
    """狀態選擇頁需要時才讀取職業技能索引。"""
    global _BUNDLED_JOB_SKILL_CATALOG
    if _BUNDLED_JOB_SKILL_CATALOG is None:
        _BUNDLED_JOB_SKILL_CATALOG = _load_bundled_job_skill_catalog()
    return _BUNDLED_JOB_SKILL_CATALOG

def release_bundled_job_skill_catalog() -> None:
    """離開狀態選擇頁後釋放可由內建 JSON 重建的職業索引。"""
    global _BUNDLED_JOB_SKILL_CATALOG
    _BUNDLED_JOB_SKILL_CATALOG = None

@dataclass(frozen=True)
class GrfEntry:
    path: str
    compressed_size: int
    aligned_size: int
    decompressed_size: int
    entry_type: int
    offset: int

class GrfError(RuntimeError):
    """GRF 索引或檔案內容無法讀取。"""

class GrfUnsupportedVersionError(GrfError):
    """區分已知舊格式與現行資料損毀，避免以錯誤文字判斷。"""

    def __init__(self, version: int, magic: bytes=b'') -> None:
        self.version = int(version)
        self.known_legacy_format = self.version == 258 and magic.startswith(b'Master of Magic')
        super().__init__(f'不支援的 GRF 版本 0x{self.version:03X}')

class GrfArchive:
    """只讀的 GRF 索引與單檔解壓器，支援目前 Event Horizon v0x300。"""
    HEADER_SIZE = 46
    EVENT_HORIZON_MAGIC = b'Event Horizon'

    def __init__(self, path: Path, progress_callback: Callable[[float, str], None] | None=None) -> None:
        self.path = path
        self.progress_callback = progress_callback
        self.version = 0
        self.magic = b''
        self.entries: dict[str, GrfEntry] = {}
        self._open_and_index()

    def _report_progress(self, value: float, message: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(max(0.0, min(1.0, value)), message)
        except Exception:
            pass

    @staticmethod
    def _read_u32(data: bytes, offset: int) -> int:
        if offset + 4 > len(data):
            raise GrfError('GRF 標頭不完整')
        return struct.unpack_from('<I', data, offset)[0]

    def _open_and_index(self) -> None:
        self._report_progress(0.0, f'讀取 {self.path.name} 的 GRF 索引')
        try:
            with self.path.open('rb') as handle:
                header = handle.read(self.HEADER_SIZE)
                if len(header) != self.HEADER_SIZE:
                    raise GrfError('GRF 標頭不完整')
                self.magic = header[:15]
                table_offset = self._read_u32(header, 30)
                self.version = self._read_u32(header, 42)
                if self.version >= 768 and self.magic.startswith(self.EVENT_HORIZON_MAGIC):
                    table_header_pos = self.HEADER_SIZE + table_offset + 4
                    record_size = 21
                    offset_size = 8
                elif self.version >= 512:
                    table_header_pos = self.HEADER_SIZE + table_offset
                    record_size = 17
                    offset_size = 4
                else:
                    if self.version == 258 and self.magic.startswith(b'Master of Magic'):
                        table_header_pos = self.HEADER_SIZE + table_offset
                        file_size = os.fstat(handle.fileno()).st_size
                        if table_header_pos > file_size or file_size - table_header_pos < 8:
                            raise GrfError('舊版 GRF 檔案表超出檔案範圍或標頭不完整')
                    raise GrfUnsupportedVersionError(self.version, self.magic)
                handle.seek(table_header_pos)
                table_header = handle.read(8)
                if len(table_header) != 8:
                    raise GrfError('GRF 檔案表標頭不完整')
                compressed_size, decompressed_size = struct.unpack('<II', table_header)
                file_size = self.path.stat().st_size
                if compressed_size <= 0 or decompressed_size <= 0 or compressed_size > file_size or (decompressed_size > 256 * 1024 * 1024):
                    raise GrfError('GRF 檔案表大小不合理')
                compressed_table = handle.read(compressed_size)
                if len(compressed_table) != compressed_size:
                    raise GrfError('GRF 檔案表讀取不完整')
        except OSError as exc:
            raise GrfError(f'讀取 GRF 失敗：{exc}') from exc
        self._report_progress(0.3, f'解壓 {self.path.name} 的檔案表')
        try:
            table = zlib.decompress(compressed_table)
        except zlib.error as exc:
            raise GrfError(f'GRF 檔案表解壓失敗：{exc}') from exc
        if len(table) != decompressed_size:
            raise GrfError('GRF 檔案表解壓大小不符')
        self._report_progress(0.35, f'建立 {self.path.name} 的檔案索引')
        cursor = 0
        file_size = self.path.stat().st_size
        progress_step = max(65536, len(table) // 100)
        last_progress_cursor = 0
        while cursor < len(table):
            nul = table.find(b'\x00', cursor)
            if nul < 0 or nul + 1 + record_size > len(table):
                raise GrfError('GRF 檔案表記錄格式不符')
            raw_path = table[cursor:nul]
            record = nul + 1
            compressed, aligned, original = struct.unpack_from('<III', table, record)
            entry_type = table[record + 12]
            if offset_size == 8:
                offset = struct.unpack_from('<Q', table, record + 13)[0]
            else:
                offset = struct.unpack_from('<I', table, record + 13)[0]
            if len(raw_path) > 1024 or compressed > file_size or aligned > file_size or (original > 512 * 1024 * 1024) or (aligned < compressed):
                raise GrfError('GRF 檔案表記錄數值不合理')
            normalized = self.normalize_path(raw_path.decode('latin1', errors='ignore'))
            if normalized:
                self.entries[normalized] = GrfEntry(normalized, compressed, aligned, original, entry_type, offset)
            cursor = record + record_size
            if cursor - last_progress_cursor >= progress_step:
                self._report_progress(0.35 + 0.65 * min(1.0, cursor / max(1, len(table))), f'建立 {self.path.name} 的檔案索引')
                last_progress_cursor = cursor
        self._report_progress(1.0, f'完成 {self.path.name} 索引')

    @staticmethod
    def normalize_path(value: str) -> str:
        return value.replace('/', '\\').strip('\\').casefold()

    def find(self, *candidate_paths: str) -> GrfEntry | None:
        for candidate in candidate_paths:
            entry = self.entries.get(self.normalize_path(candidate))
            if entry is not None:
                return entry
        return None

    def read_entry(self, entry: GrfEntry) -> bytes:
        absolute_offset = self.HEADER_SIZE + entry.offset
        try:
            with self.path.open('rb') as handle:
                handle.seek(absolute_offset)
                payload = handle.read(entry.aligned_size)
        except OSError as exc:
            raise GrfError(f'讀取 GRF 內容失敗：{exc}') from exc
        if len(payload) != entry.aligned_size:
            raise GrfError('GRF 內容讀取不完整')
        if not entry.entry_type & 1:
            return payload[:entry.decompressed_size]
        try:
            decoded = zlib.decompress(payload[:entry.compressed_size])
        except zlib.error as exc:
            if entry.entry_type & 2:
                raise GrfError(f'GRF 檔案使用加密格式，無法直接讀取：{entry.path}') from exc
            raise GrfError(f'GRF 檔案解壓失敗：{entry.path}') from exc
        if len(decoded) != entry.decompressed_size:
            raise GrfError(f'GRF 檔案解壓大小不符：{entry.path}')
        return decoded

class _LuaTable:

    def __init__(self) -> None:
        self.fields: dict[object, object] = {}
        self.array: dict[int, object] = {}

class _LuaSymbol:

    def __init__(self, name: str) -> None:
        self.name = name

def _lua_parse_chunk(data: bytes) -> dict[str, object]:
    """讀取 Lua 5.1 chunk 的常數、指令與巢狀 prototype。"""
    if len(data) < 12 or data[:4] != b'\x1bLua' or data[4] != 81:
        raise GrfError('狀態資料不是 Lua 5.1 bytecode')
    cursor = 12
    int_size = data[7]
    size_t_size = data[8]
    instruction_size = data[9]
    number_size = data[10]
    if data[6] != 1 or int_size not in (4, 8) or size_t_size not in (4, 8):
        raise GrfError('Lua bytecode 的位元組格式不支援')

    def read_uint() -> int:
        nonlocal cursor
        end = cursor + int_size
        if end > len(data):
            raise GrfError('Lua bytecode 截斷')
        value = int.from_bytes(data[cursor:end], 'little')
        cursor = end
        return value

    def read_size() -> int:
        nonlocal cursor
        end = cursor + size_t_size
        if end > len(data):
            raise GrfError('Lua bytecode 字串長度截斷')
        value = int.from_bytes(data[cursor:end], 'little')
        cursor = end
        return value

    def read_string() -> bytes | None:
        nonlocal cursor
        size = read_size()
        if size == 0:
            return None
        if size < 1 or cursor + size > len(data):
            raise GrfError(f'Lua bytecode 字串截斷：offset={cursor} size={size} total={len(data)}')
        value = data[cursor:cursor + size - 1]
        cursor += size
        return value

    def read_number() -> float:
        nonlocal cursor
        if number_size != 8 or cursor + number_size > len(data):
            raise GrfError('Lua byte碼數值格式不支援')
        value = struct.unpack_from('<d', data, cursor)[0]
        cursor += number_size
        return value

    def read_proto() -> dict[str, object]:
        nonlocal cursor
        source = read_string()
        read_uint()
        read_uint()
        if cursor + 4 > len(data):
            raise GrfError('Lua bytecode prototype 標頭截斷')
        cursor += 4
        code_count = read_uint()
        code: list[int] = []
        for _ in range(code_count):
            if cursor + instruction_size > len(data):
                raise GrfError('Lua bytecode 指令截斷')
            code.append(int.from_bytes(data[cursor:cursor + instruction_size], 'little'))
            cursor += instruction_size
        constant_count = read_uint()
        constants: list[object] = []
        for constant_index in range(constant_count):
            if cursor >= len(data):
                raise GrfError('Lua bytecode 常數截斷')
            constant_type = data[cursor]
            cursor += 1
            if constant_type == 0:
                constants.append(None)
            elif constant_type == 1:
                if cursor >= len(data):
                    raise GrfError('Lua bytecode 布林常數截斷')
                constants.append(bool(data[cursor]))
                cursor += 1
            elif constant_type == 3:
                constants.append(read_number())
            elif constant_type == 4:
                try:
                    constants.append(read_string())
                except GrfError as exc:
                    raise GrfError(f'{exc}（常數 {constant_index + 1}/{constant_count}）') from exc
            else:
                raise GrfError(f'Lua bytecode 常數類型不支援：{constant_type}')
        child_count = read_uint()
        children = [read_proto() for _ in range(child_count)]
        line_count = read_uint()
        line_bytes = line_count * int_size
        if cursor + line_bytes > len(data):
            raise GrfError('Lua bytecode 行號資料截斷')
        cursor += line_bytes
        local_count = read_uint()
        for _ in range(local_count):
            read_string()
            read_uint()
            read_uint()
        upvalue_count = read_uint()
        for _ in range(upvalue_count):
            read_string()
        return {'source': source, 'code': code, 'constants': constants, 'children': children}
    return read_proto()

def _lua_instruction_fields(instruction: int) -> tuple[int, int, int]:
    return (instruction >> 6 & 255, instruction >> 23 & 511, instruction >> 14 & 511)

def _lua_static_table(proto: dict[str, object], initial_globals: dict[str, object] | None=None, retained_root_keys: set[int] | None=None) -> dict[str, object]:
    """執行狀態資料中使用的靜態 table 建立指令，不執行任意 Lua 程式。"""
    code = proto.get('code', [])
    constants = proto.get('constants', [])
    if not isinstance(code, list) or not isinstance(constants, list):
        raise GrfError('Lua bytecode prototype 格式不符')
    registers: dict[int, object] = {}
    globals_map: dict[str, object] = dict(initial_globals or {})
    root_table: _LuaTable | None = None

    def constant(index: int) -> object:
        if index < 0 or index >= len(constants):
            return None
        return constants[index]

    def rk(value: int) -> object:
        if value & 256:
            return constant(value & 255)
        return registers.get(value)

    def global_name(index: int) -> str:
        value = constant(index)
        if isinstance(value, bytes):
            return value.decode('latin1')
        return str(value or '')

    def set_table(table: object, key: object, value: object) -> None:
        if isinstance(table, _LuaTable):
            if table is root_table and retained_root_keys is not None:
                integer_key = _lua_integer(key)
                if integer_key not in retained_root_keys:
                    return
            table.fields[key] = value
    for instruction in code:
        if not isinstance(instruction, int):
            continue
        opcode = instruction & 63
        a, b, c = _lua_instruction_fields(instruction)
        if opcode == 0:
            registers[a] = registers.get(b)
        elif opcode == 1:
            registers[a] = constant(instruction >> 14 & 262143)
        elif opcode == 5:
            name = global_name(instruction >> 14 & 262143)
            registers[a] = globals_map.get(name, _LuaSymbol(name))
        elif opcode == 6:
            table = registers.get(b)
            key = rk(c)
            registers[a] = table.fields.get(key) if isinstance(table, _LuaTable) else None
        elif opcode == 7:
            globals_map[global_name(instruction >> 14 & 262143)] = registers.get(a)
        elif opcode == 9:
            set_table(registers.get(a), rk(b), rk(c))
        elif opcode == 10:
            table = _LuaTable()
            registers[a] = table
            if root_table is None:
                root_table = table
        elif opcode == 34:
            table = registers.get(a)
            if isinstance(table, _LuaTable) and b > 0 and (c > 0):
                for index in range(1, b + 1):
                    table.array[(c - 1) * 50 + index] = registers.get(a + index)
    return globals_map

def _lua_text(value: bytes | None) -> str:
    if not value:
        return ''
    candidates: list[str] = []
    for encoding in ('utf-8', 'cp950', 'big5'):
        try:
            candidates.append(value.decode(encoding))
        except UnicodeDecodeError:
            continue
    if not candidates:
        return ''
    return min(candidates, key=lambda item: (item.count('�') * 10 + sum((1 for char in item if ord(char) < 32 and char not in '\n\r\t')), -sum((1 for char in item if '一' <= char <= '鿿')))).strip()

def _usable_status_text(value: str) -> bool:
    return bool(value) and '�' not in value and (not any((ord(char) < 32 for char in value)))

def _lua_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if value == number else None

def _lua_table_values(value: object) -> list[object]:
    if not isinstance(value, _LuaTable):
        return []
    return [*value.fields.values(), *value.array.values()]

def _parse_client_item_bytecode(data: bytes) -> dict[int, str]:
    """讀取 RO 客戶端 ItemDBNameTbl 的物品 ID／內部名稱對照。"""
    globals_map = _lua_static_table(_lua_parse_chunk(data))
    table = globals_map.get('ItemDBNameTbl') or globals_map.get('ItemDB_To_ItemID')
    if not isinstance(table, _LuaTable):
        raise GrfError('itemdbnametbl.lub 沒有找到物品資料表')
    result: dict[int, str] = {}
    for key, value in table.fields.items():
        item_id = _lua_integer(value)
        if not isinstance(key, bytes) or item_id is None or (not 0 < item_id <= 4294967295):
            continue
        name = key.decode('ascii', errors='ignore').strip()
        if name:
            result[item_id] = name
    if not result:
        raise GrfError('itemdbnametbl.lub 沒有可用的物品資料')
    return result

def _parse_client_job_table(data: bytes) -> _LuaTable:
    """讀取 petinfo.lub 可能使用的職業／寵物 ID 對照。"""
    globals_map = _lua_static_table(_lua_parse_chunk(data))
    table = globals_map.get('JTtbl')
    if not isinstance(table, _LuaTable):
        raise GrfError('jobidentity.lub 沒有找到 JTtbl')
    return table

def _parse_client_pet_bytecode(data: bytes, job_table: _LuaTable | None=None) -> tuple[dict[int, str], set[int]]:
    """讀取 RO 客戶端定義的寵物名稱與寵物食物 ID。"""
    initial_globals = {'jobtbl': job_table} if job_table is not None else None
    globals_map = _lua_static_table(_lua_parse_chunk(data), initial_globals)
    pet_names: dict[int, str] = {}
    pet_name_table = globals_map.get('PetNameTable')
    if isinstance(pet_name_table, _LuaTable):
        for key, value in pet_name_table.fields.items():
            pet_id = _lua_integer(key)
            if pet_id is None or not 0 < pet_id <= 4294967295 or (not isinstance(value, bytes)):
                continue
            name = _lua_text(value)
            if name:
                pet_names[pet_id] = name
    pet_food_ids: set[int] = set()
    food_table = globals_map.get('PetFoodTable')
    for value in _lua_table_values(food_table):
        item_id = _lua_integer(value)
        if item_id is not None and 0 < item_id <= 4294967295:
            pet_food_ids.add(item_id)
    return (pet_names, pet_food_ids)

def _load_bundled_client_catalog() -> ClientCatalogResult:
    """讀取隨程式發布的 RO 客戶端基線；讀取失敗仍保留最低限度的安全預設。"""
    path = APPLICATION_DIR / 'data' / 'client_catalog.json'
    fallback = {601: '蒼蠅翅膀', 12333: '神秘水晶', 23280: '新手專用蒼蠅翅膀'}
    try:
        with path.open('r', encoding='utf-8') as handle:
            payload = json.load(handle)
        if not bundled_catalog_is_twro(payload):
            raise ValueError('game_id')
        raw_items = payload.get('items', {})
        item_names: dict[int, str] = {}
        for key, value in dict(raw_items).items():
            if isinstance(value, dict):
                value = value.get('name', '')
            name = str(value).strip()
            if name:
                item_names[int(key)] = name
        pet_names = {int(key): str(value).strip() for key, value in dict(payload.get('pets', {})).items() if str(value).strip()}
        pet_food_ids = {int(value) for value in payload.get('pet_food_item_ids', []) if isinstance(value, int) and 0 < value <= 4294967295}
        return ClientCatalogResult(item_names, pet_names, pet_food_ids, str(path), f'內建客戶端資料庫：{len(item_names)} 個物品、{len(pet_names)} 個寵物', [])
    except (OSError, TypeError, ValueError):
        return ClientCatalogResult(fallback, {}, set(), '內建客戶端基線', '內建資料檔讀取失敗，沿用最低限度基線', [])

def _load_confirmed_client_catalog() -> tuple[dict[int, str], dict[int, str], set[int], str]:
    """載入可擴充資料庫中已確認的物品、寵物與寵物食物資料。"""
    path = APPLICATION_DIR / 'data' / '可擴充資料庫.json'
    if not path.is_file():
        return ({}, {}, set(), '未找到可擴充客戶端資料')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return ({}, {}, set(), '可擴充客戶端資料讀取失敗')
    if not isinstance(payload, dict) or payload.get('schema_version') != 1 or (not bundled_catalog_is_twro(payload)):
        return ({}, {}, set(), '可擴充客戶端資料格式不支援')
    records = payload.get('records', {})
    if not isinstance(records, dict):
        return ({}, {}, set(), '可擴充客戶端資料格式不支援')

    def confirmed_names(kind: str) -> dict[int, str]:
        raw_records = records.get(kind, {})
        if not isinstance(raw_records, dict):
            return {}
        result: dict[int, str] = {}
        for raw_id, record in raw_records.items():
            if not isinstance(record, dict) or record.get('review_status') != 'confirmed':
                continue
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = str(record.get('name', '')).strip()
            if 0 < item_id <= 4294967295 and name and ('�' not in name):
                result[item_id] = name
        return result
    items = confirmed_names('items')
    pets = confirmed_names('pets')
    raw_food_records = records.get('pet_food_item_ids', {})
    food_ids: set[int] = set()
    if isinstance(raw_food_records, dict):
        for raw_id, record in raw_food_records.items():
            if not isinstance(record, dict) or record.get('review_status') != 'confirmed':
                continue
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if 0 < item_id <= 4294967295:
                food_ids.add(item_id)
    count = len(items) + len(pets) + len(food_ids)
    return (items, pets, food_ids, f'已載入可擴充客戶端資料 {count} 項')
_bundled_client_catalog = _load_bundled_client_catalog()
EXTENDED_ITEM_NAMES, EXTENDED_PET_NAMES, EXTENDED_PET_FOOD_IDS, EXTENDED_CLIENT_DATA_REPORT = _load_confirmed_client_catalog()
BUNDLED_CLIENT_CATALOG = ClientCatalogResult({**_bundled_client_catalog.item_names, **EXTENDED_ITEM_NAMES}, {**_bundled_client_catalog.pet_names, **EXTENDED_PET_NAMES}, set(_bundled_client_catalog.pet_food_item_ids) | EXTENDED_PET_FOOD_IDS, _bundled_client_catalog.source, _bundled_client_catalog.report + (f'；{EXTENDED_CLIENT_DATA_REPORT}' if EXTENDED_ITEM_NAMES or EXTENDED_PET_NAMES or EXTENDED_PET_FOOD_IDS else ''), _bundled_client_catalog.signatures)
ITEM_NAMES_BY_ID = BUNDLED_CLIENT_CATALOG.item_names
PET_NAMES_BY_ID = BUNDLED_CLIENT_CATALOG.pet_names
PET_FOOD_ITEM_IDS = BUNDLED_CLIENT_CATALOG.pet_food_item_ids
CLIENT_CATALOG_SOURCE = BUNDLED_CLIENT_CATALOG.source

def _parse_client_status_bytecode(efst_data: bytes, icon_data: bytes) -> tuple[dict[int, str], dict[int, str], int]:
    efst_globals = _lua_static_table(_lua_parse_chunk(efst_data))
    efst_table = efst_globals.get('EFST_IDs')
    if not isinstance(efst_table, _LuaTable):
        raise GrfError('EFSTIDs.lub 沒有找到 EFST_IDs')
    names_by_id: dict[int, str] = {}
    name_by_id: dict[int, str] = {}
    for key, value in efst_table.fields.items():
        if isinstance(key, bytes) and key.startswith(b'EFST_') and isinstance(value, (int, float)):
            status_id = int(value)
            if 0 <= status_id <= 65535:
                efst_name = key.decode('ascii', errors='ignore')
                name_by_id[status_id] = efst_name
                fallback = BUNDLED_EFST_NAMES.get(status_id, '')
                names_by_id[status_id] = fallback if _usable_status_text(fallback) else efst_name
    icon_globals = _lua_static_table(icon_data and _lua_parse_chunk(icon_data), {'EFST_IDs': efst_table})
    icon_table = icon_globals.get('StateIconList')
    if not isinstance(icon_table, _LuaTable):
        raise GrfError('stateiconinfo.lub 沒有找到 StateIconList')
    color_groups = {id(icon_globals.get('COLOR_TITLE_BUFF')): '增益', id(icon_globals.get('COLOR_TITLE_DEBUFF')): '減益', id(icon_globals.get('COLOR_TITLE_TOGGLE')): '開關／特殊'}
    groups_by_id: dict[int, str] = {}
    for status_id, state_info in icon_table.fields.items():
        if not isinstance(status_id, (int, float)) or not isinstance(state_info, _LuaTable):
            continue
        status_id = int(status_id)
        if not 0 <= status_id <= 65535:
            continue
        descriptions = state_info.fields.get(b'descript')
        if not isinstance(descriptions, _LuaTable):
            continue
        first_title = ''
        group = ''
        for row in descriptions.array.values():
            if not isinstance(row, _LuaTable):
                continue
            for value in row.array.values():
                if isinstance(value, bytes):
                    text = _lua_text(value)
                    if text and text != '%s' and (not first_title):
                        first_title = text
                elif id(value) in color_groups:
                    group = color_groups[id(value)]
        if first_title and _usable_status_text(first_title):
            names_by_id[status_id] = first_title
        if group:
            groups_by_id[status_id] = group
    names_by_id[FOCUS_STATUS_ID] = '經驗值倍增'
    groups_by_id[FOCUS_STATUS_ID] = '主要監控'
    return (names_by_id, groups_by_id, len(icon_table.fields))

def _grf_signatures(ro_dir: Path) -> list[dict[str, object]]:
    signatures: list[dict[str, object]] = []
    try:
        paths = sorted(ro_dir.glob('*.grf'), key=lambda item: item.name.casefold())
    except OSError:
        return signatures
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        signatures.append({'name': path.name, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns})
    return signatures

def _client_system_paths(ro_dir: Path) -> list[Path]:
    """取得可用的 RO 明文資料檔；只記錄檔案簽章，不把檔案複製進程式。"""
    system_dir = ro_dir / 'System'
    paths: list[Path] = []
    for filename in ('iteminfo_new.lub', 'iteminfo.lub', 'iteminfo_f.lub'):
        path = system_dir / filename
        try:
            if path.is_file():
                paths.append(path)
        except OSError:
            continue
    return paths

def _client_data_signatures(ro_dir: Path, *, grf_signatures: list[dict[str, object]] | None=None) -> list[dict[str, object]]:
    """建立狀態、物品與寵物資料共用的變更簽章。"""
    shared_grf_signatures = _grf_signatures(ro_dir) if grf_signatures is None else grf_signatures
    signatures = [{'kind': 'grf', **signature} for signature in shared_grf_signatures]
    for path in _client_system_paths(ro_dir):
        try:
            info = path.stat()
        except OSError:
            continue
        signatures.append({'kind': 'system', 'name': str(path.relative_to(ro_dir)), 'size': info.st_size, 'mtime_ns': info.st_mtime_ns})
    return signatures

def _read_status_cache(cache_path: Path, ro_dir: Path, signatures: list[dict[str, object]]) -> StatusDataResult | None:
    try:
        with cache_path.open('r', encoding='utf-8') as handle:
            cache = json.load(handle)
        if cache.get('version') != 2 or cache.get('game_id') != TWRO_GAME_ID or cache.get('ro_dir') != str(ro_dir.resolve()) or (cache.get('signatures') != signatures):
            return None
        names = {int(key): str(value) for key, value in cache.get('names', {}).items()}
        groups = {int(key): str(value) for key, value in cache.get('groups', {}).items()}
        if not names:
            return None
        cached_report = str(cache.get('report', '已讀取 RO 狀態資料'))
        return StatusDataResult(names, groups, str(cache.get('source', 'RO 主程式快取')), f'已使用 RO 狀態資料快取：{cached_report}', signatures)
    except (OSError, ValueError, TypeError):
        return None

def _write_status_cache(cache_path: Path, ro_dir: Path, result: StatusDataResult, *, strict_errors: bool=False) -> None:
    payload = {'version': 2, 'game_id': TWRO_GAME_ID, 'ro_dir': str(ro_dir.resolve()), 'signatures': result.signatures, 'source': result.source, 'report': result.report, 'names': {str(key): value for key, value in result.names.items()}, 'groups': {str(key): value for key, value in result.groups.items()}}
    try:
        write_catalog_json_atomically(cache_path, payload, backup=True)
    except (OSError, TypeError, ValueError):
        if strict_errors:
            raise
        pass

def _client_grf_paths(ro_dir: Path) -> list[Path]:
    try:
        paths = [path for path in ro_dir.glob('*.grf') if path.is_file()]
    except OSError:
        return []
    return sorted(paths, key=lambda path: (0 if path.name.casefold() == 'data.grf' else 1, 0 if path.name.casefold() == 'data0.grf' else 1, path.name.casefold()))

def ro_install_ready(ro_dir: Path) -> bool:
    """確認指定路徑是可供校對的 RO 主程式資料夾。"""
    ready, _message = twro_install_message(ro_dir)
    return ready

def _bundled_client_catalog_result(report: str | None=None) -> ClientCatalogResult:
    return ClientCatalogResult(dict(BUNDLED_CLIENT_CATALOG.item_names), dict(BUNDLED_CLIENT_CATALOG.pet_names), set(BUNDLED_CLIENT_CATALOG.pet_food_item_ids), BUNDLED_CLIENT_CATALOG.source, report or BUNDLED_CLIENT_CATALOG.report, [])

def _read_client_catalog_cache(cache_path: Path, ro_dir: Path, signatures: list[dict[str, object]]) -> ClientCatalogResult | None:
    """只接受同一台電腦、同一個 RO 資料版本產生的快取。

    第 3 版淘汰名稱修正前的快取；原檔仍保留，啟動先用內建資料，
    下次校對才重新產生，不在監控時為了格式升級掃描 GRF。
    """
    try:
        with cache_path.open('r', encoding='utf-8') as handle:
            payload = json.load(handle)
        if payload.get('version') != 3 or payload.get('game_id') != TWRO_GAME_ID or payload.get('ro_dir') != str(ro_dir.resolve()) or (payload.get('signatures') != signatures):
            return None
        item_names = {int(key): str(value) for key, value in dict(payload.get('item_names', {})).items() if str(value).strip()}
        pet_names = {int(key): str(value) for key, value in dict(payload.get('pet_names', {})).items() if str(value).strip()}
        pet_food_ids = {int(value) for value in payload.get('pet_food_item_ids', []) if isinstance(value, int) and 0 < value <= 4294967295}
        if not item_names and (not pet_names) and (not pet_food_ids):
            return None
        return ClientCatalogResult(item_names, pet_names, pet_food_ids, str(payload.get('source', 'RO 主程式資料快取')), f"已使用 RO 物品／寵物資料快取：{payload.get('report', '資料未變更')}", signatures, bool(payload.get('complete', False)))
    except (OSError, TypeError, ValueError):
        return None

def _write_client_catalog_cache(cache_path: Path, ro_dir: Path, result: ClientCatalogResult, *, strict_errors: bool=False) -> None:
    payload = {'version': 3, 'game_id': TWRO_GAME_ID, 'ro_dir': str(ro_dir.resolve()), 'signatures': result.signatures, 'source': result.source, 'report': result.report, 'complete': bool(result.complete), 'item_names': {str(key): value for key, value in result.item_names.items()}, 'pet_names': {str(key): value for key, value in result.pet_names.items()}, 'pet_food_item_ids': sorted(result.pet_food_item_ids)}
    try:
        write_catalog_json_atomically(cache_path, payload, backup=True)
    except (OSError, TypeError, ValueError):
        if strict_errors:
            raise
        pass

def _read_iteminfo_names(path: Path, wanted_ids: set[int]) -> dict[int, str]:
    """靜態解析 iteminfo 表格的 identifiedDisplayName，不執行 Lua。"""
    if not wanted_ids:
        return {}
    wanted = {int(item_id) for item_id in wanted_ids if isinstance(item_id, int) and 0 < item_id <= 4294967295}
    if not wanted:
        return {}
    try:
        raw = path.read_bytes()
        globals_map = _lua_static_table(_lua_parse_chunk(raw), retained_root_keys=wanted)
    except (OSError, GrfError, ValueError):
        return {}
    result: dict[int, str] = {}
    item_table = globals_map.get('tbl') or globals_map.get('itemInfo')
    if not isinstance(item_table, _LuaTable):
        return result
    for raw_item_id, raw_record in item_table.fields.items():
        item_id = _lua_integer(raw_item_id)
        if item_id not in wanted or not isinstance(raw_record, _LuaTable):
            continue
        fields = {_lua_text(key) if isinstance(key, bytes) else str(key): value for key, value in raw_record.fields.items()}
        raw_name = fields.get('identifiedDisplayName') or fields.get('unidentifiedDisplayName')
        if not isinstance(raw_name, bytes):
            continue
        name = _lua_text(raw_name).strip()
        if _usable_status_text(name):
            result[item_id] = name
    return result

def load_client_catalog_data(ro_dir: Path, cache_path: Path, progress_callback: Callable[[float, str], None] | None=None, force: bool=False, wanted_item_ids: set[int] | None=None, allow_full_scan: bool=True, strict_errors: bool=False) -> ClientCatalogResult:
    """讀取 RO 物品／寵物資料；只有明確啟動校對時才會執行。"""

    def report_progress(value: float, message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(max(0.0, min(100.0, value)), message)
        except Exception:
            pass
    twro_ready, twro_message = twro_install_message(ro_dir)
    if not twro_ready:
        if strict_errors:
            raise GrfError(twro_message)
        report_progress(100, twro_message)
        return _bundled_client_catalog_result(f'未執行校對：{twro_message}')
    wanted = {item_id for item_id in wanted_item_ids or set() if isinstance(item_id, int)}
    report_progress(2, '檢查 RO 物品／寵物資料')
    signatures = _client_data_signatures(ro_dir)
    cached = _read_client_catalog_cache(cache_path, ro_dir, signatures)
    if cached is not None and (not force) and wanted.issubset(cached.item_names) and (cached.complete or not allow_full_scan):
        report_progress(100, '已使用物品／寵物資料快取')
        return cached
    base = cached or _bundled_client_catalog_result()
    item_names = dict(base.item_names)
    pet_names = dict(base.pet_names)
    pet_food_ids = set(base.pet_food_item_ids)
    source_files: list[str] = []
    errors: list[str] = []
    found_item_data = False
    found_pet_data = False
    legacy_archives: list[str] = []
    missing_wanted = wanted - set(item_names)
    if not ro_dir.is_dir():
        report_progress(100, '找不到 RO，沿用內建物品／寵物資料')
        return _bundled_client_catalog_result(f'RO 資料夾不存在，沿用內建物品／寵物資料：{ro_dir}')
    scan_grf = allow_full_scan and (force or cached is None or (not cached.complete))
    if scan_grf:
        grf_paths = _client_grf_paths(ro_dir)
        total_grf = max(1, len(grf_paths))
        for index, grf_path in enumerate(grf_paths):
            report_progress(5 + 65 * (index / total_grf), f'讀取 {grf_path.name} 的物品／寵物資料')
            try:
                archive = GrfArchive(grf_path)
                item_entry = archive.find('data\\luafiles514\\lua files\\itemdbnametbl.lub', 'data\\lua files\\itemdbnametbl.lub')
                pet_entry = archive.find('data\\luafiles514\\lua files\\datainfo\\petinfo.lub', 'data\\lua files\\datainfo\\petinfo.lub')
                job_entry = archive.find('data\\luafiles514\\lua files\\datainfo\\jobidentity.lub', 'data\\lua files\\datainfo\\jobidentity.lub')
                job_table = None
                if job_entry is not None:
                    try:
                        job_table = _parse_client_job_table(archive.read_entry(job_entry))
                    except (GrfError, OSError, ValueError) as exc:
                        errors.append(f'{grf_path.name}：{exc}')
                if item_entry is not None:
                    parsed_items = _parse_client_item_bytecode(archive.read_entry(item_entry))
                    found_item_data = found_item_data or bool(parsed_items)
                    item_names.update(parsed_items)
                    if grf_path.name not in source_files:
                        source_files.append(grf_path.name)
                if pet_entry is not None:
                    parsed_pets, parsed_food_ids = _parse_client_pet_bytecode(archive.read_entry(pet_entry), job_table)
                    found_pet_data = found_pet_data or bool(parsed_pets)
                    pet_names.update(parsed_pets)
                    pet_food_ids.update(parsed_food_ids)
                    if grf_path.name not in source_files:
                        source_files.append(grf_path.name)
            except GrfUnsupportedVersionError as exc:
                if exc.known_legacy_format:
                    legacy_archives.append(grf_path.name)
                else:
                    errors.append(f'{grf_path.name}：{exc}')
            except (GrfError, OSError, ValueError) as exc:
                errors.append(f'{grf_path.name}：{exc}')
            report_progress(5 + 65 * ((index + 1) / total_grf), f'完成 {grf_path.name}')
    if strict_errors and scan_grf:
        if errors:
            raise GrfError('RO 物品／寵物資料檢查未完成：' + errors[0])
        if not found_item_data or not found_pet_data:
            raise GrfError('RO 物品／寵物資料不完整，保留原有快取')
    missing_wanted = wanted - set(item_names)
    iteminfo_ids = set(item_names) | missing_wanted if scan_grf else missing_wanted
    for path in _client_system_paths(ro_dir):
        if not iteminfo_ids:
            break
        parsed = _read_iteminfo_names(path, iteminfo_ids)
        if parsed:
            item_names.update(parsed)
            iteminfo_ids -= set(parsed)
            source_files.append(str(path.relative_to(ro_dir)))
        report_progress(75, f'比對 {path.name} 的物品顯示名稱')
    missing_wanted = wanted - set(item_names)
    if item_names or pet_names or pet_food_ids:
        source_label = ', '.join(dict.fromkeys(source_files)) or '內建基線'
        report = f'已建立 RO 客戶端資料：{len(item_names)} 個物品、{len(pet_names)} 個寵物、{len(pet_food_ids)} 個寵物食物 ID'
        if missing_wanted:
            report += f'；仍有 {len(missing_wanted)} 個未知物品待下次錄影確認'
        if legacy_archives:
            report += '；略過舊格式附屬資料 ' + ', '.join(legacy_archives)
        result = ClientCatalogResult(item_names, pet_names, pet_food_ids, f'RO 主程式資料（{source_label}）', report, signatures, bool(scan_grf or (cached is not None and cached.complete)))
        _write_client_catalog_cache(cache_path, ro_dir, result)
        report_progress(100, 'RO 物品／寵物資料完成')
        return result
    reason = errors[0] if errors else '找不到 itemdbnametbl.lub 或 petinfo.lub'
    report_progress(100, '校對未完成，沿用內建物品／寵物資料')
    return _bundled_client_catalog_result(f'RO 物品／寵物資料讀取失敗，沿用內建資料：{reason}')

def load_client_status_data(ro_dir: Path, cache_path: Path, progress_callback: Callable[[float, str], None] | None=None, force: bool=False, allow_full_scan: bool=True, strict_errors: bool=False) -> StatusDataResult:
    """從 RO 主程式資料夾讀取狀態資料；只有明確啟動校對時才會執行。"""

    def report_progress(value: float, message: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(max(0.0, min(100.0, value)), message)
        except Exception:
            pass
    twro_ready, twro_message = twro_install_message(ro_dir)
    if not twro_ready:
        if strict_errors:
            raise GrfError(twro_message)
        report_progress(100, twro_message)
        return StatusDataResult(dict(BUNDLED_EFST_NAMES), dict(BUNDLED_EFST_GROUPS), BUNDLED_STATUS_DATA_SOURCE, f'未執行校對：{twro_message}', [])
    report_progress(2, '檢查 RO 主程式資料夾')
    signatures = _grf_signatures(ro_dir)
    if not force:
        cached = _read_status_cache(cache_path, ro_dir, signatures)
        if cached is not None:
            report_progress(100, '已使用狀態資料快取')
            return cached
    if not ro_dir.is_dir():
        report_progress(100, '找不到 RO 主程式資料夾，沿用內建資料')
        return StatusDataResult(dict(BUNDLED_EFST_NAMES), dict(BUNDLED_EFST_GROUPS), BUNDLED_STATUS_DATA_SOURCE, f'RO 資料夾不存在，沿用內建狀態資料：{ro_dir}', signatures)
    if not allow_full_scan:
        report_progress(100, '非完整校對不執行大型 GRF 掃描，沿用內建狀態資料')
        return StatusDataResult(dict(BUNDLED_EFST_NAMES), dict(BUNDLED_EFST_GROUPS), BUNDLED_STATUS_DATA_SOURCE, '非完整校對略過大型 GRF 掃描，沿用目前內建狀態資料；需要完整更新時請手動校對', signatures)
    efst_data: bytes | None = None
    icon_data: bytes | None = None
    source_files: list[str] = []
    errors: list[str] = []
    legacy_archives: list[str] = []
    grf_paths = _client_grf_paths(ro_dir)
    total_grf = max(1, len(grf_paths))
    for grf_index, grf_path in enumerate(grf_paths):
        base_progress = 5 + 80 * (grf_index / total_grf)
        report_progress(base_progress, f'開始校對 {grf_path.name}')

        def archive_progress(local_value: float, message: str) -> None:
            report_progress(base_progress + 80 * (local_value / total_grf), message)
        try:
            archive = GrfArchive(grf_path, progress_callback=archive_progress)
            efst_entry = archive.find('data\\luafiles514\\lua files\\stateicon\\efstids.lub', 'data\\lua files\\stateicon\\efstids.lub')
            icon_entry = archive.find('data\\luafiles514\\lua files\\stateicon\\stateiconinfo.lub', 'data\\lua files\\stateicon\\stateiconinfo.lub')
            if efst_entry is not None:
                efst_data = archive.read_entry(efst_entry)
                source_files.append(grf_path.name)
            if icon_entry is not None:
                icon_data = archive.read_entry(icon_entry)
                if grf_path.name not in source_files:
                    source_files.append(grf_path.name)
            if efst_data is not None and icon_data is not None:
                pass
        except GrfUnsupportedVersionError as exc:
            if exc.known_legacy_format:
                legacy_archives.append(grf_path.name)
            else:
                errors.append(f'{grf_path.name}：{exc}')
        except (GrfError, OSError, ValueError) as exc:
            errors.append(f'{grf_path.name}：{exc}')
        report_progress(5 + 80 * ((grf_index + 1) / total_grf), f'完成 {grf_path.name} 校對')
    if strict_errors and errors:
        raise GrfError('RO 狀態資料檢查未完成：' + errors[0])
    if efst_data is not None and icon_data is not None:
        try:
            report_progress(90, '解析 EFSTIDs 與狀態圖示資料')
            names, groups, icon_count = _parse_client_status_bytecode(efst_data, icon_data)
            if strict_errors and (not names or not icon_count):
                raise GrfError('RO 狀態資料為空，保留原有快取')
            source = f"RO 主程式資料（{', '.join(source_files)}）"
            report = f'已讀取 RO 狀態資料：{len(names)} 個 EFST、{icon_count} 個圖示定義'
            if legacy_archives:
                report += '；略過舊格式附屬資料 ' + ', '.join(legacy_archives)
            result = StatusDataResult(names, groups, source, report, signatures)
            _write_status_cache(cache_path, ro_dir, result)
            report_progress(100, 'RO 狀態資料校對完成')
            return result
        except GrfError as exc:
            errors.append(str(exc))
    reason = errors[0] if errors else '找不到 EFSTIDs.lub 或 stateiconinfo.lub'
    if strict_errors:
        raise GrfError('RO 狀態資料檢查未完成：' + reason)
    report_progress(100, '校對未完成，沿用內建狀態資料')
    return StatusDataResult(dict(BUNDLED_EFST_NAMES), dict(BUNDLED_EFST_GROUPS), BUNDLED_STATUS_DATA_SOURCE, f'RO 狀態資料讀取失敗，沿用內建資料：{reason}', signatures)

def set_status_data(names: dict[int, str], groups: dict[int, str], source: str) -> None:
    global EFST_NAMES, EFST_GROUPS, STATUS_DATA_SOURCE, STATUS_LIBRARY_CATEGORIES
    EFST_NAMES = dict(BUNDLED_EFST_NAMES)
    EFST_NAMES.update(names)
    EFST_NAMES.update(REVIEWED_STATUS_DISPLAY_NAMES)
    EFST_NAMES[FOCUS_STATUS_ID] = '經驗值倍增'
    EFST_GROUPS = dict(BUNDLED_EFST_GROUPS)
    EFST_GROUPS.update(groups)
    EFST_GROUPS[FOCUS_STATUS_ID] = '主要監控'
    STATUS_LIBRARY_CATEGORIES = BUNDLED_STATUS_LIBRARY_CATEGORIES
    STATUS_DATA_SOURCE = source

def set_client_catalog(result: ClientCatalogResult) -> None:
    global ITEM_NAMES_BY_ID, PET_NAMES_BY_ID, PET_FOOD_ITEM_IDS, CLIENT_CATALOG_SOURCE
    if result is BUNDLED_CLIENT_CATALOG:
        ITEM_NAMES_BY_ID = BUNDLED_CLIENT_CATALOG.item_names
        PET_NAMES_BY_ID = BUNDLED_CLIENT_CATALOG.pet_names
        PET_FOOD_ITEM_IDS = BUNDLED_CLIENT_CATALOG.pet_food_item_ids
    else:
        missing_bundled_items = set(BUNDLED_CLIENT_CATALOG.item_names) - set(result.item_names)
        if missing_bundled_items:
            ITEM_NAMES_BY_ID = dict(BUNDLED_CLIENT_CATALOG.item_names)
            ITEM_NAMES_BY_ID.update(result.item_names)
        else:
            ITEM_NAMES_BY_ID = result.item_names
        missing_bundled_pets = set(BUNDLED_CLIENT_CATALOG.pet_names) - set(result.pet_names)
        if missing_bundled_pets:
            PET_NAMES_BY_ID = dict(BUNDLED_CLIENT_CATALOG.pet_names)
            PET_NAMES_BY_ID.update(result.pet_names)
        else:
            PET_NAMES_BY_ID = result.pet_names
        if BUNDLED_CLIENT_CATALOG.pet_food_item_ids.issubset(result.pet_food_item_ids):
            PET_FOOD_ITEM_IDS = result.pet_food_item_ids
        else:
            PET_FOOD_ITEM_IDS = set(BUNDLED_CLIENT_CATALOG.pet_food_item_ids)
            PET_FOOD_ITEM_IDS.update(result.pet_food_item_ids)
    CLIENT_CATALOG_SOURCE = result.source

def merge_client_catalog_delta(result: ClientCatalogResult) -> None:
    """合併定向辨識的小量結果，不重建完整 5,000 筆物品字典。"""
    global ITEM_NAMES_BY_ID, PET_NAMES_BY_ID, PET_FOOD_ITEM_IDS, CLIENT_CATALOG_SOURCE
    if ITEM_NAMES_BY_ID is BUNDLED_CLIENT_CATALOG.item_names and result.item_names:
        ITEM_NAMES_BY_ID = dict(ITEM_NAMES_BY_ID)
    if PET_NAMES_BY_ID is BUNDLED_CLIENT_CATALOG.pet_names and result.pet_names:
        PET_NAMES_BY_ID = dict(PET_NAMES_BY_ID)
    if PET_FOOD_ITEM_IDS is BUNDLED_CLIENT_CATALOG.pet_food_item_ids and result.pet_food_item_ids:
        PET_FOOD_ITEM_IDS = set(PET_FOOD_ITEM_IDS)
    ITEM_NAMES_BY_ID.update(result.item_names)
    PET_NAMES_BY_ID.update(result.pet_names)
    PET_FOOD_ITEM_IDS.update(result.pet_food_item_ids)
    CLIENT_CATALOG_SOURCE = result.source

def merge_status_data_delta(result: StatusDataResult) -> None:
    """合併定向辨識的小量狀態名稱，不複製完整狀態表。"""
    global STATUS_DATA_SOURCE
    EFST_NAMES.update(result.names)
    EFST_NAMES.update(REVIEWED_STATUS_DISPLAY_NAMES)
    EFST_GROUPS.update(result.groups)
    STATUS_DATA_SOURCE = result.source

def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    """開發資料庫使用原子替換，寫入失敗時不破壞原檔。"""
    temp_path = path.with_name(path.name + '.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp_path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise

def _status_result_payload(result: StatusDataResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {'names': {str(key): value for key, value in result.names.items()}, 'groups': {str(key): value for key, value in result.groups.items()}, 'source': result.source, 'report': result.report, 'signatures': result.signatures}

def _client_result_payload(result: ClientCatalogResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {'item_names': {str(key): value for key, value in result.item_names.items()}, 'pet_names': {str(key): value for key, value in result.pet_names.items()}, 'pet_food_item_ids': sorted(result.pet_food_item_ids), 'source': result.source, 'report': result.report, 'signatures': result.signatures, 'complete': bool(result.complete)}

def _status_result_from_payload(payload: object) -> StatusDataResult | None:
    if not isinstance(payload, dict):
        return None
    return StatusDataResult({int(key): str(value) for key, value in dict(payload.get('names', {})).items() if str(value).strip()}, {int(key): str(value) for key, value in dict(payload.get('groups', {})).items() if str(value).strip()}, str(payload.get('source', 'RO 主程式資料')), str(payload.get('report', '資料處理完成')), list(payload.get('signatures', [])))

def _client_result_from_payload(payload: object) -> ClientCatalogResult | None:
    if not isinstance(payload, dict):
        return None
    return ClientCatalogResult({int(key): str(value) for key, value in dict(payload.get('item_names', {})).items() if str(value).strip()}, {int(key): str(value) for key, value in dict(payload.get('pet_names', {})).items() if str(value).strip()}, {int(value) for value in payload.get('pet_food_item_ids', []) if isinstance(value, int) and 0 < value <= 4294967295}, str(payload.get('source', 'RO 主程式資料')), str(payload.get('report', '資料處理完成')), list(payload.get('signatures', [])), bool(payload.get('complete', False)))

def run_catalog_worker(request_path: Path, result_path: Path, progress_path: Path) -> int:
    """在短期子程序處理 RO 資料。

    大型 GRF 索引和 Lua 剖析物件不進入監控器主程序，子程序結束後由
    Windows 一次回收。這個入口不會建立 Tk 視窗。
    """

    def progress(value: float, message: str) -> None:
        _write_json_atomically(progress_path, {'value': max(0.0, min(100.0, float(value))), 'message': str(message)})
    try:
        request = json.loads(request_path.read_text(encoding='utf-8'))
        if not isinstance(request, dict):
            raise ValueError('校對請求格式錯誤')
        mode = str(request.get('mode', ''))
        ro_dir = Path(str(request.get('ro_dir', '')))
        status_cache_path = Path(str(request.get('status_cache_path', '')))
        client_cache_path = Path(str(request.get('client_cache_path', '')))
        status_result: StatusDataResult | None = None
        client_result: ClientCatalogResult | None = None
        if mode == 'full':
            force = bool(request.get('force', True))
            allow_full_scan = bool(request.get('allow_full_scan', True))
            wanted_item_ids = {int(value) for value in request.get('wanted_item_ids', []) if isinstance(value, int) and 0 < value <= 4294967295}
            work_status_cache = result_path.parent / 'status_cache.work.json'
            work_client_cache = result_path.parent / 'client_cache.work.json'
            status_signatures = _grf_signatures(ro_dir)
            client_signatures = _client_data_signatures(ro_dir, grf_signatures=status_signatures)
            status_result = None if force else _read_status_cache(status_cache_path, ro_dir, status_signatures)
            client_result = None if force else _read_client_catalog_cache(client_cache_path, ro_dir, client_signatures)
            if client_result is not None and (allow_full_scan and (not client_result.complete) or not wanted_item_ids.issubset(client_result.item_names)):
                client_result = None
            rebuilt_status = status_result is None
            rebuilt_client = client_result is None
            if rebuilt_status:
                status_result = load_client_status_data(ro_dir, work_status_cache, progress_callback=lambda value, message: progress(value * 0.55, message), force=True, allow_full_scan=allow_full_scan, strict_errors=allow_full_scan)
            else:
                progress(55, 'RO 狀態資料未變更，沿用有效快取')
            if rebuilt_client:
                client_result = load_client_catalog_data(ro_dir, work_client_cache, progress_callback=lambda value, message: progress(55 + value * 0.45, message), force=True, wanted_item_ids=wanted_item_ids, allow_full_scan=allow_full_scan, strict_errors=allow_full_scan)
            else:
                progress(98, 'RO 物品與寵物資料未變更，沿用有效快取')
            replacements: list[tuple[Path, Path]] = []
            if rebuilt_status and status_result is not None:
                _write_status_cache(work_status_cache, ro_dir, status_result, strict_errors=True)
                replacements.append((work_status_cache, status_cache_path))
            if rebuilt_client and client_result is not None:
                _write_client_catalog_cache(work_client_cache, ro_dir, client_result, strict_errors=True)
                replacements.append((work_client_cache, client_cache_path))
            commit_catalog_cache_files(replacements)
        elif mode == 'unknown':
            status_ids = {int(value) for value in request.get('status_ids', []) if isinstance(value, int) and 0 <= value <= 65535}
            item_ids = {int(value) for value in request.get('item_ids', []) if isinstance(value, int) and 0 < value <= 4294967295}
            progress(8, f'準備比對 {len(status_ids) + len(item_ids)} 筆未知資料')
            status_signatures = _grf_signatures(ro_dir)
            cached_status = _read_status_cache(status_cache_path, ro_dir, status_signatures)
            if cached_status is None and status_ids and bool(request.get('build_status_index_if_missing', False)):
                progress(12, '首次建立 RO 狀態索引；監控持續執行')
                attempted_status = load_client_status_data(ro_dir, status_cache_path, progress_callback=lambda value, message: progress(12 + value * 0.32, message), force=False, allow_full_scan=True)
                cached_status = _read_status_cache(status_cache_path, ro_dir, status_signatures)
                if cached_status is None:
                    raise RuntimeError('RO 狀態索引未建立：' + attempted_status.report)
            if cached_status is not None:
                status_result = StatusDataResult({status_id: cached_status.names[status_id] for status_id in status_ids if status_id in cached_status.names}, {status_id: cached_status.groups[status_id] for status_id in status_ids if status_id in cached_status.groups}, cached_status.source, cached_status.report, cached_status.signatures)
            progress(45, '已比對狀態名稱')
            if item_ids:
                loaded_client = load_client_catalog_data(ro_dir, client_cache_path, progress_callback=lambda value, message: progress(45 + value * 0.5, message), force=False, wanted_item_ids=item_ids, allow_full_scan=False)
                client_result = ClientCatalogResult({item_id: loaded_client.item_names[item_id] for item_id in item_ids if item_id in loaded_client.item_names}, {}, set(), loaded_client.source, loaded_client.report, loaded_client.signatures, False)
            progress(98, '整理比對結果')
        else:
            raise ValueError(f"不支援的校對模式：{mode or '未指定'}")
        _write_json_atomically(result_path, {'ok': True, 'mode': mode, 'status_result': _status_result_payload(status_result), 'client_result': _client_result_payload(client_result)})
        progress(100, 'RO 資料處理完成')
        return 0
    except BaseException as exc:
        try:
            _write_json_atomically(result_path, {'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
            progress(100, 'RO 資料處理失敗')
        except Exception:
            pass
        return 1

def promote_verified_unknowns_to_bundled_data(status_ids: set[int], item_ids: set[int], status_result: StatusDataResult | None, client_result: ClientCatalogResult | None) -> tuple[int, int]:
    """內建資料保持唯讀；此操作不寫入發布資料。"""
    return (0, 0)

def player_facing_status_name(value: str) -> str:
    """一般畫面保留中文主名；尾端英文括號仍留作搜尋別名。"""
    text = str(value).strip()
    match = re.match('^(.*?)\\s*[（(]([A-Za-z][^（）()]*)[）)]\\s*$', text)
    return match.group(1).strip() if match and match.group(1).strip() else text

def status_name(status_id: int, source_header: int | None=None) -> str:
    if source_header == 406:
        return f'伺服器狀態（0x{status_id:04X}）'
    return player_facing_status_name(EFST_NAMES.get(status_id, f'未確認狀態（0x{status_id:04X}）'))

def is_readable_status_name(status_id: int) -> bool:
    """判斷狀態是否適合直接顯示給一般使用者選取。

    EFST_* 是資料庫只有常數名稱、沒有遊戲內可讀標題的項目；這些 ID
    仍然保留在解析與校對流程，但不應混進玩家要勾選的清單。
    """
    name = str(EFST_NAMES.get(status_id, '')).strip()
    if not name:
        return False
    technical_prefixes = ('EFST_', 'STATE_', 'STATUS_', 'SC_', '未確認狀態', '伺服器狀態')
    if name.upper().startswith(tuple((prefix.upper() for prefix in technical_prefixes))):
        return False
    if len(name) >= 3 and re.fullmatch('[A-Z][A-Z0-9_]*', name):
        return False
    return True

def status_group(status_id: int, source_header: int | None=None) -> str:
    if status_id == FOCUS_STATUS_ID:
        return '主要監控'
    if source_header == 406:
        return '伺服器狀態'
    return EFST_GROUPS.get(status_id, '未分類')

def status_library_category(status_id: int, source_header: int | None=None) -> str:
    """將狀態整理成使用者容易選取的分類。

    RRF 狀態事件沒有可靠的技能／物品／NPC 來源欄位，因此「消耗品」與
    「技能」只代表目前有可靠職業技能連結或資料類別，不把
    來源推定成確定事實。
    """
    if status_id in STATUS_LIBRARY_CATEGORIES:
        return STATUS_LIBRARY_CATEGORIES[status_id]
    name = status_name(status_id, source_header)
    searchable = f"{name} {EFST_NAMES.get(status_id, '')}".casefold()
    experience_code_match = re.search('(?<![a-z])exp(?:up|memory|drop|bonus|increase)?(?![a-z])', searchable)
    if status_id == FOCUS_STATUS_ID or any((hint.casefold() in searchable for hint in STATUS_LIBRARY_NAME_HINTS['經驗'])) or experience_code_match:
        return '經驗'
    if any((hint.casefold() in searchable for hint in STATUS_LIBRARY_NAME_HINTS['掉寶'])):
        return '掉寶'
    if status_id in BUNDLED_SKILL_STATUS_IDS:
        return '技能'
    group = status_group(status_id, source_header)
    if group == '增益':
        return 'BUFF'
    if group == '減益':
        return 'DEBUFF'
    if group in {'主要監控', '開關／特殊'}:
        return '技能'
    if is_readable_status_name(status_id) and (not name.startswith(('未確認狀態', '伺服器狀態'))):
        return '其他'
    return '其他'

def status_job_links(status_id: int) -> tuple[dict[str, object], ...]:
    """回傳官方索引中的職業技能連結；沒有直接可靠連結時回傳空值。"""
    return bundled_job_skill_catalog().status_links.get(status_id, ())

def status_job_names(status_id: int) -> tuple[str, ...]:
    names = {str(record.get('job_name', '')).strip() for record in status_job_links(status_id) if str(record.get('job_name', '')).strip()}
    return tuple(sorted(names, key=str.casefold))

def status_skill_names(status_id: int) -> tuple[str, ...]:
    """回傳狀態可靠連結到的官方繁中技能名稱。"""
    names = {str(record.get('skill_name', '')).strip() for record in status_job_links(status_id) if str(record.get('skill_name', '')).strip()}
    return tuple(sorted(names, key=str.casefold))

def job_filter_entries() -> tuple[tuple[str, str], ...]:
    """產生依共同一轉職業聚集的玩家下拉選單。"""
    jobs = tuple(bundled_job_skill_catalog().jobs)
    by_id = {int(job.get('id', -1)): job for job in jobs if isinstance(job, dict) and str(job.get('name', '')).strip()}

    def lineage_root(job_id: int, visiting: set[int] | None=None) -> int:
        visiting = set(visiting or ())
        if job_id in visiting:
            return job_id
        visiting.add(job_id)
        job = by_id.get(job_id, {})
        known_parents = sorted((int(parent_id) for parent_id in job.get('parent_ids', []) if isinstance(parent_id, int) and int(parent_id) != 0 and (int(parent_id) in by_id)))
        if not known_parents:
            return job_id
        return min((lineage_root(parent_id, visiting) for parent_id in known_parents))

    def lineage_depth(job_id: int, visiting: set[int] | None=None) -> int:
        visiting = set(visiting or ())
        if job_id in visiting:
            return 0
        visiting.add(job_id)
        job = by_id.get(job_id, {})
        known_parents = [int(parent_id) for parent_id in job.get('parent_ids', []) if isinstance(parent_id, int) and int(parent_id) != 0 and (int(parent_id) in by_id)]
        return 0 if not known_parents else 1 + max((lineage_depth(parent_id, visiting) for parent_id in known_parents))
    entries: list[tuple[str, str, int, str]] = []
    for job in by_id.values():
        name = str(job.get('name', '')).strip()
        job_id = int(job.get('id', -1))
        root_id = lineage_root(job_id)
        root_name = str(by_id.get(root_id, {}).get('name', name)).strip() or name
        label = name if name == root_name else f'{root_name}系｜{name}'
        entries.append((root_name, name, lineage_depth(job_id), label))
    entries.sort(key=lambda item: (item[0].casefold(), item[2], item[1].casefold()))
    return tuple(((label, name) for _root, name, _depth, label in entries))

def status_consumable_subcategory(status_id: int, source_header: int | None=None) -> str:
    """依實際用途建立消耗品快速入口；經驗／掉寶狀態也能由此找到。"""
    library_category = status_library_category(status_id, source_header)
    if library_category in {'經驗', '掉寶'}:
        return '經驗／掉寶'
    if library_category != '消耗品':
        return ''
    explicit = BUNDLED_CONSUMABLE_SUBCATEGORIES.get(status_id)
    name = status_name(status_id, source_header)
    metadata = RUNTIME_STATUS_METADATA.get(status_id, {})
    searchable = f"{name} {EFST_NAMES.get(status_id, '')} {explicit or ''} {metadata.get('functional_category', '')} {' '.join((str(value) for value in metadata.get('item_names', [])))}".casefold()
    normalized_explicit = CONSUMABLE_SUBCATEGORY_ALIASES.get(str(explicit or ''), explicit)
    if normalized_explicit in CONSUMABLE_SUBCATEGORY_ORDER and normalized_explicit != '其他／待確認':
        return str(normalized_explicit)
    if any((token in searchable for token in ('scroll', '卷軸', 'book', '書籍', 'spellbook'))):
        return '技能卷軸'
    if any((token in searchable for token in ('experience', 'exp', '掉寶', '掉落', 'drop'))):
        return '經驗／掉寶'
    if any((token in searchable for token in ('攻速', '移速', '速度', '集中', '覺醒', '菠色克', 'aspd', 'speed'))):
        return '攻速／移速'
    if any((token in searchable for token in ('毒', 'poison', '失憶', '狂笑'))):
        return '毒藥／負面效果'
    if any((token in searchable for token in ('攻擊', '魔法', '傷害', '命中', '暴擊', 'atk', 'matk', 'critical'))):
        return '攻擊／魔法'
    if any((token in searchable for token in ('防禦', '抗性', '減傷', '迴避', 'def', 'resist', 'resistance'))):
        return '防禦／抗性'
    if any((token in searchable for token in ('料理', 'food_str', 'food_agi', 'food_vit', 'food_int', 'food_dex', 'food_luk', 'str提升', 'agi提升', 'vit提升', 'int提升', 'dex提升', 'luk提升'))):
        return '料理／能力值'
    if any((token in searchable for token in ('potion', '藥水', '藥劑', '生命水', '魔力水', 'heal', 'sp_', 'hp_', '補品', '恢復'))):
        return 'HP／SP／恢復'
    if any((token in searchable for token in ('變身', 'costume', 'transform', 'candy', '糖'))):
        return '特殊效果／變身'
    if normalized_explicit in CONSUMABLE_SUBCATEGORY_ORDER:
        return str(normalized_explicit)
    return '其他／待確認'

def available_consumable_subcategories(index: StatusSearchIndex) -> tuple[str, ...]:
    """只顯示目前資料庫確實有內容的用途，避免玩家點進空清單。"""
    return (CONSUMABLE_SUBCATEGORY_ORDER[0], *tuple((category for category in CONSUMABLE_SUBCATEGORY_ORDER[1:] if index.by_consumable.get(category))))

def status_display_priority(status_id: int, source_header: int | None=None) -> int:
    """玩家視角排序：異常最前，其次收益、消耗品、一般增益與未知。"""
    if not is_readable_status_name(status_id):
        return 5
    category = status_library_category(status_id, source_header)
    group = status_group(status_id, source_header)
    if category == 'DEBUFF' or group == '減益':
        return 0
    if category in {'經驗', '掉寶'} or status_id == FOCUS_STATUS_ID:
        return 1
    if category == '消耗品':
        return 2
    if category == 'BUFF' or group in {'增益', '主要監控'}:
        return 3
    return 4

def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from('<H', data, offset)[0]

def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<I', data, offset)[0]

def decode_target_name(raw: bytes) -> str | None:
    """解碼隊伍封包中的人物名稱；無法可靠解碼時回傳 None。"""
    raw = raw.split(b'\x00', 1)[0].strip(b' \t\r\n')
    if not raw:
        return None
    for encoding in ('cp950', 'big5', 'utf-8'):
        try:
            value = raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if not value or '�' in value:
            continue
        if any((ord(char) < 32 or ord(char) == 127 for char in value)):
            continue
        return value
    return None

def i32(data: bytes, offset: int) -> int:
    return struct.unpack_from('<i', data, offset)[0]

def decrypt_packet(date_tuple: tuple[int, int, int, int, int, int], encrypted: bytes) -> bytes:
    """解開 RRF v0.05 封包資料；尾端不足 4 bytes 的資料保持原樣。"""
    year, month, day, hour, minute, second = date_tuple
    key1 = struct.unpack('<i', struct.pack('<HBB', year, month, day))[0] >> 5
    key2 = struct.unpack('<i', struct.pack('<BBBB', 0, hour, minute, second))[0] >> 3
    decoded = bytearray(encrypted)
    for cursor in range(len(encrypted) // 4):
        offset = cursor * 4
        old_value = struct.unpack_from('<i', encrypted, offset)[0]
        xor_value = (key1 + cursor + 1) * key2
        struct.pack_into('<I', decoded, offset, (old_value ^ xor_value) & 4294967295)
    return bytes(decoded)

@dataclass(frozen=True)
class ReplayPacket:
    index: int
    timeline_ms: int
    header: int
    data: bytes

@dataclass(frozen=True)
class ReplayIdentitySnapshot:
    """RRF 建檔時寫入的錄影角色識別資料。"""
    aid: int | None = None
    name: str = ''

@dataclass
class RrfParseSession:
    path: Path | None = None
    replay_date: tuple[int, ...] = ()
    stream_offset: int = 0
    cursor: int = 0
    packet_index: int = 0
    file_size: int = 0
    prefix: bytes = b''
    stream_probes: tuple[tuple[int, bytes], ...] = ()
    ready: bool = False
    identity_snapshot: ReplayIdentitySnapshot = field(default_factory=ReplayIdentitySnapshot)
    pet_snapshot: ReplayPetSnapshot | None = None
    suppress_apply_events: bool = False

@dataclass
class StatusState:
    status_id: int
    target_id: int
    active: bool
    total_ms: int | None
    remaining_ms: int | None
    event_timeline_ms: int
    observed_monotonic: float
    source_header: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.status_id, self.target_id)

@dataclass
class PetMonitorState:
    """寵物面板的狀態快照；數值只接受已核對的 RRF 欄位。"""
    pet_id: int | None = None
    pet_name: str = ''
    level: int | None = None
    satiety: int | None = None
    intimacy: int | None = None
    state_text: str = '等待寵物 RRF 資料'
    source_text: str = '等待已核對的寵物封包'
    identity_generation: int = 0
    satiety_revision: int = 0
    satiety_timeline_ms: int | None = None
    pet_gid: int | None = None

class PetTracker:
    """解析已由「寵物測試1.rrf」核對的寵物封包。

    只處理 0x01A2 寵物資料與 0x01A4 寵物數值變更。0x01A2 的寵物種類、
    等級、飽食度與親密度欄位，以及 0x01A4 的 type 1／2 對應，均已由
    RRF 實測確認；其他 type 不猜測、不寫入。0x01A3 餵食回覆目前只作為
    已知封包保留，不會把餵食事件當成新的飽食度數值。
    """
    RELEVANT_HEADERS = {PET_PROPERTY_HEADER, PET_STATE_HEADER, PET_FEED_HEADER, SELF_ID_HEADER}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.selected_pet_id: int | None = None
        self._identity_generation = 0
        self._satiety_revision = 0
        self._observed_pet_name = ''
        self.state = PetMonitorState()
        self.processed_packets = -1
        self.last_timeline_ms = 0
        self._has_verified_property = False
        self._last_pet_gid: int | None = None
        self._owner_id: int | None = None
        self._pending_pet_init: tuple[int, int, int] | None = None
        self._retired_pet_gid: int | None = None

    def reset(self) -> None:
        with self._lock:
            self._reset_unlocked()

    def _reset_unlocked(self) -> None:
        self.selected_pet_id = None
        self._identity_generation += 1
        self._satiety_revision = 0
        self._observed_pet_name = ''
        self.state = PetMonitorState(identity_generation=self._identity_generation)
        self.processed_packets = -1
        self.last_timeline_ms = 0
        self._has_verified_property = False
        self._last_pet_gid = None
        self._owner_id = None
        self._pending_pet_init = None
        self._retired_pet_gid = None

    def seed_replay_snapshot(self, snapshot: ReplayPetSnapshot) -> bool:
        """只在空追蹤器種入歷史初值；不回傳可播報的即時觀測。"""
        with self._lock:
            if not isinstance(snapshot, ReplayPetSnapshot) or not snapshot.is_valid() or self._has_verified_property or (self.processed_packets >= 0):
                return False
            self._begin_observed_pet_unlocked(snapshot.pet_id, snapshot.pet_name)
            self._owner_id = snapshot.owner_id
            self._last_pet_gid = snapshot.pet_gid
            self._has_verified_property = True
            self.state = PetMonitorState(pet_id=snapshot.pet_id, pet_name=snapshot.pet_name, level=snapshot.level, satiety=snapshot.satiety, intimacy=snapshot.intimacy, state_text=pet_satiety_label(snapshot.satiety), source_text='RRF 錄影起始寵物資料（已核對）', identity_generation=self._identity_generation, pet_gid=snapshot.pet_gid)
            return True

    def set_selected_pet_id(self, pet_id: int | None) -> None:
        with self._lock:
            if self.selected_pet_id == pet_id:
                self.state.pet_id = pet_id
                self.state.pet_name = PET_NAMES_BY_ID.get(pet_id, '') if pet_id is not None else ''
                return
            self.selected_pet_id = pet_id
            self._identity_generation += 1
            self._observed_pet_name = ''
            self.state = PetMonitorState(pet_id=pet_id, pet_name=PET_NAMES_BY_ID.get(pet_id, '') if pet_id is not None else '', identity_generation=self._identity_generation)
            self._has_verified_property = False
            self._last_pet_gid = None
            self._pending_pet_init = None
            self._retired_pet_gid = None

    def consume(self, packets: list[ReplayPacket]) -> list[PetMonitorState]:
        """回傳按封包順序取得的新飽食度；親密度、餵食與未知封包不列入。"""
        observations: list[PetMonitorState] = []
        with self._lock:
            for packet in packets:
                if packet.index <= self.processed_packets:
                    continue
                previous_revision = self._satiety_revision
                if packet.header == SELF_ID_HEADER and len(packet.data) == 6:
                    owner_id = u32(packet.data, 2)
                    if 0 < owner_id < 4294967295:
                        if self._owner_id is not None and self._owner_id != owner_id:
                            self._reset_unlocked()
                            previous_revision = self._satiety_revision
                        self._owner_id = owner_id
                elif packet.header == PET_PROPERTY_HEADER:
                    self._consume_pet_property(packet)
                elif packet.header == PET_STATE_HEADER:
                    self._consume_pet_state(packet)
                if self._satiety_revision != previous_revision:
                    observations.append(replace(self.state))
                self.processed_packets = packet.index
                self.last_timeline_ms = max(self.last_timeline_ms, packet.timeline_ms)
        return observations

    def _begin_observed_pet_unlocked(self, pet_id: int, pet_name: str) -> None:
        changed = not self._has_verified_property or self.state.pet_id != pet_id or bool(pet_name and self._observed_pet_name and (pet_name != self._observed_pet_name))
        if changed:
            self._identity_generation += 1
            self._observed_pet_name = pet_name
        elif pet_name:
            self._observed_pet_name = pet_name
        self.selected_pet_id = pet_id

    def _record_satiety_unlocked(self, value: int, timeline_ms: int) -> None:
        self._satiety_revision += 1
        self.state.satiety = value
        self.state.satiety_revision = self._satiety_revision
        self.state.satiety_timeline_ms = timeline_ms
        self.state.state_text = pet_satiety_label(value)

    def _consume_pet_property(self, packet: ReplayPacket) -> None:
        data = packet.data
        if len(data) < 37:
            return
        pet_id = u16(data, 35)
        if pet_id <= 0:
            return
        level = u16(data, 27)
        satiety = u16(data, 29)
        intimacy = u16(data, 31)
        if level <= 0:
            level = None
        if not 0 <= satiety <= 100:
            satiety = None
        if not 0 <= intimacy <= 1000:
            intimacy = None
        raw_name = data[2:26].split(b'\x00', 1)[0]
        try:
            pet_name = raw_name.decode('cp950').strip()
        except UnicodeDecodeError:
            pet_name = raw_name.decode('cp950', errors='replace').strip()
        old_generation = self._identity_generation
        old_gid = self._last_pet_gid
        self._begin_observed_pet_unlocked(pet_id, pet_name)
        pending = self._pending_pet_init
        self._pending_pet_init = None
        if pending is not None and pending[1:] == (packet.index - 1, packet.timeline_ms):
            self._last_pet_gid = pending[0]
        elif self._identity_generation != old_generation:
            self._last_pet_gid = None
        if old_gid is not None and old_gid != self._last_pet_gid:
            self._retired_pet_gid = old_gid
        self.state = PetMonitorState(pet_id=pet_id, pet_name=pet_name or PET_NAMES_BY_ID.get(pet_id, ''), level=level, satiety=satiety, intimacy=intimacy, state_text=pet_satiety_label(satiety), source_text='RRF 0x01A2／0x01A4（已核對）', identity_generation=self._identity_generation, satiety_revision=self._satiety_revision, pet_gid=self._last_pet_gid)
        if satiety is not None:
            self._record_satiety_unlocked(satiety, packet.timeline_ms)
        self._has_verified_property = True

    def _consume_pet_state(self, packet: ReplayPacket) -> None:
        data = packet.data
        if len(data) != 11:
            return
        state_type = data[2]
        gid = u32(data, 3)
        value = u32(data, 7)
        if not 0 < gid < 4294967295:
            return
        if state_type == 0:
            if value == 0:
                self._pending_pet_init = (gid, packet.index, packet.timeline_ms)
            return
        if not self._has_verified_property or self.state.pet_id is None:
            return
        if self._last_pet_gid is not None and gid != self._last_pet_gid:
            return
        if self._last_pet_gid is None and gid == self._retired_pet_gid:
            return
        if state_type == 2:
            if not 0 <= value <= 100:
                return
            self._record_satiety_unlocked(value, packet.timeline_ms)
        elif state_type == 1:
            if not 0 <= value <= 1000:
                return
            self.state.intimacy = value
        else:
            return
        self._last_pet_gid = gid
        self.state.pet_gid = gid
        self.state.source_text = 'RRF 0x01A2／0x01A4（已核對）'

    def consume_verified_observation(self, *, pet_id: int, timeline_ms: int, level: int | None=None, satiety: int | None=None, intimacy: int | None=None, state_text: str='已讀取寵物資料', source_text: str='RRF（已驗證格式）') -> None:
        """保留測試與外部驗證入口；正式監控由 consume 解析已核對封包。"""
        with self._lock:
            if not isinstance(pet_id, int) or isinstance(pet_id, bool) or pet_id <= 0:
                return
            if not isinstance(satiety, int) or isinstance(satiety, bool) or (not 0 <= satiety <= 100):
                satiety = None
            self._begin_observed_pet_unlocked(pet_id, '')
            self._has_verified_property = True
            self.state = PetMonitorState(pet_id=pet_id, pet_name=PET_NAMES_BY_ID.get(pet_id, ''), level=level, satiety=satiety, intimacy=intimacy, state_text=state_text, source_text=source_text, identity_generation=self._identity_generation, satiety_revision=self._satiety_revision)
            if satiety is not None:
                self._record_satiety_unlocked(satiety, timeline_ms)
            self.last_timeline_ms = max(self.last_timeline_ms, timeline_ms)

    def snapshot(self) -> PetMonitorState:
        with self._lock:
            result = replace(self.state)
            if result.pet_id is None:
                return result
            result.pet_name = result.pet_name or PET_NAMES_BY_ID.get(result.pet_id, '')
            return result

def pet_satiety_label(satiety: int | None) -> str:
    """依目前玩家提供的五段飽食度區間產生顯示文字。"""
    if satiety is None:
        return '等待寵物資料'
    value = max(0, min(100, int(satiety)))
    if value <= 10:
        return '飢餓'
    if value <= 25:
        return '稍微飢餓'
    if value <= 75:
        return '普通'
    if value <= 90:
        return '吃飽'
    return '非常飽'

class RrfParser:
    """RRF v0.05 的最小唯讀解析器。"""
    HEADER_OFFSET = 100
    CONTAINER_TABLE_OFFSET = 112
    CONTAINER_COUNT = 24
    PACKET_STREAM_TYPE = 1
    REPLAY_DATA_TYPE = 2
    SESSION_TYPE = 3
    SESSION_AID_CHUNK_ID = 1010
    REPLAY_NAME_CHUNK_INDEX = 4
    MAX_METADATA_CONTAINER_BYTES = 4 * 1024 * 1024
    COMPANIONS_TYPE = 9
    MAX_PET_SNAPSHOT_BYTES = 64 * 1024

    def read_replay_date(self, data: bytes) -> tuple[int, ...] | None:
        if len(data) < self.HEADER_OFFSET + 12:
            return None
        if not data.startswith(b'<< Ragnarok Replay File Version'):
            return None
        year = u16(data, self.HEADER_OFFSET + 4)
        month = data[self.HEADER_OFFSET + 6]
        day = data[self.HEADER_OFFSET + 7]
        hour = data[self.HEADER_OFFSET + 9]
        minute = data[self.HEADER_OFFSET + 10]
        second = data[self.HEADER_OFFSET + 11]
        try:
            datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None
        return (year, month, day, hour, minute, second)

    def find_packet_stream(self, data: bytes, file_size: int | None=None) -> tuple[int, int] | None:
        if len(data) < self.CONTAINER_TABLE_OFFSET + self.CONTAINER_COUNT * 10:
            return None
        available_size = len(data) if file_size is None else max(len(data), file_size)
        for container_index in range(self.CONTAINER_COUNT):
            offset = self.CONTAINER_TABLE_OFFSET + container_index * 10
            container_type = u16(data, offset)
            container_length = i32(data, offset + 2)
            container_offset = i32(data, offset + 6)
            if container_type != self.PACKET_STREAM_TYPE or container_offset <= 0:
                continue
            if container_offset > available_size:
                continue
            if container_length > 0:
                stream_end = min(available_size, container_offset + container_length)
            else:
                stream_end = available_size
            return (container_offset, stream_end)
        return None

    def find_container(self, data: bytes, container_type: int, file_size: int | None=None) -> tuple[int, int] | None:
        """從固定容器表找出完整且大小合理的非封包容器。"""
        if len(data) < self.CONTAINER_TABLE_OFFSET + self.CONTAINER_COUNT * 10:
            return None
        available_size = len(data) if file_size is None else max(len(data), file_size)
        for container_index in range(self.CONTAINER_COUNT):
            offset = self.CONTAINER_TABLE_OFFSET + container_index * 10
            current_type = u16(data, offset)
            container_length = i32(data, offset + 2)
            container_offset = i32(data, offset + 6)
            if current_type != container_type:
                continue
            if container_offset <= 0 or container_length <= 0 or container_length > self.MAX_METADATA_CONTAINER_BYTES or (container_offset + container_length > available_size):
                return None
            return (container_offset, container_length)
        return None

    @staticmethod
    def decode_container_chunks(encrypted: bytes, replay_date: tuple[int, ...]) -> list[tuple[int, bytes]]:
        """解開 RRF 一般容器中的 ``chunk id + length + payload``。"""
        decoded = decrypt_packet(replay_date, encrypted)
        chunks: list[tuple[int, bytes]] = []
        cursor = 0
        while cursor + 6 <= len(decoded) and len(chunks) < 4096:
            chunk_id = u16(decoded, cursor)
            chunk_length = i32(decoded, cursor + 2)
            cursor += 6
            if chunk_length < 0 or cursor + chunk_length > len(decoded):
                break
            chunks.append((chunk_id, decoded[cursor:cursor + chunk_length]))
            cursor += chunk_length
        return chunks

    def read_identity_snapshot_from_handle(self, handle, header: bytes, file_size: int, replay_date: tuple[int, ...]) -> ReplayIdentitySnapshot:
        """只讀取兩個小型中繼資料容器，取得錄影角色 AID 與名稱。"""
        original_position = handle.tell()
        aid: int | None = None
        name = ''
        try:
            session_info = self.find_container(header, self.SESSION_TYPE, file_size)
            if session_info is not None:
                session_offset, session_length = session_info
                handle.seek(session_offset)
                session_chunks = self.decode_container_chunks(handle.read(session_length), replay_date)
                for chunk_id, payload in session_chunks:
                    if chunk_id == self.SESSION_AID_CHUNK_ID and len(payload) >= 4:
                        candidate = u32(payload, 0)
                        if 0 < candidate <= 4294967295:
                            aid = candidate
                        break
            replay_info = self.find_container(header, self.REPLAY_DATA_TYPE, file_size)
            if replay_info is not None:
                replay_offset, replay_length = replay_info
                handle.seek(replay_offset)
                replay_chunks = self.decode_container_chunks(handle.read(replay_length), replay_date)
                if len(replay_chunks) > self.REPLAY_NAME_CHUNK_INDEX:
                    decoded_name = decode_target_name(replay_chunks[self.REPLAY_NAME_CHUNK_INDEX][1])
                    name = decoded_name or ''
        except (OSError, struct.error, ValueError):
            return ReplayIdentitySnapshot()
        finally:
            try:
                handle.seek(original_position)
            except OSError:
                pass
        return ReplayIdentitySnapshot(aid=aid, name=name)

    def read_identity_snapshot(self, path: Path) -> ReplayIdentitySnapshot:
        """開發驗證與工具共用入口；不載入整份 RRF。"""
        try:
            file_size = path.stat().st_size
            with path.open('rb') as handle:
                header = handle.read(4096)
                replay_date = self.read_replay_date(header)
                if replay_date is None:
                    return ReplayIdentitySnapshot()
                return self.read_identity_snapshot_from_handle(handle, header, file_size, replay_date)
        except OSError:
            return ReplayIdentitySnapshot()

    def read_pet_snapshot_from_handle(self, handle, header: bytes, file_size: int, replay_date: tuple[int, ...], owner_id: int | None) -> ReplayPetSnapshot | None:
        """只讀自身 companions 容器；完整有效後才供歷史初始化。"""
        original_position = handle.tell()
        try:
            info = self.find_container(header, self.COMPANIONS_TYPE, file_size)
            if info is None or info[1] > self.MAX_PET_SNAPSHOT_BYTES:
                return None
            offset, length = info
            handle.seek(offset)
            encrypted = handle.read(length)
            if len(encrypted) != length:
                return None
            chunks = self.decode_container_chunks(encrypted, replay_date)
            if sum((6 + len(payload) for _chunk_id, payload in chunks)) != length:
                return None
            return decode_pet_snapshot(chunks, owner_id)
        except (OSError, struct.error, ValueError):
            return None
        finally:
            handle.seek(original_position)

    def decode_packets(self, data: bytes, replay_date: tuple[int, ...], stream_offset: int, stream_end: int, cursor: int=0, packet_index: int=0) -> tuple[list[ReplayPacket], int, int]:
        packets: list[ReplayPacket] = []
        while stream_offset + cursor + 10 <= stream_end:
            packet_start = stream_offset + cursor
            timeline_ms = i32(data, packet_start + 4)
            payload_length = u16(data, packet_start + 8)
            end = packet_start + 10 + payload_length
            if end > stream_end:
                break
            encrypted = data[packet_start + 10:end]
            decoded = decrypt_packet(replay_date, encrypted)
            if len(decoded) >= 2:
                packets.append(ReplayPacket(index=packet_index, timeline_ms=timeline_ms, header=u16(decoded, 0), data=decoded))
            packet_index += 1
            cursor = end - stream_offset
        return (packets, cursor, packet_index)

    def decode_packets_from_handle(self, handle, replay_date: tuple[int, ...], stream_offset: int, stream_end: int, cursor: int=0, packet_index: int=0, chunk_size: int=1024 * 1024) -> tuple[list[ReplayPacket], int, int]:
        """相容舊呼叫端；即時監控請使用 stream_packets_from_handle。"""
        packets: list[ReplayPacket] = []
        absolute_cursor = cursor

        def collect(batch: list[ReplayPacket], batch_cursor: int, batch_packet_index: int) -> None:
            nonlocal absolute_cursor, packet_index
            packets.extend(batch)
            absolute_cursor = batch_cursor
            packet_index = batch_packet_index
        self.stream_packets_from_handle(handle, replay_date, stream_offset, stream_end, cursor=cursor, packet_index=packet_index, chunk_size=chunk_size, batch_size=max(1, len(packets) or PROCESS_PACKET_BATCH_SIZE), max_batch_seconds=PROCESS_BATCH_TIME_BUDGET_SECONDS, on_batch=collect)
        return (packets, absolute_cursor, packet_index)

    def stream_packets_from_handle(self, handle, replay_date: tuple[int, ...], stream_offset: int, stream_end: int, cursor: int=0, packet_index: int=0, chunk_size: int=1024 * 1024, batch_size: int=PROCESS_PACKET_BATCH_SIZE, max_batch_seconds: float=PROCESS_BATCH_TIME_BUDGET_SECONDS, on_batch: Callable[[list[ReplayPacket], int, int], None] | None=None, should_continue: Callable[[], bool] | None=None) -> tuple[int, int]:
        """串流解碼並分批交付，不把本次所有封包留在記憶體。"""
        absolute_cursor = max(0, cursor)
        packet_index = max(0, packet_index)
        batch_size = max(1, int(batch_size))
        max_batch_seconds = max(0.001, float(max_batch_seconds))
        handle.seek(stream_offset + absolute_cursor)
        buffer = bytearray()
        batch: list[ReplayPacket] = []
        batch_started_at = time.monotonic()
        cancelled = False

        def flush_batch() -> None:
            nonlocal batch_started_at
            if not batch:
                return
            if on_batch is not None:
                on_batch(batch, absolute_cursor, packet_index)
            batch.clear()
            batch_started_at = time.monotonic()
        while stream_offset + absolute_cursor < stream_end:
            if should_continue is not None and (not should_continue()):
                cancelled = True
                break
            read_size = min(chunk_size, stream_end - handle.tell())
            if read_size <= 0:
                break
            chunk = handle.read(read_size)
            if not chunk:
                break
            buffer.extend(chunk)
            buffer_cursor = 0
            while len(buffer) - buffer_cursor >= 10:
                packet_start = buffer_cursor
                timeline_ms = i32(buffer, packet_start + 4)
                payload_length = u16(buffer, packet_start + 8)
                packet_end = packet_start + 10 + payload_length
                if packet_end > len(buffer):
                    break
                encrypted = bytes(buffer[packet_start + 10:packet_end])
                decoded = decrypt_packet(replay_date, encrypted)
                if len(decoded) >= 2:
                    batch.append(ReplayPacket(index=packet_index, timeline_ms=timeline_ms, header=u16(decoded, 0), data=decoded))
                packet_index += 1
                absolute_cursor += packet_end - packet_start
                buffer_cursor = packet_end
                if should_continue is not None and packet_index % 256 == 0 and (not should_continue()):
                    cancelled = True
                    break
                if len(batch) >= batch_size or time.monotonic() - batch_started_at >= max_batch_seconds:
                    flush_batch()
                    if should_continue is not None and (not should_continue()):
                        cancelled = True
                        break
            if buffer_cursor:
                del buffer[:buffer_cursor]
            if cancelled:
                break
        if cancelled:
            batch.clear()
        else:
            flush_batch()
        return (absolute_cursor, packet_index)

    def parse_file(self, path: Path) -> tuple[list[ReplayPacket], tuple[int, ...]]:
        data = path.read_bytes()
        replay_date = self.read_replay_date(data)
        if replay_date is None:
            return ([], ())
        stream_info = self.find_packet_stream(data)
        if stream_info is None:
            return ([], replay_date)
        stream_offset, stream_end = stream_info
        packets, _cursor, _packet_index = self.decode_packets(data, replay_date, stream_offset, stream_end)
        return (packets, replay_date)

class RrfIncrementalParser:
    """只解碼 RRF 自上次輪詢後新增的完整封包。"""
    PREFIX_CHECK_BYTES = 4096
    HEADER_READ_BYTES = 4096
    STREAM_PROBE_BYTES = 512

    def __init__(self, parser: RrfParser | None=None) -> None:
        self.parser = parser or RrfParser()
        self.session = RrfParseSession()

    @property
    def packet_count(self) -> int:
        return self.session.packet_index

    @property
    def suppress_apply_events(self) -> bool:
        return self.session.suppress_apply_events

    def reset(self) -> None:
        self.session = RrfParseSession()

    def header_prefix(self, data: bytes, stream_offset: int) -> bytes:
        return data[:min(max(0, stream_offset), self.PREFIX_CHECK_BYTES)]

    def _consumed_stream_matches(self, handle: BinaryIO) -> bool:
        for offset, expected in self.session.stream_probes:
            handle.seek(offset)
            if handle.read(len(expected)) != expected:
                return False
        return True

    def _capture_stream_probes(self, handle: BinaryIO) -> None:
        consumed = self.session.cursor
        width = min(consumed, self.STREAM_PROBE_BYTES)
        probes: list[tuple[int, bytes]] = []
        if width:
            offsets = dict.fromkeys((self.session.stream_offset, self.session.stream_offset + consumed - width))
            for offset in offsets:
                handle.seek(offset)
                probes.append((offset, handle.read(width)))
        self.session.stream_probes = tuple(probes)

    def parse_incremental(self, path: Path) -> tuple[list[ReplayPacket], tuple[int, ...], bool]:
        """相容測試與舊呼叫端；即時監控使用分批 callback 版本。"""
        packets: list[ReplayPacket] = []

        def collect(batch: list[ReplayPacket], _reset_required: bool) -> None:
            packets.extend(batch)
        replay_date, reset_required, _packet_count = self.parse_incremental_batches(path, collect)
        return (packets, replay_date, reset_required)

    def parse_incremental_batches(self, path: Path, on_batch: Callable[[list[ReplayPacket], bool], None], batch_size: int=PROCESS_PACKET_BATCH_SIZE, max_batch_seconds: float=PROCESS_BATCH_TIME_BUDGET_SECONDS, should_continue: Callable[[], bool] | None=None) -> tuple[tuple[int, ...], bool, int]:
        """只解碼新增完整封包，並在每批完成後立即交給呼叫端。"""
        file_size = path.stat().st_size
        with path.open('rb') as handle:
            header = handle.read(self.HEADER_READ_BYTES)
            replay_date = self.parser.read_replay_date(header)
            stream_info = self.parser.find_packet_stream(header, file_size) if replay_date is not None else None
            if replay_date is None or stream_info is None:
                reset_required = self.session.ready or self.session.path != path
                self.session.path = path
                self.session.file_size = file_size
                self.session.prefix = header[:self.PREFIX_CHECK_BYTES]
                self.session.ready = False
                self.session.identity_snapshot = ReplayIdentitySnapshot()
                self.session.pet_snapshot = None
                if reset_required:
                    on_batch([], True)
                return (replay_date or (), reset_required, self.session.packet_index)
            stream_offset, stream_end = stream_info
            current_prefix = self.header_prefix(header, stream_offset)
            reset_required = not self.session.ready or self.session.path != path or file_size < self.session.file_size or (self.session.prefix != current_prefix) or (self.session.replay_date != replay_date) or (self.session.stream_offset != stream_offset) or (not self._consumed_stream_matches(handle))
            identity_snapshot = self.session.identity_snapshot
            if reset_required or identity_snapshot.aid is None:
                identity_snapshot = self.parser.read_identity_snapshot_from_handle(handle, header, file_size, replay_date)
                if not reset_required and identity_snapshot.aid is not None:
                    reset_required = True
            pet_snapshot = self.session.pet_snapshot
            if reset_required or pet_snapshot is None:
                pet_snapshot = self.parser.read_pet_snapshot_from_handle(handle, header, file_size, replay_date, identity_snapshot.aid)
                if not reset_required and pet_snapshot is not None:
                    reset_required = True
            if reset_required:
                self.session = RrfParseSession(path=path, replay_date=replay_date, stream_offset=stream_offset, file_size=file_size, prefix=current_prefix, ready=True, identity_snapshot=identity_snapshot, pet_snapshot=pet_snapshot, suppress_apply_events=True)
            read_start = stream_offset + self.session.cursor
            if stream_end <= read_start:
                self.session.file_size = file_size
                self.session.prefix = current_prefix
                if reset_required:
                    on_batch([], True)
                self.session.suppress_apply_events = False
                return (replay_date, reset_required, self.session.packet_index)
            if reset_required:
                on_batch([], True)
            first_batch = not reset_required

            def deliver(batch: list[ReplayPacket], cursor: int, packet_index: int) -> None:
                nonlocal first_batch
                reset_for_batch = reset_required and first_batch
                on_batch(batch, reset_for_batch)
                self.session.cursor = cursor
                self.session.packet_index = packet_index
                self.session.file_size = file_size
                self.session.prefix = current_prefix
                self.session.ready = True
                first_batch = False
            cursor, packet_index = self.parser.stream_packets_from_handle(handle, replay_date, stream_offset, stream_end, self.session.cursor, self.session.packet_index, batch_size=batch_size, max_batch_seconds=max_batch_seconds, on_batch=deliver, should_continue=should_continue)
            self.session.cursor = cursor
            self.session.packet_index = packet_index
            self._capture_stream_probes(handle)
            if should_continue is None or should_continue():
                self.session.suppress_apply_events = False
            self.session.file_size = file_size
            self.session.prefix = current_prefix
            self.session.ready = True
            return (replay_date, reset_required, packet_index)

class StatusTracker:
    """將 RRF 狀態事件轉成目前可顯示的倒數。"""
    STATUS_WITH_REMAINING = {1087, 2435}
    STATUS_END = 406

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.states: dict[tuple[int, int], StatusState] = {}
        self.allowed_status_ids: set[int] | None = None
        self.allowed_target_ids: set[int] | None = set()
        self.protected_target_ids: set[int] = set()
        self.processed_packets = -1
        self.last_timeline_ms = 0
        self.pending_apply_events: deque[tuple[int, int, int]] = deque(maxlen=MAX_PENDING_APPLY_EVENTS)
        self.actor_status_ids_by_target: dict[int, set[int]] = {}

    def reset(self) -> None:
        with self._lock:
            self.allowed_target_ids = set()
            self.states.clear()
            self.processed_packets = -1
            self.last_timeline_ms = 0
            self.pending_apply_events.clear()
            self.actor_status_ids_by_target.clear()

    def set_allowed_target_ids(self, target_ids: set[int] | None) -> None:
        """限制狀態追蹤目標；切換自身 ID 時同步移除其他目標的既有狀態。"""
        with self._lock:
            normalized_ids = set() if target_ids is None else set(target_ids)
            if self.allowed_target_ids == normalized_ids:
                return
            self.allowed_target_ids = normalized_ids
            if self.allowed_target_ids is not None:
                self.states = {key: state for key, state in self.states.items() if state.target_id in self.allowed_target_ids}
                self.pending_apply_events = deque((event for event in self.pending_apply_events if event[1] in self.allowed_target_ids), maxlen=MAX_PENDING_APPLY_EVENTS)
                self.actor_status_ids_by_target = {target_id: status_ids for target_id, status_ids in self.actor_status_ids_by_target.items() if target_id in self.allowed_target_ids}

    def set_allowed_status_ids(self, status_ids: set[int] | None) -> None:
        """更新即時監控狀態白名單，避免未勾選狀態長期佔用追蹤資料。"""
        with self._lock:
            normalized_ids = None if status_ids is None else {int(status_id) for status_id in status_ids if 0 <= int(status_id) <= 65535}
            if self.allowed_status_ids == normalized_ids:
                return
            self.allowed_status_ids = normalized_ids
            if self.allowed_status_ids is not None:
                self.states = {key: state for key, state in self.states.items() if state.status_id in self.allowed_status_ids}
                self.pending_apply_events = deque((event for event in self.pending_apply_events if event[0] in self.allowed_status_ids), maxlen=MAX_PENDING_APPLY_EVENTS)

    def set_protected_target_ids(self, target_ids: Iterable[int]) -> None:
        """標記目前規則允許的目標；其餘狀態最多保留隔離區上限。"""
        with self._lock:
            self.protected_target_ids = {int(target_id) for target_id in target_ids if int(target_id) > 0}
            self._prune_excess_active_states()

    def consume(self, packets: list[ReplayPacket], emit_apply_events: bool=True) -> None:
        with self._lock:
            if self.processed_packets >= 0 and packets and (packets[-1].index < self.processed_packets):
                self.states.clear()
                self.pending_apply_events.clear()
                self.processed_packets = -1
                self.last_timeline_ms = 0
                self.actor_status_ids_by_target.clear()
            for packet in packets:
                if packet.index <= self.processed_packets:
                    continue
                if packet.header == ACTOR_STATE_HEADER:
                    applied_events = self._consume_actor_state_packet(packet)
                    if emit_apply_events:
                        self.pending_apply_events.extend(applied_events)
                else:
                    applied = self._consume_packet(packet)
                    if emit_apply_events and applied is not None:
                        self.pending_apply_events.append(applied)
                self.processed_packets = packet.index
                self.last_timeline_ms = max(self.last_timeline_ms, packet.timeline_ms)
            for state in self.states.values():
                if state.active and state.remaining_ms is not None:
                    elapsed = max(0, self.last_timeline_ms - state.event_timeline_ms)
                    state.remaining_ms = max(0, state.remaining_ms - elapsed)
                    state.event_timeline_ms = self.last_timeline_ms
                    state.observed_monotonic = time.monotonic()
            self._prune_finished_states()
            self._prune_excess_active_states()

    def _consume_actor_state_packet(self, packet: ReplayPacket) -> list[tuple[int, int, int]]:
        """把 0x0229 旗標合併到一般狀態模型；未知 bit 僅忽略、不猜名稱。"""
        snapshot = decode_actor_state_packet(packet.data)
        if snapshot is None:
            return []
        target_id = snapshot.target_id
        if self.allowed_target_ids is not None and target_id not in self.allowed_target_ids:
            return []
        previous_ids = self.actor_status_ids_by_target.get(target_id, set())
        current_ids = set(snapshot.status_ids)
        if current_ids:
            if target_id not in self.actor_status_ids_by_target and len(self.actor_status_ids_by_target) >= MAX_TRACKED_TARGETS:
                return []
            self.actor_status_ids_by_target[target_id] = current_ids
        else:
            self.actor_status_ids_by_target.pop(target_id, None)
        allowed_ids = self.allowed_status_ids
        for status_id in previous_ids - current_ids:
            state = self.states.get((status_id, target_id))
            if state is not None and state.source_header == ACTOR_STATE_HEADER:
                state.active = False
                state.remaining_ms = 0
                state.event_timeline_ms = packet.timeline_ms
        applied_events: list[tuple[int, int, int]] = []
        for status_id in current_ids:
            if allowed_ids is not None and status_id not in allowed_ids:
                continue
            existing = self.states.get((status_id, target_id))
            was_active = status_id in previous_ids or (existing is not None and existing.active)
            if existing is None or existing.source_header == ACTOR_STATE_HEADER or (not existing.active):
                self.states[status_id, target_id] = StatusState(status_id=status_id, target_id=target_id, active=True, total_ms=None, remaining_ms=None, event_timeline_ms=packet.timeline_ms, observed_monotonic=time.monotonic(), source_header=ACTOR_STATE_HEADER)
            if not was_active:
                applied_events.append((status_id, target_id, packet.timeline_ms))
        return applied_events

    def _prune_finished_states(self) -> None:
        """保留有限的結束狀態，避免超長錄影使記憶體無限制成長。"""
        finished = [(state.event_timeline_ms, key) for key, state in self.states.items() if not state.active]
        excess = len(finished) - MAX_FINISHED_STATES
        if excess <= 0:
            return
        for _timeline_ms, key in sorted(finished)[:excess]:
            self.states.pop(key, None)

    def _prune_excess_active_states(self) -> None:
        """限制異常 RRF 或大量目標造成的 active 狀態無限成長。"""
        active = [(state.event_timeline_ms, key) for key, state in self.states.items() if state.active]
        protected_targets = set(self.allowed_target_ids) if self.allowed_target_ids is not None else set(self.protected_target_ids)
        unprotected = sorted(((timeline_ms, key) for timeline_ms, key in active if key[1] not in protected_targets))
        quarantine_excess = len(unprotected) - MAX_QUARANTINED_STATES
        if quarantine_excess > 0:
            for _timeline_ms, key in unprotected[:quarantine_excess]:
                self.states.pop(key, None)
        active = [(state.event_timeline_ms, key) for key, state in self.states.items() if state.active]
        excess = len(active) - MAX_ACTIVE_STATES
        if excess <= 0:
            return
        removable = sorted(((timeline_ms, key) for timeline_ms, key in active if key[1] not in protected_targets))
        removed_keys = [key for _timeline_ms, key in removable[:excess]]
        for key in removed_keys:
            self.states.pop(key, None)
        remaining_excess = excess - len(removed_keys)
        if remaining_excess > 0:
            removed_set = set(removed_keys)
            remaining_active = [(timeline_ms, key) for timeline_ms, key in active if key not in removed_set]
            for _timeline_ms, key in sorted(remaining_active)[:remaining_excess]:
                self.states.pop(key, None)

    def _consume_packet(self, packet: ReplayPacket) -> tuple[int, int, int] | None:
        data = packet.data
        if packet.header == self.STATUS_END and len(data) >= 9:
            status_id = u16(data, 2)
            if self.allowed_status_ids is not None and status_id not in self.allowed_status_ids:
                return None
            target_id = u32(data, 4)
            if self.allowed_target_ids is not None and target_id not in self.allowed_target_ids:
                return None
            state = self.states.get((status_id, target_id))
            if state is None:
                state = StatusState(status_id=status_id, target_id=target_id, active=False, total_ms=None, remaining_ms=0, event_timeline_ms=packet.timeline_ms, observed_monotonic=time.monotonic(), source_header=packet.header)
                self.states[state.key] = state
            else:
                state.active = False
                state.remaining_ms = 0
                state.event_timeline_ms = packet.timeline_ms
            return None
        if packet.header not in self.STATUS_WITH_REMAINING:
            return None
        if packet.header == 1087 and len(data) < 13:
            return None
        if packet.header == 2435 and len(data) < 17:
            return None
        status_id = u16(data, 2)
        if self.allowed_status_ids is not None and status_id not in self.allowed_status_ids:
            return None
        target_id = u32(data, 4)
        if self.allowed_target_ids is not None and target_id not in self.allowed_target_ids:
            return None
        active = data[8] != 0
        if packet.header == 1087:
            total_ms = None
            remaining_ms = u32(data, 9)
        else:
            total_ms = u32(data, 9)
            remaining_ms = u32(data, 13)
        self.states[status_id, target_id] = StatusState(status_id=status_id, target_id=target_id, active=active, total_ms=total_ms, remaining_ms=remaining_ms, event_timeline_ms=packet.timeline_ms, observed_monotonic=time.monotonic(), source_header=packet.header)
        return (status_id, target_id, packet.timeline_ms) if active else None

    def advance_timeline(self, timeline_ms: int) -> None:
        """讓監控保留整段 RRF 的最新時間軸，不必把所有封包送進狀態追蹤器。"""
        with self._lock:
            self.last_timeline_ms = max(self.last_timeline_ms, int(timeline_ms))

    def pop_apply_events(self) -> list[tuple[int, int, int]]:
        """取出新套用事件；由 UI 執行緒一次取走，避免重複提示。"""
        with self._lock:
            events = list(self.pending_apply_events)
            self.pending_apply_events.clear()
            return events

    def snapshot(self) -> list[tuple[StatusState, int | None]]:
        with self._lock:
            now = time.monotonic()
            result = []
            for state in self.states.values():
                remaining = state.remaining_ms
                if state.active and remaining is not None:
                    remaining = max(0, remaining - int((now - state.observed_monotonic) * 1000))
                result.append((replace(state), remaining))
            return result

class TargetTracker:
    """從 RRF 的隊伍與畫面人物封包建立目標分類。

    狀態封包使用 AID 作為目標 ID；現代客戶端的隊伍封包為 0x0AE4/0x0AE5，
    畫面人物則由 0x09FD/0x09FE/0x09FF 的 PC 型別辨識。自身 ID 只採用可重現
    的明確來源，優先序為 0x0283、RRF 錄影快照、唯一同名配對、儲存設定；
    自我施放技能只保留為候選清單，不拿來猜測自己。
    人物 ID 以有界集合保存，避免異常錄影持續累積人物資料。
    """
    PARTY_MEMBER_INFO = 2788
    PARTY_INFO = 2789
    PARTY_WITHDRAW = 261
    PARTY_MEMBER_PACKET_SIZE = 89
    PARTY_MEMBER_PARTY_NAME_OFFSET = 23
    UNIT_PACKETS = {2557, 2558, 2559}
    UNIT_NAME_OFFSETS = {2557: 90, 2558: 83, 2559: 84}
    USE_SKILL = 2507
    SELF_ID = SELF_ID_HEADER
    ACTOR_NAME_INFO = 2608
    ACTOR_NAME_OFFSET = 6
    ACTOR_NAME_SIZE = 24
    STATUS_HEADERS = {406, 1087, 2435}
    PARTY_MEMBER_SIZE = 54
    PARTY_NAME_SIZE = 24
    PARTY_MEMBER_NAME_OFFSET = 47
    PARTY_LIST_MEMBER_NAME_OFFSET = 8
    MAX_PARTY_TARGETS = MAX_TRACKED_TARGETS - 1
    CORE_TRACKING_HEADERS = {PARTY_MEMBER_INFO, PARTY_INFO, PARTY_WITHDRAW, USE_SKILL, SELF_ID, ACTOR_NAME_INFO} | UNIT_PACKETS
    TARGET_TRACKING_HEADERS = CORE_TRACKING_HEADERS | STATUS_HEADERS | {ACTOR_STATE_HEADER}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processed_packets = -1
        self.enabled_scopes: set[str] = {'自己', '隊伍成員'}
        self.party_ids: set[int] = set()
        self.target_names: dict[int, str] = {}
        self.target_name_sources: dict[int, set[str]] = {}
        self.screen_player_ids: set[int] = set()
        self.status_target_ids: set[int] = set()
        self.status_target_counts: Counter[int] = Counter()
        self.self_hint_counts: Counter[int] = Counter()
        self.target_last_seen_ms: dict[int, int] = {}
        self.latest_timeline_ms = 0
        self._configured_self_id: int | None = None
        self._configured_self_name: str | None = None
        self._snapshot_self_id: int | None = None
        self._snapshot_self_name = ''
        self._packet_confirmed_self_id: int | None = None
        self.core_monitoring_only = False
        self._scoped_party_name = b''
        self._scoped_identity_generation = 0

    def reset(self) -> None:
        with self._lock:
            self.processed_packets = -1
            self.party_ids.clear()
            self.target_names.clear()
            self.target_name_sources.clear()
            self.screen_player_ids.clear()
            self.status_target_ids.clear()
            self.status_target_counts.clear()
            self.self_hint_counts.clear()
            self.target_last_seen_ms.clear()
            self.latest_timeline_ms = 0
            self._snapshot_self_id = None
            self._snapshot_self_name = ''
            self._packet_confirmed_self_id = None
            self._scoped_party_name = b''
            self._scoped_identity_generation += 1

    def set_configured_self_id(self, target_id: int | None) -> None:
        with self._lock:
            self._configured_self_id = None

    def set_configured_self_name(self, target_name: str | None) -> None:
        """記住使用者輸入的遊戲內人物名稱，收到 RRF 人物資料後再配對 ID。"""
        name = str(target_name or '').strip()
        with self._lock:
            self._configured_self_name = None

    def set_replay_identity(self, target_id: int | None, target_name: str | None) -> None:
        """套用目前 RRF 的錄影者快照；切換錄影時不沿用上一段資料。"""
        name = str(target_name or '').strip()
        with self._lock:
            self._clear_scoped_records_unlocked()
            self._packet_confirmed_self_id = None
            self._remove_name_source('replay_snapshot')
            valid_id = int(target_id) if target_id is not None and self._valid_id(int(target_id)) else None
            self._snapshot_self_id = valid_id
            self._snapshot_self_name = name if valid_id is not None else ''
            if valid_id is not None and name:
                self._set_target_name(valid_id, name, 'replay_snapshot')

    @staticmethod
    def _normalized_target_name(target_name: str | None) -> str:
        return str(target_name or '').strip().casefold()

    def find_target_ids_by_name(self, target_name: str) -> list[int]:
        """以完整人物名稱尋找目前 RRF 已解析到的目標 ID。"""
        normalized = self._normalized_target_name(target_name)
        if not normalized:
            return []
        with self._lock:
            return sorted((target_id for target_id, known_name in self.target_names.items() if self._normalized_target_name(known_name) == normalized and target_id in self._trusted_target_ids_unlocked()))

    def _resolved_self_id_unlocked(self) -> int | None:
        if self._packet_confirmed_self_id is not None:
            return self._packet_confirmed_self_id
        if self._snapshot_self_id is not None:
            return self._snapshot_self_id
        return None

    def _unique_configured_name_match_unlocked(self) -> int | None:
        return None

    def set_core_monitoring_only(self, enabled: bool) -> None:
        """核心模式只保留自身辨識線索，不收集隊伍／畫面人物等非必要資料。"""
        with self._lock:
            if self.core_monitoring_only == bool(enabled):
                return
            self.core_monitoring_only = bool(enabled)
            if self.core_monitoring_only:
                scoped_self_id = self._resolved_self_id_unlocked()
                scoped_self_name = self.target_names.get(scoped_self_id, '')
                self.party_ids.clear()
                self.screen_player_ids.clear()
                self.target_names.clear()
                self.target_name_sources.clear()
                self.status_target_ids.clear()
                self.status_target_counts.clear()
                self.target_last_seen_ms.clear()
                self._scoped_party_name = b''
                self._scoped_identity_generation += 1
                if scoped_self_id is not None and scoped_self_name:
                    self._set_target_name(scoped_self_id, scoped_self_name, 'self')
                if self._snapshot_self_id is not None and self._snapshot_self_name:
                    self._set_target_name(self._snapshot_self_id, self._snapshot_self_name, 'replay_snapshot')

    def set_enabled_scopes(self, scopes: Iterable[str]) -> None:
        """套用人物分類掃描開關；停用後保留既有分類，避免重新分類造成誤判。"""
        enabled = set(scopes) & set(TARGET_SCOPE_ORDER)
        with self._lock:
            self.enabled_scopes = {'自己', '隊伍成員'}

    def scope_enabled(self, scope: str) -> bool:
        with self._lock:
            return scope in {'自己', '隊伍成員'}

    def _trusted_target_ids_unlocked(self) -> set[int]:
        self_id = self._resolved_self_id_unlocked()
        if self_id is None:
            return set()
        return {self_id} | (set() if self.core_monitoring_only else set(self.party_ids))

    def trusted_target_ids(self) -> set[int]:
        with self._lock:
            return self._trusted_target_ids_unlocked()

    def scoped_identity_signature(self) -> tuple[int | None, tuple[int, ...], int]:
        with self._lock:
            self_id = self._resolved_self_id_unlocked()
            party_ids = self._trusted_target_ids_unlocked() - ({self_id} if self_id is not None else set())
            return (self_id, tuple(sorted(party_ids)), self._scoped_identity_generation)

    def _clear_scoped_records_unlocked(self) -> None:
        self.party_ids.clear()
        self._scoped_party_name = b''
        self.target_names.clear()
        self.target_name_sources.clear()
        self.screen_player_ids.clear()
        self.status_target_ids.clear()
        self.status_target_counts.clear()
        self.self_hint_counts.clear()
        self.target_last_seen_ms.clear()
        self._scoped_identity_generation += 1

    def _prune_scoped_records_unlocked(self) -> None:
        allowed = self._trusted_target_ids_unlocked()
        self.screen_player_ids.clear()
        self.self_hint_counts.clear()
        self.status_target_ids.intersection_update(allowed)
        for mapping in (self.target_names, self.target_name_sources, self.status_target_counts, self.target_last_seen_ms):
            for target_id in list(mapping):
                if target_id not in allowed:
                    mapping.pop(target_id, None)

    @staticmethod
    def _valid_id(target_id: int) -> bool:
        return 0 < target_id <= 4294967295

    def consume(self, packets: list[ReplayPacket]) -> None:
        with self._lock:
            if self.processed_packets >= 0 and packets and (packets[-1].index < self.processed_packets):
                self.processed_packets = -1
                self.party_ids.clear()
                self.target_names.clear()
                self.target_name_sources.clear()
                self.screen_player_ids.clear()
                self.status_target_ids.clear()
                self.status_target_counts.clear()
                self.self_hint_counts.clear()
                self.target_last_seen_ms.clear()
                self.latest_timeline_ms = 0
                self._snapshot_self_id = None
                self._snapshot_self_name = ''
                self._packet_confirmed_self_id = None
                self._scoped_party_name = b''
                self._scoped_identity_generation += 1
            for packet in packets:
                if packet.index <= self.processed_packets:
                    continue
                self._consume_packet(packet)
                self.processed_packets = packet.index
                self.latest_timeline_ms = max(self.latest_timeline_ms, packet.timeline_ms)
            self._prune_target_records_unlocked()

    def _mark_seen(self, target_id: int, timeline_ms: int) -> None:
        if target_id not in self._trusted_target_ids_unlocked():
            return
        if self._valid_id(target_id):
            self.target_last_seen_ms[target_id] = max(timeline_ms, self.target_last_seen_ms.get(target_id, 0))

    def _set_target_name(self, target_id: int, target_name: str, source: str) -> None:
        if target_id not in self._trusted_target_ids_unlocked():
            return
        self.target_names[target_id] = target_name
        self.target_name_sources.setdefault(target_id, set()).add(source)

    def _remove_name_source(self, source: str) -> None:
        for target_id, sources in list(self.target_name_sources.items()):
            sources.discard(source)
            if not sources:
                self.target_name_sources.pop(target_id, None)
                self.target_names.pop(target_id, None)

    def _prune_target_records_unlocked(self) -> None:
        """限制異常錄影造成的人物集合持續成長；保留自身與隊伍資料。"""
        self._prune_scoped_records_unlocked()
        return

    def _consume_packet(self, packet: ReplayPacket) -> None:
        data = packet.data
        if len(data) < 2 or u16(data, 0) != packet.header:
            return
        if packet.header == self.SELF_ID:
            if len(data) == 6:
                target_id = u32(data, 2)
                if self._valid_id(target_id):
                    if target_id != self._resolved_self_id_unlocked():
                        self._clear_scoped_records_unlocked()
                        self._snapshot_self_id = None
                        self._snapshot_self_name = ''
                    self._packet_confirmed_self_id = target_id
                    if self._snapshot_self_name and target_id == self._snapshot_self_id and (target_id not in self.target_names):
                        self._set_target_name(target_id, self._snapshot_self_name, 'replay_snapshot')
                    self._mark_seen(target_id, packet.timeline_ms)
            return
        if packet.header == self.ACTOR_NAME_INFO:
            minimum_length = self.ACTOR_NAME_OFFSET + self.ACTOR_NAME_SIZE
            if len(data) >= minimum_length:
                target_id = u32(data, 2)
                target_name = decode_target_name(data[self.ACTOR_NAME_OFFSET:self.ACTOR_NAME_OFFSET + self.ACTOR_NAME_SIZE])
                resolved_self_id = self._resolved_self_id_unlocked()
                if self._valid_id(target_id) and target_name and (not self.core_monitoring_only or target_id == resolved_self_id):
                    self._set_target_name(target_id, target_name, 'actor_info')
                    self._mark_seen(target_id, packet.timeline_ms)
            return
        if packet.header in self.STATUS_HEADERS and len(data) >= 8:
            target_id = u32(data, 4)
            if not self.core_monitoring_only and self._valid_id(target_id) and (target_id in self._trusted_target_ids_unlocked()):
                self.status_target_ids.add(target_id)
                self.status_target_counts[target_id] += 1
                self._mark_seen(target_id, packet.timeline_ms)
        if packet.header == ACTOR_STATE_HEADER:
            actor_state = decode_actor_state_packet(data)
            if actor_state is not None and (not self.core_monitoring_only) and self._valid_id(actor_state.target_id) and (actor_state.target_id in self._trusted_target_ids_unlocked()):
                self.status_target_ids.add(actor_state.target_id)
                self.status_target_counts[actor_state.target_id] += 1
                self._mark_seen(actor_state.target_id, packet.timeline_ms)
        if packet.header == self.PARTY_WITHDRAW:
            if len(data) == 31 and data[30] in (0, 1):
                target_id = u32(data, 2)
                if target_id == self._resolved_self_id_unlocked():
                    self.party_ids.clear()
                    self._scoped_party_name = b''
                    self._scoped_identity_generation += 1
                else:
                    self.party_ids.discard(target_id)
                self._prune_scoped_records_unlocked()
            return
        if packet.header == self.PARTY_MEMBER_INFO and len(data) >= 10:
            resolved_self = self._resolved_self_id_unlocked()
            if len(data) != self.PARTY_MEMBER_PACKET_SIZE or resolved_self is None:
                return
            target_id = u32(data, 2)
            party_name = data[self.PARTY_MEMBER_PARTY_NAME_OFFSET:self.PARTY_MEMBER_NAME_OFFSET].split(b'\x00', 1)[0]
            if not self._valid_id(target_id) or not party_name.strip():
                return
            if self._scoped_party_name and party_name != self._scoped_party_name:
                if target_id != resolved_self:
                    return
                self.party_ids.clear()
                self._scoped_identity_generation += 1
                self._prune_scoped_records_unlocked()
            if not self.core_monitoring_only:
                if target_id not in self.party_ids and len(self.party_ids) >= self.MAX_PARTY_TARGETS:
                    return
                self._scoped_party_name = party_name
                self.party_ids.add(target_id)
            name_start = self.PARTY_MEMBER_NAME_OFFSET
            target_name = decode_target_name(data[name_start:name_start + self.PARTY_NAME_SIZE])
            if target_name:
                self._set_target_name(target_id, target_name, 'party')
            self._mark_seen(target_id, packet.timeline_ms)
            return
        if packet.header == self.PARTY_INFO and len(data) >= 28:
            declared_length = u16(data, 2)
            start = 4 + self.PARTY_NAME_SIZE
            if declared_length < start or declared_length > len(data) or declared_length != len(data) or ((declared_length - start) % self.PARTY_MEMBER_SIZE != 0) or ((declared_length - start) // self.PARTY_MEMBER_SIZE > self.MAX_PARTY_TARGETS):
                return
            members: dict[int, int] = {}
            for offset in range(start, declared_length, self.PARTY_MEMBER_SIZE):
                target_id = u32(data, offset)
                if not self._valid_id(target_id) or target_id in members:
                    return
                members[target_id] = offset + self.PARTY_LIST_MEMBER_NAME_OFFSET
            resolved_self = self._resolved_self_id_unlocked()
            party_name = data[4:start].split(b'\x00', 1)[0]
            if resolved_self is None or resolved_self not in members or (not party_name.strip()):
                return
            if not self.core_monitoring_only:
                if self._scoped_party_name != party_name:
                    self.party_ids.clear()
                    self._scoped_identity_generation += 1
                    self._prune_scoped_records_unlocked()
                self._scoped_party_name = party_name
                self.party_ids = set(members)
                self._prune_scoped_records_unlocked()
            if self.core_monitoring_only:
                resolved_self_id = self._resolved_self_id_unlocked()
                if resolved_self_id is None:
                    return
                name_start = members.get(resolved_self_id)
                target_name = decode_target_name(data[name_start:name_start + self.PARTY_NAME_SIZE]) if name_start is not None else None
                if target_name:
                    self._set_target_name(resolved_self_id, target_name, 'self')
                return
            self.party_ids = set(members)
            for target_id, name_start in members.items():
                self._mark_seen(target_id, packet.timeline_ms)
                target_name = decode_target_name(data[name_start:name_start + self.PARTY_NAME_SIZE])
                if target_name:
                    self._set_target_name(target_id, target_name, 'party')
            return
        if packet.header in self.UNIT_PACKETS and len(data) >= 9:
            target_id = u32(data, 5)
            if data[4] == 0 and target_id in self._trusted_target_ids_unlocked():
                name_start = self.UNIT_NAME_OFFSETS.get(packet.header)
                if name_start is not None and len(data) > name_start:
                    target_name = decode_target_name(data[name_start:])
                    if target_name:
                        self._set_target_name(target_id, target_name, 'unit')
                self._mark_seen(target_id, packet.timeline_ms)
            return
        if packet.header == self.USE_SKILL and len(data) >= 17:
            return

    @property
    def self_id(self) -> int | None:
        with self._lock:
            return self._resolved_self_id_unlocked()

    def self_candidate_ids(self) -> list[int]:
        with self._lock:
            return [target_id for target_id, _count in self.self_hint_counts.most_common()]

    @property
    def self_id_source(self) -> str:
        with self._lock:
            if self._packet_confirmed_self_id is not None:
                return 'RRF 過傳點自動確認'
            if self._snapshot_self_id is not None:
                return 'RRF 錄影資料自動確認'
            return '等待本次 RRF 確認自身'

    @property
    def packet_confirmed_self_id(self) -> int | None:
        with self._lock:
            return self._packet_confirmed_self_id

    @property
    def snapshot_self_id(self) -> int | None:
        with self._lock:
            return self._snapshot_self_id

    @property
    def snapshot_self_name(self) -> str:
        with self._lock:
            return self._snapshot_self_name

    def relation_for(self, target_id: int) -> str:
        with self._lock:
            self_id = self._resolved_self_id_unlocked()
            if self_id == target_id:
                return '自己'
            if target_id not in self._trusted_target_ids_unlocked():
                return '其他目標'
            if target_id in self.party_ids:
                return '隊伍成員'
            if target_id in self.screen_player_ids:
                return '畫面成員'
            return '其他目標'

    def known_target_ids(self) -> set[int]:
        with self._lock:
            return self._trusted_target_ids_unlocked()

    def recent_target_ids(self, active_status_target_ids: set[int], window_ms: int=TARGET_RECENT_WINDOW_MS) -> set[int]:
        """回傳最近活動目標；保留隊伍與目前仍啟用狀態，避免短暫沒封包就消失。"""
        with self._lock:
            return self._trusted_target_ids_unlocked()

    def target_name(self, target_id: int) -> str | None:
        with self._lock:
            if target_id not in self._trusted_target_ids_unlocked():
                return None
            return self.target_names.get(target_id)

class RrfMonitorApp(tk.Tk):

    def __init__(self) -> None:
        startup_timeline = StartupTimeline()
        super().__init__()
        self.startup_timeline = startup_timeline
        startup_timeline.mark('Tk')
        self.title('RO RRF 即時狀態監控器｜1.6.5')
        self.geometry('1080x780')
        self.minsize(900, 620)
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.config_path = APPLICATION_DIR / local_data_filename('config.json')
        self.startup_log_path = APPLICATION_DIR / local_data_filename('startup.log')
        self.settings = self.load_settings()
        startup_timeline.mark('設定')
        self.settings_import_pending_restart = False
        self._last_saved_settings: dict[str, object] | None = None
        self.core_monitoring_only = bool(self.settings.get('core_monitoring_only', CORE_MONITORING_ONLY_DEFAULT))
        self.status_data_cache_path = APPLICATION_DIR / local_data_filename('status_data_cache.json')
        self.client_data_cache_path = APPLICATION_DIR / local_data_filename('client_data_cache.json')
        self.unknown_journal_path = APPLICATION_DIR / local_data_filename('unknown_data_journal.json')
        self.ro_install_dir = Path(self.settings.get('ro_install_dir', str(DEFAULT_RO_DIR)))
        self.auto_client_data = False
        self.ro_data_ready = True
        self.status_data_signatures: list[dict[str, object]] = []
        self.client_data_signatures: list[dict[str, object]] = []
        set_status_data(dict(BUNDLED_EFST_NAMES), dict(BUNDLED_EFST_GROUPS), BUNDLED_STATUS_DATA_SOURCE)
        set_client_catalog(BUNDLED_CLIENT_CATALOG)
        cached_status_data: StatusDataResult | None = None
        startup_status_report = '已載入內建狀態資料'
        self.client_data_report = BUNDLED_CLIENT_CATALOG.report
        startup_timeline.mark('內建資料')
        self.status_data_report = startup_status_report + '；' + ('核心模式只監控自身，停用非必要目標掃描與背景校對' if self.core_monitoring_only else '未知資料只做定向低負載辨識；完整校對在關閉或手動執行')
        if EXTENDED_STATUS_DATA_REPORT.startswith('已載入'):
            self.status_data_report += f'（{EXTENDED_STATUS_DATA_REPORT}）'
        self.status_data_report_var = tk.StringVar(value=self.status_data_report)
        self.client_data_report_var = tk.StringVar(value=self.client_data_report)
        self.data_status_summary_var = tk.StringVar(value='可使用｜內建資料')
        self.catalog_manifest_valid = True
        self.startup_catalog_check_thread: threading.Thread | None = None
        self.parser = RrfParser()
        self.incremental_parser = RrfIncrementalParser(self.parser)
        self.tracker = StatusTracker()
        self.pet_tracker = PetTracker()
        self.pet_alert_controller = PetAlertController()
        self.pet_alert_lock = threading.Lock()
        self.pet_alert_events = deque(maxlen=8)
        self.pet_live_observation_key = None
        self.pet_overlay: PetOverlay | None = None
        self.pet_overlay_settings = {key: self.settings.get(key, value) for key, value in DEFAULT_CONFIG.items() if key.startswith('pet_overlay_')}
        self.target_tracker = TargetTracker()
        self.monitor_session = MonitorSession()
        self.monitor_snapshot = None
        self.last_resolved_self_identity: tuple[int, str] | None = None
        self.target_tracker.set_core_monitoring_only(self.core_monitoring_only)
        configured_self_name = str(self.settings.get('self_target_name', '') or '').strip()
        self.target_tracker.set_configured_self_name(configured_self_name or None)
        configured_self_id = self.parse_target_id(self.settings.get('self_target_id'))
        self.target_tracker.set_configured_self_id(configured_self_id)
        if self.core_monitoring_only:
            self.tracker.set_allowed_target_ids({configured_self_id} if configured_self_id is not None else set())
        self.pet_tracker.set_selected_pet_id(None)
        self.running = True
        self.monitoring_active = self.monitor_session.active_event
        self.monitor_reset_requested = self.monitor_session.reset_event
        self.monitor_wake_event = self.monitor_session.wake_event
        self.monitoring_has_started = False
        self.close_after_data_load = False
        self.close_finalized = False
        self.close_choice_dialog: tk.Toplevel | None = None
        self.close_progress_dialog: tk.Toplevel | None = None
        self.close_progress_heading_var = tk.StringVar(value='正在處理資料')
        self.close_progress_detail_var = tk.StringVar(value='完成前可取消並返回監視器，或直接關閉。')
        self.close_progress_started_at: float | None = None
        self.close_progress_elapsed_var = tk.StringVar(value='已用時間：0 秒')
        self.close_progress_job: str | None = None
        self.close_unknown_processed_keys: set[tuple[str, int]] = set()
        self.catalog_cancel_requested = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        self.data_load_thread: threading.Thread | None = None
        self.unknown_resolve_thread: threading.Thread | None = None
        self.catalog_worker_client = CatalogWorkerClient()
        self.data_load_lock = threading.Lock()
        self.client_data_io_lock = threading.Lock()
        self.unknown_journal = UnknownJournal(self.unknown_journal_path, max_records=MAX_UNKNOWN_IDS, batch_limit=UNKNOWN_MEMORY_BATCH_LIMIT)
        self.unknown_journal.resolve((record.key for record in self.unknown_journal.snapshot()))
        self.unknown_journal.flush()
        saved_unknowns = self.unknown_journal.snapshot()
        self.pending_client_item_ids: set[int] = {record.value_id for record in saved_unknowns if record.kind == 'item'}
        self.pending_unknown_status_ids: set[int] = {record.value_id for record in saved_unknowns if record.kind == 'status'}
        self.background_seen_status_ids: set[int] = set(self.pending_unknown_status_ids)
        self.background_seen_item_ids: set[int] = set(self.pending_client_item_ids)
        self.unknown_status_index_attempted = False
        self.unknown_status_index_attempt_count = 0
        self.unknown_status_retry_job: str | None = None
        self.unknown_last_log_at = 0.0
        self.unknown_controls_update_queued = False
        self.auto_resolve_unknown = bool(self.settings.get('auto_resolve_unknown', True))
        self.config_lock = threading.Lock()
        self.message_lock = threading.Lock()
        self.ui_callback_queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=MAX_UI_CALLBACK_QUEUE)
        self.ui_critical_lock = threading.Lock()
        self.ui_critical_callbacks: deque[Callable[[], None]] = deque(maxlen=32)
        self.ui_callback_overflow_count = 0
        self.ui_callback_overflow_logged = False
        self.ui_progress_lock = threading.Lock()
        self.pending_ui_progress: tuple[float, str] | None = None
        self.ui_progress_callback_queued = False
        self.monitor_message = '待機｜完成設定後按開始監控'
        self.current_path: Path | None = None
        self.last_signature: tuple[int, int] | None = None
        self.last_rrf_data_monotonic: float | None = None
        self.last_rrf_packet_count = 0
        self.last_rrf_new_packet_count = 0
        self.latest_file_cache_dir = ''
        self.latest_file_cache_path: Path | None = None
        self.latest_file_scan_at = 0.0
        self.live_replay_discovery = LiveReplayDiscovery(active_grace_seconds=REPLAY_ACTIVE_GRACE_SECONDS)
        self.settings_save_job: str | None = None
        self.target_options_next_refresh_at = 0.0
        self.alerted: set[tuple[tuple[int, int], str]] = set()
        self.recent_apply_alerts: dict[tuple[int, int], float] = {}
        self.recent_event_history: deque[str] = deque(maxlen=5)
        self.last_apply_alert_at = 0.0
        self.last_monitor_error = ''
        self.last_ui_refresh_error = ''
        self.sync_state_lock = threading.Lock()
        self.live_sync_path: Path | None = None
        self.live_sync_offset_ms: int | None = None
        self.status_checks: dict[int, tk.BooleanVar] = {}
        initial_scope_settings = resolved_target_scope_settings(self.settings.get('target_scope_enabled'), core_monitoring_only=self.core_monitoring_only)
        self.target_scope_status_ids: dict[str, set[int]] = resolved_target_scope_status_ids(self.settings.get('target_scope_status_ids'), self.settings.get('alert_status_ids', []), initial_scope_settings, core_monitoring_only=self.core_monitoring_only)
        self.status_edit_scope_var = tk.StringVar(value='自己')
        self.status_edit_scope = '自己'
        self.status_selected_ids: set[int] = set(self.target_scope_status_ids['自己'])
        self.status_scope_edit_buttons: dict[str, ttk.Radiobutton] = {}
        self.status_alert_rules: dict[int, dict[str, bool]] = self.load_status_alert_rules()
        self.status_alert_rule_touched: set[int] = set(self.status_alert_rules)
        raw_alert_policies = self.settings.get('alert_policies', {})
        if not isinstance(raw_alert_policies, dict):
            raw_alert_policies = {}
        alert_policy_seed = AlertPolicyResolver(scope_status_rules=raw_alert_policies.get('scope_status_rules', {}), target_overrides=raw_alert_policies.get('target_overrides', {}))
        self.scope_status_alert_rules = alert_policy_seed.scope_status_rules
        self.target_alert_overrides = alert_policy_seed.target_overrides
        self.status_alert_rule_vars: dict[int, dict[str, tk.BooleanVar]] = {}
        self.status_checkbuttons: dict[int, ttk.Checkbutton] = {}
        self.status_option_ids: set[int] = set()
        self.status_option_source_headers: dict[int, int] = {}
        self.status_option_signature: tuple[object, ...] | None = None
        self.status_option_generation = 0
        self.readable_status_generation = -1
        self.readable_status_ids_cache: frozenset[int] = frozenset()
        self.status_rebuild_job: str | None = None
        self.status_filter_job: str | None = None
        self.status_rebuild_token = 0
        self.status_options_inner: ttk.Frame | None = None
        self.status_options_empty: ttk.Label | None = None
        self.status_library_tab: ttk.Frame | None = None
        self.status_library_ui_built = False
        self.status_library_widgets_ready = False
        self.status_library_tree: ttk.Treeview | None = None
        self.status_library_records: dict[int, StatusLibraryRecord] = {}
        self.status_library_index = StatusSearchIndex()
        self.status_library_name_counts: dict[str, int] = {}
        self.status_tree_row_ids: dict[int, str] = {}
        self.status_tree_status_ids: dict[str, int] = {}
        self.status_tree_row_values: dict[int, tuple[object, ...]] = {}
        self.status_tree_order_ids: list[int] = []
        self.status_current_page_ids: tuple[int, ...] = ()
        self.status_detail_status_id: int | None = None
        self.status_detail_name_var = tk.StringVar(value='請從清單選擇狀態')
        self.status_detail_meta_var = tk.StringVar(value='選取後可設定重點狀態與聲音')
        self.status_detail_selected_var = tk.BooleanVar(value=False)
        self.status_detail_alert_vars = {key: tk.BooleanVar(value=False) for key in ALERT_RULE_KEYS}
        self.status_detail_controls: list[ttk.Checkbutton] = []
        self.status_index_mode_var = tk.StringVar(value=STATUS_LIBRARY_INDEX_MODES[0])
        self.status_category_var = tk.StringVar(value='經驗')
        self.status_consumable_subcategory_var = tk.StringVar(value=CONSUMABLE_SUBCATEGORY_ORDER[0])
        self.status_job_var = tk.StringVar(value='全部職業')
        self.status_job_value_map: dict[str, str] = {'全部職業': '全部職業'}
        self.status_job_effect_var = tk.StringVar(value=JOB_EFFECT_FILTER_ORDER[0])
        self.status_function_var = tk.StringVar(value=STATUS_FUNCTION_FILTER_ORDER[0])
        self.status_job_combobox: ttk.Combobox | None = None
        self.status_job_effect_combobox: ttk.Combobox | None = None
        self.status_consumable_combobox: ttk.Combobox | None = None
        self.status_function_combobox: ttk.Combobox | None = None
        self.status_category_buttons: dict[str, ttk.Button] = {}
        self.status_page_index = 0
        self.status_page_info_var = tk.StringVar(value='尚未載入狀態清單')
        self.status_page_previous_button: ttk.Button | None = None
        self.status_page_next_button: ttk.Button | None = None
        self.status_unresolved_ids: set[int] = set()
        self.target_scope_vars: dict[str, tk.BooleanVar] = {}
        self.target_scope_display_mode_vars: dict[str, tk.StringVar] = {}
        self.target_scope_display_mode_boxes: dict[str, ttk.Combobox] = {}
        self.target_sound_vars: dict[str, tk.BooleanVar] = {}
        self.target_sound_checkbuttons: dict[str, ttk.Checkbutton] = {}
        self.target_scope_status_buttons: dict[str, ttk.Button] = {}
        self.target_tree: ttk.Treeview | None = None
        self.target_tree_row_ids: dict[int, str] = {}
        self.target_tree_row_values: dict[int, tuple[object, ...]] = {}
        self.target_tree_order_ids: list[int] = []
        self.target_detail_target_id: int | None = None
        self.target_detail_var = tk.StringVar(value='開始監控後顯示人物')
        self.target_action_buttons: list[ttk.Button] = []
        self.target_status_overrides: dict[int, set[int]] = self.load_target_status_overrides()
        self.target_display_mode_overrides: dict[int, str] = self.load_target_display_mode_overrides()
        self.target_option_ids: set[int] = set()
        self.target_option_relations: dict[int, str] = {}
        self.target_option_names: dict[int, str] = {}
        self.target_option_recent_ids: set[int] = set()
        self.target_filter_job: str | None = None
        self.file_entry: ttk.Entry | None = None
        self.file_button: ttk.Button | None = None
        self.start_monitor_button: ttk.Button | None = None
        self.stop_monitor_button: ttk.Button | None = None
        self.rescan_button: ttk.Button | None = None
        self.status_reload_button: ttk.Button | None = None
        self.unknown_review_button: ttk.Button | None = None
        self.catalog_cancel_button: ttk.Button | None = None
        self.status_reload_progressbar: ttk.Progressbar | None = None
        self.pet_ui_built = False
        self.settings_tab: ttk.Frame | None = None
        self.settings_ui_built = False
        self.overlay: tk.Toplevel | None = None
        self.overlay_canvas: tk.Canvas | None = None
        self.overlay_drag_offset: tuple[int, int] | None = None
        self.overlay_drag_position: tuple[int, int] | None = None
        self.overlay_drag_job: str | None = None
        self.overlay_save_job: str | None = None
        self.overlay_summary_text = ''
        self.overlay_pet_visible = False
        self.overlay_render_groups: tuple[object, ...] = ()
        self.overlay_render_pet: tuple[object, ...] | None = None
        self.overlay_render_signature: tuple[object, ...] | None = None
        self.overlay_structure_signature: tuple[object, ...] | None = None
        self.overlay_target_items: dict[int, int] = {}
        self.overlay_row_items: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        self.overlay_pet_items: dict[int, int] = {}
        self.overlay_target_text_limits: dict[int, int] = {}
        self.overlay_row_text_limits: dict[tuple[int, int], int] = {}
        self.overlay_pet_text_limit = 12
        self.overlay_font_cache: tuple[int, tkfont.Font, tkfont.Font] | None = None
        self.overlay_natural_height = OVERLAY_MIN_HEIGHT
        self.overlay_scroll_offset = 0
        self.overlay_scroll_max = 0
        self.overlay_resize_mode = ''
        self.overlay_resize_origin: tuple[int, int] | None = None
        self.overlay_resize_geometry: tuple[int, int, int, int] | None = None
        self.overlay_pending_size: tuple[int, int] | None = None
        self.overlay_control_regions: dict[str, tuple[int, int, int, int]] = {}
        self.overlay_render_job: str | None = None
        self.overlay_geometry_programmatic = False
        self.overlay_width = self.safe_dimension(self.settings.get('overlay_width'), OVERLAY_DEFAULT_WIDTH, OVERLAY_MIN_WIDTH, 1600)
        self.overlay_height = self.safe_dimension(self.settings.get('overlay_height'), OVERLAY_DEFAULT_HEIGHT, OVERLAY_MIN_HEIGHT, 1200)
        raw_overlay_x = self.settings.get('overlay_x')
        raw_overlay_y = self.settings.get('overlay_y')
        self.overlay_x = self.safe_coordinate(raw_overlay_x)
        self.overlay_y = self.safe_coordinate(raw_overlay_y)
        self.overlay_anchor_y = self.overlay_y
        self.overlay_auto_height = bool(self.settings.get('overlay_auto_height', True))
        self.overlay_locked = bool(self.settings.get('overlay_locked', False))
        self.overlay_opacity = max(OVERLAY_MIN_OPACITY, min(OVERLAY_MAX_OPACITY, self.safe_float(self.settings.get('overlay_opacity', OVERLAY_DEFAULT_OPACITY), OVERLAY_DEFAULT_OPACITY)))
        self.overlay_font_size = self.safe_dimension(self.settings.get('overlay_font_size'), OVERLAY_DEFAULT_FONT_SIZE, OVERLAY_MIN_FONT_SIZE, OVERLAY_MAX_FONT_SIZE)
        self.tree_column_widths = self.load_tree_column_widths(self.settings.get('tree_column_widths'))
        self.tree_row_ids: dict[tuple[int, int], str] = {}
        self.tree_row_values: dict[tuple[int, int], tuple[object, ...]] = {}
        self.tree_order_keys: list[tuple[int, int]] = []
        self.file_var = tk.StringVar(value=self.settings.get('selected_file', ''))
        self.dir_var = tk.StringVar(value=self.settings.get('replay_dir', str(DEFAULT_REPLAY_DIR)))
        self.ro_dir_var = tk.StringVar(value=str(self.ro_install_dir))
        self.auto_latest_var = tk.BooleanVar(value=bool(self.settings.get('auto_latest', True)))
        self.status_reload_progress_var = tk.DoubleVar(value=0)
        self.status_reload_progress_text_var = tk.StringVar(value='尚未執行手動校對')
        self.unknown_review_button_var = tk.StringVar(value='比對 0 筆未知資料')
        self.unknown_data_summary_var = tk.StringVar(value='0 筆')
        self.yellow_var = tk.StringVar(value=str(self.settings.get('yellow_seconds', 30)))
        self.red_var = tk.StringVar(value=str(self.settings.get('red_seconds', 15)))
        self.sync_var = tk.StringVar(value=str(self.settings.get('sync_offset_seconds', 5)))
        self.auto_sync_var = tk.BooleanVar(value=bool(self.settings.get('auto_sync', True)))
        self.interval_var = tk.StringVar(value=str(self.settings.get('poll_seconds', 0.5)))
        self.status_filter_var = tk.StringVar()
        self.show_all_statuses_var = tk.BooleanVar(value=bool(self.settings.get('show_all_statuses', False)))
        self.status_scope_text_var = tk.StringVar()
        self.status_category_summary_var = tk.StringVar()
        self.status_selection_summary_var = tk.StringVar()
        self.selected_statuses_var = tk.StringVar(value='目前勾選：尚未設定')
        self.latest_event_var = tk.StringVar(value='尚無新套用事件')
        self.status_view_var = tk.StringVar(value='啟用中')
        self.status_sort_var = tk.StringVar(value='目標優先')
        self.status_table_count_var = tk.StringVar(value='目前 0 筆')
        self.show_technical_columns_var = tk.BooleanVar(value=bool(self.settings.get('show_technical_columns', False)))
        self.monitor_summary_var = tk.StringVar(value='待機｜完成設定後按開始監控')
        self.monitor_control_var = tk.StringVar(value='待機｜完成設定後按開始監控')
        self.overlay_summary_var = tk.StringVar(value='等待狀態資料')
        self.onboarding_var = tk.StringVar(value='')
        self.replay_source_state_var = tk.StringVar(value='')
        self.ro_source_state_var = tk.StringVar(value='')
        self.self_id_state_var = tk.StringVar(value='')
        self.tab_label_signature: tuple[object, ...] | None = None
        self.onboarding_next_refresh_at = 0.0
        self.onboarding_box: ttk.LabelFrame | None = None
        resolved_modes = resolved_target_display_modes(self.settings.get('target_scope_display_modes'), self.settings.get('target_scope_enabled'), core_monitoring_only=self.core_monitoring_only)
        for scope in TARGET_SCOPE_ORDER:
            self.target_scope_display_mode_vars[scope] = tk.StringVar(value=resolved_modes[scope])
            self.target_scope_vars[scope] = tk.BooleanVar(value=resolved_modes[scope] != TARGET_DISPLAY_MODE_OFF)
        self.target_scope_display_modes_snapshot = dict(resolved_modes)
        self.target_display_mode_overrides_snapshot = dict(self.target_display_mode_overrides)
        resolved_sound_scopes = resolved_target_sound_settings(self.settings.get('target_sound_enabled'), core_monitoring_only=self.core_monitoring_only)
        for scope in TARGET_SCOPE_ORDER:
            self.target_sound_vars[scope] = tk.BooleanVar(value=resolved_sound_scopes[scope])
        self.target_tracker.set_enabled_scopes((scope for scope, variable in self.target_scope_vars.items() if variable.get()))
        self.self_target_name_var = tk.StringVar(value=configured_self_name)
        self.self_target_id_var = tk.StringVar(value=self.format_target_id(configured_self_id or 0) if configured_self_id else '')
        saved_policy_resolver = PolicyResolver.from_settings(self.settings)
        self.target_policy_resolver_snapshot = saved_policy_resolver
        self.target_rule_name_var = tk.StringVar()
        self.target_rule_mode_var = tk.StringVar(value=TARGET_DISPLAY_MODE_ALL)
        self.target_rule_summary_var = tk.StringVar(value=f'已保存人物名稱規則：{len(saved_policy_resolver.name_overrides)} 條')
        self.target_filter_var = tk.StringVar()
        target_view = self.settings.get('target_view', TARGET_VIEW_MODES[0])
        target_view = {'全部解析': '全部人物', '最近活動': '最近出現'}.get(target_view, target_view)
        if target_view not in TARGET_VIEW_MODES:
            target_view = TARGET_VIEW_MODES[0]
        self.target_view_var = tk.StringVar(value=target_view)
        self.target_scope_text_var = tk.StringVar()
        self.overlay_enabled_var = tk.BooleanVar(value=bool(self.settings.get('overlay_enabled', True)))
        self.overlay_opacity_var = tk.DoubleVar(value=self.overlay_opacity)
        self.overlay_font_size_var = tk.IntVar(value=self.overlay_font_size)
        self.overlay_auto_height_var = tk.BooleanVar(value=self.overlay_auto_height)
        self.overlay_locked_var = tk.BooleanVar(value=self.overlay_locked)
        self.settings_category_frames: dict[str, ttk.Frame] = {}
        self.settings_category_buttons: dict[str, ttk.Button] = {}
        self.settings_category_var = tk.StringVar(value='一般')
        migration_report = self.settings_migration_report
        self.settings_import_var = tk.StringVar(value='舊設定已轉換完成' if migration_report.migrated else '可匯入舊版本設定')
        self.pet_monitor_enabled_var = tk.BooleanVar(value=bool(self.settings.get('pet_monitor_enabled', True)))
        configured_pet_id = self.pet_tracker.selected_pet_id
        self.pet_selected_id_var = tk.StringVar(value=self.format_target_id(configured_pet_id) if configured_pet_id is not None else '')
        self.pet_alert_enabled_var = tk.BooleanVar(value=bool(self.settings.get('pet_alert_enabled', True)))
        pet_threshold = max(0, min(100, int(self.safe_float(self.settings.get('pet_alert_threshold', PET_ALERT_DEFAULT_THRESHOLD), PET_ALERT_DEFAULT_THRESHOLD))))
        self.pet_alert_threshold_var = tk.StringVar(value=str(pet_threshold))
        self.pet_alert_enabled = bool(self.pet_alert_enabled_var.get())
        self.pet_alert_threshold = pet_threshold
        self.pet_overlay_enabled_var = tk.BooleanVar(value=bool(self.settings.get('pet_overlay_enabled', True)))
        self.pet_overlay_locked_var = tk.BooleanVar(value=bool(self.settings.get('pet_overlay_locked', False)))
        self.pet_overlay_font_size_var = tk.IntVar(value=max(10, min(20, int(self.safe_float(self.settings.get('pet_overlay_font_size', 12), 12)))))
        self.pet_overlay_width_var = tk.IntVar(value=max(220, min(600, int(self.safe_float(self.settings.get('pet_overlay_width', 280), 280)))))
        self.pet_overlay_opacity_var = tk.DoubleVar(value=max(0.4, min(1.0, self.safe_float(self.settings.get('pet_overlay_opacity', 0.9), 0.9))))
        self.pet_search_var = tk.StringVar()
        self.pet_catalog_count_var = tk.StringVar()
        self.pet_selection_summary_var = tk.StringVar()
        self.pet_status_name_var = tk.StringVar(value='尚未指定寵物')
        self.pet_level_var = tk.StringVar(value='等級：—')
        self.pet_species_var = tk.StringVar(value='種類：等待資料')
        self.pet_food_var = tk.StringVar(value='食物：等待資料')
        self.pet_satiety_var = tk.StringVar(value='飽食度：等待 RRF')
        self.pet_intimacy_var = tk.StringVar(value='親密度：等待 RRF')
        self.pet_state_var = tk.StringVar(value='狀態：等待寵物封包')
        self.pet_source_var = tk.StringVar(value='資料來源：等待已核對的寵物封包')
        self.pet_hint_var = tk.StringVar(value='餵食或過傳點後會自動辨識寵物。')
        self.pet_alert_state_var = tk.StringVar(value='飽食度提醒：尚未取得數值')
        self.pet_panel_render_key: tuple[object, ...] | None = None
        self.pet_catalog_render_key: tuple[object, ...] | None = None
        self.pet_tab: ttk.Frame | None = None
        self.pet_catalog_tree: ttk.Treeview | None = None
        self.pet_status_card: ttk.LabelFrame | None = None
        self.yellow_sound_var = tk.BooleanVar(value=bool(self.settings.get('yellow_sound_enabled', True)))
        self.red_sound_var = tk.BooleanVar(value=bool(self.settings.get('red_sound_enabled', True)))
        self.apply_sound_var = tk.BooleanVar(value=bool(self.settings.get('apply_sound_enabled', True)))
        sound_mode = self.settings.get('sound_mode', SOUND_MODES[0])
        self.sound_mode_var = tk.StringVar(value=sound_mode if sound_mode in SOUND_MODES else SOUND_MODES[0])
        self.sound_file_var = tk.StringVar(value=str(self.settings.get('sound_file', '')))
        volume = max(0, min(100, int(self.safe_float(self.settings.get('sound_volume', 100), 100))))
        self.sound_volume_var = tk.IntVar(value=volume)
        self.volume_text_var = tk.StringVar(value=f'{volume}%')
        self.monitor_dir = self.dir_var.get()
        self.monitor_file = self.file_var.get()
        self.auto_latest = self.auto_latest_var.get()
        self.monitor_interval = max(0.2, self.safe_float(self.interval_var.get(), 0.5))
        self.pet_monitor_enabled = bool(self.pet_monitor_enabled_var.get())
        self.sync_tracker_status_filter()
        self.build_ui()
        startup_timeline.mark('主畫面')
        self.after(50, self.start_startup_catalog_check)
        self.update_onboarding()
        self.update_tab_labels()
        slow_startup = startup_timeline.slow_summary()
        if slow_startup:
            startup_mode = '核心模式只追蹤自身狀態' if self.core_monitoring_only else '完整檢查只由設定頁手動執行'
            self.log_event(f'{slow_startup}；{startup_mode}')
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.update_monitor_control_ui()
        self.after(UI_CALLBACK_INTERVAL_MS, self.process_ui_callbacks)
        self.after(UI_REFRESH_INTERVAL_MS, self.refresh_ui)

    def start_startup_catalog_check(self) -> None:
        """視窗顯示後才做一次輕量資料版本檢查，不啟動完整校對。"""
        thread = self.__dict__.get('startup_catalog_check_thread')
        if thread is not None and thread.is_alive():
            return
        self.data_status_summary_var.set('檢查本機資料版本…')
        self.startup_catalog_check_thread = threading.Thread(target=self._startup_catalog_check_worker, daemon=True)
        self.startup_catalog_check_thread.start()

    @staticmethod
    def _cache_has_different_signature(cache_path: Path, ro_dir: Path, signatures: list[dict[str, object]]) -> bool:
        """快取屬於同一套台版 RO，但官方檔案簽章已變更。"""
        if not cache_path.is_file():
            return False
        try:
            payload = read_json_object(cache_path)
            return bool(payload.get('game_id') == TWRO_GAME_ID and payload.get('ro_dir') == str(ro_dir.resolve()) and isinstance(payload.get('signatures'), list) and (payload.get('signatures') != signatures))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _startup_catalog_check_worker(self) -> None:
        ro_dir = Path(self.ro_install_dir)
        status_signatures: list[dict[str, object]] = []
        client_signatures: list[dict[str, object]] = []
        cached_status: StatusDataResult | None = None
        cached_client: ClientCatalogResult | None = None
        manifest_errors: tuple[str, ...] = ()
        client_changed = False
        error_text = ''
        try:
            manifest_path = APPLICATION_DIR / 'data' / 'catalog_manifest.json'
            if manifest_path.is_file():
                manifest = CatalogManifest.from_payload(read_json_object(manifest_path))
                _valid, manifest_errors = manifest.validate_files(APPLICATION_DIR / 'data')
            status_signatures = _grf_signatures(ro_dir)
            client_signatures = _client_data_signatures(ro_dir, grf_signatures=status_signatures)
            cached_status = _read_status_cache(self.status_data_cache_path, ro_dir, status_signatures)
            cached_client = _read_client_catalog_cache(self.client_data_cache_path, ro_dir, client_signatures)
            client_changed = self._cache_has_different_signature(self.status_data_cache_path, ro_dir, status_signatures) or self._cache_has_different_signature(self.client_data_cache_path, ro_dir, client_signatures)
        except Exception as exc:
            error_text = f'{type(exc).__name__}: {exc}'
        self.enqueue_ui_callback(lambda: self._finish_startup_catalog_check(status_signatures, client_signatures, cached_status, cached_client, manifest_errors, client_changed, error_text), critical=True)

    def _finish_startup_catalog_check(self, status_signatures: list[dict[str, object]], client_signatures: list[dict[str, object]], cached_status: StatusDataResult | None, cached_client: ClientCatalogResult | None, manifest_errors: tuple[str, ...], client_changed: bool, error_text: str) -> None:
        self.startup_catalog_check_thread = None
        if not self.running:
            return
        self.status_data_signatures = status_signatures
        self.client_data_signatures = client_signatures
        self.catalog_manifest_valid = not manifest_errors
        if cached_status is not None:
            set_status_data(cached_status.names, cached_status.groups, cached_status.source)
            self.status_data_report = cached_status.report
        if cached_client is not None:
            set_client_catalog(cached_client)
            self.client_data_report = cached_client.report
            self.client_data_report_var.set(self.client_data_report)
        if cached_status is not None or cached_client is not None:
            self.status_option_generation += 1
            self.status_option_signature = None
        if manifest_errors:
            self.data_status_summary_var.set('內建資料檢查失敗')
            self.log_event('內建資料檔案檢查失敗：' + '；'.join(manifest_errors))
        elif client_changed:
            self.data_status_summary_var.set('RO 資料可能已更新｜可手動完整校對')
            self.log_event('偵測到 RO 主程式資料版本變更；等待使用者手動完整校對')
        elif cached_status is not None or cached_client is not None:
            self.data_status_summary_var.set('可使用｜本機資料')
        else:
            self.data_status_summary_var.set('可使用｜內建資料')
        if error_text:
            self.log_event('啟動資料版本檢查未完成：' + error_text)
        self.status_data_report_var.set(self.status_data_report)
        self.update_unknown_review_controls()
        self.update_onboarding()

    def load_settings(self) -> dict:
        saved: dict[str, object] | None = None
        if self.config_path.exists():
            try:
                with self.config_path.open('r', encoding='utf-8') as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    saved = payload
            except (OSError, ValueError):
                pass
        source = saved if saved is not None else dict(DEFAULT_CONFIG)
        migrated, report = migrate_settings_to_v2(source)
        merged = dict(DEFAULT_CONFIG)
        merged.update(migrated)
        merged.update(legacy_runtime_fields(merged))
        if 'alert_status_ids' not in merged and 'status_ids' in merged:
            merged['alert_status_ids'] = merged.get('status_ids', [])
        if 'red_seconds' not in merged and 'alert_seconds' in merged:
            merged['red_seconds'] = merged.get('alert_seconds', 15)
        self.settings_migration_report = report
        return scoped_settings(merged)

    def import_legacy_settings(self) -> None:
        """由使用者明確選擇設定檔；不搜尋其他資料夾。"""
        source_name = filedialog.askopenfilename(title='選擇舊版本設定檔', filetypes=(('JSON 設定檔', '*.json'), ('所有檔案', '*.*')))
        if not source_name:
            return
        try:
            _settings, report, backup_path = import_settings_to_config(Path(source_name), self.config_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror('設定匯入失敗', f'舊設定沒有套用，現有設定保持不變。\n\n{exc}', parent=self)
            return
        self.settings_import_pending_restart = True
        summary = '；'.join(report.messages)
        backup_text = f'\n原設定備份：{backup_path.name}' if backup_path is not None else ''
        self.settings_import_var.set('匯入完成，重新啟動後生效')
        self.set_message('舊設定匯入完成；請重新啟動監視器')
        close_now = messagebox.askyesno('設定匯入完成', f'設定已安全轉換。\n{summary}{backup_text}\n\n為避免目前畫面覆寫匯入結果，本次執行已停止儲存設定。\n要現在關閉監視器嗎？', parent=self)
        if close_now:
            self.on_close()

    def load_status_alert_rules(self) -> dict[int, dict[str, bool]]:
        """讀取每個狀態的提醒覆寫；沒有覆寫的狀態沿用全域開關。"""
        alert_policies = self.settings.get('alert_policies', {})
        raw_rules = alert_policies.get('status_rules', {}) if isinstance(alert_policies, dict) else {}
        if not isinstance(raw_rules, dict) or not raw_rules:
            raw_rules = self.settings.get('status_alert_rules', {})
        if not isinstance(raw_rules, dict):
            return {}
        result: dict[int, dict[str, bool]] = {}
        for raw_id, raw_rule in raw_rules.items():
            if not isinstance(raw_rule, dict):
                continue
            try:
                status_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not 0 <= status_id <= 65535:
                continue
            result[status_id] = {key: bool(raw_rule[key]) for key in ALERT_RULE_KEYS if key in raw_rule}
        return result

    def load_target_status_overrides(self) -> dict[int, set[int]]:
        """讀取人物個別監控狀態；有該 ID 就代表啟用個別清單，即使清單為空。"""
        raw_overrides = self.settings.get('target_status_overrides', {})
        if not isinstance(raw_overrides, dict):
            return {}
        result: dict[int, set[int]] = {}
        for raw_target_id, raw_statuses in raw_overrides.items():
            try:
                target_id = int(raw_target_id)
            except (TypeError, ValueError):
                continue
            if not 0 < target_id <= 4294967295:
                continue
            if isinstance(raw_statuses, dict):
                raw_statuses = raw_statuses.get('status_ids', [])
            if not isinstance(raw_statuses, list):
                continue
            status_ids: set[int] = set()
            for raw_status_id in raw_statuses:
                try:
                    status_id = int(raw_status_id)
                except (TypeError, ValueError):
                    continue
                if 0 <= status_id <= 65535:
                    status_ids.add(status_id)
            result[target_id] = status_ids
        return result

    def load_target_display_mode_overrides(self) -> dict[int, str]:
        """讀取人物個別顯示模式；未設定的人物跟隨人物類型。"""
        raw_overrides = self.settings.get('target_display_mode_overrides', {})
        if not isinstance(raw_overrides, dict):
            return {}
        result: dict[int, str] = {}
        for raw_target_id, raw_mode in raw_overrides.items():
            try:
                target_id = int(raw_target_id)
            except (TypeError, ValueError):
                continue
            if 0 < target_id <= 4294967295 and raw_mode in TARGET_DISPLAY_MODES:
                result[target_id] = str(raw_mode)
        return result

    def global_alert_rule_enabled(self, rule_key: str) -> bool:
        variable_name = {'apply': 'apply_sound_var', 'yellow': 'yellow_sound_var', 'red': 'red_sound_var'}.get(rule_key)
        variable = getattr(self, variable_name, None)
        if variable is not None:
            try:
                return bool(variable.get())
            except tk.TclError:
                pass
        return bool(self.settings.get({'apply': 'apply_sound_enabled', 'yellow': 'yellow_sound_enabled', 'red': 'red_sound_enabled'}.get(rule_key, ''), True))

    def default_status_alert_rule(self, status_id: int) -> dict[str, bool]:
        return {'apply': False, 'yellow': False, 'red': False}

    def ensure_status_alert_rule_vars(self, status_id: int) -> dict[str, tk.BooleanVar]:
        existing = self.status_alert_rule_vars.get(status_id)
        if existing is not None:
            return existing
        values = self.default_status_alert_rule(status_id)
        values.update(self.status_alert_rules.get(status_id, {}))
        variables = {key: tk.BooleanVar(value=bool(values.get(key, False))) for key in ALERT_RULE_KEYS}
        self.status_alert_rule_vars[status_id] = variables
        return variables

    def status_alert_rule_enabled(self, status_id: int, rule_key: str) -> bool:
        if rule_key not in ALERT_RULE_KEYS or not self.global_alert_rule_enabled(rule_key):
            return False
        variables = self.status_alert_rule_vars.get(status_id)
        if variables is not None:
            return bool(variables[rule_key].get())
        values = self.default_status_alert_rule(status_id)
        values.update(self.status_alert_rules.get(status_id, {}))
        return bool(values.get(rule_key, False))

    def current_alert_policy_resolver(self) -> AlertPolicyResolver:
        """建立提示專用規則；不讀取主表或卡片顯示狀態。"""
        status_rules = {int(status_id): dict(rule) for status_id, rule in self.__dict__.get('status_alert_rules', {}).items()}
        for status_id, variables in self.__dict__.get('status_alert_rule_vars', {}).items():
            try:
                status_rules[int(status_id)] = {key: bool(variables[key].get()) for key in ALERT_RULE_KEYS}
            except (KeyError, tk.TclError):
                continue
        global_enabled = {key: self.global_alert_rule_enabled(key) for key in ALERT_RULE_KEYS}
        scope_enabled: dict[str, bool] = {}
        for scope in TARGET_SCOPE_ORDER:
            variable = self.__dict__.get('target_sound_vars', {}).get(scope)
            try:
                scope_enabled[scope] = bool(variable.get()) if variable is not None else False
            except tk.TclError:
                scope_enabled[scope] = False
        return AlertPolicyResolver(global_enabled=global_enabled, status_rules=status_rules, scope_enabled=scope_enabled, scope_status_rules=self.__dict__.get('scope_status_alert_rules', {}), target_overrides=self.__dict__.get('target_alert_overrides', {}))

    def alert_rule_for_target(self, status_id: int, rule_key: str, target_id: int) -> bool:
        """個別人物＋狀態＞人物類型＋狀態＞狀態預設。"""
        default_enabled = bool(self.default_status_alert_rule(status_id).get(rule_key, False))
        decision = self.current_alert_policy_resolver().resolve(rule_key=rule_key, status_id=status_id, relation=self.target_tracker.relation_for(target_id), target_name=self.target_tracker.target_name(target_id) or '', default_enabled=default_enabled)
        return decision.enabled

    def clear_expiration_alerts(self, state_key: tuple[int, int]) -> None:
        """清掉單一目標狀態的黃／紅燈紀錄。

        不重建整個集合，避免多人多狀態時的平方級掃描。
        """
        self.alerted.discard((state_key, 'yellow'))
        self.alerted.discard((state_key, 'red'))

    @serialized_scoped_monitor
    def process_expiration_alerts(self, states: list[tuple[StatusState, int | None]], *, sync_ms: int, yellow_ms: int, red_ms: int, yellow_seconds: float, red_seconds: float) -> None:
        """依狀態事實處理到期聲音，不依賴主表或卡片是否顯示該狀態。"""
        display_resolver = self.current_policy_resolver()
        alert_resolver = self.current_alert_policy_resolver()
        for state, remaining in states:
            if state.target_id not in self.target_tracker.trusted_target_ids():
                self.clear_expiration_alerts(state.key)
                continue
            if not is_readable_status_name(state.status_id):
                self.clear_expiration_alerts(state.key)
                continue
            target_name = self.target_tracker.target_name(state.target_id) or ''
            relation = self.target_tracker.relation_for(state.target_id)
            policy = display_resolver.resolve(target_id=state.target_id, relation=relation, target_name=target_name)
            if policy.mode == MODE_OFF or not state.active or remaining is None:
                self.clear_expiration_alerts(state.key)
                continue
            adjusted_remaining = max(0, remaining - sync_ms)
            if adjusted_remaining <= 0:
                self.clear_expiration_alerts(state.key)
                continue
            level = status_visual_level(group=status_group(state.status_id, state.source_header), category=status_library_category(state.status_id, state.source_header), remaining_ms=adjusted_remaining, yellow_ms=yellow_ms, red_ms=red_ms)
            phase = expiration_phase(level)
            if not phase:
                self.clear_expiration_alerts(state.key)
                continue
            decision = alert_resolver.resolve(rule_key=phase, status_id=state.status_id, relation=relation, target_name=target_name, default_enabled=bool(self.default_status_alert_rule(state.status_id).get(phase, False)))
            alert_key = (state.key, phase)
            if decision.enabled and alert_key not in self.alerted:
                self.alerted.add(alert_key)
                self.play_alert_sound(phase, status_name(state.status_id, state.source_header), yellow_seconds if phase == 'yellow' else red_seconds, self.spoken_target_name(state.target_id))

    def on_status_alert_rule_changed(self, status_id: int) -> None:
        variables = self.ensure_status_alert_rule_vars(status_id)
        self.status_alert_rules[status_id] = {key: bool(variables[key].get()) for key in ALERT_RULE_KEYS}
        self.status_alert_rule_touched.add(status_id)
        self.save_settings()

    def current_status_edit_scope(self) -> str:
        scope = self.__dict__.get('status_edit_scope', '自己')
        return scope if scope in TARGET_SCOPE_ORDER else '自己'

    def commit_current_scope_status_selection(self) -> None:
        """把目前畫面上的狀態勾選保存到正在編輯的人物類型。"""
        self.sync_visible_status_checks()
        scope = self.current_status_edit_scope()
        self.target_scope_status_ids[scope] = set(self.status_selected_ids)

    def all_scope_status_ids(self) -> set[int]:
        """回傳所有人物類型需要解析的狀態，不含顯示用的重複資料。"""
        result: set[int] = set()
        active_scope = self.current_status_edit_scope()
        for scope in TARGET_SCOPE_ORDER:
            if scope == active_scope:
                result.update(self.status_selected_ids)
            else:
                result.update(self.target_scope_status_ids.get(scope, set()))
        return result

    def select_status_edit_scope(self, scope: str) -> None:
        """切換正在編輯的人物類型，各類清單互不覆蓋。"""
        if scope not in TARGET_SCOPE_ORDER:
            return
        if self.core_monitoring_only and scope != '自己':
            scope = '自己'
        old_scope = self.current_status_edit_scope()
        self.sync_visible_status_checks()
        self.target_scope_status_ids[old_scope] = set(self.status_selected_ids)
        self.status_edit_scope = scope
        self.status_edit_scope_var.set(scope)
        self.status_selected_ids = set(self.target_scope_status_ids.get(scope, set()))
        self.status_page_index = 0
        self.status_option_signature = None
        if self.status_detail_status_id is not None:
            self.status_detail_selected_var.set(self.status_detail_status_id in self.status_selected_ids)
        self.sync_tracker_status_filter()
        self.update_scope_status_buttons()
        self.update_selected_status_summary()
        if self.status_library_widgets_ready:
            self.rebuild_status_options()
        self.update_tab_labels()
        self.save_settings()

    def update_scope_status_buttons(self) -> None:
        """更新人物頁上的各類狀態數量。"""
        active_scope = self.current_status_edit_scope()
        for scope, button in self.target_scope_status_buttons.items():
            count = len(self.status_selected_ids if scope == active_scope else self.target_scope_status_ids.get(scope, set()))
            mode = self.scope_display_mode(scope)
            button.configure(text=f'設定（{count} 項）', state='normal' if mode == TARGET_DISPLAY_MODE_FOCUSED and (not (self.core_monitoring_only and scope != '自己')) else 'disabled')

    def open_scope_status_settings(self, scope: str) -> None:
        """從人物頁跳到指定人物類型的狀態清單。"""
        self.select_status_edit_scope(scope)
        self.open_status_settings()

    def status_is_selected(self, status_id: int) -> bool:
        """讀取狀態選取值；只有目前畫面上的列才會使用 Tk 變數。"""
        selected_ids = self.__dict__.get('status_selected_ids')
        if selected_ids is None:
            selected_ids = set()
            self.status_selected_ids = selected_ids
        variable = self.status_checks.get(status_id)
        if variable is not None:
            try:
                return bool(variable.get())
            except tk.TclError:
                self.status_checks.pop(status_id, None)
        return status_id in self.status_selected_ids

    def sync_visible_status_checks(self) -> None:
        """把目前可見列的 Tk 變數同步回輕量集合。"""
        selected_ids = self.__dict__.get('status_selected_ids')
        if selected_ids is None:
            selected_ids = set()
            self.status_selected_ids = selected_ids
        for status_id, variable in list(self.status_checks.items()):
            try:
                selected = bool(variable.get())
            except tk.TclError:
                self.status_checks.pop(status_id, None)
                continue
            if selected:
                selected_ids.add(status_id)
            else:
                selected_ids.discard(status_id)

    def sync_visible_status_alert_rules(self) -> None:
        """保存目前可見列的提示規則，讓離開分頁後可釋放 Tcl 變數。"""
        for status_id, variables in list(self.status_alert_rule_vars.items()):
            try:
                values = {key: bool(variables[key].get()) for key in ALERT_RULE_KEYS}
            except (KeyError, tk.TclError):
                self.status_alert_rule_vars.pop(status_id, None)
                continue
            if status_id in self.status_alert_rules or status_id in self.status_alert_rule_touched:
                self.status_alert_rules[status_id] = values
                self.status_alert_rule_touched.add(status_id)

    def set_status_selected(self, status_id: int, selected: bool) -> None:
        """更新狀態選取值；畫面有該列時同步更新控制項。"""
        selected_ids = self.__dict__.get('status_selected_ids')
        if selected_ids is None:
            selected_ids = set()
            self.status_selected_ids = selected_ids
        if selected:
            selected_ids.add(status_id)
        else:
            selected_ids.discard(status_id)
        variable = self.status_checks.get(status_id)
        if variable is not None:
            try:
                variable.set(selected)
            except tk.TclError:
                self.status_checks.pop(status_id, None)

    def status_is_selected_for_target(self, status_id: int, target_id: int) -> bool:
        """相容舊呼叫名稱；清單判定改由唯一人物規則解析器負責。"""
        tracker = self.__dict__.get('target_tracker')
        if tracker is None:
            return self.status_is_selected(status_id)
        policy = self.current_policy_resolver().resolve(target_id=target_id, relation=tracker.relation_for(target_id), target_name=tracker.target_name(target_id) or '')
        return int(status_id) in policy.custom_status_ids

    @serialized_scoped_monitor
    def target_display_mode_for(self, target_id: int) -> str:
        """取得唯一規則解析後的顯示模式。"""
        if target_id not in self.target_tracker.trusted_target_ids():
            return TARGET_DISPLAY_MODE_OFF
        relation = self.target_tracker.relation_for(target_id)
        policy = self.current_policy_resolver().resolve(target_id=target_id, relation=relation, target_name=self.target_tracker.target_name(target_id) or '')
        return legacy_mode(policy.mode)

    def scope_display_mode(self, relation: str) -> str:
        """取得人物類型顯示模式，並相容 1.5.8 的布林開關測試／設定。"""
        if scope_code(relation) not in frozenset({'self', 'party'}):
            return TARGET_DISPLAY_MODE_OFF
        mode_variables = self.__dict__.get('target_scope_display_mode_vars', {})
        variable = mode_variables.get(relation)
        if variable is not None:
            try:
                mode = variable.get()
                if mode in TARGET_DISPLAY_MODES:
                    return mode
            except tk.TclError:
                pass
        configured = self.__dict__.get('settings', {}).get('target_scope_display_modes', {})
        if isinstance(configured, dict) and configured.get(relation) in TARGET_DISPLAY_MODES:
            return str(configured[relation])
        legacy_variables = self.__dict__.get('target_scope_vars', {})
        legacy_variable = legacy_variables.get(relation)
        if legacy_variable is not None:
            try:
                return TARGET_DISPLAY_MODE_FOCUSED if legacy_variable.get() else TARGET_DISPLAY_MODE_OFF
            except tk.TclError:
                pass
        return TARGET_DISPLAY_MODE_DEFAULTS.get(relation, TARGET_DISPLAY_MODE_OFF)

    def current_policy_resolver(self) -> PolicyResolver:
        """把目前介面設定轉成不含 Tk 變數的唯一人物規則。"""
        modes = {scope: self.scope_display_mode(scope) for scope in TARGET_SCOPE_ORDER}
        scope_status_ids = self.__dict__.get('target_scope_status_ids', {})
        status_lists = {scope: set(scope_status_ids.get(scope, set())) for scope in TARGET_SCOPE_ORDER}
        active_scope = self.current_status_edit_scope()
        if 'status_selected_ids' in self.__dict__:
            status_lists[active_scope] = set(self.status_selected_ids)
        policies = build_scope_policies(modes, status_lists)
        saved_resolver = PolicyResolver.from_settings(self.__dict__.get('settings', {}))
        name_overrides: dict[str, ScopePolicy] = dict(saved_resolver.name_overrides)
        session_id_overrides: dict[int, ScopePolicy] = {}
        target_ids = set(self.__dict__.get('target_display_mode_overrides', {}))
        target_ids.update(self.__dict__.get('target_status_overrides', {}))
        for target_id in target_ids:
            raw_mode = self.__dict__.get('target_display_mode_overrides', {}).get(target_id, TARGET_DISPLAY_MODE_FOCUSED if target_id in self.__dict__.get('target_status_overrides', {}) else TARGET_DISPLAY_MODE_INHERIT)
            if raw_mode == TARGET_DISPLAY_MODE_INHERIT:
                continue
            mode = normalize_mode(raw_mode, MODE_OFF)
            custom_ids = frozenset(self.__dict__.get('target_status_overrides', {}).get(target_id, set()))
            policy = ScopePolicy(mode, custom_ids)
            target_name = self.target_tracker.target_name(target_id) or ''
            if target_name.strip():
                name_overrides[target_name] = policy
            else:
                session_id_overrides[int(target_id)] = policy
        return PolicyResolver(policies, name_overrides=name_overrides, self_override=saved_resolver.self_override, session_id_overrides=session_id_overrides)

    def status_is_displayed_for_target(self, status_id: int, target_id: int) -> bool:
        """相容舊呼叫名稱；顯示權限只由 effective policy 決定。"""
        policy = self.current_policy_resolver().resolve(target_id=target_id, relation=self.target_tracker.relation_for(target_id), target_name=self.target_tracker.target_name(target_id) or '')
        return policy.displays(status_id)

    def target_status_override_label(self, target_id: int) -> str:
        relation = self.target_tracker.relation_for(target_id)
        policy = self.current_policy_resolver().resolve(target_id=target_id, relation=relation, target_name=self.target_tracker.target_name(target_id) or '')
        mode = legacy_mode(policy.mode)
        if policy.source.endswith('覆寫'):
            return f'{mode}｜{policy.source}｜自訂 {len(policy.custom_status_ids)} 項'
        return f'{mode}｜{policy.source}'

    def configured_alert_ids(self) -> set[int]:
        return normalized_status_ids(self.settings.get('alert_status_ids', []))

    @staticmethod
    def parse_target_id(value: object) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value if 0 < value <= 4294967295 else None
        text = str(value).strip().replace('_', '')
        if not text:
            return None
        try:
            target_id = int(text, 16 if text.casefold().startswith('0x') else 10)
        except ValueError:
            return None
        return target_id if 0 < target_id <= 4294967295 else None

    @staticmethod
    def format_target_id(target_id: int) -> str:
        return f'0x{target_id:08X}'

    def target_display_name(self, target_id: int) -> str:
        """有可靠人物名稱時只顯示名稱；沒有名稱才回退顯示 ID。"""
        return self.target_tracker.target_name(target_id) or self.format_target_id(target_id)

    def save_settings(self) -> None:
        if self.__dict__.get('settings_import_pending_restart', False):
            return
        self.commit_current_scope_status_selection()
        alert_status_ids = sorted(self.all_scope_status_ids())
        status_alert_rules: dict[str, dict[str, bool]] = {}
        for status_id in sorted(self.status_alert_rule_touched):
            variables = self.status_alert_rule_vars.get(status_id)
            if variables is not None:
                status_alert_rules[str(status_id)] = {key: bool(variables[key].get()) for key in ALERT_RULE_KEYS}
            elif status_id in self.status_alert_rules:
                status_alert_rules[str(status_id)] = dict(self.status_alert_rules[status_id])
        scope_policies = build_scope_policies({scope: self.scope_display_mode(scope) for scope in TARGET_SCOPE_ORDER}, {scope: self.target_scope_status_ids.get(scope, set()) for scope in TARGET_SCOPE_ORDER})
        target_overrides: dict[str, dict[str, object]] = {}
        saved_resolver = PolicyResolver.from_settings(self.__dict__.get('settings', {}))
        for normalized_name, policy in saved_resolver.name_overrides.items():
            target_overrides[normalized_name] = {'mode': policy.mode, 'custom_status_ids': sorted(policy.custom_status_ids)}
        target_override_ids = set(self.__dict__.get('target_display_mode_overrides', {}))
        target_override_ids.update(self.__dict__.get('target_status_overrides', {}))
        for target_id in sorted(target_override_ids):
            target_name = (self.target_tracker.target_name(target_id) or '').strip()
            if not target_name:
                continue
            raw_mode = self.__dict__.get('target_display_mode_overrides', {}).get(target_id, TARGET_DISPLAY_MODE_FOCUSED)
            mode = normalize_mode(raw_mode, MODE_CUSTOM)
            target_overrides[target_name] = {'mode': mode, 'custom_status_ids': sorted(self.__dict__.get('target_status_overrides', {}).get(target_id, set()))}
        self_target_id = self.parse_target_id(self.self_target_id_var.get())
        self_target_name = self.self_target_name_var.get().strip()
        settings = {'settings_schema_version': 2, 'scope_policies': serialize_scope_policies(scope_policies), 'target_overrides': target_overrides, 'alert_policies': {'status_rules': status_alert_rules, 'scope_enabled': {scope: bool(variable.get()) for scope, variable in self.target_sound_vars.items()}, 'scope_status_rules': self.__dict__.get('scope_status_alert_rules', {}), 'target_overrides': self.__dict__.get('target_alert_overrides', {})}, 'replay_dir': self.dir_var.get().strip(), 'ro_install_dir': self.ro_dir_var.get().strip(), 'auto_client_data': False, 'selected_file': self.file_var.get().strip(), 'auto_latest': bool(self.auto_latest_var.get()), 'alert_status_ids': alert_status_ids, 'show_all_statuses': bool(self.show_all_statuses_var.get()), 'show_technical_columns': bool(self.show_technical_columns_var.get()), 'alert_seconds': max(0, self.safe_float(self.red_var.get(), 15)), 'yellow_seconds': max(0, self.safe_float(self.yellow_var.get(), 30)), 'red_seconds': max(0, self.safe_float(self.red_var.get(), 15)), 'yellow_sound_enabled': bool(self.yellow_sound_var.get()), 'red_sound_enabled': bool(self.red_sound_var.get()), 'apply_sound_enabled': bool(self.apply_sound_var.get()), 'status_alert_rules': status_alert_rules, 'sound_mode': self.sound_mode_var.get(), 'sound_file': self.sound_file_var.get().strip(), 'sound_volume': int(self.sound_volume_var.get()), 'overlay_enabled': bool(self.overlay_enabled_var.get()), 'overlay_width': int(self.overlay_width), 'overlay_height': int(self.overlay_height), 'overlay_x': self.overlay_x, 'overlay_y': self.overlay_y, 'overlay_auto_height': bool(self.overlay_auto_height_var.get()), 'overlay_locked': bool(self.overlay_locked_var.get()), 'overlay_opacity': max(OVERLAY_MIN_OPACITY, min(OVERLAY_MAX_OPACITY, float(self.overlay_opacity))), 'overlay_font_size': int(self.overlay_font_size), 'tree_column_widths': self.current_tree_column_widths(), 'sync_offset_seconds': max(0, self.safe_float(self.sync_var.get(), 5)), 'auto_sync': bool(self.auto_sync_var.get()), 'poll_seconds': max(0.2, self.safe_float(self.interval_var.get(), 0.5)), 'core_monitoring_only': bool(self.core_monitoring_only), 'target_sound_enabled': {scope: bool(variable.get()) for scope, variable in self.target_sound_vars.items()}, 'self_target_id': self_target_id, 'self_target_name': self_target_name, 'target_view': self.target_view_var.get() if self.target_view_var.get() in TARGET_VIEW_MODES else TARGET_VIEW_MODES[0], 'pet_monitor_enabled': bool(self.pet_monitor_enabled_var.get()), 'pet_selected_id': self.parse_target_id(self.pet_selected_id_var.get()), 'pet_alert_enabled': bool(self.pet_alert_enabled_var.get()), 'pet_alert_threshold': max(0, min(100, int(self.safe_float(self.pet_alert_threshold_var.get(), PET_ALERT_DEFAULT_THRESHOLD)))), 'auto_resolve_unknown': bool(self.settings.get('auto_resolve_unknown', True))}
        pet_overlay = self.__dict__.get('pet_overlay')
        if pet_overlay is not None and pet_overlay.winfo_exists():
            self.pet_overlay_settings.update(pet_overlay.export_settings())
        settings.update(self.__dict__.get('pet_overlay_settings', {}))
        settings['pet_overlay_enabled'] = bool(self.pet_overlay_enabled_var.get())
        settings = scoped_settings(settings)
        settings_changed = getattr(self, '_last_saved_settings', None) != settings
        self.settings.update(settings)
        if not settings_changed:
            return
        try:
            write_catalog_json_atomically(self.config_path, settings, backup=True)
            self._last_saved_settings = settings
        except (OSError, TypeError, ValueError) as exc:
            self.set_message(f'設定儲存失敗：{exc}')
            self.log_event(f'設定儲存失敗，原設定已保留：{type(exc).__name__}: {exc}')

    def schedule_settings_save(self) -> None:
        """延後儲存快速變動的設定，避免拖曳滑桿時反覆寫入硬碟。"""
        if self.settings_save_job is not None:
            try:
                self.after_cancel(self.settings_save_job)
            except (tk.TclError, RuntimeError):
                pass
        try:
            self.settings_save_job = self.after(SETTINGS_SAVE_DEBOUNCE_MS, self._flush_scheduled_settings_save)
        except (tk.TclError, RuntimeError):
            self.settings_save_job = None

    def _flush_scheduled_settings_save(self) -> None:
        self.settings_save_job = None
        if self.running:
            self.save_settings()

    @staticmethod
    def safe_float(value: object, fallback: float) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else fallback
        except (TypeError, ValueError, OverflowError):
            return fallback

    @staticmethod
    def safe_dimension(value: object, fallback: int, minimum: int, maximum: int) -> int:
        """讀取視窗尺寸設定；舊設定或異常值一律回到安全範圍。"""
        try:
            number = int(float(str(value)))
        except (TypeError, ValueError, OverflowError):
            number = fallback
        return max(minimum, min(maximum, number))

    @staticmethod
    def safe_coordinate(value: object) -> int | None:
        """保留多螢幕負座標，異常設定回到自動定位。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            if not math.isfinite(value):
                return None
            return int(max(-2 ** 30, min(2 ** 30, value)))
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def load_tree_column_widths(cls, value: object) -> dict[str, int]:
        """讀取主表欄寬；只接受合理尺寸，避免舊設定把畫面壓縮到不可用。"""
        if not isinstance(value, dict):
            return {}
        result: dict[str, int] = {}
        for column, width in value.items():
            if not isinstance(column, str):
                continue
            result[column] = cls.safe_dimension(width, 100, 60, 1000)
        return result

    def current_tree_column_widths(self) -> dict[str, int]:
        tree = getattr(self, 'tree', None)
        if tree is None:
            return dict(getattr(self, 'tree_column_widths', {}))
        widths: dict[str, int] = {}
        columns = tree['columns']
        if isinstance(columns, str):
            columns = tuple(columns.split())
        for column in columns:
            try:
                widths[column] = int(tree.column(column, 'width'))
            except (tk.TclError, TypeError, ValueError):
                continue
        self.tree_column_widths = widths
        return widths

    def on_tree_button_release(self, event: tk.Event) -> None:
        """只有拖曳主表欄位分隔線時才保存欄寬，避免一般點擊頻繁寫檔。"""
        tree = getattr(self, 'tree', None)
        if tree is None:
            return
        try:
            if tree.identify_region(event.x, event.y) == 'separator':
                self.save_settings()
        except tk.TclError:
            pass

    def set_message(self, message: str) -> None:
        with self.message_lock:
            self.monitor_message = message

    def get_message(self) -> str:
        with self.message_lock:
            return self.monitor_message

    def log_event(self, message: str) -> None:
        """將啟動與資料校對階段寫入本機紀錄，方便 VBS 隱藏視窗時除錯。"""
        timestamp = datetime.now().isoformat(timespec='seconds')
        try:
            with self.startup_log_path.open('a', encoding='utf-8') as handle:
                handle.write(f'[{timestamp}] {message}\n')
        except OSError:
            pass

    def enqueue_ui_callback(self, callback: Callable[[], None], critical: bool=False) -> bool:
        """把背景工作要做的 UI 更新交給 Tk 主執行緒。

        進度等可丟棄更新走有上限的普通佇列；完成／錯誤／關閉等必要事件
        使用小型保留佇列，避免大量進度更新把必要事件擠掉。
        """
        if critical:
            with self.ui_critical_lock:
                if len(self.ui_critical_callbacks) < self.ui_critical_callbacks.maxlen:
                    self.ui_critical_callbacks.append(callback)
                    return True
        try:
            self.ui_callback_queue.put_nowait(callback)
            return True
        except queue.Full:
            self.ui_callback_overflow_count += 1
            if not self.ui_callback_overflow_logged:
                self.ui_callback_overflow_logged = True
                self.log_event('UI 更新佇列已滿，捨棄非必要更新以保護監控畫面')
            return False

    def queue_status_reload_progress(self, value: float, message: str) -> None:
        """合併校對進度更新，只把最新進度送入 Tk 主執行緒。"""
        with self.ui_progress_lock:
            self.pending_ui_progress = (float(value), str(message))
            if self.ui_progress_callback_queued:
                return
            self.ui_progress_callback_queued = True
        if not self.enqueue_ui_callback(self.flush_status_reload_progress):
            with self.ui_progress_lock:
                self.ui_progress_callback_queued = False

    def flush_status_reload_progress(self) -> None:
        with self.ui_progress_lock:
            progress = self.pending_ui_progress
            self.pending_ui_progress = None
            self.ui_progress_callback_queued = False
        if progress is not None:
            self.set_status_reload_progress(*progress)
        with self.ui_progress_lock:
            needs_reschedule = self.pending_ui_progress is not None and (not self.ui_progress_callback_queued)
            if needs_reschedule:
                self.ui_progress_callback_queued = True
        if needs_reschedule:
            if not self.enqueue_ui_callback(self.flush_status_reload_progress):
                with self.ui_progress_lock:
                    self.ui_progress_callback_queued = False

    def process_ui_callbacks(self) -> None:
        """由 Tk 主執行緒定時執行背景工作排入的 UI 更新。"""
        if not self.running and (not getattr(self, 'close_after_data_load', False)):
            return
        processed = 0
        started_at = time.perf_counter()
        while processed < UI_CALLBACK_MAX_PER_TICK:
            if processed and (time.perf_counter() - started_at) * 1000 >= UI_CALLBACK_TIME_BUDGET_MS:
                break
            callback = None
            with self.ui_critical_lock:
                if self.ui_critical_callbacks:
                    callback = self.ui_critical_callbacks.popleft()
            if callback is None:
                try:
                    callback = self.ui_callback_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                callback()
            except (tk.TclError, RuntimeError):
                pass
            except Exception as exc:
                self.log_event(f'UI 更新例外：{type(exc).__name__}: {exc}')
            processed += 1
        try:
            if self.running or getattr(self, 'close_after_data_load', False):
                self.after(UI_CALLBACK_INTERVAL_MS, self.process_ui_callbacks)
        except (tk.TclError, RuntimeError):
            pass

    def set_status_reload_progress(self, value: float, message: str) -> None:
        """更新手動校對進度；這個方法只由 Tk 主執行緒呼叫。"""
        value = max(0.0, min(100.0, float(value)))
        self.status_reload_progress_var.set(value)
        self.status_reload_progress_text_var.set(f'{value:.0f}%｜{message}')

    def unknown_pending_counts(self) -> tuple[int, int]:
        with self.data_load_lock:
            return (len(self.pending_unknown_status_ids), len(self.pending_client_item_ids))

    def show_close_choice_dialog(self) -> None:
        """有未知資料時提供明確三選項，不把完整校對綁進關閉。"""
        existing = self.__dict__.get('close_choice_dialog')
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    return
            except tk.TclError:
                pass
        status_count, item_count = self.unknown_pending_counts()
        total_count = status_count + item_count
        dialog = tk.Toplevel(self)
        self.close_choice_dialog = dialog
        dialog.title('關閉監視器')
        dialog.geometry('500x210')
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.protocol('WM_DELETE_WINDOW', self.dismiss_close_choice_dialog)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f'尚有 {total_count} 筆未知資料', font=('Microsoft JhengHei', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'狀態 {status_count} 筆｜物品 {item_count} 筆\n直接關閉會保存待辦，下次仍可處理。', foreground='#555555', justify='left').pack(anchor='w', pady=(6, 18))
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        ttk.Button(actions, text='取消', command=self.dismiss_close_choice_dialog).pack(side='right')
        ttk.Button(actions, text='比對未知後關閉', command=self.begin_close_unknown_resolution).pack(side='right', padx=8)
        ttk.Button(actions, text='直接關閉', command=self._finalize_close).pack(side='right')
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def dismiss_close_choice_dialog(self) -> None:
        dialog = self.__dict__.get('close_choice_dialog')
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.grab_release()
                    dialog.destroy()
            except tk.TclError:
                pass
        self.close_choice_dialog = None

    def begin_close_unknown_resolution(self) -> None:
        ro_dir = Path(self.ro_dir_var.get().strip() or DEFAULT_RO_DIR)
        ready, ready_message = twro_install_message(ro_dir)
        if not ready:
            messagebox.showwarning('目前無法比對', f'{ready_message}。未知資料已保存，可直接關閉或取消後重新設定 RO 資料夾。', parent=self.close_choice_dialog or self)
            return
        self.dismiss_close_choice_dialog()
        self.ro_install_dir = ro_dir
        self.close_after_data_load = True
        self.__dict__.setdefault('close_unknown_processed_keys', set()).clear()
        self.monitoring_active.clear()
        self.monitor_wake_event.set()
        self.save_settings()
        status_count, item_count = self.unknown_pending_counts()
        self.close_progress_heading_var.set(f'正在比對 {status_count + item_count} 筆未知資料')
        self.close_progress_detail_var.set('每次最多處理 100 筆；未辨識資料會保留到下次。')
        self.show_close_progress_dialog()
        self.schedule_unknown_resolution(force=True)

    def show_close_progress_dialog(self) -> None:
        """顯示關閉前資料工作進度，並提供取消與直接關閉。"""
        dialog = self.__dict__.get('close_progress_dialog')
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.deiconify()
                    dialog.lift()
                    return
            except tk.TclError:
                pass
        dialog = tk.Toplevel(self)
        self.close_progress_dialog = dialog
        dialog.title('關閉前處理未知資料')
        dialog.geometry('540x230')
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.protocol('WM_DELETE_WINDOW', self.cancel_catalog_work)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, textvariable=self.close_progress_heading_var, font=('Microsoft JhengHei', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, textvariable=self.close_progress_detail_var, foreground='#666666').pack(anchor='w', pady=(4, 12))
        ttk.Progressbar(frame, mode='determinate', maximum=100, variable=self.status_reload_progress_var).pack(fill='x')
        ttk.Label(frame, textvariable=self.status_reload_progress_text_var, anchor='w').pack(fill='x', pady=(7, 2))
        ttk.Label(frame, textvariable=self.close_progress_elapsed_var, foreground='#666666').pack(anchor='w')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(10, 0))
        ttk.Button(actions, text='取消並返回', command=self.cancel_catalog_work).pack(side='right')
        ttk.Button(actions, text='停止比對並關閉', command=self.request_direct_close).pack(side='right', padx=(0, 8))
        self.close_progress_started_at = time.monotonic()
        self.update_close_progress_elapsed()
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def update_close_progress_elapsed(self) -> None:
        if self.close_progress_started_at is None:
            return
        elapsed = max(0, int(time.monotonic() - self.close_progress_started_at))
        self.close_progress_elapsed_var.set(f'已用時間：{elapsed} 秒')
        try:
            if self.close_after_data_load and (not self.close_finalized):
                self.close_progress_job = self.after(500, self.update_close_progress_elapsed)
        except (tk.TclError, RuntimeError):
            self.close_progress_job = None

    def request_direct_close(self) -> None:
        if messagebox.askyesno('直接關閉？', '尚未完成的未知資料會保留，下次仍可繼續。要直接關閉嗎？', parent=self.close_progress_dialog or self):
            self.catalog_cancel_requested.set()
            self.catalog_worker_client.cancel()
            self._finalize_close()

    def _hide_close_progress_dialog(self) -> None:
        close_progress_job = self.__dict__.get('close_progress_job')
        if close_progress_job is not None:
            try:
                self.after_cancel(close_progress_job)
            except tk.TclError:
                pass
            self.close_progress_job = None
        dialog = self.__dict__.get('close_progress_dialog')
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.grab_release()
                    dialog.destroy()
            except tk.TclError:
                pass
        self.close_progress_dialog = None
        self.close_progress_started_at = None

    def cancel_catalog_work(self) -> None:
        """取消校對並保留既有快取；關閉流程中取消則回到待機。"""
        self.catalog_cancel_requested.set()
        self.catalog_worker_client.cancel()
        was_closing = bool(self.close_after_data_load)
        self.close_after_data_load = False
        if was_closing:
            self.running = True
            self.monitoring_active.clear()
            self.monitor_wake_event.set()
            self._hide_close_progress_dialog()
            self.set_message('待機：已取消關閉前比對')
        self.set_status_reload_progress(0, '正在取消；原有資料保持不變')

    def start_client_data_load(self, reason: str, force: bool=True, wanted_item_ids: set[int] | None=None, allow_full_scan: bool=True) -> None:
        """以背景工作執行 RO 資料同步；完整 GRF 掃描需明確允許。"""
        with self.data_load_lock:
            if self.data_load_thread is not None and self.data_load_thread.is_alive():
                self.pending_client_item_ids.update(wanted_item_ids or set())
                prefix = '監控中' if self.monitoring_is_active() else '待機中'
                self.set_message(f'{prefix}：RO 資料校對仍在進行，請稍候…')
                return
            ro_dir = Path(self.ro_install_dir)
            twro_ready, twro_message = twro_install_message(ro_dir)
            if not twro_ready:
                self.set_status_reload_progress(0, twro_message)
                self.set_message(f'無法校對：{twro_message}')
                self.update_source_controls()
                return
            requested_ids = set(self.pending_client_item_ids)
            requested_ids.update(wanted_item_ids or set())
            self.ro_data_ready = True
            self.set_status_reload_progress(0, f'準備 RO 資料同步（{reason}）')
            if self.monitoring_is_active():
                self.set_message(f'監控中：正在同步 RO 資料（{reason}）；RRF 監控持續執行')
            else:
                self.set_message(f'正在校對 RO 資料（{reason}）；目前仍為待機狀態')
            if self.status_reload_button is not None:
                self.status_reload_button.configure(state=tk.DISABLED)
            if self.unknown_review_button is not None:
                self.unknown_review_button.configure(state=tk.DISABLED)
            if self.catalog_cancel_button is not None:
                self.catalog_cancel_button.configure(state=tk.NORMAL)
            self.catalog_cancel_requested.clear()
            self.log_event(f'開始 RO 資料校對：{ro_dir}')
            self.data_load_thread = threading.Thread(target=self._client_data_load_worker, args=(ro_dir, force, requested_ids, allow_full_scan), daemon=True)
            self.data_load_thread.start()

    def _client_data_load_worker(self, ro_dir: Path, force: bool, wanted_item_ids: set[int], allow_full_scan: bool) -> None:
        result: StatusDataResult | None = None
        client_result: ClientCatalogResult | None = None
        error_text = ''
        try:
            with self.client_data_io_lock:
                result, client_result = self._run_catalog_worker_process({'mode': 'full', 'ro_dir': str(ro_dir), 'status_cache_path': str(self.status_data_cache_path), 'client_cache_path': str(self.client_data_cache_path), 'force': bool(force), 'allow_full_scan': bool(allow_full_scan), 'wanted_item_ids': sorted(wanted_item_ids)})
        except CatalogWorkerCancelled:
            error_text = '__cancelled__'
        except Exception as exc:
            error_text = f'{type(exc).__name__}: {exc}'
            self.log_event(f'RO 資料校對例外：{error_text}')
        self.enqueue_ui_callback(lambda: self._finish_client_data_load(ro_dir, result, client_result, error_text), critical=True)

    @staticmethod
    def _catalog_worker_command(request_path: Path, result_path: Path, progress_path: Path) -> list[str]:
        arguments = ['--catalog-worker', str(request_path), str(result_path), str(progress_path)]
        if getattr(sys, 'frozen', False):
            return [str(Path(sys.executable).resolve()), *arguments]
        return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()), *arguments]

    def _run_catalog_worker_process(self, request: dict[str, object]) -> tuple[StatusDataResult | None, ClientCatalogResult | None]:
        """執行短期校對程序；主程序只接回結果，不保留 GRF 索引。"""
        scoped_generation = request.pop('_scoped_generation', None)
        if scoped_generation is not None and scoped_generation != self.__dict__.get('_scoped_generation', 0):
            raise CatalogWorkerCancelled()
        payload = self.catalog_worker_client.run(request=request, command_builder=self._catalog_worker_command, on_progress=lambda progress: self.queue_status_reload_progress(progress.value, progress.message), should_cancel=lambda: bool(self.__dict__.get('catalog_cancel_requested') and self.catalog_cancel_requested.is_set()) or (not self.running and (not self.close_after_data_load)) or (scoped_generation is not None and scoped_generation != self.__dict__.get('_scoped_generation', 0)))
        self.queue_status_reload_progress(100, 'RO 資料處理完成')
        return (_status_result_from_payload(payload.get('status_result')), _client_result_from_payload(payload.get('client_result')))

    @serialized_scoped_monitor
    def _finish_client_data_load(self, ro_dir: Path, result: StatusDataResult | None, client_result: ClientCatalogResult | None, error_text: str) -> None:
        with self.data_load_lock:
            self.data_load_thread = None
        if not self.running and (not getattr(self, 'close_after_data_load', False)):
            return
        if error_text == '__cancelled__':
            self.catalog_cancel_requested.clear()
            self.close_after_data_load = False
            if self.status_reload_button is not None:
                self.status_reload_button.configure(state=tk.NORMAL)
            catalog_cancel_button = self.__dict__.get('catalog_cancel_button')
            if catalog_cancel_button is not None:
                catalog_cancel_button.configure(state=tk.DISABLED)
            self.update_unknown_review_controls()
            self.set_status_reload_progress(0, '已取消；原有資料保持不變')
            self.data_status_summary_var.set('已取消｜沿用原有資料')
            self.set_message('待機：資料檢查已取消，原有資料仍可使用')
            self._hide_close_progress_dialog()
            return
        if result is None:
            self.ro_data_ready = True
            if self.status_reload_button is not None:
                self.status_reload_button.configure(state=tk.NORMAL)
            if self.catalog_cancel_button is not None:
                self.catalog_cancel_button.configure(state=tk.DISABLED)
            self.status_data_report = f"RO 狀態資料校對失敗：{error_text or '未知錯誤'}"
            self.status_data_report_var.set(self.status_data_report)
            data_summary_var = self.__dict__.get('data_status_summary_var')
            if data_summary_var is not None:
                data_summary_var.set('檢查失敗｜沿用目前資料')
            self.set_status_reload_progress(100, '校對失敗，沿用目前資料')
            prefix = '監控中' if self.monitoring_is_active() else '待機中'
            self.set_message(f'{prefix}：RO 校對失敗，沿用目前資料｜{self.status_data_report}')
            self.log_event(self.status_data_report)
            self.update_unknown_review_controls()
            if getattr(self, 'close_after_data_load', False):
                self._finalize_close()
            return
        set_status_data(result.names, result.groups, result.source)
        self.status_data_report = result.report
        self.status_data_report_var.set(result.report)
        data_summary_var = self.__dict__.get('data_status_summary_var')
        if data_summary_var is not None:
            data_summary_var.set('已完成更新')
        if client_result is not None:
            set_client_catalog(client_result)
            self.client_data_report = client_result.report
            self.client_data_report_var.set(self.client_data_report)
        with self.data_load_lock:
            resolved_status_candidates = set(self.pending_unknown_status_ids)
            resolved_item_candidates = set(self.pending_client_item_ids)
            self.pending_client_item_ids = {item_id for item_id in self.pending_client_item_ids if item_id not in ITEM_NAMES_BY_ID}
            self.pending_unknown_status_ids = {status_id for status_id in self.pending_unknown_status_ids if not is_readable_status_name(status_id)}
            newly_resolved_status_ids = resolved_status_candidates - self.pending_unknown_status_ids
            newly_resolved_item_ids = resolved_item_candidates - self.pending_client_item_ids
        unknown_journal = self.__dict__.get('unknown_journal')
        if (newly_resolved_status_ids or newly_resolved_item_ids) and unknown_journal is not None:
            unknown_journal.resolve([('status', value) for value in newly_resolved_status_ids] + [('item', value) for value in newly_resolved_item_ids])
            try:
                unknown_journal.flush()
            except OSError as exc:
                self.log_event(f'未知資料待辦暫時無法保存：{exc}')
        promoted_statuses, promoted_items = promote_verified_unknowns_to_bundled_data(newly_resolved_status_ids, newly_resolved_item_ids, result, client_result)
        self.ro_data_ready = True
        if self.status_reload_button is not None:
            self.status_reload_button.configure(state=tk.NORMAL)
        if self.catalog_cancel_button is not None:
            self.catalog_cancel_button.configure(state=tk.DISABLED)
        if self.unknown_review_button is not None:
            self.unknown_review_button.configure(state=tk.NORMAL)
        self.client_data_signatures = _client_data_signatures(ro_dir)
        self.status_data_signatures = _grf_signatures(ro_dir)
        self.save_settings()
        self.status_option_source_headers = {}
        self.status_option_generation += 1
        selected_tab = self.notebook.select() if hasattr(self, 'notebook') else ''
        if self.status_library_tab is not None and selected_tab == str(self.status_library_tab):
            self.refresh_status_options(self.tracker.snapshot())
        if self.pet_tab is not None and selected_tab == str(self.pet_tab):
            self.refresh_pet_catalog()
            self.refresh_pet_panel()
        self.set_status_reload_progress(100, result.report)
        client_report = client_result.report if client_result is not None else '物品／寵物資料未更新'
        prefix = '監控中' if self.monitoring_is_active() else '待機中'
        self.set_message(f'{prefix}：RO 校對完成｜{result.report}｜{client_report}')
        promotion_text = ''
        self.log_event(f'RO 資料校對完成：{result.report}｜{client_report}{promotion_text}')
        self.update_unknown_review_controls()
        if getattr(self, 'close_after_data_load', False):
            self._finalize_close()
        else:
            self.schedule_unknown_resolution()

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill='both', expand=True)
        monitor_controls = ttk.Frame(root, padding=(2, 2, 2, 8))
        monitor_controls.pack(fill='x', pady=(0, 8))
        monitor_controls.columnconfigure(0, weight=1)
        ttk.Label(monitor_controls, textvariable=self.monitor_control_var, font=('Microsoft JhengHei', 10, 'bold'), anchor='w').grid(row=0, column=0, sticky='ew', padx=(0, 10))
        self.start_monitor_button = ttk.Button(monitor_controls, text='開始監控', command=self.start_monitoring, width=12)
        self.start_monitor_button.grid(row=0, column=1, padx=(0, 6))
        self.stop_monitor_button = ttk.Button(monitor_controls, text='停止監控', command=self.stop_monitoring, width=12)
        self.stop_monitor_button.grid(row=0, column=2)
        notebook = ttk.Notebook(root)
        notebook.pack(fill='both', expand=True)
        self.notebook = notebook
        monitor_tab = ttk.Frame(notebook, padding=8)
        status_library_tab = ttk.Frame(notebook, padding=12)
        target_tab = ttk.Frame(notebook, padding=12)
        pet_tab = ttk.Frame(notebook, padding=12)
        settings_tab = ttk.Frame(notebook, padding=12)
        self.status_library_tab = status_library_tab
        self.target_selection_tab = target_tab
        self.pet_tab = pet_tab
        self.settings_tab = settings_tab
        notebook.add(monitor_tab, text='即時監控')
        notebook.add(target_tab, text='人物設定')
        notebook.add(status_library_tab, text='自訂規則')
        notebook.add(pet_tab, text='寵物')
        notebook.add(settings_tab, text='設定')
        notebook.bind('<<NotebookTabChanged>>', lambda _event: self.on_notebook_tab_changed())
        monitor_tab.columnconfigure(0, weight=1)
        monitor_tab.rowconfigure(4, weight=1)
        self.onboarding_box = ttk.LabelFrame(monitor_tab, text='快速開始', padding=8)
        self.onboarding_box.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        self.onboarding_box.columnconfigure(0, weight=1)
        ttk.Label(self.onboarding_box, textvariable=self.onboarding_var, justify='left', anchor='w', wraplength=900).grid(row=0, column=0, sticky='ew', pady=(0, 6))
        onboarding_actions = ttk.Frame(self.onboarding_box)
        onboarding_actions.grid(row=1, column=0, sticky='w')
        ttk.Button(onboarding_actions, text='設定 RRF 來源', command=self.choose_directory).pack(side='left', padx=(0, 6))
        ttk.Button(onboarding_actions, text='設定我的角色', command=self.open_target_settings).pack(side='left', padx=(0, 6))
        ttk.Button(onboarding_actions, text='設定重點狀態', command=self.open_status_settings).pack(side='left')
        summary = ttk.Frame(monitor_tab, padding=(2, 2, 2, 6))
        summary.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, textvariable=self.monitor_summary_var, font=('Microsoft JhengHei', 12, 'bold')).grid(row=0, column=0, sticky='w')
        info = ttk.Frame(summary)
        info.grid(row=0, column=1, sticky='e')
        info.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(info, text=self.get_message(), foreground='#555555')
        self.status_label.grid(row=0, column=0, sticky='e')
        self.rescan_button = ttk.Button(info, text='重新掃描', command=self.rescan)
        self.rescan_button.grid(row=0, column=1, padx=(8, 0))
        event_info = ttk.Frame(summary)
        event_info.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 0))
        ttk.Label(event_info, text='最新事件：', foreground='#666666').pack(side='left')
        ttk.Label(event_info, textvariable=self.latest_event_var).pack(side='left', fill='x', expand=True)
        status_summary = ttk.Frame(monitor_tab, padding=(2, 2, 2, 6))
        status_summary.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        status_summary.columnconfigure(0, weight=1)
        ttk.Label(status_summary, textvariable=self.selected_statuses_var, anchor='w').grid(row=0, column=0, sticky='ew')
        summary_actions = ttk.Frame(status_summary)
        summary_actions.grid(row=0, column=1, sticky='e', padx=(8, 0))
        ttk.Button(summary_actions, text='自訂規則', command=lambda: self.notebook.select(self.status_library_tab)).pack(side='left')
        ttk.Button(summary_actions, text='人物設定', command=lambda: self.notebook.select(self.target_selection_tab)).pack(side='left', padx=(6, 0))
        self.build_lazy_tab_placeholder(status_library_tab, '開啟分頁後載入狀態與職業資料')
        self.build_lazy_tab_placeholder(pet_tab, '開啟分頁後載入自身寵物面板')
        target_tab.columnconfigure(0, weight=1)
        target_tab.rowconfigure(0, weight=1)
        target_options = ttk.LabelFrame(target_tab, text='監控對象', padding=8)
        target_options.grid(row=0, column=0, sticky='nsew')
        target_header = ttk.Frame(target_options)
        target_header.pack(fill='x', pady=(0, 4))
        description = ttk.Label(target_header, text='核心模式只監控自身；隊伍目前停用。' if self.core_monitoring_only else '監控自身與目前隊友；人物身分由 RRF 自動確認。')
        description.pack(fill='x')
        scope_box = ttk.LabelFrame(target_header, text='監控範圍', padding=6)
        scope_box.pack(fill='x', pady=(5, 2))
        scope_box.columnconfigure(0, weight=1)
        ttk.Label(scope_box, text='對象', foreground='#555555').grid(row=0, column=0, sticky='w')
        ttk.Label(scope_box, text='顯示模式', foreground='#555555').grid(row=0, column=1, padx=12)
        ttk.Label(scope_box, text='重點狀態', foreground='#555555').grid(row=0, column=2, padx=12)
        ttk.Label(scope_box, text='聲音', foreground='#555555').grid(row=0, column=3, padx=12)
        for row, scope in enumerate(TARGET_SCOPE_ORDER, start=1):
            ttk.Label(scope_box, text=scope).grid(row=row, column=0, sticky='w', pady=2)
            mode_box = ttk.Combobox(scope_box, textvariable=self.target_scope_display_mode_vars[scope], values=TARGET_DISPLAY_MODES, width=10, state='disabled' if self.core_monitoring_only and scope != '自己' else 'readonly')
            mode_box.grid(row=row, column=1, padx=12, pady=2)
            mode_box.bind('<<ComboboxSelected>>', lambda _event, selected_scope=scope: self.on_target_display_mode_changed(selected_scope))
            self.target_scope_display_mode_boxes[scope] = mode_box
            status_button = ttk.Button(scope_box, text='設定（0 項）', command=lambda selected_scope=scope: self.open_scope_status_settings(selected_scope), state=tk.DISABLED if self.core_monitoring_only and scope != '自己' else tk.NORMAL, width=14)
            status_button.grid(row=row, column=2, padx=12, pady=2)
            self.target_scope_status_buttons[scope] = status_button
            sound_check = ttk.Checkbutton(scope_box, variable=self.target_sound_vars[scope], command=self.on_target_sound_changed)
            sound_check.grid(row=row, column=3, padx=12, pady=2)
            self.target_sound_checkbuttons[scope] = sound_check
        self.update_target_sound_controls()
        self.update_scope_status_buttons()
        target_id_row = ttk.Frame(target_options)
        target_id_row.pack(fill='x', pady=(0, 4))
        ttk.Label(target_id_row, text='我的角色').pack(side='left')
        self.self_target_entry = ttk.Entry(target_id_row, width=24, textvariable=self.self_target_name_var, state='readonly')
        self.self_target_entry.pack(side='left', padx=(6, 4))
        ttk.Label(target_id_row, textvariable=self.self_id_state_var, foreground='#666666').pack(side='left', padx=(8, 0))
        ttk.Label(target_options, textvariable=self.target_scope_text_var, foreground='#666666', anchor='w').pack(fill='x', pady=(0, 4))
        named_rule_row = ttk.LabelFrame(target_options, text='指定人物規則', padding=6)
        named_rule_row.pack(fill='x', pady=(0, 4))
        ttk.Label(named_rule_row, text='人物名稱').pack(side='left')
        ttk.Entry(named_rule_row, textvariable=self.target_rule_name_var, width=22).pack(side='left', padx=(6, 8))
        ttk.Combobox(named_rule_row, textvariable=self.target_rule_mode_var, values=TARGET_DISPLAY_MODES, state='readonly', width=12).pack(side='left')
        ttk.Button(named_rule_row, text='儲存規則', command=self.apply_named_target_rule).pack(side='left', padx=(8, 4))
        ttk.Button(named_rule_row, text='移除規則', command=lambda: self.apply_named_target_rule(remove=True)).pack(side='left')
        ttk.Label(named_rule_row, textvariable=self.target_rule_summary_var, foreground='#666666').pack(side='right', padx=(8, 0))
        target_filter_row = ttk.Frame(target_options)
        target_filter_row.pack(fill='x', pady=(0, 4))
        ttk.Label(target_filter_row, text='搜尋人物名稱').pack(side='left')
        ttk.Entry(target_filter_row, textvariable=self.target_filter_var).pack(side='left', fill='x', expand=True, padx=(6, 4))
        ttk.Button(target_filter_row, text='清除', command=lambda: self.target_filter_var.set('')).pack(side='left')
        ttk.Label(target_filter_row, text='清單：', foreground='#666666').pack(side='left', padx=(10, 3))
        target_view_box = ttk.Combobox(target_filter_row, width=10, state='readonly', values=TARGET_VIEW_MODES, textvariable=self.target_view_var)
        target_view_box.pack(side='left')
        target_view_box.bind('<<ComboboxSelected>>', lambda _event: self.on_target_view_changed())
        self.target_filter_var.trace_add('write', lambda *_args: self.on_target_filter_text_changed())
        target_body = ttk.Frame(target_options)
        target_body.pack(fill='both', expand=True)
        target_body.columnconfigure(0, weight=1)
        target_body.rowconfigure(0, weight=1)
        target_columns = ('name', 'relation', 'mode', 'id')
        target_tree = ttk.Treeview(target_body, columns=target_columns, show='headings', selectmode='browse')
        target_tree.heading('name', text='人物名稱')
        target_tree.heading('relation', text='分類')
        target_tree.heading('mode', text='監控模式')
        target_tree.heading('id', text='ID')
        target_tree.column('name', width=300, minwidth=160, stretch=True, anchor='w')
        target_tree.column('relation', width=110, minwidth=90, stretch=False, anchor='w')
        target_tree.column('mode', width=100, minwidth=90, stretch=False, anchor='center')
        target_tree.column('id', width=120, minwidth=105, stretch=False, anchor='w')
        target_tree.grid(row=0, column=0, sticky='nsew')
        target_scrollbar = ttk.Scrollbar(target_body, orient='vertical', command=target_tree.yview)
        target_scrollbar.grid(row=0, column=1, sticky='ns')
        target_tree.configure(yscrollcommand=target_scrollbar.set, displaycolumns=('name', 'relation', 'mode'))
        target_tree.bind('<<TreeviewSelect>>', self.on_target_tree_select)
        self.target_tree = target_tree
        target_actions = ttk.Frame(target_body)
        target_actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        target_actions.columnconfigure(0, weight=1)
        ttk.Label(target_actions, textvariable=self.target_detail_var, foreground='#555555').grid(row=0, column=0, sticky='w', padx=(0, 8))
        action_specs = (('複製 ID', self.copy_selected_target_id), ('個別監控', self.open_selected_target_status_dialog))
        for column, (label, command) in enumerate(action_specs, start=1):
            button = ttk.Button(target_actions, text=label, command=command, state='disabled')
            button.grid(row=0, column=column, sticky='e', padx=(4, 0))
            self.target_action_buttons.append(button)
        self.rebuild_target_options()
        table_controls = ttk.Frame(monitor_tab)
        table_controls.grid(row=3, column=0, sticky='ew', pady=(0, 4))
        ttk.Label(table_controls, text='主表顯示：').pack(side='left')
        ttk.Combobox(table_controls, width=11, state='readonly', values=('啟用中', '全部', '即將到期', '已到期／結束'), textvariable=self.status_view_var).pack(side='left', padx=(4, 12))
        ttk.Label(table_controls, text='排序：').pack(side='left')
        ttk.Combobox(table_controls, width=11, state='readonly', values=('目標優先', '狀態優先', '剩餘時間'), textvariable=self.status_sort_var).pack(side='left', padx=(4, 12))
        ttk.Checkbutton(table_controls, text='顯示進階欄位', variable=self.show_technical_columns_var, command=self.toggle_technical_columns).pack(side='right', padx=(8, 0))
        ttk.Label(table_controls, textvariable=self.status_table_count_var, foreground='#666666').pack(side='right')
        table_frame = ttk.Frame(monitor_tab)
        table_frame.grid(row=4, column=0, sticky='nsew')
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ('status', 'name', 'target', 'scope', 'state', 'remaining', 'total', 'source')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', height=12)
        headings = {'status': '狀態 ID', 'name': '名稱', 'target': '目標（名稱／ID）', 'scope': '目標分類', 'state': '狀態', 'remaining': '校正後剩餘', 'total': '總時間', 'source': '事件封包'}
        widths = {'status': 82, 'name': 210, 'target': 132, 'scope': 96, 'state': 90, 'remaining': 118, 'total': 100, 'source': 92}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            width = self.safe_dimension(self.tree_column_widths.get(column), widths[column], 60, 1000)
            self.tree.column(column, width=width, anchor='center', stretch=column in {'name', 'remaining'})
        self.tree.tag_configure('yellow', background='#FFF4B8', foreground='#5C4700')
        self.tree.tag_configure('red', background='#FFE0E0', foreground='#760000')
        self.tree.tag_configure('debuff', background='#F0E3FF', foreground='#54247A')
        self.tree.tag_configure('neutral', background='#E9F0F5', foreground='#3E5666')
        self.tree.tag_configure('normal', background='#FFFFFF', foreground='#000000')
        self.tree.grid(row=0, column=0, sticky='nsew')
        self.tree.bind('<ButtonRelease-1>', self.on_tree_button_release, add='+')
        tree_y = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        tree_y.grid(row=0, column=1, sticky='ns')
        tree_x = ttk.Scrollbar(table_frame, orient='horizontal', command=self.tree.xview)
        tree_x.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.toggle_technical_columns(persist=False)
        self.update_source_controls()
        self.build_lazy_tab_placeholder(settings_tab, '開啟分頁後載入設定選項')
        return

    def ensure_settings_ui(self) -> None:
        """首次開啟設定分頁時才建立非即時控制元件。"""
        settings_tab = self.settings_tab
        if settings_tab is None or self.settings_ui_built:
            return
        self.clear_tab_children(settings_tab)
        self.settings_ui_built = True
        settings_tab.columnconfigure(0, weight=1)
        settings_tab.rowconfigure(1, weight=1)
        settings_nav = ttk.Frame(settings_tab)
        settings_nav.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        settings_host = ttk.Frame(settings_tab)
        settings_host.grid(row=1, column=0, sticky='nsew')
        settings_host.columnconfigure(0, weight=1)
        settings_host.rowconfigure(0, weight=1)
        self.settings_category_frames.clear()
        self.settings_category_buttons.clear()
        for category in ('一般', '提醒', '卡片', '資料'):
            button = ttk.Button(settings_nav, text='資料維護（進階）' if category == '資料' else category, width=16 if category == '資料' else 12, command=lambda category_name=category: self.select_settings_category(category_name))
            button.pack(side='left', padx=(0, 5))
            self.settings_category_buttons[category] = button
            frame = ttk.Frame(settings_host)
            frame.grid(row=0, column=0, sticky='nsew')
            frame.columnconfigure(0, weight=1)
            self.settings_category_frames[category] = frame
        settings = self.settings_category_frames['一般']
        ttk.Label(settings, text='選擇錄影來源。', foreground='#555555').grid(row=0, column=0, sticky='w', pady=(0, 6))
        source_box = ttk.LabelFrame(settings, text='RRF 來源', padding=8)
        source_box.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        source_box.columnconfigure(1, weight=1)
        ttk.Label(source_box, text='Replay 資料夾').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=4)
        ttk.Entry(source_box, textvariable=self.dir_var).grid(row=0, column=1, sticky='ew', pady=4)
        ttk.Button(source_box, text='選擇資料夾', command=self.choose_directory).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Label(source_box, textvariable=self.replay_source_state_var, foreground='#666666').grid(row=0, column=3, sticky='w', padx=(8, 0), pady=4)
        ttk.Label(source_box, text='指定錄影檔').grid(row=1, column=0, sticky='w', padx=(0, 8), pady=4)
        self.file_entry = ttk.Entry(source_box, textvariable=self.file_var)
        self.file_entry.grid(row=1, column=1, sticky='ew', pady=4)
        self.file_button = ttk.Button(source_box, text='選擇檔案', command=self.choose_file)
        self.file_button.grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Label(source_box, text='來源模式').grid(row=2, column=0, sticky='w', padx=(0, 8), pady=(0, 4))
        source_mode = ttk.Frame(source_box)
        source_mode.grid(row=2, column=1, columnspan=3, sticky='w', pady=(0, 4))
        ttk.Radiobutton(source_mode, text='自動讀取最新 RRF', variable=self.auto_latest_var, value=True, command=self.toggle_auto_latest).pack(side='left')
        ttk.Radiobutton(source_mode, text='手動指定單一 RRF', variable=self.auto_latest_var, value=False, command=self.toggle_auto_latest).pack(side='left', padx=(12, 0))
        import_box = ttk.LabelFrame(settings, text='匯入設定', padding=8)
        import_box.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        import_box.columnconfigure(0, weight=1)
        ttk.Label(import_box, textvariable=self.settings_import_var, foreground='#666666').grid(row=0, column=0, sticky='w')
        ttk.Button(import_box, text='選擇舊設定檔', command=self.import_legacy_settings).grid(row=0, column=1, sticky='e', padx=(8, 0))
        data_page = self.settings_category_frames['資料']
        ttk.Label(data_page, text='RO 更新後再執行完整檢查。', foreground='#555555').grid(row=0, column=0, sticky='w', pady=(0, 6))
        data_box = ttk.LabelFrame(data_page, text='RO 資料與未知項目', padding=8)
        data_box.grid(row=1, column=0, sticky='ew')
        data_box.columnconfigure(1, weight=1)
        ttk.Label(data_box, text='RO 資料夾').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=4)
        ttk.Entry(data_box, textvariable=self.ro_dir_var).grid(row=0, column=1, sticky='ew', pady=4)
        ttk.Button(data_box, text='選擇資料夾', command=self.choose_ro_directory).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Label(data_box, textvariable=self.ro_source_state_var, foreground='#666666').grid(row=0, column=3, sticky='w', padx=(8, 0), pady=4)
        self.status_reload_button = ttk.Button(data_box, text='完整檢查', command=self.reload_client_status_data)
        self.status_reload_button.grid(row=1, column=2, padx=(8, 0), pady=(0, 4))
        self.unknown_review_button = ttk.Button(data_box, textvariable=self.unknown_review_button_var, command=self.review_unknown_data)
        self.unknown_review_button.grid(row=1, column=3, padx=(8, 0), pady=(0, 4))
        self.status_reload_progressbar = ttk.Progressbar(data_box, mode='determinate', maximum=100, variable=self.status_reload_progress_var)
        self.status_reload_progressbar.grid(row=1, column=1, sticky='ew', pady=(0, 4))
        ttk.Label(data_box, textvariable=self.status_reload_progress_text_var, foreground='#666666').grid(row=2, column=1, columnspan=2, sticky='w', pady=(0, 2))
        self.catalog_cancel_button = ttk.Button(data_box, text='取消', command=self.cancel_catalog_work, state=tk.DISABLED)
        self.catalog_cancel_button.grid(row=2, column=3, sticky='e', padx=(8, 0))
        ttk.Label(data_box, text='資料狀態').grid(row=3, column=0, sticky='w', padx=(0, 8), pady=(0, 2))
        ttk.Label(data_box, textvariable=self.data_status_summary_var, foreground='#666666').grid(row=3, column=1, columnspan=2, sticky='w', pady=(0, 2))
        ttk.Label(data_box, text='待辨識').grid(row=4, column=0, sticky='w', padx=(0, 8), pady=(0, 2))
        ttk.Label(data_box, textvariable=self.unknown_data_summary_var, foreground='#666666').grid(row=4, column=1, columnspan=2, sticky='w', pady=(0, 2))
        ttk.Button(data_box, text='顯示詳細資料', command=self.show_data_details).grid(row=5, column=2, sticky='e', pady=(4, 0))
        alert_page = self.settings_category_frames['提醒']
        alert_box = ttk.LabelFrame(alert_page, text='提醒與倒數', padding=8)
        alert_box.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(alert_box, text='黃燈／紅燈門檻（秒）').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=4)
        threshold = ttk.Frame(alert_box)
        threshold.grid(row=0, column=1, sticky='w', pady=4)
        ttk.Label(threshold, text='黃').pack(side='left')
        ttk.Entry(threshold, width=7, textvariable=self.yellow_var).pack(side='left', padx=(3, 10))
        ttk.Label(threshold, text='紅').pack(side='left')
        ttk.Entry(threshold, width=7, textvariable=self.red_var).pack(side='left', padx=(3, 0))
        ttk.Label(alert_box, text='RRF 延遲校正（秒）').grid(row=1, column=0, sticky='w', padx=(0, 8), pady=4)
        sync_settings = ttk.Frame(alert_box)
        sync_settings.grid(row=1, column=1, sticky='w', pady=4)
        self.sync_entry = ttk.Entry(sync_settings, width=7, textvariable=self.sync_var)
        self.sync_entry.pack(side='left')
        ttk.Checkbutton(sync_settings, text='自動估算直播延遲', variable=self.auto_sync_var, command=self.on_auto_sync_changed).pack(side='left', padx=(8, 0))
        ttk.Button(alert_box, text='套用時間校正', command=self.apply_time_correction).grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Checkbutton(alert_box, text='新狀態提示音', variable=self.apply_sound_var, command=self.save_settings).grid(row=2, column=0, columnspan=3, sticky='w', pady=(4, 0))
        ttk.Label(alert_box, text='各狀態可在「自訂規則」分頁設定立即、黃燈與紅燈提示。', foreground='#666666').grid(row=3, column=0, columnspan=3, sticky='w', pady=(4, 0))
        sound_box = ttk.LabelFrame(alert_page, text='提示音', padding=8)
        sound_box.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        sound_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(sound_box, text='黃燈提示音', variable=self.yellow_sound_var, command=self.save_settings).grid(row=0, column=0, sticky='w', pady=4)
        ttk.Checkbutton(sound_box, text='紅燈提示音', variable=self.red_sound_var, command=self.save_settings).grid(row=0, column=1, sticky='w', padx=(8, 10), pady=4)
        ttk.Label(sound_box, text='提示音樣式').grid(row=1, column=0, sticky='w', pady=4)
        sound_mode_box = ttk.Combobox(sound_box, width=14, state='readonly', values=SOUND_MODES, textvariable=self.sound_mode_var)
        sound_mode_box.grid(row=1, column=1, sticky='w', padx=(4, 4), pady=4)
        sound_mode_box.bind('<<ComboboxSelected>>', lambda _event: self.save_settings())
        ttk.Button(sound_box, text='選 WAV', command=self.choose_sound_file).grid(row=1, column=2, padx=(6, 0), pady=4)
        ttk.Button(sound_box, text='測試提示音', command=self.test_alert_sound).grid(row=1, column=3, padx=(6, 0), pady=4)
        ttk.Label(sound_box, text='音量').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Scale(sound_box, from_=0, to=100, length=150, variable=self.sound_volume_var, command=self.on_volume_changed).grid(row=2, column=1, sticky='ew', pady=4)
        ttk.Label(sound_box, textvariable=self.volume_text_var, width=5).grid(row=2, column=2, sticky='w', padx=(8, 0), pady=4)
        card_page = self.settings_category_frames['卡片']
        card_box = ttk.LabelFrame(card_page, text='右側狀態卡片', padding=10)
        card_box.grid(row=0, column=0, sticky='ew')
        card_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(card_box, text='顯示狀態卡片', variable=self.overlay_enabled_var, command=self.toggle_overlay).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 8))
        ttk.Label(card_box, text='透明度').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Scale(card_box, from_=OVERLAY_MIN_OPACITY, to=OVERLAY_MAX_OPACITY, length=180, variable=self.overlay_opacity_var, command=self.on_overlay_opacity_changed).grid(row=1, column=1, sticky='ew', pady=4)
        ttk.Label(card_box, text='文字大小（8～14）').grid(row=2, column=0, sticky='w', pady=4)
        font_size_box = ttk.Spinbox(card_box, from_=OVERLAY_MIN_FONT_SIZE, to=OVERLAY_MAX_FONT_SIZE, width=6, state='readonly', textvariable=self.overlay_font_size_var, command=self.on_overlay_font_size_changed)
        font_size_box.grid(row=2, column=1, sticky='w', pady=4)
        font_size_box.bind('<<Increment>>', lambda _event: self.after_idle(self.on_overlay_font_size_changed))
        font_size_box.bind('<<Decrement>>', lambda _event: self.after_idle(self.on_overlay_font_size_changed))
        ttk.Checkbutton(card_box, text='自動調整高度', variable=self.overlay_auto_height_var, command=self.on_overlay_auto_height_changed).grid(row=3, column=0, columnspan=3, sticky='w', pady=4)
        ttk.Checkbutton(card_box, text='鎖定位置與大小', variable=self.overlay_locked_var, command=self.on_overlay_lock_changed).grid(row=4, column=0, columnspan=3, sticky='w', pady=4)
        ttk.Button(card_box, text='重設卡片位置', command=self.reset_overlay_position).grid(row=5, column=0, sticky='w', pady=(8, 0))
        self.select_settings_category('一般')
        self.update_unknown_review_controls()
        self.update_sync_entry_state()
        self.update_source_controls()

    def select_settings_category(self, category: str) -> None:
        """以四個簡單頁面呈現設定，避免小視窗截斷底部選項。"""
        frame = self.settings_category_frames.get(category)
        if frame is None:
            return
        self.settings_category_var.set(category)
        frame.tkraise()
        for name, button in self.settings_category_buttons.items():
            button.configure(state='disabled' if name == category else 'normal')

    def show_data_details(self) -> None:
        """把開發與來源細節收進按需開啟的視窗，避免設定頁常駐長文。"""
        messagebox.showinfo('資料詳細資訊', '\n'.join((self.status_data_report_var.get(), self.client_data_report_var.get(), self.unknown_data_summary_var.get())), parent=self)

    def on_overlay_auto_height_changed(self) -> None:
        self.overlay_auto_height = bool(self.overlay_auto_height_var.get())
        if self.overlay_auto_height:
            self.adjust_overlay_height_to_content()
        self.save_settings()

    def on_overlay_font_size_changed(self) -> None:
        size = self.safe_dimension(self.overlay_font_size_var.get(), OVERLAY_DEFAULT_FONT_SIZE, OVERLAY_MIN_FONT_SIZE, OVERLAY_MAX_FONT_SIZE)
        self.overlay_font_size = size
        self.overlay_font_size_var.set(size)
        self.overlay_font_cache = None
        self.overlay_render_signature = None
        self.overlay_structure_signature = None
        self.overlay_natural_height = self.overlay_model_natural_height(self.overlay_render_groups, self.overlay_render_pet)
        if self.overlay_auto_height:
            self.adjust_overlay_height_to_content()
        self.render_overlay_canvas()
        self.save_settings()

    def on_overlay_lock_changed(self) -> None:
        self.overlay_locked = bool(self.overlay_locked_var.get())
        if self.overlay_canvas is not None:
            self.render_overlay_canvas()
        self.save_settings()

    def reset_overlay_position(self) -> None:
        self.overlay_x = None
        self.overlay_y = None
        self.overlay_auto_height = True
        self.overlay_auto_height_var.set(True)
        if self.overlay is not None and self.overlay.winfo_exists():
            self.position_overlay_at_default()
            self.render_overlay_canvas()
        self.save_settings()

    def open_status_settings(self) -> None:
        """從主畫面的快速開始直接跳到自訂規則頁。"""
        if getattr(self, 'status_library_tab', None) is not None:
            self.notebook.select(self.status_library_tab)

    def open_target_settings(self) -> None:
        """從主畫面的快速開始直接跳到人物設定頁。"""
        if getattr(self, 'target_selection_tab', None) is not None:
            self.notebook.select(self.target_selection_tab)
        target_entry = getattr(self, 'self_target_entry', None)
        if target_entry is not None:
            target_entry.focus_set()

    def update_onboarding(self) -> None:
        """用三個步驟說明首次使用流程；完成後收起，保持主畫面簡潔。"""
        if self.onboarding_box is None:
            return
        now = time.monotonic()
        if now < self.onboarding_next_refresh_at:
            return
        self.onboarding_next_refresh_at = now + 1.0
        replay_text = self.dir_var.get().strip()
        if self.auto_latest_var.get():
            replay_ready = bool(replay_text) and Path(replay_text).is_dir()
            replay_status = '已設定資料夾' if replay_ready else '尚未找到資料夾'
        else:
            selected_file = self.file_var.get().strip()
            replay_ready = bool(selected_file) and Path(selected_file).is_file()
            replay_status = '已選擇檔案' if replay_ready else '尚未選擇檔案'
        self_name = self.self_target_name_var.get().strip()
        self_id = self.target_tracker.self_id
        self_ready = self_id is not None or bool(self_name) or (not self.core_monitoring_only)
        if self.target_tracker.packet_confirmed_self_id is not None and self_id is not None:
            self_status = f'{self.target_display_name(self_id)}，已自動確認'
        elif self_id is not None and self_name:
            self_status = f'暫時沿用 {self_name}，等待過傳點確認'
        elif self_name:
            self_status = f'已輸入 {self_name}，等待 RRF 配對'
        elif self_id is not None:
            self_status = '已有上次確認資料，等待本次過傳點'
        else:
            self_status = '等待自動辨識'
        selected_count = len(self.all_scope_status_ids())
        status_ready = selected_count > 0
        status_text = f'已選 {selected_count} 項' if status_ready else '尚未選擇'
        self.onboarding_var.set(f'請依序完成：\n1. RRF 來源：{replay_status}\n2. 我的角色：{self_status}\n3. 重點狀態：{status_text}\n完成後按上方「開始監控」。')
        if replay_ready and self_ready and status_ready:
            self.onboarding_box.grid_remove()
        else:
            self.onboarding_box.grid()

    def update_tab_labels(self) -> None:
        """在分頁標籤上顯示少量摘要，減少使用者來回切換確認的需要。"""
        if not hasattr(self, 'notebook'):
            return
        selected_count = len(self.all_scope_status_ids())
        self_name = self.self_target_name_var.get().strip()
        self_id = self.target_tracker.self_id
        target_suffix = '已設定' if self_name or self_id is not None else '待設定'
        pet_id = self.pet_tracker.snapshot().pet_id
        pet_suffix = '已辨識' if pet_id is not None else '等待資料'
        if not self.pet_monitor_enabled_var.get():
            pet_suffix = '已關閉'
        signature = (selected_count, target_suffix, pet_suffix)
        if signature == self.tab_label_signature:
            return
        self.tab_label_signature = signature
        self.notebook.tab(self.status_library_tab, text=f'自訂規則（{selected_count}）')
        self.notebook.tab(self.target_selection_tab, text=f'人物設定（{target_suffix}）')
        self.notebook.tab(self.pet_tab, text=f'寵物（{pet_suffix}）')

    @staticmethod
    def build_lazy_tab_placeholder(parent: ttk.Frame, text: str) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        ttk.Label(parent, text=text, foreground='#666666', anchor='center').grid(row=0, column=0, sticky='nsew')

    @staticmethod
    def clear_tab_children(parent: ttk.Frame) -> None:
        for child in parent.winfo_children():
            child.destroy()

    def ensure_status_library_ui(self) -> None:
        tab = self.status_library_tab
        if tab is None:
            return
        if not self.status_library_ui_built:
            self.clear_tab_children(tab)
            self.build_status_library_ui(tab)
            self.status_library_ui_built = True
        else:
            self.populate_status_job_filters()

    def populate_status_job_filters(self) -> None:
        """只在狀態頁可見時保留職業清單與官方技能索引。"""
        if self.status_job_combobox is None:
            return
        lineage_entries = job_filter_entries()
        self.status_job_value_map = {'全部職業': '全部職業', **dict(lineage_entries)}
        self.status_job_combobox.configure(values=('全部職業', *tuple((label for label, _name in lineage_entries))))

    def release_status_library_memory(self) -> None:
        """離開狀態頁即釋放可重建的搜尋與職業資料。"""
        if not self.status_library_ui_built:
            return
        self.status_library_widgets_ready = False
        self.clear_status_options()
        self.status_library_name_counts.clear()
        self.status_job_value_map = {'全部職業': '全部職業'}
        if self.status_job_combobox is not None:
            self.status_job_combobox.configure(values=('全部職業',))
        release_bundled_job_skill_catalog()

    def ensure_pet_ui(self) -> None:
        tab = self.pet_tab
        if tab is None or self.pet_ui_built:
            return
        self.clear_tab_children(tab)
        self.pet_ui_built = True
        self.build_pet_tab(tab)

    def build_status_library_ui(self, status_library_tab: ttk.Frame) -> None:
        """建立單一清單與單筆設定面板；不為每個狀態建立一組 Tk 元件。"""
        status_library_tab.columnconfigure(0, weight=1)
        status_library_tab.rowconfigure(5, weight=1)
        library_header = ttk.Frame(status_library_tab)
        library_header.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        library_header.columnconfigure(0, weight=1)
        ttk.Label(library_header, text='重點狀態', font=('Microsoft JhengHei', 11, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(library_header, textvariable=self.status_selection_summary_var, foreground='#555555').grid(row=0, column=1, sticky='e')
        scope_selector = ttk.Frame(library_header)
        scope_selector.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        ttk.Label(scope_selector, text='套用對象：').pack(side='left')
        for scope in TARGET_SCOPE_ORDER:
            button = ttk.Radiobutton(scope_selector, text=scope, value=scope, variable=self.status_edit_scope_var, command=lambda selected_scope=scope: self.select_status_edit_scope(selected_scope), state=tk.DISABLED if self.core_monitoring_only and scope != '自己' else tk.NORMAL)
            button.pack(side='left', padx=(0, 8))
            self.status_scope_edit_buttons[scope] = button
        ttk.Label(scope_selector, text='單一人物可在人物設定中調整', foreground='#666666').pack(side='right')
        category_box = ttk.LabelFrame(status_library_tab, text='快速入口', padding=6)
        category_box.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        for column in range(3):
            category_box.columnconfigure(column, weight=1)
        for index, category in enumerate(STATUS_LIBRARY_PLAYER_ENTRIES):
            category_button = ttk.Button(category_box, text=category, command=lambda entry_name=category: self.select_status_player_entry(entry_name))
            category_button.grid(row=index // 3, column=index % 3, sticky='ew', padx=(0 if index % 3 == 0 else 3, 3 if index % 3 < 2 else 0), pady=2)
            self.status_category_buttons[category] = category_button
        index_box = ttk.Frame(status_library_tab)
        index_box.grid(row=2, column=0, sticky='ew', pady=(0, 6))
        index_box.columnconfigure(3, weight=1)
        ttk.Label(index_box, text='職業').grid(row=0, column=0, sticky='w', padx=(0, 4))
        self.status_job_combobox = ttk.Combobox(index_box, textvariable=self.status_job_var, values=('全部職業',), state='readonly', width=17)
        self.status_job_combobox.grid(row=0, column=1, sticky='ew', padx=(0, 4))
        self.status_job_combobox.bind('<<ComboboxSelected>>', lambda _event: self.apply_status_filter())
        self.status_job_effect_combobox = ttk.Combobox(index_box, textvariable=self.status_job_effect_var, values=JOB_EFFECT_FILTER_ORDER, state='readonly', width=11)
        self.status_job_effect_combobox.grid(row=0, column=2, sticky='ew')
        self.status_job_effect_combobox.bind('<<ComboboxSelected>>', lambda _event: self.apply_status_filter())
        ttk.Label(index_box, text='消耗品用途').grid(row=1, column=0, sticky='w', padx=(0, 4), pady=(5, 0))
        self.status_consumable_combobox = ttk.Combobox(index_box, textvariable=self.status_consumable_subcategory_var, values=CONSUMABLE_SUBCATEGORY_ORDER, state='disabled', width=17)
        self.status_consumable_combobox.grid(row=1, column=1, columnspan=2, sticky='ew', pady=(5, 0))
        self.status_consumable_combobox.bind('<<ComboboxSelected>>', lambda _event: self.apply_status_filter())
        ttk.Label(index_box, text='用途').grid(row=2, column=0, sticky='w', padx=(0, 4), pady=(5, 0))
        self.status_function_combobox = ttk.Combobox(index_box, textvariable=self.status_function_var, values=STATUS_FUNCTION_FILTER_ORDER, state='readonly', width=24)
        self.status_function_combobox.grid(row=2, column=1, columnspan=2, sticky='ew', pady=(5, 0))
        self.status_function_combobox.bind('<<ComboboxSelected>>', lambda _event: self.apply_status_filter())
        search_row = ttk.Frame(status_library_tab)
        search_row.grid(row=3, column=0, sticky='ew', pady=(0, 6))
        ttk.Label(search_row, text='搜尋').pack(side='left')
        self.status_filter_entry = ttk.Entry(search_row, textvariable=self.status_filter_var)
        self.status_filter_entry.pack(side='left', fill='x', expand=True, padx=(6, 4))
        self.status_filter_entry.bind('<Return>', lambda _event: self.apply_status_filter())
        ttk.Button(search_row, text='清除', command=self.clear_status_filter).pack(side='left')
        self.status_filter_var.trace_add('write', lambda *_args: self.on_status_filter_changed())
        pager = ttk.Frame(status_library_tab)
        pager.grid(row=4, column=0, sticky='ew', pady=(0, 6))
        pager.columnconfigure(1, weight=1)
        self.status_page_previous_button = ttk.Button(pager, text='上一頁', command=lambda: self.change_status_page(-1), state='disabled')
        self.status_page_previous_button.grid(row=0, column=0, sticky='w')
        ttk.Label(pager, textvariable=self.status_page_info_var, foreground='#555555', anchor='center').grid(row=0, column=1, sticky='ew', padx=8)
        self.status_page_next_button = ttk.Button(pager, text='下一頁', command=lambda: self.change_status_page(1), state='disabled')
        self.status_page_next_button.grid(row=0, column=2, sticky='e')
        self.status_select_none_button = ttk.Button(pager, text='取消本頁 0 項', command=self.select_no_statuses)
        self.status_select_none_button.grid(row=0, column=4, sticky='e')
        self.status_select_all_button = ttk.Button(pager, text='勾選本頁 0 項', command=self.select_all_statuses)
        self.status_select_all_button.grid(row=0, column=3, sticky='e', padx=(8, 4))
        body = ttk.Frame(status_library_tab)
        body.grid(row=5, column=0, sticky='nsew')
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)
        list_frame = ttk.Frame(body)
        list_frame.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ('selected', 'name', 'category', 'jobs')
        tree = ttk.Treeview(list_frame, columns=columns, show='headings', selectmode='browse')
        tree.heading('selected', text='已選')
        tree.heading('name', text='狀態名稱')
        tree.heading('category', text='分類')
        tree.heading('jobs', text='技能／職業')
        tree.column('selected', width=55, minwidth=45, stretch=False, anchor='center')
        tree.column('name', width=240, minwidth=150, stretch=True, anchor='w')
        tree.column('category', width=110, minwidth=85, stretch=False, anchor='w')
        tree.column('jobs', width=180, minwidth=100, stretch=True, anchor='w')
        tree.tag_configure('selected', background='#E8F3FF')
        tree.grid(row=0, column=0, sticky='nsew')
        list_scroll = ttk.Scrollbar(list_frame, orient='vertical', command=tree.yview)
        list_scroll.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=list_scroll.set)
        tree.bind('<<TreeviewSelect>>', self.on_status_tree_select)
        tree.bind('<Double-1>', self.toggle_status_tree_selected)
        tree.bind('<space>', self.toggle_status_tree_selected)
        self.status_library_tree = tree
        detail = ttk.LabelFrame(body, text='所選狀態', padding=10)
        detail.grid(row=0, column=1, sticky='nsew')
        detail.columnconfigure(0, weight=1)
        ttk.Label(detail, textvariable=self.status_detail_name_var, font=('Microsoft JhengHei', 11, 'bold'), wraplength=290, justify='left').grid(row=0, column=0, sticky='w')
        ttk.Label(detail, textvariable=self.status_detail_meta_var, foreground='#666666', wraplength=290, justify='left').grid(row=1, column=0, sticky='w', pady=(4, 12))
        monitor_check = ttk.Checkbutton(detail, text='列入重點狀態', variable=self.status_detail_selected_var, command=self.on_status_detail_changed, state='disabled')
        monitor_check.grid(row=2, column=0, sticky='w', pady=(0, 10))
        self.status_detail_controls.append(monitor_check)
        ttk.Label(detail, text='提示音', font=('Microsoft JhengHei', 9, 'bold')).grid(row=3, column=0, sticky='w')
        for row, rule_key in enumerate(ALERT_RULE_KEYS, start=4):
            rule_check = ttk.Checkbutton(detail, text={'apply': '套用時立即提示', 'yellow': '進入黃燈時提示', 'red': '進入紅燈時提示'}[rule_key], variable=self.status_detail_alert_vars[rule_key], command=self.on_status_detail_changed, state='disabled')
            rule_check.grid(row=row, column=0, sticky='w', pady=2)
            self.status_detail_controls.append(rule_check)
        ttk.Label(detail, text='聲音範圍可在人物設定中調整', foreground='#666666', wraplength=290, justify='left').grid(row=7, column=0, sticky='sw', pady=(14, 0))
        self.update_status_filter_controls()
        self.populate_status_job_filters()

    def build_pet_tab(self, parent: ttk.Frame) -> None:
        """自身寵物資料與獨立卡片設定。"""
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        ttk.Checkbutton(toolbar, text='啟用自身寵物監控', variable=self.pet_monitor_enabled_var, command=self.on_pet_settings_changed).pack(side='left')
        ttk.Label(toolbar, text='從錄影自動辨識目前寵物', foreground='#666666').pack(side='left', padx=12)
        result = ttk.LabelFrame(parent, text='目前寵物', padding=12)
        self.pet_status_card = result
        result.grid(row=1, column=0, sticky='nsew', padx=(0, 10))
        result.columnconfigure(0, weight=1)
        ttk.Label(result, textvariable=self.pet_status_name_var, font=('Microsoft JhengHei', 14, 'bold'), wraplength=390).grid(row=0, column=0, sticky='w', pady=(0, 10))
        for row, variable in ((6, self.pet_species_var), (7, self.pet_food_var), (8, self.pet_level_var)):
            ttk.Label(result, textvariable=variable, wraplength=390).grid(row=row, column=0, sticky='w', pady=3)
        self.pet_satiety_label = ttk.Label(result, textvariable=self.pet_satiety_var, font=('Microsoft JhengHei', 16, 'bold'))
        self.pet_satiety_label.grid(row=1, column=0, sticky='w', pady=(12, 3))
        self.pet_satiety_progressbar = ttk.Progressbar(result, maximum=100, mode='determinate')
        self.pet_satiety_progressbar.grid(row=2, column=0, sticky='ew', pady=(3, 10))
        ttk.Label(result, textvariable=self.pet_intimacy_var).grid(row=5, column=0, sticky='w', pady=3)
        ttk.Label(result, textvariable=self.pet_state_var, wraplength=390).grid(row=3, column=0, sticky='w', pady=3)
        ttk.Label(result, textvariable=self.pet_hint_var, foreground='#666666', wraplength=390, justify='left').grid(row=4, column=0, sticky='w', pady=(8, 4))
        options = ttk.Frame(parent)
        options.grid(row=1, column=1, sticky='new')
        card = ttk.LabelFrame(options, text='獨立寵物卡片', padding=10)
        ttk.Checkbutton(card, text='顯示寵物卡片', variable=self.pet_overlay_enabled_var, command=self.toggle_pet_overlay).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(card, text='鎖定位置', variable=self.pet_overlay_locked_var, command=self.on_pet_overlay_settings_changed).grid(row=1, column=0, columnspan=2, sticky='w', pady=4)
        self.pet_card_options_button = ttk.Button(card, text='卡片外觀設定', command=self.toggle_pet_card_options)
        self.pet_card_options_button.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(6, 4))
        appearance = ttk.Frame(card)
        self.pet_card_options_frame = appearance
        appearance.grid(row=3, column=0, columnspan=2, sticky='ew')
        for row, label, variable, lower, upper in ((0, '字體大小', self.pet_overlay_font_size_var, 10, 20), (1, '卡片寬度', self.pet_overlay_width_var, 220, 600)):
            ttk.Label(appearance, text=label).grid(row=row, column=0, sticky='w', pady=4)
            box = ttk.Spinbox(appearance, from_=lower, to=upper, width=7, textvariable=variable, command=self.on_pet_overlay_settings_changed)
            box.grid(row=row, column=1, sticky='e', padx=(14, 0))
            box.bind('<FocusOut>', lambda _event: self.on_pet_overlay_settings_changed())
            box.bind('<Return>', lambda _event: self.on_pet_overlay_settings_changed())
        ttk.Label(appearance, text='不透明度').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Scale(appearance, from_=0.4, to=1.0, variable=self.pet_overlay_opacity_var, command=lambda _value: self.on_pet_overlay_settings_changed()).grid(row=2, column=1, sticky='ew')
        ttk.Button(appearance, text='重設寵物卡片位置', command=self.reset_pet_overlay_position).grid(row=3, column=0, columnspan=2, sticky='ew', pady=(8, 4))
        appearance.grid_remove()
        ttk.Label(card, text='可拖曳移動；隱藏卡片不會停止提醒。', foreground='#666666', wraplength=245).grid(row=4, column=0, columnspan=2, sticky='w', pady=3)
        reminder = ttk.LabelFrame(options, text='飽食提醒', padding=10)
        reminder.pack(fill='x')
        ttk.Checkbutton(reminder, text='低飽食時提醒我', variable=self.pet_alert_enabled_var, command=self.on_pet_settings_changed).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(reminder, text='飽食度 ≤').grid(row=1, column=0, sticky='w', pady=5)
        threshold = ttk.Spinbox(reminder, from_=0, to=100, width=6, textvariable=self.pet_alert_threshold_var, command=self.on_pet_settings_changed)
        threshold.grid(row=1, column=1, sticky='w', padx=(8, 0))
        threshold.bind('<FocusOut>', lambda _event: self.on_pet_settings_changed())
        threshold.bind('<Return>', lambda _event: self.on_pet_settings_changed())
        ttk.Label(reminder, textvariable=self.pet_alert_state_var, foreground='#666666', wraplength=245).grid(row=2, column=0, columnspan=2, sticky='w', pady=4)
        ttk.Label(reminder, text='新低值：一般 60 秒，0～10 為 20 秒。\n降入 0～10 立即提醒。\n回升超過門檻後停止。', foreground='#666666', wraplength=245).grid(row=3, column=0, columnspan=2, sticky='w', pady=3)
        card.pack(fill='x', pady=(10, 0))
        self.refresh_pet_panel()

    def toggle_pet_card_options(self) -> None:
        if self.pet_card_options_frame.winfo_manager():
            self.pet_card_options_frame.grid_remove()
            self.pet_card_options_button.configure(text='卡片外觀設定')
        else:
            self.pet_card_options_frame.grid()
            self.pet_card_options_button.configure(text='收起外觀設定')

    def refresh_pet_catalog(self) -> None:
        if self.pet_catalog_tree is None:
            return
        state = self.pet_tracker.snapshot()
        pet_id = state.pet_id
        pet_name = state.pet_name.strip() if state.pet_name else ''
        if not pet_name and pet_id is not None:
            pet_name = PET_NAMES_BY_ID.get(pet_id, '')
        render_key = (pet_id, pet_name)
        if render_key == self.pet_catalog_render_key:
            return
        self.pet_catalog_render_key = render_key
        for child in self.pet_catalog_tree.get_children():
            self.pet_catalog_tree.delete(child)
        if pet_id is None:
            self.pet_catalog_count_var.set('目前尚未從 RRF 偵測到自身寵物；開始餵食或切換場景後會自動更新。')
            self.pet_selection_summary_var.set('等待自身寵物資料；不需要手動輸入寵物 ID。')
            return
        display_name = pet_name or '未取得遊戲內名稱'
        self.pet_catalog_tree.insert('', 'end', values=(pet_id, display_name))
        self.pet_catalog_count_var.set('目前偵測到 1 隻自身寵物｜名稱來自 RRF 寵物資料封包')

    def on_pet_catalog_selected(self) -> None:
        if self.pet_catalog_tree is None:
            return
        selected = self.pet_catalog_tree.selection()
        if not selected:
            return
        values = self.pet_catalog_tree.item(selected[0], 'values')
        if not values:
            return
        self.pet_selected_id_var.set(self.format_target_id(int(values[0])))

    def apply_pet_selection(self) -> None:
        value = self.pet_selected_id_var.get().strip()
        pet_id = self.parse_target_id(value)
        if value and pet_id is None:
            self.set_message('寵物 ID 格式錯誤，請輸入十進位或 0x 開頭的 ID')
            return
        self.pet_tracker.set_selected_pet_id(pet_id)
        self.pet_selected_id_var.set(self.format_target_id(pet_id) if pet_id is not None else '')
        self.save_settings()
        self.refresh_pet_panel()
        self.set_message(f'已設定追蹤寵物：{PET_NAMES_BY_ID.get(pet_id, self.format_target_id(pet_id))}' if pet_id is not None else '已清除追蹤寵物')

    def on_pet_settings_changed(self) -> None:
        enabled = bool(self.pet_monitor_enabled_var.get())
        with self.config_lock:
            changed = enabled != self.pet_monitor_enabled
            self.pet_monitor_enabled = enabled
            self.pet_alert_enabled = bool(self.pet_alert_enabled_var.get())
            self.pet_alert_threshold = max(0, min(100, int(self.safe_float(self.pet_alert_threshold_var.get(), PET_ALERT_DEFAULT_THRESHOLD))))
        if changed:
            if enabled and self.monitoring_is_active():
                self.rescan()
            else:
                self.reset_pet_tracking()
        self.pet_panel_render_key = None
        self.save_settings()
        self.refresh_pet_panel()
        self.refresh_pet_overlay()
        self.update_tab_labels()

    def pet_data_status(self, state: PetMonitorState) -> tuple[bool, str]:
        if not self.pet_monitor_enabled_var.get():
            return (False, '寵物監控已關閉')
        if not self.monitoring_is_active():
            return (False, '監控已停止' if self.monitoring_has_started else '等待開始監控')
        if state.pet_id is None:
            return (False, '等待自身寵物資料')
        if self.incremental_parser.suppress_apply_events:
            return (False, '正在讀取錄影資料')
        updated = self.last_rrf_data_monotonic
        if self.current_path is None or updated is None or time.monotonic() - updated > 10:
            return (False, '上次資料｜等待錄影更新')
        key = (state.identity_generation, state.satiety_revision)
        if key != self.__dict__.get('pet_live_observation_key'):
            return (False, '上次資料｜等待寵物更新')
        return (True, '資料接收中')

    def refresh_pet_panel(self) -> None:
        if not self.__dict__.get('pet_ui_built', False):
            return
        state = self.pet_tracker.snapshot()
        valid, data_text = self.pet_data_status(state)
        threshold = max(0, min(100, int(self.safe_float(self.pet_alert_threshold_var.get(), PET_ALERT_DEFAULT_THRESHOLD))))
        info = get_pet_info(state.pet_id) if state.pet_id is not None else None
        species = info.name_zh if info is not None else None
        food = info.food_name_zh if info is not None else None
        render_key = (state.pet_id, state.pet_name, state.level, state.satiety, state.intimacy, state.state_text, data_text, species, food, self.pet_alert_enabled_var.get(), threshold)
        if render_key == self.pet_panel_render_key:
            return
        self.pet_panel_render_key = render_key
        self.pet_selected_id_var.set(self.format_target_id(state.pet_id) if state.pet_id is not None else '')
        self.pet_status_name_var.set(state.pet_name or species or '等待自身寵物資料')
        self.pet_species_var.set(f"種類：{species or ('尚無對照' if state.pet_id is not None else '等待資料')}")
        self.pet_food_var.set(f"食物：{food or ('尚無對照' if state.pet_id is not None else '等待資料')}")
        self.pet_level_var.set(f"等級：{(state.level if state.level is not None else '—')}")
        self.pet_satiety_var.set(f'飽食度：{state.satiety}/100' if state.satiety is not None else '飽食度：—')
        self.pet_intimacy_var.set(f'親密度：{state.intimacy}/1000' if state.intimacy is not None else '親密度：—')
        low = valid and state.satiety is not None and (state.satiety <= threshold)
        self.pet_state_var.set(f"狀態：{('需要餵食' if low else state.state_text if valid else data_text)}")
        self.pet_hint_var.set(data_text if state.pet_id is not None else '收到目前自身寵物的資料後會自動顯示。')
        self.pet_source_var.set(state.source_text)
        self.pet_selection_summary_var.set(state.pet_name or data_text)
        self.pet_satiety_label.configure(foreground='#A94024' if low else '#202B32' if valid else '#777777')
        self.pet_satiety_progressbar.configure(value=state.satiety or 0)
        if not self.pet_alert_enabled_var.get():
            alert_text = '聲音提醒已關閉'
        elif not valid:
            alert_text = '等待有效資料，暫不提醒'
        else:
            alert_text = '需要餵食' if low else '尚未達提醒門檻'
        self.pet_alert_state_var.set(alert_text)

    def on_pet_overlay_change(self, settings: dict) -> None:
        self.pet_overlay_settings.update(settings)
        self.pet_overlay_locked_var.set(bool(settings.get('pet_overlay_locked', False)))
        self.pet_overlay_width_var.set(int(settings.get('pet_overlay_width', 280)))
        self.schedule_settings_save()

    def on_pet_overlay_settings_changed(self) -> None:
        if self.pet_overlay is not None and self.pet_overlay.winfo_exists():
            self.pet_overlay_settings.update(self.pet_overlay.export_settings())

        def number(variable, default):
            try:
                return float(variable.get())
            except (tk.TclError, ValueError, TypeError):
                return default
        self.pet_overlay_settings.update({'pet_overlay_locked': bool(self.pet_overlay_locked_var.get()), 'pet_overlay_width': max(220, min(600, int(number(self.pet_overlay_width_var, 280)))), 'pet_overlay_font_size': max(10, min(20, int(number(self.pet_overlay_font_size_var, 12)))), 'pet_overlay_opacity': max(0.4, min(1.0, number(self.pet_overlay_opacity_var, 0.9)))})
        if self.pet_overlay is not None:
            self.pet_overlay.update_settings(self.pet_overlay_settings)
        self.schedule_settings_save()

    def reset_pet_overlay_position(self) -> None:
        self.pet_overlay_settings.update({'pet_overlay_x': None, 'pet_overlay_y': None})
        if self.pet_overlay is not None:
            self.pet_overlay.update_settings(self.pet_overlay_settings)
        self.schedule_settings_save()

    def toggle_pet_overlay(self) -> None:
        self.refresh_pet_overlay()
        self.save_settings()

    def close_pet_overlay(self) -> None:
        self.pet_overlay_enabled_var.set(False)
        self.hide_pet_overlay()
        self.save_settings()

    def hide_pet_overlay(self) -> None:
        overlay = self.__dict__.get('pet_overlay')
        if overlay is not None and overlay.winfo_exists():
            overlay.hide()

    def refresh_pet_overlay(self) -> None:
        if not (self.monitoring_is_active() and self.pet_monitor_enabled_var.get() and self.pet_overlay_enabled_var.get()):
            self.hide_pet_overlay()
            return
        state = self.pet_tracker.snapshot()
        valid, data_text = self.pet_data_status(state)
        if self.pet_overlay is None or not self.pet_overlay.winfo_exists():
            self.pet_overlay = PetOverlay(self, self.pet_overlay_settings, self.on_pet_overlay_change, self.close_pet_overlay)
        info = get_pet_info(state.pet_id) if state.pet_id is not None else None
        name = state.pet_name or (info.name_zh if info is not None else None) or '等待自身寵物資料'
        threshold = max(0, min(100, int(self.safe_float(self.pet_alert_threshold_var.get(), PET_ALERT_DEFAULT_THRESHOLD))))
        low = valid and state.satiety is not None and (state.satiety <= threshold)
        self.pet_overlay.set_content(name=name, satiety=state.satiety, intimacy=state.intimacy, message='需要餵食' if low else data_text, low=low, stale=not valid)
        self.pet_overlay.show()

    def reset_pet_tracking(self) -> None:
        lock = self.__dict__.get('pet_alert_lock')
        if lock is None:
            self.pet_tracker.reset()
            return
        with lock:
            self.pet_tracker.reset()
            self.pet_alert_controller.reset()
            self.pet_alert_events.clear()
            self.pet_live_observation_key = None

    def seed_replay_pet_snapshot(self, snapshot: ReplayPetSnapshot | None) -> None:
        """初始化限目前錄影的自身；歷史初值永遠不進即時提醒佇列。"""
        if snapshot is None or snapshot.owner_id != self.target_tracker.self_id:
            return
        lock = self.__dict__.get('pet_alert_lock')
        if lock is None:
            if self.pet_monitor_enabled:
                self.pet_tracker.seed_replay_snapshot(snapshot)
            return
        with lock:
            if not self.monitoring_should_continue() or not self.pet_monitor_enabled:
                return
            if self.pet_tracker.seed_replay_snapshot(snapshot):
                self.pet_alert_controller.reset()
                self.pet_alert_events.clear()
                self.pet_live_observation_key = None

    def consume_pet_packets(self, packets: list[ReplayPacket], reset_required: bool) -> None:
        """依封包順序處理觀測；與 UI 提醒共用鎖，避免播出已被替換的值。"""
        controller = self.__dict__.get('pet_alert_controller')
        if controller is None:
            self.pet_tracker.consume(packets)
            return
        live = not reset_required and (not self.incremental_parser.suppress_apply_events)
        with self.config_lock:
            enabled = self.pet_alert_enabled
            monitor_enabled = self.pet_monitor_enabled
            threshold = self.pet_alert_threshold
        with self.pet_alert_lock:
            if not self.monitoring_should_continue() or not self.pet_monitor_enabled:
                return
            observations = self.pet_tracker.consume(packets)
            current_state = self.pet_tracker.snapshot()
            if current_state.pet_id is None:
                self.pet_live_observation_key = None
                self.pet_alert_events.clear()
            for state in observations:
                if state.identity_generation != current_state.identity_generation:
                    continue
                key = (state.identity_generation, state.satiety_revision)
                self.pet_live_observation_key = key if live else None
                pending = [(event, queued_at) for event, queued_at in self.pet_alert_events if live and state.satiety is not None and (state.satiety <= threshold) and (event.identity_generation == state.identity_generation) and (event.threshold == threshold) and ((event.satiety <= CRITICAL_SATIETY_THRESHOLD) == (state.satiety <= CRITICAL_SATIETY_THRESHOLD))]
                self.pet_alert_events.clear()
                self.pet_alert_events.extend(pending)
                event = controller.evaluate(state, enabled=enabled, monitoring_enabled=monitor_enabled, live=live, valid=live, threshold=threshold)
                if event is not None:
                    self.pet_alert_events.append((event, time.monotonic()))

    @serialized_scoped_monitor
    def process_pet_alerts(self) -> None:
        with self.pet_alert_lock:
            if not (self.monitoring_is_active() and self.pet_monitor_enabled_var.get() and self.pet_alert_enabled_var.get()):
                self.pet_alert_events.clear()
                return
            state = self.pet_tracker.snapshot()
            now = time.monotonic()
            while self.pet_alert_events and now - self.pet_alert_events[0][1] > 5:
                self.pet_alert_events.popleft()
            valid, _message = self.pet_data_status(state)
            if not valid:
                return
            threshold = max(0, min(100, int(self.safe_float(self.pet_alert_threshold_var.get(), PET_ALERT_DEFAULT_THRESHOLD))))
            events = [event for event, _at in self.pet_alert_events if event.identity_generation == state.identity_generation and event.threshold == threshold and (state.satiety is not None) and ((event.satiety <= CRITICAL_SATIETY_THRESHOLD) == (state.satiety <= CRITICAL_SATIETY_THRESHOLD))]
            self.pet_alert_events.clear()
            if events and state.satiety is not None and (state.satiety <= threshold):
                self.play_alert_sound('pet', state.pet_name or '寵物', state.satiety)

    def invalidate_stale_pet_data(self) -> None:
        """來源一段時間未更新後，須等新的寵物觀測才恢復有效。"""
        updated = self.last_rrf_data_monotonic
        if updated is None or time.monotonic() - updated <= 10:
            return
        with self.pet_alert_lock:
            self.pet_live_observation_key = None
            self.pet_alert_events.clear()

    def refresh_status_options(self, states: list[tuple[StatusState, int | None]]) -> None:
        if self.status_library_tree is None:
            return
        observed_ids = {state.status_id for state, _remaining in states}
        configured_ids = self.all_scope_status_ids()
        signature = (frozenset(observed_ids), frozenset(configured_ids), self.status_option_generation)
        if signature == self.status_option_signature:
            return
        self.status_option_signature = signature
        all_status_ids = sorted(set(EFST_NAMES) | observed_ids | configured_ids | {FOCUS_STATUS_ID})
        readable_ids = sorted((status_id for status_id in all_status_ids if is_readable_status_name(status_id)))
        self.status_unresolved_ids = set(all_status_ids) - set(readable_ids)
        source_headers = {status_id: 2435 for status_id in readable_ids}
        self.status_scope_text_var.set(f'可選狀態：{len(readable_ids)} 項｜目前錄影已出現：{len(observed_ids)} 項｜內部保留未命名／編碼：{len(self.status_unresolved_ids)} 項')
        category_counts = Counter((status_library_category(status_id, source_headers.get(status_id, 2435)) for status_id in readable_ids))
        self.status_category_summary_var.set('分類數量：' + '｜'.join((f'{category} {category_counts.get(category, 0)}' for category in STATUS_LIBRARY_CATEGORY_ORDER)) + f'｜未命名／編碼 {len(self.status_unresolved_ids)}（僅供內部解析）')
        self.status_selection_summary_var.set(f'{self.current_status_edit_scope()}｜可選 {len(readable_ids)} 項；已勾選 {len(self.status_selected_ids)} 項')
        self.update_selected_status_summary()
        new_ids = set(readable_ids)
        if new_ids == self.status_option_ids and source_headers == self.status_option_source_headers:
            return
        self.status_option_ids = new_ids
        self.status_option_source_headers = source_headers
        self.status_library_records = {status_id: StatusLibraryRecord(status_id=status_id, source_header=source_headers.get(status_id, 2435), name=status_name(status_id, source_headers.get(status_id, 2435)), category=status_library_category(status_id, source_headers.get(status_id, 2435)), jobs=tuple(status_job_names(status_id)), skills=tuple(status_skill_names(status_id)), effect_group=status_group(status_id, source_headers.get(status_id, 2435)), consumable_subcategory=status_consumable_subcategory(status_id, source_headers.get(status_id, 2435)), aliases=(str(EFST_NAMES.get(status_id, '')), *RUNTIME_STATUS_METADATA.get(status_id, {}).get('aliases', ()), *tuple((str(value) for value in RUNTIME_STATUS_METADATA.get(status_id, {}).get('item_names', [])))), functional_category=str(RUNTIME_STATUS_METADATA.get(status_id, {}).get('functional_category', '其他／待確認')), source_tags=tuple((str(value) for value in RUNTIME_STATUS_METADATA.get(status_id, {}).get('source_tags', [])))) for status_id in readable_ids}
        self.status_library_index = StatusSearchIndex(self.status_library_records.values())
        available_consumables = available_consumable_subcategories(self.status_library_index)
        if self.status_consumable_combobox is not None:
            self.status_consumable_combobox.configure(values=available_consumables)
        if self.status_consumable_subcategory_var.get() not in available_consumables:
            self.status_consumable_subcategory_var.set(available_consumables[0])
        self.status_library_name_counts = dict(Counter((record.name for record in self.status_library_records.values())))
        if self.status_library_widgets_ready:
            self.rebuild_status_options()

    def on_notebook_tab_changed(self) -> None:
        """只在使用者開啟對應分頁時刷新該分頁，避免切頁時同步處理全部內容。"""
        selected_tab = self.notebook.select()
        if self.status_library_tab is not None and selected_tab != str(self.status_library_tab) and self.status_library_widgets_ready:
            self.release_status_library_memory()
        if self.status_library_tab is not None and selected_tab == str(self.status_library_tab):
            self.ensure_status_library_ui()
            self.status_library_widgets_ready = True
            self.refresh_status_options(self.tracker.snapshot())
        elif self.target_selection_tab is not None and selected_tab == str(self.target_selection_tab):
            self.refresh_target_options(self.tracker.snapshot(), force=True)
        elif self.pet_tab is not None and selected_tab == str(self.pet_tab):
            self.ensure_pet_ui()
            self.refresh_pet_catalog()
            self.refresh_pet_panel()
        elif self.settings_tab is not None and selected_tab == str(self.settings_tab):
            self.ensure_settings_ui()

    def toggle_status_scope(self) -> None:
        self.refresh_status_options(self.tracker.snapshot())
        self.save_settings()

    def select_status_category(self, category: str) -> None:
        if category not in STATUS_LIBRARY_CATEGORY_ORDER:
            return
        self.status_index_mode_var.set(STATUS_LIBRARY_INDEX_MODES[0])
        self.status_category_var.set(category)
        self.status_function_var.set(STATUS_FUNCTION_FILTER_ORDER[0])
        self.status_page_index = 0
        self.update_status_filter_controls()
        if self.status_library_widgets_ready:
            self.rebuild_status_options()

    def select_status_player_entry(self, entry: str) -> None:
        """把玩家入口轉成既有輕量索引條件，不建立第二份狀態資料。"""
        if entry not in STATUS_LIBRARY_PLAYER_ENTRIES:
            return
        self.status_function_var.set(STATUS_FUNCTION_FILTER_ORDER[0])
        if entry == '職業技能':
            self.status_index_mode_var.set('職業技能')
        elif entry == '已選擇':
            self.status_index_mode_var.set('已勾選')
        else:
            self.status_index_mode_var.set('分類')
            self.status_category_var.set({'消耗品': '消耗品', '異常狀態': 'DEBUFF', '其他／搜尋': '其他'}[entry])
        self.status_page_index = 0
        self.update_status_filter_controls()
        if self.status_library_widgets_ready:
            self.rebuild_status_options()

    def select_status_index_mode(self, mode: str) -> None:
        """切換原分類或官方職業技能索引；只在清單頁需要時重建目前一頁。"""
        if mode not in STATUS_LIBRARY_INDEX_MODES:
            return
        self.status_index_mode_var.set(mode)
        self.status_page_index = 0
        self.update_status_filter_controls()
        if self.status_library_widgets_ready:
            self.rebuild_status_options()

    def update_status_filter_controls(self) -> None:
        """只啟用目前檢視方式需要的篩選器，減少選項混淆。"""
        mode = self.status_index_mode_var.get()
        if self.status_job_combobox is not None:
            self.status_job_combobox.configure(state='readonly' if mode == '職業技能' else 'disabled')
        if self.status_job_effect_combobox is not None:
            self.status_job_effect_combobox.configure(state='readonly' if mode == '職業技能' else 'disabled')
        if self.status_consumable_combobox is not None:
            self.status_consumable_combobox.configure(state='readonly' if mode == '分類' and self.status_category_var.get() == '消耗品' else 'disabled')
        if self.status_function_combobox is not None:
            self.status_function_combobox.configure(state='readonly')

    def on_status_filter_changed(self) -> None:
        """搜尋條件改變時延後更新，避免中文輸入法每次組字都重建畫面。"""
        if self.status_filter_job is not None:
            try:
                self.after_cancel(self.status_filter_job)
            except tk.TclError:
                pass
            self.status_filter_job = None
        if not self.status_library_widgets_ready:
            return
        try:
            self.status_filter_job = self.after(180, self.apply_status_filter)
        except (tk.TclError, RuntimeError):
            self.status_filter_job = None

    def apply_status_filter(self) -> None:
        """套用延後的狀態搜尋結果。"""
        self.status_filter_job = None
        self.status_page_index = 0
        if self.status_library_widgets_ready:
            self.rebuild_status_options()

    def change_status_page(self, offset: int) -> None:
        if not self.status_library_widgets_ready:
            return
        self.status_page_index = max(0, self.status_page_index + int(offset))
        self.rebuild_status_options()

    def filtered_status_ids(self) -> set[int]:
        """回傳目前搜尋結果；全選操作只作用於畫面上可見的項目。"""
        return set(self.filtered_status_id_order())

    def filtered_status_id_order(self) -> tuple[int, ...]:
        return self.status_library_index.filter(query=self.status_filter_var.get(), mode=self.status_index_mode_var.get(), category=self.status_category_var.get(), job=self.status_job_value_map.get(self.status_job_var.get(), self.status_job_var.get()), effect=self.status_job_effect_var.get(), consumable_subcategory=self.status_consumable_subcategory_var.get(), selected_ids=set(self.status_selected_ids) | {FOCUS_STATUS_ID} if self.status_index_mode_var.get() == '常用狀態' else set(self.status_selected_ids), functional_category=self.status_function_var.get())

    def sync_tracker_status_filter(self) -> None:
        """更新唯一 policy；狀態事實不再因畫面清單被提前刪除。"""
        self.tracker.set_allowed_status_ids(None)
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.set_resolver(self.current_policy_resolver())
        self.sync_tracker_target_filter()

    def sync_tracker_target_filter(self) -> None:
        """預設只解析自己與隊伍；自己尚未確認時暫緩收窄以保留辨識線索。"""
        self.target_scope_display_modes_snapshot = {scope: self.scope_display_mode(scope) for scope in TARGET_SCOPE_ORDER}
        self.target_display_mode_overrides_snapshot = dict(self.__dict__.get('target_display_mode_overrides', {}))
        self.target_policy_resolver_snapshot = self.current_policy_resolver()
        self.sync_tracker_target_filter_snapshot()

    def target_display_mode_from_snapshot(self, target_id: int) -> str:
        """背景執行緒使用的純資料判斷，不讀取任何 Tk 元件。"""
        if target_id not in self.target_tracker.trusted_target_ids():
            return TARGET_DISPLAY_MODE_OFF
        relation = self.target_tracker.relation_for(target_id)
        resolver = self.__dict__.get('target_policy_resolver_snapshot')
        if resolver is None:
            return self.target_scope_display_modes_snapshot.get(relation, TARGET_DISPLAY_MODE_OFF)
        policy = resolver.resolve(target_id=target_id, relation=relation, target_name=self.target_tracker.target_name(target_id) or '')
        return legacy_mode(policy.mode)

    def target_is_monitored_from_snapshot(self, target_id: int) -> bool:
        return self.target_display_mode_from_snapshot(target_id) != TARGET_DISPLAY_MODE_OFF

    @serialized_scoped_monitor
    def sync_tracker_target_filter_snapshot(self) -> None:
        """保留有上限的狀態事實；人物規則只在快照層決定顯示。"""
        trusted = self.target_tracker.trusted_target_ids()
        self_id = self.target_tracker.self_id
        signature = self.target_tracker.scoped_identity_signature()
        previous = self.__dict__.get('_scoped_identity_signature', (None, (), -1))
        previous_ids = self.__dict__.get('_scoped_trusted_ids', frozenset())
        changed_self = previous[0] != self_id
        changed_generation = previous[2] != signature[2]
        revoked = previous_ids - trusted
        self._scoped_identity_signature = signature
        self._scoped_trusted_ids = frozenset(trusted)
        if changed_self:
            self.tracker.reset()
            self.reset_pet_tracking()
        elif changed_generation:
            self.tracker.set_allowed_target_ids({self_id} if self_id is not None else set())
        self.tracker.set_allowed_target_ids(set(trusted))
        self.tracker.set_protected_target_ids(trusted)
        session = self.__dict__.get('monitor_session')
        if session is not None:
            if changed_self:
                session.store.clear()
            elif changed_generation:
                session.set_trusted_targets(self_id=self_id)
            session.set_trusted_targets(self_id=self_id, party_ids=trusted - {self_id})
        if changed_self or changed_generation or revoked:
            self._clear_scoped_transient_data()
        return

    def update_selected_status_summary(self) -> None:
        """以固定長度摘要目前勾選狀態，不讓主表被長文字向下擠壓。"""
        self.sync_visible_status_checks()
        self.sync_tracker_status_filter()
        selected_ids = sorted(self.status_selected_ids)
        labels = (status_name(status_id, self.status_option_source_headers.get(status_id, 2435)) for status_id in selected_ids[:3])
        scope = self.current_status_edit_scope()
        compact = compact_status_summary(labels, len(selected_ids))
        scope_counts = []
        for target_scope in TARGET_SCOPE_ORDER:
            count = len(self.status_selected_ids if target_scope == scope else self.target_scope_status_ids.get(target_scope, set()))
            mode = self.scope_display_mode(target_scope)
            if mode == TARGET_DISPLAY_MODE_ALL:
                scope_counts.append(f'{TARGET_SCOPE_DISPLAY_NAMES.get(target_scope, target_scope)}：全部')
            elif mode == TARGET_DISPLAY_MODE_FOCUSED:
                scope_counts.append(f'{TARGET_SCOPE_DISPLAY_NAMES.get(target_scope, target_scope)}：重點（{count}）')
        counts_text = '｜'.join(scope_counts) or '尚未選擇'
        self.selected_statuses_var.set(f'顯示範圍｜{counts_text}\u3000{scope}重點：{compact}')
        self.status_selection_summary_var.set(f'{scope}｜可選 {len(self.status_option_ids)}｜已選 {len(selected_ids)}')
        self.update_scope_status_buttons()

    def rebuild_status_options(self) -> None:
        """差異更新單一 Treeview；不再建立或銷毀每個狀態的控制項。"""
        tree = self.status_library_tree
        if tree is None or not self.status_library_widgets_ready:
            return
        selected_count = len(self.status_selected_ids)
        self.status_selection_summary_var.set(f'{self.current_status_edit_scope()}｜可選 {len(self.status_option_ids)}｜已選 {selected_count}')
        filtered_ids = self.filtered_status_id_order()
        page = paginate_status_ids(filtered_ids, self.status_page_index, STATUS_LIBRARY_PAGE_SIZE)
        self.status_page_index = page.page_index
        visible_ids = list(page.ids)
        self.status_current_page_ids = page.ids
        query = self.status_filter_var.get().strip()
        mode = self.status_index_mode_var.get()
        if query:
            context = f'搜尋「{query}」'
        elif mode == '職業技能':
            context = f'{self.status_job_var.get()}｜{self.status_job_effect_var.get()}'
        elif mode == '已勾選':
            context = '已勾選'
        else:
            context = self.status_category_var.get()
            if context == '消耗品':
                context = f'{context}｜{self.status_consumable_subcategory_var.get()}'
        if self.status_function_var.get() != STATUS_FUNCTION_FILTER_ORDER[0]:
            context = f'{context}｜{self.status_function_var.get()}'
        self.status_page_info_var.set(f'{context}｜共 {page.total_count} 項｜第 {page.page_index + 1}/{page.page_count} 頁')
        if self.status_page_previous_button is not None:
            self.status_page_previous_button.configure(state='normal' if page.page_index > 0 else 'disabled')
        if self.status_page_next_button is not None:
            self.status_page_next_button.configure(state='normal' if page.page_index < page.page_count - 1 else 'disabled')
        if hasattr(self, 'status_select_all_button'):
            self.status_select_all_button.configure(text=f'勾選本頁 {len(visible_ids)} 項')
        if hasattr(self, 'status_select_none_button'):
            self.status_select_none_button.configure(text=f'取消本頁 {len(visible_ids)} 項')
        desired = set(visible_ids)
        for status_id, item_id in list(self.status_tree_row_ids.items()):
            if status_id not in desired or not tree.exists(item_id):
                if tree.exists(item_id):
                    tree.delete(item_id)
                self.status_tree_status_ids.pop(item_id, None)
                self.status_tree_row_ids.pop(status_id, None)
                self.status_tree_row_values.pop(status_id, None)
        order_changed = visible_ids != self.status_tree_order_ids
        for index, status_id in enumerate(visible_ids):
            values = self.status_library_row_values(status_id)
            item_id = self.status_tree_row_ids.get(status_id)
            tags = ('selected',) if status_id in self.status_selected_ids else ()
            if item_id is None or not tree.exists(item_id):
                item_id = tree.insert('', 'end', values=values, tags=tags)
                self.status_tree_row_ids[status_id] = item_id
                self.status_tree_status_ids[item_id] = status_id
                self.status_tree_row_values[status_id] = values
            else:
                if self.status_tree_row_values.get(status_id) != values:
                    tree.item(item_id, values=values)
                    self.status_tree_row_values[status_id] = values
                if tuple(tree.item(item_id, 'tags')) != tags:
                    tree.item(item_id, tags=tags)
            if order_changed:
                tree.move(item_id, '', index)
        self.status_tree_order_ids = visible_ids
        if self.status_detail_status_id in desired:
            item_id = self.status_tree_row_ids[self.status_detail_status_id]
            if tree.selection() != (item_id,):
                tree.selection_set(item_id)
        elif self.status_detail_status_id is not None:
            selection = tree.selection()
            if selection:
                tree.selection_remove(*selection)

    def status_library_row_values(self, status_id: int) -> tuple[object, ...]:
        record = self.status_library_records.get(status_id)
        if record is None:
            source_header = self.status_option_source_headers.get(status_id, 2435)
            name = status_name(status_id, source_header)
            category = status_library_category(status_id, source_header)
            jobs = tuple(status_job_names(status_id))
            skills = tuple(status_skill_names(status_id))
            functional_category = '其他／待確認'
        else:
            name = record.name
            category = record.category
            jobs = record.jobs
            skills = record.skills
            functional_category = record.functional_category
        display_name = name
        source_labels = skills or jobs
        source_text = '、'.join(source_labels[:3]) if source_labels else '—'
        return ('✓' if status_id in self.status_selected_ids else '', display_name, functional_category if functional_category != '其他／待確認' else category, source_text)

    def status_id_from_tree_selection(self) -> int | None:
        tree = self.status_library_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        return self.status_tree_status_ids.get(item_id)

    def on_status_tree_select(self, _event: tk.Event | None=None) -> None:
        status_id = self.status_id_from_tree_selection()
        if status_id is None:
            return
        self.status_detail_status_id = status_id
        record = self.status_library_records.get(status_id)
        source_header = self.status_option_source_headers.get(status_id, 2435)
        name = record.name if record is not None else status_name(status_id, source_header)
        category = record.category if record is not None else status_library_category(status_id, source_header)
        jobs = record.jobs if record is not None else tuple(status_job_names(status_id))
        skills = record.skills if record is not None else tuple(status_skill_names(status_id))
        functional_category = record.functional_category if record is not None else '其他／待確認'
        job_text = '、'.join(jobs) if jobs else '未建立職業索引'
        skill_text = '、'.join(skills) if skills else '未建立技能連結'
        self.status_detail_name_var.set(name)
        self.status_detail_meta_var.set(f'分類：{category}｜用途：{functional_category}｜ID：{status_id}\n技能：{skill_text}\n適用職業：{job_text}')
        self.status_detail_selected_var.set(status_id in self.status_selected_ids)
        rule = self.default_status_alert_rule(status_id)
        rule.update(self.status_alert_rules.get(status_id, {}))
        for key in ALERT_RULE_KEYS:
            self.status_detail_alert_vars[key].set(bool(rule.get(key, False)))
        for control in self.status_detail_controls:
            control.configure(state='normal')

    def on_status_detail_changed(self) -> None:
        status_id = self.status_detail_status_id
        if status_id is None:
            return
        selected = bool(self.status_detail_selected_var.get())
        self.set_status_selected(status_id, selected)
        self.status_alert_rules[status_id] = {key: bool(self.status_detail_alert_vars[key].get()) for key in ALERT_RULE_KEYS}
        self.status_alert_rule_touched.add(status_id)
        self.sync_tracker_status_filter()
        self.update_selected_status_summary()
        if self.status_index_mode_var.get() == '已勾選' and (not selected):
            self.rebuild_status_options()
        else:
            tree = self.status_library_tree
            item_id = self.status_tree_row_ids.get(status_id)
            if tree is not None and item_id is not None and tree.exists(item_id):
                values = self.status_library_row_values(status_id)
                tree.item(item_id, values=values, tags=('selected',) if selected else ())
                self.status_tree_row_values[status_id] = values
            self.status_selection_summary_var.set(f'{self.current_status_edit_scope()}｜可選 {len(self.status_option_ids)}｜已選 {len(self.status_selected_ids)}')
        self.save_settings()

    def toggle_status_tree_selected(self, event: tk.Event | None=None) -> str:
        tree = self.status_library_tree
        if tree is None:
            return 'break'
        if event is not None and getattr(event, 'y', None) is not None:
            item_id = tree.identify_row(event.y)
            if item_id:
                tree.selection_set(item_id)
        self.on_status_tree_select()
        if self.status_detail_status_id is not None:
            self.status_detail_selected_var.set(not self.status_detail_selected_var.get())
            self.on_status_detail_changed()
        return 'break'

    def bind_status_scroll(self, widget: tk.Misc) -> None:
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            widget.bind(sequence, self.on_status_options_mousewheel, add='+')

    def on_status_options_mousewheel(self, event: tk.Event) -> str:
        canvas: tk.Canvas | None = None
        widget = getattr(event, 'widget', None)
        while widget is not None:
            if widget is getattr(self, 'status_options_canvas', None):
                canvas = self.status_options_canvas
                break
            widget = getattr(widget, 'master', None)
        if canvas is None:
            return 'break'
        if getattr(event, 'num', None) == 4:
            units = -1
        elif getattr(event, 'num', None) == 5:
            units = 1
        else:
            delta = getattr(event, 'delta', 0)
            units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
        canvas.yview_scroll(units, 'units')
        return 'break'

    def select_status_group(self, group: str, selected: bool) -> None:
        for status_id in self.filtered_status_ids():
            source_header = self.status_option_source_headers.get(status_id, 2435)
            if status_library_category(status_id, source_header) == group:
                self.set_status_selected(status_id, selected)
        self.save_settings()
        self.rebuild_status_options()

    def clear_status_filter(self) -> None:
        self.status_filter_var.set('')
        self.status_function_var.set(STATUS_FUNCTION_FILTER_ORDER[0])
        self.status_job_var.set('全部職業')
        self.status_job_effect_var.set(JOB_EFFECT_FILTER_ORDER[0])
        self.status_consumable_subcategory_var.set(CONSUMABLE_SUBCATEGORY_ORDER[0])
        self.apply_status_filter()

    def clear_status_options(self) -> None:
        self.status_rebuild_token += 1
        if self.status_rebuild_job is not None:
            try:
                self.after_cancel(self.status_rebuild_job)
            except tk.TclError:
                pass
            self.status_rebuild_job = None
        if self.status_filter_job is not None:
            try:
                self.after_cancel(self.status_filter_job)
            except tk.TclError:
                pass
            self.status_filter_job = None
        if self.status_options_inner is not None:
            for child in self.status_options_inner.winfo_children():
                child.destroy()
        if self.status_library_tree is not None:
            children = self.status_library_tree.get_children('')
            if children:
                self.status_library_tree.delete(*children)
        self.status_tree_row_ids.clear()
        self.status_tree_status_ids.clear()
        self.status_tree_row_values.clear()
        self.status_tree_order_ids.clear()
        self.status_current_page_ids = ()
        self.status_detail_status_id = None
        self.status_detail_name_var.set('請從清單選擇狀態')
        self.status_detail_meta_var.set('選取後可設定監控與提示')
        for control in self.status_detail_controls:
            control.configure(state='disabled')
        self.status_checkbuttons.clear()
        self.status_checks.clear()
        self.status_option_ids.clear()
        self.status_option_source_headers.clear()
        self.status_library_records.clear()
        self.status_library_index = StatusSearchIndex()
        if self.status_consumable_combobox is not None:
            self.status_consumable_combobox.configure(values=(CONSUMABLE_SUBCATEGORY_ORDER[0],))
        self.status_consumable_subcategory_var.set(CONSUMABLE_SUBCATEGORY_ORDER[0])
        self.status_option_signature = None
        self.status_unresolved_ids.clear()
        self.status_page_index = 0
        self.status_page_info_var.set('等待狀態資料載入')
        if self.status_page_previous_button is not None:
            self.status_page_previous_button.configure(state='disabled')
        if self.status_page_next_button is not None:
            self.status_page_next_button.configure(state='disabled')
        self.status_options_empty = None
        if self.status_library_widgets_ready:
            self.rebuild_status_options()
        if hasattr(self, 'status_options_canvas'):
            self.status_options_canvas.yview_moveto(0)

    def on_status_checkbox_changed(self) -> None:
        self.sync_visible_status_checks()
        self.sync_tracker_status_filter()
        self.update_selected_status_summary()
        self.save_settings()
        self.rebuild_status_options()

    def select_all_statuses(self) -> None:
        status_ids = set(self.status_current_page_ids)
        if len(status_ids) > 20 and (not messagebox.askyesno('確認批次勾選', f'目前這一頁有 {len(status_ids)} 項。\n\n確定全部加入監控嗎？', parent=self)):
            return
        for status_id in status_ids:
            self.set_status_selected(status_id, True)
        self.sync_tracker_status_filter()
        self.save_settings()
        self.rebuild_status_options()

    def select_no_statuses(self) -> None:
        for status_id in self.status_current_page_ids:
            self.set_status_selected(status_id, False)
        self.sync_tracker_status_filter()
        self.save_settings()
        self.rebuild_status_options()

    @serialized_scoped_monitor
    def refresh_target_options(self, states: list[tuple[StatusState, int | None]], force: bool=False) -> None:
        """更新目標 ID 清單；同一狀態在不同目標上維持各自一列。"""
        now = time.monotonic()
        if not force and now < self.target_options_next_refresh_at:
            return
        self.target_options_next_refresh_at = now + TARGET_OPTIONS_REFRESH_INTERVAL_SECONDS
        observed_ids = {state.target_id for state, _remaining in states}
        observed_ids.update(self.target_tracker.known_target_ids())
        observed_ids.intersection_update(self.target_tracker.trusted_target_ids())
        self_candidate_ids = set(self.target_tracker.self_candidate_ids())
        observed_ids = {target_id for target_id in observed_ids if self.target_scope_is_enabled(target_id) or (target_id in self_candidate_ids and self.target_tracker.scope_enabled('自己'))}
        active_status_target_ids = {state.target_id for state, remaining in states if state.active and (remaining is None or remaining > 0) and (state.target_id in observed_ids)}
        recent_ids = self.target_tracker.recent_target_ids(active_status_target_ids)
        relation_map = {target_id: self.target_tracker.relation_for(target_id) for target_id in observed_ids}
        name_map = {target_id: target_name for target_id in observed_ids if (target_name := self.target_tracker.target_name(target_id))}
        self_id = self.target_tracker.self_id
        self_candidates = self.target_tracker.self_candidate_ids()
        self.self_target_name_var.set(self.target_tracker.target_name(self_id) or '' if self_id else '')
        if self_id is None:
            self.self_target_id_var.set('')
        if self_id is not None:
            self.self_target_id_var.set(self.format_target_id(self_id))
        configured_name = self.self_target_name_var.get().strip()
        self_summary = self.target_display_name(self_id) if self_id else configured_name or '未判定'
        if self_id is None and self_candidates:
            self_summary += f'（有 {len(self_candidates)} 個候選）'
        if self.target_tracker.packet_confirmed_self_id is not None:
            self_text = f'自己：{self_summary}  ✓ 已自動確認'
        else:
            self_text = f'自己：{self_summary}（{self.target_tracker.self_id_source}）'
        self.target_scope_text_var.set(f'{self_text}｜人物：{len(observed_ids)}｜最近出現：{len(recent_ids & observed_ids)}')
        if observed_ids == self.target_option_ids and relation_map == self.target_option_relations and (name_map == self.target_option_names) and (recent_ids & observed_ids == self.target_option_recent_ids):
            return
        self.target_option_ids = observed_ids
        self.target_option_relations = relation_map
        self.target_option_names = name_map
        self.target_option_recent_ids = recent_ids & observed_ids
        self.sync_tracker_target_filter()
        self.rebuild_target_options()

    def rebuild_target_options(self) -> None:
        tree = self.target_tree
        if tree is None:
            return
        self_candidate_ids = set(self.target_tracker.self_candidate_ids())
        visible_ids = sorted(self.filtered_target_ids(), key=lambda target_id: (TARGET_SCOPE_ORDER.index(self.target_option_relations.get(target_id, '其他目標')), self.target_option_names.get(target_id, '').casefold(), target_id))
        desired = set(visible_ids)
        for target_id, item_id in list(self.target_tree_row_ids.items()):
            if target_id not in desired or not tree.exists(item_id):
                if tree.exists(item_id):
                    tree.delete(item_id)
                self.target_tree_row_ids.pop(target_id, None)
                self.target_tree_row_values.pop(target_id, None)
        order_changed = visible_ids != self.target_tree_order_ids
        for index, target_id in enumerate(visible_ids):
            relation = self.target_option_relations.get(target_id, '其他目標')
            target_name = self.target_option_names.get(target_id, '') or self.format_target_id(target_id)
            if target_id in self_candidate_ids and self.target_tracker.self_id is None:
                target_name = f'候選自己｜{target_name}'
            values = (target_name, TARGET_SCOPE_DISPLAY_NAMES.get(relation, relation), self.target_display_mode_for(target_id), self.format_target_id(target_id))
            item_id = self.target_tree_row_ids.get(target_id)
            tags = ()
            if item_id is None or not tree.exists(item_id):
                item_id = tree.insert('', 'end', values=values, tags=tags)
                self.target_tree_row_ids[target_id] = item_id
                self.target_tree_row_values[target_id] = values
            else:
                if self.target_tree_row_values.get(target_id) != values:
                    tree.item(item_id, values=values)
                    self.target_tree_row_values[target_id] = values
                if tuple(tree.item(item_id, 'tags')) != tags:
                    tree.item(item_id, tags=tags)
            if order_changed:
                tree.move(item_id, '', index)
        self.target_tree_order_ids = visible_ids
        if not visible_ids:
            if not self.target_option_ids:
                message = '開始監控後顯示人物'
            elif self.target_view_var.get() == '最近出現' and (not self.target_option_recent_ids):
                message = '目前沒有最近出現的人物'
            else:
                message = '沒有符合搜尋條件的人物'
            self.target_detail_target_id = None
            self.target_detail_var.set(message)
            for button in self.target_action_buttons:
                button.configure(state='disabled')
        elif self.target_detail_target_id in desired:
            item_id = self.target_tree_row_ids[self.target_detail_target_id]
            if tree.selection() != (item_id,):
                tree.selection_set(item_id)
        elif self.target_detail_target_id is not None:
            self.target_detail_target_id = None
            self.target_detail_var.set('請從清單選擇人物')
            for button in self.target_action_buttons:
                button.configure(state='disabled')

    def target_id_from_tree_selection(self) -> int | None:
        tree = self.target_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        for target_id, known_item_id in self.target_tree_row_ids.items():
            if known_item_id == item_id:
                return target_id
        return None

    @serialized_scoped_monitor
    def on_target_tree_select(self, _event: tk.Event | None=None) -> None:
        target_id = self.target_id_from_tree_selection()
        if target_id is None:
            return
        if target_id not in self.target_tracker.trusted_target_ids():
            self.target_detail_target_id = None
            self.target_detail_var.set('')
            for button in self.target_action_buttons:
                button.configure(state='disabled')
            return
        self.target_detail_target_id = target_id
        relation = self.target_option_relations.get(target_id, '其他目標')
        self.target_detail_var.set(f'{self.target_display_name(target_id)}｜{TARGET_SCOPE_DISPLAY_NAMES.get(relation, relation)}｜{self.target_display_mode_for(target_id)}')
        for button in self.target_action_buttons:
            button.configure(state='normal')

    @serialized_scoped_monitor
    def set_selected_target_as_self(self) -> None:
        return

    def copy_selected_target_id(self) -> None:
        if self.target_detail_target_id is not None:
            self.copy_target_id(self.target_detail_target_id)

    def open_selected_target_status_dialog(self) -> None:
        if self.target_detail_target_id is not None:
            self.open_target_status_dialog(self.target_detail_target_id)

    @serialized_scoped_monitor
    def filtered_target_ids(self) -> set[int]:
        """回傳目前搜尋結果；分類與 ID 操作只影響目前畫面上的項目。"""
        query = self.target_filter_var.get().strip().casefold()
        visible: set[int] = set()
        for target_id in self.target_option_ids:
            if target_id not in self.target_tracker.trusted_target_ids():
                continue
            if self.target_view_var.get() == '最近出現' and target_id not in self.target_option_recent_ids:
                continue
            relation = self.target_option_relations.get(target_id, '其他目標')
            target_name = self.target_option_names.get(target_id, '')
            search_text = f'{target_name} {target_id} 0x{target_id:08X} {relation}'.casefold()
            if not query or query in search_text:
                visible.add(target_id)
        return visible

    def on_target_filter_changed(self) -> None:
        if self.core_monitoring_only:
            for scope in TARGET_SCOPE_ORDER:
                mode = TARGET_DISPLAY_MODE_ALL if scope == '自己' else TARGET_DISPLAY_MODE_OFF
                self.target_scope_display_mode_vars[scope].set(mode)
                self.target_scope_vars[scope].set(scope == '自己')
        else:
            for scope in TARGET_SCOPE_ORDER:
                enabled = bool(self.target_scope_vars[scope].get())
                mode = self.scope_display_mode(scope)
                if enabled and mode == TARGET_DISPLAY_MODE_OFF:
                    self.target_scope_display_mode_vars[scope].set(TARGET_DISPLAY_MODE_FOCUSED)
                elif not enabled and mode != TARGET_DISPLAY_MODE_OFF:
                    self.target_scope_display_mode_vars[scope].set(TARGET_DISPLAY_MODE_OFF)
        self.target_tracker.set_enabled_scopes((scope for scope in TARGET_SCOPE_ORDER if self.scope_display_mode(scope) != TARGET_DISPLAY_MODE_OFF))
        self.sync_tracker_status_filter()
        self.save_settings()
        self.refresh_target_options(self.tracker.snapshot(), force=True)
        self.update_target_sound_controls()
        enabled = [f'{scope}（{self.scope_display_mode(scope)}）' for scope in TARGET_SCOPE_ORDER if self.scope_display_mode(scope) != TARGET_DISPLAY_MODE_OFF]
        self.set_message('監控範圍已更新：' + ('、'.join(enabled) if enabled else '目前全部停用'))

    def on_target_display_mode_changed(self, scope: str) -> None:
        """切換人物類型模式，同步資料收集、顯示與重點狀態控制。"""
        if scope not in TARGET_SCOPE_ORDER:
            return
        mode = self.scope_display_mode(scope)
        self.target_scope_vars[scope].set(mode != TARGET_DISPLAY_MODE_OFF)
        self.on_target_filter_changed()
        self.update_scope_status_buttons()

    def update_target_sound_controls(self) -> None:
        """未顯示的人物不能啟用聲音，但保留原本的勾選值。"""
        for scope, checkbutton in self.target_sound_checkbuttons.items():
            enabled = self.scope_display_mode(scope) != TARGET_DISPLAY_MODE_OFF
            if self.core_monitoring_only and scope != '自己':
                enabled = False
                self.target_sound_vars[scope].set(False)
            checkbutton.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def on_target_sound_changed(self) -> None:
        if self.core_monitoring_only:
            for scope in TARGET_SCOPE_ORDER:
                self.target_sound_vars[scope].set(scope == '自己')
        self.save_settings()
        enabled = [scope for scope in TARGET_SCOPE_ORDER if self.target_sound_vars[scope].get()]
        self.set_message('允許提示音：' + ('、'.join(enabled) if enabled else '目前全部靜音'))

    def on_target_filter_text_changed(self) -> None:
        """人物搜尋使用防抖，避免中文輸入法每次組字都重建整頁。"""
        if self.target_filter_job is not None:
            try:
                self.after_cancel(self.target_filter_job)
            except (tk.TclError, RuntimeError):
                pass
            self.target_filter_job = None
        if self.target_tree is None:
            return
        try:
            self.target_filter_job = self.after(450, self.apply_target_filter)
        except (tk.TclError, RuntimeError):
            self.target_filter_job = None

    def apply_target_filter(self) -> None:
        """套用延後的人物搜尋結果。"""
        self.target_filter_job = None
        if self.target_tree is not None:
            self.rebuild_target_options()

    def on_target_view_changed(self) -> None:
        self.save_settings()
        self.rebuild_target_options()

    @serialized_scoped_monitor
    def apply_replay_snapshot_identity(self, target_id: int, target_name: str) -> None:
        """保存目前 RRF 標頭中的錄影角色資料。"""
        if not 0 < int(target_id) <= 4294967295:
            return
        if self.target_tracker.self_id != target_id:
            return
        if self.target_tracker.snapshot_self_id != target_id:
            return
        if self.target_tracker.packet_confirmed_self_id is not None:
            return
        name = str(target_name or '').strip()
        self.target_tracker.set_configured_self_id(target_id)
        self.self_target_id_var.set(self.format_target_id(target_id))
        self.self_target_name_var.set(name)
        self.target_tracker.set_configured_self_name(name or None)
        if self.core_monitoring_only:
            self.tracker.set_allowed_target_ids({target_id})
        self.save_settings()
        self.refresh_target_options(self.tracker.snapshot(), force=True)
        self.update_source_controls()
        self.update_onboarding()
        self.update_tab_labels()
        display_name = name or self.format_target_id(target_id)
        self.set_message(f'已由 RRF 錄影資料自動確認自己：{display_name}')

    @serialized_scoped_monitor
    def apply_packet_confirmed_self_id(self, target_id: int) -> None:
        """在 Tk 主執行緒保存 RRF 自動確認結果並更新玩家畫面。"""
        if not 0 < int(target_id) <= 4294967295:
            return
        if self.target_tracker.packet_confirmed_self_id != target_id:
            return
        target_name = self.target_tracker.target_name(target_id) or ''
        self.target_tracker.set_configured_self_id(target_id)
        self.self_target_id_var.set(self.format_target_id(target_id))
        self.self_target_name_var.set(target_name)
        self.target_tracker.set_configured_self_name(target_name or None)
        if self.core_monitoring_only:
            self.tracker.set_allowed_target_ids({target_id})
        self.save_settings()
        self.refresh_target_options(self.tracker.snapshot(), force=True)
        self.update_source_controls()
        self.update_onboarding()
        self.update_tab_labels()
        display_name = target_name or self.format_target_id(target_id)
        self.set_message(f'已由 RRF 過傳點自動確認自己：{display_name}')

    @serialized_scoped_monitor
    def apply_resolved_self_name(self, target_id: int, target_name: str) -> None:
        """人物詳細資料稍後到達時，補上並保存已確認自身的遊戲名稱。"""
        name = str(target_name).strip()
        if not name or self.target_tracker.self_id != target_id or self.target_tracker.target_name(target_id) != name:
            return
        if self.self_target_name_var.get().strip() == name:
            return
        self.self_target_name_var.set(name)
        self.target_tracker.set_configured_self_name(name)
        self.save_settings()
        self.refresh_target_options(self.tracker.snapshot(), force=True)
        self.update_source_controls()
        self.update_onboarding()
        self.set_message(f'已由 RRF 取得自己人物名稱：{name}')

    @serialized_scoped_monitor
    def apply_self_target_name(self) -> None:
        """以遊戲內人物名稱尋找自己；找不到時先記住名稱，等待後續 RRF 封包。"""
        return

    def apply_named_target_rule(self, *, remove: bool=False) -> None:
        """以人物名稱保存長期規則；不要求玩家知道暫時性人物 ID。"""
        name = self.target_rule_name_var.get().strip()
        if not name:
            self.set_message('請先輸入遊戲內人物名稱')
            return
        raw_overrides = self.settings.get('target_overrides', {})
        overrides = dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
        matched_key = next((key for key in overrides if str(key).strip().casefold() == name.casefold()), name)
        if remove:
            overrides.pop(matched_key, None)
            message = f'已移除人物規則：{name}'
        else:
            mode = normalize_mode(self.target_rule_mode_var.get(), MODE_AUTO)
            custom_ids = sorted(self.status_selected_ids) if mode == MODE_CUSTOM else []
            overrides[matched_key] = {'mode': mode, 'custom_status_ids': custom_ids}
            message = f'已儲存人物規則：{name}｜{legacy_mode(mode)}'
            if mode == MODE_CUSTOM:
                message += f'（目前自訂清單 {len(custom_ids)} 項）'
        self.settings['target_overrides'] = overrides
        self.target_rule_summary_var.set(f'已保存人物名稱規則：{len(overrides)} 條')
        self.monitor_session.set_resolver(self.current_policy_resolver())
        self.save_settings()
        self.rebuild_target_options()
        self.set_message(message)

    @serialized_scoped_monitor
    def apply_self_target_id(self) -> None:
        return

    @serialized_scoped_monitor
    def set_self_from_target(self, target_id: int) -> None:
        return

    @serialized_scoped_monitor
    def copy_target_id(self, target_id: int) -> None:
        if target_id not in self.target_tracker.trusted_target_ids():
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self.format_target_id(target_id))
            self.update()
            self.set_message(f'已複製目標 ID：{self.format_target_id(target_id)}')
        except tk.TclError:
            self.set_message('無法複製目標 ID，請手動記錄')

    @serialized_scoped_monitor
    def open_target_status_dialog(self, target_id: int) -> None:
        """開啟單一人物的狀態清單；不改變全域狀態選擇。"""
        if target_id not in self.target_tracker.trusted_target_ids():
            return
        dialog = tk.Toplevel(self)
        dialog.title(f'人物個別監控設定｜{self.target_display_name(target_id)}')
        dialog.geometry('720x680')
        dialog.minsize(620, 540)
        dialog.transient(self)
        existing_override = target_id in self.target_status_overrides
        if not self.status_option_ids:
            self.refresh_status_options(self.tracker.snapshot())
        self.sync_visible_status_checks()
        relation = self.target_tracker.relation_for(target_id)
        scope_ids = set(self.status_selected_ids if relation == self.current_status_edit_scope() else self.target_scope_status_ids.get(relation, set()))
        option_ids = sorted(set(self.status_option_ids) | {status_id for status_id in scope_ids if is_readable_status_name(status_id)} | {status_id for status_id in self.target_status_overrides.get(target_id, set()) if is_readable_status_name(status_id)})
        selected_ids = set(self.target_status_overrides.get(target_id, set())) if existing_override else scope_ids
        working_ids = set(selected_ids)
        page_status_vars: dict[int, tk.BooleanVar] = {}
        follow_global_var = tk.BooleanVar(value=not existing_override)
        display_mode_var = tk.StringVar(value=self.target_display_mode_overrides.get(target_id, TARGET_DISPLAY_MODE_INHERIT))
        target_name = (self.target_tracker.target_name(target_id) or '').strip()
        existing_alert_override = self.target_alert_overrides.get(target_name.casefold(), {})
        existing_alert_enabled = existing_alert_override.get('enabled')
        alert_mode_var = tk.StringVar(value='允許提示音' if existing_alert_enabled is True else '此人物靜音' if existing_alert_enabled is False else '跟隨人物類型')
        category_var = tk.StringVar(value=STATUS_LIBRARY_CATEGORY_ORDER[0])
        filter_var = tk.StringVar()
        page_index = 0
        page_info_var = tk.StringVar(value='')
        search_after_id: str | None = None
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)
        header = ttk.LabelFrame(dialog, text='目前人物', padding=8)
        header.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 6))
        ttk.Label(header, text=f'{self.target_tracker.relation_for(target_id)}｜{self.target_display_name(target_id)}｜{self.format_target_id(target_id)}', font=('Microsoft JhengHei', 10, 'bold')).pack(anchor='w')
        ttk.Label(header, text='可單獨決定顯示全部、只顯示重點，或不監控；沒有覆寫時跟隨人物類型。', foreground='#666666').pack(anchor='w', pady=(3, 0))
        mode_row = ttk.Frame(header)
        mode_row.pack(fill='x', pady=(5, 0))
        ttk.Label(mode_row, text='顯示模式').pack(side='left')
        ttk.Combobox(mode_row, textvariable=display_mode_var, values=(TARGET_DISPLAY_MODE_INHERIT, *TARGET_DISPLAY_MODES), state='readonly', width=16).pack(side='left', padx=(8, 0))
        ttk.Label(mode_row, text='提示音').pack(side='left', padx=(18, 0))
        alert_mode_box = ttk.Combobox(mode_row, textvariable=alert_mode_var, values=('跟隨人物類型', '允許提示音', '此人物靜音'), state='readonly' if target_name else 'disabled', width=14)
        alert_mode_box.pack(side='left', padx=(8, 0))
        ttk.Checkbutton(header, text=f'跟隨{relation}的監控狀態', variable=follow_global_var).pack(anchor='w', pady=(5, 0))
        controls = ttk.Frame(dialog)
        controls.grid(row=1, column=0, sticky='ew', padx=10, pady=(0, 6))
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text='分類').grid(row=0, column=0, sticky='w')
        category_box = ttk.Combobox(controls, width=18, state='readonly', values=STATUS_LIBRARY_CATEGORY_ORDER, textvariable=category_var)
        category_box.grid(row=0, column=1, sticky='w', padx=(5, 12))
        ttk.Label(controls, text='搜尋').grid(row=0, column=2, sticky='w')
        ttk.Entry(controls, textvariable=filter_var).grid(row=0, column=3, sticky='ew', padx=(5, 5))
        ttk.Button(controls, text='清除', command=lambda: filter_var.set('')).grid(row=0, column=4, sticky='e')
        body_frame = ttk.Frame(dialog)
        body_frame.grid(row=2, column=0, sticky='nsew', padx=10)
        body_frame.columnconfigure(0, weight=1)
        body_frame.rowconfigure(1, weight=1)
        page_controls = ttk.Frame(body_frame)
        page_controls.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        previous_button = ttk.Button(page_controls, text='上一頁', width=8)
        previous_button.pack(side='left')
        ttk.Label(page_controls, textvariable=page_info_var, foreground='#555555').pack(side='left', padx=8)
        next_button = ttk.Button(page_controls, text='下一頁', width=8)
        next_button.pack(side='left')
        ttk.Button(page_controls, text='全選目前結果', command=lambda: set_visible_statuses(True)).pack(side='right', padx=(6, 0))
        ttk.Button(page_controls, text='全不選目前結果', command=lambda: set_visible_statuses(False)).pack(side='right')
        canvas = tk.Canvas(body_frame, highlightthickness=0)
        canvas.grid(row=1, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(body_frame, orient='vertical', command=canvas.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = ttk.Frame(canvas)
        inner.columnconfigure(0, weight=1)
        inner.columnconfigure(1, weight=1)
        window_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        self.bind_status_scroll(canvas)
        self.bind_status_scroll(inner)
        footer = ttk.Frame(dialog)
        footer.grid(row=3, column=0, sticky='ew', padx=10, pady=10)
        ttk.Label(footer, text='套用後更新此人物的個別監控清單。', foreground='#666666').pack(side='left')
        ttk.Button(footer, text='取消', command=dialog.destroy).pack(side='right', padx=(6, 0))

        def visible_status_ids() -> list[int]:
            query = filter_var.get().strip().casefold()
            result: list[int] = []
            for status_id in option_ids:
                source_header = self.status_option_source_headers.get(status_id, 2435)
                category = status_library_category(status_id, source_header)
                record = self.status_library_records.get(status_id)
                search_text = record.search_text if record is not None else f'{status_name(status_id, source_header)} {status_id} 0x{status_id:04X} {category}'.casefold()
                if category == category_var.get() and (not query or query in search_text):
                    result.append(status_id)
            return result

        def set_visible_statuses(selected: bool) -> None:
            for status_id in visible_status_ids():
                if selected:
                    working_ids.add(status_id)
                else:
                    working_ids.discard(status_id)
            rebuild()

        def update_working_status(status_id: int, variable: tk.BooleanVar) -> None:
            if variable.get():
                working_ids.add(status_id)
            else:
                working_ids.discard(status_id)

        def rebuild() -> None:
            nonlocal page_index
            for child in inner.winfo_children():
                child.destroy()
            page_status_vars.clear()
            visible_ids = visible_status_ids()
            total_pages = max(1, (len(visible_ids) + STATUS_LIBRARY_PAGE_SIZE - 1) // STATUS_LIBRARY_PAGE_SIZE)
            page_index = min(page_index, total_pages - 1)
            page_start = page_index * STATUS_LIBRARY_PAGE_SIZE
            page_ids = visible_ids[page_start:page_start + STATUS_LIBRARY_PAGE_SIZE]
            page_info_var.set(f'{category_var.get()}｜共 {len(visible_ids)} 項｜第 {page_index + 1}/{total_pages} 頁')
            previous_button.configure(state='normal' if page_index > 0 else 'disabled')
            next_button.configure(state='normal' if page_index < total_pages - 1 else 'disabled')
            if not page_ids:
                ttk.Label(inner, text='沒有符合搜尋條件的可讀名稱狀態').grid(row=0, column=0, columnspan=2, sticky='w', padx=4, pady=4)
            for index, status_id in enumerate(page_ids):
                variable = tk.BooleanVar(value=status_id in working_ids)
                page_status_vars[status_id] = variable
                source_header = self.status_option_source_headers.get(status_id, 2435)
                checkbutton = ttk.Checkbutton(inner, text=f'{status_name(status_id, source_header)}  [0x{status_id:04X}]', variable=variable, command=lambda selected_id=status_id, selected_var=variable: update_working_status(selected_id, selected_var))
                checkbutton.grid(row=index // 2, column=index % 2, sticky='w', padx=4, pady=3)
                self.bind_status_scroll(checkbutton)
            inner.update_idletasks()
            canvas.yview_moveto(0)

        def move_page(offset: int) -> None:
            nonlocal page_index
            page_index = max(0, page_index + offset)
            rebuild()

        @serialized_scoped_monitor
        def save_dialog(_app) -> None:
            if target_id not in self.target_tracker.trusted_target_ids():
                dialog.destroy()
                return
            selected_mode = display_mode_var.get()
            if selected_mode == TARGET_DISPLAY_MODE_INHERIT:
                self.target_display_mode_overrides.pop(target_id, None)
            elif selected_mode in TARGET_DISPLAY_MODES:
                self.target_display_mode_overrides[target_id] = selected_mode
            if follow_global_var.get():
                self.target_status_overrides.pop(target_id, None)
                message = f'已套用人物設定：{self.target_display_mode_for(target_id)}，跟隨{relation}重點清單'
            else:
                self.target_status_overrides[target_id] = set(working_ids)
                message = f'已套用人物設定：{self.target_display_mode_for(target_id)}，個別重點 {len(self.target_status_overrides[target_id])} 項'
            if target_name:
                normalized_name = target_name.casefold()
                existing_alert = dict(self.target_alert_overrides.get(normalized_name, {}))
                selected_alert_mode = alert_mode_var.get()
                if selected_alert_mode == '跟隨人物類型':
                    existing_alert.pop('enabled', None)
                else:
                    existing_alert['enabled'] = selected_alert_mode == '允許提示音'
                if existing_alert:
                    self.target_alert_overrides[normalized_name] = existing_alert
                else:
                    self.target_alert_overrides.pop(normalized_name, None)
            self.sync_tracker_status_filter()
            self.save_settings()
            self.rebuild_target_options()
            self.set_message(message)
            dialog.destroy()
        previous_button.configure(command=lambda: move_page(-1))
        next_button.configure(command=lambda: move_page(1))
        ttk.Button(footer, text='套用個別設定', command=lambda: save_dialog(self)).pack(side='right')

        def reset_dialog_page() -> None:
            nonlocal page_index
            page_index = 0
            rebuild()

        def schedule_search_rebuild() -> None:
            nonlocal search_after_id
            if search_after_id is not None:
                try:
                    dialog.after_cancel(search_after_id)
                except tk.TclError:
                    pass
            search_after_id = dialog.after(STATUS_SEARCH_DEBOUNCE_MS, reset_dialog_page)
        category_box.bind('<<ComboboxSelected>>', lambda _event: reset_dialog_page())
        filter_var.trace_add('write', lambda *_args: schedule_search_rebuild())
        rebuild()
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    @serialized_scoped_monitor
    def target_scope_is_enabled(self, target_id: int) -> bool:
        if target_id not in self.target_tracker.trusted_target_ids():
            return False
        relation = self.target_tracker.relation_for(target_id)
        policy = self.current_policy_resolver().resolve(target_id=target_id, relation=relation, target_name=self.target_tracker.target_name(target_id) or '')
        return policy.mode != MODE_OFF

    def target_sound_is_enabled(self, target_id: int) -> bool:
        """人物是否允許發聲；未知關係一律套用「其他目標」安全設定。"""
        relation = self.target_tracker.relation_for(target_id)
        variable = self.target_sound_vars.get(relation)
        return bool(variable is not None and variable.get())

    @serialized_scoped_monitor
    def target_is_selected(self, target_id: int) -> bool:
        """相容舊呼叫名稱；唯一權限是人物 effective policy。"""
        return self.target_scope_is_enabled(target_id)

    def clear_target_options(self) -> None:
        if self.target_tree is not None:
            for item_id in self.target_tree.get_children(''):
                self.target_tree.delete(item_id)
        self.target_option_ids.clear()
        self.target_option_relations.clear()
        self.target_option_names.clear()
        self.target_option_recent_ids.clear()
        self.target_tree_row_ids.clear()
        self.target_tree_row_values.clear()
        self.target_tree_order_ids.clear()
        self.target_detail_target_id = None
        self.target_detail_var.set('開始監控後顯示人物')
        for button in self.target_action_buttons:
            button.configure(state='disabled')
        self.rebuild_target_options()

    def update_source_controls(self) -> None:
        """讓畫面明確表示目前是自動找最新檔，還是手動指定檔案。"""
        auto_latest = bool(self.auto_latest_var.get())
        state = tk.DISABLED if auto_latest else tk.NORMAL
        if self.file_entry is not None:
            self.file_entry.configure(state=state)
        if self.file_button is not None:
            self.file_button.configure(state=state)
        replay_dir = self.dir_var.get().strip()
        selected_file = self.file_var.get().strip()
        if auto_latest:
            try:
                replay_ready = bool(replay_dir) and Path(replay_dir).is_dir()
            except OSError:
                replay_ready = False
            self.replay_source_state_var.set('已找到資料夾' if replay_ready else '找不到資料夾')
        else:
            try:
                replay_ready = bool(selected_file) and Path(selected_file).is_file()
            except OSError:
                replay_ready = False
            self.replay_source_state_var.set('已選擇檔案' if replay_ready else '尚未選擇檔案')
        ro_dir = self.ro_dir_var.get().strip()
        try:
            ro_ready, ro_message = twro_install_message(Path(ro_dir)) if ro_dir else (False, '尚未設定')
        except OSError:
            ro_ready = False
            ro_message = '路徑無法讀取'
        self.ro_source_state_var.set(ro_message)
        self_id = self.target_tracker.self_id
        self_id_text = f'{self.target_display_name(self_id)}  ✓ 已自動確認' if self.target_tracker.packet_confirmed_self_id is not None and self_id is not None else f'暫時沿用 {self.target_display_name(self_id)}，等待過傳點確認' if self_id is not None else f'已輸入「{self.self_target_name_var.get().strip()}」，等待 RRF 配對' if self.self_target_name_var.get().strip() else '等待自動辨識'
        self.self_id_state_var.set(self_id_text)

    def toggle_technical_columns(self, persist: bool=True) -> None:
        """切換主表的進階欄位，預設只保留一般使用者需要的資訊。"""
        tree = getattr(self, 'tree', None)
        if tree is None:
            return
        columns = tree['columns']
        if isinstance(columns, str):
            columns = tuple(columns.split())
        basic_columns = ('name', 'target', 'scope', 'state', 'remaining')
        tree.configure(displaycolumns=columns if self.show_technical_columns_var.get() else basic_columns)
        if persist:
            self.save_settings()

    def choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.dir_var.get() or str(DEFAULT_REPLAY_DIR))
        if selected:
            self.dir_var.set(selected)
            self.file_var.set('')
            self.auto_latest_var.set(True)
            self.apply_settings()

    def choose_ro_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.ro_dir_var.get() or str(DEFAULT_RO_DIR))
        if selected:
            self.ro_dir_var.set(selected)
            self.ro_install_dir = Path(selected)
            self.client_data_signatures = []
            self.update_source_controls()
            self.save_settings()
            twro_ready, twro_message = twro_install_message(self.ro_install_dir)
            self.set_message('台版 RO 路徑已更新；資料校對請按下方按鈕手動執行' if twro_ready else f'RO 路徑未套用：{twro_message}')

    def update_unknown_review_controls(self) -> None:
        """在主執行緒更新未知資料數量與手動比對按鈕。"""
        lock = getattr(self, 'data_load_lock', None)
        if lock is None:
            return
        with lock:
            status_count = len(self.pending_unknown_status_ids)
            item_count = len(self.pending_client_item_ids)
            data_thread = self.__dict__.get('data_load_thread')
            unknown_thread = self.__dict__.get('unknown_resolve_thread')
            catalog_busy = bool(data_thread is not None and data_thread.is_alive() or (unknown_thread is not None and unknown_thread.is_alive()))
        total_count = status_count + item_count
        if total_count:
            button_text = f'比對 {total_count} 筆未知資料'
            summary = f'{total_count} 筆（狀態 {status_count}｜物品 {item_count}）'
        else:
            button_text = '比對 0 筆未知資料'
            summary = '0 筆'
        button_var = getattr(self, 'unknown_review_button_var', None)
        if button_var is not None:
            button_var.set(button_text)
        summary_var = getattr(self, 'unknown_data_summary_var', None)
        if summary_var is not None:
            summary_var.set(summary)
        button = getattr(self, 'unknown_review_button', None)
        if button is not None:
            try:
                button.configure(textvariable=button_var, state=tk.NORMAL if total_count and (not catalog_busy) else tk.DISABLED)
            except tk.TclError:
                pass

    def queue_unknown_review_controls(self) -> None:
        """合併短時間內的未知數量更新，避免 UI 佇列堆積。"""
        with self.data_load_lock:
            if getattr(self, 'unknown_controls_update_queued', False):
                return
            self.unknown_controls_update_queued = True
        self.enqueue_ui_callback(self._flush_unknown_review_controls)

    def _flush_unknown_review_controls(self) -> None:
        with self.data_load_lock:
            self.unknown_controls_update_queued = False
        self.update_unknown_review_controls()

    def review_unknown_data(self) -> None:
        """只在使用者按下按鈕後，才完整比對目前累積的未知資料。"""
        if self.core_monitoring_only:
            self.set_message('核心模式不執行未知資料校對；只保留自身即時監控')
            return
        with self.data_load_lock:
            status_ids = set(self.pending_unknown_status_ids)
            item_ids = set(self.pending_client_item_ids)
        total_count = len(status_ids) + len(item_ids)
        if total_count == 0:
            self.update_unknown_review_controls()
            messagebox.showinfo('沒有待比對資料', '目前沒有累積的未知狀態或物品。', parent=self)
            return
        ro_dir = Path(self.ro_dir_var.get().strip() or DEFAULT_RO_DIR)
        twro_ready, twro_message = twro_install_message(ro_dir)
        if not twro_ready:
            messagebox.showwarning('無法使用這個 RO 資料夾', '只支援台版 RO，請選擇包含 RO1TW.ini 的資料夾。', parent=self)
            self.update_source_controls()
            return
        with self.data_load_lock:
            loading = self.data_load_thread is not None and self.data_load_thread.is_alive()
        if loading:
            messagebox.showinfo('校對進行中', 'RO 資料校對正在進行，請等待目前進度完成。', parent=self)
            return
        confirmed = messagebox.askyesno('開始比對未知資料？', f'本次最多比對 {min(total_count, UNKNOWN_MEMORY_BATCH_LIMIT)} 筆未知資料。期間監控會繼續，是否開始？', parent=self)
        if not confirmed:
            return
        self.ro_install_dir = ro_dir
        self.save_settings()
        self.__dict__.setdefault('close_unknown_processed_keys', set()).clear()
        self.set_status_reload_progress(0, '準備定向比對未知資料')
        if self.status_reload_button is not None:
            self.status_reload_button.configure(state=tk.DISABLED)
        if self.unknown_review_button is not None:
            self.unknown_review_button.configure(state=tk.DISABLED)
        if self.catalog_cancel_button is not None:
            self.catalog_cancel_button.configure(state=tk.NORMAL)
        self.schedule_unknown_resolution(force=True)

    def reload_client_status_data(self) -> None:
        """只在停止監控後手動完整比對 RO 資料。"""
        if self.monitoring_is_active():
            messagebox.showinfo('請先停止監控', '完整檢查會大量讀取 RO 資料。請先按「停止監控」，再執行完整檢查。', parent=self)
            return
        ro_dir = Path(self.ro_dir_var.get().strip() or DEFAULT_RO_DIR)
        twro_ready, twro_message = twro_install_message(ro_dir)
        if not twro_ready:
            messagebox.showwarning('無法使用這個 RO 資料夾', '只支援台版 RO，請選擇包含 RO1TW.ini 的資料夾。', parent=self)
            self.set_message(f'無法校對：{twro_message}')
            self.update_source_controls()
            return
        with self.data_load_lock:
            loading = self.data_load_thread is not None and self.data_load_thread.is_alive()
        if loading:
            messagebox.showinfo('校對進行中', 'RO 資料校對正在進行，請等待目前進度完成。\n即時 RRF 監控不會停止。', parent=self)
            return
        confirmed = messagebox.askyesno('開始完整檢查？', '將重新檢查台版 RO 資料。完成前可以取消，取消或失敗都會沿用原有資料。是否開始？', parent=self)
        if not confirmed:
            return
        self.ro_install_dir = ro_dir
        self.save_settings()
        self.start_client_data_load('完整檢查', force=False)

    def choose_file(self) -> None:
        selected = filedialog.askopenfilename(initialdir=self.dir_var.get() or str(DEFAULT_REPLAY_DIR), filetypes=[('Ragnarok Replay', '*.rrf'), ('所有檔案', '*.*')])
        if selected:
            self.file_var.set(selected)
            self.auto_latest_var.set(False)
            self.apply_settings()

    def choose_sound_file(self) -> None:
        selected = filedialog.askopenfilename(filetypes=[('WAV 音效', '*.wav'), ('所有檔案', '*.*')])
        if selected:
            self.sound_file_var.set(selected)
            self.sound_mode_var.set('自訂 WAV')
            self.save_settings()

    def test_alert_sound(self) -> None:
        """立即測試目前選擇的提示音，不需要等待狀態倒數。"""
        self.play_alert_sound('yellow', status_name(FOCUS_STATUS_ID, 2435), 30)

    def on_volume_changed(self, value: str) -> None:
        self.volume_text_var.set(f'{float(value):.0f}%')
        self.schedule_settings_save()

    def toggle_overlay(self) -> None:
        if self.overlay_enabled_var.get() and self.monitoring_is_active():
            self.show_overlay()
        else:
            self.hide_overlay()
        if self.overlay_enabled_var.get() and (not self.monitoring_is_active()):
            self.set_message('待機中：右側狀態卡片會在開始監控後顯示')
        self.save_settings()

    def on_overlay_opacity_changed(self, value: str) -> None:
        try:
            self.overlay_opacity = max(OVERLAY_MIN_OPACITY, min(OVERLAY_MAX_OPACITY, float(value)))
        except (TypeError, ValueError):
            return
        if self.overlay is not None and self.overlay.winfo_exists():
            try:
                self.overlay.attributes('-alpha', self.overlay_opacity)
            except tk.TclError:
                pass
        self.schedule_settings_save()

    def toggle_auto_latest(self) -> None:
        with self.config_lock:
            self.auto_latest = bool(self.auto_latest_var.get())
        self.update_source_controls()
        self.save_settings()
        self.rescan()

    def show_overlay(self) -> None:
        created = False
        if self.overlay is None or not self.overlay.winfo_exists():
            created = True
            overlay = tk.Toplevel(self)
            overlay.title('RO 狀態卡片')
            overlay.overrideredirect(True)
            overlay.attributes('-topmost', True)
            overlay.configure(bg=OVERLAY_TRANSPARENT)
            try:
                overlay.attributes('-alpha', self.overlay_opacity)
            except tk.TclError:
                pass
            if os.name == 'nt':
                try:
                    overlay.wm_attributes('-transparentcolor', OVERLAY_TRANSPARENT)
                except tk.TclError:
                    overlay.configure(bg=OVERLAY_BG)
            overlay.protocol('WM_DELETE_WINDOW', self.close_overlay)
            overlay.bind('<Configure>', self.on_overlay_configure)
            self.overlay = overlay
            self.overlay_canvas = tk.Canvas(overlay, bg=OVERLAY_TRANSPARENT, highlightthickness=0, borderwidth=0, cursor='arrow')
            self.overlay_canvas.pack(fill='both', expand=True)
            self.overlay_canvas.bind('<ButtonPress-1>', self.start_overlay_drag)
            self.overlay_canvas.bind('<B1-Motion>', self.move_overlay_drag)
            self.overlay_canvas.bind('<ButtonRelease-1>', self.stop_overlay_drag)
            self.overlay_canvas.bind('<Motion>', self.update_overlay_cursor)
            self.overlay_canvas.bind('<Leave>', lambda _event: self.overlay_canvas.configure(cursor='arrow'))
            self.overlay_canvas.bind('<MouseWheel>', self.on_overlay_mousewheel)
            self.overlay_canvas.bind('<Button-4>', self.on_overlay_mousewheel)
            self.overlay_canvas.bind('<Button-5>', self.on_overlay_mousewheel)
            self.position_overlay_at_default(force=False)
            self.render_overlay_canvas()
        try:
            if created or self.overlay.state() == 'withdrawn':
                self.overlay.deiconify()
            if created:
                self.overlay.attributes('-topmost', True)
                self.overlay.attributes('-alpha', self.overlay_opacity)
                self.render_overlay_canvas()
        except tk.TclError:
            pass

    def hide_overlay(self) -> None:
        if self.overlay is not None and self.overlay.winfo_exists():
            self.overlay.withdraw()

    def close_overlay(self) -> None:
        self.overlay_enabled_var.set(False)
        self.hide_overlay()
        self.save_settings()

    def start_overlay_drag(self, event: tk.Event) -> None:
        overlay = self.overlay
        if overlay is None or not overlay.winfo_exists():
            return
        for action, bounds in self.overlay_control_regions.items():
            left, top, right, bottom = bounds
            if left <= event.x <= right and top <= event.y <= bottom:
                if action == 'close':
                    self.close_overlay()
                elif action == 'lock':
                    self.overlay_locked = True
                    self.overlay_locked_var.set(True)
                    self.render_overlay_canvas()
                    self.save_settings()
                return
        if self.overlay_locked:
            return
        self.overlay_resize_mode = self.overlay_resize_mode_at(event.x, event.y)
        if self.overlay_resize_mode:
            self.overlay_resize_origin = (event.x_root, event.y_root)
            self.overlay_resize_geometry = (overlay.winfo_x(), overlay.winfo_y(), overlay.winfo_width(), overlay.winfo_height())
            self.overlay_pending_size = (overlay.winfo_width(), overlay.winfo_height())
            self.overlay_drag_position = (overlay.winfo_x(), overlay.winfo_y())
            return
        self.overlay_drag_offset = (event.x_root - overlay.winfo_x(), event.y_root - overlay.winfo_y())
        self.overlay_drag_position = (overlay.winfo_x(), overlay.winfo_y())

    def move_overlay_drag(self, event: tk.Event) -> None:
        if self.overlay is None or not self.overlay.winfo_exists() or self.overlay_locked:
            return
        if self.overlay_resize_mode and self.overlay_resize_origin and self.overlay_resize_geometry:
            origin_x, origin_y = self.overlay_resize_origin
            start_x, start_y, start_width, start_height = self.overlay_resize_geometry
            delta_x = event.x_root - origin_x
            delta_y = event.y_root - origin_y
            x, y, width, height = (start_x, start_y, start_width, start_height)
            mode = self.overlay_resize_mode
            if 'e' in mode:
                width = max(OVERLAY_MIN_WIDTH, start_width + delta_x)
            if 's' in mode:
                height = max(OVERLAY_MIN_HEIGHT, start_height + delta_y)
            if 'w' in mode:
                width = max(OVERLAY_MIN_WIDTH, start_width - delta_x)
                x = start_x + start_width - width
            if 'n' in mode:
                height = max(OVERLAY_MIN_HEIGHT, start_height - delta_y)
                y = start_y + start_height - height
            work_left, work_top, work_right, work_bottom = self.overlay_work_area()
            width = min(width, max(OVERLAY_MIN_WIDTH, work_right - work_left))
            height = min(height, max(OVERLAY_MIN_HEIGHT, work_bottom - work_top))
            x = max(work_left, min(x, work_right - width))
            y = max(work_top, min(y, work_bottom - height))
            self.overlay_drag_position = (x, y)
            self.overlay_pending_size = (width, height)
            if self.overlay_auto_height:
                self.overlay_auto_height = False
                self.overlay_auto_height_var.set(False)
        elif self.overlay_drag_offset is not None:
            offset_x, offset_y = self.overlay_drag_offset
            x = event.x_root - offset_x
            y = event.y_root - offset_y
            work_left, work_top, work_right, work_bottom = self.overlay_work_area()
            x = max(work_left, min(x, work_right - self.overlay.winfo_width()))
            y = max(work_top, min(y, work_bottom - self.overlay.winfo_height()))
            self.overlay_drag_position = (x, y)
        else:
            return
        if self.overlay_drag_job is not None:
            return
        try:
            self.overlay_drag_job = self.after(OVERLAY_DRAG_INTERVAL_MS, self.apply_overlay_drag)
        except (tk.TclError, RuntimeError):
            self.overlay_drag_job = None

    def apply_overlay_drag(self) -> None:
        """合併拖曳事件，只把最新位置送給視窗管理員。"""
        self.overlay_drag_job = None
        if self.overlay is None or not self.overlay.winfo_exists() or self.overlay_drag_position is None:
            return
        x, y = self.overlay_drag_position
        if self.overlay_pending_size is not None:
            width, height = self.overlay_pending_size
            self.overlay.geometry(f'{width}x{height}+{x}+{y}')
        else:
            self.overlay.geometry(f'+{x}+{y}')

    def stop_overlay_drag(self, _event: tk.Event | None=None) -> None:
        """放開滑鼠時套用最後座標並停止拖曳排程。"""
        if self.overlay_drag_job is not None:
            try:
                self.after_cancel(self.overlay_drag_job)
            except tk.TclError:
                pass
            self.overlay_drag_job = None
        if self.overlay_drag_offset is not None or self.overlay_resize_mode:
            self.apply_overlay_drag()
        if self.overlay is not None and self.overlay.winfo_exists():
            try:
                self.overlay.update_idletasks()
            except tk.TclError:
                pass
            self.overlay_x = self.overlay.winfo_x()
            self.overlay_y = self.overlay.winfo_y()
            self.overlay_anchor_y = self.overlay_y
            self.overlay_width = self.overlay.winfo_width()
            self.overlay_height = self.overlay.winfo_height()
        self.overlay_drag_offset = None
        self.overlay_drag_position = None
        self.overlay_pending_size = None
        self.overlay_resize_mode = ''
        self.overlay_resize_origin = None
        self.overlay_resize_geometry = None
        try:
            self.after_idle(self.refresh_overlay_layout_after_drag)
        except (tk.TclError, RuntimeError):
            pass

    def refresh_overlay_layout_after_drag(self) -> None:
        """拖曳完成後補做一次必要的彈窗版面更新。"""
        if self.overlay_canvas is None:
            return
        try:
            if self.overlay_auto_height:
                self.adjust_overlay_height_to_content()
            self.render_overlay_canvas()
            self.schedule_overlay_geometry_save()
        except tk.TclError:
            pass

    def on_overlay_configure(self, event: tk.Event) -> None:
        """視窗大小改變時只排程一次 Canvas 重繪。"""
        if self.overlay is None or event.widget is not self.overlay:
            return
        width = self.safe_dimension(event.width, self.overlay_width, OVERLAY_MIN_WIDTH, 1600)
        height = self.safe_dimension(event.height, self.overlay_height, OVERLAY_MIN_HEIGHT, 1200)
        if width == self.overlay_width and height == self.overlay_height:
            return
        self.overlay_width = width
        self.overlay_height = height
        self.overlay_scroll_max = max(0, self.overlay_natural_height - self.overlay_height)
        self.overlay_scroll_offset = min(self.overlay_scroll_offset, self.overlay_scroll_max)
        if self.overlay_render_job is not None:
            try:
                self.after_cancel(self.overlay_render_job)
            except tk.TclError:
                pass
        try:
            self.overlay_render_job = self.after(OVERLAY_RESIZE_RENDER_INTERVAL_MS, self.render_overlay_canvas)
        except tk.TclError:
            self.overlay_render_job = None

    def overlay_work_area(self) -> tuple[int, int, int, int]:
        """回傳主螢幕可用區域；Windows 會排除工作列。"""
        if os.name == 'nt':
            try:
                rect = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
                    return (rect.left, rect.top, rect.right, rect.bottom)
            except (AttributeError, OSError):
                pass
        width = self.winfo_screenwidth()
        height = self.winfo_screenheight()
        return (0, 0, width, height)

    def position_overlay_at_default(self, force: bool=True) -> None:
        """首次靠右置於工作區約 22% 高度；有效舊位置則保留。"""
        if self.overlay is None or not self.overlay.winfo_exists():
            return
        left, top, right, bottom = self.overlay_work_area()
        work_width = max(OVERLAY_MIN_WIDTH, right - left)
        work_height = max(OVERLAY_MIN_HEIGHT, bottom - top)
        self.overlay_width = min(self.overlay_width, work_width)
        if force or self.overlay_x is None:
            self.overlay_x = right - self.overlay_width - OVERLAY_RIGHT_MARGIN
        self.overlay_x = max(left, min(self.overlay_x, right - self.overlay_width))
        if force or self.overlay_anchor_y is None:
            self.overlay_anchor_y = top + int(work_height * OVERLAY_TOP_FRACTION)
        self.overlay_anchor_y = max(top + OVERLAY_SAFE_TOP_MARGIN, min(self.overlay_anchor_y, bottom - OVERLAY_SAFE_BOTTOM_MARGIN - OVERLAY_MIN_HEIGHT))
        self.overlay_y = self.overlay_anchor_y
        if self.overlay_auto_height:
            self.adjust_overlay_height_to_content()
            return
        self.overlay_height = min(self.overlay_height, work_height)
        self.overlay_y = max(top, min(self.overlay_y, bottom - self.overlay_height))
        self.overlay.geometry(f'{self.overlay_width}x{self.overlay_height}+{self.overlay_x}+{self.overlay_y}')

    def adjust_overlay_height_to_content(self) -> None:
        """先向下增高，碰到底後再向上擴展；上下皆滿才捲動。"""
        if not self.overlay_auto_height or self.overlay is None or (not self.overlay.winfo_exists()):
            return
        left, top, right, bottom = self.overlay_work_area()
        desired_height = max(OVERLAY_MIN_HEIGHT, int(self.overlay_natural_height))
        anchor_y = self.overlay_anchor_y
        if anchor_y is None:
            anchor_y = top + int((bottom - top) * OVERLAY_TOP_FRACTION)
            self.overlay_anchor_y = anchor_y
        y, height, scroll_max = self.calculate_overlay_auto_geometry(anchor_y, desired_height, top, bottom)
        width = min(self.overlay_width, max(OVERLAY_MIN_WIDTH, right - left))
        x = self.overlay_x
        if x is None:
            x = right - width - OVERLAY_RIGHT_MARGIN
        x = max(left, min(x, right - width))
        self.overlay_width = width
        self.overlay_height = height
        self.overlay_x = x
        self.overlay_y = y
        self.overlay_scroll_max = scroll_max
        self.overlay_scroll_offset = min(self.overlay_scroll_offset, self.overlay_scroll_max)
        self.overlay.geometry(f'{width}x{height}+{x}+{y}')

    @staticmethod
    def calculate_overlay_auto_geometry(anchor_y: int, natural_height: int, work_top: int, work_bottom: int) -> tuple[int, int, int]:
        """純計算版高度規則，供 GUI 與回歸測試共用。"""
        return calculate_auto_height_geometry(anchor_y, natural_height, work_top, work_bottom, minimum_height=OVERLAY_MIN_HEIGHT, top_margin=OVERLAY_SAFE_TOP_MARGIN, bottom_margin=OVERLAY_SAFE_BOTTOM_MARGIN)

    def overlay_resize_mode_at(self, x: int, y: int) -> str:
        if self.overlay is None or self.overlay_locked:
            return ''
        width = max(1, self.overlay.winfo_width())
        height = max(1, self.overlay.winfo_height())
        west = x <= OVERLAY_RESIZE_BORDER
        east = x >= width - OVERLAY_RESIZE_BORDER
        north = y <= OVERLAY_RESIZE_BORDER
        south = y >= height - OVERLAY_RESIZE_BORDER
        return ('n' if north else 's' if south else '') + ('w' if west else 'e' if east else '')

    def update_overlay_cursor(self, event: tk.Event) -> None:
        if self.overlay_canvas is None:
            return
        mode = self.overlay_resize_mode_at(event.x, event.y)
        cursor = {'n': 'sb_v_double_arrow', 's': 'sb_v_double_arrow', 'e': 'sb_h_double_arrow', 'w': 'sb_h_double_arrow', 'ne': 'size_ne_sw', 'sw': 'size_ne_sw', 'nw': 'size_nw_se', 'se': 'size_nw_se'}.get(mode, 'arrow' if self.overlay_locked else 'fleur')
        try:
            self.overlay_canvas.configure(cursor=cursor)
        except tk.TclError:
            self.overlay_canvas.configure(cursor='arrow')

    def on_overlay_mousewheel(self, event: tk.Event) -> str:
        if self.overlay_scroll_max <= 0:
            return 'break'
        if getattr(event, 'num', None) == 4:
            delta = -48
        elif getattr(event, 'num', None) == 5:
            delta = 48
        else:
            raw_delta = int(getattr(event, 'delta', 0) or 0)
            delta = -48 if raw_delta > 0 else 48
        self.overlay_scroll_offset = max(0, min(self.overlay_scroll_max, self.overlay_scroll_offset + delta))
        self.render_overlay_canvas()
        return 'break'

    def schedule_overlay_geometry_save(self) -> None:
        if self.overlay_save_job is not None:
            try:
                self.after_cancel(self.overlay_save_job)
            except tk.TclError:
                pass
        try:
            self.overlay_save_job = self.after(600, self.save_overlay_geometry)
        except tk.TclError:
            self.overlay_save_job = None

    def save_overlay_geometry(self) -> None:
        self.overlay_save_job = None
        if self.running:
            self.save_settings()

    @staticmethod
    def light_level(remaining_ms: int, yellow_ms: int, red_ms: int) -> str:
        return expiration_level(remaining_ms, yellow_ms, red_ms)

    @staticmethod
    def light_colors(level: str) -> tuple[str, str, str]:
        if level == 'red':
            return ('#FF5A5A', '#FFE0E0', '#760000')
        if level == 'yellow':
            return ('#FFD84D', '#FFF4B8', '#5C4700')
        if level == 'debuff':
            return ('#B983FF', '#F0E3FF', '#54247A')
        if level == 'neutral':
            return ('#7FA9C0', '#E9F0F5', '#3E5666')
        return ('#55C878', '#E8F8EC', '#164B26')

    @staticmethod
    def chinese_number(value: int) -> str:
        digits = '零一二三四五六七八九'
        value = max(0, int(value))
        if value < 10:
            return digits[value]
        if value < 20:
            return '十' if value == 10 else f'十{digits[value - 10]}'
        if value < 100:
            tens, ones = divmod(value, 10)
            return f'{digits[tens]}十' if ones == 0 else f'{digits[tens]}十{digits[ones]}'
        return str(value)

    def speak_text(self, text: str) -> None:
        """使用 Windows SAPI 中文語音在背景播報，不阻塞監控畫面。"""
        safe_text = text.replace("'", "''")
        volume = self.current_sound_volume()
        if volume <= 0:
            return
        script = '\n'.join(('$voice = New-Object -ComObject SAPI.SpVoice', '$voices = $voice.GetVoices()', 'for ($i = 0; $i -lt $voices.Count; $i++) {', '  $description = $voices.Item($i).GetDescription()', "  if ($description -match 'Chinese|Hanhan|Taiwan') {", '    $voice.Voice = $voices.Item($i)', '    break', '  }', '}', '$voice.Rate = 0', f'$voice.Volume = {volume}', f"$voice.Speak('{safe_text}')"))
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        try:
            process = subprocess.Popen(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            processes = self.__dict__.setdefault('_scoped_speech_processes', [])
            processes[:] = [existing for existing in processes if existing.poll() is None]
            processes.append(process)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def sanitize_spoken_text(text: object) -> str:
        """語音只保留可讀文字與數字；裝飾符號改成單一空白。"""
        pieces: list[str] = []
        previous_space = True
        for character in str(text or ''):
            if character.isalnum():
                pieces.append(character)
                previous_space = False
            elif not previous_space:
                pieces.append(' ')
                previous_space = True
        return ''.join(pieces).strip()

    def spoken_target_name(self, target_id: int) -> str:
        """語音只念可靠人物名稱；沒有名稱時不朗讀人物 ID。"""
        target_name = self.target_tracker.target_name(target_id) or ''
        return self.sanitize_spoken_text(target_name)

    def current_sound_volume(self) -> int:
        try:
            return max(0, min(100, int(float(self.sound_volume_var.get()))))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return 100

    def play_alert_sound(self, level: str, status_text: str, threshold_seconds: float, target_text: str='', event_age_seconds: int | None=None) -> None:
        if self.current_sound_volume() <= 0:
            return
        mode = self.sound_mode_var.get()
        if mode == '導航人聲':
            status_text = self.sanitize_spoken_text(status_text) or '狀態'
            if level == 'pet':
                self.speak_text(f"寵物{(status_text if status_text != '寵物' else '')}，飽食度剩下{round(threshold_seconds)}，請餵食")
                return
            target_text = self.sanitize_spoken_text(target_text)
            target_prefix = f'{target_text}，' if target_text else ''
            if level == 'apply_batch':
                self.speak_text(status_text)
                return
            if level == 'apply':
                apply_text = '剛剛獲得' if event_age_seconds is None else self.format_apply_event_age(event_age_seconds)
                self.speak_text(f'{target_prefix}{status_text}，{apply_text}')
                return
            prefix = '注意，' if level == 'red' else ''
            seconds_text = self.chinese_number(round(threshold_seconds))
            self.speak_text(f'{prefix}{target_prefix}{status_text}，剩餘{seconds_text}秒')
            return
        if winsound is None:
            return
        sound_file = Path(self.sound_file_var.get().strip())
        try:
            if mode == '自訂 WAV' and sound_file.exists():
                winsound.PlaySound(str(sound_file), winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            if level == 'apply':
                sound = winsound.MB_ICONEXCLAMATION
            elif level in {'red', 'pet'}:
                sound = winsound.MB_ICONHAND if mode == '較明顯警告音' else winsound.MB_ICONEXCLAMATION
            else:
                sound = winsound.MB_ICONEXCLAMATION if mode == '較明顯警告音' else winsound.MB_ICONASTERISK
            winsound.MessageBeep(sound)
        except (OSError, RuntimeError):
            pass

    @serialized_scoped_monitor
    def process_apply_events(self, apply_events: list[tuple[int, int, int]], sync_ms: int=0) -> None:
        """顯示新套用事件，並將同一時間的大量提示合併，避免聲音洪水。"""
        if not apply_events:
            return
        now = time.monotonic()
        cutoff = now - max(600.0, APPLY_ALERT_COOLDOWN_SECONDS * 4)
        if len(self.recent_apply_alerts) > 512:
            self.recent_apply_alerts = {key: stamp for key, stamp in self.recent_apply_alerts.items() if stamp >= cutoff}
        selected_events: list[tuple[str, str, int]] = []
        seen_in_batch: set[tuple[int, int]] = set()
        current_timeline_ms = self.tracker.last_timeline_ms + max(0, int(sync_ms))
        for status_id, target_id, event_timeline_ms in apply_events:
            event_key = (status_id, target_id)
            if event_key in seen_in_batch:
                continue
            seen_in_batch.add(event_key)
            if not self.target_is_selected(target_id):
                continue
            if not is_readable_status_name(status_id):
                continue
            source_header = self.status_option_source_headers.get(status_id, 2435)
            status_text = status_name(status_id, source_header)
            target_text = f'{self.target_tracker.relation_for(target_id)} {self.target_display_name(target_id)}'
            spoken_target_text = self.spoken_target_name(target_id)
            event_age_seconds = self.estimate_apply_event_age_seconds(current_timeline_ms, event_timeline_ms)
            stamp = datetime.now().strftime('%H:%M:%S')
            self.recent_event_history.appendleft(f'{stamp} {target_text}｜{status_text}（{self.format_apply_event_age(event_age_seconds)}）')
            last_alert_at = self.recent_apply_alerts.get(event_key)
            if last_alert_at is not None and now - last_alert_at < APPLY_ALERT_COOLDOWN_SECONDS:
                continue
            self.recent_apply_alerts[event_key] = now
            if self.alert_rule_for_target(status_id, 'apply', target_id):
                selected_events.append((status_text, spoken_target_text, event_age_seconds))
        if self.recent_event_history:
            history_count = len(self.recent_event_history)
            self.latest_event_var.set(f'{self.recent_event_history[0]}（最近 {history_count} 件）')
        if not self.apply_sound_var.get() or not selected_events:
            return
        if now - self.last_apply_alert_at < APPLY_ALERT_GLOBAL_COOLDOWN_SECONDS:
            return
        self.last_apply_alert_at = now
        if len(selected_events) == 1:
            status_text, target_text, event_age_seconds = selected_events[0]
            self.play_alert_sound('apply', status_text, 0, target_text, event_age_seconds=event_age_seconds)
        else:
            self.play_alert_sound('apply_batch', f'新套用{self.chinese_number(len(selected_events))}個已勾選狀態，請查看最新事件', 0)

    @staticmethod
    def estimate_apply_event_age_seconds(current_timeline_ms: int, event_timeline_ms: int) -> int:
        """以校正後的目前時間軸估算狀態大約幾秒前生效。"""
        elapsed_ms = max(0, int(current_timeline_ms) - int(event_timeline_ms))
        return max(0, int(round(elapsed_ms / 1000)))

    @staticmethod
    def format_apply_event_age(event_age_seconds: int) -> str:
        """將事件年齡轉成自然的提示文字，避免出現「約零秒前」。"""
        if int(event_age_seconds) <= 0:
            return '剛剛獲得'
        return f'約{RrfMonitorApp.chinese_number(event_age_seconds)}秒前獲得'

    def monitoring_is_active(self) -> bool:
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            return monitor_session.active
        event = self.__dict__.get('monitoring_active')
        return bool(event is not None and event.is_set())

    def monitoring_should_continue(self) -> bool:
        return bool(self.running and self.monitoring_is_active() and (not self.monitor_reset_requested.is_set()))

    def update_monitor_control_ui(self) -> None:
        """同步開始／停止／重新掃描按鈕，不用使用者判斷目前狀態。"""
        active = self.monitoring_is_active()
        if self.start_monitor_button is not None:
            self.start_monitor_button.configure(state=tk.DISABLED if active else tk.NORMAL)
        if self.stop_monitor_button is not None:
            self.stop_monitor_button.configure(state=tk.NORMAL if active else tk.DISABLED)
        if self.rescan_button is not None:
            self.rescan_button.configure(state=tk.NORMAL if active else tk.DISABLED)
        with self.data_load_lock:
            data_thread = self.__dict__.get('data_load_thread')
            unknown_thread = self.__dict__.get('unknown_resolve_thread')
            catalog_busy = bool(data_thread is not None and data_thread.is_alive() or (unknown_thread is not None and unknown_thread.is_alive()))
        if self.status_reload_button is not None:
            self.status_reload_button.configure(state=tk.DISABLED if active or catalog_busy else tk.NORMAL)

    def clear_live_monitor_display(self) -> None:
        """清除容易被誤認為仍在監控的即時結果；使用者設定全部保留。"""
        if hasattr(self, 'tree'):
            self.update_main_table([])
        self.status_table_count_var.set('目前 0 筆')
        self.latest_event_var.set('尚無新套用事件')
        self.overlay_summary_var.set('監控已停止')
        self.hide_overlay()
        self.hide_pet_overlay()
        self.clear_target_options()
        self.pet_panel_render_key = None
        self.refresh_pet_panel()
        self.update_tab_labels()

    @serialized_scoped_monitor
    def reset_monitoring_engine(self) -> None:
        """由監控執行緒重建解析工作；不呼叫任何 Tkinter 元件。"""
        self._clear_scoped_transient_data()
        self.tracker.reset()
        self.reset_pet_tracking()
        self.target_tracker.reset()
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.reset()
        self.last_resolved_self_identity = None
        self.incremental_parser.reset()
        self.last_signature = None
        self.current_path = None
        self.last_rrf_data_monotonic = None
        self.last_rrf_packet_count = 0
        self.last_rrf_new_packet_count = 0
        self.latest_file_cache_path = None
        self.latest_file_scan_at = 0.0
        self.live_replay_discovery.reset()
        self.alerted.clear()
        self.recent_apply_alerts.clear()
        self.recent_event_history.clear()
        self.last_apply_alert_at = 0.0
        with self.data_load_lock:
            unknown_journal = self.__dict__.get('unknown_journal')
            saved_unknowns = unknown_journal.snapshot() if unknown_journal is not None else ()
            self.background_seen_status_ids = {record.value_id for record in saved_unknowns if record.kind == 'status'}
            self.background_seen_item_ids = {record.value_id for record in saved_unknowns if record.kind == 'item'}
            self.unknown_last_log_at = 0.0
        with self.sync_state_lock:
            self.live_sync_path = None
            self.live_sync_offset_ms = None

    @serialized_scoped_monitor
    def clear_inactive_replay_state(self) -> None:
        """活動錄影停止後清除舊畫面，但保留檔案觀察基線。"""
        self._clear_scoped_transient_data()
        if self.__dict__.get('current_path') is None:
            return
        self.tracker.reset()
        self.reset_pet_tracking()
        self.target_tracker.reset()
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.reset()
        self.incremental_parser.reset()
        self.current_path = None
        self.last_signature = None
        self.last_rrf_data_monotonic = None
        self.last_rrf_packet_count = 0
        self.last_rrf_new_packet_count = 0
        self.alerted.clear()
        self.recent_apply_alerts.clear()
        self.recent_event_history.clear()
        self.last_apply_alert_at = 0.0
        with self.sync_state_lock:
            self.live_sync_path = None
            self.live_sync_offset_ms = None

    def start_monitoring(self) -> None:
        """套用目前設定後才開始讀取 RRF。"""
        if not self.running or self.monitoring_is_active():
            return
        if not self.apply_settings():
            return
        self.monitoring_has_started = True
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.start_monitoring()
        else:
            self.monitor_reset_requested.set()
            self.monitoring_active.set()
        self.rescan()
        if self.overlay_enabled_var.get():
            self.show_overlay()
        self.update_monitor_control_ui()
        self.log_event('使用者開始 RRF 監控')

    @serialized_scoped_monitor
    def stop_monitoring(self) -> None:
        """停止 RRF 讀取、倒數與提示；保留所有選取與設定。"""
        if not self.monitoring_is_active():
            return
        self._clear_scoped_transient_data()
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.stop_monitoring()
        else:
            self.monitoring_active.clear()
            self.monitor_reset_requested.set()
            self.monitor_wake_event.set()
        self.tracker.reset()
        self.reset_pet_tracking()
        self.target_tracker.reset()
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.reset()
        self.last_resolved_self_identity = None
        self.alerted.clear()
        self.recent_apply_alerts.clear()
        self.recent_event_history.clear()
        self.clear_live_monitor_display()
        self.monitor_summary_var.set('監控已停止')
        self.monitor_control_var.set('監控已停止｜可先調整設定')
        self.set_message('監控已停止')
        self.update_monitor_control_ui()
        self.log_event('使用者停止 RRF 監控')

    def update_sync_entry_state(self) -> None:
        entry = getattr(self, 'sync_entry', None)
        if entry is not None:
            entry.configure(state=tk.DISABLED if self.auto_sync_var.get() else tk.NORMAL)

    def on_auto_sync_changed(self) -> None:
        self.update_sync_entry_state()
        self.save_settings()
        self.set_message('已開啟自動延遲估算' if self.auto_sync_var.get() else '已改用手動延遲秒數')

    def apply_time_correction(self) -> None:
        """延遲只影響顯示與提醒時間，不需要重新解析整份 RRF。"""
        self.save_settings()
        _sync_ms, sync_label = self.effective_sync_offset()
        self.set_message(f'時間校正已套用：{sync_label}')

    def apply_settings(self) -> bool:
        yellow_seconds = max(0, self.safe_float(self.yellow_var.get(), 30))
        red_seconds = max(0, self.safe_float(self.red_var.get(), 15))
        if yellow_seconds < red_seconds:
            self.set_message('設定未套用：黃燈門檻不可低於紅燈門檻')
            return False
        with self.config_lock:
            self.monitor_dir = self.dir_var.get().strip()
            self.monitor_file = self.file_var.get().strip()
            self.auto_latest = bool(self.auto_latest_var.get())
            self.monitor_interval = max(0.2, self.safe_float(self.interval_var.get(), 0.5))
            self.ro_install_dir = Path(self.ro_dir_var.get().strip() or DEFAULT_RO_DIR)
        self.update_source_controls()
        self.save_settings()
        if self.monitoring_is_active():
            self.rescan()
        else:
            self.monitor_summary_var.set('待機｜完成設定後按開始監控')
            self.monitor_control_var.set('待機｜設定已儲存')
            self.set_message('設定已儲存')
            self.update_monitor_control_ui()
        return True

    @serialized_scoped_monitor
    def rescan(self) -> None:
        """要求監控執行緒安全重建解析狀態；待機時不會讀取 RRF。"""
        self._clear_scoped_transient_data()
        self.tracker.reset()
        self.reset_pet_tracking()
        self.target_tracker.reset()
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.reset()
        self.last_resolved_self_identity = None
        self.alerted.clear()
        self.recent_apply_alerts.clear()
        self.recent_event_history.clear()
        self.last_apply_alert_at = 0.0
        self.latest_event_var.set('尚無新套用事件')
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.request_reset()
        else:
            self.monitor_reset_requested.set()
            self.monitor_wake_event.set()
        self.clear_target_options()
        if hasattr(self, 'tree'):
            self.update_main_table([])
        self.status_table_count_var.set('目前 0 筆')
        self.update_unknown_review_controls()
        self.onboarding_next_refresh_at = 0.0
        self.update_onboarding()
        self.update_tab_labels()
        if self.monitoring_is_active():
            self.monitor_summary_var.set('正在建立目前狀態')
            self.monitor_control_var.set('監控啟動中：正在讀取目前 RRF 並建立狀態…')
            self.set_message('正在建立目前狀態：等待讀取 RRF…')
        else:
            self.monitor_summary_var.set('待機｜完成設定後按開始監控')
            self.monitor_control_var.set('待機｜完成設定後按開始監控')
            self.set_message('待機｜完成設定後按開始監控')
        self.update_monitor_control_ui()

    def choose_current_file(self) -> Path | None:
        with self.config_lock:
            selected = self.monitor_file
            replay_dir_text = self.monitor_dir
            auto_latest = self.auto_latest
        if not auto_latest and selected:
            path = Path(selected)
            return path if path.exists() and path.suffix.lower() == '.rrf' else None
        if replay_dir_text != self.latest_file_cache_dir:
            self.latest_file_cache_dir = replay_dir_text
            self.latest_file_cache_path = None
            self.latest_file_scan_at = 0.0
            self.live_replay_discovery.reset()
        now = time.monotonic()
        if now < self.latest_file_scan_at:
            active_path = self.live_replay_discovery.current_path
            if active_path is None:
                return None
            try:
                return active_path if active_path.exists() else None
            except OSError:
                return None
        self.latest_file_scan_at = now + REPLAY_DISCOVERY_INTERVAL_SECONDS
        replay_dir = Path(replay_dir_text or DEFAULT_REPLAY_DIR)
        try:
            samples: list[ReplayFileSample] = []
            for item in replay_dir.glob('*.rrf'):
                try:
                    if not item.is_file():
                        continue
                    stat = item.stat()
                except OSError:
                    continue
                samples.append(ReplayFileSample(path=item, size=stat.st_size, mtime_ns=stat.st_mtime_ns))
            self.latest_file_cache_path = self.live_replay_discovery.observe(replay_dir, samples, now=now)
            return self.latest_file_cache_path
        except OSError:
            active_path = self.live_replay_discovery.current_path
            try:
                return active_path if active_path is not None and active_path.exists() else None
            except OSError:
                return None

    def update_live_sync_estimate(self, path: Path, replay_date: tuple[int, ...], latest_timeline_ms: int) -> None:
        """用 RRF 錄影起始時間與最新封包時間軸估算檔案寫入延遲。"""
        if not replay_date or latest_timeline_ms <= 0:
            return
        try:
            replay_start = datetime(*replay_date)
            wall_elapsed_ms = int((datetime.now() - replay_start).total_seconds() * 1000)
        except (TypeError, ValueError):
            return
        lag_ms = wall_elapsed_ms - latest_timeline_ms
        if lag_ms < 0 or lag_ms > 120000:
            with self.sync_state_lock:
                if self.live_sync_path == path:
                    self.live_sync_offset_ms = None
            return
        with self.sync_state_lock:
            if self.live_sync_path != path or self.live_sync_offset_ms is None:
                self.live_sync_path = path
                self.live_sync_offset_ms = lag_ms
            else:
                self.live_sync_offset_ms = int(self.live_sync_offset_ms * 0.7 + lag_ms * 0.3)

    def effective_sync_offset(self) -> tuple[int, str]:
        manual_seconds = max(0, self.safe_float(self.sync_var.get(), 5))
        manual_ms = int(manual_seconds * 1000)
        if self.auto_sync_var.get():
            with self.sync_state_lock:
                live_ms = self.live_sync_offset_ms
            if live_ms is not None:
                return (live_ms, f'自動 {live_ms / 1000:.1f} 秒')
            return (manual_ms, f'自動等待（暫用 {manual_seconds:.1f} 秒）')
        return (manual_ms, f'手動 {manual_seconds:.1f} 秒')

    @staticmethod
    def observed_client_ids(packets: list[ReplayPacket]) -> tuple[set[int], set[int]]:
        """只找受監控人物實際收到的狀態 ID；物品不進即時路徑。"""
        status_ids: set[int] = set()
        for packet in packets:
            data = packet.data
            if packet.header in StatusTracker.STATUS_WITH_REMAINING and len(data) >= 6:
                status_ids.add(u16(data, 2))
        return (status_ids, set())

    def _clear_scoped_transient_data(self) -> None:
        """撤銷身分時清除無法逐目標拆分的待辦與提醒。"""
        self._scoped_generation = self.__dict__.get('_scoped_generation', 0) + 1
        self._scoped_observed_status_targets = {}
        for key in ('alerted', 'recent_apply_alerts', 'recent_event_history', 'pending_unknown_status_ids', 'pending_client_item_ids', 'background_seen_status_ids', 'background_seen_item_ids', 'close_unknown_processed_keys'):
            value = self.__dict__.get(key)
            if value is not None:
                value.clear()
        self.last_apply_alert_at = 0.0
        journal = self.__dict__.get('unknown_journal')
        if journal is not None:
            journal.resolve((record.key for record in journal.snapshot()))
            try:
                journal.flush()
            except OSError:
                pass
        for process in self.__dict__.pop('_scoped_speech_processes', []):
            try:
                if process.poll() is None:
                    process.terminate()
            except OSError:
                pass
        self.enqueue_ui_callback(self._clear_scoped_event_ui, critical=True)

    def _clear_scoped_event_ui(self) -> None:
        latest = self.__dict__.get('latest_event_var')
        if latest is not None:
            latest.set('尚無新套用事件')

    def _process_scoped_packet_batch(self, packets: list[ReplayPacket], reset_required: bool) -> None:
        """依封包先後授權；入隊前的狀態永遠不補收。"""
        self.sync_tracker_target_filter_snapshot()
        synced_identity = self.target_tracker.scoped_identity_signature()
        status_ids: set[int] = set()
        pending_status_packets: list[ReplayPacket] = []

        def flush_status_packets() -> None:
            if pending_status_packets:
                self.tracker.consume(pending_status_packets, emit_apply_events=not reset_required and (not self.incremental_parser.suppress_apply_events))
                pending_status_packets.clear()
        for packet in packets:
            generation = self.__dict__.get('_scoped_generation', 0)
            previous_self = self.target_tracker.packet_confirmed_self_id
            self.target_tracker.consume([packet])
            current_identity = self.target_tracker.scoped_identity_signature()
            if current_identity != synced_identity:
                flush_status_packets()
                self.sync_tracker_target_filter_snapshot()
                synced_identity = current_identity
            if generation != self.__dict__.get('_scoped_generation', 0):
                status_ids.clear()
            current_self = self.target_tracker.packet_confirmed_self_id
            if current_self is not None and current_self != previous_self:
                self.enqueue_ui_callback(lambda target_id=current_self: self.apply_packet_confirmed_self_id(target_id), critical=True)
            trusted = self.target_tracker.trusted_target_ids()
            if self.pet_monitor_enabled and self.target_tracker.self_id is not None:
                if packet.header in PetTracker.RELEVANT_HEADERS:
                    self.consume_pet_packets([packet], reset_required)
            if packet.header in StatusTracker.STATUS_WITH_REMAINING or packet.header in (StatusTracker.STATUS_END, ACTOR_STATE_HEADER):
                pending_status_packets.append(packet)
                if packet.index < self.tracker.processed_packets or len(pending_status_packets) >= PROCESS_PACKET_BATCH_SIZE:
                    flush_status_packets()
                minimum = {1087: 13, 2435: 17}.get(packet.header)
                if minimum is not None and len(packet.data) >= minimum:
                    target_id = u32(packet.data, 4)
                    if target_id in trusted and self.target_is_monitored_from_snapshot(target_id):
                        sid = u16(packet.data, 2)
                        observed = self.__dict__.setdefault('_scoped_observed_status_targets', {})
                        if sid in observed or len(observed) < MAX_UNKNOWN_IDS:
                            observed.setdefault(sid, set()).add(target_id)
                        status_ids.add(sid)
        flush_status_packets()
        resolved_id = self.target_tracker.self_id
        resolved_name = self.target_tracker.target_name(resolved_id) if resolved_id is not None else None
        identity = (resolved_id, resolved_name) if resolved_name else None
        if identity and identity != self.last_resolved_self_identity:
            self.last_resolved_self_identity = identity
            self.enqueue_ui_callback(lambda target_id=resolved_id, target_name=resolved_name: self.apply_resolved_self_name(target_id, target_name), critical=True)
        if packets:
            self.tracker.advance_timeline(packets[-1].timeline_ms)
        if not reset_required and (not self.core_monitoring_only):
            self.maybe_schedule_client_data_sync(observed=(status_ids, set()))

    @serialized_scoped_monitor
    def process_packet_batch(self, packet_batch: list[ReplayPacket], reset_required: bool) -> None:
        """一次分流封包，避免人物、寵物、狀態追蹤器重複掃描整批資料。"""
        if not self.monitoring_should_continue():
            return
        if reset_required:
            self._clear_scoped_transient_data()
            self.tracker.reset()
            self.reset_pet_tracking()
            self.target_tracker.reset()
            self.last_resolved_self_identity = None
            monitor_session = self.__dict__.get('monitor_session')
            if monitor_session is not None:
                monitor_session.reset()
            self.alerted.clear()
            self.recent_apply_alerts.clear()
            self.last_apply_alert_at = 0.0
            session = self.incremental_parser.session
            replay_identity = session.identity_snapshot
            self.target_tracker.set_replay_identity(replay_identity.aid, replay_identity.name)
            if self.core_monitoring_only:
                resolved_self_id = self.target_tracker.self_id
                self.tracker.set_allowed_target_ids({resolved_self_id} if resolved_self_id is not None else set())
            if replay_identity.aid is not None:
                self.enqueue_ui_callback(lambda target_id=replay_identity.aid, target_name=replay_identity.name: self.apply_replay_snapshot_identity(target_id, target_name), critical=True)
            self.sync_tracker_target_filter_snapshot()
            self.seed_replay_pet_snapshot(getattr(session, 'pet_snapshot', None))
        self._process_scoped_packet_batch(packet_batch, reset_required)
        return

    @staticmethod
    def update_bounded_id_set(target: set[int], values: Iterable[int], limit: int=MAX_UNKNOWN_IDS, *, minimum_id: int=1) -> None:
        """更新 ID 集合並限制容量，避免異常錄影讓未知清單無限增長。"""
        target.update((int(value) for value in values if int(value) >= minimum_id))
        excess = len(target) - max(1, int(limit))
        if excess > 0:
            for value in sorted(target, reverse=True)[:excess]:
                target.discard(value)

    @serialized_scoped_monitor
    def maybe_schedule_client_data_sync(self, packets: list[ReplayPacket] | None=None, observed: tuple[set[int], set[int]] | None=None) -> None:
        if self.core_monitoring_only:
            return
        if observed is None:
            candidates = packets or []
            trusted = self.target_tracker.trusted_target_ids()
            candidates = [packet for packet in candidates if packet.header in StatusTracker.STATUS_WITH_REMAINING and len(packet.data) >= {1087: 13, 2435: 17}[packet.header] and (u32(packet.data, 4) in trusted) and self.target_is_monitored_from_snapshot(u32(packet.data, 4))]
            observed = self.observed_client_ids(candidates)
        status_ids, item_ids = observed
        trusted = self.target_tracker.trusted_target_ids()
        allowed = self.__dict__.get('_scoped_observed_status_targets', {})
        status_ids = {sid for sid in status_ids if allowed.get(sid, set()) & trusted}
        item_ids = set()
        unknown_status_ids = {status_id for status_id in status_ids if not is_readable_status_name(status_id)}
        unknown_item_ids = {item_id for item_id in item_ids if item_id not in ITEM_NAMES_BY_ID}
        new_status_ids = unknown_status_ids - self.background_seen_status_ids
        new_item_ids = unknown_item_ids - self.background_seen_item_ids
        if not new_status_ids and (not new_item_ids):
            return
        now = time.monotonic()
        self.update_bounded_id_set(self.background_seen_status_ids, new_status_ids, minimum_id=0)
        self.update_bounded_id_set(self.background_seen_item_ids, new_item_ids)
        with self.data_load_lock:
            self.update_bounded_id_set(self.pending_unknown_status_ids, new_status_ids, minimum_id=0)
            self.update_bounded_id_set(self.pending_client_item_ids, new_item_ids)
            queued_count = len(self.pending_unknown_status_ids) + len(self.pending_client_item_ids)
        unknown_journal = self.__dict__.get('unknown_journal')
        journal_changed = False
        if unknown_journal is not None:
            for status_id in new_status_ids:
                journal_changed = unknown_journal.record('status', status_id) or journal_changed
            for item_id in new_item_ids:
                journal_changed = unknown_journal.record('item', item_id) or journal_changed
        if journal_changed and unknown_journal is not None:
            try:
                unknown_journal.flush()
            except OSError as exc:
                self.log_event(f'未知資料待辦暫時無法保存：{exc}')
        details: list[str] = []
        if new_status_ids:
            details.append(f'未知狀態 {len(new_status_ids)} 項')
        if new_item_ids:
            details.append(f'未知物品 {len(new_item_ids)} 項')
        if now - self.unknown_last_log_at >= 60.0:
            self.unknown_last_log_at = now
            self.log_event('RRF 發現監控目標的新 ID，加入定向辨識：' + '、'.join(details) + f'（目前累積 {queued_count} 項）')
        self.queue_unknown_review_controls()
        self.schedule_unknown_resolution()

    @serialized_scoped_monitor
    def schedule_unknown_resolution(self, force: bool=False) -> None:
        """每個 RO 版本只定向查一次小批未知資料，不做完整 GRF 掃描。"""
        if not force and (self.__dict__.get('core_monitoring_only', False) or not self.__dict__.get('auto_resolve_unknown', True)):
            return
        with self.data_load_lock:
            data_thread = self.__dict__.get('data_load_thread')
            if data_thread is not None and data_thread.is_alive():
                return
            unknown_thread = self.__dict__.get('unknown_resolve_thread')
            if unknown_thread is not None:
                return
            client_signature = json_sha256({'ro_dir': str(Path(self.__dict__.get('ro_install_dir', DEFAULT_RO_DIR))), 'status': self.__dict__.get('status_data_signatures', []), 'client': self.__dict__.get('client_data_signatures', [])})
            unknown_journal = self.__dict__.get('unknown_journal')
            processed_keys = self.__dict__.setdefault('close_unknown_processed_keys', set())
            if force and unknown_journal is not None:
                candidates = [record for record in unknown_journal.snapshot() if record.kind in {'status', 'item'} and record.key not in processed_keys]
                candidates.sort(key=lambda record: (record.attempt_count, -record.seen_count, record.first_seen_at, record.kind, record.value_id))
                batch = tuple(candidates[:UNKNOWN_MEMORY_BATCH_LIMIT])
                processed_keys.update((record.key for record in batch))
                status_ids = {record.value_id for record in batch if record.kind == 'status'}
                item_ids = {record.value_id for record in batch if record.kind == 'item'}
            elif unknown_journal is not None:
                batch = unknown_journal.next_batch(client_signature=client_signature, limit=UNKNOWN_MEMORY_BATCH_LIMIT)
                status_ids = {record.value_id for record in batch if record.kind == 'status'}
                item_ids = {record.value_id for record in batch if record.kind == 'item'}
            else:
                batch = ()
                status_candidates = [value for value in sorted(self.pending_unknown_status_ids) if ('status', value) not in processed_keys]
                status_ids = set(status_candidates[:UNKNOWN_MEMORY_BATCH_LIMIT])
                remaining = UNKNOWN_MEMORY_BATCH_LIMIT - len(status_ids)
                item_candidates = [value for value in sorted(self.pending_client_item_ids) if ('item', value) not in processed_keys]
                item_ids = set(item_candidates[:remaining])
                if force:
                    processed_keys.update([('status', value) for value in status_ids] + [('item', value) for value in item_ids])
            if not status_ids and (not item_ids):
                if force and self.__dict__.get('close_after_data_load', False):
                    self._finalize_close()
                return
            ro_dir = Path(self.__dict__.get('ro_install_dir', DEFAULT_RO_DIR))
            ready, _message = twro_install_message(ro_dir)
            if not ready:
                return
            build_status_index_if_missing = False
            if unknown_journal is not None:
                unknown_journal.mark_attempted([record.key for record in batch], client_signature=client_signature)
                try:
                    unknown_journal.flush()
                except OSError as exc:
                    self.log_event(f'未知資料待辦暫時無法保存：{exc}')
            cancel_requested = self.__dict__.get('catalog_cancel_requested')
            if cancel_requested is not None:
                cancel_requested.clear()
            self.unknown_resolve_thread = threading.Thread(target=self._unknown_resolution_worker, args=(ro_dir, status_ids, item_ids, build_status_index_if_missing, self.__dict__.get('_scoped_generation', 0)), daemon=True)
            self.unknown_resolve_thread.start()

    def _unknown_resolution_worker(self, ro_dir: Path, status_ids: set[int], item_ids: set[int], build_status_index_if_missing: bool=False, scoped_generation: int | None=None) -> None:
        """定向查詢未知 ID；快取缺少時只在本次執行建立一次狀態索引。"""
        status_result: StatusDataResult | None = None
        client_result: ClientCatalogResult | None = None
        error_text = ''
        try:
            with self.client_data_io_lock:
                status_result, client_result = self._run_catalog_worker_process({'mode': 'unknown', '_scoped_generation': scoped_generation, 'ro_dir': str(ro_dir), 'status_cache_path': str(self.status_data_cache_path), 'client_cache_path': str(self.client_data_cache_path), 'status_ids': sorted(status_ids), 'item_ids': sorted(item_ids), 'build_status_index_if_missing': bool(build_status_index_if_missing)})
        except CatalogWorkerCancelled:
            error_text = '__cancelled__'
        except Exception as exc:
            error_text = f'{type(exc).__name__}: {exc}'
        self.enqueue_ui_callback(lambda: self._finish_unknown_resolution(status_ids, item_ids, status_result, client_result, error_text, build_status_index_if_missing, scoped_generation), critical=True)

    @serialized_scoped_monitor
    def _finish_unknown_resolution(self, status_ids: set[int], item_ids: set[int], status_result: StatusDataResult | None, client_result: ClientCatalogResult | None, error_text: str, build_status_index_if_missing: bool=False, scoped_generation: int | None=None) -> None:
        if scoped_generation != self.__dict__.get('_scoped_generation', 0):
            with self.data_load_lock:
                self.unknown_resolve_thread = None
            self.schedule_unknown_resolution(force=self.__dict__.get('close_after_data_load', False))
            return
        if not self.running and (not self.close_after_data_load):
            with self.data_load_lock:
                self.unknown_resolve_thread = None
            return
        if error_text == '__cancelled__':
            with self.data_load_lock:
                self.unknown_resolve_thread = None
            cancel_requested = self.__dict__.get('catalog_cancel_requested')
            if cancel_requested is not None:
                cancel_requested.clear()
            self.close_after_data_load = False
            catalog_cancel_button = self.__dict__.get('catalog_cancel_button')
            if catalog_cancel_button is not None:
                catalog_cancel_button.configure(state=tk.DISABLED)
            self.update_unknown_review_controls()
            self.set_status_reload_progress(0, '已取消；未知資料仍保留')
            self._hide_close_progress_dialog()
            return
        if status_result is not None:
            merge_status_data_delta(status_result)
        if client_result is not None:
            merge_client_catalog_delta(client_result)
        resolved_status_ids = {status_id for status_id in status_ids if is_readable_status_name(status_id)}
        resolved_item_ids = {item_id for item_id in item_ids if item_id in ITEM_NAMES_BY_ID}
        promoted_statuses, promoted_items = promote_verified_unknowns_to_bundled_data(resolved_status_ids, resolved_item_ids, status_result, client_result)
        with self.data_load_lock:
            self.pending_unknown_status_ids.difference_update(resolved_status_ids)
            self.pending_client_item_ids.difference_update(resolved_item_ids)
            self.unknown_resolve_thread = None
        unknown_journal = self.__dict__.get('unknown_journal')
        if (resolved_status_ids or resolved_item_ids) and unknown_journal is not None:
            unknown_journal.resolve([('status', value) for value in resolved_status_ids] + [('item', value) for value in resolved_item_ids])
            try:
                unknown_journal.flush()
            except OSError as exc:
                self.log_event(f'未知資料待辦暫時無法保存：{exc}')
        if resolved_status_ids or resolved_item_ids:
            self.status_option_generation += 1
            self.status_option_signature = None
            self.status_data_report = f'已定向辨識：狀態 {len(resolved_status_ids)}、物品 {len(resolved_item_ids)}'
            self.status_data_report_var.set(self.status_data_report)
            data_summary_var = self.__dict__.get('data_status_summary_var')
            if data_summary_var is not None:
                data_summary_var.set('已辨識新資料')
            if client_result is not None:
                self.client_data_report = client_result.report
                self.client_data_report_var.set(self.client_data_report)
            promotion_text = ''
            self.log_event(self.status_data_report + promotion_text)
            selected_tab = self.notebook.select() if hasattr(self, 'notebook') else ''
            if resolved_status_ids and self.status_library_tab is not None and (selected_tab == str(self.status_library_tab)):
                self.refresh_status_options(self.tracker.snapshot())
            if resolved_status_ids:
                self.set_message('監控中：已辨識新狀態，已加入可選清單（尚未勾選，提示音關閉）' if self.monitoring_is_active() else '待機中：已辨識新狀態，已加入可選清單（尚未勾選，提示音關閉）')
        elif error_text:
            self.log_event(f'未知資料定向辨識未完成：{error_text}')
            if build_status_index_if_missing:
                retry_allowed = self.unknown_status_index_attempt_count < UNKNOWN_STATUS_INDEX_MAX_ATTEMPTS
                self.unknown_status_index_attempted = not retry_allowed
                if retry_allowed and self.unknown_status_retry_job is None:
                    after_callback = self.__dict__.get('after')
                    if after_callback is None and self.__dict__.get('tk') is not None:
                        after_callback = self.after
                    if after_callback is not None:
                        try:
                            self.unknown_status_retry_job = after_callback(UNKNOWN_STATUS_INDEX_RETRY_DELAY_MS, self.retry_unknown_status_index)
                            self.set_message('新狀態辨識暫未完成；五分鐘後自動重試一次')
                        except (tk.TclError, RuntimeError):
                            self.unknown_status_retry_job = None
        status_reload_button = self.__dict__.get('status_reload_button')
        if status_reload_button is not None:
            status_reload_button.configure(state=tk.NORMAL)
        catalog_cancel_button = self.__dict__.get('catalog_cancel_button')
        if catalog_cancel_button is not None:
            catalog_cancel_button.configure(state=tk.DISABLED)
        self.update_unknown_review_controls()
        if self.__dict__.get('close_after_data_load', False):
            self.schedule_unknown_resolution(force=True)

    def retry_unknown_status_index(self) -> None:
        """首次索引遇到暫時錯誤時只自動重試一次，避免週期性掃描 GRF。"""
        self.unknown_status_retry_job = None
        self.schedule_unknown_resolution()

    @serialized_scoped_monitor
    def _publish_monitor_read(self, path: Path, signature: tuple[int, int], replay_date: tuple[int, ...], packet_count: int, new_packet_count: int) -> bool:
        """解析在鎖外進行，完成後只交付仍屬目前監控的健康資訊。"""
        if not self.monitoring_should_continue():
            return False
        self.update_live_sync_estimate(path, replay_date, self.tracker.last_timeline_ms)
        self.current_path = path
        self.last_signature = signature
        self.last_rrf_packet_count = packet_count
        self.last_rrf_new_packet_count = new_packet_count
        if new_packet_count > 0:
            self.last_rrf_data_monotonic = time.monotonic()
        self.last_monitor_error = ''
        self.set_message(f'監控中：{path.name}')
        return True

    def monitor_loop(self) -> None:
        idle_sleep = self.monitor_interval
        while self.running:
            if not self.monitoring_is_active():
                self.monitor_wake_event.wait(0.5)
                self.monitor_wake_event.clear()
                continue
            monitor_session = self.__dict__.get('monitor_session')
            reset_requested = monitor_session.consume_reset_request() if monitor_session is not None else self.monitor_reset_requested.is_set()
            if reset_requested:
                self.reset_monitoring_engine()
                if monitor_session is None:
                    self.monitor_reset_requested.clear()
                idle_sleep = self.monitor_interval
            if not self.monitoring_should_continue():
                continue
            with self.config_lock:
                poll_interval = self.monitor_interval
            idle_cap = max(MONITOR_IDLE_INTERVAL_MAX_SECONDS, poll_interval)
            path = self.choose_current_file()
            if path is None:
                if self.__dict__.get('current_path') is not None:
                    self.clear_inactive_replay_state()
                    self.enqueue_ui_callback(lambda: self.latest_event_var.set('尚無新套用事件'), critical=True)
                self.set_message('未偵測到正在錄影；請先開啟 RO 錄影')
                idle_sleep = min(idle_cap, max(poll_interval, idle_sleep * 1.25))
                self.monitor_wake_event.wait(idle_sleep)
                self.monitor_wake_event.clear()
                continue
            changed = False
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if path != self.current_path or signature != self.last_signature:
                    previous_packet_count = self.incremental_parser.packet_count
                    replay_date, reset_required, packet_count = self.incremental_parser.parse_incremental_batches(path, self.process_packet_batch, should_continue=self.monitoring_should_continue)
                    new_packet_count = packet_count if reset_required else max(0, packet_count - previous_packet_count)
                    if not self._publish_monitor_read(path, signature, replay_date, packet_count, new_packet_count):
                        continue
                    changed = True
            except (OSError, ValueError, struct.error):
                self.set_message(f'讀取中：{path.name}（檔案可能正在寫入）')
            except Exception as exc:
                error_text = f'{type(exc).__name__}: {exc}'
                self.set_message(f'監控讀取錯誤：{path.name}（{error_text}）')
                if error_text != self.last_monitor_error:
                    self.last_monitor_error = error_text
                    self.log_event(f'監控讀取錯誤：{path.name}｜{error_text}')
            if changed:
                idle_sleep = poll_interval
            else:
                self.invalidate_stale_pet_data()
                idle_sleep = min(idle_cap, max(poll_interval, idle_sleep * 1.25))
            self.monitor_wake_event.wait(idle_sleep)
            self.monitor_wake_event.clear()

    def refresh_ui(self) -> None:
        """執行一次畫面更新；即使單次 UI 更新失敗也要繼續排程。"""
        if not self.running:
            return
        try:
            self._refresh_ui_once()
            self.last_ui_refresh_error = ''
        except Exception as exc:
            error_text = f'{type(exc).__name__}: {exc}'
            if error_text != self.last_ui_refresh_error:
                self.last_ui_refresh_error = error_text
                self.log_event(f'畫面更新例外：{error_text}')
            if self.monitoring_is_active():
                self.set_message(f'監控中：畫面更新例外，監控仍持續執行（{error_text}）')
            else:
                self.set_message(f'待機中：畫面更新例外（{error_text}）')
        finally:
            if self.running:
                try:
                    self.after(UI_REFRESH_INTERVAL_MS, self.refresh_ui)
                except (tk.TclError, RuntimeError):
                    pass

    def current_readable_status_ids(self) -> frozenset[int]:
        """依資料世代快取可顯示狀態，避免每秒重掃整個狀態字典。"""
        generation = self.status_option_generation
        if self.readable_status_generation != generation:
            self.readable_status_ids_cache = frozenset((status_id for status_id in EFST_NAMES if is_readable_status_name(status_id)))
            self.readable_status_generation = generation
        return self.readable_status_ids_cache

    @serialized_scoped_monitor
    def build_monitor_snapshot(self, states: list[tuple[StatusState, int | None]]):
        """把解析事實轉成主表與卡片共用的不可變快照。"""
        self.sync_tracker_target_filter_snapshot()
        trusted = self.target_tracker.trusted_target_ids()
        states = [(state, remaining) for state, remaining in states if state.target_id in trusted]
        observations = [StatusObservation(target_id=state.target_id, status_id=state.status_id, active=state.active, remaining_ms=remaining, total_ms=state.total_ms, event_timeline_ms=state.event_timeline_ms, source_header=state.source_header) for state, remaining in states]
        resolver = self.current_policy_resolver()
        self.monitor_session.set_resolver(resolver)
        self.monitor_session.replace_observations(observations, identity_confirmed=lambda target_id: resolver.resolve(target_id=target_id, relation=self.target_tracker.relation_for(target_id), target_name=self.target_tracker.target_name(target_id) or '').mode != MODE_OFF)
        snapshot = self.monitor_session.snapshot(relation_for=self.target_tracker.relation_for, name_for=self.target_tracker.target_name, internal_status_ids=frozenset(), readable_status_ids=self.current_readable_status_ids())
        self.monitor_snapshot = snapshot
        return snapshot

    @serialized_scoped_monitor
    def _refresh_ui_once(self) -> None:
        if not self.running:
            return
        if not self.monitoring_is_active():
            message = self.get_message()
            if 'RO 校對' in message or message.startswith('正在校對 RO'):
                self.monitor_summary_var.set('待機｜正在檢查 RO 資料')
            elif message.startswith('監控已停止'):
                self.monitor_summary_var.set('監控已停止')
            else:
                self.monitor_summary_var.set('待機｜完成設定後按開始監控')
            self.status_label.configure(text=message)
            self.status_table_count_var.set('目前 0 筆')
            return
        sync_ms, sync_label = self.effective_sync_offset()
        message = self.get_message()
        if message.startswith('監控中：'):
            summary = '監控中'
        elif message.startswith('正在校對 RO'):
            summary = '正在校對 RO'
        elif message.startswith('RO 校對失敗'):
            summary = 'RO 校對失敗'
        elif message.startswith('RO 校對未完成'):
            summary = 'RO 路徑待確認'
        elif message.startswith('RO 校對完成'):
            summary = '等待 RRF'
        elif message.startswith(('找不到 RRF', '未偵測到正在錄影')):
            summary = '等待 RRF'
        elif message.startswith('讀取中：'):
            summary = '正在讀取 RRF'
        elif message.startswith('正在建立目前狀態'):
            summary = '正在建立目前狀態'
        elif message.startswith('等待') or message.startswith('尚未'):
            summary = '等待開始'
        else:
            summary = '監控訊息'
        control_text = f'監控中｜{self.current_path.name}' if self.current_path is not None else '監控中｜等待新的錄影'
        if self.monitor_control_var.get() != control_text:
            self.monitor_control_var.set(control_text)
        if self.core_monitoring_only and self.target_tracker.self_id is None and (not message.startswith(('找不到 RRF', '未偵測到正在錄影', '讀取中：'))):
            summary = '等待自己人物'
        self.monitor_summary_var.set(summary)
        self.status_label.configure(text=self.monitor_health_text(message, sync_label))
        if self.core_monitoring_only:
            resolved_self_id = self.target_tracker.self_id
            self.tracker.set_allowed_target_ids({resolved_self_id} if resolved_self_id is not None else set())
        states = self.tracker.snapshot()
        monitor_snapshot = self.build_monitor_snapshot(states)
        if self.core_monitoring_only and self.target_tracker.self_id is not None and (not states) and message.startswith('監控中：'):
            summary = '監控中（等待狀態）'
            self.monitor_summary_var.set(summary)
        selected_tab = self.notebook.select() if hasattr(self, 'notebook') else ''
        if self.status_library_tab is not None and selected_tab == str(self.status_library_tab):
            self.refresh_status_options(states)
        if self.target_selection_tab is not None and selected_tab == str(self.target_selection_tab):
            self.refresh_target_options(states)
        if self.pet_tab is not None and selected_tab == str(self.pet_tab):
            self.refresh_pet_panel()
        self.update_onboarding()
        self.update_tab_labels()
        yellow_seconds = max(0, self.safe_float(self.yellow_var.get(), 30))
        red_seconds = max(0, self.safe_float(self.red_var.get(), 15))
        yellow_seconds = max(yellow_seconds, red_seconds)
        yellow_ms = int(yellow_seconds * 1000)
        red_ms = int(red_seconds * 1000)
        apply_events = self.tracker.pop_apply_events()
        self.process_apply_events(apply_events, sync_ms)
        self.process_pet_alerts()
        self.process_expiration_alerts(states, sync_ms=sync_ms, yellow_ms=yellow_ms, red_ms=red_ms, yellow_seconds=yellow_seconds, red_seconds=red_seconds)
        display_keys = monitor_snapshot.keys
        display_states = [item for item in states if item[0].key in display_keys]
        display_states.sort(key=lambda item: (TARGET_SCOPE_ORDER.index(self.target_tracker.relation_for(item[0].target_id)), item[0].target_id, status_display_priority(item[0].status_id, item[0].source_header), status_name(item[0].status_id, item[0].source_header).casefold(), item[0].status_id))
        overlay_states: list[tuple[StatusState, int | None, str]] = []
        table_rows: list[tuple[StatusState, str, str, str, str, int | None]] = []
        for state, remaining in display_states:
            adjusted_remaining: int | None = None
            if state.active:
                if remaining is None:
                    level = status_visual_level(group=status_group(state.status_id, state.source_header), category=status_library_category(state.status_id, state.source_header), remaining_ms=max(yellow_ms, red_ms) + 1, yellow_ms=yellow_ms, red_ms=red_ms)
                    state_text = '生效中'
                    remaining_text = '生效中'
                    self.clear_expiration_alerts(state.key)
                    overlay_states.append((state, None, level))
                    total_text = self.format_duration(state.total_ms) if state.total_ms else '—'
                    table_rows.append((state, state_text, remaining_text, total_text, level, None))
                    continue
                adjusted_remaining = max(0, remaining - sync_ms)
                level = status_visual_level(group=status_group(state.status_id, state.source_header), category=status_library_category(state.status_id, state.source_header), remaining_ms=adjusted_remaining, yellow_ms=yellow_ms, red_ms=red_ms)
                state_text = '啟用' if adjusted_remaining > 0 else '已到期'
                remaining_text = self.format_duration(adjusted_remaining)
                if adjusted_remaining <= 0:
                    self.clear_expiration_alerts(state.key)
                if adjusted_remaining > 0:
                    overlay_states.append((state, adjusted_remaining, level))
            else:
                state_text = '結束'
                remaining_text = '—'
                self.clear_expiration_alerts(state.key)
                level = 'normal'
            total_text = self.format_duration(state.total_ms) if state.total_ms else '—'
            table_rows.append((state, state_text, remaining_text, total_text, level, adjusted_remaining if state.active else None))
        if self.status_sort_var.get() == '狀態優先':
            table_rows.sort(key=lambda row: (status_name(row[0].status_id, row[0].source_header), TARGET_SCOPE_ORDER.index(self.target_tracker.relation_for(row[0].target_id)), row[0].target_id))
        elif self.status_sort_var.get() == '剩餘時間':
            table_rows.sort(key=lambda row: (row[5] if row[5] is not None else 10 ** 12, TARGET_SCOPE_ORDER.index(self.target_tracker.relation_for(row[0].target_id)), row[0].target_id))
        view = self.status_view_var.get()
        visible_rows = []
        for row in table_rows:
            remaining_value = row[5]
            if view == '啟用中' and row[1] not in {'啟用', '生效中'}:
                continue
            if view == '即將到期' and (remaining_value is None or remaining_value <= 0 or remaining_value > yellow_ms):
                continue
            if view == '已到期／結束' and row[0].active and (remaining_value is None or remaining_value > 0):
                continue
            visible_rows.append(row)
        self.update_main_table(visible_rows)
        self.status_table_count_var.set(f'顯示 {len(visible_rows)} 筆／符合目標 {len(table_rows)} 筆')
        self.refresh_overlay(overlay_states, yellow_ms, red_ms)
        self.refresh_pet_overlay()

    def monitor_health_text(self, message: str, sync_label: str) -> str:
        """把開發統計轉成玩家可立即採取行動的 RRF 健康狀態。"""
        if not message.startswith('監控中：') or self.current_path is None:
            return f'{message}｜倒數校正 {sync_label}'
        now = time.monotonic()
        if self.last_rrf_data_monotonic is None:
            health = f'● 等待新資料｜{self.current_path.name}'
        else:
            elapsed = max(0, int(now - self.last_rrf_data_monotonic))
            if elapsed <= 4:
                health = f'● 更新正常｜{self.current_path.name}｜剛剛收到資料'
            elif elapsed <= 10:
                health = f'● 等待新資料｜{self.current_path.name}｜最近更新於 {elapsed} 秒前'
            else:
                health = f'● 已 {elapsed} 秒沒有新資料｜請確認 RO 是否仍在錄影'
        health += f'｜延遲 {sync_label}'
        if self.show_technical_columns_var.get():
            health += f'｜封包 {self.last_rrf_packet_count}｜本次新增 {self.last_rrf_new_packet_count}'
        return health

    def update_main_table(self, rows: list[tuple[StatusState, str, str, str, str, int | None]]) -> None:
        """以狀態／目標 key 更新既有列，避免每次倒數刷新都整表重建。"""
        desired_keys = [row[0].key for row in rows]
        desired_set = set(desired_keys)
        order_changed = desired_keys != self.tree_order_keys
        for key, item_id in list(self.tree_row_ids.items()):
            if key not in desired_set or not self.tree.exists(item_id):
                if self.tree.exists(item_id):
                    self.tree.delete(item_id)
                del self.tree_row_ids[key]
                self.tree_row_values.pop(key, None)
        for index, (state, state_text, remaining_text, total_text, level, _remaining_value) in enumerate(rows):
            values = (f'0x{state.status_id:04X}', status_name(state.status_id, state.source_header), self.target_display_name(state.target_id), self.target_tracker.relation_for(state.target_id), state_text if level == 'normal' else f'{state_text}｜{level_label(level)}', remaining_text, total_text, f'0x{state.source_header:04X}')
            item_id = self.tree_row_ids.get(state.key)
            if item_id is None or not self.tree.exists(item_id):
                item_id = self.tree.insert('', 'end', values=values, tags=(level,))
                self.tree_row_ids[state.key] = item_id
                self.tree_row_values[state.key] = values
            else:
                if self.tree_row_values.get(state.key) != values:
                    self.tree.item(item_id, values=values)
                    self.tree_row_values[state.key] = values
                if self.tree.item(item_id, 'tags') != (level,):
                    self.tree.item(item_id, tags=(level,))
            if order_changed:
                self.tree.move(item_id, '', index)
        self.tree_order_keys = desired_keys

    def overlay_light_colors(self, level: str) -> tuple[str, str, str]:
        """白底深字為主，提醒使用淡底與深色文字。"""
        if level == 'red':
            return ('#C64242', '#FCE9E8', '#8B2929')
        if level == 'yellow':
            return ('#B58618', '#FFF5D6', '#705114')
        if level == 'debuff':
            return ('#9362B0', '#F1E8FA', '#59336F')
        if level == 'neutral':
            return ('#708392', OVERLAY_BG, OVERLAY_MUTED_TEXT)
        return ('#3A8C57', OVERLAY_BG, OVERLAY_TEXT)

    @staticmethod
    def draw_rounded_rectangle(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **options: object) -> int:
        radius = max(0.0, min(float(radius), (x2 - x1) / 2, (y2 - y1) / 2))
        points = (x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius, x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2, x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1)
        return canvas.create_polygon(points, smooth=True, splinesteps=8, **options)

    @staticmethod
    def shorten_overlay_text(text: str, max_characters: int) -> str:
        return shorten_text(text, max_characters)

    @staticmethod
    def shorten_overlay_text_pixels(text: str, font: tkfont.Font, max_pixels: int) -> str:
        """依實際字型寬度截短文字，讓倒數能緊接在名稱後方。"""
        value = str(text)
        limit = max(1, int(max_pixels))
        if font.measure(value) <= limit:
            return value
        ellipsis = '…'
        if font.measure(ellipsis) > limit:
            return ''
        low, high = (0, len(value))
        while low < high:
            middle = (low + high + 1) // 2
            if font.measure(value[:middle] + ellipsis) <= limit:
                low = middle
            else:
                high = middle - 1
        return value[:low] + ellipsis

    def overlay_card_metrics(self) -> tuple[int, int, int]:
        size = max(OVERLAY_MIN_FONT_SIZE, int(self.overlay_font_size))
        target_header_height = max(24, size + 15)
        status_row_height = max(22, size + 13)
        pet_line_height = max(20, size + 11)
        return (target_header_height, status_row_height, pet_line_height)

    def overlay_measure_fonts(self, canvas: tk.Canvas) -> tuple[tkfont.Font, tkfont.Font]:
        """重用量測字型，避免每秒倒數刷新反覆建立 Tcl 字型物件。"""
        cache = self.overlay_font_cache
        if cache is not None and cache[0] == self.overlay_font_size:
            return (cache[1], cache[2])
        regular = tkfont.Font(root=canvas, font=('Microsoft JhengHei', self.overlay_font_size))
        bold = tkfont.Font(root=canvas, font=('Microsoft JhengHei', self.overlay_font_size, 'bold'))
        self.overlay_font_cache = (self.overlay_font_size, regular, bold)
        return (regular, bold)

    def overlay_model_natural_height(self, groups: tuple[object, ...], pet: tuple[object, ...] | None) -> int:
        cards = self.overlay_card_models(groups, pet)
        layout = arrange_card_heights(self.overlay_width, (card[2] for card in cards))
        return max(OVERLAY_MIN_HEIGHT, layout.natural_height)

    def overlay_card_models(self, groups: tuple[object, ...], pet: tuple[object, ...] | None) -> tuple[tuple[str, object, int], ...]:
        target_header_height, status_row_height, pet_line_height = self.overlay_card_metrics()
        cards: list[tuple[str, object, int]] = []
        for _scope, people in groups:
            for target_id, target_name, rows in people:
                cards.append(('target', (target_id, target_name, rows), target_header_height + len(rows) * status_row_height))
        return tuple(cards)

    @staticmethod
    def overlay_structure_for(width: int, height: int, scroll_offset: int, locked: bool, groups: tuple[object, ...], pet: tuple[object, ...] | None) -> tuple[object, ...]:
        """只描述會改變卡片幾何的資料；倒數與燈色不觸發重建。"""
        grouped_keys = tuple(((scope, tuple(((target_id, tuple((row[0] for row in rows))) for target_id, _target_name, rows in people))) for scope, people in groups))
        return (width, height, scroll_offset, locked, grouped_keys, pet is not None)

    @staticmethod
    def overlay_pet_lines(pet: tuple[object, ...]) -> tuple[str, ...]:
        pet_name, level_text, satiety_text, intimacy_text, state_text = pet
        return (f'寵物｜{pet_name}', f'{level_text}\u3000{satiety_text}', str(intimacy_text), str(state_text))

    def update_overlay_dynamic_items(self, width: int) -> None:
        """原地更新倒數、名稱與燈色，避免每秒刪除並重畫整張透明視窗。"""
        canvas = self.overlay_canvas
        if canvas is None:
            return
        row_font_measure, row_bold_font_measure = self.overlay_measure_fonts(canvas)
        for _scope, people in self.overlay_render_groups:
            for target_id, target_name, rows in people:
                text_limit = self.overlay_target_text_limits.get(int(target_id), max(20, width - 32))
                target_item = self.overlay_target_items.get(int(target_id))
                if target_item is not None:
                    canvas.itemconfigure(target_item, text=self.shorten_overlay_text_pixels(str(target_name), row_bold_font_measure, text_limit), fill=OVERLAY_TITLE_TEXT)
                for state_key, name, remaining_text, level in rows:
                    state_key = tuple(state_key)
                    items = self.overlay_row_items.get(state_key)
                    if items is None:
                        continue
                    background_item, dot_item, name_item, time_item = items
                    background_box = canvas.bbox(background_item)
                    name_coords = canvas.coords(name_item)
                    time_coords = canvas.coords(time_item)
                    name_x = name_coords[0] if name_coords else 22
                    time_width = row_bold_font_measure.measure(str(remaining_text))
                    row_text_limit = self.overlay_row_text_limits.get(state_key, max(20, width - 118))
                    if background_box is not None and time_coords:
                        time_x = time_coords[0]
                        row_text_limit = max(20, int(time_x - name_x - time_width - 8))
                        self.overlay_row_text_limits[state_key] = row_text_limit
                    dot_color, row_bg, foreground = self.overlay_light_colors(str(level))
                    canvas.itemconfigure(background_item, fill=row_bg)
                    canvas.itemconfigure(dot_item, fill=dot_color)
                    display_name = self.shorten_overlay_text_pixels(str(name), row_font_measure, row_text_limit)
                    canvas.itemconfigure(name_item, text=display_name, fill=foreground)
                    canvas.itemconfigure(time_item, text=str(remaining_text), fill=foreground)
        pet = self.overlay_render_pet
        if pet is not None:
            pet_lines = self.overlay_pet_lines(pet)
            for index, line in enumerate(pet_lines):
                item_id = self.overlay_pet_items.get(index)
                if item_id is not None:
                    canvas.itemconfigure(item_id, text=self.shorten_overlay_text(line, self.overlay_pet_text_limit), fill=OVERLAY_TITLE_TEXT if index == 0 else OVERLAY_TEXT)

    def render_overlay_canvas(self) -> None:
        """以單一 Canvas 繪製卡片；內容更新不建立或銷毀子元件。"""
        self.overlay_render_job = None
        canvas = self.overlay_canvas
        overlay = self.overlay
        if canvas is None or overlay is None or (not overlay.winfo_exists()):
            return
        width = max(OVERLAY_MIN_WIDTH, canvas.winfo_width(), self.overlay_width)
        height = max(OVERLAY_MIN_HEIGHT, canvas.winfo_height(), self.overlay_height)
        signature = (width, height, self.overlay_scroll_offset, self.overlay_locked, self.overlay_summary_text, self.overlay_render_groups, self.overlay_render_pet)
        if signature == self.overlay_render_signature:
            return
        structure_signature = self.overlay_structure_for(width, height, self.overlay_scroll_offset, self.overlay_locked, self.overlay_render_groups, self.overlay_render_pet)
        if structure_signature == self.overlay_structure_signature:
            self.overlay_render_signature = signature
            self.update_overlay_dynamic_items(width)
            return
        self.overlay_render_signature = signature
        self.overlay_structure_signature = structure_signature
        chrome_geometry = (width, height)
        if self.__dict__.get('overlay_chrome_geometry') != chrome_geometry or not canvas.find_withtag('overlay_chrome'):
            canvas.delete('all')
            self.overlay_chrome_geometry = chrome_geometry
            self.draw_rounded_rectangle(canvas, 1, 1, width - 2, height - 2, 3, fill=OVERLAY_BG, outline=OVERLAY_BORDER, width=1, tags='overlay_chrome')
            canvas.create_rectangle(2, 2, width - 3, 31, fill=OVERLAY_SUMMARY_BG, outline='', tags=('overlay_chrome', 'overlay_header'))
            canvas.create_line(3, 3, width - 4, 3, fill='#F8FCFF', tags=('overlay_chrome', 'overlay_header'))
            canvas.create_line(3, 5, width - 4, 5, fill='#EDF5FB', tags=('overlay_chrome', 'overlay_header'))
            canvas.create_line(2, 31, width - 3, 31, fill=OVERLAY_BORDER, tags=('overlay_chrome', 'overlay_header'))
        else:
            canvas.delete('overlay_content')
        self.overlay_control_regions.clear()
        self.overlay_target_items.clear()
        self.overlay_row_items.clear()
        self.overlay_pet_items.clear()
        self.overlay_target_text_limits.clear()
        self.overlay_row_text_limits.clear()
        header_height = 32
        font_size = self.overlay_font_size
        target_header_height, status_row_height, pet_line_height = self.overlay_card_metrics()
        row_font_spec = ('Microsoft JhengHei', font_size)
        row_bold_font_spec = ('Microsoft JhengHei', font_size, 'bold')
        row_font_measure, row_bold_font_measure = self.overlay_measure_fonts(canvas)
        canvas.create_text(12, header_height / 2, text='RO狀態卡片', fill=OVERLAY_TITLE_TEXT, font=('Microsoft JhengHei', max(9, font_size)), anchor='w', tags=('overlay_content', 'overlay_header'))
        if not self.overlay_locked:
            lock_region = (width - 66, 4, width - 37, 28)
            close_region = (width - 34, 4, width - 5, 28)
            self.overlay_control_regions['lock'] = lock_region
            self.overlay_control_regions['close'] = close_region
            for region in (lock_region, close_region):
                self.draw_rounded_rectangle(canvas, *region, 2, fill='#F2F7FB', outline=OVERLAY_BORDER, tags=('overlay_content', 'overlay_header'))
            canvas.create_text((lock_region[0] + lock_region[2]) / 2, 16, text='鎖', fill=OVERLAY_TITLE_TEXT, font=('Microsoft JhengHei', 9, 'bold'), tags=('overlay_content', 'overlay_header'))
            canvas.create_text((close_region[0] + close_region[2]) / 2, 15, text='×', fill=OVERLAY_TITLE_TEXT, font=('Segoe UI', 13, 'bold'), tags=('overlay_content', 'overlay_header'))
        content_top = header_height
        content_bottom = height - 7
        offset = self.overlay_scroll_offset
        cards = self.overlay_card_models(self.overlay_render_groups, self.overlay_render_pet)
        layout = arrange_card_heights(width, (card[2] for card in cards))
        for placement, (card_kind, payload, _card_height) in zip(layout.placements, cards):
            card_left = placement.x
            card_right = placement.x + placement.width
            card_top = placement.y - offset
            card_bottom = card_top + placement.height
            if card_bottom < content_top or card_top > content_bottom:
                continue
            visible_top = max(card_top, content_top)
            visible_bottom = min(card_bottom, content_bottom)
            card_background = OVERLAY_CARD_BG if card_kind == 'target' else OVERLAY_PET_BG
            if visible_top == card_top and visible_bottom == card_bottom:
                canvas.create_rectangle(card_left, card_top, card_right, card_bottom, fill=card_background, outline='', tags='overlay_content')
            elif visible_bottom > visible_top:
                canvas.create_rectangle(card_left, visible_top, card_right, visible_bottom, fill=card_background, outline='', tags='overlay_content')
            if card_kind == 'target':
                _target_id, target_name, rows = payload
                text_limit = max(20, placement.width - 16)
                self.overlay_target_text_limits[int(_target_id)] = text_limit
                header_y = card_top + target_header_height // 2
                if content_top + 2 <= header_y <= content_bottom:
                    self.overlay_target_items[int(_target_id)] = canvas.create_text(card_left + 8, header_y, text=self.shorten_overlay_text_pixels(str(target_name), row_bold_font_measure, text_limit), fill=OVERLAY_TITLE_TEXT, font=row_bold_font_spec, anchor='w', tags='overlay_content')
                separator_y = card_top + target_header_height - 1
                if content_top <= separator_y <= content_bottom:
                    canvas.create_line(card_left + 6, separator_y, card_right - 6, separator_y, fill=OVERLAY_SEPARATOR, tags='overlay_content')
                row_y = card_top + target_header_height
                for _state_key, name, remaining_text, level in rows:
                    state_key = tuple(_state_key)
                    name_x = card_left + 22
                    time_x = card_right - 12
                    time_width = row_bold_font_measure.measure(str(remaining_text))
                    max_name_pixels = max(20, int(time_x - name_x - time_width - 8))
                    self.overlay_row_text_limits[state_key] = max_name_pixels
                    display_name = self.shorten_overlay_text_pixels(str(name), row_font_measure, max_name_pixels)
                    row_center = row_y + status_row_height // 2
                    if content_top + 2 <= row_center <= content_bottom - 2:
                        dot_color, row_bg, foreground = self.overlay_light_colors(str(level))
                        background_item = self.draw_rounded_rectangle(canvas, card_left + 4, row_y, card_right - 4, row_y + status_row_height - 2, 1, fill=row_bg, outline='', tags='overlay_content')
                        dot_item = canvas.create_oval(card_left + 10, row_center - 3, card_left + 16, row_center + 3, fill=dot_color, outline='', tags='overlay_content')
                        name_item = canvas.create_text(name_x, row_center, text=display_name, fill=foreground, font=row_font_spec, anchor='w', tags='overlay_content')
                        time_item = canvas.create_text(time_x, row_center, text=str(remaining_text), fill=foreground, font=row_bold_font_spec, anchor='e', tags='overlay_content')
                        self.overlay_row_items[state_key] = (background_item, dot_item, name_item, time_item)
                    row_y += status_row_height
            else:
                pet = payload
                pet_lines = self.overlay_pet_lines(pet)
                self.overlay_pet_text_limit = max(10, (placement.width - 16) // max(8, font_size + 2))
                for index, line in enumerate(pet_lines):
                    color = OVERLAY_TITLE_TEXT if index == 0 else OVERLAY_TEXT
                    font = ('Microsoft JhengHei', font_size, 'bold') if index == 0 else ('Microsoft JhengHei', font_size)
                    line_y = card_top + 13 + index * pet_line_height
                    if content_top + 2 <= line_y <= content_bottom - 2:
                        self.overlay_pet_items[index] = canvas.create_text(card_left + 8, line_y, text=self.shorten_overlay_text(line, self.overlay_pet_text_limit), fill=color, font=font, anchor='w', tags='overlay_content')
        if not cards:
            empty_y = header_height + 30
            canvas.create_text(14, empty_y, text='目前沒有可顯示的狀態', fill=OVERLAY_MUTED_TEXT, font=('Microsoft JhengHei', font_size), anchor='nw', width=max(1, width - 28), tags='overlay_content')
        if self.overlay_scroll_max > 0:
            track_top = header_height + 4
            track_bottom = height - 10
            track_height = max(20, track_bottom - track_top)
            thumb_height = max(24, int(track_height * height / self.overlay_natural_height))
            travel = max(1, track_height - thumb_height)
            thumb_top = track_top + int(travel * self.overlay_scroll_offset / max(1, self.overlay_scroll_max))
            canvas.create_rectangle(width - 6, track_top, width - 3, track_bottom, fill=OVERLAY_SEPARATOR, outline='', tags='overlay_content')
            self.draw_rounded_rectangle(canvas, width - 6, thumb_top, width - 3, thumb_top + thumb_height, 2, fill='#7F99AD', outline='', tags='overlay_content')
        canvas.tag_raise('overlay_header')

    def refresh_overlay(self, states: list[tuple[StatusState, int | None, str]], yellow_ms: int, red_ms: int) -> None:
        del yellow_ms, red_ms
        if not self.overlay_enabled_var.get():
            self.hide_overlay()
            return
        self.show_overlay()
        if self.overlay is None or self.overlay_canvas is None:
            return
        grouped: dict[str, dict[int, list[tuple[StatusState, int | None, str]]]] = {scope: {} for scope in TARGET_SCOPE_ORDER}
        for state, remaining, level in states:
            scope = self.target_tracker.relation_for(state.target_id)
            grouped.setdefault(scope, {}).setdefault(state.target_id, []).append((state, remaining, level))
        rendered_groups: list[object] = []
        for scope in TARGET_SCOPE_ORDER:
            people: list[object] = []
            for target_id in sorted(grouped.get(scope, {})):
                rendered_rows = tuple(((state.key, status_name(state.status_id, state.source_header), format_overlay_duration(remaining), level) for state, remaining, level in sorted(grouped[scope][target_id], key=lambda item: (item[1] if item[1] is not None else 10 ** 12, status_name(item[0].status_id, item[0].source_header).casefold()))))
                people.append((target_id, self.target_display_name(target_id), rendered_rows))
            if people:
                rendered_groups.append((scope, tuple(people)))
        pet_model = None
        counts = {scope: len(grouped.get(scope, {})) for scope in TARGET_SCOPE_ORDER}
        summary_parts = [f'{TARGET_SCOPE_DISPLAY_NAMES.get(scope, scope)} {counts[scope]}' for scope in TARGET_SCOPE_ORDER if counts[scope] > 0]
        summary_text = '｜'.join(summary_parts) or '目前沒有監控中的狀態'
        self.overlay_summary_text = summary_text
        self.overlay_summary_var.set(summary_text)
        groups_model = tuple(rendered_groups)
        old_natural_height = self.overlay_natural_height
        self.overlay_render_groups = groups_model
        self.overlay_render_pet = pet_model
        self.overlay_natural_height = self.overlay_model_natural_height(groups_model, pet_model)
        if self.overlay_drag_offset is not None or self.overlay_resize_mode:
            return
        if self.overlay_auto_height and self.overlay_natural_height != old_natural_height:
            self.adjust_overlay_height_to_content()
        else:
            self.overlay_scroll_max = max(0, self.overlay_natural_height - self.overlay_height)
            self.overlay_scroll_offset = min(self.overlay_scroll_offset, self.overlay_scroll_max)
        self.render_overlay_canvas()

    @staticmethod
    def format_duration(milliseconds: int | None) -> str:
        if milliseconds is None:
            return '—'
        total_seconds = max(0, int(round(milliseconds / 1000)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f'{hours}:{minutes:02d}:{seconds:02d}'
        return f'{minutes:02d}:{seconds:02d}'

    def _finalize_close(self) -> None:
        """保存最小待辦、終止資料工作並完成關閉。"""
        if getattr(self, 'close_finalized', False):
            return
        self.close_finalized = True
        self.close_after_data_load = False
        cancel_requested = self.__dict__.get('catalog_cancel_requested')
        if cancel_requested is not None:
            cancel_requested.set()
        worker_client = self.__dict__.get('catalog_worker_client')
        if worker_client is not None:
            worker_client.cancel()
        unknown_journal = self.__dict__.get('unknown_journal')
        if unknown_journal is not None:
            try:
                unknown_journal.flush()
            except (OSError, TypeError, ValueError):
                pass
        self.dismiss_close_choice_dialog()
        close_progress_job = self.__dict__.get('close_progress_job')
        if close_progress_job is not None:
            try:
                self.after_cancel(close_progress_job)
            except tk.TclError:
                pass
            self.close_progress_job = None
        self.running = False
        monitoring_event = self.__dict__.get('monitoring_active')
        monitor_session = self.__dict__.get('monitor_session')
        if monitor_session is not None:
            monitor_session.shutdown()
        else:
            if monitoring_event is not None:
                monitoring_event.clear()
            wake_event = self.__dict__.get('monitor_wake_event')
            if wake_event is not None:
                wake_event.set()
        if self.settings_save_job is not None:
            try:
                self.after_cancel(self.settings_save_job)
            except tk.TclError:
                pass
            self.settings_save_job = None
        if self.overlay_save_job is not None:
            try:
                self.after_cancel(self.overlay_save_job)
            except tk.TclError:
                pass
            self.overlay_save_job = None
        if self.overlay_drag_job is not None:
            try:
                self.after_cancel(self.overlay_drag_job)
            except tk.TclError:
                pass
            self.overlay_drag_job = None
        if self.overlay_render_job is not None:
            try:
                self.after_cancel(self.overlay_render_job)
            except tk.TclError:
                pass
            self.overlay_render_job = None
        try:
            self.save_settings()
        except Exception as exc:
            self.log_event(f'關閉時設定儲存失敗：{type(exc).__name__}: {exc}')
        if self.overlay is not None and self.overlay.winfo_exists():
            self.overlay.destroy()
        if self.pet_overlay is not None and self.pet_overlay.winfo_exists():
            self.pet_overlay.destroy()
        close_dialog = self.__dict__.get('close_progress_dialog')
        if close_dialog is not None:
            try:
                if close_dialog.winfo_exists():
                    close_dialog.destroy()
            except tk.TclError:
                pass
        self.destroy()

    def on_close(self) -> None:
        """沒有未知立即關閉；有未知才由使用者決定是否先比對。"""
        if getattr(self, 'close_finalized', False):
            return
        with self.data_load_lock:
            data_thread = self.__dict__.get('data_load_thread')
            loading = bool(data_thread is not None and data_thread.is_alive())
        status_count, item_count = self.unknown_pending_counts()
        action = decide_close_action(close_finalized=False, closing_after_work=bool(getattr(self, 'close_after_data_load', False)), catalog_work_active=loading, unknown_count=status_count + item_count)
        if action == CloseAction.SHOW_EXISTING_PROGRESS:
            self.show_close_progress_dialog()
            return
        if action == CloseAction.WAIT_FOR_ACTIVE_WORK:
            self.close_after_data_load = True
            self.monitoring_active.clear()
            self.monitor_wake_event.set()
            self.close_progress_heading_var.set('正在等待目前的資料檢查')
            self.close_progress_detail_var.set('完成後會關閉；也可以取消返回，或停止檢查並立即關閉。')
            self.show_close_progress_dialog()
            return
        if action == CloseAction.CHOOSE_UNKNOWN:
            self.show_close_choice_dialog()
            return
        self._finalize_close()

def write_startup_failure(exc: BaseException) -> None:
    """在 GUI 尚未建立前也留下啟動例外，避免 pythonw 靜默關閉。"""
    log_path = APPLICATION_DIR / local_data_filename('startup.log')
    timestamp = datetime.now().isoformat(timespec='seconds')
    message = ''.join([f'[{timestamp}] 啟動失敗：{type(exc).__name__}: {exc}\n', traceback.format_exc(), '\n'])
    try:
        with log_path.open('a', encoding='utf-8') as handle:
            handle.write(message)
    except OSError:
        pass

def _current_process_working_set_mb() -> float:
    """只用於開發驗收入口，不參與一般監控迴圈。"""
    if os.name != 'nt':
        return 0.0

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return 0.0
    return round(counters.WorkingSetSize / (1024 * 1024), 2)

def run_development_gui_probe(result_path: Path, replay_dir: Path) -> int:
    """由打包後 EXE 自行驗證 GUI 與監控，避免開發 Python 環境影響 Tk。"""
    app: RrfMonitorApp | None = None
    report: dict[str, object] = {'ok': False}
    try:
        app = RrfMonitorApp()
        app.withdraw()
        app.update()
        report['initial'] = {'working_set_mb': _current_process_working_set_mb(), 'status_ui_built': app.status_library_ui_built, 'pet_ui_built': app.pet_ui_built, 'settings_ui_built': app.settings_ui_built, 'job_catalog_loaded': _BUNDLED_JOB_SKILL_CATALOG is not None}
        assert not app.status_library_ui_built
        assert not app.pet_ui_built
        assert not app.settings_ui_built
        assert _BUNDLED_JOB_SKILL_CATALOG is None
        app.notebook.select(app.status_library_tab)
        app.on_notebook_tab_changed()
        app.update()
        loaded_records = len(app.status_library_records)
        report['status_loaded'] = {'working_set_mb': _current_process_working_set_mb(), 'records': loaded_records, 'job_catalog_loaded': _BUNDLED_JOB_SKILL_CATALOG is not None}
        assert app.status_library_ui_built
        assert loaded_records > 0
        assert _BUNDLED_JOB_SKILL_CATALOG is not None
        app.select_status_category('消耗品')
        app.update()
        visible_consumables = tuple(app.status_consumable_combobox.cget('values'))
        consumable_counts: dict[str, int] = {}
        for category in visible_consumables:
            app.status_consumable_subcategory_var.set(category)
            app.apply_status_filter()
            app.update_idletasks()
            consumable_counts[category] = len(app.filtered_status_id_order())
        report['consumable_catalog'] = {'visible_categories': visible_consumables, 'counts': consumable_counts}
        assert consumable_counts.get('全部消耗品') == 110
        assert all((count > 0 for category, count in consumable_counts.items() if category != '全部消耗品'))
        assert '特殊效果／變身' not in visible_consumables
        assert '其他／待確認' not in visible_consumables
        app.notebook.select(app.notebook.tabs()[0])
        app.on_notebook_tab_changed()
        app.update()
        report['status_released'] = {'working_set_mb': _current_process_working_set_mb(), 'records': len(app.status_library_records), 'job_catalog_loaded': _BUNDLED_JOB_SKILL_CATALOG is not None}
        assert not app.status_library_records
        assert _BUNDLED_JOB_SKILL_CATALOG is None
        app.notebook.select(app.pet_tab)
        app.on_notebook_tab_changed()
        app.update()
        assert app.pet_ui_built and app.pet_status_card is not None
        assert app.pet_satiety_label.winfo_exists()
        assert get_pet_info(1002).food_name_zh == '蘋果汁'
        app.notebook.select(app.settings_tab)
        app.on_notebook_tab_changed()
        app.update()
        assert app.settings_ui_built and app.sync_entry is not None
        report['lazy_tabs'] = {'pet_ui_built': app.pet_ui_built, 'settings_ui_built': app.settings_ui_built, 'working_set_mb': _current_process_working_set_mb()}
        app.dir_var.set(str(replay_dir))
        app.file_var.set('')
        app.auto_latest_var.set(True)
        app.interval_var.set('0.2')
        app.overlay_enabled_var.set(False)
        app.toggle_overlay()
        app.start_monitoring()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and app.incremental_parser.packet_count < 1:
            app.update()
            time.sleep(0.02)
        packet_count = app.incremental_parser.packet_count
        settle_started_wall = time.perf_counter()
        settle_started_cpu = time.process_time()
        settle_deadline = time.monotonic() + 3.0
        while time.monotonic() < settle_deadline:
            app.update()
            time.sleep(0.02)
        settle_wall = max(0.001, time.perf_counter() - settle_started_wall)
        settle_cpu = time.process_time() - settle_started_cpu
        report['monitor'] = {'working_set_mb': _current_process_working_set_mb(), 'packet_count': packet_count, 'active': app.monitoring_is_active(), 'stable_seconds': round(settle_wall, 2), 'cpu_percent_one_core': round(100 * settle_cpu / settle_wall, 2), 'self_id': app.target_tracker.self_id, 'snapshot_statuses': len(app.monitor_snapshot.statuses) if app.monitor_snapshot is not None else 0, 'main_table_rows': len(app.tree.get_children(''))}
        assert packet_count >= 1
        assert app.pet_overlay is not None and app.pet_overlay.winfo_exists()
        assert app.pet_overlay.window.state() != 'withdrawn'
        assert app.overlay is None or app.overlay.state() == 'withdrawn'
        report['independent_pet_card'] = True
        app.stop_monitoring()
        assert not app.monitoring_is_active()
        report['ok'] = True
        _write_json_atomically(result_path, report)
        return 0
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        try:
            _write_json_atomically(result_path, report)
        except OSError:
            pass
        return 1
    finally:
        if app is not None:
            app.running = False
            app.monitoring_active.clear()
            app.monitor_wake_event.set()
            try:
                if app.overlay is not None and app.overlay.winfo_exists():
                    app.overlay.destroy()
                app.destroy()
            except tk.TclError:
                pass

def run_self_test(paths: list[str]) -> int:
    parser = RrfParser()
    for text_path in paths:
        path = Path(text_path)
        try:
            packets, replay_date = parser.parse_file(path)
        except OSError as exc:
            print(f'{path}: 讀取失敗：{exc}')
            return 1
        headers = Counter((packet.header for packet in packets))
        status_packets = [packet for packet in packets if packet.header in {406, 1087, 2435}]
        damage_count = sum((headers.get(value, 0) for value in (138, 478, 737)))
        timeline = max((packet.timeline_ms for packet in packets), default=0)
        print(f'=== {path.name}')
        print(f"日期：{(datetime(*replay_date) if replay_date else '未知')}")
        print(f'封包：{len(packets)}，時間軸：{timeline / 1000:.3f} 秒，狀態事件：{len(status_packets)}，傷害封包：{damage_count}')
        print('狀態範例：')
        for packet in status_packets[:12]:
            data = packet.data
            status_id = u16(data, 2)
            target_id = u32(data, 4)
            if packet.header == 1087:
                remain = u32(data, 9) if len(data) >= 13 else None
                total = None
                state = data[8] if len(data) > 8 else None
            elif packet.header == 2435:
                total = u32(data, 9) if len(data) >= 13 else None
                remain = u32(data, 13) if len(data) >= 17 else None
                state = data[8] if len(data) > 8 else None
            else:
                total = None
                remain = None
                state = data[8] if len(data) > 8 else None
            print(f'  t={packet.timeline_ms:>6}ms header=0x{packet.header:04X} status=0x{status_id:04X} target=0x{target_id:08X} state={state} total={total} remain={remain}')
    return 0
if __name__ == '__main__':
    if '--catalog-worker' in sys.argv:
        worker_index = sys.argv.index('--catalog-worker')
        worker_paths = sys.argv[worker_index + 1:worker_index + 4]
        if len(worker_paths) != 3:
            raise SystemExit(2)
        raise SystemExit(run_catalog_worker(*(Path(value) for value in worker_paths)))
    if '--self-test' in sys.argv:
        test_paths = [item for item in sys.argv[1:] if item != '--self-test']
        if not test_paths:
            test_paths = [str(DEFAULT_REPLAY_DIR)]
        raise SystemExit(run_self_test(test_paths))
    try:
        app = RrfMonitorApp()
        app.mainloop()
    except BaseException as exc:
        write_startup_failure(exc)
        raise
