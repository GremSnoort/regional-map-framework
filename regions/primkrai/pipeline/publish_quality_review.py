"""Publish a validated local quality overlay and dataset pointer, not density."""
import argparse,csv,json
from pathlib import Path
from verify_housing_audit import checksum
from verify_quality_six_point import verify_quality


def publish(root,dataset):
    root=root.resolve();dataset=dataset.resolve();verification=verify_quality(root,dataset)
    out=root/'review/quality-six-point-20261006-display';out.mkdir(parents=True,exist_ok=True)
    source=root/'sources/quality-review-20261006/reviewed-source-addendum.json';facts=json.loads(source.read_text())
    for name,digest in facts['source_hashes'].items():
        if checksum(source.parent/name)!=digest:raise ValueError('Supplemental source changed')
    rows={r['cell_id']:r for r in csv.DictReader((dataset/'density-scenarios-by-cell.csv').open(encoding='utf-8-sig'))}
    features=[]
    for name in ['settlement_density','settlement_density_unverified']:
        for f in json.loads((root/'data'/f'{name}.geojson').read_text())['features']:
            p=f['properties'];r=rows[p['cell_id']];values=[float(r[key]) for key in ['footprint_only','explicit_residential_only','lifecycle_signals_excluded','assumed_storeys_half','assumed_storeys_double'] if r[key]]
            features.append({'type':'Feature','geometry':f['geometry'],'properties':{'cell_id':p['cell_id'],'settlement_name':p['settlement_name'],
                'population_control':p['population_control'],'population_as_of':p['population_as_of'],'population_quality':p['population_quality'],
                'baseline_population':p['population_estimate'],'alternative_scenario_min_population':min(values) if values else None,
                'alternative_scenario_max_population':max(values) if values else None,'grid_metres':p['grid_metres'],
                'sensitivity_range_type':'five_assumption_scenarios_not_confidence_interval','independent_validation_status':'not_available',
                'residential_area_verified':False,'occupancy_verified':False,'applied_to_active_density':False}})
    output=out/'density-quality-overlay.geojson';output.write_text(json.dumps({'type':'FeatureCollection','metadata':{'scope':'68 existing models; assumptions only','current_population_estimate':False,'confidence_interval':False},'features':features},ensure_ascii=False,separators=(',',':'))+'\n')
    pins={'inputs':{str(p.relative_to(root)):checksum(p) for p in [dataset/'integrity-manifest.json',source,root/'pipeline/publish_quality_review.py',root/'pipeline/verify_quality_six_point.py',root/'pipeline/test_quality_six_point.py']},'outputs':{str(output.relative_to(root)):checksum(output)}}
    manifest=out/'integrity-manifest.json';manifest.write_text(json.dumps(pins,ensure_ascii=False,indent=2)+'\n')
    pointer={'schema_version':1,'checked_on':'2026-10-06','role':'six_point_quality_review','dataset':str(dataset.relative_to(root)),
        'settlements_refined':str((dataset/'settlements-refined.geojson').relative_to(root)),
        'housing_assessments':str((dataset/'housing-evidence-assessment.jsonl').relative_to(root)),
        'density_quality_overlay':str(output.relative_to(root)),'dataset_manifest_sha256':checksum(dataset/'integrity-manifest.json'),
        'display_manifest_sha256':checksum(manifest),'source_addendum_sha256':checksum(source),
        'verification':verification,'active_density_changed':False,'independent_accuracy_measured':False}
    target=root/'sources/quality-current.json';tmp=target.with_suffix('.json.tmp');tmp.write_text(json.dumps(pointer,ensure_ascii=False,indent=2)+'\n');tmp.replace(target);print(json.dumps(pointer,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);args=parser.parse_args();publish(Path(__file__).resolve().parents[1],args.dataset)
