import unittest
from recheck_housing_links import VisibleTable,assess_candidate,occupancy_from_registry_counts,street_type,street_identity


class HousingLinkTest(unittest.TestCase):
    def setUp(self):
        self.record={'osm_address':{'addr:city':'Ольга','addr:street':'улица Ленина','addr:housenumber':'6/4'},
                     'footprint_area_m2':300,'issues':[],'match':{'candidate_house_ids':['fkr/1']}}
        self.official={'house_id':'fkr/1','address':'пгт. Ольга, ул. Ленина, д. 6/4',
                       'address_key':['ольга','ленина','6/4'],'matched_osm_ids':['way/1'],'gross_area_m2':600,'storeys':2}
    def assess(self):return assess_candidate(self.record,self.official)

    def test_visible_table_does_not_import_comment_template_residents(self):
        parser=VisibleTable();parser.feed('<table><tr><td>Площадь</td><td>500</td></tr><!-- <tr><td>Количество жителей</td><td>2</td></tr> --></table>')
        self.assertEqual(parser.rows,[['Площадь','500']])

    def test_street_types_must_not_be_conflated(self):
        self.assertEqual(street_type('ул. Ленина'),street_type('улица Ленина'))
        self.record['osm_address']['addr:street']='переулок Ленина'
        self.assertEqual(self.assess()['status'],'quarantined_text_conflict')

    def test_street_normalization_does_not_strip_letters_from_names(self):
        self.assertEqual(street_identity('Ульяновская'),(None,'ульяновская'))
        self.assertEqual(street_identity('ул.Ульяновская'),('street','ульяновская'))
        self.assertEqual(street_identity('Ульяновская улица'),('street','ульяновская'))

    def test_house_number_slash_remains_distinct(self):
        self.record['osm_address']['addr:housenumber']='6'
        self.assertIn('house_number_conflict',self.assess()['flags'])

    def test_text_consistency_does_not_confirm_geometry_or_occupancy(self):
        result=self.assess();self.assertEqual(result['status'],'text_consistent_geometry_unverified')
        self.assertFalse(result['geometry_verified']);self.assertFalse(result['occupancy_verified'])

    def test_inferred_or_conflicting_locality_requires_review(self):
        del self.record['osm_address']['addr:city']
        self.assertIn('locality_inferred_from_model_domain',self.assess()['flags'])
        self.record['osm_address']['addr:city']='Сибирцево'
        self.assertIn('explicit_osm_locality_conflict',self.assess()['flags'])

    def test_multiple_contours_do_not_become_unique_match(self):
        self.official['matched_osm_ids'].append('way/2')
        self.assertEqual(self.assess()['status'],'ambiguous')

    def test_plausibility_flag_is_not_independent_geometry_verification(self):
        self.official['gross_area_m2']=100
        result=self.assess();self.assertIn('gross_area_vs_footprint_requires_extent_check',result['flags'])
        self.assertFalse(result['geometry_verified'])

    def test_realty_linkage_does_not_imply_occupancy(self):
        self.assertIsNone(occupancy_from_registry_counts(60,54))
        self.assertIsNone(occupancy_from_registry_counts(60,60))
        self.assertIsNone(occupancy_from_registry_counts(60,0))


if __name__=='__main__':unittest.main()
