"""Application-level unknown observations and reviewed catalog updates."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rrf_monitor as mon


def observation(target_id=1001, status_id=65000, remaining=20000):
    return (SimpleNamespace(target_id=target_id, status_id=status_id, active=True,
                            total_ms=60000, event_timeline_ms=0, source_header=0x0983), remaining)


def snapshot_app(mode=mon.MODE_AUTO, selected=()):
    resolver = mon.PolicyResolver({
        'self': mon.ScopePolicy(mode, frozenset(selected)),
        'party': mon.ScopePolicy(mon.MODE_AUTO),
    })
    session = mon.MonitorSession(resolver)
    session.set_trusted_targets(self_id=1001, party_ids={1002})
    tracker = SimpleNamespace(
        trusted_target_ids=lambda: frozenset({1001, 1002}),
        relation_for=lambda target: {1001: '自己', 1002: '隊伍成員'}.get(target, '其他目標'),
        target_name=lambda target: {1001: 'ExampleSelf', 1002: 'ExampleMember'}.get(target, ''),
    )
    return SimpleNamespace(monitor_session=session, target_tracker=tracker,
        current_policy_resolver=lambda: resolver,
        sync_tracker_target_filter_snapshot=lambda: None,
        current_readable_status_ids=lambda: frozenset({0, 78}))


class ObservationDisplayTests(unittest.TestCase):
    def test_observed_unknown_survives_auto_and_explicit_custom_selection(self):
        for mode in (mon.MODE_AUTO, mon.MODE_CUSTOM):
            with self.subTest(mode=mode):
                app = snapshot_app(mode, {0, 65000})
                result = mon.RrfMonitorApp.build_monitor_snapshot(app, [observation(), observation(status_id=0)])
                self.assertEqual(result.keys, frozenset({(65000, 1001), (0, 1001)}))
                self.assertEqual(result.excluded_unreadable_count, 0)

    def test_custom_and_off_still_control_unknown_visibility(self):
        for mode in (mon.MODE_CUSTOM, mon.MODE_OFF):
            app = snapshot_app(mode, {78})
            result = mon.RrfMonitorApp.build_monitor_snapshot(app, [observation()])
            self.assertEqual(result.statuses, ())

    def test_same_person_duplicates_collapse_but_people_and_times_are_independent(self):
        app = snapshot_app()
        result = mon.RrfMonitorApp.build_monitor_snapshot(app, [
            observation(remaining=30000), observation(remaining=18000),
            observation(target_id=1002, remaining=9000), observation(target_id=1003),
        ])
        self.assertEqual({item.observation.key: item.observation.remaining_ms for item in result.statuses},
                         {(65000, 1001): 18000, (65000, 1002): 9000})

    def test_names_use_identifiable_placeholders_without_hiding_observations(self):
        with patch.dict(mon.EFST_NAMES, {65000: 'EFST_EXAMPLE', 65001: 'ODINS_POWER', 65002: '示例狀態'}):
            self.assertEqual(mon.status_name(65000), '未確認狀態 65000')
            self.assertEqual(mon.status_name(65001), '未確認狀態 65001')
            self.assertEqual(mon.status_name(65002), '示例狀態')
            self.assertIn('伺服器狀態', mon.status_name(65000, 0x0196))


class ReviewedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.before = (dict(mon.EFST_NAMES), dict(mon.EFST_GROUPS), mon.STATUS_DATA_SOURCE,
                       dict(mon.CURRENT_EFST_CODES))

    def tearDown(self):
        names, groups, source, codes = self.before
        mon.set_status_data(names, groups, source, efst_codes=codes)

    def test_review_survives_full_update_and_incremental_raw_title_color(self):
        mon.set_status_data({1150: '致命放射', 13: '敏捷降低'}, {1150: '增益', 13: '增益'}, 'example')
        self.assertEqual(mon.status_group(1150), '減益')
        self.assertEqual(mon.status_group(13), '減益')
        self.assertEqual(mon.status_group(78), '增益')
        mon.merge_status_data_delta(mon.StatusDataResult({1150: '致命放射'}, {1150: '增益'}, 'example delta', '', []))
        self.assertEqual(mon.status_group(1150), '減益')

    def test_changed_code_is_neutral_and_recovery_restores_review(self):
        mon.set_status_data({1150: '新的不同效果'}, {1150: '增益'}, 'example',
                            efst_codes={1150: 'EFST_DIFFERENT_EXAMPLE'})
        self.assertEqual(mon.status_name(1150), '新的不同效果')
        self.assertEqual(mon.status_group(1150), '未分類')
        self.assertTrue(any(item.status_id == 1150 for item in mon.STATUS_REVIEW_CONFLICTS))
        mon.merge_status_data_delta(mon.StatusDataResult({1150: '致命放射'}, {1150: '增益'}, 'example', '', [],
            {1150: mon.STATUS_EFFECT_REVIEWS.code_map[1150]}))
        self.assertEqual(mon.status_group(1150), '減益')

    def test_code_identity_survives_worker_payload_and_cache_roundtrip(self):
        value = mon.StatusDataResult({1150: '新的不同效果'}, {1150: '增益'}, 'example', 'example', [],
                                     {1150: 'EFST_DIFFERENT_EXAMPLE'})
        restored = mon._status_result_from_payload(mon._status_result_payload(value))
        self.assertEqual(restored.efst_codes, value.efst_codes)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            cache = folder / 'cache.json'
            mon._write_status_cache(cache, folder, restored, strict_errors=True)
            cached = mon._read_status_cache(cache, folder, [])
            self.assertIsNotNone(cached)
            self.assertEqual(cached.efst_codes, value.efst_codes)
            mon.set_status_data(cached.names, cached.groups, cached.source, efst_codes=cached.efst_codes)
            self.assertEqual(mon.status_group(1150), '未分類')

    def test_unreviewed_baseline_identity_is_checked_and_recovers_without_new_label(self):
        self.assertNotIn('1',mon.STATUS_EFFECT_REVIEWS.records)
        mon.set_status_data({1:'合成替代狀態'},{1:'增益'},'synthetic',
                            efst_codes={1:'EFST_SYNTHETIC_REPLACEMENT'})
        self.assertEqual(mon.status_group(1),'未分類')
        record=mon.RUNTIME_STATUS_METADATA[1]
        self.assertEqual(record['efst_code'],'EFST_SYNTHETIC_REPLACEMENT')
        self.assertEqual(record['functional_category'],'其他／待確認')
        self.assertFalse(record['has_skill_link'])
        self.assertEqual(record['review_status'],'代碼變動，待重新核對')
        mon.merge_status_data_delta(mon.StatusDataResult({}, {1:'增益'},'synthetic','',[]))
        self.assertEqual(mon.status_group(1),'未分類')
        mon.merge_status_data_delta(mon.StatusDataResult({}, {},'synthetic','',[],{1:'EFST_ENDURE'}))
        self.assertEqual(mon.EFST_NAMES[1],mon.BUNDLED_EFST_NAMES[1])
        self.assertEqual(mon.status_group(1),mon.BUNDLED_EFST_GROUPS[1])
        self.assertTrue(mon.RUNTIME_STATUS_METADATA[1]['has_skill_link'])

    def test_code_only_replacement_does_not_reuse_old_bundled_name(self):
        mon.set_status_data({}, {},'synthetic',efst_codes={1:'EFST_SYNTHETIC_REPLACEMENT'})
        self.assertEqual(mon.EFST_NAMES[1],'EFST_SYNTHETIC_REPLACEMENT')
        self.assertEqual(mon.status_name(1),'未確認狀態 1')
        mon.merge_status_data_delta(mon.StatusDataResult({}, {},'synthetic','',[],{1:'EFST_SECOND_REPLACEMENT'}))
        self.assertEqual(mon.EFST_NAMES[1],'EFST_SECOND_REPLACEMENT')
        self.assertEqual(mon.status_group(1),'未分類')

    def test_partial_bundle_update_keeps_missing_ids_and_accepts_new_id(self):
        mon.set_status_data({65001:'新增示範狀態'},{65001:'未分類'},'synthetic',
                            efst_codes={65001:'EFST_SYNTHETIC_NEW'})
        self.assertEqual(mon.status_name(65001),'新增示範狀態')
        self.assertEqual(mon.status_group(65001),'未分類')
        self.assertEqual(mon.status_group(13),'減益')
        self.assertEqual(mon.status_group(78),'增益')
        self.assertFalse(any(c.status_id==65001 for c in mon.STATUS_REVIEW_CONFLICTS))

    def test_old_or_incomplete_code_cache_is_rejected_without_changing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); path=folder/'cache.json'
            common={'game_id':mon.TWRO_GAME_ID,'ro_dir':str(folder.resolve()),'signatures':[],
                    'names':{'13':'敏捷降低'},'groups':{'13':'增益'}}
            cases=[{'version':3}, {'version':4}, {'version':4,'efst_codes':{}},
                   {'version':4,'efst_codes':{'14':'EFST_OTHER'}},
                   {'version':4,'efst_codes':{'13':None}}]
            for case in cases:
                with self.subTest(case=case):
                    path.write_text(json.dumps({**common,**case}),encoding='utf-8')
                    before=path.read_bytes()
                    self.assertIsNone(mon._read_status_cache(path,folder,[]))
                    self.assertEqual(path.read_bytes(),before)
            path.write_text('[]',encoding='utf-8')
            self.assertIsNone(mon._read_status_cache(path,folder,[]))

    def test_cache_v4_accepts_id_zero_and_valid_unknown_code(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); path=folder/'cache.json'
            value=mon.StatusDataResult({0:'挑釁',65001:'新增示範狀態'},
                {0:'增益',65001:'未分類'},'synthetic','',[],
                {0:'EFST_PROVOKE',65001:'EFST_SYNTHETIC_NEW'})
            mon._write_status_cache(path,folder,value,strict_errors=True)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'],4)
            restored=mon._read_status_cache(path,folder,[])
            self.assertIsNotNone(restored)
            self.assertEqual(restored.efst_codes,value.efst_codes)

    def test_initial_import_neutralizes_conflicting_review_identity(self):
        root=Path(mon.__file__).resolve().parent
        program='''
import json
from catalog import effect_reviews as er
original=er.load_effect_reviews()
codes=dict(original.code_map); codes[13]='EFST_SYNTHETIC_REPLACEMENT'
er.load_effect_reviews=lambda path=None: er.EffectReviews(original.records,codes)
import rrf_monitor as m
print(json.dumps({'group':m.status_group(13),'marker':m.RUNTIME_STATUS_METADATA[13].get('review_status'),'name':m.status_name(13)},ensure_ascii=False))
'''
        result=subprocess.run([sys.executable,'-B','-c',program],cwd=root,
            capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        actual=json.loads(result.stdout)
        self.assertEqual(actual['group'],'未分類')
        self.assertEqual(actual['marker'],'代碼變動，待重新核對')
        self.assertEqual(actual['name'],'未確認狀態 13')


if __name__ == '__main__':
    unittest.main()
