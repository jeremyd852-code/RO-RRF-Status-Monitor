"""Synthetic review persistence and bounded catalog evidence checks; no game I/O."""
from __future__ import annotations
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog.effect_reviews import EffectReviews, load_effect_reviews, load_efst_codes
from catalog.repository import CatalogRepository

DATA = ROOT / 'data'


def read(name):
    return json.loads((DATA / name).read_text(encoding='utf-8-sig'))


class ReviewPersistenceTests(unittest.TestCase):
    def test_id_zero_is_valid_and_unknown_new_id_is_preserved(self):
        review = EffectReviews({'0': {'efst_code':'EFST_ZERO','effect_group':'開關／特殊'}}, {0:'EFST_ZERO'})
        names, groups = {0:'示範',65001:'新狀態'}, {0:'增益',65001:'未分類'}
        self.assertEqual(review.apply(names,groups), ())
        self.assertEqual(groups, {0:'開關／特殊',65001:'未分類'})
        self.assertEqual(names[65001], '新狀態')

    def test_code_conflict_is_isolated_and_does_not_rename_wrong_state(self):
        review = EffectReviews({'1':{'efst_code':'EFST_OLD','effect_group':'減益','display_name_override':'舊狀態'},
                                '2':{'efst_code':'EFST_VALID','effect_group':'減益'}},
                               {1:'EFST_CHANGED',2:'EFST_VALID'})
        names,groups={1:'新狀態',3:'新ID'}, {1:'未分類',2:'增益',3:'未分類'}
        conflicts=review.apply(names,groups)
        self.assertEqual([(c.status_id,c.reason) for c in conflicts],[(1,'efst_code_mismatch')])
        self.assertEqual(names[1],'新狀態')
        self.assertEqual(groups,{1:'未分類',2:'減益',3:'未分類'})

    def test_incoming_metadata_code_conflict_blocks_all_reviewed_fields(self):
        review=EffectReviews({'1':{'efst_code':'EFST_A','effect_group':'減益',
            'functional_category':'負面／異常狀態','display_name_override':'已覆核'}},{1:'EFST_A'})
        metadata={1:{'efst_code':'EFST_B','name':'新狀態','functional_category':'其他／待確認'}}
        before=deepcopy(metadata)
        names,groups={1:'新狀態'},{1:'未分類'}
        self.assertEqual(review.apply(names,groups,metadata)[0].reason,'metadata_code_mismatch')
        self.assertEqual(metadata,before)
        self.assertEqual((names[1],groups[1]),('新狀態','未分類'))

    def test_invalid_row_does_not_block_valid_review(self):
        review=EffectReviews({'bad':{},'1':{'efst_code':'EFST_A','effect_group':[]},
            '2':{'efst_code':'EFST_B','effect_group':'減益'}},{1:'EFST_A',2:'EFST_B'})
        names,groups={},{}
        conflicts=review.apply(names,groups)
        self.assertEqual(len(conflicts),2)
        self.assertEqual(groups,{2:'減益'})

    def test_apply_performs_no_file_io_and_is_idempotent(self):
        reviews=load_effect_reviews(DATA)
        names,groups,metadata={}, {13:'增益'}, deepcopy(read('status_catalog.json')['records'])
        with patch.object(Path,'read_text',side_effect=AssertionError('must be cached')):
            self.assertFalse(reviews.apply(names,groups,metadata))
            once=deepcopy((names,groups,metadata))
            self.assertFalse(reviews.apply(names,groups,metadata))
        self.assertEqual((names,groups,metadata),once)

    def test_full_delta_and_reload_preserve_name_purpose_effect_and_aliases(self):
        reviews=load_effect_reviews(DATA)
        records=read('status_catalog.json')['records']
        name_id=next(int(k) for k,r in reviews.records.items() if r.get('display_name_override'))
        expected=reviews.records[str(name_id)]['display_name_override']
        for cycle in range(3):
            names={name_id:'不完整客戶端文字',65001:'新狀態'}
            groups={13:'增益',78:'主要監控',1150:'增益',65001:'未分類'}
            metadata=deepcopy(records)
            metadata['1150']['functional_category']='防禦／減傷／迴避'
            if cycle==2:
                reviews=load_effect_reviews(DATA)
            self.assertFalse(reviews.apply(names,groups,metadata))
            self.assertEqual(names[name_id],expected)
            self.assertEqual(groups[13],'減益')
            self.assertEqual(groups[78],'增益')
            self.assertEqual(groups[1150],'減益')
            self.assertEqual(groups[65001],'未分類')
            self.assertEqual(metadata['1150']['functional_category'],'負面／異常狀態')
            self.assertEqual(metadata[str(name_id)]['aliases'],records[str(name_id)]['aliases'])

    def test_repository_merge_and_replace_reapply_review_and_advance_generation(self):
        repo=CatalogRepository.from_data_dir(DATA)
        first=repo.snapshot()
        self.assertEqual(first.status_groups[13],'減益')
        merged=repo.merge(status_names={65001:'新狀態'},status_groups={13:'增益',65001:'未分類'})
        self.assertEqual(merged.generation, first.generation+1)
        self.assertEqual(merged.status_groups[13],'減益')
        self.assertEqual(merged.status_names[65001],'新狀態')
        replaced=repo.replace(status_names=merged.status_names,status_groups={13:'增益',65001:'未分類'},
            item_names=merged.item_names,pet_names=merged.pet_names,pet_food_item_ids=merged.pet_food_item_ids,source='synthetic')
        self.assertEqual(replaced.status_groups[13],'減益')
        self.assertEqual(replaced.status_groups[65001],'未分類')
        self.assertEqual(replaced.generation,merged.generation+1)

    def test_repository_default_constructor_keeps_review_protection(self):
        snapshot=CatalogRepository.from_data_dir(DATA).snapshot()
        repo=CatalogRepository(snapshot)
        self.assertEqual(repo.merge(status_groups={13:'增益'}).status_groups[13],'減益')

    def test_code_loader_rejects_ambiguous_identity_without_losing_other_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'EFSTIDs.lua').write_text('EFST_ZERO=0, EFST_A=1, EFST_B=1, EFST_OK=2',encoding='utf-8')
            self.assertEqual(load_efst_codes(root),{0:'EFST_ZERO',2:'EFST_OK'})

    def test_repository_code_conflict_stays_neutral_until_identity_is_restored(self):
        repo=CatalogRepository.from_data_dir(DATA)
        changed=repo.merge(status_names={13:'新的狀態'},status_groups={13:'增益'},
                           efst_codes={13:'EFST_DIFFERENT'})
        self.assertEqual(changed.status_groups[13],'未分類')
        self.assertTrue(changed.review_conflicts)
        self.assertEqual(repo.merge(status_groups={13:'增益'}).status_groups[13],'未分類')
        restored_snapshot=CatalogRepository(repo.snapshot())
        self.assertEqual(restored_snapshot.merge(status_groups={13:'增益'}).status_groups[13],'未分類')
        restored=repo.merge(efst_codes={13:'EFST_DEC_AGI'})
        self.assertEqual(restored.status_groups[13],'減益')
        self.assertFalse(restored.review_conflicts)

    def test_initial_code_conflict_does_not_reuse_bundled_reviewed_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('EFSTIDs.lua','status_catalog.json','client_catalog.json','status_reviews.json'):
                (root/name).write_bytes((DATA/name).read_bytes())
            path=root/'EFSTIDs.lua'
            path.write_text(path.read_text(encoding='utf-8-sig').replace('EFST_DEC_AGI','EFST_REPLACEMENT'),encoding='utf-8')
            repo=CatalogRepository.from_data_dir(root)
            self.assertEqual(repo.snapshot().status_groups[13],'未分類')
            self.assertTrue(repo.snapshot().review_conflicts)
            self.assertEqual(repo.merge(status_groups={13:'增益'}).status_groups[13],'未分類')


class AdoptedCatalogTests(unittest.TestCase):
    def test_all_a_decisions_have_meaningful_purpose(self):
        records=read('status_catalog.json')['records']
        expected={72:'移動／行動／施法',112:'其他／待確認',125:'其他／待確認',421:'負面／異常狀態',
            577:'防禦／減傷／迴避',893:'防禦／減傷／迴避',1033:'移動／行動／施法',1150:'負面／異常狀態',
            1217:'防禦／減傷／迴避',1242:'負面／異常狀態',1398:'消耗品／攻速／移速',1463:'移動／行動／施法'}
        for sid,category in expected.items():
            with self.subTest(id=sid):
                self.assertEqual(records[str(sid)]['functional_category'],category)
                self.assertTrue(records[str(sid)]['purpose_basis'])

    def test_b_negative_effects_preserve_original_client_color_evidence(self):
        reviews=read('status_reviews.json')['statuses']
        records=read('status_catalog.json')['records']
        for sid in (13,373,401,421,438,450,452,453,476,1212,1242,1259,1260):
            with self.subTest(id=sid):
                self.assertEqual(records[str(sid)]['effect_group'],'減益')
                self.assertEqual(reviews[str(sid)]['raw_title_color'],'COLOR_TITLE_BUFF')
                self.assertEqual(reviews[str(sid)]['raw_effect_group'],'增益')
                self.assertTrue(reviews[str(sid)]['client_descriptions'])

    def test_mixed_effects_are_special_and_insufficient_evidence_stays_unknown(self):
        reviews=load_effect_reviews(DATA)
        groups={}
        reviews.apply({},groups)
        for sid in (0,368,403,407,583,659,662,1203):
            self.assertEqual(groups[sid],'開關／特殊')
        for sid in (107,112):
            self.assertEqual(groups[sid],'未分類')
        self.assertEqual(groups[125],'減益')

    def test_focus_is_experience_benefit_not_ui_category(self):
        review=read('status_reviews.json')['statuses']['78']
        self.assertEqual(review['efst_code'],'EFST_RICHMANKIM')
        self.assertEqual(review['effect_group'],'增益')
        self.assertIn('獲得之經驗值增加',review['client_descriptions'])

    def test_1150_has_recipient_evidence_and_cross_region_limit(self):
        review=read('status_reviews.json')['statuses']['1150']
        self.assertEqual(review['effect_group'],'減益')
        self.assertEqual(review['evidence_sources'][0]['region'],'jRO')
        self.assertIn('不宣稱台版實測',review['effect_note'])

    def test_runtime_index_matches_reviewed_metadata_and_retains_all_names(self):
        index=read('runtime_status_index.json')
        self.assertEqual(len(index['names']),1462)
        self.assertEqual(index.get('review_conflicts'),[])
        for sid,review in read('status_reviews.json')['statuses'].items():
            if 'effect_group' in review:
                self.assertEqual(index['groups'][sid],review['effect_group'])

    def test_builder_applies_reviews_last_to_actual_generated_file(self):
        spec=importlib.util.spec_from_file_location('effect_index_builder',ROOT/'build_tools/build_runtime_status_index.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'data').mkdir()
            for name in ('status_reviews.json','EFSTIDs.lua'):
                (root/'data'/name).write_bytes((DATA/name).read_bytes())
            with patch.object(module,'ROOT',root), patch.object(module.rrf_monitor,'_load_twro_status_names',
                    return_value=({13:'敏捷降低',78:'經驗值倍增',65001:'新狀態'},{13:'增益',78:'主要監控',65001:'未分類'},'synthetic')):
                self.assertEqual(module.main(),0)
            result=json.loads((root/'data/runtime_status_index.json').read_text(encoding='utf-8'))
            self.assertEqual(result['groups']['13'],'減益')
            self.assertEqual(result['groups']['78'],'增益')
            self.assertEqual(result['groups']['65001'],'未分類')
            codes=root/'data/EFSTIDs.lua'
            codes.write_text(codes.read_text(encoding='utf-8-sig').replace('EFST_DEC_AGI','EFST_REPLACEMENT'),encoding='utf-8')
            with patch.object(module,'ROOT',root), patch.object(module.rrf_monitor,'_load_twro_status_names',
                    return_value=({13:'新的狀態',65001:'新ID'},{13:'增益',65001:'未分類'},'synthetic')):
                self.assertEqual(module.main(),0)
            changed=json.loads((root/'data/runtime_status_index.json').read_text(encoding='utf-8'))
            self.assertEqual(changed['groups']['13'],'未分類')
            self.assertEqual(changed['groups']['78'],'增益')
            self.assertEqual(changed['groups']['65001'],'未分類')
            self.assertTrue(any(c['status_id']==13 for c in changed['review_conflicts']))


if __name__ == '__main__':
    unittest.main()
