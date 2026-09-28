#!/usr/bin/env python3
"""Build federal and major-road GeoJSON using only declarative regional settings."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import osmium
from shapely.geometry import LineString, mapping, shape
from shapely.ops import unary_union


def normalized(value: str) -> str:
    return (value or "").upper().replace(" ", "").replace("–", "-").replace("—", "-").translate(str.maketrans({"Р": "R", "А": "A"}))


def parts(geometry):
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return list(geometry.geoms)
    return [item for item in getattr(geometry, "geoms", []) if item.geom_type == "LineString"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    region = Path(os.environ["ANALYTICAL_REGION_ROOT"])
    output = Path(os.environ["ANALYTICAL_OUTPUT_DIR"])
    config = json.loads((region / args.config).read_text(encoding="utf-8"))
    pbf = region / config["pbf"]
    boundaries = json.loads((region / config["boundaries"]).read_text(encoding="utf-8"))
    polygon = unary_union([shape(item["geometry"]) for item in boundaries["features"] if item.get("geometry")])
    routes = config.get("federal_routes", {})
    route_features = {name: [] for name in routes}
    major = []
    classes = set(config.get("major_highway_classes", ["trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link"]))
    variants = {name: [normalized(value) for value in values] for name, values in routes.items()}

    class Handler(osmium.SimpleHandler):
        def way(self, way):
            tags = {tag.k: tag.v for tag in way.tags}
            highway = tags.get("highway", "")
            if highway not in classes:
                return
            text = normalized(tags.get("ref", "") + " " + tags.get("name", ""))
            route = next((name for name, values in variants.items() if any(value in text for value in values)), None)
            try:
                coordinates = [(node.location.lon, node.location.lat) for node in way.nodes if node.location.valid()]
            except Exception:
                return
            if len(coordinates) < 2:
                return
            line = LineString(coordinates)
            if not line.intersects(polygon):
                return
            for item in parts(line.intersection(polygon)):
                if len(item.coords) < 2 or item.length == 0:
                    continue
                feature = {"type": "Feature", "properties": {"name": tags.get("name", ""), "ref": route or tags.get("ref", ""), "highway": highway, "osm_way_id": int(way.id)}, "geometry": mapping(item)}
                (route_features[route] if route else major).append(feature)

    Handler().apply_file(str(pbf), locations=True)
    timestamp = config.get("build_timestamp") or datetime.now().astimezone().isoformat(timespec="seconds")
    source = config.get("source_note", f"OpenStreetMap PBF {pbf.name}; clipped by regional boundaries")
    federal = {"type": "FeatureCollection", "metadata": {"generated_at": timestamp, "source": source, "complete": all(route_features.values()), "route_feature_counts": {name: len(items) for name, items in route_features.items()}}, "features": [item for name in routes for item in route_features[name]]}
    major_payload = {"type": "FeatureCollection", "metadata": {"generated_at": timestamp, "source": source, "selection": "configured major non-federal highway classes", "feature_count": len(major)}, "features": major}
    output.mkdir(parents=True, exist_ok=True)
    (output / config.get("federal_output", "federal_roads.geojson")).write_text(json.dumps(federal, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    (output / config.get("major_output", "major_roads.geojson")).write_text(json.dumps(major_payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if routes and not federal["metadata"]["complete"]:
        raise ValueError(f"Federal routes missing: {federal['metadata']['route_feature_counts']}")


if __name__ == "__main__":
    main()
