"""Corrected, reproducible housing evidence; incompatible links cannot enrich fields."""
import argparse
from collections import Counter,defaultdict
import csv
import hashlib
import json
from pathlib import Path
from shapely.geometry import Point,shape
from shapely.strtree import STRtree
from housing_inventory import FIELDS,observation,check_area_consistency
from housing_match_v3 import address_identity,compatible_address,derived_field,locality_name,match_allowed,municipality_identity,parse_official_address,preferred_geometry_member
from recheck_housing_links import load_official
from verify_housing_audit import checksum,verify
from verify_housing_link_recheck import verify_recheck


def typed_id(feature):
    p=feature['properties'];return f"{p['osm_type']}/{p['osm_id']}"


def build(root,out):
    previous=root/'review/regional-housing-audit-20261005';recheck=root/'review/building-link-recheck-20261005'
    verify(root,previous);verify_recheck(root,recheck)
    source=root/'sources/housing-corrections-20261005'
    evidence=json.loads((source/'osm-address-evidence.json').read_text())
    if evidence['pbf_sha256']!=json.loads((root/'sources/osm-cache/osm-extraction.json').read_text())['pbf_sha256']:raise ValueError('Address extraction is from a different PBF')
    if evidence['extractor_sha256']!=checksum(root/'pipeline/extract_housing_addresses.py'):raise ValueError('Address extractor changed')
    features=json.loads((root/'sources/osm-cache/osm-buildings.geojson').read_text())['features']
    geometries=[shape(f['geometry']) for f in features];tree=STRtree(geometries)
    ids={typed_id(f):i for i,f in enumerate(features)}
    grouped=defaultdict(list)
    for i,g in enumerate(geometries):grouped[hashlib.sha256(g.normalize().wkb).hexdigest()].append(i)
    canonical={};duplicates=[]
    for members in grouped.values():
        chosen=typed_id(preferred_geometry_member([features[i] for i in members]))
        for i in members:canonical[i]=chosen
        if len(members)>1:duplicates.append({'canonical_geometry_member':chosen,'members':[typed_id(features[i]) for i in members]})
    address_nodes=defaultdict(list);node_issues=Counter()
    for node in evidence['address_nodes']:
        point=Point(node['coordinates']);hits=[int(i) for i in tree.query(point,predicate='intersects')]
        slots={canonical[i] for i in hits}
        if len(slots)!=1:node_issues['outside_buildings' if not slots else 'ambiguous_containment']+=1;continue
        for i in hits:address_nodes[i].append({'source_id':'legacy-osm-addresses','locator':node['osm_id'],'tags':node['tags'],'method':'address_node_covered_by_unique_geometry'})
        node_issues['nodes_linked_to_one_geometry']+=1
    related=defaultdict(list)
    for relation in evidence['associated_streets']:
        names=set(relation['street_names'])
        if relation['name']:names.add(relation['name'])
        normalized={tuple((address_identity({'addr:street':name})[k] for k in ['street_type','street_name'])) for name in names}
        if not names:node_issues['associated_street_name_missing']+=1;continue
        if len(normalized)!=1:node_issues['associated_street_name_conflict']+=1;continue
        name=sorted(names)[0]
        for house in relation['houses']:
            identifier=f"{'way' if house['type']=='w' else 'relation'}/{house['id']}"
            if identifier in ids:related[ids[identifier]].append({'source_id':'legacy-osm-addresses','locator':relation['osm_id'],'tags':{'addr:street':name},'method':'associatedStreet_member'})
    municipal=json.loads((root/'data/municipalities.geojson').read_text())['features'];mun_geoms=[shape(f['geometry']) for f in municipal];mt=STRtree(mun_geoms)
    municipal_map=defaultdict(set)
    for i,f in enumerate(municipal):
        p=f['properties']
        for name in [p['municipality_name'],*p.get('official_name','').split(' + ')]:municipal_map[municipality_identity(name)].add(i)
    official=load_official(previous/'official-housing-addresses.csv');official_index=defaultdict(list);unmapped=[];unparsed=[]
    for row in official.values():
        row['typed_address']=parse_official_address(row['address'])
        if not row['typed_address']:unparsed.append(row['house_id']);continue
        namespace=municipal_map.get(municipality_identity(row['municipality']),set())
        row['municipality_alias_method']='exact_administrative_identity'
        if not namespace:
            name=municipality_identity(row['municipality'])[0]
            namespace={i for (key,_),members in municipal_map.items() if key==name for i in members}
            row['municipality_alias_method']='unique_name_historical_administrative_label'
        if len(namespace)!=1:unmapped.append(row['house_id']);continue
        row['municipality_index']=next(iter(namespace));a=row['typed_address']
        official_index[(row['municipality_index'],a['street_name'],a['house_number'])].append(row)
    boundaries=json.loads((root/'sources/osm-cache/osm-boundaries.json').read_text());names=list(boundaries)
    city_geoms=[shape(boundaries[name]['geometry']) for name in names];ct=STRtree(city_geoms)
    records=[];counts=Counter();old_link_changes=[];official_slots=defaultdict(set);by_town=defaultdict(Counter)
    old_assessments={r['osm_id']:r for r in (json.loads(line) for line in (recheck/'building-link-assessments.jsonl').open())}
    source_catalog=json.loads((previous/'sources.json').read_text());source_catalog['schema_version']=3
    facts=json.loads((root/'sources/housing-verification-20261005/reviewed-source-facts.json').read_text())
    for key,value in facts['sources'].items():
        source_catalog['sources'][key]={**value,'snapshot':'sources/housing-verification-20261005/'+value['snapshot']}
    with (previous/'housing-inventory-v2.jsonl').open() as stream:
        for n,line in enumerate(stream):
            old=json.loads(line);f=features[n];identifier=typed_id(f)
            if old['osm_id']!=identifier:raise ValueError('Source ordering changed')
            tags=f['properties']['tags'];point=geometries[n].representative_point();mun_hits=list(mt.query(point,predicate='intersects'))
            mun=int(mun_hits[0]) if len(mun_hits)==1 else None
            context=sorted({locality_name(names[int(i)]) for i in ct.query(point,predicate='intersects')})
            own={'source_id':'legacy-osm','locator':identifier,'tags':{k:v for k,v in tags.items() if k.startswith('addr:')},'method':'building_tags'}
            addresses=[own]+address_nodes.get(n,[])
            for rel in related.get(n,[]):
                merged=dict(own['tags'])
                if not merged.get('addr:street'):merged.update(rel['tags']);addresses.append({**rel,'tags':merged})
            complete=[{**a,'identity':address_identity(a['tags'])} for a in addresses if a['tags'].get('addr:street') and a['tags'].get('addr:housenumber')]
            identities=[address_identity(a['tags']) for a in addresses]
            conflict=any(not compatible_address(a,b) for j,a in enumerate(identities) for b in identities[j+1:])
            links={};rejected=[]
            if not conflict and mun is not None:
                for address in complete:
                    a=address['identity']
                    for row in official_index.get((mun,a['street_name'],a['house_number']),[]):
                        classification=match_allowed(a,row['typed_address'],context)
                        if not classification:continue
                        # A node inside a unique contour can add the address; it is
                        # still OSM evidence, never an independent cadastral match.
                        link=links.setdefault(row['house_id'],{'official_house_id':row['house_id'],'official_address':row['address'],
                            'candidate_classes':[],'address_evidence':[],'municipality_alias_method':row['municipality_alias_method']})
                        link['candidate_classes'].append(classification);link['address_evidence'].append({'source_id':address['source_id'],'locator':address['locator'],'method':address['method']})
            for old_id in old['match']['candidate_house_ids']:
                if old_id not in links:
                    rejected.append({'official_house_id':old_id,'reason':'conflicting_osm_addresses' if conflict else 'typed_address_or_explicit_locality_incompatible_with_primary_address'})
            alias=[]
            # Previously proposed DOS alias is kept as a review suggestion only.
            # GIS establishes the official address identity for 442, not its polygon.
            for assessment in old_assessments[identifier]['address_assessments']:
                old_id=assessment['official_house_id'];a=official[old_id]['typed_address']
                if old_id not in links and a and a['locality']=='монастырище' and a['street_name']=='дос':
                    alias.append({'official_house_id':old_id,'official_address':official[old_id]['address'],'status':'alias_requires_geometry_review','gis_card_available':a['house_number']=='442'})
            fields={name:derived_field([o for o in old['fields'][name]['observations'] if o['source_id']=='legacy-osm']) for name in FIELDS}
            strong=[official[k] for k,v in links.items() if any(c!='municipal_registry_candidate' for c in v['candidate_classes'])]
            for row in strong:
                locator=f"{row['sheet']}!B{row['excel_row']}"
                fields['house_type']['observations'].append(observation('mkd','fkr-program',locator,'address_candidate_contour_unverified'))
                for name,key,col in [('above_ground_storeys','storeys','F'),('gross_building_area_m2','gross_area_m2','H'),('premises_total_area_m2','premises_total_area_m2','I')]:
                    value=row[key]
                    if value not in ('',None):fields[name]['observations'].append(observation(float(value),'fkr-program',f"{row['sheet']}!{col}{row['excel_row']}",'address_candidate_contour_unverified'))
            fields={name:derived_field(value['observations']) for name,value in fields.items()}
            record={'schema_version':3,'osm_id':identifier,'geometry_sha256':old['geometry_sha256'],'model_settlement':old['model_settlement'],
                'municipality':municipal[mun]['properties']['municipality_name'] if mun is not None else None,
                'raw_osm_address':own['tags'],'address_evidence':addresses,'address_conflict':conflict,
                'osm_boundary_locality_context':context,'canonical_geometry_member':canonical[n],
                'allocation_role':'geometry_primary' if canonical[n]==identifier else 'duplicate_geometry_shadow',
                'match':{'status':'quarantined' if conflict else 'address_candidate' if strong else 'context_candidate' if links else 'alias_review' if alias else 'unmatched',
                         'links':list(links.values()),'rejected_previous_links':rejected,'alias_suggestions':alias,'canonical_house_id':None,'geometry_verified':False},
                'fields':fields,'footprint_area_m2':old['footprint_area_m2'],'original_quality_flags':old['issues'],
                'area_consistency_flags':check_area_consistency(fields['gross_building_area_m2']['value'],fields['premises_total_area_m2']['value'],fields['residential_premises_area_m2']['value']),
                'current_occupied_fraction':None,'current_residents':None,'occupancy_verified':False,'weight_override_allowed':False,
                'resettlement_evidence':old_assessments[identifier]['resettlement_register_evidence'],
                'gis_card_address_evidence':old_assessments[identifier]['gis_card_address_evidence']}
            for k in links:official_slots[k].add(canonical[n])
            records.append(record)
            for r in rejected:old_link_changes.append({'osm_id':identifier,**r})
            if n and n%100000==0:print(json.dumps({'processed':n}),flush=True)
    out.mkdir(parents=True,exist_ok=True);target=[];map_candidates=[]
    labels={'quarantined':'Конфликт адресов: применение заблокировано','ambiguous':'Несколько домов или контуров: применение заблокировано',
            'address_candidate':'Адрес сопоставлен; кадастровый контур не подтверждён','context_candidate':'Кандидат по муниципалитету; населённый пункт не подтверждён',
            'alias_review':'Адресный алиас требует проверки','unmatched':'Официальное сопоставление не найдено'}
    with (out/'housing-inventory-v3.jsonl').open('w') as stream:
        for n,r in enumerate(records):
            links=r['match']['links']
            if len(links)>1 or any(len(official_slots[a['official_house_id']])>1 for a in links):
                r['match']['status']='ambiguous'
                # Candidate fields remain visible in each linked primary row but
                # cannot be assigned to the contour when the link is ambiguous.
                r['fields']={name:derived_field([o for o in value['observations'] if o['source_id']=='legacy-osm']) for name,value in r['fields'].items()}
                r['area_consistency_flags']=[]
            counts['contours']+=1;counts['status:'+r['match']['status']]+=1;counts['allocation_role:'+r['allocation_role']]+=1
            if r['address_conflict']:counts['conflicting_address_evidence']+=1
            if links:counts['contours_with_compatible_links']+=1
            if any(a['method']=='address_node_covered_by_unique_geometry' for a in r['address_evidence']):counts['contours_with_address_node_evidence']+=1
            if any(a['method']=='associatedStreet_member' for a in r['address_evidence']):counts['contours_with_associatedStreet_evidence']+=1
            if r['model_settlement']:by_town[r['model_settlement']]['contours']+=1;by_town[r['model_settlement']][r['match']['status']]+=1
            stream.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
            if r['model_settlement'] in {'Монастырище','Ольга','Кавалерово'} or links or r['match']['rejected_previous_links']:
                priority=(not bool(r['resettlement_evidence']),r['match']['status'] not in {'quarantined','ambiguous','alias_review'},r['model_settlement'] not in {'Монастырище','Ольга','Кавалерово'},-r['footprint_area_m2'])
                map_candidates.append((priority,n))
            if r['model_settlement'] in {'Монастырище','Ольга','Кавалерово'}:
                target.append({'settlement':r['model_settlement'],'osm_id':r['osm_id'],'status':r['match']['status'],'address':json.dumps(r['raw_osm_address'],ensure_ascii=False),
                               'candidate_ids':';'.join(a['official_house_id'] for a in links),'removed_links':';'.join(a['official_house_id'] for a in r['match']['rejected_previous_links']),
                               'address_node_ids':';'.join(a['locator'] for a in r['address_evidence'] if a['method']=='address_node_covered_by_unique_geometry'),'geometry_confirmed':False,'occupancy_confirmed':False})
    def csv_file(name,rows,columns):
        with (out/name).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,columns);writer.writeheader();writer.writerows(rows)
    csv_file('removed-address-links.csv',old_link_changes,['osm_id','official_house_id','reason'])
    csv_file('three-settlements-corrected.csv',target,list(target[0]))
    map_features=[]
    for _,n in sorted(map_candidates)[:1000]:
        r=records[n];links=r['match']['links'];tags=features[n]['properties']['tags']
        address=', '.join(str(tags[k]) for k in ['addr:city','addr:place','addr:street','addr:housenumber'] if tags.get(k)) or 'Адрес на контуре не указан'
        props={'osm_id':r['osm_id'],'address':address,'settlement':r['model_settlement'] or 'Вне модельных областей','match_status':r['match']['status'],'match_status_label':labels[r['match']['status']],
               'official_addresses':'; '.join(a['official_address'] for a in links),'removed_links':len(r['match']['rejected_previous_links']),
               'source_address_evidence':'; '.join(a['locator'] for a in r['address_evidence']),
               'reported_storeys':r['fields']['above_ground_storeys']['value'],'storeys_status':r['fields']['above_ground_storeys']['status'],
               'occupancy_note':'Фактическая заселённость неизвестна','geometry_note':'Кадастровая привязка не подтверждена','applied_to_density':'Нет',
               'resettlement_note':'Адрес есть в программе расселения; актуальное проживание не подтверждено' if r['resettlement_evidence'] else '',
               'source_url':source_catalog['sources']['fkr-program']['url'] if links else f"https://www.openstreetmap.org/{r['osm_id']}",
               'resettlement_source_url':source_catalog['sources']['kav-program-2026']['url'] if r['resettlement_evidence'] else '',
               'osm_url':f"https://www.openstreetmap.org/{r['osm_id']}"}
        map_features.append({'type':'Feature','properties':props,'geometry':features[n]['geometry']})
    (out/'housing_quality.geojson').write_text(json.dumps({'type':'FeatureCollection','metadata':{'schema_version':3,'selection':'First 1000 priority findings, not entire housing inventory','independent_geometry_confirmations':0,'current_occupancy_confirmations':0},'features':map_features},ensure_ascii=False,separators=(',',':'))+'\n')
    (out/'geometry-duplicate-groups.json').write_text(json.dumps(duplicates,ensure_ascii=False,indent=2)+'\n')
    summary={'schema_version':3,'counts':dict(counts),'removed_previous_links':len(old_link_changes),'address_node_results':dict(node_issues),'official_rows_unparsed':unparsed,
        'official_rows_municipality_unresolved':unmapped,'exact_duplicate_groups':len(duplicates),'map_findings':len(map_features),
        'targets':{town:dict(by_town[town]) for town in ['Монастырище','Ольга','Кавалерово']},
        'geometry_confirmations':0,'current_occupancy_confirmations':0,'applied_density_overrides':0,'source_policy':'CORRECTED_ADDRESS_EVIDENCE_SEPARATE_FROM_LEGACY_POPULATION_MODEL'}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    source_catalog['sources']['legacy-osm-addresses']={'kind':'primary_osm','snapshot':str((source/'osm-address-evidence.json').relative_to(root)),
        'sha256':checksum(source/'osm-address-evidence.json'),'as_of':evidence['osm_snapshot_as_of'],'limitation':'Address nodes/relations from same OSM source, not independent verification'}
    (out/'sources.json').write_text(json.dumps(source_catalog,ensure_ascii=False,indent=2)+'\n')
    paths=[root/'pipeline/housing_match_v3.py',root/'pipeline/build_housing_v3.py',root/'pipeline/extract_housing_addresses.py',source/'osm-address-evidence.json',previous/'housing-inventory-v2.jsonl',previous/'official-housing-addresses.csv',recheck/'building-link-assessments.jsonl',root/'sources/osm-cache/osm-buildings.geojson',root/'sources/osm-cache/osm-boundaries.json',root/'data/municipalities.geojson']
    paths.extend(root/'pipeline'/name for name in ['housing_inventory.py','recheck_housing_links.py','verify_housing_audit.py','verify_housing_link_recheck.py','verify_housing_v3.py'])
    paths.extend([previous/'integrity-manifest.json',recheck/'integrity-manifest.json',previous/'sources.json',root/'sources/housing-verification-20261005/reviewed-source-facts.json'])
    paths.extend(root/s['snapshot'] for s in source_catalog['sources'].values())
    pins={'inputs':{str(p.relative_to(root)):checksum(p) for p in paths},'outputs':{str(p.relative_to(root)):checksum(p) for p in sorted(out.iterdir()) if p.is_file() and p.name not in {'integrity-manifest.json','verification.json'}}}
    (out/'integrity-manifest.json').write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve()
    immutable={root/'review/regional-housing-audit-20261005',root/'review/building-link-recheck-20261005'}
    if root/'review' not in out.parents or any(p==out or p in out.parents for p in immutable):raise ValueError('Use a separate new-project review directory')
    build(root,out)
