"""Synthetic self/party trust-boundary checks.

Only generated packets and temporary settings are used.
Run this file directly to verify the source beside it."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
SELF, PARTY, NEW_PARTY, OUTSIDE, PET = (1001, 1002, 1003, 9001, 2001)

def run_source_checks(source: Path) -> dict:
    sys.path.insert(0, str(source))
    from monitor_core import capabilities
    from monitor_core.policies import PolicyResolver, ScopePolicy, migrate_settings_to_v2
    from monitor_core.alert_policies import AlertPolicyResolver
    from monitor_core.session import MonitorSession, ObservationStore
    from monitor_core.snapshots import StatusObservation
    import rrf_monitor as mon

    def packet(index, header, data, time=None):
        return mon.ReplayPacket(index, 1000 + index * 100 if time is None else time, header, bytes(data))

    def self_packet(index=0, aid=SELF):
        return packet(index, 643, struct.pack('<HI', 643, aid))

    def roster(index, aids, *, name=b'SyntheticParty'):
        data = bytearray(28 + 54 * len(aids))
        struct.pack_into('<HH24s', data, 0, 2789, len(data), name)
        for n, aid in enumerate(aids):
            off = 28 + n * 54
            struct.pack_into('<II24s16sBBHH', data, off, aid, aid + 10000, b'SyntheticMember', b'test_map', 0, 0, 1, 100)
        return packet(index, 2789, data)

    def member(index, aid, *, name=b'SyntheticMember', party_name=b'SyntheticParty'):
        data = bytearray(89)
        struct.pack_into('<HIIIHHHHB', data, 0, 2788, aid, aid + 10000, 0, 1, 100, 10, 10, 0)
        data[23:47] = party_name.ljust(24, b'\x00')
        data[47:71] = name.ljust(24, b'\x00')
        data[71:87] = b'test_map'.ljust(16, b'\x00')
        return packet(index, 2788, data)

    def leave(index, aid, result=0):
        return packet(index, 261, struct.pack('<HI24sB', 261, aid, b'SyntheticMember', result))

    def status(index, target, sid=78, *, remaining=60000):
        return packet(index, 2435, struct.pack('<HHIBIIiii', 2435, sid, target, 1, remaining, remaining, 1, 0, 0))

    def unit(index, target, name=b'SyntheticMember'):
        data = bytearray(108)
        struct.pack_into('<HHBII', data, 0, 2559, len(data), 0, target, target + 10000)
        struct.pack_into('<H', data, 23, 1)
        data[84:108] = name.ljust(24, b'\x00')
        return packet(index, 2559, data)

    def observation(target, sid=78):
        return StatusObservation(target, sid, True, 60000, 60000, 1000, 2435)

    def all_resolver():
        return PolicyResolver({scope: ScopePolicy('auto') for scope in ('self', 'party', 'screen', 'other')}, name_overrides={'SyntheticMember': ScopePolicy('auto')}, session_id_overrides={OUTSIDE: ScopePolicy('auto')})

    def trusted_tracker():
        tracker = mon.TargetTracker()
        tracker.consume([self_packet(), roster(1, [SELF, PARTY])])
        return tracker

    def app_fixture():
        app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
        app.running = True
        app.core_monitoring_only = False
        app.pet_monitor_enabled = True
        app.monitoring_should_continue = lambda: True
        app.tracker = mon.StatusTracker()
        app.pet_tracker = mon.PetTracker()
        app.target_tracker = mon.TargetTracker()
        app.incremental_parser = mon.RrfIncrementalParser()
        app.incremental_parser.session.identity_snapshot = mon.ReplayIdentitySnapshot(SELF, 'SyntheticSelf')
        app.monitor_session = MonitorSession(all_resolver())
        app.settings = {}
        app.alerted = set()
        app.recent_apply_alerts = {}
        app.last_apply_alert_at = 0.0
        app.last_resolved_self_identity = None
        app.pending_unknown_status_ids = set()
        app.pending_client_item_ids = set()
        app.background_seen_status_ids = set()
        app.background_seen_item_ids = set()
        app.data_load_lock = threading.RLock()
        app.config_lock = threading.RLock()
        app.unknown_last_log_at = 0.0
        app.unknown_journal = None
        app.unknown_resolve_thread = None
        app.unknown_resolve_cancel_event = threading.Event()
        app.data_load_thread = None
        app.auto_resolve_unknown = False
        app.unknown_controls_update_queued = False
        app.target_policy_resolver_snapshot = all_resolver()
        app.current_policy_resolver = all_resolver
        app.target_scope_display_modes_snapshot = {scope: mon.TARGET_DISPLAY_MODE_ALL for scope in mon.TARGET_SCOPE_ORDER}
        app.target_display_mode_overrides = {OUTSIDE: mon.TARGET_DISPLAY_MODE_ALL}
        app.target_status_overrides = {OUTSIDE: {78}}
        app.target_display_mode_overrides_snapshot = dict(app.target_display_mode_overrides)
        app.logs = []
        app.callbacks = []
        app.played = []
        app.log_event = app.logs.append
        app.enqueue_ui_callback = lambda callback, critical=False: app.callbacks.append(callback) or True
        app.queue_unknown_review_controls = lambda: None
        app.schedule_unknown_resolution = lambda *args, **kwargs: None
        app.current_alert_policy_resolver = lambda: AlertPolicyResolver(scope_enabled={s: True for s in ('self', 'party', 'screen', 'other')}, status_rules={78: {'apply': True, 'yellow': True, 'red': True}}, target_overrides={'SyntheticMember': {'enabled': True, 'status_rules': {'78': {'apply': True, 'yellow': True, 'red': True}}}})
        app.default_status_alert_rule = lambda sid: {'apply': True, 'yellow': True, 'red': True}
        app.spoken_target_name = lambda aid: f'Synthetic-{aid}'
        app.play_alert_sound = lambda *args, **kwargs: app.played.append((args, kwargs))
        app.status_option_generation = 0
        app.readable_status_generation = -1
        app.readable_status_ids_cache = frozenset()
        return app

    def state_facts(app):
        return [(key, s.active, s.total_ms, s.remaining_ms, s.event_timeline_ms, s.source_header) for key, s in sorted(app.tracker.states.items())]

    class Value:
        """Small display-variable stand-in; no Tcl interpreter is required."""

        def __init__(self, value=''):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class ObservedRLock:
        """Real recursive lock exposing when the competing thread tries to enter."""

        def __init__(self):
            self.lock = threading.RLock()
            self.owner = threading.get_ident()
            self.contending = threading.Event()

        def __enter__(self):
            if threading.get_ident() != self.owner:
                self.contending.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_exc):
            self.lock.release()

    class Contract(unittest.TestCase):

        def test_fixed_source_and_local_filenames(self):
            self.assertEqual(Path(mon.__file__).resolve(), (source / 'rrf_monitor.py').resolve())
            self.assertEqual(capabilities.ALLOWED_TARGET_SCOPES, frozenset({'self', 'party'}))
            self.assertEqual(capabilities.local_data_filename('settings.json'), 'settings.json')

        def test_policy_scope_gate_precedes_name_and_id_overrides(self):
            resolver = all_resolver()
            for scope in ('self', 'party', 'screen', 'other', 'unknown'):
                allowed = scope in ('self', 'party')
                self.assertEqual(resolver.resolve(target_id=OUTSIDE, relation=scope, target_name='SyntheticMember').displays(78), allowed)

        def test_apply_yellow_red_scope_gate_precedes_overrides(self):
            resolver = app_fixture().current_alert_policy_resolver()
            for phase in ('apply', 'yellow', 'red'):
                for relation in ('self', 'party', 'screen', 'other'):
                    self.assertEqual(resolver.resolve(rule_key=phase, status_id=78, relation=relation, target_name='SyntheticMember', default_enabled=True).enabled, relation in ('self', 'party'))

        def test_settings_import_cannot_add_untrusted_scope(self):
            source_settings = {'settings_schema_version': 2, 'scope_policies': {s: {'mode': 'auto'} for s in ('self', 'party', 'screen', 'other')}, 'target_overrides': {'SyntheticMember': {'mode': 'auto'}, 'self': {'mode': 'auto'}}, 'self_target_id': OUTSIDE, 'self_target_name': 'SyntheticMember'}
            migrated, _ = migrate_settings_to_v2(source_settings)
            resolver = PolicyResolver.from_settings(migrated)
            self.assertEqual(resolver.resolve(target_id=OUTSIDE, relation='other', target_name='SyntheticMember').mode, 'off')
            self.assertEqual(resolver.resolve(target_id=PARTY, relation='party', target_name='SyntheticMember').mode, 'auto')

        def test_import_and_saved_settings_remove_unavailable_alert_scope_rules(self):
            scopes = ('self', 'party', 'screen', 'other', '自己', '隊伍成員', '畫面成員', '其他目標')
            rules = {scope: {'78': {'apply': index % 2 == 0, 'red': False}} for index, scope in enumerate(scopes)}
            alert_settings = {'scope_enabled': {scope: True for scope in scopes}, 'scope_status_rules': rules, 'scope_rules': rules, 'status_rules': {'78': {'yellow': False}}}
            source_settings = {'settings_schema_version': 2, 'scope_policies': {scope: {'mode': 'auto'} for scope in ('self', 'party', 'screen', 'other')}, 'alert_policies': alert_settings}
            original = json.dumps(source_settings, ensure_ascii=False, sort_keys=True)
            expected_scopes = {'self', 'party', '自己', '隊伍成員'}

            def check_payload(payload):
                alerts = payload['alert_policies']
                for key in ('scope_enabled', 'scope_status_rules', 'scope_rules'):
                    self.assertEqual(set(alerts[key]), expected_scopes, key)
                    self.assertEqual(alerts[key], {scope: value for scope, value in alert_settings[key].items() if scope in expected_scopes}, key)
                self.assertEqual(alerts['status_rules'], {'78': {'yellow': False}})
            with tempfile.TemporaryDirectory(prefix='synthetic-scope-settings-') as directory:
                old_path = Path(directory) / 'synthetic-old-config.json'
                imported_path = Path(directory) / 'config.json'
                old_path.write_text(original, encoding='utf-8')
                imported, _report, _backup = mon.import_settings_to_config(old_path, imported_path)
                check_payload(imported)
                check_payload(json.loads(imported_path.read_text(encoding='utf-8')))
                saved = mon.scoped_settings(source_settings)
                saved_path = Path(directory) / 'saved.json'
                mon.write_catalog_json_atomically(saved_path, saved, backup=False)
                check_payload(json.loads(saved_path.read_text(encoding='utf-8')))
            self.assertEqual(json.dumps(source_settings, ensure_ascii=False, sort_keys=True), original)

        def test_configured_id_and_same_name_are_not_scoped_identity(self):
            tracker = mon.TargetTracker()
            tracker.set_configured_self_id(OUTSIDE)
            tracker.set_configured_self_name('SyntheticMember')
            tracker.consume([unit(0, OUTSIDE)])
            self.assertEqual(tracker.self_id, None)
            tracker.set_replay_identity(SELF, 'SyntheticSelf')
            self.assertEqual(tracker.self_id, SELF)
            tracker.reset()
            self.assertEqual(tracker.self_id, None)

        def test_party_identity_comes_from_complete_roster(self):
            tracker = trusted_tracker()
            self.assertEqual(tracker.relation_for(SELF), '自己')
            self.assertEqual(tracker.relation_for(PARTY), '隊伍成員')
            tracker.consume([unit(2, OUTSIDE)])
            self.assertNotEqual(tracker.relation_for(OUTSIDE), '隊伍成員')
            self.assertNotIn(OUTSIDE, tracker.known_target_ids())

        def test_truncated_member_packet_cannot_add_party(self):
            tracker = trusted_tracker()
            truncated = member(2, OUTSIDE).data[:10]
            tracker.consume([packet(2, 2788, truncated)])
            self.assertNotIn(OUTSIDE, tracker.party_ids)

        def test_corrupt_complete_roster_preserves_previous_members(self):
            for bad in (roster(2, [SELF]).data[:-1], roster(2, [SELF, SELF]).data, roster(2, [0]).data):
                tracker = trusted_tracker()
                tracker.consume([packet(2, 2789, bad)])
                self.assertIn(PARTY, tracker.party_ids)

        def test_leave_failure_and_hp_update_do_not_revoke(self):
            tracker = trusted_tracker()
            tracker.consume([leave(2, PARTY, 2), leave(3, PARTY, 3), packet(4, 262, struct.pack('<HIHH', 262, PARTY, 100, 100))])
            self.assertIn(PARTY, tracker.party_ids)

        def test_member_leave_then_self_leave_clear_correct_members(self):
            tracker = trusted_tracker()
            tracker.consume([leave(2, PARTY)])
            self.assertNotIn(PARTY, tracker.party_ids)
            tracker.consume([roster(3, [SELF, NEW_PARTY]), leave(4, SELF, 1)])
            self.assertEqual(tracker.party_ids, set())

        def test_foreign_member_packet_cannot_replace_current_party(self):
            tracker = trusted_tracker()
            before = tracker.scoped_identity_signature()
            tracker.consume([member(2, OUTSIDE, party_name=b'SyntheticOtherParty')])
            self.assertEqual(tracker.scoped_identity_signature(), before)
            self.assertNotIn(OUTSIDE, tracker.trusted_target_ids())
            tracker.consume([member(3, SELF, party_name=b'SyntheticOtherParty')])
            self.assertNotIn(PARTY, tracker.trusted_target_ids())
            tracker.consume([member(4, NEW_PARTY, party_name=b'SyntheticOtherParty')])
            self.assertIn(NEW_PARTY, tracker.trusted_target_ids())

        def test_same_members_new_party_revokes_old_party_status_but_preserves_self(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, SELF, 78), status(2, PARTY, 78)], False)
            before = app.target_tracker.scoped_identity_signature()
            app.process_packet_batch([roster(3, [SELF, PARTY], name=b'SyntheticOtherParty')], False)
            self.assertNotEqual(app.target_tracker.scoped_identity_signature(), before)
            self.assertIn((78, SELF), app.tracker.states)
            self.assertNotIn((78, PARTY), app.tracker.states)
            app.process_packet_batch([status(4, PARTY, 78)], False)
            self.assertIn((78, PARTY), app.tracker.states)

        def test_unknown_quarantine_cannot_be_promoted_or_retained(self):
            store = ObservationStore()
            store.observe(observation(OUTSIDE), identity_confirmed=False)
            self.assertEqual(store.quarantine_count, 0)
            self.assertEqual(store.promote_target(OUTSIDE), 0)
            store.observe(observation(OUTSIDE), identity_confirmed=True)
            self.assertEqual(store.observations(), ())

        def test_session_retention_and_scope_changes(self):
            session = MonitorSession(all_resolver())
            session.set_trusted_targets(self_id=SELF, party_ids={PARTY}, own_pet_ids={PET})
            session.replace_observations([observation(aid) for aid in (SELF, PARTY, OUTSIDE, PET)], identity_confirmed=lambda aid: True)
            self.assertEqual({o.target_id for o in session.store.observations()}, {SELF, PARTY, PET})
            session.set_trusted_targets(self_id=SELF, party_ids={NEW_PARTY}, own_pet_ids={PET})
            self.assertNotIn(PARTY, {o.target_id for o in session.store.observations()})
            session.reset()
            self.assertEqual(session.store.observations(), ())

        def test_batch_does_not_retroactively_authorize_prejoin_status(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([status(0, PARTY, 65001), roster(1, [SELF, PARTY])], False)
            self.assertNotIn((65001, PARTY), app.tracker.states)
            self.assertNotIn(65001, app.pending_unknown_status_ids)
            app.process_packet_batch([status(2, PARTY, 78)], False)
            self.assertIn((78, PARTY), app.tracker.states)

        def test_status_burst_syncs_once_per_batch_and_keeps_latest_state(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY])], False)
            packets = [status(index, SELF if index % 2 else PARTY, 78 if index % 2 else 79, remaining=30000 + index) for index in range(1, 2001)]
            with mock.patch.object(app, 'sync_tracker_target_filter_snapshot', wraps=app.sync_tracker_target_filter_snapshot) as sync:
                app.process_packet_batch(packets, False)
                self.assertEqual(sync.call_count, 1)
            self.assertEqual(set(app.tracker.states), {(78, SELF), (79, PARTY)})
            self.assertEqual(app.tracker.states[78, SELF].remaining_ms, 31899)
            self.assertEqual(app.tracker.states[79, PARTY].remaining_ms, 32000)
            self.assertEqual(app.tracker.allowed_target_ids, {SELF, PARTY})

        def test_twelve_member_status_batch_matches_single_packet_reference(self):
            batched = app_fixture()
            reference = app_fixture()
            aids = list(range(SELF, SELF + 12))
            ids = (78, 10, 9, 20, 21, 32)
            seed = [packet(1 + n * 6 + i, 2435, struct.pack('<HHIBII', 2435, sid, aid, 1, 120000, 120000), time=14000) for n, aid in enumerate(aids) for i, sid in enumerate(ids)]
            for app in (batched, reference):
                app.process_packet_batch([], True)
                app.process_packet_batch([roster(0, aids)] + seed, False)
                app.tracker.pop_apply_events()
            packets = [packet(73 + i, 2435, struct.pack('<HHIBII', 2435, ids[i // 96 % 6], SELF + i % 96, 1, 120000, 120000), time=15000 + i // 100) for i in range(1536)]
            sizes = []
            original_consume = batched.tracker.consume

            def measured_consume(items, **kwargs):
                sizes.append(len(items))
                return original_consume(items, **kwargs)
            with mock.patch.object(batched.tracker, 'consume', side_effect=measured_consume) as consume:
                batched.process_packet_batch(packets, False)
                self.assertEqual(consume.call_count, 3)
                self.assertEqual(sizes, [mon.PROCESS_PACKET_BATCH_SIZE] * 3)
            for item in packets:
                reference.process_packet_batch([item], False)
            self.assertEqual(len(batched.tracker.states), 72)
            self.assertEqual(state_facts(batched), state_facts(reference))
            self.assertEqual(batched.tracker.pop_apply_events(), reference.tracker.pop_apply_events())
            self.assertEqual(batched.target_tracker.trusted_target_ids(), set(aids))

        def test_buffered_membership_actor_and_historical_events_match_reference(self):
            packets = [status(0, PARTY, 65001), roster(1, [SELF, PARTY]), status(2, SELF, 0), status(3, PARTY, 78), packet(4, mon.ACTOR_STATE_HEADER, struct.pack('<HIHHIB', mon.ACTOR_STATE_HEADER, SELF, 1, 0, 0, 0)), packet(5, 406, struct.pack('<HHIB', 406, 78, PARTY, 0)), status(6, PARTY, 78), leave(7, PARTY), status(8, PARTY, 65002), roster(9, [SELF, NEW_PARTY]), status(10, NEW_PARTY, 79), packet(11, mon.ACTOR_STATE_HEADER, struct.pack('<HIHHIB', mon.ACTOR_STATE_HEADER, SELF, 0, 0, 0, 0)), status(12, SELF, 0)]
            for suppressed in (False, True):
                with self.subTest(historical=suppressed):
                    batched = app_fixture()
                    reference = app_fixture()
                    for app in (batched, reference):
                        app.process_packet_batch([], True)
                        app.incremental_parser.session.suppress_apply_events = suppressed
                    batched.process_packet_batch(packets, False)
                    for item in packets:
                        reference.process_packet_batch([item], False)
                    self.assertEqual(state_facts(batched), state_facts(reference))
                    events = batched.tracker.pop_apply_events()
                    self.assertEqual(events, reference.tracker.pop_apply_events())
                    self.assertEqual(batched.pending_unknown_status_ids, reference.pending_unknown_status_ids)
                    self.assertFalse(any((key[1] == PARTY for key in batched.tracker.states)))
                    self.assertIn((0, SELF), batched.tracker.states)
                    if suppressed:
                        self.assertEqual(events, [])

        def test_same_self_rewind_resets_before_buffer_passes_old_cursor(self):
            batched = app_fixture()
            reference = app_fixture()
            for app in (batched, reference):
                app.process_packet_batch([], True)
                app.process_packet_batch([self_packet(0), status(100, SELF, 78)], False)
                app.tracker.pop_apply_events()
            packets = [self_packet(0)] + [status(i, SELF, 79, remaining=30000 + i) for i in range(1, 103)]
            batched.process_packet_batch(packets, False)
            for item in packets:
                reference.process_packet_batch([item], False)
            self.assertEqual(state_facts(batched), state_facts(reference))
            events = batched.tracker.pop_apply_events()
            self.assertEqual(events, reference.tracker.pop_apply_events())
            self.assertEqual(len(events), 102)
            self.assertEqual(set(batched.tracker.states), {(79, SELF)})

        def test_status_index_rewind_syncs_revocation_before_collecting_state(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(20, PARTY, 65001)], False)
            self.assertIn((65001, PARTY), app.tracker.states)
            self.assertIn(65001, app.pending_unknown_status_ids)
            with mock.patch.object(app, 'sync_tracker_target_filter_snapshot', wraps=app.sync_tracker_target_filter_snapshot) as sync:
                app.process_packet_batch([status(5, SELF, 78), status(6, PARTY, 65002)], False)
                self.assertEqual(sync.call_count, 2)
            self.assertEqual(app.target_tracker.trusted_target_ids(), set())
            self.assertEqual(app.tracker.allowed_target_ids, set())
            self.assertEqual(app.tracker.states, {})
            self.assertEqual(app.tracker.pop_apply_events(), [])
            self.assertEqual(app.pending_unknown_status_ids, set())

        def test_join_unknown_status_leave_in_one_batch_does_not_restore_pending(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, PARTY, 65001), leave(2, PARTY)], False)
            self.assertNotIn(65001, app.pending_unknown_status_ids)
            self.assertEqual(app.tracker.states, {})

        def test_packet_self_switch_clears_previous_identity_without_replay_reset(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, SELF, 78), status(2, PARTY, 78)], False)
            app.process_packet_batch([self_packet(3, NEW_PARTY), status(4, PARTY, 78)], False)
            self.assertEqual(app.target_tracker.self_id, NEW_PARTY)
            self.assertEqual(app.target_tracker.party_ids, set())
            self.assertEqual(app.tracker.states, {})

        def test_outside_packets_never_enter_states_alerts_or_unknown_queue(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), unit(1, OUTSIDE), status(2, OUTSIDE, 65001), status(3, SELF, 78), status(4, PARTY, 78)], False)
            self.assertEqual({key[1] for key in app.tracker.states}, {SELF, PARTY})
            self.assertFalse(any((ev[1] == OUTSIDE for ev in app.tracker.pop_apply_events())))
            self.assertNotIn(65001, app.pending_unknown_status_ids)
            self.assertNotIn(OUTSIDE, app.target_tracker.known_target_ids())
            app.maybe_schedule_client_data_sync(packets=[status(5, OUTSIDE, 65002)])
            self.assertNotIn(65002, app.pending_unknown_status_ids)

        def test_leaving_revokes_states_pending_alerts_and_unknowns(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, PARTY, 65001), status(2, PARTY, 78)], False)
            self.assertIn((78, PARTY), app.tracker.states)
            app.alerted.add(((78, PARTY), 'red'))
            app.recent_apply_alerts[78, PARTY] = 1.0
            app.process_packet_batch([leave(3, PARTY)], False)
            self.assertFalse(any((key[1] == PARTY for key in app.tracker.states)))
            self.assertFalse(any((ev[1] == PARTY for ev in app.tracker.pop_apply_events())))
            self.assertNotIn(((78, PARTY), 'red'), app.alerted)
            self.assertNotIn((78, PARTY), app.recent_apply_alerts)
            self.assertNotIn(65001, app.pending_unknown_status_ids)

        def test_new_replay_or_rerecord_discards_old_party_and_pending_work(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, PARTY, 65001)], False)
            app.incremental_parser.session.identity_snapshot = mon.ReplayIdentitySnapshot(NEW_PARTY, 'SyntheticNew')
            app.process_packet_batch([], True)
            self.assertEqual(app.target_tracker.self_id, NEW_PARTY)
            self.assertEqual(app.target_tracker.party_ids, set())
            self.assertEqual(app.tracker.states, {})
            self.assertEqual(app.pending_unknown_status_ids, set())
            self.assertEqual(app.tracker.pop_apply_events(), [])
            app.process_packet_batch([status(0, PARTY, 78)], False)
            self.assertEqual(app.tracker.states, {})

        def test_expiration_path_rejects_stale_departed_state(self):
            app = app_fixture()
            app.target_tracker = trusted_tracker()
            app.tracker.set_allowed_target_ids({SELF, PARTY})
            app.tracker.consume([status(2, PARTY, 78, remaining=10000)])
            stale = app.tracker.snapshot()
            app.target_tracker.consume([leave(3, PARTY)])
            app.process_expiration_alerts(stale, sync_ms=0, yellow_ms=30000, red_ms=15000, yellow_seconds=30, red_seconds=15)
            self.assertEqual(app.played, [])

        def test_old_unknown_worker_completion_cannot_merge_after_departure(self):
            app = app_fixture()
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, PARTY, 65001)], False)
            old_generation = app._scoped_generation
            app.process_packet_batch([leave(2, PARTY)], False)
            with mock.patch.object(mon, 'merge_status_data_delta', side_effect=AssertionError('stale result merged')):
                app._finish_unknown_resolution({65001}, set(), object(), None, '', False, old_generation)
            self.assertEqual(app.pending_unknown_status_ids, set())

        def assert_serialized_monitor_reset(self, action_name):
            app = app_fixture()
            del app.monitoring_should_continue
            app.monitor_reset_requested = app.monitor_session.reset_event
            app.monitor_session.start_monitoring()
            app.monitor_session.consume_reset_request()
            app.recent_event_history = []
            for name in ('monitor_summary_var', 'monitor_control_var', 'latest_event_var', 'status_table_count_var'):
                setattr(app, name, Value())
            for name in ('clear_live_monitor_display', 'update_monitor_control_ui', 'clear_target_options', 'update_unknown_review_controls', 'update_onboarding', 'update_tab_labels'):
                setattr(app, name, lambda: None)
            app.tree = None
            app.update_main_table = lambda _rows: None
            app.set_message = app.logs.append
            app.process_packet_batch([], True)
            app.process_packet_batch([roster(0, [SELF, PARTY]), status(1, PARTY, 78), status(2, PARTY, 65001)], False)
            self.assertIn((78, PARTY), app.tracker.states)
            self.assertIn(65001, app.pending_unknown_status_ids)
            old_generation = app._scoped_generation
            lock = ObservedRLock()
            app._scoped_monitor_lock = lock
            finished = threading.Event()
            errors = []

            def invoke():
                try:
                    getattr(app, action_name)()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            worker = threading.Thread(target=invoke, daemon=True)
            try:
                with lock:
                    worker.start()
                    self.assertTrue(lock.contending.wait(2), 'control action did not attempt the shared lock')
                    self.assertFalse(finished.is_set(), 'control action bypassed the shared lock')
                    self.assertIn((78, PARTY), app.tracker.states)
                    self.assertIn(65001, app.pending_unknown_status_ids)
                    self.assertEqual(app._scoped_generation, old_generation)
            finally:
                worker.join(2)
            self.assertFalse(worker.is_alive(), 'control action did not finish after the lock was released')
            self.assertEqual(errors, [])
            self.assertEqual(app.tracker.states, {})
            self.assertEqual(app.pending_unknown_status_ids, set())
            self.assertGreater(app._scoped_generation, old_generation)
            self.assertEqual(app.target_tracker.trusted_target_ids(), set())
            self.assertFalse(app.monitoring_should_continue())
            app.process_packet_batch([self_packet(3), roster(4, [SELF, PARTY]), status(5, PARTY, 78), status(6, PARTY, 65001)], True)
            self.assertEqual(app.tracker.states, {})
            self.assertEqual(app.pending_unknown_status_ids, set())
            self.assertEqual(app.target_tracker.trusted_target_ids(), set())

        def test_stop_waits_for_packet_lock_then_rejects_pending_batches(self):
            self.assert_serialized_monitor_reset('stop_monitoring')

        def test_rescan_waits_for_packet_lock_then_rejects_pending_batches(self):
            self.assert_serialized_monitor_reset('rescan')

        def test_manual_identity_handlers_are_disabled_even_if_called_directly(self):
            app = app_fixture()
            app.target_tracker = trusted_tracker()
            app.self_target_name_var = Value('SyntheticMember')
            app.self_target_id_var = Value(str(OUTSIDE))
            app.target_detail_target_id = OUTSIDE
            before = app.target_tracker.scoped_identity_signature()
            for handler in (app.apply_self_target_name, app.apply_self_target_id, app.set_selected_target_as_self, lambda: app.set_self_from_target(OUTSIDE)):
                handler()
                self.assertEqual(app.target_tracker.scoped_identity_signature(), before)

        def test_queued_old_identity_callbacks_cannot_replace_current_self(self):
            app = app_fixture()
            app.target_tracker = trusted_tracker()
            app.target_tracker.consume([self_packet(2, NEW_PARTY)])
            app.self_target_name_var = Value('SyntheticCurrent')
            app.self_target_id_var = Value(str(NEW_PARTY))
            app.apply_replay_snapshot_identity(SELF, 'SyntheticOld')
            app.apply_packet_confirmed_self_id(SELF)
            app.apply_resolved_self_name(SELF, 'SyntheticOld')
            self.assertEqual(app.self_target_name_var.get(), 'SyntheticCurrent')
            self.assertEqual(app.self_target_id_var.get(), str(NEW_PARTY))

        def test_actual_target_ui_rejects_overrides_and_stale_departed_selection(self):
            app = app_fixture()
            app.target_tracker = trusted_tracker()
            app.settings = {'target_scope_display_modes': {'其他目標': mon.TARGET_DISPLAY_MODE_ALL, '畫面成員': mon.TARGET_DISPLAY_MODE_ALL}}
            self.assertEqual(app.scope_display_mode('其他目標'), mon.TARGET_DISPLAY_MODE_OFF)
            self.assertEqual(app.scope_display_mode('畫面成員'), mon.TARGET_DISPLAY_MODE_OFF)
            for aid in (SELF, PARTY):
                self.assertTrue(app.target_is_selected(aid))
            self.assertFalse(app.target_is_selected(OUTSIDE))
            self.assertEqual(app.target_display_mode_for(OUTSIDE), mon.TARGET_DISPLAY_MODE_OFF)
            app.target_filter_var = Value()
            app.target_view_var = Value('全部')
            app.target_option_ids = {SELF, PARTY, OUTSIDE}
            app.target_option_relations = {SELF: '自己', PARTY: '隊伍成員', OUTSIDE: '隊伍成員'}
            app.target_option_names = {aid: 'SyntheticMember' for aid in app.target_option_ids}
            app.target_option_recent_ids = set(app.target_option_ids)
            self.assertEqual(app.filtered_target_ids(), {SELF, PARTY})
            app.target_tracker.consume([leave(2, PARTY)])
            self.assertEqual(app.filtered_target_ids(), {SELF})
            self.assertFalse(app.target_is_selected(PARTY))
            with mock.patch.object(mon.tk, 'Toplevel', side_effect=AssertionError('untrusted dialog opened')):
                app.open_target_status_dialog(PARTY)
                app.open_target_status_dialog(OUTSIDE)
            app.clipboard_clear = lambda: self.fail('untrusted clipboard read')
            app.copy_target_id(PARTY)
            app.copy_target_id(OUTSIDE)

        def test_own_pet_property_preserved_and_reset_clears_it(self):
            tracker = mon.PetTracker()
            data = bytearray(37)
            struct.pack_into('<H24sBHHHHH', data, 0, 418, b'SyntheticPet', 0, 25, 70, 500, 0, 1002)
            tracker.consume([packet(0, 418, data), packet(1, 420, struct.pack('<HBII', 420, 2, PET, 80))])
            self.assertEqual(tracker.snapshot().satiety, 80)
            self.assertEqual(tracker.snapshot().pet_id, 1002)
            tracker.reset()
            self.assertIsNone(tracker.snapshot().pet_id)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contract)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {'source': str(source), 'tests_run': result.testsRun, 'passed': result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped), 'failed': len(result.failures) + len(result.errors), 'skipped': len(result.skipped), 'details': stream.getvalue()}

def main() -> int:
    parser = argparse.ArgumentParser(description='Synthetic self/party scope checks')
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run_source_checks(args.source.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result['failed']))
if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
