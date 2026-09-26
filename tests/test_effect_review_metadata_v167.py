"""Review metadata creation and provenance preservation; no GUI or game I/O."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog.effect_reviews import EffectReviews


class ReviewMetadataTests(unittest.TestCase):
    def test_missing_json_record_preserves_verified_fields_and_provenance(self):
        row = {
            "efst_code": "EFST_REVIEWED", "display_name_override": "覆核名稱",
            "effect_group": "未分類", "functional_category": "其他／待確認",
            "effect_basis": "僅確認名稱", "effect_note": "效果待確認",
            "aliases": ["EFST_REVIEWED", "舊稱", "覆核名稱"],
            "source_evidence": [{"archive": "data.grf", "sha256": "synthetic"}],
            "evidence_sources": [{"region": "reference", "scope": "name only"}],
            "client_descriptions": ["原文"], "client_skill_descriptions": ["技能原文"],
            "name_basis": "verified identity", "name_note": "個別核對",
            "source_limit": "不套用外部數值", "review_status": "reviewed",
            "raw_effect_group": "增益", "raw_title_color": None,
        }
        reviews = EffectReviews({"43": row}, {43: "EFST_REVIEWED"})
        names, groups, metadata = {}, {43: "增益"}, {}
        self.assertEqual(reviews.apply(names, groups, metadata), ())
        actual = metadata["43"]
        self.assertEqual((actual["id"], actual["efst_code"], actual["name"], actual["effect_group"]),
                         (43, "EFST_REVIEWED", "覆核名稱", "未分類"))
        self.assertEqual(actual["aliases"], ["EFST_REVIEWED", "舊稱"])
        for field in ("functional_category", "source_evidence", "evidence_sources", "client_descriptions",
                      "client_skill_descriptions", "name_basis", "name_note", "source_limit", "raw_title_color"):
            self.assertEqual(actual[field], row[field])
        self.assertEqual((names[43], groups[43]), ("覆核名稱", "未分類"))
        for invented_field in ("has_skill_link", "skill_names", "item_names", "source_tags"):
            self.assertNotIn(invented_field, actual)
        actual["source_evidence"][0]["archive"] = "changed"
        self.assertEqual(row["source_evidence"][0]["archive"], "data.grf")

    def test_missing_record_uses_existing_integer_key_style(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "名稱"}}, {43: "EFST_A"})
        metadata = {99: {"name": "保留"}}
        self.assertEqual(reviews.apply({}, {}, metadata), ())
        self.assertIn(43, metadata)
        self.assertNotIn("43", metadata)
        self.assertEqual(metadata[43]["effect_group"], "未分類")
        self.assertEqual(metadata[99], {"name": "保留"})

    def test_effect_only_record_keeps_available_name_without_inventing_one(self):
        reviews = EffectReviews({"717": {"efst_code": "EFST_A", "effect_group": "增益"}}, {717: "EFST_A"})
        for original_names, expected_name in (({717: "已知名稱"}, "已知名稱"), ({}, "EFST_A")):
            with self.subTest(names=original_names):
                names, groups, metadata = dict(original_names), {}, {}
                self.assertEqual(reviews.apply(names, groups, metadata), ())
                self.assertEqual(names, original_names)
                self.assertEqual(metadata["717"]["name"], expected_name)
                self.assertEqual(metadata["717"]["effect_group"], "增益")
                self.assertNotIn("display_name_override", metadata["717"])

    def test_name_only_record_keeps_existing_group_and_names_only_api(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "名稱"}}, {43: "EFST_A"})
        names, groups, metadata = {}, {43: "開關／特殊"}, {}
        self.assertEqual(reviews.apply(names, groups, metadata), ())
        self.assertEqual(metadata["43"]["effect_group"], "開關／特殊")
        plain_names, plain_groups = {}, {43: "開關／特殊", 99: "未分類"}
        self.assertEqual(reviews.apply(plain_names, plain_groups), ())
        self.assertEqual(plain_names, {43: "名稱"})
        self.assertEqual(plain_groups, {43: "開關／特殊", 99: "未分類"})

    def test_existing_metadata_keeps_links_and_unions_aliases(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "新名稱",
                                       "aliases": ["新別名"], "source_limit": "僅名稱"}}, {43: "EFST_A"})
        metadata = {43: {"efst_code": "EFST_A", "name": "新名稱", "aliases": ["舊別名"],
                         "has_skill_link": True, "skill_names": ["已有技能"], "item_names": ["已有物品"],
                         "source_tags": ["已有來源"], "source_evidence": [{"old": "evidence"}]}}
        self.assertEqual(reviews.apply({}, {}, metadata), ())
        self.assertEqual(metadata[43]["aliases"], ["新別名", "舊別名"])
        self.assertEqual(metadata[43]["skill_names"], ["已有技能"])
        self.assertEqual(metadata[43]["item_names"], ["已有物品"])
        self.assertEqual(metadata[43]["source_tags"], ["已有來源"])
        self.assertEqual(metadata[43]["source_evidence"], [{"old": "evidence"}])

    def test_alias_only_update_can_preserve_an_existing_record(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "aliases": ["另一名稱"]}}, {43: "EFST_A"})
        metadata = {"43": {"efst_code": "EFST_A", "name": "名稱", "aliases": ["原別名"]}}
        self.assertEqual(reviews.apply({}, {}, metadata), ())
        self.assertEqual(set(metadata["43"]["aliases"]), {"原別名", "另一名稱"})

    def test_notes_alone_do_not_create_unconfirmed_metadata(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "effect_note": "尚無決定"}}, {43: "EFST_A"})
        names, groups, metadata = {}, {}, {}
        self.assertEqual(reviews.apply(names, groups, metadata), ())
        self.assertEqual((names, groups, metadata), ({}, {}, {}))

    def test_invalid_review_fields_never_partially_apply(self):
        bad_values = {
            "effect_group": None, "functional_category": [], "aliases": "bad", "name_note": {},
            "source_evidence": ["not an evidence object"], "source_limit": [],
            "client_descriptions": [1], "client_skill_descriptions": {}, "raw_title_color": 1,
        }
        for field, value in bad_values.items():
            with self.subTest(field=field):
                row = {"efst_code": "EFST_A", "display_name_override": "新名稱", "effect_group": "減益", field: value}
                reviews = EffectReviews({"43": row}, {43: "EFST_A"})
                names, groups, metadata = {43: "原名稱"}, {43: "增益"}, {}
                before = deepcopy((names, groups, metadata))
                self.assertEqual(len(reviews.apply(names, groups, metadata)), 1)
                self.assertEqual((names, groups, metadata), before)

    def test_external_or_existing_metadata_identity_conflict_does_not_write(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "覆核名稱", "effect_group": "減益"}}, {43: "EFST_A"})
        for codes, metadata in (({43: "EFST_OTHER"}, {}), ({43: "EFST_A"}, {43: {"efst_code": "EFST_OTHER"}})):
            with self.subTest(codes=codes, metadata=metadata):
                names, groups = {43: "現行名稱"}, {43: "未分類"}
                before = deepcopy((names, groups, metadata))
                self.assertEqual(len(reviews.apply(names, groups, metadata, codes)), 1)
                self.assertEqual((names, groups, metadata), before)

    def test_malformed_existing_metadata_does_not_partially_change_names(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "新名稱"}}, {43: "EFST_A"})
        names, groups, metadata = {43: "原名稱"}, {}, {"43": []}
        before = deepcopy((names, groups, metadata))
        self.assertEqual(reviews.apply(names, groups, metadata)[0].reason, "invalid_metadata_record")
        self.assertEqual((names, groups, metadata), before)

    def test_creation_is_idempotent_and_performs_no_file_io(self):
        reviews = EffectReviews({"43": {"efst_code": "EFST_A", "display_name_override": "名稱",
                                       "effect_group": "未分類", "source_evidence": [{"source": "checked"}]}}, {43: "EFST_A"})
        names, groups, metadata = {}, {}, {}
        with patch.object(Path, "read_text", side_effect=AssertionError("No file I/O during apply")):
            self.assertEqual(reviews.apply(names, groups, metadata), ())
            first = deepcopy((names, groups, metadata))
            self.assertEqual(reviews.apply(names, groups, metadata), ())
        self.assertEqual((names, groups, metadata), first)


if __name__ == "__main__":
    unittest.main()
