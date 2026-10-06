import copy,unittest
from housing_inventory import FIELDS,unknown,parse_address,street,number,locality,reconcile,observation,confirmed_weight,compile_verified_weights,allocate_verified_population,check_area_consistency
from audit_housing_region import address_candidates,municipal_token


class HousingInventoryTest(unittest.TestCase):
    def test_duplicate_contour_cannot_silently_replace_weight(self):
        with self.assertRaises(ValueError):compile_verified_weights([self.record,self.record],self.sources)

    def test_residential_weight_blocks_unresolved_gross_area_conflict(self):
        self.match();self.confirm('use','residential');self.confirm('residential_premises_area_m2',3000,'whole_contour_residential_premises')
        self.record['fields']['gross_building_area_m2']=reconcile([observation(4000,'official','H1'),observation(2500,'official','H2')])
        with self.assertRaises(ValueError):self.weight()

    def setUp(self):
        self.record={'osm_id':'way/1','footprint_area_m2':200,'match':{'status':'candidate','canonical_house_id':'official/1','reviewer':'test','reviewed_at':'2026-10-05'},'fields':{n:unknown() for n in FIELDS},'area_consistency_flags':[]}
        self.sources={'official':{'kind':'primary'},'osm':{'kind':'primary_osm'}}

    def confirm(self,name,value,scope=None,source='official',date='2025-01-01'):
        o=observation(value,source,'row 1',scope);o['as_of']=date
        self.record['fields'][name]={'value':value,'status':'confirmed','observations':[o],'reviewer':'test','reviewed_at':'2026-10-05'}

    def match(self):self.record['match']['status']='confirmed'

    def weight(self):return confirmed_weight(self.record,200,self.sources)

    def test_area_semantics_remain_separate(self):
        self.match();self.confirm('use','mixed');self.confirm('gross_building_area_m2',5000);self.confirm('premises_total_area_m2',4000)
        self.assertIsNone(self.weight())
        self.confirm('residential_premises_area_m2',3000,'whole_contour_residential_premises')
        self.assertEqual(self.weight(),{'method':'residential_area','unit':'m2','weight':3000.})

    def test_candidate_address_and_reported_fields_cannot_supply_weight(self):
        self.confirm('use','residential');self.confirm('above_ground_storeys',5,'uniform_above_ground_whole_contour')
        self.assertIsNone(self.weight());self.match();self.record['fields']['above_ground_storeys']['status']='reported';self.assertIsNone(self.weight())

    def test_primary_osm_and_undated_sources_cannot_be_independent_confirmation(self):
        self.match();self.confirm('use','residential',source='osm')
        with self.assertRaises(ValueError):self.weight()
        self.confirm('use','residential',date=None)
        with self.assertRaises(ValueError):self.weight()

    def test_occupancy_is_not_imputed_and_dates_must_match(self):
        self.match();self.confirm('use','residential');self.confirm('apartments',60)
        self.assertIsNone(self.weight())
        self.confirm('occupied_fraction',.8);self.confirm('mean_household_size',2.5)
        self.assertEqual(self.weight()['weight'],120.)
        self.record['fields']['mean_household_size']['observations'][0]['as_of']='2021-10-01'
        with self.assertRaises(ValueError):self.weight()

    def test_verified_empty_house_can_have_zero_weight(self):
        self.match();self.confirm('use','residential');self.confirm('apartments',60);self.confirm('occupied_fraction',0);self.confirm('mean_household_size',2.5)
        self.assertEqual(self.weight()['weight'],0.)

    def test_same_house_cannot_supply_area_to_two_contours(self):
        self.match();self.confirm('use','residential');self.confirm('above_ground_storeys',5,'uniform_above_ground_whole_contour')
        second=copy.deepcopy(self.record);second['osm_id']='way/2'
        with self.assertRaises(ValueError):compile_verified_weights([self.record,second],self.sources)

    def test_maximum_storeys_and_wrong_residential_area_scope_are_rejected(self):
        self.match();self.confirm('use','residential');self.confirm('above_ground_storeys',5,'maximum_in_some_section')
        with self.assertRaises(ValueError):self.weight()
        self.confirm('residential_premises_area_m2',1000,'gross_building')
        with self.assertRaises(ValueError):self.weight()

    def test_source_conflicts_are_preserved_and_block_confirmation(self):
        self.assertEqual(reconcile([observation(5,'a','row1'),observation(2,'b','row2')])['status'],'conflicting')
        self.match();self.confirm('use','residential');self.confirm('residential_premises_area_m2',1000,'whole_contour_residential_premises')
        self.record['fields']['residential_premises_area_m2']['observations'].append(observation(2000,'official','row2'))
        with self.assertRaises(ValueError):self.weight()
        self.assertIn('residential_area_exceeds_gross',check_area_consistency(900,None,1000))

    def test_allocation_does_not_mix_area_and_persons_and_conserves_control(self):
        with self.assertRaises(ValueError):allocate_verified_population(100,{'a':{'unit':'m2','weight':100},'b':{'unit':'persons_proxy','weight':100}})
        out=allocate_verified_population(100,{(0,0,1):{'unit':'m2','weight':100},(1,0,1):{'unit':'m2','weight':300}})
        self.assertEqual(sum(out.values()),100000);self.assertEqual(out[0,0,1],25000)

    def test_address_normalization_preserves_building_numbers(self):
        self.assertEqual(parse_address('с. Монастырище, кв-л ДОС, д. 442'),('монастырище','дос','442'))
        self.assertNotEqual(number('6/4'),number('6'));self.assertNotEqual(number('6к1'),number('6'))
        self.assertEqual(number('6A'),number('6А'));self.assertEqual(street('улица 8 Марта'),'8марта')
        self.assertEqual(municipal_token('Черниговский МО'),municipal_token('Черниговский муниципальный округ'))

    def test_address_candidates_stay_in_the_correct_municipality(self):
        a={'house_id':'1','municipality':'Анучинский МО'};b={'house_id':'2','municipality':'Яковлевский МО'}
        tags={'addr:city':'Сергеевка','addr:street':'Центральная улица','addr:housenumber':'1'}
        rows,alias=address_candidates(tags,None,{('сергеевка','центральная','1'):[a,b]},'Анучинский муниципальный округ')
        self.assertEqual(rows,[a]);self.assertFalse(alias)


if __name__=='__main__':unittest.main()
