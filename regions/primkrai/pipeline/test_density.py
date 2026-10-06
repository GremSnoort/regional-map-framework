import unittest
from pathlib import Path

from build_density import allocate, coarsen, proxy_parameters, deduplicate_footprints, apply_building_review
from population_sources import controls, official_match


class DensityTest(unittest.TestCase):
    def test_addressed_school_and_hospital_are_not_residential(self):
        for amenity in ["school", "kindergarten", "hospital", "place_of_worship", "fuel"]:
            self.assertIsNone(proxy_parameters({"building": "yes", "addr:housenumber": "6", "amenity": amenity}))
        # A facility on a mixed-use residential contour cannot cancel all homes.
        self.assertEqual(proxy_parameters({"building": "apartments", "amenity": "hospital", "building:levels": "5"}), (5., True, True))

    def test_manual_exclusion_fails_on_changed_snapshot_or_missing_id(self):
        feature = {"properties": {"osm_type": "way", "osm_id": 1, "tags": {"building": "yes", "addr:housenumber": "453"}}}
        review = {"exclusions": [{"osm_id": "way/1", "expected_tags": {"addr:housenumber": "453"}, "source_url": "https://www.educhernigovka.ru/node/41", "reason": "school"}]}
        self.assertEqual(apply_building_review([feature], review), ([], ["way/1"]))
        with self.assertRaises(ValueError):
            apply_building_review([], review)
        feature["properties"]["tags"]["addr:housenumber"] = "454"
        with self.assertRaises(ValueError):
            apply_building_review([feature], review)

    def test_duplicate_geometries_do_not_double_proxy_or_hide_tag_conflicts(self):
        def feature(identifier, kind):
            return {"type": "Feature", "properties": {"osm_type": "way", "osm_id": identifier, "tags": {"building": kind}},
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}
        selected, report = deduplicate_footprints([feature(2, "house"), feature(1, "house")])
        self.assertEqual([f["properties"]["osm_id"] for f in selected], [1])
        self.assertEqual(report["exact_duplicate_objects_removed"], 1)
        selected, report = deduplicate_footprints([feature(1, "apartments"), feature(2, "house")])
        self.assertEqual(selected, [])
        self.assertEqual(len(report["conflicting_duplicate_footprints_excluded"]), 1)

    def test_exact_fractional_control_and_deterministic_ties(self):
        weights = {(0, 0, 1): 1, (1, 0, 1): 1, (2, 0, 1): 1}
        self.assertEqual(allocate(1, weights), {(0, 0, 1): 334, (1, 0, 1): 333, (2, 0, 1): 333})
        self.assertEqual(sum(allocate(594201, weights).values()), 594201000)

    def test_coarsening_counts_unique_buildings_crossing_cells(self):
        rows = {(0, 0): {"proxy": 1., "explicit": 1., "observed_levels": 1., "ids": {"way/1"}},
                (1, 0): {"proxy": 3., "explicit": 0., "observed_levels": 0., "ids": {"way/1"}}}
        result = coarsen(rows, 400)
        self.assertEqual(list(result), [(0, 0, 4)])
        self.assertEqual(result[0, 0, 4]["proxy"], 4.)
        self.assertEqual(result[0, 0, 4]["ids"], {"way/1"})

    def test_residential_proxy_distinguishes_observed_and_assumed_storeys(self):
        self.assertIsNone(proxy_parameters({"building": "school", "addr:housenumber": "1"}))
        self.assertEqual(proxy_parameters({"building": "apartments", "building:levels": "9"}), (9., True, True))
        self.assertEqual(proxy_parameters({"building": "apartments", "building:levels": "nan"}), (5., True, False))
        self.assertEqual(proxy_parameters({"building": "yes", "addr:street": "Тест"}), (.55, False, False))

    def test_source_suffix_and_merged_municipal_context(self):
        source = Path(__file__).resolve().parents[1] / "sources"
        if not (source / "rosstat_vpn2020_table5.xlsx").exists():
            self.skipTest("Private Rosstat snapshots not installed")
        municipal, current, census = controls(source)
        self.assertEqual(sum(r["population"] for r in municipal.values()), 1799659)
        spassk = [{"municipality_name": "муниципальный округ Спасск-Дальний",
                   "official_name": "Спасск-Дальний городской округ + Спасский муниципальный район"}]
        record = official_match({"name": "Спасское", "district": "муниципальный округ Спасск-Дальний"}, spassk, current, census)
        self.assertEqual((record["population"], record["source_cell"]), (4418, "B26916"))
        record = official_match({"name": "Горные Ключи", "district": "Кировский муниципальный округ"}, [], current, census)
        self.assertEqual((record["population"], record["source_cell"]), (4121, "C8262"))
        partizansk = [{"municipality_name": "муниципальный округ Партизанск", "official_name": "Партизанский городской округ"}]
        record = official_match({"name": "Углекаменск", "district": "муниципальный округ Партизанск"}, partizansk, current, census)
        self.assertEqual((record["population"], record["source_cell"]), (3017, "B26787"))
        record = official_match({"name": "Владивосток", "district": "Владивостокский городской округ"}, [], current, census)
        self.assertEqual(record["population"], 594201)
        self.assertEqual(municipal["Владивостокский городской округ"]["population"], 625630)
        self.assertIsNone(official_match({"name": "Сергеевка", "district": "Несуществующий округ"}, [], current, census))


if __name__ == "__main__":
    unittest.main()
