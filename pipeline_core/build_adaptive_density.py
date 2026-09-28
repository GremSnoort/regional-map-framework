#!/usr/bin/env python3
"""Build a spatially adaptive population grid from official controls and OSM buildings.

The finest cell is used only where the residential fabric is sufficiently
complex. Sparse blocks remain coarse. Population controls are conserved exactly;
the result is a model, not official block-level statistics.
"""
from __future__ import annotations
import argparse,json,math,os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import osmium
from shapely.geometry import box,mapping,shape
from shapely.ops import transform,unary_union
from shapely.prepared import prep

RESIDENTIAL={"apartments","residential","house","detached","terrace","dormitory","semidetached_house","bungalow","static_caravan","ger"}
NON_RESIDENTIAL={"commercial","retail","industrial","warehouse","school","kindergarten","hospital","office","church","garage","garages","service","train_station","public","civic","sports_centre","construction"}

def factor(tags):
    kind=tags.get("building","")
    if kind in RESIDENTIAL:return 1.0,"explicit_residential"
    if kind in NON_RESIDENTIAL:return 0.0,"excluded_non_residential"
    if kind=="yes" and (tags.get("addr:housenumber") or tags.get("addr:street")):return .55,"ambiguous_addressed"
    return 0.0,"excluded_unclassified"
def levels(tags):
    try:
        value=float(tags.get("building:levels","").replace(",","."))
        if .5<=value<=80:return value
    except ValueError:pass
    return 5.0 if tags.get("building") in {"apartments","dormitory"} else 2.0 if tags.get("building")=="residential" else 1.0
def rule(pop,rules,key):
    for item in rules:
        if pop>=item["minimum_population"]:return item[key]
    raise ValueError(f"No adaptive rule for population {pop}")

def adaptive_rows(rows,min_grid,max_grid,refinement):
    """Merge fine occupied cells into a non-overlapping quadtree grid."""
    if max_grid%min_grid or max_grid<min_grid:
        raise ValueError("max grid must be an integer multiple of min grid")
    factor=max_grid//min_grid
    if factor&(factor-1):raise ValueError("max/min grid ratio must be a power of two")
    split_buildings=int(refinement.get("split_buildings",12))
    split_proxy_density=float(refinement.get("split_proxy_m2_per_km2",80_000))
    result={}
    def aggregate(keys):
        values=[rows[key] for key in keys]
        return {"proxy":sum(v["proxy"] for v in values),"buildings":sum(v["buildings"] for v in values),"explicit":sum(v["explicit"] for v in values)}
    def visit(ix,iy,span,available):
        keys=[key for key in available if ix<=key[0]<ix+span and iy<=key[1]<iy+span]
        if not keys:return
        value=aggregate(keys);area_km2=((span*min_grid)/1000)**2
        complex_block=value["buildings"]>=split_buildings or value["proxy"]/area_km2>=split_proxy_density
        if span>1 and complex_block:
            half=span//2
            for dx,dy in ((0,0),(half,0),(0,half),(half,half)):visit(ix+dx,iy+dy,half,keys)
        else:result[(ix,iy,span)]=value
    roots={(math.floor(ix/factor)*factor,math.floor(iy/factor)*factor) for ix,iy in rows}
    for ix,iy in sorted(roots):visit(ix,iy,factor,list(rows))
    return result
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);args=parser.parse_args()
    root=Path(os.environ["ANALYTICAL_REGION_ROOT"]).resolve();out=Path(os.environ["ANALYTICAL_OUTPUT_DIR"]).resolve()
    config=json.loads((root/args.config).read_text(encoding="utf-8"));priority=json.loads((out/config["priority_output"]).read_text(encoding="utf-8"))
    excluded=set(config.get("exclude_settlements",[]));included=set(config.get("include_settlements",[]));candidates=[]
    for feature in priority["features"]:
        p=feature["properties"];pop=int(p.get("population",0));capacity=int(p.get("estimated_additional_capacity",0))
        if (capacity<=0 and p.get("name") not in included) or p.get("name") in excluded:continue
        lon,lat=feature["geometry"]["coordinates"];selected=next(item for item in config["adaptive_rules"] if pop>=item["minimum_population"]);max_grid=int(selected["grid_metres"]);grid=int(selected.get("min_grid_metres",config.get("refinement",{}).get("min_grid_metres",max_grid)));radius=float(selected["radius_km"])
        mx=111_320*math.cos(math.radians(lat));my=110_574
        district=p.get("district",p.get("municipality",""))
        candidate_key=f"{p['name']}|{district}|{lon:.7f}|{lat:.7f}"
        candidates.append({"key":candidate_key,"name":p["name"],"district":district,"population":pop,"quality":p.get("population_quality",p.get("confidence","unknown")),"source":p.get("population_source",""),"as_of":p.get("population_as_of"),"lon":lon,"lat":lat,"grid":grid,"max_grid":max_grid,"radius":radius,"mx":mx,"my":my})
    factory=osmium.geom.GeoJSONFactory();boundary_specs=config.get("settlement_boundaries",{});boundary_keys={}
    for name,spec in boundary_specs.items():boundary_keys[(spec.get("osm_area_type")=="way",int(spec["osm_area_id"]))]=name
    boundaries={};boundary_geometries={}
    if boundary_keys:
        class BoundaryHandler(osmium.SimpleHandler):
            def area(self,area):
                name=boundary_keys.get((area.from_way(),area.orig_id()))
                if not name:return
                try:
                    geometry=shape(json.loads(factory.create_multipolygon(area)))
                    boundary_geometries[name]=geometry;boundaries[name]=prep(geometry)
                except Exception as error:raise RuntimeError(f"Cannot construct settlement boundary for {name}") from error
        BoundaryHandler().apply_file(str(root/config["pbf"]))
        missing=sorted(set(boundary_specs)-set(boundaries))
        if missing:raise RuntimeError(f"Configured settlement boundaries missing from PBF: {missing}")
    for c in candidates:c["boundary"]=boundaries.get(c["name"])
    protect_boundaries=bool(config.get("protect_configured_boundaries",False))
    protected_union=unary_union(list(boundary_geometries.values())) if boundary_geometries else None
    # Names are not regionally unique (for example, several villages can be
    # called Sergeevka). Keep model state by a stable settlement key rather
    # than silently merging equally named controls.
    cells={c["key"]:defaultdict(lambda:{"proxy":0.0,"buildings":0,"explicit":0.0}) for c in candidates}
    class Handler(osmium.SimpleHandler):
        def area(self,area):
            tags={t.k:t.v for t in area.tags};weight,quality=factor(tags)
            if weight==0:return
            try:geom=shape(json.loads(factory.create_multipolygon(area)))
            except Exception:return
            point=geom.representative_point();matches=[]
            for c in candidates:
                dx=(point.x-c["lon"])*c["mx"]/1000;dy=(point.y-c["lat"])*c["my"]/1000;distance=math.hypot(dx,dy)
                if distance>c["radius"] or (c["boundary"] is not None and not c["boundary"].covers(point)):continue
                # Radius-only models must not borrow buildings from a neighbouring
                # settlement whose explicit boundary is known.
                if protect_boundaries and c["boundary"] is None and any(
                    other_name!=c["name"] and boundary.covers(point)
                    for other_name,boundary in boundaries.items()
                ):continue
                matches.append((distance/c["radius"],distance,c))
            if not matches:return
            _,_,c=min(matches,key=lambda item:(item[0],item[1],item[2]["name"]));project=lambda x,y,z=None:((x-c["lon"])*c["mx"],(y-c["lat"])*c["my"])
            projected=transform(project,geom);area_m2=projected.area
            if not 12<=area_m2<=80_000:return
            proxy=area_m2*levels(tags)*weight;center=projected.representative_point();key=math.floor(center.x/c["grid"]),math.floor(center.y/c["grid"])
            if protect_boundaries and c["boundary"] is None and protected_union is not None:
                unproject=lambda x,y,z=None:(x/c["mx"]+c["lon"],y/c["my"]+c["lat"])
                candidate_square=transform(unproject,box(key[0]*c["grid"],key[1]*c["grid"],(key[0]+1)*c["grid"],(key[1]+1)*c["grid"]))
                if candidate_square.intersection(protected_union).area>1e-15:return
            cell=cells[c["key"]][key];cell["proxy"]+=proxy;cell["buildings"]+=1
            if quality=="explicit_residential":cell["explicit"]+=proxy
    Handler().apply_file(str(root/config["pbf"]));features=[];summary=[]
    # A configured-boundary model keeps complete squares, including squares that
    # cross its boundary.  Reserve those exact squares before allocating nearby
    # radius-only models so two settlement controls can never paint the same area.
    protect_grid_cells=bool(config.get("protect_configured_grid_cells",False))
    def square_for(c,key):
        ix,iy,*rest=key;span=rest[0] if rest else 1;grid=c["grid"]
        unproject=lambda x,y,z=None:(x/c["mx"]+c["lon"],y/c["my"]+c["lat"])
        return transform(unproject,box(ix*grid,iy*grid,(ix+span)*grid,(iy+span)*grid))
    protected_squares=[]
    if protect_grid_cells:
        for c in candidates:
            if c["boundary"] is not None:
                protected_squares.extend(square_for(c,key) for key in cells[c["key"]])
    protected_grid_union=unary_union(protected_squares) if protected_squares else None
    if protected_grid_union is not None:
        for c in candidates:
            if c["boundary"] is None:
                cells[c["key"]]=defaultdict(lambda:{"proxy":0.0,"buildings":0,"explicit":0.0},{
                    key:value for key,value in cells[c["key"]].items()
                    if not square_for(c,key).intersects(protected_grid_union)
                })
    for c in candidates:
        rows=cells[c["key"]];total=sum(v["proxy"] for v in rows.values())
        if total<=0:raise RuntimeError(f"No residential building proxy for {c['name']}")
        rows=adaptive_rows(rows,c["grid"],c["max_grid"],config.get("refinement",{}))
        raw={k:c["population"]*v["proxy"]/total for k,v in rows.items()};allocated={k:math.floor(v) for k,v in raw.items()};remainder=c["population"]-sum(allocated.values())
        for key in sorted(raw,key=lambda k:raw[k]-math.floor(raw[k]),reverse=True)[:remainder]:allocated[key]+=1
        for (ix,iy,span),row in sorted(rows.items()):
            people=allocated[(ix,iy,span)]
            if people<=0:continue
            grid=c["grid"]*span;square=square_for(c,(ix,iy,span));cell_area_km2=(grid/1000)**2
            share=row["explicit"]/row["proxy"] if row["proxy"] else 0
            confidence="средняя" if row["buildings"]>=max(3,round(8*(grid/500)**2)) and share>=.7 else "низкая"
            props={"cell_id":f"{config['region_id']}_{len(summary)}_{grid}_{ix}_{iy}","settlement_key":c["key"],"settlement_name":c["name"],"municipality":c["district"],"population_estimate":people,"density_per_km2":round(people/cell_area_km2),"cell_area_km2":cell_area_km2,"residential_floor_proxy_m2":round(row["proxy"]),"building_count":row["buildings"],"explicit_residential_share":round(share,3),"confidence":confidence,"grid_metres":grid,"population_control":c["population"],"population_quality":c["quality"],"population_as_of":c["as_of"],"population_source":c["source"],"model":"единый адаптивный официальный/лучший доступный контроль + OSM-прокси жилой площади"}
            features.append({"type":"Feature","properties":props,"geometry":mapping(square)})
        actual=sum(f["properties"]["population_estimate"] for f in features if f["properties"]["settlement_key"]==c["key"])
        if actual!=c["population"]:raise AssertionError(f"Control mismatch for {c['name']}: {actual} != {c['population']}")
        sizes=sorted({f["properties"]["grid_metres"] for f in features if f["properties"]["settlement_key"]==c["key"]})
        summary.append({"key":c["key"],"name":c["name"],"district":c["district"],"population":c["population"],"grid_metres":sizes,"cells":sum(1 for f in features if f["properties"]["settlement_key"]==c["key"]),"buildings":sum(v["buildings"] for v in rows.values()),"quality":c["quality"],"boundary_constrained":c["boundary"] is not None})
    metadata={"generated_at":os.environ.get("GAB_BUILD_TIMESTAMP") or datetime.now().astimezone().isoformat(timespec="seconds"),"model_status":"MODELLED_NOT_OFFICIAL_BLOCK_STATISTICS","scope":"positive expansion-priority settlements plus explicitly included analytical centres","explicitly_included":sorted(included),"adaptive_rules":config["adaptive_rules"],"refinement":config.get("refinement",{}),"osm_snapshot_date":config["osm_snapshot_date"],"protect_configured_boundaries":protect_boundaries,"protect_configured_grid_cells":protect_grid_cells,"settlements":summary,"warning":"Локальная модель распределяет официальный/лучший доступный контроль по OSM-прокси жилой площади; размер ячейки адаптируется к сложности застройки и не означает официальную квартальную статистику."}
    (out/config["output"]).write_text(json.dumps({"type":"FeatureCollection","metadata":metadata,"features":features},ensure_ascii=False,separators=(",",":")),encoding="utf-8");print(json.dumps({"settlements":len(summary),"cells":len(features)},ensure_ascii=False))
if __name__=="__main__":main()
