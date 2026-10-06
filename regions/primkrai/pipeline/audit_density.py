"""Independent numeric and spatial acceptance checks for a Primorye publication."""
from __future__ import annotations
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from pyproj import Geod, Transformer
from shapely.geometry import shape
from shapely.geometry.polygon import orient
from shapely.ops import transform
from shapely.strtree import STRtree


def area(geometry):
    polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    geod = Geod(ellps="WGS84")
    return sum(abs(geod.geometry_area_perimeter(orient(p))[0]) for p in polygons) / 1e6


def overlaps(features):
    geometries = [shape(f["geometry"]) for f in features]
    project = Transformer.from_crs("EPSG:4326", "+proj=laea +lat_0=45 +lon_0=134 +datum=WGS84 +units=m", always_xy=True).transform
    geometries = [transform(project, g) for g in geometries]
    tree = STRtree(geometries)
    pairs, pair_area = defaultdict(int), defaultdict(float)
    for index, g in enumerate(geometries):
        for other in tree.query(g, predicate="intersects"):
            if other <= index:
                continue
            intersection = g.intersection(geometries[other]).area
            if intersection <= .01:
                continue
            names = tuple(sorted(f["properties"].get("settlement_name", f["properties"].get("municipality_name", "")) for f in [features[index], features[other]]))
            pairs[names] += 1
            pair_area[names] += intersection / 1e6
    return [{"settlements": list(k), "pairs": pairs[k], "area_km2": pair_area[k]} for k in sorted(pairs)]


def audit(root, strict=False):
    municipalities = json.loads((root / "municipalities.geojson").read_text())
    settlement_features = json.loads((root / "settlements.geojson").read_text())["features"]
    settlements_by_key = {(f["properties"]["name"], f["properties"]["district"]): f["properties"] for f in settlement_features}
    issues, municipal_rows = [], []
    for f in municipalities["features"]:
        p, geometry = f["properties"], shape(f["geometry"])
        actual = area(geometry)
        if not geometry.is_valid:
            issues.append(f"Invalid municipality {p['municipality_name']}")
        relative_error = abs(p["area_km2"] / actual - 1)
        if strict and relative_error > 1e-6:
            issues.append(f"Municipal area differs from WGS84: {p['municipality_name']}")
        if strict and abs(p["density_per_km2"] - p["population"] / actual) > 1e-5:
            issues.append(f"Municipal density formula differs: {p['municipality_name']}")
        municipal_rows.append({"name": p["municipality_name"], "area_km2": p["area_km2"], "wgs84_area_km2": actual, "area_relative_error": relative_error})
    municipal_overlap = overlaps(municipalities["features"])
    if municipal_overlap:
        issues.append("Municipal polygons overlap")
    population_total = sum(f["properties"]["population"] for f in municipalities["features"])
    if population_total != 1_799_659:
        issues.append("Municipal total differs from saved 2025 control")
    density = json.loads((root / "settlement_density.geojson").read_text())
    features = density["features"].copy()
    unverified = root / "settlement_density_unverified.geojson"
    if unverified.exists():
        features.extend(json.loads(unverified.read_text())["features"])
    domains_path = root / "allocation-domains.geojson"
    project = Transformer.from_crs("EPSG:4326", "+proj=laea +lat_0=45 +lon_0=134 +datum=WGS84 +units=m", always_xy=True).transform
    domains = {} if not domains_path.exists() else {f["properties"]["settlement_key"]: transform(project, shape(f["geometry"])) for f in json.loads(domains_path.read_text())["features"]}
    groups = defaultdict(list)
    identifiers = set()
    max_area_error, max_formula_error = 0., 0.
    for feature in features:
        p, geometry = feature["properties"], shape(feature["geometry"])
        if p["cell_id"] in identifiers:
            issues.append("Duplicate cell_id")
        identifiers.add(p["cell_id"])
        if not geometry.is_valid or geometry.is_empty:
            issues.append(f"Invalid grid geometry {p['cell_id']}")
            continue
        actual_area = area(geometry)
        actual_density = p["population_estimate"] / actual_area
        max_area_error = max(max_area_error, abs(p["cell_area_km2"] / actual_area - 1))
        max_formula_error = max(max_formula_error, abs(p["density_per_km2"] - actual_density))
        if strict and abs(p["cell_area_km2"] / actual_area - 1) > 1e-6:
            issues.append(f"Cell area differs from geometry {p['cell_id']}")
        if strict and abs(p["density_per_km2"] - actual_density) > 1e-5:
            issues.append(f"Cell density differs from geometry {p['cell_id']}")
        if p["population_estimate"] <= 0 or not math.isfinite(p["density_per_km2"]):
            issues.append(f"Invalid cell population/density {p['cell_id']}")
        # Compare in the construction CRS. In lon/lat, a long inverse-projected
        # domain edge and its subdivided cell edges have different straight
        # chord approximations, which is not an allocation-domain violation.
        if domains and transform(project, geometry).difference(domains[p["settlement_key"]]).area > .01:
            issues.append(f"Cell outside settlement allocation domain {p['cell_id']}")
        groups[p["settlement_key"]].append(p)
    settlement_rows = []
    for key, cells in groups.items():
        p = cells[0]
        district = p.get("district", p.get("municipality"))
        official = settlements_by_key[p["settlement_name"], district]
        total = sum(c["population_estimate"] for c in cells)
        if abs(total - p["population_control"]) > 1e-6:
            issues.append(f"Population not conserved: {key}")
        if official["population"] != p["population_control"]:
            issues.append(f"Grid and point population controls differ: {key}")
        if any(c["population_control"] != p["population_control"] or c["population_quality"] != official["population_quality"] or c["population_as_of"] != official["population_as_of"] for c in cells):
            issues.append(f"Inconsistent control metadata: {key}")
        if strict and official["population_quality"].startswith("official") and not all(c.get("population_source_cell") for c in cells):
            issues.append(f"Missing exact source locator: {key}")
        settlement_rows.append({"name": p["settlement_name"], "district": district, "population_control": p["population_control"],
                                "allocated": round(total, 6), "cells": len(cells), "population_quality": p["population_quality"],
                                "grid_metres": sorted({c["grid_metres"] for c in cells})})
    overlap = overlaps(features)
    if overlap:
        issues.append("Density cells overlap")
    if strict and any(not f["properties"]["population_quality"].startswith("official") for f in density["features"]):
        issues.append("Unverified control in primary density layer")
    return {"passed": not issues, "strict": strict, "issues": issues, "municipalities": len(municipal_rows), "municipal_population_total": population_total,
            "municipal_overlap": municipal_overlap, "municipal_area_checks": municipal_rows, "density_cells": len(features),
            "density_settlements": len(groups), "maximum_cell_area_relative_error": max_area_error,
            "maximum_density_formula_error": max_formula_error, "density_overlap": overlap, "settlement_controls": settlement_rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    result = audit(args.data, args.strict)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in {"municipal_area_checks", "settlement_controls"}}, ensure_ascii=False))
    if args.strict and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
