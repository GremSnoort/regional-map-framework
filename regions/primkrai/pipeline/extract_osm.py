"""Extract the reviewed snapshot once; preserve building IDs and raw tags."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import osmium
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.prepared import prep


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--municipalities", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.pbf.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    reader = osmium.io.Reader(str(args.pbf))
    timestamp = reader.header().get("osmosis_replication_timestamp")
    reader.close()
    municipalities = json.loads(args.municipalities.read_text())
    region = prep(unary_union([shape(f["geometry"]) for f in municipalities["features"]]))
    config = json.loads(args.config.read_text())
    keys = {(v["osm_area_type"] == "way", v["osm_area_id"]): k for k, v in config["settlement_boundaries"].items()}
    buildings, boundaries, errors = [], {}, 0
    factory = osmium.geom.GeoJSONFactory()

    class Handler(osmium.SimpleHandler):
        def area(self, area):
            nonlocal errors
            boundary = keys.get((area.from_way(), area.orig_id()))
            tags = {t.k: t.v for t in area.tags}
            if not boundary and not tags.get("building"):
                return
            try:
                geometry = shape(json.loads(factory.create_multipolygon(area)))
            except Exception:
                errors += 1
                return
            osm_type = "way" if area.from_way() else "relation"
            record = {"type": "Feature", "properties": {"osm_id": area.orig_id(), "osm_type": osm_type, "tags": tags}, "geometry": mapping(geometry)}
            if boundary:
                boundaries[boundary] = record
            if tags.get("building") and region.covers(geometry.representative_point()):
                buildings.append(record)

    Handler().apply_file(str(args.pbf))
    if set(boundaries) != set(config["settlement_boundaries"]):
        raise ValueError("Configured city boundaries missing from snapshot")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "osm-buildings.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": buildings}, ensure_ascii=False, separators=(",", ":")))
    (args.output / "osm-boundaries.json").write_text(json.dumps(boundaries, ensure_ascii=False, separators=(",", ":")))
    (args.output / "osm-extraction.json").write_text(json.dumps({"buildings": len(buildings), "boundaries": {k: v["properties"] for k, v in boundaries.items()}, "geometry_decode_errors_in_district_extract": errors,
        "pbf_sha256": digest, "pbf_bytes": args.pbf.stat().st_size, "pbf_replication_timestamp": timestamp,
        "source_url": "https://download.geofabrik.de/russia/far-eastern-fed-district.html",
        "extraction_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, ensure_ascii=False, indent=2))
    print(json.dumps({"buildings": len(buildings), "boundaries": len(boundaries), "decode_errors": errors}))


if __name__ == "__main__":
    main()
