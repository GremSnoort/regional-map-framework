"""Validate store source provenance, spatial scope and unsupported status claims."""
import argparse,json,csv
from collections import Counter
from pathlib import Path
from shapely.geometry import Point,shape
from shapely.ops import unary_union
from store_catalog_audit import parse_catalog,catalog_locality,address_key
from verify_housing_audit import checksum


def verify_stores(root,out):
    root=root.resolve();out=out.resolve()
    if root/'review' not in out.parents:raise ValueError('Expected local review dataset')
    pins=json.loads((out/'integrity-manifest.json').read_text())
    for group in ['inputs','outputs']:
        for name,digest in pins[group].items():
            path=(root/name).resolve()
            if root not in path.parents or checksum(path)!=digest:raise ValueError('Integrity failed: '+name)
    source=root/'sources/store-audit-20261006';current,_=parse_catalog((source/'5dfo-current.html').read_text());by_id={r['id']:r for r in current}
    records=json.loads((out/'store-assessments.json').read_text());summary=json.loads((out/'summary.json').read_text());counts=Counter();ids=set()
    for r in records:
        if r['catalog_id'] in ids:raise ValueError('Duplicate store observation')
        ids.add(r['catalog_id']);c=by_id[r['catalog_numeric_id']]
        if (r['locality'],r['address'])!=catalog_locality(c) or r['reported_hours']!=c['hours'] or r['catalog_region']!=c['region']:raise ValueError('Source fields lost')
        if r['operating_status']!='unknown' or r['operating_status_verified'] or r['coordinate_verified']:raise ValueError('Unconfirmed store promoted')
        if r['catalog_snapshot_sha256']!=checksum(source/'5dfo-current.html'):raise ValueError('Incorrect catalog provenance')
        for match in r['historical_official_address_matches']:
            if match['promo_period_end']!='2025-12-31' or match['measurement_as_of'] is not None or match['coordinate_confirmation']:raise ValueError('Historical promotion treated as current observation')
            if address_key(match['locality'],match['address'])!=address_key(r['locality'],r['address']):raise ValueError('Historical address link is incompatible')
        counts[r['catalog_change']]+=1;counts['current_region_candidates']+=1
        for flag in r['quality_flags']:counts['flag:'+flag]+=1
        if r['historical_official_address_matches']:counts['historical_official_address_matches']+=1
        if r['osm_address_candidates']:counts['exact_osm_address_candidates']+=1
    if dict(counts)!=summary['counts']:raise ValueError('Full inventory and summary disagree')
    geo=json.loads((out/'stores-refined.geojson').read_text());union=unary_union([shape(f['geometry']) for f in json.loads((root/'data/municipalities.geojson').read_text())['features']])
    if len(geo['features'])!=summary['refined_primorye_layer_count']:raise ValueError('Layer count mismatch')
    layer_ids=set()
    for f in geo['features']:
        p=f['properties'];c=by_id[p['id']]
        if p['id'] in layer_ids:raise ValueError('Duplicate layer ID')
        layer_ids.add(p['id'])
        if p['catalog_region']!='Приморский край' or p['scope_status']!='primorye_catalog_candidate':raise ValueError('Foreign catalog card contaminated regional layer')
        if c['coordinates']!=f['geometry']['coordinates'] or [p['lon'],p['lat']]!=c['coordinates']:raise ValueError('Coordinates differ from source')
        if not union.covers(Point(c['coordinates'])):raise ValueError('Regional layer point outside region')
    if len(layer_ids)!=170 or not set(str(f['properties']['id']) for f in json.loads((root/'data/stores.geojson').read_text())['features'])<=layer_ids:raise ValueError('Existing cards removed without evidence')
    osm=json.loads((source/'osm-store-evidence.json').read_text())
    if osm['extractor_sha256']!=checksum(root/'pipeline/extract_store_evidence.py'):raise ValueError('OSM extractor changed')
    manifest=json.loads((root/'sources/legacy-snapshot-manifest.json').read_text());pbf=next(r['sha256'] for r in manifest['files'] if r['path']=='sources/cache/far-eastern-fed-district-latest.osm.pbf')
    if osm['source_pbf_sha256']!=pbf:raise ValueError('Different OSM snapshot')
    protected=json.loads((root/'sources/housing-verification-20261005/protected-before.json').read_text())
    if any(checksum(root/name)!=digest for name,digest in protected.items()):raise ValueError('Previous active map data changed')
    return {'previous_active_stores_unchanged':166,'refined_catalog_candidates':len(layer_ids),'all_source_and_output_hashes':'passed','foreign_card_quarantine':'passed','protected_map_and_metadata_files_unchanged':len(protected),'verified_current_operation':0,'verified_coordinates':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();root=Path(__file__).resolve().parents[1];result=verify_stores(root,a.output);(a.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result))
