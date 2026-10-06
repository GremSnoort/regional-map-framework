"""Regional housing audit; builds a separate evidence inventory, never edits inputs."""
from __future__ import annotations
import argparse,csv,hashlib,json,math,re
from collections import Counter,defaultdict
from pathlib import Path
import xlrd
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform
from shapely.strtree import STRtree
from build_density import CRS,RESIDENTIAL,NON_RESIDENTIAL,NON_RESIDENTIAL_AMENITIES,proxy_parameters
from housing_inventory import FIELDS,token,locality,street,number,parse_address,positive,unknown,observation,reconcile,check_area_consistency


def checksum(p):
    with p.open('rb') as s:return hashlib.file_digest(s,'sha256').hexdigest()


def excel_column(index):
    out=''
    while index>=0:out=chr(65+index%26)+out;index=index//26-1
    return out


def official_rows(path):
    workbook=xlrd.open_workbook(str(path));sheet=workbook.sheet_by_index(0)
    expected={1:'Адрес МКД',5:'Количество этажей',7:'Общая площадь МКД, всего',8:'Площадь помещений МКД, всего:'}
    if any(token(sheet.cell_value(2,c))!=token(h) for c,h in expected.items()):raise ValueError('Official source columns changed; review measurement semantics')
    rows=[];municipality=''
    for i in range(7,sheet.nrows):
        values=sheet.row_values(i)
        if isinstance(values[0],str) and values[0] and not values[1] and 'наименование' not in values[0]:municipality=values[0]
        if not isinstance(values[0],(int,float)) or not isinstance(values[1],str) or not values[1]:continue
        gross=positive(values[7]);premises=positive(values[8]);floors=positive(values[5],integer=True,maximum=80)
        flags=check_area_consistency(gross,premises,None)
        if floors is None:flags.append('invalid_or_variable_storeys')
        if gross is None:flags.append('missing_or_invalid_gross_area')
        if premises is None:flags.append('missing_or_invalid_all_premises_area')
        rows.append({'house_id':f'fkr25:{sheet.name}:{i+1}','excel_row':i+1,'sheet':sheet.name,'municipality':municipality,'address':values[1],
                     'address_key':parse_address(values[1]),'storeys':floors,'raw_storeys':values[5], 'gross_area_m2':gross,'premises_total_area_m2':premises,
                     'area_semantics':'all premises, not residential premises','residential_area_m2':None,'apartments':None,'occupied_fraction':None,
                     'flags':flags,'matched_osm_ids':[]})
    return rows


def locality_matches(tags,domain_name):
    values={locality(tags[k]) for k in ['addr:city','addr:place','addr:village'] if tags.get(k)}
    if domain_name:values.add(locality(domain_name))
    return values


def municipal_token(value):
    return re.sub(r'\s+','',re.sub(r'\b(?:городской округ|муниципальный округ|муниципальный район|го|мо|мр|район|округ)\b','',token(value)))


def address_candidates(tags,domain_name,index,municipality=None):
    h=number(tags.get('addr:housenumber',''));s=street(tags.get('addr:street',''))
    if not h or not s:return [],False
    names=locality_matches(tags,domain_name);alias=False
    if domain_name=='Монастырище' and token(tags.get('addr:street',''))=='монастырище':
        s='дос';names.add('монастырище');alias=True
    result={row['house_id']:row for name in names for row in index.get((name,s,h),[])}
    if municipality and municipality!='boundary_or_outside':
        allowed={municipal_token(x) for x in municipality} if not isinstance(municipality,str) else {municipal_token(municipality)}
        result={k:r for k,r in result.items() if municipal_token(r['municipality']) in allowed}
    return list(result.values()),alias


def write_csv(path,rows,columns=None):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,columns or list(rows[0]));writer.writeheader();writer.writerows(rows)


def main():
    p=argparse.ArgumentParser();p.add_argument('--region-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    root=args.region_root.resolve();out=args.output.resolve()
    if root!=Path(__file__).resolve().parents[1] or root not in out.parents:raise ValueError('Audit output must be inside the new Primorye region')
    if root/'review' not in out.parents:raise ValueError('Independent audit output must be in review/, outside sources and active map data')
    out.mkdir(parents=True,exist_ok=True)
    source=root/'sources/housing-audit-20261005';cache=root/'sources/osm-cache';osm_path=cache/'osm-buildings.geojson'
    fetches=json.loads((source/'source-fetches.json').read_text())
    for row in fetches:
        if row.get('status')==200 and checksum(source/row['file'])!=row['sha256']:raise ValueError('Downloaded primary source changed')
    buildings=json.loads(osm_path.read_text())['features'];official=official_rows(source/'fkr-program-2026.xls')
    gis=json.loads((source/'gis-442-response.txt').read_text())
    if gis.get('guid')!='07ec3423-37d5-4daa-9166-84134526d0ec' or gis.get('address',{}).get('house',{}).get('houseNumber')!='442':raise ValueError('GIS pilot identity changed')
    index=defaultdict(list)
    for row in official:
        if row['address_key']:index[tuple(row['address_key'])].append(row)
    domains=json.loads((cache/'housing-review-domains.geojson').read_text())['features']
    to_metric=Transformer.from_crs('EPSG:4326',CRS,always_xy=True).transform
    domain_geoms=[transform(to_metric,shape(f['geometry'])) for f in domains];dtree=STRtree(domain_geoms)
    municipal=json.loads((root/'data/municipalities.geojson').read_text())['features']
    mun_names={f['properties']['municipality_name']:[f['properties']['municipality_name'],*f['properties'].get('official_name','').split(' + ')] for f in municipal}
    mun_geoms=[transform(to_metric,shape(f['geometry'])) for f in municipal];mtree=STRtree(mun_geoms)
    projected=[];signatures=defaultdict(list);addresses=defaultdict(list);ids=set();records=[];coverage=Counter();by_municipality=defaultdict(Counter);priority=[]
    for n,f in enumerate(buildings):
        props=f['properties'];tags=props['tags'];identifier=f"{props['osm_type']}/{props['osm_id']}"
        if identifier in ids:raise ValueError('Duplicate typed OSM ID')
        ids.add(identifier);g=shape(f['geometry']);metric=transform(to_metric,g);projected.append(metric)
        sig=hashlib.sha256(g.normalize().wkb).hexdigest();signatures[sig].append(n)
        point=metric.representative_point();dm=list(dtree.query(point,predicate='intersects'));mm=list(mtree.query(point,predicate='intersects'))
        settlement=domains[dm[0]]['properties']['name'] if len(dm)==1 else None
        municipality=municipal[mm[0]]['properties']['municipality_name'] if len(mm)==1 else 'boundary_or_outside'
        fields={name:unknown() for name in FIELDS};kind=tags.get('building','');issues=[]
        explicit=kind in RESIDENTIAL;facility=tags.get('amenity') in NON_RESIDENTIAL_AMENITIES or kind in NON_RESIDENTIAL
        use='mixed' if explicit and (tags.get('shop') or tags.get('amenity')) else 'residential' if explicit else 'non_residential' if facility else None
        if use:fields['use']=reconcile([observation(use,'legacy-osm',f'{identifier}:building/amenity/shop')])
        if explicit and facility:issues.append('residential_facility_conflict')
        floor=positive(tags.get('building:levels'),integer=True,maximum=80)
        if tags.get('building:levels') and floor is None:issues.append('invalid_or_fractional_osm_storeys')
        if floor:fields['above_ground_storeys']=reconcile([observation(floor,'legacy-osm',f'{identifier}:building:levels')])
        flats=positive(tags.get('building:flats'),integer=True,maximum=10000)
        if flats:fields['apartments']=reconcile([observation(flats,'legacy-osm',f'{identifier}:building:flats')])
        if tags.get('building:flats') and flats is None:issues.append('invalid_osm_apartments')
        if not metric.is_valid or metric.is_empty:issues.append('invalid_geometry')
        if not 12<=metric.area<=80000:issues.append('footprint_area_outside_model_range')
        if tags.get('building:part'):issues.append('building_part_requires_parent_review')
        if tags.get('abandoned')=='yes' or tags.get('disused')=='yes' or kind in {'construction','ruins'}:issues.append('occupancy_cannot_be_assumed')
        if not floor and (explicit or proxy_parameters(tags)):issues.append('storeys_assumed_by_model')
        if not use:issues.append('building_use_unknown')
        h=number(tags.get('addr:housenumber',''));s=street(tags.get('addr:street',''));names=locality_matches(tags,None)
        if h and s and names:
            for name in names:addresses[(municipality,name,s,h)].append(n)
        if not h or not s:issues.append('address_incomplete')
        if settlement and names and locality(settlement) not in names:issues.append('osm_locality_differs_from_model_domain')
        candidates,alias=address_candidates(tags,settlement,index,mun_names.get(municipality,municipality))
        if alias and candidates:issues.append('dos_locality_alias_requires_review')
        if candidates:
            for row in candidates:row['matched_osm_ids'].append(identifier)
            if len(candidates)>1:issues.append('multiple_official_address_rows')
            fields['house_type']=reconcile([observation('mkd','fkr-program',f"{row['sheet']}!B{row['excel_row']}") for row in candidates])
            obs=list(fields['above_ground_storeys']['observations'])
            for row in candidates:
                if row['storeys'] is not None:obs.append(observation(row['storeys'],'fkr-program',f"{row['sheet']}!F{row['excel_row']}", 'reported_storeys_scope_unknown'))
            fields['above_ground_storeys']=reconcile(obs)
            for field,col,attribute in [('gross_building_area_m2',7,'gross_area_m2'),('premises_total_area_m2',8,'premises_total_area_m2')]:
                fields[field]=reconcile([observation(row[attribute],'fkr-program',f"{row['sheet']}!{excel_column(col)}{row['excel_row']}",'official_address_not_yet_verified_contour') for row in candidates if row[attribute] is not None])
            if any(row['flags'] for row in candidates):issues.append('official_source_has_quality_flags')
            if fields['above_ground_storeys']['status']=='conflicting':issues.append('storeys_source_conflict')
        if any(row['address_key']==('монастырище','дос','442') for row in candidates):
            for field,key_name in [('gross_building_area_m2','totalSquare'),('residential_premises_area_m2','residentialSquare'),('above_ground_storeys','floorCount'),('apartments','residentialPremiseActualCount')]:
                value=positive(gis.get(key_name),integer=field in {'above_ground_storeys','apartments'})
                if value is not None:fields[field]=reconcile(fields[field]['observations']+[observation(value,'gis-dos442',key_name,'address_only_contour_unverified')])
            if fields['gross_building_area_m2']['status']=='conflicting':issues.append('gross_area_source_conflict')
            if check_area_consistency(positive(gis.get('totalSquare')),None,positive(gis.get('residentialSquare'))):issues.append('gis_residential_area_exceeds_gross')
        rec={'schema_version':2,'osm_id':identifier,'geometry_sha256':sig,'osm_tags_sha256':hashlib.sha256(json.dumps(tags,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
             'municipality':municipality,'model_settlement':settlement,'osm_address':{k:v for k,v in tags.items() if k.startswith('addr:')},'footprint_area_m2':round(metric.area,3),
             'match':{'status':'candidate' if candidates else 'unmatched','canonical_house_id':None,'candidate_house_ids':[c['house_id'] for c in candidates]},'fields':fields,
             'issues':issues,'independent_occupancy_verified':False,'area_consistency_flags':check_area_consistency(fields['gross_building_area_m2']['value'],fields['premises_total_area_m2']['value'],None)}
        records.append(rec);coverage['buildings']+=1;by_municipality[municipality]['buildings']+=1
        if settlement:coverage['in_68_model_domains']+=1
        if candidates:coverage['with_official_address_candidates']+=1;by_municipality[municipality]['with_official_address_candidates']+=1
        for field,v in fields.items():
            coverage[f'{field}:{v["status"]}']+=1
        if use is None:by_municipality[municipality]['use_unknown']+=1
        if floor is None:by_municipality[municipality]['osm_storeys_missing_or_invalid']+=1
        if n and n%100000==0:print(json.dumps({'processed':n}),flush=True)
    tree=STRtree(projected);pairs=[];duplicate_groups=[]
    for members in signatures.values():
        if len(members)>1:
            duplicate_groups.append([records[i]['osm_id'] for i in members])
            for i in members:records[i]['issues'].append('identical_geometry_duplicate')
    # Partial overlaps and containment are review candidates, not automatic deletion:
    # OSM building parts and mixed-use complexes can legitimately overlap.
    for i,g in enumerate(projected):
        if not g.is_valid or g.is_empty:continue
        for j in tree.query(g,predicate='intersects'):
            if j<=i or not projected[j].is_valid:continue
            a=g.intersection(projected[j]).area
            if a<=1.:continue
            ratio=a/min(g.area,projected[j].area) if min(g.area,projected[j].area)>0 else 0
            if ratio<.05:continue
            label='nested_or_nearly_duplicate_geometry' if ratio>=.95 else 'partial_footprint_overlap'
            pairs.append({'osm_id_1':records[i]['osm_id'],'osm_id_2':records[j]['osm_id'],'intersection_m2':round(a,3),'fraction_of_smaller':round(ratio,6),'classification':label})
            for k in [i,int(j)]:records[k]['issues'].append(label)
    duplicate_addresses=[]
    for key,members in addresses.items():
        if len(members)>1:
            duplicate_addresses.append({'address':list(key),'osm_ids':[records[i]['osm_id'] for i in members]})
            for i in members:records[i]['issues'].append('duplicate_osm_address')
    for row in official:
        row['matched_osm_ids']=sorted(set(row['matched_osm_ids']))
        if len(row['matched_osm_ids'])>1:
            row['flags'].append('multiple_contours_for_official_house')
    multiple={identifier for row in official if len(row['matched_osm_ids'])>1 for identifier in row['matched_osm_ids']}
    for record in records:
        if record['osm_id'] in multiple:record['issues'].append('multiple_contours_for_official_house')
    issues=Counter()
    with (out/'housing-inventory-v2.jsonl').open('w') as stream:
        for rec in records:
            rec['issues']=sorted(set(rec['issues']));issues.update(rec['issues']);stream.write(json.dumps(rec,ensure_ascii=False,separators=(',',':'))+'\n')
            if rec['issues']:
                priority.append({'osm_id':rec['osm_id'],'municipality':rec['municipality'],'model_settlement':rec['model_settlement'],'footprint_area_m2':rec['footprint_area_m2'],'official_candidate_ids':';'.join(rec['match']['candidate_house_ids']),'storeys_status':rec['fields']['above_ground_storeys']['status'],'issues':';'.join(rec['issues'])})
    priority.sort(key=lambda r:(not bool(r['official_candidate_ids']),r['model_settlement'] not in {'Монастырище','Ольга','Кавалерово'},-r['footprint_area_m2'],r['osm_id']))
    write_csv(out/'building-review-queue.csv',priority)
    write_csv(out/'municipality-housing-coverage.csv',[{'municipality':m,**{k:c.get(k,0) for k in ['buildings','with_official_address_candidates','use_unknown','osm_storeys_missing_or_invalid']}} for m,c in sorted(by_municipality.items())])
    write_csv(out/'official-housing-addresses.csv',[{k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(list,tuple)) else v) for k,v in row.items()} for row in official])
    write_csv(out/'footprint-overlaps.csv',pairs,columns=['osm_id_1','osm_id_2','intersection_m2','fraction_of_smaller','classification'])
    (out/'duplicate-addresses.json').write_text(json.dumps(duplicate_addresses,ensure_ascii=False,indent=2))
    (out/'exact-geometry-duplicates.json').write_text(json.dumps(duplicate_groups,ensure_ascii=False,indent=2))
    source_catalog={'schema_version':2,'sources':{
        'gis-dos442':{'kind':'primary','snapshot':'sources/housing-audit-20261005/gis-442-response.txt','sha256':checksum(source/'gis-442-response.txt'),'url':'https://dom.gosuslugi.ru/#!/passport/show?houseGuid=07ec3423-37d5-4daa-9166-84134526d0ec','retrieved_at':'2026-10-05','as_of':None,'limitation':'No field measurement dates, storeys/occupancy/counts missing; residential area exceeds gross; contour not verified'},
        'legacy-osm':{'kind':'primary_osm','snapshot':'sources/osm-cache/osm-buildings.geojson','sha256':checksum(osm_path),'as_of':'2026-09-08','limitation':'OSM is not independent verification of housing use, occupancy or storeys'},
        'fkr-program':{'kind':'primary','snapshot':'sources/housing-audit-20261005/fkr-program-2026.xls','sha256':checksum(source/'fkr-program-2026.xls'),'url':next(r['url'] for r in fetches if r['file']=='fkr-program-2026.xls'),'retrieved_at':'2026-10-05','measurement_as_of':None,'publication_context':'The fund website links this workbook alongside decree 393-pp of 2026-05-21; workbook measurement dates not stated','columns':{'storeys':'F','gross_building_area_m2':'H','premises_total_area_m2':'I'},'limitation':'No separate residential-premises area, apartments, or occupancy columns; official address does not verify contour'}
    }}
    (out/'sources.json').write_text(json.dumps(source_catalog,ensure_ascii=False,indent=2)+'\n')
    summary={'schema_version':2,'scope':'All extracted OSM footprints of Primorye; not proof of completeness of real buildings','coverage':dict(coverage),'issue_counts':dict(issues),'municipalities':len(by_municipality),
             'official_rows':len(official),'official_rows_address_parsed':sum(bool(r['address_key']) for r in official),'official_rows_with_candidates':sum(bool(r['matched_osm_ids']) for r in official),
             'official_rows_without_candidates':sum(not r['matched_osm_ids'] for r in official),'official_rows_with_multiple_contours':sum(len(r['matched_osm_ids'])>1 for r in official),
             'official_rows_with_quality_flags':sum(bool(r['flags']) for r in official),'exact_duplicate_groups':len(duplicate_groups),'footprint_overlap_pairs':len(pairs),'duplicate_address_groups':len(duplicate_addresses),
             'independently_confirmed_geometry_matches':0,'independently_confirmed_occupancy':0,'automatic_model_overrides_applied':0,
             'status':'AUTOMATED_CHECKS_COMPLETED_MANUAL_AND_OCCUPANCY_CHECKS_PENDING'}
    ids_by_identifier={r['osm_id']:i for i,r in enumerate(records)}
    selected=sorted(priority,key=lambda x:('storeys_source_conflict' not in x['issues'] and 'gross_area_source_conflict' not in x['issues'],x['model_settlement'] not in {'Монастырище','Ольга','Кавалерово'},-x['footprint_area_m2']))[:1000]
    map_features=[]
    for row in selected:
        i=ids_by_identifier[row['osm_id']];rec=records[i]
        props=dict(row);props.update({'address':', '.join(rec['osm_address'].values()) or 'Адрес не указан','official_storeys':';'.join(str(o['value']) for o in rec['fields']['above_ground_storeys']['observations'] if o['source_id']=='fkr-program'),'source_url':source_catalog['sources']['fkr-program']['url'] if rec['match']['candidate_house_ids'] else f"https://www.openstreetmap.org/{row['osm_id']}",'osm_url':f"https://www.openstreetmap.org/{row['osm_id']}",'warning':'Автоматическое обнаружение конфликта; контур и характеристики не подтверждены независимо.'})
        map_features.append({'type':'Feature','properties':props,'geometry':buildings[i]['geometry']})
    (out/'regional_housing_findings.geojson').write_text(json.dumps({'type':'FeatureCollection','metadata':{'scope':'First 1000 prioritized findings; full inventory in JSONL','verified_building_matches':0},'features':map_features},ensure_ascii=False,separators=(',',':')))
    summary['map_findings']=len(map_features)
    (out/'audit-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    from verify_housing_audit import record_integrity
    record_integrity(root,out)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
