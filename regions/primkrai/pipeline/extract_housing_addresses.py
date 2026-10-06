"""Recover address nodes/associatedStreet links from the independent local PBF."""
import argparse
import hashlib
import json
from pathlib import Path
import osmium
from shapely.geometry import Point,shape
from shapely.ops import unary_union
from shapely.prepared import prep


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=args.output.resolve()
    if root/'sources' not in out.parents or root/'sources/legacy-snapshot' in out.parents:raise ValueError('Use a new source directory, outside the immutable snapshot')
    out.mkdir(parents=True,exist_ok=True)
    pbf=root/'sources/legacy-snapshot/sources/cache/far-eastern-fed-district-latest.osm.pbf'
    polygons=json.loads((root/'data/municipalities.geojson').read_text())['features']
    union=unary_union([shape(f['geometry']) for f in polygons]);region=prep(union);bounds=union.bounds
    buildings=json.loads((root/'sources/osm-cache/osm-buildings.geojson').read_text())['features']
    ids={(f['properties']['osm_type'][0],f['properties']['osm_id']) for f in buildings};del buildings
    nodes=[];relations=[];road_names={}
    class Reader(osmium.SimpleHandler):
        def node(self,node):
            if not node.tags.get('addr:housenumber') or not node.location.valid():return
            x,y=node.location.lon,node.location.lat
            if not bounds[0]<=x<=bounds[2] or not bounds[1]<=y<=bounds[3] or not region.covers(Point(x,y)):return
            tags={t.k:t.v for t in node.tags if t.k.startswith('addr:') or t.k=='entrance'}
            nodes.append({'osm_id':f'node/{node.id}','coordinates':[x,y],'tags':tags})
        def way(self,way):
            if way.tags.get('highway') and way.tags.get('name'):road_names[way.id]=way.tags.get('name')
        def relation(self,relation):
            if relation.tags.get('type') not in {'associatedStreet','street'}:return
            houses=[{'type':m.type,'id':m.ref} for m in relation.members if m.role in {'house','address'} and (m.type,m.ref) in ids]
            if houses:relations.append({'osm_id':f'relation/{relation.id}','name':relation.tags.get('name') or relation.tags.get('addr:street'),
                'houses':houses,'street_way_ids':[m.ref for m in relation.members if m.type=='w' and m.role=='street']})
    Reader().apply_file(str(pbf))
    for relation in relations:
        relation['street_names']=sorted({road_names[i] for i in relation['street_way_ids'] if i in road_names})
    with pbf.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    header=osmium.io.Reader(str(pbf));timestamp=header.header().get('osmosis_replication_timestamp');header.close()
    data={'schema_version':1,'pbf_sha256':digest,'osm_snapshot_as_of':timestamp,'extractor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'address_nodes':nodes,'associated_streets':relations,'limitation':'Same OSM snapshot, not independent cadastral/occupancy evidence'}
    (out/'osm-address-evidence.json').write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n')
    print(json.dumps({'address_nodes':len(nodes),'associated_streets':len(relations),'snapshot_as_of':timestamp}))


if __name__=='__main__':main()
