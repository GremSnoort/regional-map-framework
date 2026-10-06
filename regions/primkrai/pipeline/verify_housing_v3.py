"""Validate every corrected record and its reproducible primary evidence."""
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
from housing_inventory import FIELDS
from housing_match_v3 import address_identity,compatible_address,derived_field,match_allowed,parse_official_address
from recheck_housing_links import load_official
from verify_housing_audit import checksum
from verify_housing_link_recheck import verify_recheck


def verify_v3(root,out):
    root=root.resolve();out=out.resolve()
    if root/'review' not in out.parents:raise ValueError('Expected private regional review directory')
    pins=json.loads((out/'integrity-manifest.json').read_text())
    for group in ['inputs','outputs']:
        for name,digest in pins[group].items():
            path=(root/name).resolve()
            if root not in path.parents or checksum(path)!=digest:raise ValueError(f'Integrity failed: {name}')
    parent=verify_recheck(root,root/'review/building-link-recheck-20261005')
    official=load_official(root/'review/regional-housing-audit-20261005/official-housing-addresses.csv')
    counts=Counter();ids=set();canonical={};slots=defaultdict(set);statuses={};removed=0;towns=defaultdict(Counter)
    for line in (out/'housing-inventory-v3.jsonl').open():
        r=json.loads(line);identifier=r['osm_id'];status=r['match']['status'];links=r['match']['links']
        if identifier in ids or r['schema_version']!=3:raise ValueError('Duplicate ID or incorrect schema')
        ids.add(identifier);canonical[identifier]=r['canonical_geometry_member'];statuses[identifier]=status
        counts['contours']+=1;counts['status:'+status]+=1;counts['allocation_role:'+r['allocation_role']]+=1
        if links:counts['contours_with_compatible_links']+=1
        if r['address_conflict']:counts['conflicting_address_evidence']+=1
        for method,key in [('address_node_covered_by_unique_geometry','contours_with_address_node_evidence'),('associatedStreet_member','contours_with_associatedStreet_evidence')]:
            if any(a['method']==method for a in r['address_evidence']):counts[key]+=1
        if r['model_settlement']:towns[r['model_settlement']]['contours']+=1;towns[r['model_settlement']][status]+=1
        if r['match']['geometry_verified'] or r['match']['canonical_house_id'] is not None or r['occupancy_verified'] or r['weight_override_allowed']:raise ValueError('Unconfirmed evidence promoted')
        if r['current_occupied_fraction'] is not None or r['current_residents'] is not None:raise ValueError('Occupancy fabricated')
        if r['address_conflict'] and (links or status!='quarantined'):raise ValueError('Conflicting addresses were linked')
        identities=[address_identity(a['tags']) for a in r['address_evidence']]
        conflict=any(not compatible_address(a,b) for j,a in enumerate(identities) for b in identities[j+1:])
        if conflict!=r['address_conflict']:raise ValueError('Address conflict detection disagrees with evidence')
        if set(r['fields'])!=set(FIELDS):raise ValueError('Housing fields missing')
        for value in r['fields'].values():
            if value!=derived_field(value['observations']):raise ValueError('Derived field disagrees with observations')
            if any(o['source_id']!='legacy-osm' for o in value['observations']) and status!='address_candidate':raise ValueError('Weak or ambiguous match enriched the building')
        evidence={(a['source_id'],a['locator'],a['method']):a for a in r['address_evidence']}
        for link in links:
            row=official[link['official_house_id']];a=parse_official_address(row['address'])
            if not a or row['address']!=link['official_address']:raise ValueError('Official address provenance lost')
            actual=[]
            for item in link['address_evidence']:
                address=evidence[(item['source_id'],item['locator'],item['method'])]
                classification=match_allowed(address_identity(address['tags']),a,r['osm_boundary_locality_context'])
                if not classification:raise ValueError('Rejected typed address became a link')
                actual.append(classification)
            if actual!=link['candidate_classes']:raise ValueError('Candidate classification differs from evidence')
            slots[link['official_house_id']].add(r['canonical_geometry_member'])
        accepted={a['official_house_id'] for a in links}
        for rejected in r['match']['rejected_previous_links']:
            if rejected['official_house_id'] in accepted:raise ValueError('Link both rejected and accepted')
            removed+=1
    for identifier,chosen in canonical.items():
        if chosen not in ids or canonical[chosen]!=chosen:raise ValueError('Invalid duplicate geometry representative')
    multi={key for key,value in slots.items() if len(value)>1}
    for line in (out/'housing-inventory-v3.jsonl').open():
        r=json.loads(line);links=r['match']['links']
        if (len(links)>1 or any(a['official_house_id'] in multi for a in links)) and r['match']['status']!='ambiguous':raise ValueError('Multiple physical slots not quarantined')
    summary=json.loads((out/'summary.json').read_text())
    if dict(counts)!=summary['counts'] or removed!=summary['removed_previous_links']:raise ValueError('Summary disagrees with full inventory')
    for town,data in summary['targets'].items():
        if dict(towns[town])!=data:raise ValueError('Target town summary disagrees')
    if len(ids)!=parent['contours_verified']:raise ValueError('Raw footprint coverage changed')
    quality=json.loads((out/'housing_quality.geojson').read_text());qids=[f['properties']['osm_id'] for f in quality['features']]
    if len(qids)!=summary['map_findings'] or len(set(qids))!=len(qids) or not set(qids)<=ids:raise ValueError('Invalid quality map sample')
    return {'schema_version':3,'contours_verified':len(ids),'source_and_output_integrity':'passed','protected_map_and_metadata_files_unchanged':parent['protected_data_files_unchanged'],'duplicate_geometry_shadows':counts['allocation_role:duplicate_geometry_shadow'],'removed_previous_links':removed,'confirmed_geometry_matches':0,'confirmed_current_occupancy':0,'applied_density_overrides':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];result=verify_v3(root,args.output)
    (args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result))
