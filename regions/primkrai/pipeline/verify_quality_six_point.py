"""Verify complete six-point outputs and refuse unsupported confirmations."""
import argparse,csv,json,math
from collections import Counter
from pathlib import Path
from verify_housing_audit import checksum
from verify_housing_v3 import verify_v3


def verify_quality(root,out):
    root=root.resolve();out=out.resolve()
    if root/'review' not in out.parents:raise ValueError('Expected local review output')
    pins=json.loads((out/'integrity-manifest.json').read_text())
    for group in ['inputs','outputs']:
        for name,digest in pins[group].items():
            p=(root/name).resolve()
            if root not in p.parents or checksum(p)!=digest:raise ValueError(f'Changed evidence: {name}')
    protected=verify_v3(root,root/'review/housing-corrected-v3-20261005')
    ids=set();counts=Counter();preferred=0
    for line in (out/'housing-evidence-assessment.jsonl').open():
        r=json.loads(line)
        if r['osm_id'] in ids:raise ValueError('Duplicate building')
        ids.add(r['osm_id']);counts['buildings']+=1;counts['use:'+r['use_status']]+=1;counts['address_review:'+r['address_review_status']]+=1
        for flag in r['use_review_flags']:counts['flag:'+flag]+=1
        for key in ['geometry_verified','weight_override_allowed']:
            if r[key]:raise ValueError('Unsupported promotion')
        for key in ['current_residents','current_occupied_fraction','occupancy_as_of','residential_area_m2','official_house_identity','fias_house_guid','cadastre_number']:
            if r[key] is not None:raise ValueError('Unsupported house/occupancy evidence assigned')
        if r['residential_area_status']!='unknown':raise ValueError('Residential area fabricated')
    settlements=json.loads((out/'settlements-refined.geojson').read_text())['features']
    for f in settlements:
        p=f['properties']
        if p['population_quality']=='not_available':
            if p['population'] is not None:raise ValueError('Missing population represented as zero')
            counts['unknown_population_null']+=1
    s=json.loads((out/'summary.json').read_text())
    if dict(counts)!=s['counts'] or len(ids)!=411886:raise ValueError('Summary mismatch')
    def csv_rows(name):return list(csv.DictReader((out/name).open(encoding='utf-8-sig')))
    controls=csv_rows('population-controls.csv');coverage=csv_rows('territorial-coverage.csv');nine=csv_rows('nine-unverified-controls.csv')
    if len(controls)!=len(settlements) or len(coverage)!=len(settlements) or len(nine)!=9:raise ValueError('Incomplete population/coverage review')
    if len({r['settlement_key'] for r in controls})!=len(controls):raise ValueError('Duplicate population control')
    if any(r['quality']!='osm_unverified' or r['official_match_status']!='no_exact_match_in_saved_primary_tables' for r in nine):raise ValueError('Unverified controls promoted')
    scenarios=csv_rows('density-scenarios-by-settlement.csv');cells=csv_rows('density-scenarios-by-cell.csv')
    if len(scenarios)!=340 or len(cells)!=43220 or len({r['cell_id'] for r in cells})!=len(cells):raise ValueError('Incomplete scenario coverage')
    keys=['footprint_only','explicit_residential_only','lifecycle_signals_excluded','assumed_storeys_half','assumed_storeys_double']
    sums={}
    for row in cells:
        if row['range_type']!='assumption_envelope_not_confidence_interval':raise ValueError('Scenario range mislabelled')
        for name in keys:
            if row[name]:
                value=float(row[name])
                if not math.isfinite(value) or value<0:raise ValueError('Invalid scenario population')
                k=(row['settlement_key'],name);sums[k]=sums.get(k,0)+value
    for row in scenarios:
        if row['is_confidence_interval']!='False' or row['is_current_population_estimate']!='False':raise ValueError('Unvalidated prediction labelled confirmed')
        if row['status']=='calculated_assumption':
            control=float(row['population_control']);total=sums[(row['settlement_key'],row['scenario'])]
            if abs(total-control)>max(1e-6,control*1e-10) or abs(float(row['scenario_population_sum'])-control)>max(1e-6,control*1e-10):raise ValueError('Scenario does not conserve population')
            if not 0<=float(row['reallocated_fraction'])<=1:raise ValueError('Invalid sensitivity metric')
    validation=json.loads((out/'independent-validation.json').read_text());sample=csv_rows('blind-validation-sample.csv')
    if len(sample)!=validation['sample_size'] or validation['reference_labels_completed']!=0:raise ValueError('False independent validation')
    if any(row['reference_source_url'] or row['reference_use'] or row['reference_status']!='pending_independent_evidence' for row in sample):raise ValueError('Unreviewed reference labels assigned')
    return {'buildings_verified':len(ids),'settlements_verified':len(settlements),'scenario_cells_verified':len(cells),'scenario_control_conservation':'passed','all_inputs_and_outputs_integrity':'passed','protected_map_files_unchanged':protected['protected_map_and_metadata_files_unchanged'],'independent_validation_sample_size':len(sample),'independent_labels_completed':0,'confirmed_building_geometry':0,'confirmed_current_occupancy':0}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    result=verify_quality(root,args.output);(args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result))
