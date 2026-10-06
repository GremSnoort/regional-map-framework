"""Check recheck summaries against every assessment and protect density inputs."""
import argparse
from collections import Counter
import json
from pathlib import Path
from verify_housing_audit import checksum


def verify_recheck(root,out):
    pins=json.loads((out/'integrity-manifest.json').read_text())
    for group in ['inputs','outputs']:
        for name,digest in pins[group].items():
            path=(root/name).resolve()
            if root not in path.parents or checksum(path)!=digest:raise ValueError(f'Integrity failed: {name}')
    counts=Counter();flags=Counter();ids=set()
    with (out/'building-link-assessments.jsonl').open() as stream:
        for line in stream:
            row=json.loads(line)
            if row['osm_id'] in ids:raise ValueError('Duplicate contour assessment')
            ids.add(row['osm_id']);counts['contours']+=1
            if row['address_assessments']:counts['contours_with_candidates']+=1
            if row['resettlement_register_evidence']:counts['emergency_program_address_candidates']+=1
            if 'osm_vacancy_or_construction_signal_unverified' in row['occupancy_review_flags']:counts['osm_occupancy_risk_signals']+=1
            if any(row[k] for k in ['geometry_verified','occupancy_verified','weight_override_allowed']):raise ValueError('Unverified evidence became confirmed')
            if row['current_occupied_fraction'] is not None or row['current_residents'] is not None:raise ValueError('Current occupancy fabricated from register/program')
            for candidate in row['address_assessments']:
                counts[candidate['status']]+=1;flags.update(candidate['flags'])
                if candidate['geometry_verified'] or candidate['occupancy_verified']:raise ValueError('Text-only match became independently verified')
                if 'street_type_conflict' in candidate['flags'] and candidate['status']!='quarantined_text_conflict':raise ValueError('Street type conflict not quarantined')
    summary=json.loads((out/'recheck-summary.json').read_text())
    if dict(counts)!=summary['counts'] or dict(flags)!=summary['candidate_flag_counts']:raise ValueError('Summary differs from full assessment')
    protected=json.loads((root/'sources/housing-verification-20261005/protected-before.json').read_text())
    changed=[name for name,digest in protected.items() if checksum(root/name)!=digest]
    if changed:raise ValueError(f'Protected map/source data changed: {changed}')
    return {'contours_verified':len(ids),'source_and_output_integrity':'passed',
            'protected_data_files_unchanged':len(protected),'confirmed_geometry_matches':0,'confirmed_current_occupancy':0,'applied_population_weight_overrides':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve()
    if root/'review' not in out.parents:raise ValueError('Expected private new-project review directory')
    result=verify_recheck(root,out);(out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result))
