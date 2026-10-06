import unittest
from store_catalog_audit import parse_catalog,catalog_locality,address_key,distance,duplicates

class StoreCatalogTest(unittest.TestCase):
    def fixture(self,center='[133.266194, 44.157401,]',count=1):
        return f'''<!--Приморский край--><!-- CITY ARRD ITEM 1 [ --><div class="city-title">Арсеньев</div><div>магазинов: {count}</div><div id="addr-id-27" class="mabboxitem"><div>Жуковского ул., дом 31</div><div>8:00 до 22:00</div></div><!-- ] CITY ARRD ITEM 1 [ -->$("#addr-id-27").click(function(){{
        //center: [37.652777, 55.711316],
        map.flyTo({{center: {center}, zoom:15}});
        }});'''
    def test_only_active_coordinates_and_trailing_comma(self):
        cards,_=parse_catalog(self.fixture());self.assertEqual(cards[0]['coordinates'],[133.266194,44.157401]);self.assertEqual(cards[0]['region'],'Приморский край')
    def test_displayed_card_count_is_checked(self):
        with self.assertRaises(ValueError):parse_catalog(self.fixture(count=2))
    def test_missing_coordinates_never_become_moscow(self):
        with self.assertRaises(ValueError):parse_catalog(self.fixture('[]'))
    def test_corpus_and_slash_are_not_conflated(self):
        self.assertNotEqual(address_key('Находка','Пограничная ул., дом 36В корпус 1'),address_key('Находка','Пограничная ул., дом 36В/1'))
    def test_street_type_preserved(self):
        self.assertNotEqual(address_key('Ольга','Ленина ул., дом 1'),address_key('Ольга','Ленина пер., дом 1'))
    def test_rural_prefix_is_parsed_without_guessing(self):
        self.assertEqual(catalog_locality({'group':'Приморский край','address':'c. Чугуевка, Титова ул., дом 42'}),('Чугуевка','Титова ул., дом 42'))
        self.assertEqual(catalog_locality({'group':'Приморский край','address':'неизвестный адрес'}),(None,'неизвестный адрес'))
    def test_nearby_points_are_not_duplicate_address(self):
        self.assertEqual(duplicates([('1',('a','street','x','1')),('2',('a','street','x','2'))],tuple),[])
        self.assertGreater(distance([133.26,44.15],[133.27,44.15]),500)

if __name__=='__main__':unittest.main()
