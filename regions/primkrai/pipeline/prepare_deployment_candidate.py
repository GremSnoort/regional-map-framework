"""Assemble a separate current-map candidate; never replace active regional data."""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from housing_match_v3 import locality_name
from verify_housing_audit import checksum
from verify_housing_v3 import verify_v3
from verify_local_setup import verify
from verify_quality_six_point import verify_quality
from verify_store_catalog_audit import verify_stores


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def prepare(destination):
    root = Path(__file__).resolve().parents[1]
    project = root.parents[1]
    destination = destination.resolve()
    if project/'deployment' not in destination.parents or destination.exists():
        raise ValueError('Use a new private directory below deployment/')
    housing = read(root/'sources/housing-current.json')
    quality = read(root/'sources/quality-current.json')
    stores = read(root/'sources/stores-current.json')
    for pointer, field in [(housing, 'integrity_manifest_sha256'), (quality, 'dataset_manifest_sha256'), (stores, 'integrity_manifest_sha256')]:
        if checksum(root/pointer['dataset']/'integrity-manifest.json') != pointer[field]:
            raise ValueError('Current dataset pointer does not match manifest')
    display = root/quality['density_quality_overlay']
    display_pins = read(display.parent/'integrity-manifest.json')
    if checksum(display.parent/'integrity-manifest.json') != quality['display_manifest_sha256']:
        raise ValueError('Display manifest changed')
    for group in ['inputs', 'outputs']:
        for name, digest in display_pins[group].items():
            if checksum(root/name) != digest:
                raise ValueError('Display input/output changed: '+name)
    verification = {
        'local': verify(root),
        'housing': verify_v3(root, root/housing['dataset']),
        'quality': verify_quality(root, root/quality['dataset']),
        'stores': verify_stores(root, root/stores['dataset']),
    }
    target = destination/'content/regions/primkrai'
    data = target/'data'
    data.mkdir(parents=True)
    inputs = {name: checksum(root/name) for name in [
        'pipeline/prepare_deployment_candidate.py', 'pipeline/build_priority.py',
        'pipeline/priority_config.json', 'pipeline/housing_match_v3.py', 'region.json',
        'sources/housing-current.json', 'sources/quality-current.json', 'sources/stores-current.json']}

    def transport(source, name):
        inputs[str(source.relative_to(root))] = checksum(source)
        shutil.copy2(source, data/name)

    cfg = read(root/'region.json')
    cfg['data_mode'] = 'external'
    cfg['lifecycle'] = 'draft'
    for spec in cfg['layers'].values():
        transport(root/spec['file'], Path(spec['file']).name)
    transport(root/stores['stores'], 'stores.geojson')
    transport(root/quality['settlements_refined'], 'settlements.geojson')
    # Missing population is null, never a fabricated zero. Numeric validation
    # of remaining known controls is performed by the quality verifier above.
    cfg['layers']['settlements']['numeric_properties'] = []
    cfg['layers']['stores']['popup_fields'] += [
        {'field': 'catalog_observed_on', 'label': 'Дата каталога'},
        {'field': 'status', 'label': 'Проверка работы магазина'},
        {'field': 'coordinate_verified', 'label': 'Координаты независимо подтверждены'},
    ]
    # Re-render all 6,566 original review geometries from the current v3
    # inventory, rather than publishing obsolete matching and housing claims.
    layer = read(data/'housing_review.geojson')
    wanted = {f['properties']['osm_id'] for f in layer['features']}
    rows = {}
    inventory = root/housing['inventory']
    inputs[str(inventory.relative_to(root))] = checksum(inventory)
    with inventory.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['osm_id'] in wanted:
                rows[row['osm_id']] = row
    if set(rows) != wanted:
        raise ValueError('Housing review contour absent from v3 inventory')
    for feature in layer['features']:
        previous = feature['properties']; row = rows[previous['osm_id']]
        props = {k: previous[k] for k in ['osm_id', 'address', 'settlement']}
        props.update(match_status=row['match']['status'], geometry_verified=False,
                     occupancy_verified=False, applied_to_density=False,
                     source_url=previous['osm_url'],
                     official_address_candidates=len(row['match']['links']),
                     review_notes='Реестр v3. Адресные кандидаты не подтверждают кадастровую привязку и заселённость.')
        for key in ['use', 'above_ground_storeys', 'residential_premises_area_m2', 'apartments']:
            props[key+'_value'] = row['fields'][key]['value']
            props[key+'_status'] = row['fields'][key]['status']
        props['resettlement_note'] = 'Адресные сведения программы; завершение и проживание не подтверждены' if row['resettlement_evidence'] else 'Нет подтверждения текущей заселённости'
        feature['properties'] = props
    layer['metadata'] = {'inventory_version': 3, 'coverage': '6566 model-area contours, not all regional buildings', 'applied_density_overrides': 0}
    write(data/'housing_review.geojson', layer)
    cfg['layers']['housing_review']['popup_fields'] = [
        {'field': k, 'label': label} for k, label in [
            ('match_status', 'Сопоставление v3'), ('official_address_candidates', 'Адресные кандидаты'),
            ('use_value', 'Сообщённое назначение'), ('use_status', 'Статус назначения'),
            ('above_ground_storeys_value', 'Сообщённая этажность'), ('above_ground_storeys_status', 'Статус этажности'),
            ('residential_premises_area_m2_value', 'Жилая площадь, м²'), ('residential_premises_area_m2_status', 'Статус жилой площади'),
            ('apartments_value', 'Квартиры'), ('apartments_status', 'Статус квартир'),
            ('geometry_verified', 'Кадастровая привязка подтверждена'), ('occupancy_verified', 'Заселённость подтверждена'),
            ('resettlement_note', 'Расселение'), ('review_notes', 'Ограничение')]]
    transport(display, 'density_quality_overlay.geojson')
    overlay = copy.deepcopy(cfg['layers']['housing_review'])
    overlay.update(file='data/density_quality_overlay.geojson', label='Чувствительность модели плотности',
                   required_properties=['cell_id', 'baseline_population'], unique_by=['cell_id'], title_field='settlement_name',
                   popup_fields=[{'field': k, 'label': label} for k, label in [
                       ('baseline_population', 'Базовая оценка'), ('alternative_scenario_min_population', 'Минимум пяти сценариев'),
                       ('alternative_scenario_max_population', 'Максимум пяти сценариев'),
                       ('population_as_of', 'Дата контроля населения'), ('sensitivity_range_type', 'Сценарии допущений, не доверительный интервал'),
                       ('independent_validation_status', 'Независимая точность')]])
    cfg['layers']['density_quality'] = overlay
    # The historical priority formula counts locality labels literally. Feed
    # canonical locality names to avoid treating rural prefixes as new towns.
    priority_input = destination/'priority-input'
    priority_input.mkdir()
    for name in ['settlements.geojson', 'stores.geojson', 'major_roads.geojson', 'federal_roads.geojson']:
        payload = read(data/name)
        if name in ['settlements.geojson', 'stores.geojson']:
            field = 'name' if name == 'settlements.geojson' else 'locality'
            for feature in payload['features']:
                feature['properties'][field] = locality_name(feature['properties'][field])
        write(priority_input/name, payload)
    pc = read(root/'pipeline/priority_config.json')
    pc.update(source_dir='priority-input', build_date='2026-10-06')
    write(destination/'priority-config.json', pc)
    env = dict(os.environ, ANALYTICAL_REGION_ROOT=str(destination), ANALYTICAL_OUTPUT_DIR=str(data))
    subprocess.run([sys.executable, '-B', str(root/'pipeline/build_priority.py'), '--config', 'priority-config.json'], env=env, check=True)
    priorities = read(data/'expansion_priority.geojson')
    names = {str(f['properties']['osm_id']): f['properties']['name'] for f in read(data/'settlements.geojson')['features']}
    counts = Counter(locality_name(f['properties']['locality']) for f in read(data/'stores.geojson')['features'])
    for feature in priorities['features']:
        p = feature['properties']; p['name'] = names[p['settlement_key'].split('/')[-1]]
        if counts[locality_name(p['name'])] and p['existing_pyaterochka_cards'] != counts[locality_name(p['name'])]:
            raise ValueError('Priority catalog count mismatch')
        p['warning'] += ' Карточки каталога на 06.10.2026; текущая работа независимо не подтверждена.'
    write(data/'expansion_priority.geojson', priorities)
    cfg['expected'] = {key: len(read(target/spec['file'])['features']) for key, spec in cfg['layers'].items()}
    cfg['source_note'] += ' Каталог 5dfo обновлён на 06.10.2026: 170 карточек, работа магазинов независимо не подтверждена. Привязки жилья v3 показаны без изменения весов плотности. Слой чувствительности показывает сценарии допущений, не измеренную точность.'
    cfg['status_note'] = 'Кандидат свежей карты: источники населения 2021/2025; заселённость и точность распределения по зданиям не подтверждены.'
    write(target/'region.json', cfg)
    write(destination/'content/registry.json', {'schema_version': 1, 'default_region': 'primkrai', 'regions': ['primkrai']})
    for name in ['SOURCES.md', 'LOCAL_DATA_SETUP.md', 'METHOD_DENSITY.md', 'HOUSING_CORRECTIONS_V3.md', 'QUALITY_REVIEW_2026-10-06.md', 'STORE_AUDIT_2026-10-06.md']:
        shutil.copy2(root/name, target/name)
    with (target/'SOURCES.md').open('a', encoding='utf-8') as stream:
        stream.write('\n\nКандидат деплоя 06.10.2026: 170 карточек каталога 5dfo, уточнённые 647 населённых пунктов, приоритет пересчитан. Жилищные привязки v3 — только очередь проверки; веса плотности не менялись. Сценарии чувствительности — не доверительный интервал. См. STORE_AUDIT_2026-10-06.md и QUALITY_REVIEW_2026-10-06.md.\n')
    write(destination/'preparation.json', {'verification': verification, 'expected': cfg['expected'], 'inputs_sha256': inputs,
          'local_active_data_replaced': False, 'remote_deployment_performed': False,
          'outputs_sha256': {str(p.relative_to(destination)): checksum(p) for p in sorted(data.glob('*.geojson'))}})
    print(json.dumps(cfg['expected']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    prepare(parser.parse_args().destination)
