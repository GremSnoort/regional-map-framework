"""Six-point evidence review. No candidate is promoted to verified occupancy."""
import argparse,csv,hashlib,json,math
from collections import Counter,defaultdict
from pathlib import Path
from pyproj import Transformer
from shapely.geometry import shape,Point
from shapely.ops import transform
from shapely.strtree import STRtree
from build_density import CRS,proxy_parameters,deduplicate_footprints
from population_sources import controls,official_match
from verify_housing_audit import checksum
from verify_housing_v3 import verify_v3

RESIDENTIAL={'apartments','residential','house','detached','terrace','dormitory','semidetached_house','bungalow','static_caravan','ger'}
NONRES={'commercial','retail','industrial','warehouse','school','kindergarten','hospital','office','church','garage','garages','service','public','civic','sports_centre','train_station'}


def use_review(tags):
    kind=tags.get('building','');flags=[]
    if kind in RESIDENTIAL:
        use='residential_reported'
        if tags.get('shop') or tags.get('office') or tags.get('amenity'):flags.append('mixed_use_requires_residential_area')
    elif kind in NONRES:use='nonresidential_reported'
    else:use='unknown'
    if tags.get('building:part'):flags.append('building_part_or_outline_requires_geometry_review')
    for key in ['abandoned:building','disused:building','demolished:building','razed:building','ruins:building','construction:building']:
        if tags.get(key):flags.append('lifecycle_osm_signal_unverified')
    if kind in {'construction','ruins'} or tags.get('abandoned')=='yes' or tags.get('disused')=='yes':flags.append('lifecycle_osm_signal_unverified')
    return use,sorted(set(flags))


def normalize_population(properties):
    p=dict(properties)
    if p.get('population_quality')=='not_available':
        p['population']=None;p['population_as_of']=None;p['population_warning']='Численность неизвестна; null не означает отсутствие жителей.'
    return p


def scenario_distribution(weights,control):
    if any(not math.isfinite(v) or v<0 for v in weights):raise ValueError('Invalid scenario weights')
    total=sum(weights)
    return None if total==0 else [control*v/total for v in weights]


def total_variation(a,b):return sum(abs(x-y) for x,y in zip(a,b))/2


def strongest_links(links):
    rank={'explicit_address_candidate':0,'osm_boundary_context_candidate':1,'municipal_registry_candidate':2}
    if not links:return []
    best=min(rank[c] for l in links for c in l['candidate_classes'])
    return [l for l in links if min(rank[c] for c in l['candidate_classes'])==best]


def write_csv(out,name,rows,columns=None):
    columns=columns or sorted({k for r in rows for k in r})
    with (out/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,columns);w.writeheader();w.writerows(rows)


def build(root,out):
    root=root.resolve();out=out.resolve()
    if root/'review' not in out.parents or out==root/'review':raise ValueError('Expected a new private review directory')
    parent=root/'review/housing-corrected-v3-20261005';verify_v3(root,parent)
    out.mkdir(parents=True,exist_ok=True)
    counts=Counter();triage=[];occupancy=[];sample=defaultdict(list);explicit_slots=defaultdict(set);records=[]
    raw=json.loads((root/'sources/osm-cache/osm-buildings.geojson').read_text())['features']
    for n,line in enumerate((parent/'housing-inventory-v3.jsonl').open()):
        r=json.loads(line);tags=raw[n]['properties']['tags'];identifier=f"{raw[n]['properties']['osm_type']}/{raw[n]['properties']['osm_id']}"
        if identifier!=r['osm_id']:raise ValueError('Building order mismatch')
        use,flags=use_review(tags);counts['use:'+use]+=1
        for flag in flags:counts['flag:'+flag]+=1
        links=strongest_links(r['match']['links']);best=[]
        for link in links:
            if 'explicit_address_candidate' in link['candidate_classes']:explicit_slots[link['official_house_id']].add(r['canonical_geometry_member'])
            best.append(link['official_house_id'])
        record={'osm_id':identifier,'parent_record':r['osm_id'],'geometry_sha256':r['geometry_sha256'],
            'municipality':r['municipality'],'model_settlement':r['model_settlement'],'use_status':use,'use_evidence_source':'legacy-osm',
            'use_review_flags':flags,'prior_match_status':r['match']['status'],'preferred_address_candidates':best,
            'canonical_geometry_member':r['canonical_geometry_member'],
            'official_house_identity':None,'fias_house_guid':None,'cadastre_number':None,'identity_chain_status':'unconfirmed',
            'geometry_verified':False,'current_residents':None,'current_occupied_fraction':None,'occupancy_as_of':None,
            'residential_area_m2':None,'residential_area_status':'unknown','weight_override_allowed':False}
        if r['gis_card_address_evidence']:
            record['separate_official_address_evidence']=r['gis_card_address_evidence']
        records.append(record)
        counts['buildings']+=1
        if r['match']['status'] in {'ambiguous','quarantined','alias_review'}:
            triage.append({'osm_id':identifier,'settlement':r['model_settlement'],'prior_status':r['match']['status'],
                'preferred_candidates':';'.join(best),'all_candidates':';'.join(l['official_house_id'] for l in r['match']['links']),
                'official_addresses':'; '.join(l['official_address'] for l in r['match']['links']),
                'aliases':json.dumps(r['match']['alias_suggestions'],ensure_ascii=False),'osm_address':json.dumps(r['raw_osm_address'],ensure_ascii=False),
                'fias_house_guid':'','cadastre_number':'','geometry_verified':False,'next_evidence':'independent_address_identity_and_cadastral_geometry'})
        if r['resettlement_evidence']:
            occupancy.append({'osm_id':identifier,'model_settlement':r['model_settlement'],'program_evidence':json.dumps(r['resettlement_evidence'],ensure_ascii=False),
                'resettlement_completed':None,'completion_as_of':None,'current_residents':None,'current_occupied_fraction':None,
                'status':'programme_address_only_no_completion_proof',
                'checked_on':'2026-10-06','source_url':'https://kavalerovskij-r25.gosweb.gosuslugi.ru/deyatelnost/proekty-i-programmy/2026-11/'})
        stratum=('target_' if r['model_settlement'] in {'Монастырище','Ольга','Кавалерово'} else 'regional_')+(kind if (kind:=tags.get('building')) in {'house','apartments','residential'} else use)
        sample[stratum].append((hashlib.sha256(('primkrai-independent-review-20261006:'+identifier).encode()).hexdigest(),identifier))
    for record in records:
        ids=record['preferred_address_candidates']
        record['address_review_status']='conflict_blocked' if record['prior_match_status']=='quarantined' else 'alias_requires_independent_identity' if record['prior_match_status']=='alias_review' else 'unique_explicit_candidate_contour_unverified' if len(ids)==1 and len(explicit_slots[ids[0]])==1 and record['canonical_geometry_member'] in explicit_slots[ids[0]] else 'candidate_requires_independent_review' if ids else 'no_official_candidate'
        counts['address_review:'+record['address_review_status']]+=1
    with (out/'housing-evidence-assessment.jsonl').open('w') as f:
        for record in records:f.write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n')
    write_csv(out,'address-identity-review.csv',triage)
    write_csv(out,'resettlement-completion-review.csv',occupancy)
    blind=[];key=[]
    for stratum,items in sorted(sample.items()):
        for _,identifier in sorted(items)[:20]:
            review_id=hashlib.sha256(identifier.encode()).hexdigest()[:16]
            blind.append({'review_id':review_id,'osm_id':identifier,'osm_url':'https://www.openstreetmap.org/'+identifier,
                'reference_use':'','reference_residential_area_m2':'','reference_storeys':'','reference_occupied_premises':'','reference_as_of':'','reference_geometry_id':'','reference_source_url':'','reviewer':'','reviewed_on':'','reference_status':'pending_independent_evidence'})
            key.append({'review_id':review_id,'stratum':stratum,'osm_id':identifier})
    write_csv(out,'blind-validation-sample.csv',blind);write_csv(out,'validation-sample-strata.csv',key)
    (out/'independent-validation.json').write_text(json.dumps({'sample_size':len(blind),'sampling':'deterministic SHA-256 within OSM-defined strata, 20 per nonempty stratum','reference_labels_completed':0,'geometry_precision':None,'use_accuracy':None,'population_error':None,'limitation':'Sampling frame uses OSM. Labels must come from independent evidence; no accuracy measured yet.'},ensure_ascii=False,indent=2)+'\n')
    settlements=json.loads((root/'data/settlements.geojson').read_text());municipal=json.loads((root/'data/municipalities.geojson').read_text())['features'];munprops=[f['properties'] for f in municipal]
    _,current,census=controls(root/'sources');coverage=[];controls_rows=[];unverified=[];yearcounts=Counter()
    domains=json.loads((root/'pipeline/outputs/allocation-domains.geojson').read_text())['features'];domainkeys={f['properties']['settlement_key'] for f in domains}
    forward=Transformer.from_crs('EPSG:4326',CRS,always_xy=True).transform
    dg=[transform(forward,shape(f['geometry'])) for f in domains];dt=STRtree(dg)
    for f in settlements['features']:
        p=normalize_population(f['properties']);counts['unknown_population_null']+=p['population'] is None;f['properties']=p
        keyid=f"osm_{p['osm_type']}/{p['osm_id']}";source=official_match(p,munprops,current,census)
        if p['population_quality'].startswith('official'):
            if source is None or source['population']!=p['population'] or source['population_as_of']!=p['population_as_of']:raise ValueError('Official control provenance mismatch')
        yearcounts[str(p.get('population_as_of'))]+=1
        control={'settlement_key':keyid,'name':p['name'],'district':p['district'],'population':p['population'],'population_as_of':p.get('population_as_of'),
            'quality':p['population_quality'],'source_url':p.get('population_source_url'),'source_sheet':p.get('source_sheet'),'source_cell':p.get('source_cell'),
            'official_match_status':'matched_saved_primary_table' if source else 'no_exact_match_in_saved_primary_tables',
            'population_is_current_2026':False,'do_not_sum_mixed_dates':True}
        controls_rows.append(control)
        if p['population_quality']=='osm_unverified' and keyid in domainkeys:unverified.append(control)
        point=transform(forward,shape(f['geometry']));hits=[int(i) for i in dt.query(point,predicate='intersects')]
        reason='covered_by_own_model' if keyid in domainkeys else 'population_unknown' if p['population'] is None else 'below_2500_threshold' if p['population']<2500 else 'above_threshold_without_model'
        coverage.append({'settlement_key':keyid,'name':p['name'],'district':p['district'],'population':p['population'],'population_quality':p['population_quality'],
            'has_own_density_model':keyid in domainkeys,'reason':reason,'point_in_other_domain':';'.join(domains[i]['properties']['name'] for i in hits if domains[i]['properties']['settlement_key']!=keyid),
            'domain_is_official':False,'population_unknown':p['population'] is None})
    settlements['metadata']={**settlements.get('metadata',{}),'quality_review_version':'2026-10-06','unknown_population_representation':'null','population_dates_mixed':True}
    (out/'settlements-refined.geojson').write_text(json.dumps(settlements,ensure_ascii=False,separators=(',',':'))+'\n')
    write_csv(out,'population-controls.csv',controls_rows);write_csv(out,'nine-unverified-controls.csv',unverified);write_csv(out,'territorial-coverage.csv',coverage)
    # Each scenario uses the exact published cell geometry, never building centroids.
    cells=[]
    for name in ['settlement_density','settlement_density_unverified']:cells.extend(json.loads((root/'data'/f'{name}.geojson').read_text())['features'])
    cg=[transform(forward,shape(f['geometry'])) for f in cells];ct=STRtree(cg)
    weights=[defaultdict(float) for _ in cells];outside=0;included=0;invalid=0;domain_review=[]
    selected,duplicate_diagnostics=deduplicate_footprints(raw)
    for n,f in enumerate(selected):
        tags=f['properties']['tags'];params=proxy_parameters(tags)
        if params is None:continue
        g=transform(forward,shape(f['geometry']))
        if not g.is_valid or not 12<=g.area<=80000:invalid+=1;continue
        hits=ct.query(g,predicate='intersects');matched=False
        use,flags=use_review(tags);multiplier,explicit,observed=params
        for i in hits:
            i=int(i);part=g.intersection(cg[i]).area
            if part<=1e-8:continue
            matched=True;w=weights[i];w['baseline_reconstructed']+=part*multiplier;w['footprint_only']+=part*(1 if explicit else .55)
            if explicit:w['explicit_residential_only']+=part*multiplier
            if 'lifecycle_osm_signal_unverified' not in flags:w['lifecycle_signals_excluded']+=part*multiplier
            if not observed:w['assumed_storeys_half']+=part*multiplier*.5;w['assumed_storeys_double']+=part*multiplier*2
            else:w['assumed_storeys_half']+=part*multiplier;w['assumed_storeys_double']+=part*multiplier
        if matched:included+=1
        else:outside+=1
        if n and n%100000==0:print(json.dumps({'scenario_buildings_processed':n}),flush=True)
    groups=defaultdict(list)
    for i,f in enumerate(cells):groups[f['properties']['settlement_key']].append(i)
    scenarios=['footprint_only','explicit_residential_only','lifecycle_signals_excluded','assumed_storeys_half','assumed_storeys_double']
    results=[];cellrows=[];maxreconstruction=0
    for keyid,indices in groups.items():
        p=cells[indices[0]]['properties'];control=p['population_control'];base=[cells[i]['properties']['population_estimate']/control for i in indices]
        reconstructed=scenario_distribution([weights[i]['baseline_reconstructed'] for i in indices],1)
        error=None if reconstructed is None else total_variation(base,reconstructed);maxreconstruction=max(maxreconstruction,error or 0)
        distributions={name:scenario_distribution([weights[i][name] for i in indices],control) for name in scenarios}
        for name,values in distributions.items():
            results.append({'settlement_key':keyid,'name':p['settlement_name'],'municipality':p['municipality'],'population_control':control,
                'population_as_of':p['population_as_of'],'population_quality':p['population_quality'],'scenario':name,
                'status':'calculated_assumption' if values is not None else 'no_supporting_weight',
                'reallocated_fraction':None if values is None else total_variation(base,[v/control for v in values]),
                'scenario_population_sum':None if values is None else sum(values),'baseline_reconstruction_reallocated_fraction':error,
                'is_confidence_interval':False,'is_current_population_estimate':False})
        for j,i in enumerate(indices):
            vals=[v[j] for v in distributions.values() if v is not None];q=cells[i]['properties']
            cellrows.append({'cell_id':q['cell_id'],'settlement_key':keyid,'baseline_population':q['population_estimate'],
                'scenario_min_population':min(vals) if vals else None,'scenario_max_population':max(vals) if vals else None,
                'grid_metres':q['grid_metres'],'population_quality':q['population_quality'],'population_as_of':q['population_as_of'],
                **{name:None if v is None else v[j] for name,v in distributions.items()},'range_type':'assumption_envelope_not_confidence_interval'})
    write_csv(out,'density-scenarios-by-settlement.csv',results);write_csv(out,'density-scenarios-by-cell.csv',cellrows)
    summary={'checked_on':'2026-10-06','counts':dict(counts),'identity_review_rows':len(triage),'resettlement_addresses':len(occupancy),
        'population_controls':len(controls_rows),'unverified_model_controls':len(unverified),'population_dates':dict(yearcounts),
        'coverage_reasons':dict(Counter(r['reason'] for r in coverage)),'points_inside_other_model':sum(bool(r['point_in_other_domain']) for r in coverage),
        'density_models':len(groups),'density_cells':len(cells),'density_scenarios':len(results),'scenario_buildings_in_cells':included,
        'proxy_eligible_buildings_outside_cells':outside,'invalid_or_area_filtered_buildings':invalid,'duplicate_diagnostics':duplicate_diagnostics,
        'maximum_baseline_reconstruction_reallocated_fraction':maxreconstruction,
        'independent_geometry_confirmations':0,'independent_occupancy_confirmations':0,'independent_validation_labels':0,
        'residential_area_scenario_status':'not_computable_no_verified_building_residential_areas','active_density_changed':False,
        'limitation':'All candidate and OSM signals remain unconfirmed; scenarios measure sensitivity, not independent prediction accuracy.'}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    paths=[root/'pipeline/quality_six_point.py',root/'pipeline/build_density.py',root/'pipeline/population_sources.py',parent/'housing-inventory-v3.jsonl',parent/'integrity-manifest.json',root/'sources/osm-cache/osm-buildings.geojson',root/'pipeline/outputs/allocation-domains.geojson',root/'sources/rosstat_municipal_population_2025.xlsx',root/'sources/rosstat_vpn2020_table5.xlsx']
    paths.extend(root/'data'/f'{name}.geojson' for name in ['settlements','municipalities','settlement_density','settlement_density_unverified'])
    paths.extend(p for p in (root/'sources/quality-review-20261006').iterdir() if p.is_file())
    pins={'inputs':{str(p.relative_to(root)):checksum(p) for p in paths},'outputs':{str(p.relative_to(root)):checksum(p) for p in out.iterdir() if p.is_file() and p.name not in {'integrity-manifest.json','verification.json'}}}
    (out/'integrity-manifest.json').write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n');print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    build(Path(__file__).resolve().parents[1],args.output)
