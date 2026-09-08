"""Synthetic status lights, freshness, and cancellable audio regressions."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon
from monitor_core.alerts import effect_nature, expiration_phase, status_visual_level, visual_colors
from monitor_ui.view_models import format_overlay_duration, remaining_seconds


class Value:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


def fixture():
    app = mon.RrfMonitorApp.__new__(mon.RrfMonitorApp)
    app.running = True
    app.monitoring_active = threading.Event()
    app.monitoring_active.set()
    app.monitor_reset_requested = threading.Event()
    app.incremental_parser = SimpleNamespace(suppress_apply_events=False)
    app.current_path = Path('synthetic.rrf')
    app.last_rrf_data_monotonic = 1000.0
    app.tracker = mon.StatusTracker()
    app.tracker.set_allowed_target_ids({1, 2})
    app.target_tracker = SimpleNamespace(trusted_target_ids=lambda: {1, 2}, relation_for=lambda _id: '自己',
        target_name=lambda _id: '示例人物')
    app.target_is_selected = lambda target_id: target_id in {1, 2}
    app.target_display_name = lambda _id: '示例人物'
    app.spoken_target_name = lambda _id: '示例人物'
    app.current_policy_resolver = lambda: SimpleNamespace(resolve=lambda **kw: SimpleNamespace(mode=mon.MODE_AUTO))
    app.current_alert_policy_resolver = lambda: SimpleNamespace(resolve=lambda **kw: SimpleNamespace(enabled=True))
    app.alert_rule_for_target = lambda *_: True
    app._expiration_source_was_valid = True
    app.alerted = set()
    app.status_option_source_headers = {}
    app.recent_apply_alerts = {}
    app.recent_event_history = deque(maxlen=5)
    app.last_apply_alert_at = 0.0
    app.latest_event_var = Value('')
    app.apply_sound_var = Value(True)
    app.sound_mode_var = Value('導航人聲')
    app.sound_file_var = Value(str(Path(__file__)))
    app.sound_volume_var = Value(100)
    app.play_alert_sound = mock.Mock()
    return app


def state(app, remaining=20000, *, status_id=3, target_id=1, active=True):
    value = mon.StatusState(status_id, target_id, active, 60000, remaining, 0, 1000.0, 0x0983)
    app.tracker.states[value.key] = value
    return value, remaining


def expire(app, states, *, yellow=30000, red=15000):
    app.process_expiration_alerts(states, sync_ms=0, yellow_ms=yellow, red_ms=red,
        yellow_seconds=yellow / 1000, red_seconds=red / 1000)


class VisualRules(unittest.TestCase):
    def test_only_reviewed_benefits_use_expiration_colors(self):
        for category in ('BUFF', 'DEBUFF', '技能', '消耗品', '經驗', '其他'):
            for group, expected in [('增益','red'), ('減益','debuff'), ('開關／特殊','special'),
                ('混合','special'), ('複合','special'), ('未分類','neutral'), ('伺服器狀態','neutral')]:
                with self.subTest(group=group, category=category):
                    level=status_visual_level(group=group, category=category, remaining_ms=5000,yellow_ms=30000,red_ms=15000)
                    self.assertEqual(level,expected)
                    self.assertEqual(bool(expiration_phase(level)), group == '增益')

    def test_default_and_custom_threshold_boundaries(self):
        for yellow, red, cases in [(30000,15000,[(31000,'normal'),(30000,'yellow'),(16000,'yellow'),(15000,'red'),(1000,'red')]),
            (12000,4000,[(12001,'normal'),(12000,'yellow'),(4001,'yellow'),(4000,'red')])]:
            for value, expected in cases:
                self.assertEqual(status_visual_level(group='增益',category='技能',remaining_ms=value,yellow_ms=yellow,red_ms=red),expected)

    def test_unknown_time_has_no_expiration_phase(self):
        level=status_visual_level(group='增益',category='BUFF',remaining_ms=None,yellow_ms=30000,red_ms=15000)
        self.assertEqual(expiration_phase(level),'')
        self.assertEqual(format_overlay_duration(None),'生效中')

    def test_all_countdown_outputs_round_positive_values_up(self):
        for value, seconds in [(1,1),(999,1),(1000,1),(1001,2),(0,0),(-1,0)]:
            self.assertEqual(remaining_seconds(value),seconds)
            self.assertEqual(mon.RrfMonitorApp.format_duration(value),f'00:{seconds:02}')
            self.assertEqual(format_overlay_duration(value),f'{seconds}秒')

    def test_palette_and_sort_follow_effect_nature(self):
        app=fixture()
        for level in ('normal','yellow','red','debuff','special','neutral','stale'):
            self.assertEqual(app.overlay_light_colors(level),visual_colors(level))
        with mock.patch.object(mon,'status_group',side_effect=lambda value,*_: {1:'減益',2:'增益',3:'混合',4:'未分類'}[value]):
            self.assertEqual(sorted([4,2,3,1],key=mon.status_display_priority),[1,2,3,4])

    def test_shared_sort_keeps_people_and_debuffs_first_for_all_modes(self):
        app=fixture()
        rows=[state(app,4000,status_id=4),state(app,60000,status_id=1),state(app,1000,status_id=2,target_id=2)]
        with mock.patch.object(mon,'status_group',side_effect=lambda value,*_: {1:'減益',2:'增益',4:'未分類'}[value]):
            for mode in ('人物優先','狀態優先','剩餘時間'):
                app.status_sort_var=Value(mode)
                self.assertEqual([row[0].key for row in sorted(rows,key=lambda row:app.status_row_sort_key(*row))],[(1,1),(4,1),(2,2)])

    def test_protocol_uncertainties_are_not_reinterpreted(self):
        for value in (0,0xffffffff):
            tracker=mon.StatusTracker();tracker.set_allowed_target_ids({1})
            data=struct.pack('<HHIBIIIII',0x0983,3,1,1,value,value,0,0,0)
            tracker.consume([mon.ReplayPacket(0,0,0x0983,data)],emit_apply_events=False)
            self.assertEqual(tracker.states[(3,1)].remaining_ms,value)
        for flag in (0,1):
            tracker=mon.StatusTracker();tracker.set_allowed_target_ids({1})
            tracker.consume([mon.ReplayPacket(0,0,0x0196,struct.pack('<HHIB',0x0196,3,1,flag))])
            self.assertFalse(tracker.states[(3,1)].active)

    def test_main_table_uses_short_distinct_labels_and_preserves_stale_text(self):
        app=fixture()
        cases=[('增益','normal','啟用','增益'),('減益','debuff','啟用','異常'),
            ('混合','special','啟用','特殊'),('未分類','neutral','啟用','待確認'),
            ('增益','yellow','啟用','增益｜快到期'),('增益','red','啟用','增益｜快結束'),
            ('增益','stale','上次資料','上次資料'),('減益','neutral','已結束','已結束'),
            ('開關／特殊','special','生效中','特殊')]
        for group,level,state_text,expected in cases:
            with self.subTest(group=group,level=level), mock.patch.object(mon,'status_group',return_value=group):
                app.tree=mock.Mock()
                app.tree.insert.return_value='synthetic-row'
                app.tree_order_keys=[];app.tree_row_ids={};app.tree_row_values={}
                value,remaining=state(app)
                app.update_main_table([(value,state_text,'00:20','01:00',level,remaining)])
                self.assertEqual(app.tree.insert.call_args.kwargs['values'][4],expected)
                self.assertEqual(app.tree.insert.call_args.kwargs['tags'],(level,))


class SourceAndExpiration(unittest.TestCase):
    def setUp(self):
        self.clock=mock.patch.object(mon.time,'monotonic',return_value=1000.0).start()
        mock.patch.object(mon,'status_group',return_value='增益').start()
        mock.patch.object(mon,'status_library_category',return_value='技能').start()
        self.addCleanup(mock.patch.stopall)

    def test_source_freshness_is_not_status_event_freshness(self):
        app=fixture();app.tracker.last_timeline_ms=0
        self.clock.return_value=1009
        self.assertTrue(app.source_data_status()[0])
        self.clock.return_value=1011
        self.assertFalse(app.source_data_status()[0])
        app.last_rrf_data_monotonic=1011
        self.assertTrue(app.source_data_status()[0])
        app.incremental_parser.suppress_apply_events=True
        self.assertEqual(app.source_data_status(),(False,'建立中'))
        app.monitoring_active.clear()
        self.assertEqual(app.source_data_status(),(False,'已停止'))

    def test_stale_values_freeze_until_valid_data_resumes(self):
        app=fixture();initial=state(app,40000)
        self.assertEqual(app.status_display_states([initial],True)[0][1],40000)
        self.assertEqual(app.status_display_states([state(app,20000)],False)[0][1],40000)
        self.assertEqual(app.status_display_states([state(app,5000)],False)[0][1],40000)
        self.assertEqual(app.status_display_states([state(app,3000)],True)[0][1],3000)
        app.reset_status_alert_tracking()
        self.assertEqual(app.status_display_states([state(app,9000)],False)[0][1],9000)

    def test_history_yellow_consumed_then_future_red_alerts(self):
        app=fixture();app.incremental_parser.suppress_apply_events=True
        expire(app,[state(app,20000)]);app.play_alert_sound.assert_not_called()
        app.incremental_parser.suppress_apply_events=False
        expire(app,[state(app,18000)]);app.play_alert_sound.assert_not_called()
        expire(app,[state(app,14000)])
        self.assertEqual(app.play_alert_sound.call_args.args[0],'red')
        self.assertEqual(app.play_alert_sound.call_args.args[2],14)

    def test_history_rebuilt_between_ui_ticks_still_establishes_baseline(self):
        app=fixture();app.reset_status_alert_tracking()
        expire(app,[state(app,10000)])
        app.play_alert_sound.assert_not_called()
        expire(app,[state(app,9000)])
        app.play_alert_sound.assert_not_called()

    def test_stale_red_never_backfills_and_new_lifecycle_can_alert(self):
        app=fixture();app.last_rrf_data_monotonic=980
        expire(app,[state(app,14000)])
        app.last_rrf_data_monotonic=1000
        expire(app,[state(app,10000)]);app.play_alert_sound.assert_not_called()
        expire(app,[state(app,0,active=False)])
        expire(app,[state(app,4000)])
        self.assertEqual(app.play_alert_sound.call_count,1)
        self.assertEqual(app.play_alert_sound.call_args.args[2],4)

    def test_gap_without_ui_tick_does_not_backfill_yellow(self):
        app=fixture();expire(app,[state(app,40000)])
        self.clock.return_value=1020;app.last_rrf_data_monotonic=1020
        expire(app,[state(app,20000)]);app.play_alert_sound.assert_not_called()
        self.clock.return_value=1021;app.last_rrf_data_monotonic=1021
        expire(app,[state(app,14000)])
        self.assertEqual(app.play_alert_sound.call_count,1)

    def test_fresh_application_is_not_mistaken_for_history(self):
        app=fixture();app.reset_status_alert_tracking();state(app,4000)
        app.process_apply_events([(3,1,0)])
        app.play_alert_sound.reset_mock()
        expire(app,[state(app,4000)])
        self.assertEqual(app.play_alert_sound.call_count,1)

    def test_expirations_merge_people_phases_and_exclude_muted(self):
        app=fixture()
        app.current_alert_policy_resolver=lambda:SimpleNamespace(resolve=lambda **kw:SimpleNamespace(enabled=kw['status_id']!=5))
        rows=[state(app,20000),state(app,4000,target_id=2),state(app,5000,status_id=5)]
        expire(app,rows);expire(app,rows)
        self.assertEqual(app.play_alert_sound.call_count,1)
        self.assertEqual(app.play_alert_sound.call_args.args[0],'expiration_batch')
        self.assertIn('二項',app.play_alert_sound.call_args.args[1])
        self.assertEqual(set(app.play_alert_sound.call_args.kwargs['status_keys']),{(3,1),(3,2)})

    def test_end_and_new_application_in_same_packet_batch_is_a_new_lifecycle(self):
        app=fixture()
        def apply(index,remaining):
            data=struct.pack('<HHIBIIIII',0x0983,3,1,1,remaining,remaining,0,0,0)
            return mon.ReplayPacket(index,index,0x0983,data)
        app.tracker.consume([apply(0,4000)])
        first_revision=app.tracker.states[(3,1)].activation_revision
        expire(app,app.tracker.snapshot())
        self.assertEqual(app.play_alert_sound.call_count,1)
        ended=mon.ReplayPacket(1,1,0x0196,struct.pack('<HHIB',0x0196,3,1,0))
        app.tracker.consume([ended,apply(2,4000)])
        self.assertGreater(app.tracker.states[(3,1)].activation_revision,first_revision)
        expire(app,app.tracker.snapshot())
        self.assertEqual(app.play_alert_sound.call_count,2)
        revision=app.tracker.states[(3,1)].activation_revision
        app.tracker.consume([apply(3,4000)])
        self.assertEqual(app.tracker.states[(3,1)].activation_revision,revision)
        expire(app,app.tracker.snapshot())
        self.assertEqual(app.play_alert_sound.call_count,2)

    def test_actor_activation_revision_changes_only_after_a_clear(self):
        tracker=mon.StatusTracker();tracker.set_allowed_target_ids({1})
        def actor(index,body):
            return mon.ReplayPacket(index,index,0x0229,struct.pack('<HIHHIB',0x0229,1,body,0,0,0))
        tracker.consume([actor(0,1)])
        first=next(iter(tracker.states.values())).activation_revision
        tracker.consume([actor(1,1)])
        self.assertEqual(next(iter(tracker.states.values())).activation_revision,first)
        tracker.consume([actor(2,0),actor(3,1)])
        self.assertGreater(next(iter(tracker.states.values())).activation_revision,first)

    def test_no_duration_ended_and_neutral_never_expire(self):
        app=fixture()
        for row in (state(app,None),state(app,0),state(app,20000,active=False)):
            expire(app,[row])
        for group in ('未分類','減益','開關／特殊','複合'):
            with mock.patch.object(mon,'status_group',return_value=group):expire(app,[state(app,4000)])
        app.play_alert_sound.assert_not_called()

    def test_explicitly_enabled_unknown_apply_uses_neutral_wording(self):
        app=fixture();state(app,30000,status_id=65000)
        with mock.patch.object(mon,'is_readable_status_name',return_value=False):
            app.process_apply_events([(65000,1,0)])
        self.assertEqual(app.play_alert_sound.call_count,1)
        self.assertIn('生效',app.latest_event_var.get())
        self.assertNotIn('獲得',app.latest_event_var.get())
        self.assertEqual(app.default_status_alert_rule(65000),{'apply':False,'yellow':False,'red':False})

    def test_apply_from_history_or_already_ended_does_not_play(self):
        app=fixture();state(app,30000)
        app.incremental_parser.suppress_apply_events=True
        app.process_apply_events([(3,1,0)])
        app.incremental_parser.suppress_apply_events=False
        state(app,0,active=False);app.process_apply_events([(3,1,0)])
        app.play_alert_sound.assert_not_called()


class AudioLifecycle(unittest.TestCase):
    def setUp(self):
        mock.patch.object(mon.time,'monotonic',return_value=1000).start()
        self.addCleanup(mock.patch.stopall)

    def test_actual_seconds_and_neutral_apply_text(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock()
        state(app,4000)
        app.play_alert_sound('red','合成狀態',4,status_keys=((3,1),))
        self.assertIn('剩餘四秒',app.speak_text.call_args.args[0])
        app.play_alert_sound('apply','合成異常',0,event_age_seconds=0,status_keys=((3,1),))
        self.assertIn('生效',app.speak_text.call_args.args[0])
        self.assertNotIn('獲得',app.speak_text.call_args.args[0])

    def test_ended_or_stale_audio_cannot_launch(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock()
        state(app,0,active=False)
        app.play_alert_sound('red','合成狀態',4,status_keys=((3,1),))
        state(app,4000);app.last_rrf_data_monotonic=980
        app.play_alert_sound('red','合成狀態',4,status_keys=((3,1),))
        app.speak_text.assert_not_called()

    def test_dispatch_rechecks_adjusted_time_and_rejects_renewed_state(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock()
        state(app,5500)
        app.play_alert_sound('red','合成狀態',15,status_keys=((3,1),),remaining_offset_ms=1500,expiration_limit_ms=15000)
        self.assertIn('剩餘四秒',app.speak_text.call_args.args[0])
        app.speak_text.reset_mock();state(app,60000)
        app.play_alert_sound('red','合成狀態',4,status_keys=((3,1),),expiration_limit_ms=15000)
        app.speak_text.assert_not_called()

    def test_close_blocks_new_audio_even_before_monitor_event_is_cleared(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock()
        app.close_finalized=True
        app.play_alert_sound('red','合成狀態',4)
        app.speak_text.assert_not_called()

    def test_explicit_sound_preview_works_while_monitor_is_stopped(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock();app.monitoring_active.clear()
        app.test_alert_sound()
        self.assertEqual(app.speak_text.call_count,1)

    def test_sapi_and_wav_cancel_in_any_runtime(self):
        app=fixture();del app.play_alert_sound
        process=mock.Mock();process.poll.return_value=None
        wav=SimpleNamespace(PlaySound=mock.Mock(),SND_FILENAME=1,SND_ASYNC=2)
        with mock.patch.object(mon.subprocess,'Popen',return_value=process),mock.patch.object(mon,'winsound',wav):
            app.play_alert_sound('yellow','合成狀態',20)
            app.sound_mode_var.set('自訂 WAV')
            app.play_alert_sound('yellow','合成狀態',20)
            app.cancel_alert_audio()
        process.terminate.assert_called_once()
        self.assertEqual(wav.PlaySound.call_args.args,(None,0))
        self.assertNotIn('_speech_processes',app.__dict__)

    def test_waiting_audio_generation_is_cancelled_before_launch(self):
        app=fixture();del app.play_alert_sound;app.speak_text=mock.Mock()
        held=threading.RLock();entered=threading.Event()
        class Gate:
            def __enter__(self):
                if threading.current_thread() is not threading.main_thread():entered.set()
                held.acquire()
            def __exit__(self,*_):held.release()
        app._alert_audio_lock=Gate()
        held.acquire()
        thread=threading.Thread(target=app.play_alert_sound,args=('red','合成狀態',4))
        try:
            thread.start();self.assertTrue(entered.wait(1))
            app.cancel_alert_audio()
        finally:
            held.release();thread.join(2)
        self.assertFalse(thread.is_alive())
        app.speak_text.assert_not_called()


if __name__=='__main__':
    unittest.main(verbosity=2)
