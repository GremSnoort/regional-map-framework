"""Verify the private regional inventory, its source graph and integrity pins."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
from housing_inventory import FIELDS, compile_verified_weights


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def record_integrity(root,out):
    """Pin inputs and completed outputs; publishing these pins is local only."""
    names=['pipeline/audit_housing_region.py','pipeline/housing_inventory.py',
           'pipeline/verify_housing_audit.py','pipeline/housing-inventory.schema.json',
           'pipeline/housing-audit-requirements.txt','pipeline/requirements-lock.txt',
           'pipeline/build_density.py','sources/osm-cache/osm-buildings.geojson',
           'sources/osm-cache/housing-review-domains.geojson','data/municipalities.geojson']
    paths=[root/n for n in names]+[p for p in (root/'sources/housing-audit-20261005').iterdir() if p.is_file()]
    outputs=[p for p in out.iterdir() if p.is_file() and p.name not in {'integrity-manifest.json','verification.json'}]
    manifest={'schema_version':1,'inputs':{str(p.relative_to(root)):checksum(p) for p in sorted(paths)},
              'outputs':{str(p.relative_to(root)):checksum(p) for p in sorted(outputs)}}
    (out/'integrity-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')


def verify(root, out):
    manifest=json.loads((out/'integrity-manifest.json').read_text())
    for group in ['inputs','outputs']:
        for name,digest in manifest[group].items():
            path=(root/name).resolve()
            if root not in path.parents or checksum(path)!=digest:
                raise ValueError(f'Integrity check failed: {name}')
    summary=json.loads((out/'audit-summary.json').read_text())
    catalog=json.loads((out/'sources.json').read_text())['sources']
    official={r['house_id'] for r in csv.DictReader((out/'official-housing-addresses.csv').open(encoding='utf-8-sig'))}
    counts=Counter();issues=Counter();ids=set();confirmed=[]
    with (out/'housing-inventory-v2.jsonl').open() as stream:
        for line in stream:
            record=json.loads(line)
            if record['schema_version']!=2 or record['osm_id'] in ids:
                raise ValueError('Invalid inventory version or duplicate contour')
            ids.add(record['osm_id']);counts['buildings']+=1
            if record['model_settlement']:counts['in_68_model_domains']+=1
            match=record['match']
            if set(match['candidate_house_ids'])-official:raise ValueError('Unknown official address candidate')
            if match['candidate_house_ids']:counts['with_official_address_candidates']+=1
            if set(record['fields'])!=set(FIELDS):raise ValueError('Measurement fields changed')
            for name,field in record['fields'].items():
                counts[f'{name}:{field["status"]}']+=1
                values=[o['value'] for o in field['observations']]
                if field['status']=='unknown' and (values or field['value'] is not None):raise ValueError('Unknown field has a fabricated value')
                if field['status']=='conflicting' and (field['value'] is not None or len(set(values))<2):raise ValueError('Incorrect conflict state')
                if field['status']=='reported' and (not values or any(v!=field['value'] for v in values)):raise ValueError('Reported value does not match evidence')
                for observation in field['observations']:
                    if observation['source_id'] not in catalog or not observation['locator']:raise ValueError('Untraceable observation')
            issues.update(record['issues'])
            if match['status']=='confirmed':confirmed.append(record)
            if record['independent_occupancy_verified']:raise ValueError('Current audit has no independent occupancy evidence')
    if dict(counts)!=summary['coverage'] or dict(issues)!=summary['issue_counts']:raise ValueError('Summary does not reconcile to the full inventory')
    if len(official)!=summary['official_rows']:raise ValueError('Official row count changed')
    weights=compile_verified_weights(confirmed,catalog)
    if weights or confirmed or summary['automatic_model_overrides_applied']!=0:raise ValueError('Unreviewed evidence must not alter population weights')
    features=json.loads((out/'regional_housing_findings.geojson').read_text())['features']
    mapped=[f['properties']['osm_id'] for f in features]
    if len(mapped)!=summary['map_findings'] or len(set(mapped))!=len(mapped) or set(mapped)-ids:raise ValueError('Invalid map finding references')
    return {'verified_contours':len(ids),'official_address_rows':len(official),'map_findings':len(mapped),'source_and_output_hashes':'passed','population_overrides':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve()
    if root/'review' not in out.parents:raise ValueError('Expected private review output in the new project')
    print(json.dumps(verify(root,out),ensure_ascii=False))
