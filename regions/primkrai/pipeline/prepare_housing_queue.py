"""Create a housing review queue solely from the copied legacy OSM snapshot."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from shapely.geometry import shape
from shapely.strtree import STRtree
from housing_register import signature
from build_density import RESIDENTIAL, NON_RESIDENTIAL, NON_RESIDENTIAL_AMENITIES
from verify_local_setup import checksum


def prepare(root):
    if root != Path(__file__).resolve().parents[1]:
        raise ValueError('Housing queue may only be written inside this new regional project')
    cache = root / 'sources/osm-cache'
    domains = json.loads((cache/'housing-review-domains.geojson').read_text())['features']
    domains = [f for f in domains if f['properties']['name'] in {'Монастырище','Ольга','Кавалерово'}]
    if len(domains)!=3:
        raise ValueError('Expected three legacy-derived housing review domains')
    geometries = [shape(f['geometry']) for f in domains]
    tree = STRtree(geometries)
    buildings = []
    for feature in json.loads((cache/'osm-buildings.geojson').read_text())['features']:
        geometry = shape(feature['geometry'])
        point = geometry.representative_point()
        matches = list(tree.query(point,predicate='intersects'))
        if not matches:continue
        if len(matches)!=1:raise ValueError('Housing review domains overlap')
        settlement = domains[matches[0]]['properties']['name']
        p,t = feature['properties'],feature['properties']['tags']
        identifier=f"{p['osm_type']}/{p['osm_id']}"
        fields = {name:{'status':'unknown','value':None,'source_ids':[],'locator':None} for name in ['use','above_ground_storeys','residential_premises_area_m2','apartments']}
        kind=t.get('building','')
        use = 'mixed' if kind in RESIDENTIAL and (t.get('shop') or t.get('amenity')) else 'residential' if kind in RESIDENTIAL else 'non_residential' if kind in NON_RESIDENTIAL or t.get('amenity') in NON_RESIDENTIAL_AMENITIES else None
        if use:
            fields['use']={'status':'reported','value':use,'source_ids':['legacy-osm'],'locator':f'{identifier}: building={kind}, amenity={t.get("amenity", "")}, shop={t.get("shop", "")}'}
        for name,tag in [('above_ground_storeys','building:levels'),('apartments','building:flats')]:
            try:value=float(t.get(tag,'').replace(',','.'))
            except ValueError:continue
            if not math.isfinite(value) or value<=0:continue
            fields[name]={'status':'reported','value':value,'source_ids':['legacy-osm'],'locator':f'{identifier}: {tag}={t[tag]}'}
        address=', '.join(str(t[k]) for k in ['addr:city','addr:place','addr:street','addr:housenumber'] if t.get(k)) or 'Адрес не указан в OSM'
        buildings.append({'osm_id':identifier,'settlement':settlement,'address':address,'geometry_sha256':signature(feature),'expected_tags':t,
                         'match':{'status':'candidate','source_ids':['legacy-osm'],'reason':'Spatial selection in a modelled settlement domain; OSM address retained verbatim; official address/contour link not verified'},
                         'fields':fields,'notes':'Сведения только из старого снимка OSM. Назначение, этажность, адрес и заселённость не проверены по независимому источнику. Площадь жилых помещений неизвестна.'})
    buildings.sort(key=lambda b:(b['settlement'],b['osm_id']))
    sources={'legacy-osm':{'kind':'primary','url':'https://download.geofabrik.de/russia/far-eastern-fed-district.html','snapshot':'sources/osm-cache/osm-buildings.geojson','sha256':checksum(cache/'osm-buildings.geojson'),
                           'as_of':'2026-09-08','checked_at':'2026-10-05','limitation':'Primary OSM snapshot, not an official housing register; observations are reported, never independently confirmed.'}}
    register={'schema_version':1,'region_id':'primkrai','policy':'LEGACY_ONLY_OFFLINE','scope':'All OSM footprints with representative point in legacy-derived model domains of three settlements; not official settlement limits or a complete housing register',
              'sources':sources,'address_inventory':[],'buildings':buildings}
    (root/'review').mkdir(exist_ok=True)
    (root/'review/housing-register.json').write_text(json.dumps(register,ensure_ascii=False,indent=2)+'\n')
    paths=['sources/osm-cache/'+name for name in ['osm-buildings.geojson','osm-boundaries.json','osm-extraction.json','housing-review-domains.geojson']]
    derived={'policy':'LEGACY_ONLY_OFFLINE','pbf_sha256':json.loads((cache/'osm-extraction.json').read_text())['pbf_sha256'],
             'domains_method':'build_density.py allocation domains from copied legacy controls/geometries; independent of external building reviews',
             'files':[{'path':p,'sha256':checksum(root/p)} for p in paths]}
    (root/'sources/local-derived-manifest.json').write_text(json.dumps(derived,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'contours':len(buildings),'by_settlement':dict(Counter(b['settlement'] for b in buildings))},ensure_ascii=False))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--region-root',type=Path,required=True)
    prepare(parser.parse_args().region_root.resolve())
