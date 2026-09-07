"""Synthetic own-pet observations and reminder state transitions; no GUI/audio."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
from monitor_core.pet_alerts import PetAlertController


def property_packet(index: int, satiety: int, *, pet_id: int = 1002, name: str = "SyntheticPet"):
    payload = struct.pack("<H24sBHHHHH", mon.PET_PROPERTY_HEADER, name.encode("cp950")[:24],
                          0, 20, satiety, 500, 0, pet_id)
    return mon.ReplayPacket(index, index * 100, mon.PET_PROPERTY_HEADER, payload)


def state_packet(index: int, value: int, *, state_type: int = 2):
    payload = struct.pack("<HBII", mon.PET_STATE_HEADER, state_type, 900001, value)
    return mon.ReplayPacket(index, index * 100, mon.PET_STATE_HEADER, payload)


class PetAlertTests(unittest.TestCase):
    def setUp(self):
        self.tracker = mon.PetTracker()
        self.reminder = PetAlertController()

    def evaluate(self, state, **overrides):
        options = dict(enabled=True, live=True, valid=True, threshold=10)
        options.update(overrides)
        return self.reminder.evaluate(state, **options)

    def consume(self, *packets, **overrides):
        return [self.evaluate(state, **overrides) for state in self.tracker.consume(list(packets))]

    def test_first_live_value_at_threshold_alerts_once_including_zero(self):
        first = self.consume(property_packet(0, 10))[0]
        self.assertEqual((first.pet_id, first.satiety, first.threshold), (1002, 10, 10))
        self.assertEqual(self.consume(state_packet(1, 9), state_packet(2, 0)), [None, None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot()))

    def test_recovery_must_exceed_threshold_then_next_low_alerts(self):
        events = self.consume(property_packet(0, 9), state_packet(1, 10), state_packet(2, 11), state_packet(3, 10))
        self.assertEqual([event is not None for event in events], [True, False, False, True])

    def test_same_pet_property_after_map_change_does_not_restart_episode(self):
        self.assertIsNotNone(self.consume(property_packet(0, 9))[0])
        generation = self.tracker.snapshot().identity_generation
        self.assertEqual(self.consume(property_packet(1, 9), property_packet(2, 8)), [None, None])
        self.assertEqual(self.tracker.snapshot().identity_generation, generation)

    def test_known_pet_change_and_recording_reset_restart_first_low(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8))[0])
        first_generation = self.tracker.snapshot().identity_generation
        self.assertIsNotNone(self.consume(property_packet(1, 8, pet_id=1003, name="SyntheticNew"))[0])
        self.assertGreater(self.tracker.snapshot().identity_generation, first_generation)
        second_generation = self.tracker.snapshot().identity_generation
        self.tracker.reset()
        self.assertIsNone(self.tracker.snapshot().satiety)
        self.assertIsNotNone(self.consume(property_packet(0, 8, pet_id=1003, name="SyntheticNew"))[0])
        self.assertGreater(self.tracker.snapshot().identity_generation, second_generation)

    def test_visible_name_change_can_be_detected_but_same_kind_and_name_are_ambiguous(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8))[0])
        generation = self.tracker.snapshot().identity_generation
        self.assertIsNotNone(self.consume(property_packet(1, 8, name="SyntheticNew"))[0])
        self.assertGreater(self.tracker.snapshot().identity_generation, generation)
        self.assertEqual(self.consume(property_packet(2, 8, name="SyntheticNew")), [None])

    def test_history_never_replays_and_first_new_live_low_can_alert(self):
        self.assertEqual(self.consume(property_packet(0, 8), state_packet(1, 7), live=False), [None, None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), live=True))
        self.assertIsNotNone(self.consume(state_packet(2, 6))[0])

    def test_invalid_or_stopped_observation_never_replays_when_context_recovers(self):
        self.assertEqual(self.consume(property_packet(0, 8), valid=False), [None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), valid=True))
        self.assertEqual(self.consume(state_packet(1, 7), monitoring_enabled=False), [None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), monitoring_enabled=True))
        self.assertIsNotNone(self.consume(state_packet(2, 6))[0])

    def test_stale_high_does_not_rearm_an_already_alerted_episode(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8))[0])
        self.assertEqual(self.consume(state_packet(1, 20), valid=False), [None])
        self.assertEqual(self.consume(state_packet(2, 7)), [None])

    def test_toggling_reminder_or_changing_threshold_does_not_replay_old_sample(self):
        self.assertEqual(self.consume(property_packet(0, 8), enabled=False), [None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), enabled=True))
        self.assertIsNotNone(self.consume(state_packet(1, 8))[0])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), enabled=False))
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), enabled=True, threshold=20))
        self.assertEqual(self.consume(state_packet(2, 8), threshold=20), [None])

    def test_only_valid_satiety_updates_advance_freshness(self):
        initial = self.tracker.consume([property_packet(0, 8)])[0]
        feed = mon.ReplayPacket(2, 200, mon.PET_FEED_HEADER, struct.pack("<HBH", mon.PET_FEED_HEADER, 1, 501))
        unknown = mon.ReplayPacket(3, 300, 0x01A5, b"\xa5\x01")
        self.assertEqual(self.tracker.consume([state_packet(1, 501, state_type=1), feed, unknown,
                                              state_packet(4, 101), state_packet(5, 99, state_type=99)]), [])
        latest = self.tracker.snapshot()
        self.assertEqual((latest.satiety_revision, latest.satiety_timeline_ms, latest.satiety),
                         (initial.satiety_revision, initial.satiety_timeline_ms, 8))
        self.assertEqual(latest.intimacy, 501)
        self.assertGreater(self.tracker.last_timeline_ms, latest.satiety_timeline_ms)

    def test_state_before_property_and_malformed_property_do_not_create_observations(self):
        self.assertEqual(self.tracker.consume([state_packet(0, 8)]), [])
        malformed = mon.ReplayPacket(1, 100, mon.PET_PROPERTY_HEADER, b"\xa2\x01")
        self.assertEqual(self.tracker.consume([malformed, property_packet(2, 101)]), [])
        self.assertIsNone(self.tracker.snapshot().satiety)
        self.assertIsNone(self.evaluate(self.tracker.snapshot()))

    def test_ordered_batch_preserves_recovery_and_returned_snapshots_are_independent(self):
        observations = self.tracker.consume([property_packet(0, 8), state_packet(1, 20), state_packet(2, 7)])
        self.assertEqual([state.satiety for state in observations], [8, 20, 7])
        self.assertEqual([self.evaluate(state) is not None for state in observations], [True, False, True])
        observations[0].satiety = 99
        self.assertEqual(self.tracker.snapshot().satiety, 7)

    def test_out_of_order_revisions_and_previous_identity_do_not_replay(self):
        old = self.tracker.consume([property_packet(0, 8)])[0]
        self.assertIsNotNone(self.evaluate(old))
        fresh = self.tracker.consume([state_packet(1, 20)])[0]
        self.assertIsNone(self.evaluate(fresh))
        self.assertIsNone(self.evaluate(old))
        new = self.tracker.consume([property_packet(2, 8, pet_id=1003)])[0]
        self.assertIsNotNone(self.evaluate(new))
        self.assertIsNone(self.evaluate(old))

    def test_invalid_external_values_never_trigger_and_reset_can_accept_new_tracker(self):
        state = self.tracker.consume([property_packet(0, 8)])[0]
        for invalid in (None, -1, 101, True, "8"):
            self.reminder.reset()
            self.assertIsNone(self.evaluate(replace(state, satiety=invalid)))
        self.reminder.reset()
        self.assertIsNotNone(self.evaluate(state))
        replacement_tracker = mon.PetTracker()
        self.reminder.reset()
        self.assertIsNotNone(self.evaluate(replacement_tracker.consume([property_packet(0, 8)])[0]))

    def test_default_threshold_is_25_and_default_clock_is_monotonic(self):
        state = self.tracker.consume([property_packet(0, 25)])[0]
        with mock.patch("monitor_core.pet_alerts.time.monotonic", return_value=123.0) as clock:
            event = self.reminder.evaluate(state, enabled=True, live=True, valid=True)
        clock.assert_called_once_with()
        self.assertEqual((event.satiety, event.threshold), (25, 25))

    def test_repeat_requires_60_seconds_and_a_new_confirmed_value(self):
        self.assertIsNotNone(self.consume(property_packet(0, 20), threshold=25, now=0)[0])
        self.assertEqual(self.consume(property_packet(1, 20), threshold=25, now=59.999), [None])
        self.assertIsNotNone(self.consume(state_packet(2, 20), threshold=25, now=60)[0])
        self.assertEqual(self.consume(property_packet(3, 20), threshold=25, now=60.001), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 19), threshold=25, now=120)[0])

    def test_time_alone_or_same_revision_never_repeats_and_missed_intervals_do_not_burst(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8), now=0)[0])
        state = self.tracker.snapshot()
        for now in (60, 120, 600):
            self.assertIsNone(self.evaluate(state, now=now))
        self.assertIsNotNone(self.consume(state_packet(1, 7), now=600)[0])
        self.assertEqual(self.consume(state_packet(2, 7), property_packet(3, 7), now=600), [None, None])

    def test_confirmed_recovery_stops_repeat_and_next_low_starts_a_new_episode(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8), now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 20), now=30), [None])
        self.assertIsNotNone(self.consume(state_packet(2, 9), now=31)[0])
        self.assertEqual(self.consume(state_packet(3, 9), now=50.999), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 9), now=51)[0])

    def test_mute_history_and_invalid_periods_require_a_new_sample_after_recovery(self):
        for suppressed in (dict(enabled=False), dict(live=False), dict(valid=False), dict(monitoring_enabled=False)):
            with self.subTest(suppressed=suppressed):
                self.setUp()
                self.assertIsNotNone(self.consume(property_packet(0, 8), now=0)[0])
                self.assertEqual(self.consume(state_packet(1, 7), now=100, **suppressed), [None])
                self.assertIsNone(self.evaluate(self.tracker.snapshot(), now=101))
                self.assertIsNotNone(self.consume(state_packet(2, 7), now=102)[0])
                self.assertEqual(self.consume(state_packet(3, 6), now=103), [None])

    def test_recovery_while_muted_is_observed_without_emitting_a_sound(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8), now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 20), now=10, enabled=False), [None])
        self.assertEqual(self.consume(state_packet(2, 8), now=20, enabled=False), [None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), now=21))
        self.assertIsNotNone(self.consume(state_packet(3, 8), now=22)[0])

    def test_unverified_recovery_cannot_bypass_repeat_interval(self):
        self.assertIsNotNone(self.consume(property_packet(0, 8), now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 20), now=10, valid=False), [None])
        self.assertEqual(self.consume(state_packet(2, 8), now=19.999), [None])
        self.assertIsNotNone(self.consume(state_packet(3, 8), now=20)[0])

    def test_nonfinite_and_backward_clock_values_cannot_trigger_extra_alerts(self):
        self.assertIsNotNone(self.consume(property_packet(0, 20), threshold=25, now=0)[0])
        for index, now in enumerate((float("nan"), float("inf"), float("-inf"), -1, "invalid"), 1):
            self.assertEqual(self.consume(state_packet(index, 20), threshold=25, now=now), [None])
        self.assertEqual(self.consume(state_packet(6, 20), threshold=25, now=59), [None])
        self.assertEqual(self.consume(state_packet(7, 20), threshold=25, now=58), [None])
        self.assertIsNotNone(self.consume(state_packet(8, 20), threshold=25, now=60)[0])
        self.assertEqual(self.consume(state_packet(9, 30), threshold=25, now=70), [None])
        self.assertEqual(self.consume(state_packet(10, 20), threshold=25, now=69), [None])
        self.assertIsNotNone(self.consume(state_packet(11, 20), threshold=25, now=71)[0])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), threshold=25, now=140))
        self.assertEqual(self.consume(state_packet(12, 20), threshold=25, now=139), [None])
        self.assertIsNotNone(self.consume(state_packet(13, 20), threshold=25, now=140)[0])

    def test_ordered_low_high_low_batch_keeps_episode_transitions_with_controlled_time(self):
        events = self.consume(property_packet(0, 8), state_packet(1, 20), state_packet(2, 7), now=10)
        self.assertEqual([event is not None for event in events], [True, False, True])
        self.assertEqual(self.consume(property_packet(3, 7), now=29.999), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 7), now=30)[0])

    def test_entering_critical_at_10_bypasses_normal_cooldown_once(self):
        self.assertIsNotNone(self.consume(property_packet(0, 11), threshold=25, now=0)[0])
        self.assertIsNotNone(self.consume(state_packet(1, 10), threshold=25, now=1)[0])
        self.assertEqual(self.consume(state_packet(2, 9), threshold=25, now=2), [None])
        self.assertEqual(self.consume(property_packet(3, 10), threshold=25, now=20.999), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 10), threshold=25, now=21)[0])

    def test_zero_satiety_repeats_only_on_new_observations_at_20_seconds(self):
        self.assertIsNotNone(self.consume(property_packet(0, 0), now=0)[0])
        self.assertEqual(self.consume(property_packet(1, 0), now=19.999), [None])
        self.assertIsNotNone(self.consume(state_packet(2, 0), now=20)[0])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), now=40))
        self.assertIsNotNone(self.consume(state_packet(3, 0), now=40)[0])

    def test_recovery_to_11_downgrades_to_60_seconds_then_reentry_is_immediate(self):
        self.assertIsNotNone(self.consume(property_packet(0, 10), threshold=25, now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 11), threshold=25, now=20), [None])
        self.assertEqual(self.consume(state_packet(2, 11), threshold=25, now=59.999), [None])
        self.assertIsNotNone(self.consume(state_packet(3, 11), threshold=25, now=60)[0])
        self.assertIsNotNone(self.consume(state_packet(4, 10), threshold=25, now=61)[0])
        self.assertEqual(self.consume(state_packet(5, 9), threshold=25, now=80.999), [None])
        self.assertIsNotNone(self.consume(state_packet(6, 9), threshold=25, now=81)[0])

    def test_history_invalid_and_stopped_samples_do_not_change_the_valid_zone(self):
        for suppressed in (dict(live=False), dict(valid=False), dict(monitoring_enabled=False)):
            for start, ignored, final, should_alert in ((11, 10, 10, True), (10, 11, 10, False)):
                with self.subTest(suppressed=suppressed, start=start):
                    self.setUp()
                    self.assertIsNotNone(self.consume(property_packet(0, start), threshold=25, now=0)[0])
                    self.assertEqual(self.consume(state_packet(1, ignored), threshold=25, now=1, **suppressed), [None])
                    event = self.consume(state_packet(2, final), threshold=25, now=2)[0]
                    self.assertEqual(event is not None, should_alert)

    def test_critical_entry_while_disabled_is_not_replayed_on_enable(self):
        self.assertIsNotNone(self.consume(property_packet(0, 11), threshold=25, now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 10), threshold=25, now=1, enabled=False), [None])
        self.assertIsNone(self.evaluate(self.tracker.snapshot(), threshold=25, now=2))
        self.assertEqual(self.consume(state_packet(2, 9), threshold=25, now=2), [None])
        self.assertIsNotNone(self.consume(state_packet(3, 9), threshold=25, now=20)[0])

    def test_critical_zone_does_not_override_a_lower_user_threshold(self):
        self.assertEqual(self.consume(property_packet(0, 11), threshold=7, now=0), [None])
        self.assertEqual(self.consume(state_packet(1, 8), threshold=7, now=1), [None])
        self.assertIsNotNone(self.consume(state_packet(2, 7), threshold=7, now=2)[0])
        self.assertEqual(self.consume(state_packet(3, 0), threshold=7, now=21.999), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 0), threshold=7, now=22)[0])
        self.assertEqual(self.consume(state_packet(5, 8), threshold=7, now=23), [None])
        self.assertIsNotNone(self.consume(state_packet(6, 7), threshold=7, now=24)[0])
        self.assertEqual(self.consume(state_packet(7, 1), threshold=0, now=25), [None])
        self.assertIsNotNone(self.consume(state_packet(8, 0), threshold=0, now=26)[0])

    def test_recovery_above_user_threshold_fully_resets_critical_episode(self):
        self.assertIsNotNone(self.consume(property_packet(0, 10), threshold=25, now=0)[0])
        self.assertEqual(self.consume(state_packet(1, 26), threshold=25, now=1), [None])
        self.assertIsNotNone(self.consume(state_packet(2, 10), threshold=25, now=2)[0])
        self.assertEqual(self.consume(state_packet(3, 9), threshold=25, now=21.999), [None])
        self.assertIsNotNone(self.consume(state_packet(4, 9), threshold=25, now=22)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
