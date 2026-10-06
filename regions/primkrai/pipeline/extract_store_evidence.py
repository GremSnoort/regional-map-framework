"""Extract dated primary address evidence and OSM brand observations locally."""
import hashlib,json,re,subprocess
from pathlib import Path
import osmium
from shapely.geometry import Point,shape
from shapely.ops import unary_union
from shapely.prepared import prep


def main():
    root=Path(__file__).resolve().parents[1];out=root/'sources/store-audit-20261006'
    pdf=out/'5ka-combo-addresses.pdf';text=subprocess.check_output(['pdftotext','-layout',str(pdf),'-']).decode();(out/'5ka-combo-addresses.txt').write_text(text)
    rows=[]
    for page,body in enumerate(text.split('\f'),1):
        for line_num,line in enumerate(body.splitlines(),1):
            if 'Приморский Край' not in line:continue
            pieces=[x.strip() for x in line.split(',')];pieces=pieces[pieces.index('Приморский Край')+1:]
            street=next((i for i,x in enumerate(pieces) if re.search(r'\b(?:Ул|Пр-Кт|Пр-Т|Пер|Б-Р|Ш)\s*$',x,re.I)),None)
            if street is None or street==0:continue
            locality=re.sub(r'\s+(?:Г|Пгт|С|П|Пос|Рп|Кп)$','',pieces[street-1],flags=re.I)
            address=', '.join(pieces[street:]);rows.append({'locality':locality,'address':address,'source_url':'https://media.5ka.ru/media/hosting/file/combo-econombo_20.02.pdf','source_locator':f'PDF page {page}, extracted line {line_num}',
                'raw_address':line.strip(),'promo_period_start':'2024-02-20','promo_period_end':'2025-12-31','measurement_as_of':None,'scope':'Listed in appendix of stores not participating in promotion; not evidence of closure or current operation','coordinate_confirmation':False})
    (out/'official-address-evidence.json').write_text(json.dumps({'source_pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'addresses':rows,'coverage':'promotion appendix only, not whole chain registry'},ensure_ascii=False,indent=2)+'\n')
    buildings=json.loads((root/'sources/osm-cache/osm-buildings.geojson').read_text())['features']
    by_way={f['properties']['osm_id']:f for f in buildings if f['properties']['osm_type']=='way' and any('пятероч' in str(v).casefold().replace('ё','е') or 'pyateroch' in str(v).casefold() for k,v in f['properties']['tags'].items() if k in {'name','brand','operator'})};del buildings
    municipality=prep(unary_union([shape(f['geometry']) for f in json.loads((root/'data/municipalities.geojson').read_text())['features']]))
    stores=[]
    def tagged(tags):return any('пятероч' in tags.get(k,'').casefold().replace('ё','е') or 'pyateroch' in tags.get(k,'').casefold() for k in ['name','brand','operator','name:ru','name:en'])
    def selected(tags):return {t.k:t.v for t in tags if t.k in ['name','brand','operator','shop','opening_hours','disused:shop','abandoned:shop'] or t.k.startswith('addr:')}
    class Reader(osmium.SimpleHandler):
        def node(self,node):
            if len(node.tags)==0:return
            if not tagged(node.tags) or not node.location.valid():return
            xy=[node.location.lon,node.location.lat]
            if municipality.covers(Point(xy)):stores.append({'osm_id':f'node/{node.id}','coordinates':xy,'coordinate_kind':'osm_node','tags':selected(node.tags)})
        def way(self,way):
            if not tagged(way.tags) or way.id not in by_way:return
            geom=shape(by_way[way.id]['geometry']);point=geom.representative_point()
            if municipality.covers(point):stores.append({'osm_id':f'way/{way.id}','coordinates':[point.x,point.y],'coordinate_kind':'building_representative_point_not_entrance','tags':selected(way.tags)})
    pbf=root/'sources/legacy-snapshot/sources/cache/far-eastern-fed-district-latest.osm.pbf'
    Reader().apply_file(str(pbf),filters=[osmium.filter.KeyFilter('name','brand','operator','name:ru','name:en')])
    with pbf.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'osm-store-evidence.json').write_text(json.dumps({'source_pbf_sha256':digest,'osm_snapshot_as_of':'2026-09-08T20:21:01Z','extractor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'stores':stores,'limitation':'Name/brand observations from OSM; no proof of current operation or completeness'},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'historical_primary_address_rows':len(rows),'osm_brand_observations':len(stores)}))

if __name__=='__main__':main()
