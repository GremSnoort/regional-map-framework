import unittest
from quality_six_point import normalize_population,scenario_distribution,total_variation,use_review,strongest_links

class QualityReviewTest(unittest.TestCase):
    def test_unknown_population_is_not_zero(self):
        self.assertIsNone(normalize_population({'population':0,'population_quality':'not_available'})['population'])
        self.assertEqual(normalize_population({'population':0,'population_quality':'official_current'})['population'],0)
    def test_scenarios_conserve_control_and_do_not_infer_vacancy(self):
        self.assertEqual(scenario_distribution([1,3],100),[25,75])
        self.assertIsNone(scenario_distribution([0,0],100))
        with self.assertRaises(ValueError):scenario_distribution([-1,2],100)
    def test_uniform_floor_multiplier_cannot_change_distribution(self):
        a=scenario_distribution([1,3,4],1);b=scenario_distribution([2,6,8],1)
        self.assertEqual(total_variation(a,b),0)
    def test_mixed_use_not_automatically_nonresidential(self):
        use,flags=use_review({'building':'apartments','shop':'supermarket'})
        self.assertEqual(use,'residential_reported');self.assertIn('mixed_use_requires_residential_area',flags)
    def test_lifecycle_tag_is_signal_not_zero_occupancy(self):
        use,flags=use_review({'building':'house','abandoned:building':'house'})
        self.assertEqual(use,'residential_reported');self.assertIn('lifecycle_osm_signal_unverified',flags)
    def test_weaker_candidates_are_ranked_but_not_confirmed(self):
        a={'official_house_id':'1','candidate_classes':['explicit_address_candidate']}
        b={'official_house_id':'2','candidate_classes':['municipal_registry_candidate']}
        self.assertEqual(strongest_links([a,b]),[a]);self.assertNotIn('geometry_verified',a)

if __name__=='__main__':unittest.main()
