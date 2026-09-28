#!/usr/bin/env python3
"""Normalize a regional CSV or GeoJSON source into a declared GeoJSON layer."""
from __future__ import annotations
import argparse,csv,json,os
from pathlib import Path

def safe_path(root,relative,label):
    path=(root/relative).resolve()
    if path!=root and root not in path.parents: raise ValueError(f"{label} escapes regional directory: {relative}")
    return path

def value_at(properties,name):
    value=properties
    for part in name.split("."):
        if not isinstance(value,dict) or part not in value:return None
        value=value[part]
    return value

def number(value,name):
    if value is None or value=="":return None
    try: result=float(str(value).replace(" ","").replace(",","."))
    except (TypeError,ValueError) as error: raise ValueError(f"Cannot parse numeric field {name}: {value!r}") from error
    return int(result) if result.is_integer() else result

def load_source(path,config):
    kind=config.get("source_type","auto")
    if kind=="auto":kind="csv" if path.suffix.lower()==".csv" else "geojson"
    if kind=="geojson":
        payload=json.loads(path.read_text(encoding=config.get("encoding","utf-8-sig")))
        if payload.get("type")!="FeatureCollection" or not isinstance(payload.get("features"),list):raise ValueError("GeoJSON source must be a FeatureCollection")
        return payload["features"],payload.get("metadata") or {}
    if kind!="csv":raise ValueError("source_type must be auto, csv or geojson")
    lat_name,lon_name=config.get("latitude_field","latitude"),config.get("longitude_field","longitude")
    features=[]
    with path.open("r",encoding=config.get("encoding","utf-8-sig"),newline="") as stream:
        for row_number,row in enumerate(csv.DictReader(stream,delimiter=config.get("delimiter",",")),2):
            try:latitude,longitude=number(row.get(lat_name),lat_name),number(row.get(lon_name),lon_name)
            except ValueError as error:raise ValueError(f"{path}:{row_number}: {error}") from error
            if latitude is None or longitude is None or not -90<=latitude<=90 or not -180<=longitude<=180:raise ValueError(f"{path}:{row_number}: invalid latitude/longitude")
            features.append({"type":"Feature","properties":row,"geometry":{"type":"Point","coordinates":[longitude,latitude]}})
    return features,{}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);args=parser.parse_args()
    region=Path(os.environ["ANALYTICAL_REGION_ROOT"]).resolve();output_root=Path(os.environ["ANALYTICAL_OUTPUT_DIR"]).resolve()
    config_path=safe_path(region,args.config,"adapter config");config=json.loads(config_path.read_text(encoding="utf-8"))
    source=safe_path(region,config["source"],"source")
    if not source.is_file():raise ValueError(f"Source is missing: {config['source']}")
    output_name=Path(config["output"])
    if output_name.is_absolute() or ".." in output_name.parts or len(output_name.parts)!=1:raise ValueError("output must be one filename inside ANALYTICAL_OUTPUT_DIR")
    features,source_metadata=load_source(source,config);allowed=set(config.get("geometry_types",[]));mapping=config.get("property_map",{});constants=config.get("constants",{});numeric=set(config.get("numeric_fields",[]));required=set(config.get("required_fields",[]));deduplicate_by=config.get("deduplicate_by",[])
    normalized=[];seen=set()
    for index,feature in enumerate(features):
        if feature.get("type")!="Feature" or not isinstance(feature.get("properties"),dict) or not isinstance(feature.get("geometry"),dict):raise ValueError(f"Source feature {index} is invalid")
        geometry=feature["geometry"]
        if allowed and geometry.get("type") not in allowed:raise ValueError(f"Source feature {index} has forbidden geometry {geometry.get('type')}")
        properties={target:value_at(feature["properties"],source_name) for target,source_name in mapping.items()};properties.update(constants)
        for name in numeric:properties[name]=number(properties.get(name),name)
        missing=sorted(name for name in required if properties.get(name) in (None,""))
        if missing:raise ValueError(f"Source feature {index} misses required values: {missing}")
        key=tuple(str(properties.get(name,"")).strip().casefold() for name in deduplicate_by)
        if deduplicate_by and key in seen:continue
        seen.add(key);normalized.append({"type":"Feature","properties":properties,"geometry":geometry})
    payload={"type":"FeatureCollection","metadata":{**source_metadata,**config.get("metadata",{}),"normalized_by":"pipeline_core/normalize_layer.py","feature_count":len(normalized)},"features":normalized}
    output_root.mkdir(parents=True,exist_ok=True);(output_root/output_name).write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    print(f"Normalized {len(normalized)} features to {output_name}")
if __name__=="__main__":main()
