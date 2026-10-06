"""Reconcile store catalogs without equating catalog presence with current operation."""
import argparse,csv,hashlib,html,json,math,re
from collections import Counter,defaultdict
from pathlib import Path
from pyproj import Geod
from shapely.geometry import Point,shape
from shapely.strtree import STRtree
from housing_match_v3 import house_number,locality_name,street_identity
from verify_housing_audit import checksum

GEOD=Geod(ellps='WGS84')


def clean(value):return html.unescape(re.sub('<[^>]+>','',value)).strip()


def parse_catalog(text):
    coord={}
    for identifier,body in re.findall(r'\$\("#addr-id-(\d+)"\)\.click\(function\(\)\{(.*?)\}\);',text,re.S):
        body=re.sub(r'//[^\n]*','',body)
        values=re.findall(r'center:\s*\[\s*([-\d.]+),\s*([-\d.]+)\s*,?\s*\]',body)
        if len(values)!=1:raise ValueError('Missing or ambiguous active center')
        value=[float(x) for x in values[0]]
        if identifier in coord and coord[identifier]!=value:raise ValueError('Conflicting coordinates for same catalog ID')
        coord[identifier]=value
    cards=[];groups=[];seen=set()
    for block_match in re.finditer(r'<!-- CITY ARRD ITEM 1 \[ -->(.*?)<!-- \] CITY ARRD ITEM 1 \[ -->',text,re.S):
        block=block_match.group(1)
        regional_headers=re.findall(r'<!--\s*(Приморский край|Хабаровский край|Амурская область)\s*-->',text[:block_match.start()])
        region=regional_headers[-1] if regional_headers else None
        title=re.search(r'<div class="city-title">(.*?)</div>',block,re.S)
        count=re.search(r'магазинов:\s*(\d+)',block)
        if not title or not count:raise ValueError('Catalog group layout changed')
        group=clean(title.group(1));items=re.findall(r'<div id="addr-id-(\d+)" class="mabboxitem">\s*<div>(.*?)</div>\s*<div>(.*?)</div>\s*</div>',block,re.S)
        if len(items)!=int(count.group(1)):raise ValueError(f'Displayed catalog count mismatch: {group}')
        groups.append({'group':group,'reported_count':int(count.group(1))})
        for identifier,address,hours in items:
            if identifier in seen:raise ValueError('Duplicate catalog card ID')
            seen.add(identifier);cards.append({'id':identifier,'group':group,'region':region,'address':clean(address),'hours':clean(hours),'coordinates':coord.get(identifier)})
    if not cards:raise ValueError('No cards parsed')
    return cards,groups


def address_key(locality,address):
    match=re.search(r',\s*(?:дом|д\.|здание|соор\.)\s*(.+)$',address,re.I)
    if not match:return None
    street=address[:match.start()].strip();kind,name=street_identity(street)
    return locality_name(locality),kind,name,house_number(match.group(1))


def catalog_locality(card):
    group=card['group'];address=card['address']
    if group=='Приморский край':
        match=re.match(r'^(?:(?:c|с|п|пос|пгт|кп|рп)\.?)\s+([^,]+),\s*(.*)$',address,re.I)
        if match:return match.group(1).strip(),match.group(2).strip()
        return None,address
    return group,address


def distance(a,b):return abs(GEOD.inv(a[0],a[1],b[0],b[1])[2])


def duplicates(items,key):
    groups=defaultdict(list)
    for identifier,value in items:
        if value is not None:groups[key(value)].append(identifier)
    return [values for values in groups.values() if len(values)>1]


def csv_file(out,name,rows,columns=None):
    columns=columns or sorted({k for r in rows for k in r})
    with (out/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,columns);w.writeheader();w.writerows(rows)


def build(root,out):
    root=root.resolve();out=out.resolve()
    if root/'review' not in out.parents or out in {root/'review',root/'review/regional-housing-audit-20261005',root/'review/housing-corrected-v3-20261005'}:raise ValueError('Use separate private store review directory')
    source=root/'sources/store-audit-20261006';oldpath=root/'sources/legacy-snapshot/sources/pyaterochka_5dfo_snapshot.html'
    old,oldgroups=parse_catalog(oldpath.read_text());current,groups=parse_catalog((source/'5dfo-current.html').read_text())
    old_by_id={r['id']:r for r in old};new_by_id={r['id']:r for r in current}
    municipalities=json.loads((root/'data/municipalities.geojson').read_text())['features'];mg=[shape(f['geometry']) for f in municipalities];mt=STRtree(mg)
    settlements=json.loads((root/'data/settlements.geojson').read_text())['features'];by_place=defaultdict(list)
    for f in settlements:by_place[locality_name(f['properties']['name'])].append(f)
    baseline=json.loads((root/'data/stores.geojson').read_text());baseline_by_id={str(f['properties']['id']):f for f in baseline['features']}
    buildings=json.loads((root/'sources/osm-cache/osm-buildings.geojson').read_text())['features']
    building_geometries=[shape(f['geometry']) for f in buildings];bt=STRtree(building_geometries)
    changes=[];quality=[];refined=[];outside=[];counts=Counter();near=[]
    official=json.loads((source/'official-address-evidence.json').read_text())
    official_keys=defaultdict(list)
    for item in official['addresses']:
        k=address_key(item['locality'],item['address'])
        if k:official_keys[k].append(item)
    osm=json.loads((source/'osm-store-evidence.json').read_text());osm_keys=defaultdict(list)
    for item in osm['stores']:
        tags=item['tags'];k=address_key(tags.get('addr:city') or tags.get('addr:place',''),tags.get('addr:street','')+', дом '+tags.get('addr:housenumber',''))
        if k and tags.get('addr:street') and tags.get('addr:housenumber'):osm_keys[k].append(item)
    for card in current:
        coordinates=card['coordinates'];locality,address=catalog_locality(card);hits=[];valid=coordinates is not None and len(coordinates)==2 and all(math.isfinite(x) for x in coordinates) and -180<=coordinates[0]<=180 and -90<=coordinates[1]<=90
        if valid:hits=[int(i) for i in mt.query(Point(coordinates),predicate='intersects')]
        region_claim=card['region']=='Приморский край'
        if not hits and not region_claim:continue
        flags=[]
        if not valid:flags.append('missing_or_invalid_coordinates')
        elif not hits:flags.append('coordinate_outside_region')
        elif len(hits)>1:flags.append('ambiguous_municipality')
        if hits and card['region']!='Приморский край':flags.append('other_region_catalog_card_coordinate_in_primorye')
        if locality is None:flags.append('locality_not_parsed')
        k=address_key(locality or '',address)
        if k is None:flags.append('address_requires_manual_normalization')
        matched_official=official_keys.get(k,[]) if k else [];osm_matches=osm_keys.get(k,[]) if k else []
        anchors=by_place.get(locality_name(locality or ''),[])
        anchor_dist=None
        if valid and anchors:
            anchor_dist=min(distance(coordinates,f['geometry']['coordinates']) for f in anchors)
            expected={f['properties']['district'] for f in anchors}
            if hits and not expected.intersection(municipalities[i]['properties']['municipality_name'] for i in hits):flags.append('catalog_locality_municipality_conflict')
            if anchor_dist>15000:flags.append('more_than_15km_from_named_place_point')
        elif locality:flags.append('named_locality_not_in_local_point_catalog')
        matched_osm_dist=[(distance(coordinates,x['coordinates']),x['osm_id']) for x in osm_matches if x.get('coordinates') and valid]
        nearest_match=min(matched_osm_dist,default=None)
        nearest_brand=min([(distance(coordinates,x['coordinates']),x['osm_id']) for x in osm['stores'] if valid and x.get('coordinates')],default=None)
        if nearest_match and nearest_match[0]>200:flags.append('address_matching_osm_store_more_than_200m_away')
        if len(osm_matches)>1:flags.append('multiple_osm_store_address_candidates')
        building_hits=[] if not valid else [int(i) for i in bt.query(Point(coordinates),predicate='intersects')]
        if valid and not building_hits:flags.append('not_on_mapped_building_not_proven_coordinate_error')
        identifier='5dfo:'+card['id'];oldcard=old_by_id.get(card['id']);before=baseline_by_id.get(card['id'])
        change='added_catalog_card' if oldcard is None else 'changed_catalog_card' if any(oldcard[k]!=card[k] for k in ['group','address','hours','coordinates']) else 'unchanged_catalog_card'
        if change!='unchanged_catalog_card':changes.append({'catalog_id':identifier,'change':change,'old':json.dumps(oldcard,ensure_ascii=False),'current':json.dumps(card,ensure_ascii=False)})
        counts[change]+=1;counts['current_region_candidates']+=1
        for flag in flags:counts['flag:'+flag]+=1
        if matched_official:counts['historical_official_address_matches']+=1
        if osm_matches:counts['exact_osm_address_candidates']+=1
        record={'name':'Пятёрочка','catalog_id':identifier,'catalog_numeric_id':card['id'],'locality':locality,'address':address,'raw_catalog_address':card['address'],'catalog_group':card['group'],'catalog_region':card['region'],
            'id':card['id'],'hours':card['hours'],'source':'5dfo.ru — публичный региональный каталог','source_url':'https://5dfo.ru/','snapshot_date':'2026-10-06',
            'status':'Присутствует в каталоге; текущая работа независимо не подтверждена','catalog_status':'observed_in_5dfo_snapshot',
            'lon':None if coordinates is None else coordinates[0],'lat':None if coordinates is None else coordinates[1],
            'scope_status':'primorye_catalog_candidate' if region_claim else 'quarantined_other_region_coordinate',
            'reported_hours':card['hours'],'catalog_source_url':'https://5dfo.ru/','catalog_observed_on':'2026-10-06','catalog_snapshot_sha256':checksum(source/'5dfo-current.html'),
            'operating_status':'unknown','operating_status_verified':False,'coordinate_verified':False,'coordinate_source':'5dfo_map_flyTo_center',
            'municipality_by_coordinate':'; '.join(municipalities[i]['properties']['municipality_name'] for i in hits),'distance_to_named_place_point_m':anchor_dist,
            'historical_official_address_matches':matched_official,'osm_address_candidates':osm_matches,'nearest_matching_osm_distance_m':None if nearest_match is None else nearest_match[0],
            'nearest_osm_brand_observation':None if nearest_brand is None else {'osm_id':nearest_brand[1],'distance_m':nearest_brand[0],'match_method':'spatial_proximity_only_not_address_identity'},
            'containing_osm_buildings':[{'osm_id':f"{buildings[i]['properties']['osm_type']}/{buildings[i]['properties']['osm_id']}",'tags':{k:v for k,v in buildings[i]['properties']['tags'].items() if k.startswith('addr:') or k in {'name','shop','building'}},'method':'point_covered_by_osm_outline_not_tenant_confirmation'} for i in building_hits],
            'quality_flags':flags,'catalog_change':change,'included_in_previous_active_layer':before is not None,'source_relationship':'Current and old 5dfo snapshots are the same source; OSM and dated promotion appendix are corroboration only'}
        quality.append(record)
        if valid and region_claim and len(hits)==1:refined.append({'type':'Feature','geometry':{'type':'Point','coordinates':coordinates},'properties':record})
        if flags:outside.append({'catalog_id':identifier,'locality':locality,'address':address,'flags':';'.join(flags),'coordinates':json.dumps(coordinates),'municipality':record['municipality_by_coordinate']})
    included={r['catalog_numeric_id'] for r in quality}
    for identifier,feature in baseline_by_id.items():
        if identifier not in included:changes.append({'catalog_id':'5dfo:'+identifier,'change':'previous_layer_card_not_in_current_region_candidates','old':json.dumps(feature['properties'],ensure_ascii=False),'current':json.dumps(new_by_id.get(identifier),ensure_ascii=False)})
    ids=[r['catalog_id'] for r in quality]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate candidate IDs')
    coordinate_dups=duplicates([(f['properties']['catalog_id'],f['geometry']['coordinates']) for f in refined],lambda x:tuple(x))
    address_dups=duplicates([(r['catalog_id'],address_key(r['locality'] or '',r['address'])) for r in quality],lambda x:tuple(x))
    for i,a in enumerate(refined):
        for b in refined[i+1:]:
            d=distance(a['geometry']['coordinates'],b['geometry']['coordinates'])
            if d<=75:near.append({'catalog_id_a':a['properties']['catalog_id'],'catalog_id_b':b['properties']['catalog_id'],'distance_m':d,'status':'nearby_points_not_proven_duplicate'})
    previous_errors=[]
    for identifier,f in baseline_by_id.items():
        p=f['properties'];c=f['geometry']['coordinates'];oldcard=old_by_id.get(identifier)
        flags=[]
        if oldcard is None:flags.append('not_in_saved_html')
        elif oldcard['coordinates']!=c:flags.append('coordinate_differs_from_saved_html')
        if [p['lon'],p['lat']]!=c:flags.append('property_geometry_coordinate_mismatch')
        if not mt.query(Point(c),predicate='intersects').size:flags.append('outside_municipal_union')
        if flags:previous_errors.append({'catalog_id':'5dfo:'+identifier,'flags':';'.join(flags)})
    out.mkdir(parents=True,exist_ok=True)
    (out/'stores-refined.geojson').write_text(json.dumps({'type':'FeatureCollection','metadata':{'checked_on':'2026-10-06','source':'5dfo.ru observed catalog','feature_count':len(refined),'operating_status_verified':False,'coordinate_verified':False,'warning':'Catalog observations and historical corroboration; not a verified current X5 operating store register'},'features':refined},ensure_ascii=False,separators=(',',':'))+'\n')
    (out/'store-assessments.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2)+'\n')
    csv_file(out,'catalog-changes.csv',changes,['catalog_id','change','old','current']);csv_file(out,'coordinate-address-review.csv',outside,['catalog_id','locality','address','flags','coordinates','municipality'])
    csv_file(out,'nearby-store-pairs.csv',near,['catalog_id_a','catalog_id_b','distance_m','status']);csv_file(out,'previous-layer-errors.csv',previous_errors,['catalog_id','flags'])
    count_old=Counter(locality_name(f['properties']['locality']) for f in baseline['features']);count_new=Counter(locality_name(f['properties']['locality'] or '') for f in refined)
    labels={locality_name(f['properties']['locality'] or ''):f['properties']['locality'] for f in refined}
    csv_file(out,'store-counts-by-locality.csv',[{'locality':labels.get(key,key),'locality_key':key,'previous_catalog_cards':count_old[key],'current_catalog_candidates':count_new[key],'change':count_new[key]-count_old[key],'verified_current_operating_stores':None} for key in sorted(set(count_old)|set(count_new))])
    current_keys=defaultdict(list)
    for r in quality:
        if r['catalog_region']=='Приморский край':current_keys[address_key(r['locality'] or '',r['address'])].append(r['catalog_id'])
    csv_file(out,'historical-official-address-crosswalk.csv',[{'locality':r['locality'],'address':r['address'],'source_locator':r['source_locator'],'candidate_ids':';'.join(current_keys.get(address_key(r['locality'],r['address']),[])),
        'match_status':'exact_typed_address_candidate' if current_keys.get(address_key(r['locality'],r['address'])) else 'no_exact_typed_address_match_not_closure_proof','promo_period_end':r['promo_period_end']} for r in official['addresses']])
    (out/'duplicates.json').write_text(json.dumps({'same_coordinates':coordinate_dups,'same_typed_address':address_dups},ensure_ascii=False,indent=2)+'\n')
    summary={'checked_on':'2026-10-06','previous_layer_count':len(baseline['features']),'old_dfo_catalog_cards':len(old),'current_dfo_catalog_cards':len(current),'counts':dict(counts),'refined_primorye_layer_count':len(refined),'quarantined_foreign_region_cards':sum(r['scope_status']=='quarantined_other_region_coordinate' for r in quality),'change_rows':len(changes),'review_rows':len(outside),'nearby_pairs_within_75m':len(near),'coordinate_duplicate_groups':len(coordinate_dups),'typed_address_duplicate_groups':len(address_dups),'previous_layer_error_rows':len(previous_errors),'verified_current_operating_stores':0,'verified_store_coordinates':0,'old_active_layer_modified':False}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    paths=[Path(__file__),oldpath,root/'data/stores.geojson',root/'data/municipalities.geojson',root/'data/settlements.geojson',root/'pipeline/housing_match_v3.py',root/'pipeline/housing_inventory.py',root/'pipeline/extract_store_evidence.py']
    paths.append(root/'sources/osm-cache/osm-buildings.geojson')
    paths.extend(p for p in source.iterdir() if p.is_file())
    pins={'inputs':{str(p.relative_to(root)):checksum(p) for p in paths},'outputs':{str(p.relative_to(root)):checksum(p) for p in out.iterdir() if p.is_file() and p.name not in {'integrity-manifest.json','verification.json'}}}
    (out/'integrity-manifest.json').write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();build(Path(__file__).resolve().parents[1],args.output)
