"""Review materialization survives rebuilding a sparse/raw status catalog."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_tools.build_reviewed_status_catalog import build_reviewed_catalog
from catalog.effect_reviews import EffectReviews, load_effect_reviews


class ReviewedBuildTests(unittest.TestCase):
    def test_raw_color_and_missing_rows_are_fixed_before_statistics(self):
        raw = {'game_id': 'twro', 'records': {
            '125': {'id': 125, 'efst_code': 'EFST_JOINTBEAT', 'name': '巧打', 'effect_group': '增益'},
        }}
        before = deepcopy(raw)
        result = build_reviewed_catalog(raw, load_effect_reviews(ROOT / 'data'))
        self.assertEqual(raw, before)
        self.assertEqual(result['records']['125']['effect_group'], '減益')
        self.assertEqual(result['records']['715']['name'], '豐年頌')
        self.assertEqual(result['records']['49']['effect_group'], '未分類')
        self.assertTrue(result['records']['715']['source_evidence'])
        self.assertNotIn('1414', result['records'])
        self.assertEqual(result['statistics']['record_count'], len(result['records']))
        self.assertEqual(sum(result['statistics']['effect_group_counts'].values()), len(result['records']))

    def test_identity_conflict_prevents_materializing_wrong_names(self):
        review = EffectReviews({'1': {'efst_code': 'EFST_OLD', 'display_name_override': '舊名'}}, {1: 'EFST_NEW'})
        raw = {'game_id': 'twro', 'records': {}}
        with self.assertRaises(ValueError):
            build_reviewed_catalog(raw, review)
        self.assertEqual(raw['records'], {})

    def test_repeated_build_keeps_catalog_content_and_signature(self):
        review = EffectReviews({'1': {'efst_code': 'EFST_A', 'display_name_override': '已確認', 'effect_group': '未分類'}}, {1: 'EFST_A'})
        first = build_reviewed_catalog({'game_id': 'twro', 'records': {}}, review)
        again = build_reviewed_catalog(first, review)
        first.pop('built_at')
        again.pop('built_at')
        self.assertEqual(first, again)


if __name__ == '__main__':
    unittest.main()
