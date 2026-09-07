"""Synthetic own-pet metadata regression checks; no game files or GUI required."""
from collections import deque
from dataclasses import replace
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
from monitor_core.pet_alerts import PetAlertController
from monitor_core.pet_snapshot import ReplayPetSnapshot, decode_pet_snapshot

DATE = (2026, 9, 8, 12, 0, 0)
OWNER, OTHER, GID, NEXT_GID = 1001, 1002, 900001, 900002
IDENTITY_OFFSET, PET_OFFSET = 4500, 5000


def chunks(*, gid=GID, pet_id=1002, name="SyntheticPet", satiety=7, intimacy=986):
    values = {5301: gid, 5305: pet_id, 5306: 1, 5307: satiety, 5308: intimacy}
    return [(5303, name.encode("cp950").ljust(32, b"\0"))] + [
        (key, struct.pack("<I", value)) for key, value in values.items()]


def snapshot(**options):
    return decode_pet_snapshot(chunks(**options), OWNER)


def encode_chunks(values):
    plain = b"".join(struct.pack("<Hi", key, len(value)) + value for key, value in values)
    return mon.decrypt_packet(DATE, plain)


def packet(index, header, payload, ms=None):
    return mon.ReplayPacket(index, index * 100 if ms is None else ms, header, payload)


def state(index, value, gid=GID, state_type=2, ms=None):
    return packet(index, 0x01A4, struct.pack("<HBII", 0x01A4, state_type, gid, value), ms)


def property_packet(index, value, pet_id=1002, name="SyntheticPet", ms=None):
    return packet(index, 0x01A2, struct.pack("<H24sBHHHHH", 0x01A2,
                  name.encode("cp950").ljust(24, b"\0"), 0, 1, value, 500, 0, pet_id), ms)


def frame(value):
    return struct.pack("<iiH", value.index, value.timeline_ms, len(value.data)) + mon.decrypt_packet(DATE, value.data)


def replay(values=None, packets=(), owner=OWNER, incomplete_pet=False, incomplete_owner=False):
    identity = encode_chunks([(1010, struct.pack("<I", owner))])
    pet = encode_chunks(chunks() if values is None else values)
    blob = bytearray(PET_OFFSET + len(pet))
    marker = b"<< Ragnarok Replay File Version"
    blob[:len(marker)] = marker
    struct.pack_into("<HBB", blob, 104, *DATE[:3])
    blob[109:112] = bytes(DATE[3:])
    for slot, (kind, size, offset) in enumerate(((1, 0, len(blob)), (3, len(identity), IDENTITY_OFFSET), (9, len(pet), PET_OFFSET))):
        struct.pack_into("<Hii", blob, 112 + slot * 10, kind, size, offset)
    if not incomplete_owner:
        blob[IDENTITY_OFFSET:IDENTITY_OFFSET + len(identity)] = identity
    if incomplete_pet:
        blob[PET_OFFSET:PET_OFFSET + len(pet)//2] = pet[:len(pet)//2]
    else:
        blob[PET_OFFSET:] = pet
    blob.extend(b"".join(frame(p) for p in packets))
    return bytes(blob)


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


def app_fixture():
    app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
    app.running = True
    app.monitoring_has_started = True
    app.monitoring_active = threading.Event()
    app.monitoring_active.set()
    app.monitor_reset_requested = threading.Event()
    app.tracker = mon.StatusTracker()
    app.target_tracker = mon.TargetTracker()
    app.pet_tracker = mon.PetTracker()
    app.incremental_parser = mon.RrfIncrementalParser()
    app.pet_alert_controller = PetAlertController()
    app.pet_alert_events = deque()
    app.pet_alert_lock = threading.Lock()
    app.config_lock = threading.Lock()
    app.pet_live_observation_key = None
    app.pet_monitor_enabled = app.pet_alert_enabled = True
    app.pet_alert_threshold = 25
    app.pet_monitor_enabled_var = Value(True)
    app.pet_alert_enabled_var = Value(True)
    app.pet_alert_threshold_var = Value("25")
    app.current_path = Path("synthetic.rrf")
    app.last_rrf_data_monotonic = time.monotonic()
    app.core_monitoring_only = True
    app.last_resolved_self_identity = None
    app.alerted, app.recent_apply_alerts = set(), {}
    app._clear_scoped_transient_data = lambda: None
    app.enqueue_ui_callback = lambda *args, **kwargs: None
    app.maybe_schedule_client_data_sync = lambda **kwargs: None
    app.log_event = lambda *args: None
    app.play_alert_sound = mock.Mock()
    return app


class SnapshotDecodeTests(unittest.TestCase):
    def test_valid_chinese_name_and_exact_little_endian_widths(self):
        result = snapshot(name="測試寵物")
        self.assertEqual((result.owner_id, result.pet_gid, result.pet_id, result.pet_name,
                          result.level, result.satiety, result.intimacy),
                         (OWNER, GID, 1002, "測試寵物", 1, 7, 986))

    def test_missing_duplicate_wrong_width_and_invalid_values_reject_whole_snapshot(self):
        cases = [chunks()[:-1], chunks() + [chunks()[0]]]
        for key in (5301, 5303, 5305, 5306, 5307, 5308):
            cases.append([(k, b[:-1] if k == key else b) for k, b in chunks()])
        for options in ({"gid": 0}, {"gid": 0xFFFFFFFF}, {"pet_id": 0}, {"pet_id": 0xFFFFFFFF},
                        {"name": ""}, {"satiety": 101}, {"intimacy": 1001}):
            cases.append(chunks(**options))
        for case in cases:
            with self.subTest(case=cases.index(case)):
                self.assertIsNone(decode_pet_snapshot(case, OWNER))
        for owner in (None, 0, 0xFFFFFFFF, True):
            self.assertIsNone(decode_pet_snapshot(chunks(), owner))

    def test_no_pet_sentinel_cannot_become_a_full_pet_from_dummy_values(self):
        self.assertIsNone(snapshot(gid=0xFFFFFFFF, pet_id=0xFFFFFFFF, name="", satiety=100, intimacy=100))


class SnapshotTrackerTests(unittest.TestCase):
    def test_seed_is_historical_and_only_matching_gid_can_update(self):
        tracker = mon.PetTracker()
        self.assertTrue(tracker.seed_replay_snapshot(snapshot()))
        seed = tracker.snapshot()
        self.assertEqual((seed.pet_gid, seed.satiety, seed.satiety_revision), (GID, 7, 0))
        controller = PetAlertController()
        self.assertIsNone(controller.evaluate(seed, enabled=True, live=True, valid=True))
        self.assertEqual(tracker.consume([state(0, 99, NEXT_GID), state(1, 999, NEXT_GID, 1)]), [])
        values = tracker.consume([state(2, 6)])
        self.assertEqual([value.satiety for value in values], [6])
        self.assertIsNotNone(controller.evaluate(values[0], enabled=True, live=True, valid=True))
        self.assertEqual(tracker.snapshot().intimacy, 986)

    def test_seed_cannot_replace_live_property_or_previous_seed(self):
        tracker = mon.PetTracker()
        tracker.consume([property_packet(0, 80)])
        self.assertFalse(tracker.seed_replay_snapshot(snapshot()))
        self.assertEqual(tracker.snapshot().satiety, 80)
        tracker.reset()
        self.assertTrue(tracker.seed_replay_snapshot(snapshot()))
        self.assertFalse(tracker.seed_replay_snapshot(snapshot(satiety=100)))
        self.assertFalse(mon.PetTracker().seed_replay_snapshot(replace(snapshot(), pet_gid=0xFFFFFFFF)))

    def test_type_zero_needs_adjacent_property_and_same_pet_map_resend_preserves_episode(self):
        tracker, controller = mon.PetTracker(), PetAlertController()
        tracker.seed_replay_snapshot(snapshot())
        first = tracker.consume([state(0, 6)])[0]
        self.assertIsNotNone(controller.evaluate(first, enabled=True, live=True, valid=True, now=100))
        generation = first.identity_generation
        self.assertEqual(tracker.consume([state(1, 0, NEXT_GID, 0)]), [])
        self.assertEqual(tracker.consume([state(2, 5, NEXT_GID)]), [])
        values = tracker.consume([state(3, 0, NEXT_GID, 0, ms=500), property_packet(4, 6, ms=500), state(5, 5, NEXT_GID)])
        self.assertEqual([s.pet_gid for s in values], [NEXT_GID, NEXT_GID])
        self.assertTrue(all(s.identity_generation == generation for s in values))
        self.assertTrue(all(controller.evaluate(s, enabled=True, live=True, valid=True, now=101) is None for s in values))
        self.assertEqual(tracker.consume([state(6, 0, GID)]), [])

    def test_new_named_pet_rejects_previous_gid_even_without_preinit(self):
        tracker = mon.PetTracker()
        tracker.seed_replay_snapshot(snapshot())
        old_generation = tracker.snapshot().identity_generation
        tracker.consume([property_packet(0, 75, pet_id=1003, name="NewPet"), state(1, 2)])
        self.assertEqual(tracker.snapshot().satiety, 75)
        self.assertGreater(tracker.snapshot().identity_generation, old_generation)
        tracker.consume([state(2, 74, NEXT_GID)])
        self.assertEqual((tracker.snapshot().pet_gid, tracker.snapshot().satiety), (NEXT_GID, 74))

    def test_changed_owner_and_explicit_reset_clear_snapshot_and_gid(self):
        tracker = mon.PetTracker()
        tracker.seed_replay_snapshot(snapshot())
        owner_packet = packet(0, 0x0283, struct.pack("<HI", 0x0283, OTHER))
        self.assertEqual(tracker.consume([owner_packet, state(1, 1)]), [])
        self.assertIsNone(tracker.snapshot().pet_id)
        tracker.reset()
        self.assertTrue(tracker.seed_replay_snapshot(snapshot(gid=NEXT_GID, satiety=80)))
        self.assertEqual(tracker.consume([state(0, 1)]), [])
        self.assertEqual(tracker.snapshot().satiety, 80)


class SnapshotAppTests(unittest.TestCase):
    def parse(self, app, path):
        app.current_path = path
        result = app.incremental_parser.parse_incremental_batches(path, app.process_packet_batch)
        app.last_rrf_data_monotonic = time.monotonic()
        app.process_pet_alerts()
        return result

    def test_empty_stream_displays_snapshot_without_alert_and_new_low_can_alert(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.rrf"
            path.write_bytes(replay())
            app = app_fixture()
            self.parse(app, path)
            self.assertEqual((app.pet_tracker.snapshot().satiety, app.pet_tracker.snapshot().pet_gid), (7, GID))
            self.assertEqual(app.incremental_parser.packet_count, 0)
            self.assertFalse(app.pet_data_status(app.pet_tracker.snapshot())[0])
            app.play_alert_sound.assert_not_called()
            with path.open("ab") as stream:
                stream.write(frame(state(0, 6)))
            self.parse(app, path)
            self.assertEqual(app.pet_tracker.snapshot().satiety, 6)
            app.play_alert_sound.assert_called_once()

    def test_history_and_replay_do_not_alert_and_switch_to_no_pet_clears_all(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "one.rrf", Path(folder) / "two.rrf"
            first.write_bytes(replay(packets=[state(0, 6), state(1, 5)]))
            second.write_bytes(replay(chunks(gid=0xFFFFFFFF, pet_id=0xFFFFFFFF, name="", satiety=100, intimacy=100), packets=[state(0, 100)]))
            app = app_fixture()
            self.parse(app, first)
            self.assertEqual(app.pet_tracker.snapshot().satiety, 5)
            app.incremental_parser.reset()
            self.parse(app, first)
            app.play_alert_sound.assert_not_called()
            self.parse(app, second)
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            self.assertEqual(list(app.pet_alert_events), [])
            self.assertIsNone(app.pet_live_observation_key)

    def test_late_metadata_rebuilds_history_once_without_overwriting_latest_value(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "late.rrf"
            path.write_bytes(replay(packets=[state(0, 87)], incomplete_pet=True))
            app = app_fixture()
            self.parse(app, path)
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            with path.open("r+b") as stream:
                stream.seek(PET_OFFSET)
                stream.write(encode_chunks(chunks()))
            self.assertTrue(self.parse(app, path)[1])
            self.assertEqual(app.pet_tracker.snapshot().satiety, 87)
            generation = app.pet_tracker.snapshot().identity_generation
            self.assertFalse(self.parse(app, path)[1])
            self.assertEqual(app.pet_tracker.snapshot().identity_generation, generation)
            app.play_alert_sound.assert_not_called()

    def test_late_identity_allows_metadata_without_ever_guessing_configured_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "late-owner.rrf"
            path.write_bytes(replay(packets=[state(0, 6)], incomplete_owner=True))
            app = app_fixture()
            self.parse(app, path)
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            with path.open("r+b") as stream:
                stream.seek(IDENTITY_OFFSET)
                stream.write(encode_chunks([(1010, struct.pack("<I", OWNER))]))
            self.assertTrue(self.parse(app, path)[1])
            self.assertEqual(app.pet_tracker.snapshot().satiety, 6)
            self.assertFalse(self.parse(app, path)[1])
            app.play_alert_sound.assert_not_called()

    def test_half_packet_does_not_remove_a_good_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "half-packet.rrf"
            encoded = frame(state(0, 6))
            path.write_bytes(replay() + encoded[:9])
            app = app_fixture()
            self.parse(app, path)
            generation = app.pet_tracker.snapshot().identity_generation
            with path.open("ab") as stream:
                stream.write(encoded[9:])
            self.assertFalse(self.parse(app, path)[1])
            self.assertEqual(app.pet_tracker.snapshot().identity_generation, generation)
            self.assertEqual(app.pet_tracker.snapshot().satiety, 6)
            app.play_alert_sound.assert_called_once()

    def test_reenabling_pet_monitor_requests_history_rebuild_not_old_seed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "toggle.rrf"
            path.write_bytes(replay(packets=[state(0, 87)]))
            app = app_fixture()
            app.pet_monitor_enabled = False
            self.parse(app, path)
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            for name in ("save_settings", "refresh_pet_panel", "refresh_pet_overlay", "update_tab_labels"):
                setattr(app, name, mock.Mock())
            app.rescan = mock.Mock(side_effect=app.incremental_parser.reset)
            app.on_pet_settings_changed()
            app.rescan.assert_called_once()
            self.parse(app, path)
            self.assertEqual(app.pet_tracker.snapshot().satiety, 87)
            app.play_alert_sound.assert_not_called()
            app.pet_monitor_enabled_var.value = False
            app.on_pet_settings_changed()
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            app.pet_monitor_enabled_var.value = True
            app.on_pet_settings_changed()
            self.parse(app, path)
            self.assertEqual(app.pet_tracker.snapshot().satiety, 87)
            self.assertEqual(app.rescan.call_count, 2)
            app.play_alert_sound.assert_not_called()

    def test_owner_change_at_end_of_live_batch_cancels_earlier_pet_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "owner-switch.rrf"
            path.write_bytes(replay())
            app = app_fixture()
            self.parse(app, path)
            app.process_packet_batch([state(0, 6), packet(1, 0x0283, struct.pack("<HI", 0x0283, OTHER))], False)
            app.process_pet_alerts()
            self.assertIsNone(app.pet_tracker.snapshot().pet_id)
            self.assertIsNone(app.pet_live_observation_key)
            self.assertEqual(list(app.pet_alert_events), [])
            app.play_alert_sound.assert_not_called()

    def test_seed_refuses_mismatching_owner_and_disabled_or_stopped_monitor(self):
        for condition in ("different-owner", "disabled", "stopped"):
            with self.subTest(condition=condition):
                app = app_fixture()
                app.target_tracker.set_replay_identity(OTHER if condition == "different-owner" else OWNER, "SyntheticOwner")
                if condition == "disabled":
                    app.pet_monitor_enabled = False
                if condition == "stopped":
                    app.monitoring_active.clear()
                app.seed_replay_pet_snapshot(snapshot())
                self.assertIsNone(app.pet_tracker.snapshot().pet_id)


if __name__ == "__main__":
    unittest.main()
