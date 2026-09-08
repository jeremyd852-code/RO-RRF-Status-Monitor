"""1.6.6 共用清單、跨入口選取與授權觀測回歸；只使用合成資料。"""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rrf_monitor as mon
import tkinter as tk
from tkinter import ttk
from monitor_core.catalog import StatusLibraryRecord, StatusSearchIndex, paginate_status_ids
from monitor_core.policies import PolicyResolver, ScopePolicy, MODE_AUTO, MODE_CUSTOM, MODE_OFF


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def record(sid, *, name=None, jobs=(), group="增益", consumable="", confirmed=True, tags=()):
    return StatusLibraryRecord(sid, 0x0983, name or f"測試狀態 {sid}",
                               "技能" if jobs else "其他", jobs, group, consumable,
                               aliases=(f"EFST_SYNTHETIC_{sid}",), source_tags=tags,
                               name_confirmed=confirmed)


class SelectionIndexTests(unittest.TestCase):
    def test_job_debuff_is_accessible_from_both_entries_once(self):
        records = [record(sid, jobs=("合成職業",), group="減益") for sid in range(40)]
        index = StatusSearchIndex([*records, records[0]])
        self.assertEqual(set(index.filter(category="DEBUFF")), set(range(40)))
        self.assertEqual(set(index.filter(mode="職業技能", job="合成職業")), set(range(40)))
        self.assertEqual(len(index.filter()), 40)

    def test_all_sources_remain_selectable(self):
        index = StatusSearchIndex(record(i, tags=(source,)) for i, source in enumerate(
            ("玩家技能", "怪物技能", "道具", "地圖", "不明來源")))
        self.assertEqual(set(index.filter()), set(range(5)))

    def test_query_is_scoped_and_searches_alias_id_and_effect(self):
        index = StatusSearchIndex([record(1, name="同名", group="減益"), record(2, name="同名")])
        self.assertEqual(index.filter(category="DEBUFF", query="同名"), (1,))
        self.assertEqual(index.filter(query="EFST_SYNTHETIC_2"), (2,))
        self.assertEqual(index.filter(query="0x0001"), (1,))
        self.assertEqual(index.filter(query="減益"), (1,))

    def test_selected_and_current_entries_ignore_source_category(self):
        index = StatusSearchIndex([record(1), record(2, jobs=("合成職業",), group="減益")])
        self.assertEqual(index.filter(mode="已勾選", category="消耗品", selected_ids={2}), (2,))
        self.assertEqual(index.filter(mode="目前身上", observed_status_ids={1}), (1,))

    def test_decimal_and_hex_id_queries_are_exact(self):
        index = StatusSearchIndex([record(10), record(110), record(1000)])
        self.assertEqual(index.filter(query="10"), (10,))
        self.assertEqual(index.filter(query="0x0a"), (10,))
        self.assertEqual(index.filter(query="0"), ())

    def test_consumable_and_job_can_share_one_id(self):
        index = StatusSearchIndex([record(1, jobs=("合成職業",), consumable="能力增強")])
        self.assertEqual(index.filter(category="消耗品"), (1,))
        self.assertEqual(index.filter(mode="職業技能"), (1,))

    def test_unknown_nature_and_unconfirmed_names_in_other_entry(self):
        index = StatusSearchIndex([record(1, jobs=("合成職業",), group="未分類"),
                                   record(2, jobs=("合成職業",), group="減益", confirmed=False),
                                   record(3, jobs=("合成職業",))])
        self.assertEqual(set(index.filter(category="其他／待確認")), {1, 2})

    def test_same_name_different_ids_are_not_merged(self):
        index = StatusSearchIndex([record(1, name="同名"), record(2, name="同名")])
        self.assertEqual(index.filter(query="同名"), (1, 2))

    def test_page_selection_is_bounded(self):
        page = paginate_status_ids(range(125), 1, 60)
        self.assertEqual(page.ids, tuple(range(60, 120)))
        self.assertEqual(page.total_count, 125)


class SelectionAppTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.names = {0: "挑釁測試", 10: "同名測試", 11: "同名測試", 12: "增益測試"}
        self.groups = {0: "減益", 10: "減益", 11: "減益", 12: "增益"}
        self.metadata = {10: {"aliases": ["舊稱測試"], "functional_category": "控制"}}
        for name, value in {
            "EFST_NAMES": self.names, "RUNTIME_STATUS_METADATA": self.metadata,
            "is_readable_status_name": lambda sid: sid in self.names,
            "status_name": lambda sid, *_: self.names.get(sid, f"未確認狀態 {sid}"),
            "status_group": lambda sid, *_: self.groups.get(sid, "未分類"),
            "status_library_category": lambda sid, *_: "技能" if sid in (0, 10, 11) else "BUFF",
            "status_job_names": lambda sid: ("合成職業",) if sid in (0, 10, 11) else (),
            "status_skill_names": lambda sid: ("合成技能",) if sid in (0, 10, 11) else (),
            "status_consumable_subcategory": lambda sid, *_: "能力增強" if sid == 12 else "",
        }.items():
            self.stack.enter_context(patch.object(mon, name, value))
        self.app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
        app = self.app
        self.states = []
        self.trusted = {101, 102, 103}
        self.relations = {101: "自己", 102: "隊伍成員", 103: "隊伍成員", 104: "其他目標"}
        app.tracker = SimpleNamespace(snapshot=lambda: list(self.states))
        app.target_tracker = SimpleNamespace(
            self_id=101, trusted_target_ids=lambda: set(self.trusted),
            relation_for=lambda sid: self.relations.get(sid, "其他目標"),
            target_name=lambda _sid: "合成人物",
        )
        self.resolver = PolicyResolver({"self": ScopePolicy(MODE_CUSTOM, frozenset()),
                                       "party": ScopePolicy(MODE_AUTO), "other": ScopePolicy(MODE_OFF)})
        app.current_policy_resolver = lambda: self.resolver
        self.valid = True
        app.source_data_status = lambda: (self.valid, "資料有效" if self.valid else "上次資料，等待更新")
        app.effective_sync_offset = lambda: (100, "test")
        app.core_monitoring_only = False
        app.status_selected_ids = {0}
        app.target_scope_status_ids = {"自己": {0}, "隊伍成員": {11}}
        app.status_edit_scope = "自己"
        app.status_edit_scope_var = Value("自己")
        app.current_status_edit_scope = lambda: app.status_edit_scope
        app.all_scope_status_ids = lambda: set(app.status_selected_ids).union(*app.target_scope_status_ids.values())
        app.target_status_overrides = {}
        app.status_option_generation = 1
        app.status_library_index_signature = None
        app.status_library_index = StatusSearchIndex()
        app.status_checks = {}
        app.status_library_widgets_ready = False
        app.status_option_ids = {mon.FOCUS_STATUS_ID}
        app.status_option_source_headers = {}
        app.status_library_records = {}
        app.status_unresolved_ids = set()
        app.status_library_name_counts = {}
        app.status_job_value_map = {"全部職業": "全部職業"}
        for attr, initial in {
            "status_filter_var": "", "status_job_var": "全部職業", "status_job_effect_var": "全部效果",
            "status_consumable_subcategory_var": "全部消耗品", "status_function_var": "全部用途",
            "status_index_mode_var": "分類", "status_category_var": "全部分類",
        }.items():
            setattr(app, attr, Value(initial))
        app.update_status_filter_controls = lambda: None
        app.sync_tracker_status_filter = lambda: None
        app.save_settings = lambda: None
        app.rebuild_status_options = lambda: None

    def observe(self, sid, target=101, remaining=None, active=True):
        self.states.append((SimpleNamespace(status_id=sid, target_id=target, active=active), remaining))

    def query_entry(self, entry, **kwargs):
        mode, category = self.app.status_entry_conditions(entry)
        return self.app.query_status_selection(mode=mode, category=category, **kwargs)

    def test_first_target_query_builds_complete_shared_index_without_widgets(self):
        ids = self.query_entry("全部／搜尋", target_id=101)
        self.assertTrue(set(self.names).issubset(ids))
        index = self.app.status_library_index
        self.app.select_status_player_entry("全部／搜尋")
        self.assertEqual(self.app.filtered_status_id_order(), ids)
        self.assertIs(self.app.status_library_index, index)

    def test_saved_unknown_zero_and_authorized_observed_unknown_selectable(self):
        self.app.target_status_overrides = {102: {65000}}
        self.observe(65001)
        self.observe(65002, target=104)
        ids = set(self.query_entry("全部／搜尋"))
        self.assertTrue({0, 65000, 65001}.issubset(ids))
        self.assertNotIn(65002, ids)
        self.assertEqual(self.app.status_library_records[65000].name, "未確認狀態 65000")
        self.assertIn(65001, self.query_entry("其他／待確認"))

    def test_unselected_observation_visible_but_other_scope_excluded(self):
        self.observe(10)
        self.observe(11, target=102)
        self.assertEqual(self.query_entry("目前身上"), (10,))
        self.assertEqual(self.query_entry("目前身上", target_id=102), (11,))
        self.assertEqual(self.app.status_selected_ids, {0})

    def test_current_scope_unions_people_but_target_query_is_independent(self):
        self.observe(10, target=102)
        self.observe(10, target=103)
        self.observe(11, target=103)
        self.app.status_edit_scope = "隊伍成員"
        self.assertEqual(self.query_entry("目前身上"), (10, 11))
        self.assertEqual(self.query_entry("目前身上", target_id=102), (10,))
        self.assertEqual(self.query_entry("目前身上", target_id=103), (10, 11))

    def test_stale_and_expired_do_not_claim_current_but_remain_selectable(self):
        self.observe(10, remaining=100)
        self.observe(11, remaining=101)
        self.observe(65001, active=False)
        self.assertEqual(self.query_entry("目前身上"), (11,))
        self.valid = False
        self.assertEqual(self.query_entry("目前身上"), ())
        self.assertIn(65001, self.query_entry("全部／搜尋"))
        self.assertIn("等待", self.app.status_observation_context)

    def test_revoked_policy_removes_current_and_unconfirmed_observation(self):
        self.observe(65001, target=102)
        self.assertEqual(self.query_entry("目前身上", target_id=102), (65001,))
        self.resolver = PolicyResolver({"self": ScopePolicy(MODE_AUTO), "party": ScopePolicy(MODE_OFF)})
        self.assertEqual(self.query_entry("目前身上", target_id=102), ())
        self.assertNotIn(65001, self.query_entry("全部／搜尋"))

    def test_core_only_never_collects_party(self):
        self.app.core_monitoring_only = True
        self.observe(65001, target=102)
        self.assertNotIn(65001, self.query_entry("全部／搜尋"))

    def test_existing_id_name_generation_refreshes_aliases_and_effect(self):
        self.query_entry("全部／搜尋")
        before = self.app.status_library_index
        self.names[10] = "更新名稱"
        self.groups[10] = "增益"
        self.metadata[10]["aliases"] = ["更新別名"]
        self.app.status_option_generation += 1
        self.assertEqual(self.query_entry("全部／搜尋", query="更新別名"), (10,))
        self.assertNotIn(10, self.query_entry("異常狀態"))
        self.assertIsNot(self.app.status_library_index, before)

    def test_code_change_drops_old_skill_links(self):
        self.metadata[10] = {"review_status": "代碼變動，待重新核對"}
        self.groups[10] = "未分類"
        self.assertNotIn(10, self.query_entry("職業技能"))
        self.assertIn(10, self.query_entry("其他／待確認"))
        self.assertEqual(self.app.status_library_records[10].skills, ())

    def test_selected_detail_refreshes_changed_record_without_reselection_or_tick_work(self):
        app = self.app
        self.query_entry("全部／搜尋")
        tree = Mock()
        tree.selection.return_value = ("row10",)
        tree.exists.return_value = True
        tree.item.return_value = ()
        app.status_library_tree = tree
        app.status_library_widgets_ready = True
        app.status_tree_row_ids = {10: "row10"}
        app.status_tree_status_ids = {"row10": 10}
        app.status_tree_row_values = {10: app.status_library_row_values(10)}
        app.status_tree_order_ids = [10]
        app.status_detail_status_id = 10
        app.status_page_index = 0
        app.status_page_previous_button = app.status_page_next_button = None
        app.status_select_all_button = app.status_select_none_button = Mock()
        for name in ("status_selection_summary_var", "status_page_info_var", "status_detail_name_var",
                     "status_detail_meta_var", "status_detail_selected_var"):
            setattr(app, name, Value())
        app.status_detail_alert_vars = {key: Value(False) for key in mon.ALERT_RULE_KEYS}
        app.status_detail_controls = []
        app.status_alert_rules = {}
        app.default_status_alert_rule = lambda _sid: {key: False for key in mon.ALERT_RULE_KEYS}
        app.filtered_status_id_order = lambda: [10]
        app.on_status_tree_select()
        self.assertEqual(app.status_detail_name_var.get(), "同名測試")
        self.names[10] = "更新詳細名稱"
        self.groups[10] = "增益"
        self.metadata[10]["functional_category"] = "更新用途"
        app.status_option_generation += 1
        self.query_entry("全部／搜尋")
        with patch.object(app, "on_status_tree_select", wraps=app.on_status_tree_select) as selected:
            mon.RrfMonitorApp.rebuild_status_options(app)
            self.assertEqual(selected.call_count, 1)
            self.assertEqual(app.status_detail_name_var.get(), "更新詳細名稱")
            self.assertIn("效果：增益｜用途：更新用途", app.status_detail_meta_var.get())
            for _ in range(3):
                mon.RrfMonitorApp.rebuild_status_options(app)
            self.assertEqual(selected.call_count, 1)
        tree.selection_set.assert_not_called()

    def test_known_observations_do_not_rebuild_shared_catalog(self):
        self.query_entry("全部／搜尋")
        before = self.app.status_library_index
        with patch.object(mon, "status_job_names", side_effect=AssertionError("unexpected rebuild")):
            self.observe(10)
            self.assertEqual(self.query_entry("目前身上"), (10,))
            self.assertIs(self.app.status_library_index, before)

    def test_entry_switching_keeps_selection_and_page_operations_are_local(self):
        for entry in mon.STATUS_LIBRARY_PLAYER_ENTRIES:
            self.app.select_status_player_entry(entry)
            self.assertEqual(self.app.status_selected_ids, {0})
        self.app.status_current_page_ids = (10, 11)
        self.app.select_all_statuses()
        self.assertEqual(self.app.status_selected_ids, {0, 10, 11})
        self.app.status_current_page_ids = (10,)
        self.app.select_no_statuses()
        self.assertEqual(self.app.status_selected_ids, {0, 11})
        self.assertEqual(self.app.target_scope_status_ids["隊伍成員"], {11})

    def test_same_name_row_disambiguates_id_and_shows_effect(self):
        self.query_entry("全部／搜尋")
        row = self.app.status_library_row_values(10)
        self.assertIn("10", row[1])
        self.assertEqual(row[2], mon.effect_label("減益"))

    def test_widget_release_preserves_shared_index_and_saved_unknown(self):
        self.app.target_status_overrides = {102: {65000}}
        self.query_entry("全部／搜尋")
        before = self.app.status_library_index
        app = self.app
        for attr in ("status_rebuild_job", "status_filter_job", "status_options_inner",
                     "status_library_tree", "status_consumable_combobox",
                     "status_page_previous_button", "status_page_next_button"):
            setattr(app, attr, None)
        app.status_rebuild_token = 0
        for attr in ("status_tree_row_ids", "status_tree_status_ids", "status_tree_row_values", "status_checkbuttons"):
            setattr(app, attr, {})
        app.status_tree_order_ids = []
        app.status_detail_controls = []
        for attr in ("status_detail_name_var", "status_detail_meta_var", "status_page_info_var"):
            setattr(app, attr, Value())
        app.status_library_ui_built = True
        app.status_options_canvas = SimpleNamespace(yview_moveto=lambda _position: None)
        app.release_status_library_memory()
        self.assertIs(app.status_library_index, before)
        self.assertIn(65000, app.status_unresolved_ids)
        self.assertIn(65000, self.query_entry("全部／搜尋", target_id=102))

    def test_scope_switch_preserves_independent_id_sets(self):
        app = self.app
        app.status_detail_status_id = None
        app.update_scope_status_buttons = lambda: None
        app.update_selected_status_summary = lambda: None
        app.update_tab_labels = lambda: None
        app.select_status_edit_scope("隊伍成員")
        app.set_status_selected(65000, True)
        app.select_status_edit_scope("自己")
        self.assertEqual(app.status_selected_ids, {0})
        self.assertEqual(app.target_scope_status_ids["隊伍成員"], {11, 65000})

    def test_target_dialog_uses_shared_entries_and_selects_only_one_page(self):
        app = self.app
        try:
            tk.Tk.__init__(app)
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        app.withdraw()
        self.addCleanup(lambda: tk.Tk.destroy(app))
        self.names.update({sid: f"合成狀態 {sid}" for sid in range(1000, 1125)})
        app.target_display_name = lambda _tid: "合成人物"
        app.target_display_mode_overrides = {}
        app.target_alert_overrides = {}
        app.target_display_mode_for = lambda _tid: "自訂監測"
        app.rebuild_target_options = lambda: None
        app.set_message = lambda _message: None
        self.stack.enter_context(patch.object(mon, "job_filter_entries", return_value=(("合成職業", "合成職業"),)))
        app.open_target_status_dialog(101)
        dialog = next(widget for widget in app.winfo_children() if isinstance(widget, tk.Toplevel))

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(dialog))
        entries = next(widget for widget in widgets if isinstance(widget, ttk.Combobox)
                       and tuple(widget.cget("values")) == mon.STATUS_LIBRARY_PLAYER_ENTRIES)
        self.assertEqual(entries.get(), "全部／搜尋")
        expected_ids = {0} | set(self.query_entry("全部／搜尋")[:mon.STATUS_LIBRARY_PAGE_SIZE])
        select_page = next(widget for widget in widgets if isinstance(widget, ttk.Button)
                           and str(widget.cget("text")).startswith("勾選本頁"))
        select_page.invoke()
        entries.set("已勾選")
        entries.event_generate("<<ComboboxSelected>>")
        app.update_idletasks()
        following = next(widget for widget in widgets if isinstance(widget, ttk.Checkbutton)
                         and str(widget.cget("text")).startswith("跟隨自己"))
        following.invoke()
        apply = next(widget for widget in widgets if isinstance(widget, ttk.Button)
                     and widget.cget("text") == "套用個別設定")
        apply.invoke()
        self.assertEqual(app.target_status_overrides[101], expected_ids)
        self.assertEqual(app.status_selected_ids, {0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
