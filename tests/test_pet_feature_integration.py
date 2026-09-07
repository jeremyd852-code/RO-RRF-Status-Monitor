"""Own-pet reminder integration without Tk windows, personal recordings or audio."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import json
import struct
import sys
import threading
import time
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
from monitor_core.pet_alerts import PetAlertController


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def property_packet(index, satiety, *, pet_id=1002, name="SyntheticPet"):
    data = struct.pack("<H24sBHHHHH", mon.PET_PROPERTY_HEADER, name.encode("cp950")[:24],
                       0, 20, satiety, 500, 0, pet_id)
    return mon.ReplayPacket(index, index * 100, mon.PET_PROPERTY_HEADER, data)


def state_packet(index, satiety):
    data = struct.pack("<HBII", mon.PET_STATE_HEADER, 2, 900001, satiety)
    return mon.ReplayPacket(index, index * 100, mon.PET_STATE_HEADER, data)


def app_fixture():
    app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
    app.pet_tracker = mon.PetTracker()
    app.pet_alert_controller = PetAlertController()
    app.pet_alert_events = deque()
    app.pet_alert_lock = threading.Lock()
    app.config_lock = threading.Lock()
    app.pet_monitor_enabled = True
    app.pet_alert_enabled = True
    app.pet_alert_threshold = 10
    app.pet_monitor_enabled_var = Value(True)
    app.pet_alert_enabled_var = Value(True)
    app.pet_alert_threshold_var = Value("10")
    app.pet_live_observation_key = None
    app.incremental_parser = SimpleNamespace(suppress_apply_events=False)
    app.monitoring_active = threading.Event()
    app.monitoring_active.set()
    app.running = True
    app.monitor_reset_requested = threading.Event()
    app.monitoring_has_started = True
    app.current_path = Path("synthetic-recording.rrf")
    app.last_rrf_data_monotonic = time.monotonic()
    app.pet_ui_built = False
    app.overlay_enabled_var = Value(False)
    app.pet_overlay_enabled_var = Value(False)
    app.play_alert_sound = mock.Mock()
    return app


class PetFeatureIntegrationTests(unittest.TestCase):
    def test_entering_critical_alerts_now_then_uses_20_seconds_and_downgrades_to_60(self):
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=1000.0) as clock:
            app = app_fixture()
            app.pet_alert_threshold = 25
            app.pet_alert_threshold_var.set("25")

            def observe(at, value, expected):
                clock.return_value = at
                app.last_rrf_data_monotonic = at
                packet = property_packet(0, value) if app.pet_tracker.snapshot().pet_id is None else state_packet(int(at), value)
                app.consume_pet_packets([packet], False)
                app.process_pet_alerts()
                self.assertEqual(app.play_alert_sound.call_count, expected)

            observe(1000, 20, 1)
            observe(1001, 10, 2)
            observe(1020.999, 9, 2)
            observe(1021, 9, 3)
            observe(1041, 11, 3)
            observe(1081, 11, 4)
            observe(1082, 10, 5)
            observe(1083, 50, 5)
            observe(1084, 0, 6)

    def test_queued_critical_is_cancelled_by_recovery_into_normal_low(self):
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=1000.0) as clock:
            app = app_fixture()
            app.pet_alert_threshold = 25
            app.pet_alert_threshold_var.set("25")
            app.consume_pet_packets([property_packet(0, 20)], False)
            app.process_pet_alerts()
            clock.return_value = 1001.0
            app.last_rrf_data_monotonic = 1001.0
            app.consume_pet_packets([state_packet(1, 10)], False)
            self.assertEqual(len(app.pet_alert_events), 1)
            app.consume_pet_packets([state_packet(2, 20)], False)
            self.assertEqual(len(app.pet_alert_events), 0)
            app.process_pet_alerts()
            app.play_alert_sound.assert_called_once_with("pet", "SyntheticPet", 20)

    def test_same_critical_batch_retains_one_reminder_using_latest_value(self):
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=1000.0) as clock:
            app = app_fixture()
            app.pet_alert_threshold = 25
            app.pet_alert_threshold_var.set("25")
            app.consume_pet_packets([property_packet(0, 20)], False)
            app.process_pet_alerts()
            clock.return_value = 1001.0
            app.last_rrf_data_monotonic = 1001.0
            app.consume_pet_packets([state_packet(1, 10), state_packet(2, 9)], False)
            self.assertEqual(len(app.pet_alert_events), 1)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 2)
            app.play_alert_sound.assert_called_with("pet", "SyntheticPet", 9)

    def test_critical_repeats_still_require_new_satiety_not_general_traffic(self):
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=1000.0) as clock:
            app = app_fixture()
            app.consume_pet_packets([property_packet(0, 8)], False)
            app.process_pet_alerts()
            for at in (1020.0, 1040.0, 1600.0):
                clock.return_value = at
                app.last_rrf_data_monotonic = at
                app.consume_pet_packets([mon.ReplayPacket(1, 100, 0x0106, b"\x06\x01")], False)
                app.process_pet_alerts()
                self.assertEqual(app.play_alert_sound.call_count, 1)
            app.consume_pet_packets([state_packet(2, 8)], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 2)

    def test_new_defaults_keep_existing_user_threshold_and_disabled_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
            app.config_path = Path(directory) / "config.json"
            settings = app.load_settings()
            self.assertTrue(settings["pet_alert_enabled"])
            self.assertEqual(settings["pet_alert_threshold"], 25)
            app.config_path.write_text(json.dumps({"pet_alert_enabled": False, "pet_alert_threshold": 7}), encoding="utf-8")
            saved = app.load_settings()
            self.assertFalse(saved["pet_alert_enabled"])
            self.assertEqual(saved["pet_alert_threshold"], 7)

    def test_repeat_requires_new_satiety_and_recovers_without_a_poll_timer(self):
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=1000.0) as clock:
            app = app_fixture()
            app.pet_alert_threshold = 25
            app.pet_alert_threshold_var.set("25")
            app.consume_pet_packets([property_packet(0, 25)], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 1)
            clock.return_value = 1059.0
            app.last_rrf_data_monotonic = 1059.0
            app.consume_pet_packets([state_packet(1, 24)], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 1)
            # General replay traffic does not create another hunger reminder.
            clock.return_value = 1600.0
            app.last_rrf_data_monotonic = 1600.0
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 1)
            app.consume_pet_packets([state_packet(2, 23)], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 2)
            clock.return_value = 1620.0
            app.last_rrf_data_monotonic = 1620.0
            app.consume_pet_packets([state_packet(3, 50), state_packet(4, 25)], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 3)
            clock.return_value = 1700.0
            app.last_rrf_data_monotonic = 1700.0
            app.consume_pet_packets([property_packet(5, 24)], True)
            app.process_pet_alerts()
            # Intimacy and feeding acknowledgements must not repeat old hunger.
            intimacy = mon.ReplayPacket(6, 600, 0x01A4, struct.pack("<HBII", 0x01A4, 1, 900001, 600))
            feeding = mon.ReplayPacket(7, 700, 0x01A3, struct.pack("<HBH", 0x01A3, 1, 531))
            app.consume_pet_packets([intimacy, feeding], False)
            app.process_pet_alerts()
            self.assertEqual(app.play_alert_sound.call_count, 3)

    def test_reminder_works_without_pet_tab_and_independent_of_both_cards(self):
        for status_card, pet_card in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(status_card=status_card, pet_card=pet_card):
                app = app_fixture()
                app.overlay_enabled_var.set(status_card)
                app.pet_overlay_enabled_var.set(pet_card)
                app.consume_pet_packets([property_packet(0, 8)], False)
                app.process_pet_alerts()
                app.process_pet_alerts()
                app.play_alert_sound.assert_called_once_with("pet", "SyntheticPet", 8)

    def test_historical_reset_and_parser_suppression_do_not_replay(self):
        for reset_required, parser_suppressed in ((True, False), (False, True)):
            with self.subTest(reset=reset_required, parser_suppressed=parser_suppressed):
                app = app_fixture()
                app.incremental_parser.suppress_apply_events = parser_suppressed
                app.consume_pet_packets([property_packet(0, 8), state_packet(1, 7)], reset_required)
                app.incremental_parser.suppress_apply_events = False
                app.process_pet_alerts()
                self.assertEqual(list(app.pet_alert_events), [])
                app.play_alert_sound.assert_not_called()
                self.assertFalse(app.pet_data_status(app.pet_tracker.snapshot())[0])
                app.consume_pet_packets([state_packet(2, 6)], False)
                app.process_pet_alerts()
                app.play_alert_sound.assert_called_once_with("pet", "SyntheticPet", 6)

    def test_ordered_batch_rearms_but_only_latest_low_is_spoken(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8), state_packet(1, 20), state_packet(2, 7)], False)
        self.assertEqual(len(app.pet_alert_events), 1, "Recovery cancels the earlier queued low event")
        app.process_pet_alerts()
        app.play_alert_sound.assert_called_once_with("pet", "SyntheticPet", 7)
        app.consume_pet_packets([property_packet(3, 7)], False)
        app.process_pet_alerts()
        self.assertEqual(app.play_alert_sound.call_count, 1)

    def test_stop_discards_queued_audio_and_start_does_not_replay_it(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.monitoring_active.clear()
        app.process_pet_alerts()
        self.assertEqual(list(app.pet_alert_events), [])
        app.monitoring_active.set()
        app.process_pet_alerts()
        app.play_alert_sound.assert_not_called()

    def test_disabled_pet_monitoring_or_reminder_discards_queued_audio(self):
        for variable in ("pet_monitor_enabled_var", "pet_alert_enabled_var"):
            with self.subTest(variable=variable):
                app = app_fixture()
                app.consume_pet_packets([property_packet(0, 8)], False)
                getattr(app, variable).set(False)
                app.process_pet_alerts()
                getattr(app, variable).set(True)
                app.process_pet_alerts()
                self.assertEqual(list(app.pet_alert_events), [])
                app.play_alert_sound.assert_not_called()

    def test_stale_recording_and_old_queued_event_never_play(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.last_rrf_data_monotonic = time.monotonic() - 11
        app.process_pet_alerts()
        app.play_alert_sound.assert_not_called()
        self.assertFalse(app.pet_data_status(app.pet_tracker.snapshot())[0])
        event, _created = app.pet_alert_events[0]
        app.pet_alert_events[0] = (event, time.monotonic() - 6)
        app.last_rrf_data_monotonic = time.monotonic()
        app.process_pet_alerts()
        self.assertEqual(list(app.pet_alert_events), [])
        app.play_alert_sound.assert_not_called()

    def test_generic_recording_recovery_does_not_make_old_pet_value_live(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.process_pet_alerts()
        app.last_rrf_data_monotonic = time.monotonic() - 11
        app.invalidate_stale_pet_data()
        self.assertIsNone(app.pet_live_observation_key)
        app.last_rrf_data_monotonic = time.monotonic()
        ordinary_packet = mon.ReplayPacket(1, 100, 0x0106, b"\x06\x01")
        app.consume_pet_packets([ordinary_packet], False)
        self.assertFalse(app.pet_data_status(app.pet_tracker.snapshot())[0])
        app.process_pet_alerts()
        self.assertEqual(app.play_alert_sound.call_count, 1)
        app.consume_pet_packets([state_packet(2, 7)], False)
        self.assertTrue(app.pet_data_status(app.pet_tracker.snapshot())[0])
        app.process_pet_alerts()
        self.assertEqual(app.play_alert_sound.call_count, 1, "A continuing low episode must not re-alert")

    def test_background_consumption_stops_after_stop_rescan_or_pet_disable(self):
        for condition in ("stop", "rescan", "pet_disabled"):
            with self.subTest(condition=condition):
                app = app_fixture()
                if condition == "stop":
                    app.monitoring_active.clear()
                elif condition == "rescan":
                    app.monitor_reset_requested.set()
                else:
                    app.pet_monitor_enabled = False
                app.consume_pet_packets([property_packet(0, 8)], False)
                self.assertIsNone(app.pet_tracker.snapshot().pet_id)
                self.assertEqual(list(app.pet_alert_events), [])
                self.assertIsNone(app.pet_live_observation_key)

    def test_recovered_value_before_ui_tick_cancels_old_low(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.consume_pet_packets([state_packet(1, 20)], False)
        app.process_pet_alerts()
        app.play_alert_sound.assert_not_called()
        self.assertEqual(list(app.pet_alert_events), [])
        app.consume_pet_packets([state_packet(2, 9)], False)
        app.process_pet_alerts()
        app.play_alert_sound.assert_called_once_with("pet", "SyntheticPet", 9)

    def test_old_pet_generation_does_not_announce_current_pet(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.pet_tracker.reset()
        new = app.pet_tracker.consume([property_packet(0, 8, pet_id=1003, name="SyntheticNew")])[0]
        app.pet_live_observation_key = (new.identity_generation, new.satiety_revision)
        app.process_pet_alerts()
        self.assertEqual(list(app.pet_alert_events), [])
        app.play_alert_sound.assert_not_called()

    def test_threshold_edit_does_not_replay_a_queued_old_rule(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        app.pet_alert_threshold_var.set("9")
        app.pet_alert_threshold = 9
        app.process_pet_alerts()
        self.assertEqual(list(app.pet_alert_events), [])
        app.play_alert_sound.assert_not_called()

    def test_packet_update_cannot_overtake_an_already_validated_reminder(self):
        app = app_fixture()
        app.consume_pet_packets([property_packet(0, 8)], False)
        unlocked, updated = threading.Event(), threading.Event()
        ui_thread = threading.get_ident()
        errors, played = [], []

        class PauseAfterUnlock:
            def __init__(self):
                self.lock = threading.Lock()
                self.paused = False

            def __enter__(self):
                self.lock.acquire()

            def __exit__(self, *exception):
                self.lock.release()
                if threading.get_ident() == ui_thread and not self.paused:
                    self.paused = True
                    unlocked.set()
                    # A broader session lock may correctly block the
                    # updater until this UI operation finishes; do not deadlock.
                    updated.wait(0.5)

        def update_pet():
            try:
                if not unlocked.wait(2):
                    raise AssertionError("UI never reached its reminder decision")
                app.consume_pet_packets([property_packet(1, 50, pet_id=1003, name="SyntheticNew")], False)
            except BaseException as exc:
                errors.append(exc)
            finally:
                updated.set()

        def capture_sound(level, name, satiety):
            current = app.pet_tracker.snapshot()
            played.append((name, satiety, current.pet_name, current.satiety))

        app.pet_alert_lock = PauseAfterUnlock()
        app.play_alert_sound = capture_sound
        worker = threading.Thread(target=update_pet, daemon=True)
        worker.start()
        try:
            app.process_pet_alerts()
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        for name, satiety, current_name, current_satiety in played:
            self.assertEqual((name, satiety), (current_name, current_satiety),
                             "A new pet was accepted before an old reminder started playing")

    def test_pet_speech_uses_satiety_without_seconds(self):
        app = app_fixture()
        app.sound_volume_var = Value(100)
        app.sound_mode_var = Value("導航人聲")
        app.speak_text = mock.Mock()
        mon.RrfMonitorApp.play_alert_sound(app, "pet", "SyntheticPet", 8)
        spoken = app.speak_text.call_args.args[0]
        self.assertIn("飽食度剩下8", spoken)
        self.assertIn("請餵食", spoken)
        self.assertNotIn("秒", spoken)

    def test_zero_volume_mutes_pet_speech_and_all_existing_sound_modes(self):
        app = app_fixture()
        app.sound_volume_var = Value(0)
        app.speak_text = mock.Mock()
        with mock.patch.object(mon, "winsound") as sound:
            for mode in mon.SOUND_MODES:
                app.sound_mode_var = Value(mode)
                mon.RrfMonitorApp.play_alert_sound(app, "pet", "SyntheticPet", 8)
            app.speak_text.assert_not_called()
            sound.PlaySound.assert_not_called()
            sound.MessageBeep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
