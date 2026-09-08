"""Settings failure recovery using temporary files and synthetic UI values only."""
from __future__ import annotations

import errno
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
import catalog.schema as schema


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def settings_fixture(path: Path):
    app = SimpleNamespace(
        config_path=path, settings={}, _last_saved_settings=None,
        commit_current_scope_status_selection=lambda: None,
        all_scope_status_ids=lambda: set(), status_alert_rule_touched=set(),
        status_alert_rule_vars={}, status_alert_rules={},
        scope_display_mode=lambda scope: mon.TARGET_DISPLAY_MODE_ALL,
        target_scope_status_ids={}, target_sound_vars={},
        target_tracker=SimpleNamespace(target_name=lambda target: None),
        parse_target_id=mon.RrfMonitorApp.parse_target_id,
        safe_float=mon.RrfMonitorApp.safe_float,
        current_tree_column_widths=lambda: {},
        overlay_width=400, overlay_height=300, overlay_x=None, overlay_y=None,
        overlay_opacity=0.9, overlay_font_size=12, core_monitoring_only=False,
        pet_overlay=None, pet_overlay_settings={},
        set_message=mock.Mock(), log_event=mock.Mock(),
    )
    values = {
        "self_target_id": "", "self_target_name": "", "dir": "synthetic-replay",
        "ro_dir": "synthetic-ro", "file": "", "auto_latest": True,
        "show_all_statuses": False, "show_technical_columns": False,
        "red": "15", "yellow": "30", "yellow_sound": False,
        "red_sound": True, "apply_sound": False, "sound_mode": "",
        "sound_file": "", "sound_volume": 100, "overlay_enabled": False,
        "overlay_auto_height": True, "overlay_locked": False, "sync": "5",
        "auto_sync": True, "interval": "0.5", "target_view": "",
        "pet_monitor_enabled": True, "pet_selected_id": "", "pet_alert_enabled": True,
        "pet_alert_threshold": "25", "pet_overlay_enabled": False,
    }
    for name, value in values.items():
        setattr(app, name + "_var", Value(value))
    return app


class SettingsFailureTests(unittest.TestCase):
    def test_unknown_extensions_survive_save_without_reviving_legacy_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            app = settings_fixture(path)
            extension = {"nested": [0, "example", False]}
            app.settings = {
                "custom_extension": extension,
                "target_ids": [999], "target_scope_enabled": {"自己": False},
                "target_status_overrides": {"999": [0]},
                "alert_policies": {"extension_note": "synthetic"},
            }
            mon.RrfMonitorApp.save_settings(app)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["custom_extension"], extension)
            self.assertEqual(saved["alert_policies"]["extension_note"], "synthetic")
            self.assertFalse(set(mon.LEGACY_AUTHORITY_KEYS) & saved.keys())
            self.assertEqual(saved["settings_schema_version"], 2)

    def test_canonical_scope_sound_takes_precedence_and_normalizes_labels(self):
        result = mon.resolved_target_sound_settings(
            {"自己": True, "隊伍成員": False}, core_monitoring_only=False,
            policy_scopes={"self": False, "party": True})
        self.assertFalse(result["自己"])
        self.assertTrue(result["隊伍成員"])
        fallback = mon.resolved_target_sound_settings(
            {"self": False, "隊伍成員": True}, core_monitoring_only=False,
            policy_scopes={"自己": True})
        self.assertTrue(fallback["自己"])
        self.assertTrue(fallback["隊伍成員"])

    def test_core_sound_scope_keeps_explicit_self_mute_and_restricts_other_scopes(self):
        result = mon.resolved_target_sound_settings(
            {}, core_monitoring_only=True, policy_scopes={"self": False, "party": True})
        self.assertFalse(any(result.values()))

    def test_partial_temporary_write_preserves_original_and_previous_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            original = b'{"sentinel":"current"}\n'
            backup = b'{"sentinel":"previous"}\n'
            path.write_bytes(original)
            path.with_suffix(".json.bak").write_bytes(backup)
            app = settings_fixture(path)
            real_fdopen = schema.os.fdopen

            class InterruptedFile:
                def __init__(self, *args, **kwargs):
                    self.handle = real_fdopen(*args, **kwargs)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.handle.close()

                def write(self, text):
                    self.handle.write(text[:16])
                    self.handle.flush()
                    raise OSError(errno.ENOSPC, "synthetic disk full")

            with mock.patch.object(schema.os, "fdopen", InterruptedFile):
                mon.RrfMonitorApp.save_settings(app)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.with_suffix(".json.bak").read_bytes(), backup)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertIsNone(app._last_saved_settings)
            app.log_event.assert_called_once()

    def test_replace_failure_preserves_original_and_does_not_mark_save_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            original = b'{"sentinel":"retain"}\n'
            path.write_bytes(original)
            app = settings_fixture(path)
            with mock.patch.object(schema.os, "replace", side_effect=PermissionError("synthetic replace failure")):
                mon.RrfMonitorApp.save_settings(app)
            self.assertEqual(path.read_bytes(), original)
            self.assertIsNone(app._last_saved_settings)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            # A later retry must still write the unsaved values.
            mon.RrfMonitorApp.save_settings(app)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pet_alert_threshold"], 25)
            self.assertEqual(path.with_suffix(".json.bak").read_bytes(), original)
            self.assertIsNotNone(app._last_saved_settings)

    def test_successful_unchanged_save_does_not_rotate_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{"sentinel":"previous"}', encoding="utf-8")
            app = settings_fixture(path)
            mon.RrfMonitorApp.save_settings(app)
            tracked = (path, path.with_suffix(".json.bak"))
            before = [(item.read_bytes(), item.stat().st_mtime_ns) for item in tracked]
            mon.RrfMonitorApp.save_settings(app)
            self.assertEqual([(item.read_bytes(), item.stat().st_mtime_ns) for item in tracked], before)

    def test_nonfinite_missing_or_unrepresentable_values_use_fallback(self):
        class OverflowValue:
            def __float__(self):
                raise OverflowError("synthetic overflow")

        for value in ("inf", "-inf", "NaN", "1e999", float("inf"), float("nan"), None, {}, 10 ** 1000, OverflowValue()):
            with self.subTest(value=type(value).__name__ + ":" + str(value)[:20]):
                self.assertEqual(mon.RrfMonitorApp.safe_float(value, 25), 25)
        self.assertEqual(mon.RrfMonitorApp.safe_float("7", 25), 7.0)
        self.assertEqual(mon.RrfMonitorApp.safe_float("0.5", 25), 0.5)

    def test_nonfinite_pet_threshold_can_be_saved_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in ("inf", "NaN", "1e999"):
                with self.subTest(value=value):
                    app = settings_fixture(path)
                    app.pet_alert_threshold_var.value = value
                    mon.RrfMonitorApp.save_settings(app)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pet_alert_threshold"], mon.PET_ALERT_DEFAULT_THRESHOLD)
                    app.log_event.assert_not_called()

    def test_dimensions_and_column_widths_recover_from_nonfinite_settings(self):
        values = ("inf", "-inf", "NaN", "1e999", float("inf"), float("nan"), None)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(mon.RrfMonitorApp.safe_dimension(value, 280, 220, 600), 280)
                self.assertEqual(mon.RrfMonitorApp.load_tree_column_widths({"name": value}), {"name": 100})
        self.assertEqual(mon.RrfMonitorApp.safe_dimension("1e100", 280, 220, 600), 600)
        self.assertEqual(mon.RrfMonitorApp.safe_dimension(-100, 280, 220, 600), 220)
        self.assertEqual(mon.RrfMonitorApp.load_tree_column_widths({"name": 180, "status": 2000}), {"name": 180, "status": 1000})

    def test_overlay_coordinates_reject_nonfinite_and_preserve_negative_monitors(self):
        for value in (float("inf"), float("-inf"), float("nan"), None, "inf", True, 10 ** 1000):
            with self.subTest(value=type(value).__name__):
                self.assertIsNone(mon.RrfMonitorApp.safe_coordinate(value))
        self.assertEqual(mon.RrfMonitorApp.safe_coordinate(-1920), -1920)
        self.assertEqual(mon.RrfMonitorApp.safe_coordinate(120.5), 120)
        self.assertEqual(mon.RrfMonitorApp.safe_coordinate(1e100), 2**30)
        self.assertEqual(mon.RrfMonitorApp.safe_coordinate(-1e100), -(2**30))

    def test_close_still_destroys_both_cards_and_root_after_save_exception(self):
        def overlay():
            return SimpleNamespace(winfo_exists=lambda: True, destroy=mock.Mock())

        app = SimpleNamespace(
            close_finalized=False, close_after_data_load=False, running=True,
            cancel_alert_audio=mock.Mock(),
            dismiss_close_choice_dialog=mock.Mock(),
            monitor_session=SimpleNamespace(shutdown=mock.Mock()),
            settings_save_job=None, overlay_save_job=None,
            overlay_drag_job=None, overlay_render_job=None,
            save_settings=mock.Mock(side_effect=ValueError("synthetic UI read failure")),
            log_event=mock.Mock(), overlay=overlay(), pet_overlay=overlay(),
            destroy=mock.Mock(),
        )
        mon.RrfMonitorApp._finalize_close(app)
        self.assertTrue(app.close_finalized)
        self.assertFalse(app.running)
        app.cancel_alert_audio.assert_called_once()
        app.monitor_session.shutdown.assert_called_once()
        app.log_event.assert_called_once()
        self.assertIn("synthetic UI read failure", app.log_event.call_args.args[0])
        app.overlay.destroy.assert_called_once()
        app.pet_overlay.destroy.assert_called_once()
        app.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
