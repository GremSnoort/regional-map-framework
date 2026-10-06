"""Verify a self-contained, legacy-only setup without reading the old project."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def local_file(root, relative):
    path = root / relative
    if root.resolve() not in path.resolve().parents or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError(f'Non-local input or symlink: {relative}')
    if not path.is_file():
        raise ValueError(f'Missing local input: {relative}')
    return path


def verify(root):
    root = root.resolve()
    manifest = json.loads(local_file(root, 'sources/legacy-snapshot-manifest.json').read_text())
    if manifest.get('policy') != 'LEGACY_ONLY_OFFLINE':
        raise ValueError('Active dataset must use legacy-only policy')
    records = {}
    for record in manifest['files']:
        name = record['path']
        if name in records:
            raise ValueError('Duplicate legacy snapshot path')
        path = local_file(root, str(Path(manifest['snapshot_root']) / name))
        if path.stat().st_size != record['bytes'] or checksum(path) != record['sha256']:
            raise ValueError(f'Legacy snapshot changed: {name}')
        records[name] = record
    comparisons = {f'sources/baseline/{name}.geojson': f'data/{name}.geojson' for name in ['municipalities','settlements','stores','expansion_priority','major_roads','federal_roads','settlement_density']}
    comparisons.update({f'sources/{name}':f'sources/{name}' for name in ['rosstat_municipal_population_2025.xlsx', 'rosstat_vpn2020_table5.xlsx']})
    comparisons.update({'pipeline/legacy-density-config.json':'pipeline/settlement_density_config.json'})
    for active, legacy in comparisons.items():
        if checksum(local_file(root, active)) != records[legacy]['sha256']:
            raise ValueError(f'Active input differs from legacy snapshot: {active}')
    active_config = json.loads(local_file(root, 'pipeline/priority_config.json').read_text())
    legacy_config = json.loads(local_file(root, 'sources/legacy-snapshot/pipeline/priority_config.json').read_text())
    if {k:v for k,v in active_config.items() if k != 'build_date'} != {k:v for k,v in legacy_config.items() if k != 'build_date'}:
        raise ValueError('Commercial model parameters differ from legacy snapshot')
    extraction = json.loads(local_file(root, 'sources/osm-cache/osm-extraction.json').read_text())
    pbf = records['sources/cache/far-eastern-fed-district-latest.osm.pbf']
    if extraction['pbf_sha256'] != pbf['sha256'] or extraction['extraction_script_sha256'] != checksum(local_file(root, 'pipeline/extract_osm.py')):
        raise ValueError('OSM extraction cannot be traced to local legacy PBF and current extractor')
    derived = json.loads(local_file(root, 'sources/local-derived-manifest.json').read_text())
    for record in derived['files']:
        if checksum(local_file(root, record['path'])) != record['sha256']:
            raise ValueError(f'Derived local source changed: {record["path"]}')
    review = json.loads(local_file(root, 'sources/building-review.json').read_text())
    if review['exclusions']:
        raise ValueError('Legacy-only setup cannot apply external manual exclusions')
    housing = json.loads(local_file(root, 'review/housing-register.json').read_text())
    if set(housing['sources']) != {'legacy-osm'} or any(f['status']=='confirmed' for b in housing['buildings'] for f in b['fields'].values()):
        raise ValueError('Legacy-only housing register cannot apply external or independently confirmed facts')
    source = housing['sources']['legacy-osm']
    if source['snapshot'] != 'sources/osm-cache/osm-buildings.geojson' or source['sha256'] != checksum(local_file(root,source['snapshot'])):
        raise ValueError('Housing evidence must be the local legacy OSM extraction')
    return {'policy':manifest['policy'], 'snapshot_files':len(records), 'independent_of_legacy_directory':True,
            'external_downloads_required':False, 'source_dates_unchanged':True, 'housing_registered_contours':len(housing['buildings'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--region-root',type=Path,required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.region_root),ensure_ascii=False))
