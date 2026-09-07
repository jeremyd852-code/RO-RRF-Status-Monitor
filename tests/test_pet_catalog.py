"""Portable pet-catalog regressions; synthetic fixtures and bundled facts only."""
from pathlib import Path
from dataclasses import FrozenInstanceError
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog import pets

class PetCatalogTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='pet-catalog-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def fixture(self, rows, **metadata):
        path = self.root / 'pet_catalog.json'
        payload = {'schema_version': 1, 'game_id': 'twro', 'pets': rows, **metadata}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def test_known_species_food_relationships(self):
        catalog = pets.PetCatalog(ROOT / 'data')
        self.assertIsNone(catalog.error)
        for species_id, name, food_id, food_name in ((1002, '波利', 531, '蘋果汁'), (1101, '小巴風特', 518, '蜂蜜'), (20571, '獸人英雄', 25377, '高級寵物飼料')):
            entry = catalog.lookup(species_id)
            self.assertEqual((entry.name_zh, entry.food_item_id, entry.food_name_zh), (name, food_id, food_name))
            self.assertEqual(entry.food_source, 'petinfo.PetFoodTable')
            self.assertEqual(entry.missing_fields, frozenset())

    def test_missing_fields_do_not_become_english_species_or_guessed_food(self):
        entry = pets.get_pet_info(2336)
        self.assertEqual(entry.missing_fields, {'name_zh', 'food_item_id', 'food_name_zh'})
        placeholder = pets.get_pet_info(2200)
        self.assertIsNone(placeholder.name_zh)
        self.assertEqual(placeholder.food_name_zh, '蘋果')
        missing_food_name = pets.get_pet_info(1630)
        self.assertEqual(missing_food_name.food_item_id, 6094)
        self.assertIsNone(missing_food_name.food_name_zh)
        self.assertEqual(missing_food_name.missing_fields, {'food_name_zh'})

    def test_bundled_counts_and_source_provenance(self):
        raw = (ROOT / 'data/pet_catalog.json').read_bytes()
        payload = json.loads(raw)
        catalog = pets.PetCatalog()
        self.assertEqual(len(catalog), 115)
        self.assertEqual(sum((entry.name_zh is not None for entry in catalog.entries.values())), 99)
        self.assertEqual(sum((entry.food_item_id is not None for entry in catalog.entries.values())), 114)
        self.assertEqual(sum((entry.food_name_zh is not None for entry in catalog.entries.values())), 113)
        self.assertEqual(len({entry.food_item_id for entry in catalog.entries.values()} - {None}), 58)
        self.assertEqual(payload['sources']['petinfo']['sha256'], '93742569d80026b4bceba4bf2982a29c5a4a52c30a7f7ce435231a1426efa620')
        for separator in (47, 92):
            marker = b'c:' + bytes((separator,)) + b'users' + bytes((separator,))
            self.assertNotIn(marker, raw.lower())
        self.assertNotIn(b'NONAME', json.dumps(payload['pets']).encode('ascii'))

    def test_unknown_or_invalid_id_returns_none(self):
        for value in (None, True, False, 0, -1, 1002.1, '1002.0', '0x3ea', '１００２', [], {}, 2 ** 32, 999999):
            with self.subTest(value=value):
                self.assertIsNone(pets.get_pet_info(value))
        self.assertEqual(pets.get_pet_info('1002').species_id, 1002)

    def test_missing_or_corrupt_file_is_safe_empty_catalog(self):
        catalog = pets.PetCatalog(self.root / 'missing.json')
        self.assertEqual(len(catalog), 0)
        self.assertIsNotNone(catalog.error)
        path = self.root / 'broken.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual(len(pets.PetCatalog(path)), 0)

    def test_wrong_game_or_schema_is_not_used(self):
        for metadata in ({'game_id': 'zero'}, {'schema_version': 2}):
            catalog = pets.PetCatalog(self.fixture({'1002': {'name_zh': '波利'}}, **metadata))
            self.assertEqual(len(catalog), 0)
            self.assertIsNotNone(catalog.error)

    def test_localized_names_and_food_ids_are_validated_independently(self):
        catalog = pets.PetCatalog(self.fixture({'1002': {'name_zh': 'poring', 'food_item_id': 531, 'food_name_zh': '蘋果汁'}, '1003': {'name_zh': '中文種類', 'food_item_id': True, 'food_name_zh': '不應顯示'}, '1004': {'name_zh': '名稱\n換行', 'food_item_id': -1, 'food_name_zh': '不可用'}, 'oops': {'name_zh': '無效'}}))
        self.assertIsNone(catalog.lookup(1002).name_zh)
        self.assertEqual(catalog.lookup(1002).food_name_zh, '蘋果汁')
        self.assertIsNone(catalog.lookup(1003).food_item_id)
        self.assertIsNone(catalog.lookup(1003).food_name_zh)
        self.assertIsNone(catalog.lookup(1004).name_zh)
        self.assertEqual(len(catalog), 3)

    def test_entries_cannot_be_mutated_and_no_nickname_is_stored(self):
        catalog = pets.PetCatalog()
        with self.assertRaises(TypeError):
            catalog.entries[1002] = None
        with self.assertRaises(FrozenInstanceError):
            catalog.lookup(1002).name_zh = '暱稱'
        self.assertFalse(hasattr(catalog.lookup(1002), 'nickname'))

    def test_explicit_path_and_cache_are_supported(self):
        path = self.fixture({'1002': {'name_zh': '測試種類'}})
        self.assertEqual(pets.get_pet_info(1002, path).name_zh, '測試種類')
        self.assertIs(pets.load_pet_catalog(path), pets.load_pet_catalog(path))
        pets.load_pet_catalog.cache_clear()

    def test_frozen_portable_data_outside_internal_directory(self):
        module_root = self.root / '_internal/catalog'
        module_root.mkdir(parents=True)
        data_root = self.root / 'data'
        data_root.mkdir()
        target = data_root / 'pet_catalog.json'
        target.write_text('{}', encoding='utf-8')
        with patch.object(pets, '__file__', str(module_root / 'pets.py')), patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(self.root / 'monitor.exe')):
            self.assertEqual(pets._catalog_path(None), target)
if __name__ == '__main__':
    unittest.main(verbosity=2)
