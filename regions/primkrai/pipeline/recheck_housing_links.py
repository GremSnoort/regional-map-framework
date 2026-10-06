"""Second-pass address and occupancy assessment; never promotes candidates."""
import argparse
from collections import Counter,defaultdict
import csv
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from housing_inventory import locality,number,street,token
from verify_housing_audit import checksum,verify

TARGETS={'Монастырище','Ольга','Кавалерово'}
STREET_TYPES={'улица':'street','ул':'street','переулок':'lane','пер':'lane',
              'проспект':'avenue','пр-кт':'avenue','пр-т':'avenue','квартал':'quarter',
              'кв-л':'quarter','микрорайон':'microdistrict','мкр':'microdistrict',
              'бульвар':'boulevard','бул':'boulevard','шоссе':'highway'}


class VisibleTable(HTMLParser):
    """Comments (including template resident counts) are never observations."""
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None;self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if tag=='tr':self.row=[]
        if tag in {'td','th'} and self.row is not None:self.cell=[]
    def handle_endtag(self,tag):
        if tag in {'script','style'}:self.hidden=max(0,self.hidden-1)
        if tag in {'td','th'} and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        if tag=='tr' and self.row is not None:self.rows.append(self.row);self.row=None
    def handle_data(self,data):
        if self.cell is not None and not self.hidden:self.cell.append(data)


def street_identity(value):
    text=token(value)
    types='|'.join(re.escape(k) for k in sorted(STREET_TYPES,key=len,reverse=True))
    prefix=re.match(rf'^({types})(?:\.|\s+)\s*',text)
    suffix=re.search(rf'\s+({types})\.?$',text) if not prefix else None
    kind=None
    if prefix:kind=STREET_TYPES[prefix.group(1)];text=text[prefix.end():]
    elif suffix:kind=STREET_TYPES[suffix.group(1)];text=text[:suffix.start()]
    return kind,re.sub(r'[\s.,]+','',text)


def street_type(value):return street_identity(value)[0]


def assess_candidate(record,official):
    address=record['osm_address'];key=official['address_key'];flags=[]
    names={locality(address[k]) for k in ['addr:city','addr:place','addr:village'] if address.get(k)}
    if not names:flags.append('locality_inferred_from_model_domain')
    elif key[0] not in names:flags.append('explicit_osm_locality_conflict')
    if len(names)>1:flags.append('multiple_explicit_localities')
    a,osm_street=street_identity(address.get('addr:street',''));b,official_street=street_identity(official['address'].split(',')[1])
    if a and b and a!=b:flags.append('street_type_conflict')
    if not a or not b:flags.append('street_type_not_fully_specified')
    if osm_street!=official_street:flags.append('street_name_alias_requires_independent_confirmation')
    if number(address.get('addr:housenumber',''))!=key[2]:flags.append('house_number_conflict')
    if len(official['matched_osm_ids'])!=1:flags.append('official_house_has_multiple_contours')
    if len(record['match']['candidate_house_ids'])!=1:flags.append('contour_has_multiple_official_houses')
    area=record['footprint_area_m2'];gross=official.get('gross_area_m2');floors=official.get('storeys')
    ratio=gross/area if gross and area>0 else None
    if ratio is not None and (ratio<.6 or floors and ratio>floors*2.5):flags.append('gross_area_vs_footprint_requires_extent_check')
    flags+=sorted(set(record['issues'])&{'identical_geometry_duplicate','nested_or_nearly_duplicate_geometry','partial_footprint_overlap','duplicate_osm_address','building_part_requires_parent_review'})
    if {'street_type_conflict','house_number_conflict'}&set(flags):status='quarantined_text_conflict'
    elif {'official_house_has_multiple_contours','contour_has_multiple_official_houses','multiple_explicit_localities'}&set(flags):status='ambiguous'
    elif {'explicit_osm_locality_conflict','street_name_alias_requires_independent_confirmation','locality_inferred_from_model_domain'}&set(flags):status='locality_or_alias_review_required'
    else:status='text_consistent_geometry_unverified'
    return {'official_house_id':official['house_id'],'official_address':official['address'],
            'status':status,'flags':sorted(set(flags)),'gross_area_to_footprint_ratio':round(ratio,4) if ratio is not None else None,
            'geometry_verified':False,'occupancy_verified':False}


def occupancy_from_registry_counts(premises,related_realty):
    # Linked realty records are not occupied apartments; absence is not vacancy.
    return None


def load_official(path):
    result={}
    for row in csv.DictReader(path.open(encoding='utf-8-sig')):
        for key in ['address_key','matched_osm_ids']:
            row[key]=json.loads(row[key]) if row[key] else None
        for key in ['gross_area_m2','storeys']:
            row[key]=float(row[key]) if row[key] else None
        result[row['house_id']]=row
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve();original=root/'review/regional-housing-audit-20261005'
    if root/'review' not in out.parents or out==original or original in out.parents:raise ValueError('Use a separate review directory in the new project')
    original_check=verify(root,original);out.mkdir(parents=True,exist_ok=True)
    source=root/'sources/housing-verification-20261005';facts=json.loads((source/'reviewed-source-facts.json').read_text())
    for fact_source in facts['sources'].values():
        if checksum(source/fact_source['snapshot'])!=fact_source['sha256']:raise ValueError('Reviewed primary document changed')
    official=load_official(original/'official-housing-addresses.csv');coverage=defaultdict(Counter);counts=Counter();flags=Counter();matched_emergency=defaultdict(list)
    emergency={(locality(f['locality']),street(f['street']),number(f['house_number'])):f for f in facts['address_observations']}
    target_rows=[]
    with (out/'building-link-assessments.jsonl').open('w') as stream,(original/'housing-inventory-v2.jsonl').open() as inputs:
        for line in inputs:
            record=json.loads(line);town=record['model_settlement'] or 'outside_model_domains';c=coverage[town];counts['contours']+=1;c['contours']+=1
            assessments=[assess_candidate(record,official[ident]) for ident in record['match']['candidate_house_ids']]
            if assessments:counts['contours_with_candidates']+=1;c['contours_with_candidates']+=1
            for a in assessments:counts[a['status']]+=1;flags.update(a['flags']);c[a['status']]+=1
            address=record['osm_address'];names={locality(address[k]) for k in ['addr:city','addr:place','addr:village'] if address.get(k)}
            evidence=[emergency[k] for name in names if (k:=(name,street(address.get('addr:street','')),number(address.get('addr:housenumber','')))) in emergency]
            for fact in evidence:matched_emergency[fact['house_number']].append(record['osm_id'])
            pilot=facts['gis_pilot'] if any(official[a['official_house_id']]['address_key']==['монастырище','дос','442'] for a in assessments) else None
            occupancy_flags=[]
            if 'occupancy_cannot_be_assumed' in record['issues']:
                occupancy_flags.append('osm_vacancy_or_construction_signal_unverified');counts['osm_occupancy_risk_signals']+=1;c['osm_occupancy_risk_signals']+=1
            if evidence:occupancy_flags.append('emergency_program_no_current_resettlement_confirmation')
            if pilot:occupancy_flags.append('gis_record_counts_are_not_occupied_apartment_counts')
            result={'osm_id':record['osm_id'],'model_settlement':record['model_settlement'],'geometry_sha256':record['geometry_sha256'],
                    'address_assessments':assessments,'source_inventory_status':'candidate' if assessments else 'unmatched',
                    'resettlement_register_evidence':evidence,'current_occupied_fraction':None,'current_residents':None,
                    'gis_card_address_evidence':pilot,'occupancy_review_flags':occupancy_flags,
                    'geometry_verified':False,'occupancy_verified':False,'weight_override_allowed':False}
            if evidence:c['emergency_program_address_candidates']+=1;counts['emergency_program_address_candidates']+=1
            stream.write(json.dumps(result,ensure_ascii=False,separators=(',',':'))+'\n')
            if town in TARGETS:
                target_rows.append({'settlement':town,'osm_id':record['osm_id'],'osm_url':f"https://www.openstreetmap.org/{record['osm_id']}",
                    'osm_address':json.dumps(address,ensure_ascii=False),'official_candidates':';'.join(a['official_house_id'] for a in assessments),
                    'assessment':';'.join(a['status'] for a in assessments) or 'unmatched','review_flags':';'.join(sorted({f for a in assessments for f in a['flags']})),
                    'original_issues':';'.join(record['issues']),'resettlement_evidence':bool(evidence),'geometry_verified':False,'occupancy_verified':False})
    def csv_out(name,rows):
        with (out/name).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,list(rows[0]));writer.writeheader();writer.writerows(rows)
    csv_out('three-settlements-review.csv',target_rows)
    keys=sorted({k for row in coverage.values() for k in row})
    csv_out('settlement-link-coverage.csv',[{'settlement':town,**{k:c.get(k,0) for k in keys}} for town,c in sorted(coverage.items())])
    parser_html=VisibleTable();html=(source/'probe-1.html').read_text();parser_html.feed(html)
    summary={'scope':'Second pass for every contour; source facts visually reviewed for four Gagarina addresses only',
             'counts':dict(counts),'candidate_flag_counts':dict(flags),'targets':{town:dict(coverage[town]) for town in sorted(TARGETS)},
             'emergency_program_candidates':dict(matched_emergency),'source_inventory_integrity':original_check,
             'independently_confirmed_geometry_matches':0,'current_occupancy_confirmations':0,'applied_weight_overrides':0,
             'source_traps':{'commented_template_resident_rows_imported':sum(any('Количество жителей' in cell for cell in row) for row in parser_html.rows),
                             'fkr_map_uses_external_address_geocoding':'maps/api/geocode' in html,
                             'gis_occupancy_from_realty_linkage':occupancy_from_registry_counts(60,54)},
             'status':'ADDRESS_AND_SOURCE_RECHECK_COMPLETE_PHYSICAL_GEOMETRY_AND_CURRENT_OCCUPANCY_NOT_CONFIRMED'}
    if summary['source_traps']['commented_template_resident_rows_imported']:raise ValueError('HTML comments became resident evidence')
    (out/'recheck-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (out/'reviewed-source-facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2)+'\n')
    input_paths=[root/'pipeline/recheck_housing_links.py',root/'pipeline/test_housing_links.py',root/'pipeline/verify_housing_link_recheck.py',root/'pipeline/housing_inventory.py',root/'pipeline/verify_housing_audit.py',original/'housing-inventory-v2.jsonl',original/'official-housing-addresses.csv',original/'integrity-manifest.json']+[p for p in source.iterdir() if p.is_file()]
    pins={'inputs':{str(p.relative_to(root)):checksum(p) for p in sorted(input_paths)},'outputs':{str(p.relative_to(root)):checksum(p) for p in sorted(out.iterdir()) if p.is_file() and p.name not in {'integrity-manifest.json','verification.json'}}}
    (out/'integrity-manifest.json').write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
