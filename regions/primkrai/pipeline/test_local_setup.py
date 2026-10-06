import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from verify_local_setup import verify, local_file


class LocalSetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'new';self.root.mkdir()
        records=[]
        names=['data/'+n+'.geojson' for n in ['municipalities','settlements','stores','expansion_priority','major_roads','federal_roads','settlement_density']]
        names+=['sources/rosstat_municipal_population_2025.xlsx','sources/rosstat_vpn2020_table5.xlsx','pipeline/priority_config.json','pipeline/settlement_density_config.json','sources/cache/far-eastern-fed-district-latest.osm.pbf']
        for name in names:
            content = b'{}' if name == 'pipeline/priority_config.json' else b'legacy'
            self.write('sources/legacy-snapshot/'+name,content)
            active='sources/baseline/'+Path(name).name if name.startswith('data/') else 'pipeline/legacy-density-config.json' if name=='pipeline/settlement_density_config.json' else name
            self.write(active,content)
            records.append({'path':name,'bytes':len(content),'sha256':self.hash(content)})
        self.write('sources/legacy-snapshot-manifest.json',{'policy':'LEGACY_ONLY_OFFLINE','legacy_root':'/does/not/exist/old-project','snapshot_root':'sources/legacy-snapshot','files':records})
        self.write('pipeline/extract_osm.py',b'extractor')
        self.write('sources/osm-cache/osm-extraction.json',{'pbf_sha256':self.hash(b'legacy'),'extraction_script_sha256':self.hash(b'extractor')})
        self.write('sources/osm-cache/osm-buildings.geojson',b'{}')
        self.write('sources/local-derived-manifest.json',{'files':[{'path':'sources/osm-cache/osm-buildings.geojson','sha256':self.hash(b'{}')}]})
        self.write('sources/building-review.json',{'exclusions':[]})
        self.housing={'sources':{'legacy-osm':{'snapshot':'sources/osm-cache/osm-buildings.geojson','sha256':self.hash(b'{}')}},'buildings':[]}
        self.write('review/housing-register.json',self.housing)

    @staticmethod
    def hash(value):return hashlib.sha256(value).hexdigest()

    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(value if isinstance(value,bytes) else json.dumps(value).encode())

    def test_offline_verification_does_not_need_legacy_directory(self):
        self.assertTrue(verify(self.root)['independent_of_legacy_directory'])
        self.assertFalse(verify(self.root)['external_downloads_required'])

    def test_external_manual_exclusion_and_confirmed_housing_are_rejected(self):
        self.write('sources/building-review.json',{'exclusions':[{'osm_id':'way/1','source_url':'https://example.org'}]})
        with self.assertRaises(ValueError):verify(self.root)
        self.write('sources/building-review.json',{'exclusions':[]})
        self.housing['buildings']=[{'fields':{'use':{'status':'confirmed'}}}]
        self.write('review/housing-register.json',self.housing)
        with self.assertRaises(ValueError):verify(self.root)

    def test_changed_active_table_or_snapshot_is_rejected(self):
        self.write('sources/rosstat_municipal_population_2025.xlsx',b'external')
        with self.assertRaises(ValueError):verify(self.root)
        self.write('sources/rosstat_municipal_population_2025.xlsx',b'legacy')
        self.write('sources/legacy-snapshot/data/stores.geojson',b'changed')
        with self.assertRaises(ValueError):verify(self.root)

    def test_symlinks_and_traversal_cannot_attach_old_project(self):
        outside=Path(self.tmp.name)/'old-data';outside.write_bytes(b'legacy')
        p=self.root/'sources/rosstat_municipal_population_2025.xlsx';p.unlink();p.symlink_to(outside)
        with self.assertRaises(ValueError):verify(self.root)
        with self.assertRaises(ValueError):local_file(self.root,'../old-data')
