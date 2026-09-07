"""Final core checks use synthetic packets and controlled thread interleavings."""
from collections import deque
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
DATE = (2026, 9, 7, 12, 0, 0)
SELF = 1001
MEMBER = 1002

def packet(index, data):
    return mon.ReplayPacket(index, index * 100, mon.u16(data, 0), bytes(data))

def self_packet(index=0, aid=SELF):
    return packet(index, struct.pack('<HI', 643, aid))

def status_packet(index, status_id=78):
    return packet(index, struct.pack('<HHIBII', 2435, status_id, SELF, 1, 60000, 60000))

def member_packet(index, aid=MEMBER, name=b'SyntheticMember'):
    data = bytearray(89)
    struct.pack_into('<HI', data, 0, 2788, aid)
    data[23:47] = b'SyntheticParty'.ljust(24, b'\x00')
    data[47:71] = name.ljust(24, b'\x00')
    return packet(index, data)

def roster_packet(index, aids):
    data = bytearray(28 + 54 * len(aids))
    struct.pack_into('<HH24s', data, 0, 2789, len(data), b'SyntheticParty')
    for offset, aid in enumerate(aids):
        struct.pack_into('<II24s', data, 28 + offset * 54, aid, aid + 10000, b'SyntheticMember')
    return packet(index, data)

def leave_packet(index, aid=MEMBER, result=0):
    return packet(index, struct.pack('<HI24sB', 261, aid, b'SyntheticMember', result))

def replay_header():
    data = bytearray(400)
    marker = b'<< Ragnarok Replay File Version'
    data[:len(marker)] = marker
    struct.pack_into('<HBB', data, 104, *DATE[:3])
    data[109:112] = bytes(DATE[3:])
    struct.pack_into('<Hii', data, 122, 1, 0, 400)
    return bytes(data)

def frame(index, status_id):
    value = status_packet(index, status_id)
    return struct.pack('<iiH', index, value.timeline_ms, len(value.data)) + mon.decrypt_packet(DATE, value.data)

class Value:

    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value

    def get(self):
        return self.value

def dummy_app():
    app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
    app.running = True
    app.monitoring_active = threading.Event()
    app.monitoring_active.set()
    app.monitor_reset_requested = threading.Event()
    app.monitor_wake_event = threading.Event()
    app.tracker = mon.StatusTracker()
    app.pet_tracker = mon.PetTracker()
    app.target_tracker = mon.TargetTracker()
    app.incremental_parser = mon.RrfIncrementalParser()
    app.core_monitoring_only = False
    app.pet_monitor_enabled = False
    app.last_resolved_self_identity = None
    app.alerted, app.recent_apply_alerts, app.recent_event_history = (set(), {}, deque())
    app.current_path = None
    app.last_rrf_packet_count = 0
    app.tree = None
    app.monitor_message = ''
    for name in ('monitor_summary_var', 'monitor_control_var', 'latest_event_var', 'status_table_count_var', 'self_target_id_var', 'self_target_name_var'):
        setattr(app, name, Value())
    app.self_target_name_var.set('')
    for name in ('clear_live_monitor_display', 'update_monitor_control_ui', 'sync_tracker_target_filter_snapshot', 'enqueue_ui_callback', 'log_event', 'clear_target_options', 'update_unknown_review_controls', 'update_onboarding', 'update_tab_labels', 'update_live_sync_estimate', 'update_main_table', 'refresh_target_options', 'update_source_controls'):
        setattr(app, name, lambda *args, **kwargs: None)
    app.set_message = lambda message: setattr(app, 'monitor_message', message)
    app.target_is_monitored_from_snapshot = lambda aid: True
    app.scheduled_while_stopped = []
    app.maybe_schedule_client_data_sync = lambda **kwargs: app.scheduled_while_stopped.append(not app.monitoring_is_active())
    app.saved_identities = []
    app.save_settings = lambda: app.saved_identities.append((app.self_target_id_var.get(), app.self_target_name_var.get()))
    app._clear_scoped_transient_data = lambda: None
    return app

class MonitorLifecycleTests(unittest.TestCase):

    def run_interleaving(self, app, operation, pending_work, pause_method):
        entered, resume, operation_started, operation_done = (threading.Event() for _ in range(4))
        errors = []
        original = getattr(app, pause_method[0]) if len(pause_method) == 1 else getattr(app.target_tracker, pause_method[1])

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not resume.wait(3):
                raise AssertionError('Controlled worker was not resumed')
            return result
        if len(pause_method) == 1:
            setattr(app, pause_method[0], paused)
        else:
            setattr(app.target_tracker, pause_method[1], paused)

        def worker():
            try:
                pending_work()
            except BaseException as exc:
                errors.append(exc)

        def change_lifecycle():
            operation_started.set()
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
            finally:
                operation_done.set()
        active = threading.Thread(target=worker)
        changing = threading.Thread(target=change_lifecycle)
        active.start()
        try:
            self.assertTrue(entered.wait(3))
            changing.start()
            self.assertTrue(operation_started.wait(3))
            self.assertFalse(operation_done.wait(0.05))
        finally:
            resume.set()
            active.join(3)
            if changing.ident is not None:
                changing.join(3)
        self.assertFalse(active.is_alive())
        self.assertFalse(changing.is_alive())
        self.assertEqual(errors, [])

    def test_stop_cannot_be_followed_by_old_batch_states_or_alerts(self):
        app = dummy_app()
        self.run_interleaving(app, app.stop_monitoring, lambda: app.process_packet_batch([self_packet(), status_packet(1)], False), ('target_tracker', 'consume'))
        self.assertFalse(app.monitoring_is_active())
        self.assertEqual(app.tracker.states, {})
        self.assertEqual(list(app.tracker.pending_apply_events), [])
        self.assertEqual(app.target_tracker.known_target_ids(), set())
        self.assertNotIn(True, app.scheduled_while_stopped)
        app.process_packet_batch([status_packet(2)], False)
        self.assertEqual(app.tracker.states, {})

    def test_rescan_clears_the_complete_old_batch_before_rebuild(self):
        app = dummy_app()
        self.run_interleaving(app, app.rescan, lambda: app.process_packet_batch([self_packet(), status_packet(1)], False), ('target_tracker', 'consume'))
        self.assertTrue(app.monitor_reset_requested.is_set())
        self.assertEqual(app.tracker.states, {})
        self.assertEqual(list(app.tracker.pending_apply_events), [])
        self.assertEqual(app.target_tracker.known_target_ids(), set())

    def test_read_health_publication_and_stop_share_the_lifecycle_lock(self):
        app = dummy_app()
        self.run_interleaving(app, app.stop_monitoring, lambda: app._publish_monitor_read(Path('synthetic.rrf'), (500, 1), DATE, 2, 2), ('update_live_sync_estimate',))
        self.assertEqual(app.monitor_message, '監控已停止')
        self.assertFalse(app._publish_monitor_read(Path('late.rrf'), (600, 2), DATE, 3, 1))
        self.assertEqual(app.last_rrf_packet_count, 2)
        self.assertEqual(app.monitor_message, '監控已停止')

    def test_queued_self_id_callback_cannot_restore_old_or_stopped_identity(self):
        app = dummy_app()
        app.target_tracker.consume([self_packet(), self_packet(1, 2001)])
        app.apply_packet_confirmed_self_id(SELF)
        self.assertEqual(app.saved_identities, [])
        app.apply_packet_confirmed_self_id(2001)
        self.assertEqual(app.self_target_id_var.get(), app.format_target_id(2001))
        self.assertEqual(len(app.saved_identities), 1)
        app.stop_monitoring()
        app.apply_packet_confirmed_self_id(2001)
        self.assertEqual(len(app.saved_identities), 1)
        self.assertEqual(app.monitor_message, '監控已停止')

    def test_queued_name_callback_must_match_current_name_and_live_identity(self):
        app = dummy_app()
        app.target_tracker.consume([self_packet(), packet(1, struct.pack('<HI24s', 2608, SELF, b'SyntheticOld')), packet(2, struct.pack('<HI24s', 2608, SELF, b'SyntheticNew'))])
        app.apply_resolved_self_name(SELF, 'SyntheticOld')
        self.assertEqual(app.saved_identities, [])
        app.apply_resolved_self_name(SELF, 'SyntheticNew')
        self.assertEqual(app.self_target_name_var.get(), 'SyntheticNew')
        self.assertEqual(len(app.saved_identities), 1)
        app.target_tracker.set_configured_self_id(SELF)
        app.stop_monitoring()
        app.apply_resolved_self_name(SELF, 'SyntheticNew')
        self.assertEqual(len(app.saved_identities), 1)
        self.assertEqual(app.monitor_message, '監控已停止')

class TargetRecordTests(unittest.TestCase):

    def test_valid_departures_revoke_members(self):
        for result in (0, 1):
            with self.subTest(result=result):
                tracker = mon.TargetTracker()
                tracker.consume([self_packet(), member_packet(1), member_packet(2, 1003)])
                tracker.consume([leave_packet(3, result=result)])
                self.assertNotIn(MEMBER, tracker.party_ids)
                self.assertIn(1003, tracker.party_ids)
                self.assertNotEqual(tracker.relation_for(MEMBER), '隊伍成員')
                tracker.consume([leave_packet(4, SELF, result)])
                self.assertEqual(tracker.party_ids, set())

    def test_failed_or_malformed_departures_and_hp_updates_keep_party(self):
        tracker = mon.TargetTracker()
        tracker.consume([self_packet(), member_packet(1)])
        tracker.consume([leave_packet(2, result=2), leave_packet(3, result=3), packet(4, leave_packet(4).data[:-1]), packet(5, struct.pack('<HIII', 262, MEMBER, 100, 200))])
        self.assertEqual(tracker.party_ids, {MEMBER})

    def test_self_id_only_packets_cannot_grow_the_last_seen_dictionary(self):
        tracker = mon.TargetTracker()
        count = mon.MAX_TRACKED_TARGETS + 100
        for start in range(0, count, 256):
            tracker.consume([self_packet(index, 10000 + index) for index in range(start, min(start + 256, count))])
            self.assertLessEqual(len(tracker.target_last_seen_ms), mon.MAX_TRACKED_TARGETS)
        self.assertIn(10000 + count - 1, tracker.target_last_seen_ms)

    def test_party_flood_is_bounded_and_departure_allows_a_new_member(self):
        tracker = mon.TargetTracker()
        tracker.consume([self_packet()])
        count = mon.MAX_TRACKED_TARGETS + 10
        for start in range(0, count, 256):
            tracker.consume([member_packet(index + 1, 10000 + index) for index in range(start, min(start + 256, count))])
        self.assertEqual(len(tracker.party_ids), tracker.MAX_PARTY_TARGETS)
        self.assertLessEqual(len(tracker.known_target_ids()), mon.MAX_TRACKED_TARGETS)
        for mapping in (tracker.target_names, tracker.target_name_sources, tracker.target_last_seen_ms):
            self.assertLessEqual(len(mapping), mon.MAX_TRACKED_TARGETS)
        tracker.consume([member_packet(count + 1, 10000, b'UpdatedMember')])
        self.assertEqual(tracker.target_name(10000), 'UpdatedMember')
        newcomer = 200000
        tracker.consume([leave_packet(count + 2, 10000), member_packet(count + 3, newcomer)])
        self.assertIn(newcomer, tracker.party_ids)
        self.assertNotIn(10000, tracker.party_ids)
        self.assertEqual(len(tracker.party_ids), tracker.MAX_PARTY_TARGETS)

    def test_complete_twelve_member_roster_survives_and_replaces_old_members(self):
        tracker = mon.TargetTracker()
        tracker.consume([self_packet(), member_packet(1, 9999)])
        ids = list(range(SELF, SELF + 12))
        tracker.consume([roster_packet(2, ids)])
        self.assertEqual(tracker.party_ids, set(ids))
        tracker.consume([roster_packet(3, ids[:6])])
        self.assertEqual(tracker.party_ids, set(ids[:6]))

class IncrementalRewriteTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='rrf-final-core-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'synthetic.rrf'
        self.parser = mon.RrfIncrementalParser()

    def read_ids(self):
        packets, _date, reset = self.parser.parse_incremental(self.path)
        return ([mon.u16(item.data, 2) for item in packets], reset)

    def test_larger_same_header_rewrite_rebuilds_all_packets(self):
        self.path.write_bytes(replay_header() + frame(0, 78))
        self.assertEqual(self.read_ids(), ([78], True))
        self.path.write_bytes(replay_header() + frame(0, 79) + frame(1, 80))
        self.assertEqual(self.read_ids(), ([79, 80], True))
        self.assertEqual(self.parser.packet_count, 2)

    def test_same_size_rewrite_is_also_detected(self):
        self.path.write_bytes(replay_header() + frame(0, 78))
        self.read_ids()
        self.path.write_bytes(replay_header() + frame(0, 79))
        self.assertEqual(self.read_ids(), ([79], True))

    def test_changed_cursor_neighborhood_is_detected_when_start_is_identical(self):
        original = [frame(index, 78) for index in range(100)]
        self.path.write_bytes(replay_header() + b''.join(original))
        self.read_ids()
        rewritten = original[:-1] + [frame(99, 79), frame(100, 80)]
        self.path.write_bytes(replay_header() + b''.join(rewritten))
        ids, reset = self.read_ids()
        self.assertTrue(reset)
        self.assertEqual(ids, [78] * 99 + [79, 80])

    def test_append_and_partial_packet_completion_never_reset(self):
        first = frame(0, 78)
        self.path.write_bytes(replay_header() + first[:7])
        self.assertEqual(self.read_ids(), ([], True))
        with self.path.open('ab') as handle:
            handle.write(first[7:])
        self.assertEqual(self.read_ids(), ([78], False))
        for index in range(1, 45):
            encoded = frame(index, 78 + index)
            with self.path.open('ab') as handle:
                handle.write(encoded[:7])
            self.assertEqual(self.read_ids(), ([], False))
            with self.path.open('ab') as handle:
                handle.write(encoded[7:])
            self.assertEqual(self.read_ids(), ([78 + index], False))
            self.assertEqual(self.read_ids(), ([], False))
            self.assertLessEqual(sum((len(data) for _offset, data in self.parser.session.stream_probes)), self.parser.STREAM_PROBE_BYTES * 2)
if __name__ == '__main__':
    unittest.main(verbosity=2)
