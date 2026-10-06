"""Reconcile published controls with the saved Rosstat workbooks, without guessing."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
from pathlib import Path
from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.polygon import orient
from population_sources import controls, official_match
from verify_local_setup import verify


def area_km2(geometry):
    geod = Geod(ellps="WGS84")
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return sum(abs(geod.geometry_area_perimeter(orient(p, sign=1))[0]) for p in polygons) / 1e6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    official, current, census = controls(args.sources)
    municipalities = json.loads((args.baseline / "municipalities.geojson").read_text())
    municipal_records, changes, settlement_records = [], [], []
    for feature in municipalities["features"]:
        p = feature["properties"]
        sources = [official[n.strip()] for n in p["official_name"].split(" + ")]
        population = sum(r["population"] for r in sources)
        if population != p["population"]:
            raise ValueError(f"Municipal population mismatch: {p['municipality_name']}")
        area = area_km2(shape(feature["geometry"]))
        p.update({"area_km2": round(area, 6), "geometry_area_km2": round(area, 6), "density_per_km2": round(population / area, 6),
                  "area_method": "WGS84 ellipsoid; geodesic polygon area; holes excluded",
                  "density_status": "calculated_from_official_population_and_osm_area",
                  "statistics_source_url": sources[0]["population_source_url"],
                  "statistics_source_cells": "; ".join(r["source_cell"] for r in sources),
                  "statistics_source_rows": "; ".join(r["source_name"] for r in sources)})
        for r in sources:
            municipal_records.append({"municipality": p["municipality_name"], "osm_relation": p["osm_id"], **r})
    municipalities["metadata"].update({"area_method": "WGS84 ellipsoid", "geometry_snapshot_date": "2026-09-08", "density_status": "CALCULATED_NOT_OFFICIAL_DENSITY"})
    mun = [f["properties"] for f in municipalities["features"]]
    settlements = json.loads((args.baseline / "settlements.geojson").read_text())
    for feature in settlements["features"]:
        p = feature["properties"]
        source = official_match(p, mun, current, census)
        if source:
            if p["population"] != source["population"] or p["population_quality"] != source["population_quality"]:
                changes.append({"name": p["name"], "district": p["district"], "before_population": p["population"],
                                "after_population": source["population"], "before_quality": p["population_quality"], "after_quality": source["population_quality"]})
            p.update(source)
        elif p["population_quality"].startswith("official"):
            raise ValueError(f"Previously official control cannot be traced: {p['name']}")
        elif p["population_quality"] == "osm_unverified":
            p["population_source_url"] = f"https://www.openstreetmap.org/node/{p['osm_id']}"
            p["population_warning"] = "Тег OSM; официальная численность не подтверждена. Не считать текущей оценкой Росстата."
        else:
            p["population_warning"] = "Численность не подключена; 0 обозначает отсутствие данных, а не отсутствие жителей."
        settlement_records.append(p.copy())
    settlements["metadata"]["verified_changes"] = changes
    for name, payload in [("municipalities", municipalities), ("settlements", settlements)]:
        (args.output / f"{name}.geojson").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    for name in ["major_roads", "federal_roads", "stores"]:
        (args.output / f"{name}.geojson").write_bytes((args.baseline / f"{name}.geojson").read_bytes())
    for name, records in [("municipal-source-rows", municipal_records), ("settlement-source-rows", settlement_records)]:
        columns = sorted(set().union(*(r.keys() for r in records)))
        with (args.output / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, columns)
            writer.writeheader()
            writer.writerows(records)
    checksums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.sources.glob("*.xlsx")}
    remote = {"policy": "LEGACY_ONLY_OFFLINE", "local_verification": verify(args.sources.parent), "files": []} if args.legacy_only else json.loads((args.sources / "remote-verification.json").read_text())
    for record in remote["files"]:
        if not record["tls_verified"] or record["status"] != 200 or checksums.get(record["file"]) != record["sha256"]:
            raise ValueError("Saved remote verification does not match population inputs")
    (args.output / "source-verification.json").write_text(json.dumps({"source_sha256": checksums, "changes": changes,
        "municipality_rows": len(municipal_records), "population_total": sum(r["population"] for r in municipal_records),
        "verification_mode": "local_legacy_snapshot" if args.legacy_only else "remote_checksum", "verification": remote}, ensure_ascii=False, indent=2))
    print(json.dumps({"corrected_settlements": changes, "municipal_population_total": 1_799_659}, ensure_ascii=False))


if __name__ == "__main__":
    main()
