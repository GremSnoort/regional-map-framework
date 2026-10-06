import unittest
from housing_match_v3 import address_identity,compatible_address,derived_field,house_number,locality_name,match_allowed,municipality_identity,parse_official_address,street_identity


class HousingV3Test(unittest.TestCase):
    def test_street_kind_and_name_are_separate(self):
        self.assertEqual(street_identity('ул.Ульяновская'),('street','ульяновская'))
        self.assertEqual(street_identity('проезд Ленина'),('drive','ленина'))
        self.assertEqual(street_identity('переулок Ленина'),('lane','ленина'))
        self.assertEqual(street_identity('Ульяновская'),(None,'ульяновская'))

    def test_locality_without_space_after_abbreviation(self):
        self.assertEqual(locality_name('пгт.Ольга'),'ольга')
        self.assertEqual(locality_name('с.Монастырище'),'монастырище')

    def test_house_corpus_and_slash_preserved(self):
        self.assertEqual(house_number('6 корпус 2'),'6к2')
        self.assertNotEqual(house_number('6/4'),house_number('6'))
        self.assertNotEqual(house_number('6 строение 2'),house_number('6 корпус 2'))

    def test_primary_parser_retains_type_and_corpus(self):
        self.assertEqual(parse_official_address('пгт.Ольга, пер. Ленина, д. 6, корпус 2')['street_type'],'lane')
        self.assertEqual(parse_official_address('пгт.Ольга, пер. Ленина, д. 6 корпус 2')['house_number'],'6к2')
        self.assertEqual(parse_official_address('пгт.Ольга, пер. Ленина, д. 6, корпус 2')['house_number'],'6к2')

    def test_nested_locality_does_not_become_parent_city(self):
        a=parse_official_address('г. Артем, с. Кневичи, ул. Авиационная, д. 1')
        self.assertEqual(a['locality'],'кневичи')
        self.assertIsNone(match_allowed(address_identity({'addr:city':'Артем','addr:street':'Авиационная','addr:housenumber':'1'}),a))

    def test_dos_number_without_house_marker(self):
        a=parse_official_address('с. Сергеевка, ДОС 388')
        self.assertEqual((a['street_type'],a['street_name'],a['house_number']),('quarter','дос','388'))

    def test_partial_building_address_blocks_conflicting_node(self):
        self.assertFalse(compatible_address(address_identity({'addr:housenumber':'6'}),address_identity({'addr:street':'Ленина','addr:housenumber':'8'})))

    def test_incompatible_street_type_rejects_matching(self):
        a=address_identity({'addr:city':'Ольга','addr:street':'улица Ленина','addr:housenumber':'6'})
        b=parse_official_address('пгт. Ольга, пер. Ленина, д. 6')
        self.assertIsNone(match_allowed(a,b))

    def test_wrong_explicit_locality_cannot_be_overridden_by_domain(self):
        a=address_identity({'addr:city':'Сибирцево','addr:street':'квартал ДОС','addr:housenumber':'442'})
        b=parse_official_address('с. Монастырище, кв-л ДОС, д. 442')
        self.assertIsNone(match_allowed(a,b,['монастырище']))

    def test_missing_locality_remains_context_candidate(self):
        a=address_identity({'addr:street':'улица Ленина','addr:housenumber':'6'})
        b=parse_official_address('пгт. Ольга, ул. Ленина, д. 6')
        self.assertEqual(match_allowed(a,b),'municipal_registry_candidate')
        self.assertEqual(match_allowed(a,b,['ольга']),'osm_boundary_context_candidate')

    def test_conflicting_node_and_building_addresses_do_not_merge(self):
        a=address_identity({'addr:street':'улица Ленина','addr:housenumber':'6'})
        b=address_identity({'addr:street':'улица Ленина','addr:housenumber':'8'})
        self.assertFalse(compatible_address(a,b))

    def test_unknown_type_does_not_fabricate_contradiction(self):
        a=address_identity({'addr:street':'Ленина','addr:housenumber':'6'})
        b=address_identity({'addr:street':'улица Ленина','addr:housenumber':'6'})
        self.assertTrue(compatible_address(a,b))

    def test_city_and_district_same_name_remain_distinct(self):
        self.assertNotEqual(municipality_identity('Дальнереченский ГО'),municipality_identity('Дальнереченский МО'))
        self.assertEqual(municipality_identity('МО г. Партизанск'),municipality_identity('муниципальный округ Партизанск'))

    def test_field_conflict_never_promotes_to_confirmation(self):
        a={'value':5,'source_id':'osm','locator':'way/1'};b={'value':2,'source_id':'fkr','locator':'F2'}
        self.assertEqual(derived_field([a,b])['status'],'conflicting')
        self.assertIsNone(derived_field([a,b])['value'])
        self.assertEqual(derived_field([a])['status'],'reported')


if __name__=='__main__':unittest.main()
