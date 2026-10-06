import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from housing_register import compile_register, housing_parameters, signature
from build_density import proxy_parameters, allocate, deduplicate_footprints


class HousingRegisterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'source.json').write_text('{}')
        self.feature = {'properties': {'osm_type': 'way', 'osm_id': 1, 'tags': {'building': 'yes', 'addr:housenumber': '1'}},
                        'geometry': {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
        fields = {n: {'status': 'unknown', 'value': None, 'source_ids': []} for n in ['use', 'above_ground_storeys', 'residential_premises_area_m2', 'apartments']}
        self.record = {'osm_id': 'way/1', 'settlement': 'Тест', 'address': 'Тест, 1', 'geometry_sha256': signature(self.feature),
                       'expected_tags': copy.deepcopy(self.feature['properties']['tags']), 'match': {'status': 'confirmed', 'reviewer': 'test', 'reviewed_at': '2026-10-05', 'reason': 'verified contour', 'source_ids': ['official']}, 'fields': fields}
        self.register = {'schema_version': 1, 'sources': {'official': {'kind': 'primary', 'snapshot': 'source.json', 'sha256': hashlib.sha256(b'{}').hexdigest(), 'url': 'https://example.org/register'}}, 'buildings': [self.record]}

    def confirm(self, name, value):
        self.record['fields'][name] = {'status': 'confirmed', 'value': value, 'source_ids': ['official'], 'locator': 'row 1', 'reviewer': 'test', 'reviewed_at': '2026-10-05', 'as_of': '2025-01-01'}

    def compile(self):
        return compile_register(self.register, [self.feature], self.root)[0]

    def test_candidates_reported_conflicts_and_unknowns_cannot_change_model(self):
        self.confirm('use', 'residential')
        self.record['match']['status'] = 'candidate'
        self.assertEqual(self.compile(), {})
        self.record['match']['status'] = 'confirmed'
        for status, value in [('reported', 5), ('conflicting', None), ('unknown', None)]:
            self.record['fields']['above_ground_storeys'] = {'status': status, 'value': value, 'source_ids': ['official'], 'locator': 'row 1'}
            self.assertEqual(self.compile(), {'way/1': {'use': 'residential'}})

    def test_secondary_source_cannot_confirm_facts_or_geometry(self):
        self.confirm('use', 'residential')
        self.register['sources']['official']['kind'] = 'secondary'
        with self.assertRaises(ValueError): self.compile()
        self.record['match']['status'] = 'candidate'
        with self.assertRaises(ValueError): self.compile()

    def test_stale_source_tags_geometry_and_missing_ids_fail(self):
        originals = copy.deepcopy(self.feature)
        self.feature['properties']['tags']['addr:housenumber'] = '2'
        with self.assertRaises(ValueError): self.compile()
        self.feature = originals
        self.feature['geometry']['coordinates'][0][1][0] = 2
        with self.assertRaises(ValueError): self.compile()
        self.feature = copy.deepcopy(originals)
        (self.root / 'source.json').write_text('{"changed":true}')
        with self.assertRaises(ValueError): self.compile()

    def test_positive_area_requires_residential_use_and_whole_contour_scope(self):
        self.confirm('residential_premises_area_m2', 600)
        with self.assertRaises(ValueError): self.compile()
        self.confirm('use', 'mixed')
        with self.assertRaises(ValueError): self.compile()
        self.record['fields']['residential_premises_area_m2']['scope'] = 'whole_contour_residential_premises'
        self.assertEqual(self.compile()['way/1']['residential_premises_area_m2'], 600)
        for value in [0, -1, float('nan'), True]:
            self.record['fields']['residential_premises_area_m2']['value'] = value
            with self.assertRaises(ValueError): self.compile()

    def test_area_precedence_intersection_allocation_and_population_conservation(self):
        approved = {'use': 'mixed', 'residential_premises_area_m2': 600, 'apartments': 60}
        params = housing_parameters(self.feature['properties']['tags'], approved, 200, proxy_parameters)
        self.assertEqual(params, (3., True, False, True, False))
        weights = {(0, 0, 1): 50 * params[0], (1, 0, 1): 150 * params[0]}
        self.assertEqual(sum(weights.values()), 600)
        self.assertEqual(allocate(100, weights), {(0, 0, 1): 25000, (1, 0, 1): 75000})

    def test_apartments_never_become_area_or_population(self):
        self.confirm('apartments', 60)
        self.assertEqual(self.compile(), {})
        tags = self.feature['properties']['tags']
        self.assertEqual(housing_parameters(tags, {'apartments': 60}, 200, proxy_parameters), (.55, False, False, False, False))

    def test_confirmed_use_does_not_invent_five_storeys(self):
        tags = self.feature['properties']['tags']
        self.assertEqual(housing_parameters(tags, {'use': 'residential'}, 200, proxy_parameters), (1., True, False, False, False))
        self.assertEqual(housing_parameters(tags, {'use': 'residential', 'above_ground_storeys': 5}, 200, proxy_parameters), (5., True, False, False, True))
        self.assertIsNone(housing_parameters(tags, {'use': 'non_residential'}, 200, proxy_parameters))

    def test_maximum_storeys_need_whole_contour_check(self):
        self.confirm('use', 'residential')
        self.confirm('above_ground_storeys', 5)
        with self.assertRaises(ValueError): self.compile()
        self.record['fields']['above_ground_storeys']['scope'] = 'uniform_above_ground_whole_contour'
        self.assertEqual(self.compile()['way/1']['above_ground_storeys'], 5)
        self.record['fields']['above_ground_storeys']['value'] = 100
        with self.assertRaises(ValueError): self.compile()

    def test_duplicate_contours_with_different_verified_area_are_excluded(self):
        second = copy.deepcopy(self.feature); second['properties']['osm_id'] = 2
        selected, report = deduplicate_footprints([self.feature, second], {'way/1': {'use': 'residential', 'residential_premises_area_m2': 600}})
        self.assertEqual(selected, [])
        self.assertEqual(len(report['conflicting_duplicate_footprints_excluded']), 1)


if __name__ == '__main__': unittest.main()
