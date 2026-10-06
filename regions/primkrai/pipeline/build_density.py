"""Primorye model: disjoint domains, shared metric lattice, conserved controls.

OSM boundaries and nearest-place domains are cartographic/model inputs, never
official statistical census-block boundaries. See ../METHOD_DENSITY.md.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from pyproj import Transformer
from shapely import voronoi_polygons
from shapely.geometry import MultiPoint, MultiPolygon, Point, box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree
from prepare_inputs import area_km2
from housing_register import compile_register, housing_parameters, write_review

CRS = "+proj=laea +lat_0=45 +lon_0=134 +datum=WGS84 +units=m +no_defs"
RESIDENTIAL = {"apartments", "residential", "house", "detached", "terrace", "dormitory", "semidetached_house", "bungalow", "static_caravan", "ger"}
NON_RESIDENTIAL = {"commercial", "retail", "industrial", "warehouse", "school", "kindergarten", "hospital", "office", "church", "garage", "garages", "service", "train_station", "public", "civic", "sports_centre", "construction"}
NON_RESIDENTIAL_AMENITIES = {"school", "kindergarten", "college", "university", "hospital", "cinema", "place_of_worship", "bus_station", "fuel", "waste_disposal", "public_utilities", "public_utilites"}


def polygons(geometry):
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    parts = [g for g in geometry.geoms if g.geom_type in {"Polygon", "MultiPolygon"}]
    return unary_union(parts) if parts else MultiPolygon([])


def proxy_parameters(tags):
    kind = tags.get("building", "")
    if kind in NON_RESIDENTIAL:
        return None
    explicit = kind in RESIDENTIAL
    # An address is not residential evidence. Preserve explicitly residential
    # mixed-use buildings, but do not allocate residents to untyped facilities.
    if not explicit and tags.get("amenity") in NON_RESIDENTIAL_AMENITIES:
        return None
    weight = 1.0 if explicit else .55 if kind == "yes" and (tags.get("addr:housenumber") or tags.get("addr:street")) else 0
    if not weight:
        return None
    try:
        levels = float(tags.get("building:levels", "").replace(",", "."))
        observed = math.isfinite(levels) and .5 <= levels <= 80
    except ValueError:
        observed = False
    if not observed:
        levels = 5.0 if kind in {"apartments", "dormitory"} else 2.0 if kind == "residential" else 1.0
    return levels * weight, explicit, observed


def apply_building_review(features, review):
    """Apply evidence-backed exclusions; fail if IDs/tags no longer match."""
    exclusions = {row["osm_id"]: row for row in review["exclusions"]}
    if len(exclusions) != len(review["exclusions"]):
        raise ValueError("Duplicate manual building review IDs")
    found, selected = set(), []
    for feature in features:
        p = feature["properties"]
        identifier = f"{p['osm_type']}/{p['osm_id']}"
        if identifier in exclusions:
            row = exclusions[identifier]
            if not row.get("source_url") or not row.get("reason"):
                raise ValueError("Building exclusion requires source and reason")
            if any(p["tags"].get(k) != v for k, v in row["expected_tags"].items()):
                raise ValueError(f"Stale building review: {identifier}")
            found.add(identifier)
        else:
            selected.append(feature)
    if found != exclusions.keys():
        raise ValueError("Reviewed building IDs missing from snapshot")
    return selected, sorted(found)


def deduplicate_footprints(features, overrides=None):
    """Identical footprint geometry must not supply residential proxy twice.

    Conflicting floor proxies are excluded, rather than resolving OSM conflicts
    by choosing an arbitrary type or higher assumed number of storeys.
    """
    overrides = overrides or {}
    groups, excluded, conflicts = defaultdict(list), [], []
    for feature in features:
        identifier = f"{feature['properties']['osm_type']}/{feature['properties']['osm_id']}"
        if proxy_parameters(feature["properties"]["tags"]) is None and identifier not in overrides:
            excluded.append(feature)
            continue
        signature = hashlib.sha256(shape(feature["geometry"]).normalize().wkb).digest()
        groups[signature].append(feature)
    selected, duplicates = [], 0
    for members in groups.values():
        parameters = {(proxy_parameters(f["properties"]["tags"]), tuple(sorted(overrides.get(f"{f['properties']['osm_type']}/{f['properties']['osm_id']}", {}).items()))) for f in members}
        if len(parameters) > 1:
            conflicts.append([f"{f['properties']['osm_type']}/{f['properties']['osm_id']}" for f in members])
            continue
        selected.append(min(members, key=lambda f: (f["properties"]["osm_type"], f["properties"]["osm_id"])))
        duplicates += len(members) - 1
    return excluded + selected, {"exact_duplicate_objects_removed": duplicates,
                                  "conflicting_duplicate_footprints_excluded": conflicts}


def coarsen(rows, maximum):
    """Quadtree over occupied 100 m cells, with unique building IDs per block."""
    result = {}

    def aggregate(keys):
        value = {"proxy": 0., "explicit": 0., "observed_levels": 0., "verified_area": 0., "verified_storeys": 0., "ids": set()}
        for key in keys:
            for name in ("proxy", "explicit", "observed_levels", "verified_area", "verified_storeys"):
                value[name] += rows[key].get(name, 0.)
            value["ids"].update(rows[key]["ids"])
        return value

    def visit(ix, iy, span, keys):
        keys = [k for k in keys if ix <= k[0] < ix + span and iy <= k[1] < iy + span]
        if not keys:
            return
        value = aggregate(keys)
        if span > 1 and (len(value["ids"]) >= 12 or value["proxy"] / ((span * 100 / 1000) ** 2) >= 80_000):
            half = span // 2
            for dx, dy in ((0, 0), (half, 0), (0, half), (half, half)):
                visit(ix + dx, iy + dy, half, keys)
        else:
            result[ix, iy, span] = value

    span = maximum // 100
    roots = defaultdict(list)
    for key in rows:
        roots[math.floor(key[0] / span) * span, math.floor(key[1] / span) * span].append(key)
    for (ix, iy), keys in sorted(roots.items()):
        visit(ix, iy, span, keys)
    return result


def allocate(population, weights):
    """Largest remainder at 0.001 person, deterministic, exact in integer units."""
    total = sum(weights.values())
    raw = {k: population * 1000 * v / total for k, v in weights.items()}
    units = {k: math.floor(v) for k, v in raw.items()}
    missing = population * 1000 - sum(units.values())
    for key in sorted(raw, key=lambda k: (-(raw[k] - units[k]), k))[:missing]:
        units[key] += 1
    assert sum(units.values()) == population * 1000
    return units


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--osm-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--building-review", type=Path)
    parser.add_argument("--housing-register", type=Path)
    parser.add_argument("--source-policy", choices=["reviewed_evidence", "legacy_only"], default="reviewed_evidence")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    forward = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform
    inverse = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform
    settlements = json.loads((args.input / "settlements.geojson").read_text())["features"]
    municipal_features = json.loads((args.input / "municipalities.geojson").read_text())["features"]
    municipalities = {f["properties"]["municipality_name"]: transform(forward, shape(f["geometry"])) for f in municipal_features}
    points = [transform(forward, shape(f["geometry"])) for f in settlements]
    if len({(p.x, p.y) for p in points}) != len(points):
        raise ValueError("Duplicate settlement coordinates require manual reconciliation")
    voronoi = list(voronoi_polygons(MultiPoint(points), ordered=True).geoms)
    raw_boundaries = json.loads((args.osm_cache / "osm-boundaries.json").read_text())
    boundaries = {name: transform(forward, shape(f["geometry"])) for name, f in raw_boundaries.items()}
    protected = unary_union(list(boundaries.values()))
    models, domains = [], []
    for index, feature in enumerate(settlements):
        p = feature["properties"]
        if p["population"] < 2500:
            continue
        municipality = municipalities[p["district"]]
        if p["name"] in boundaries:
            domain = boundaries[p["name"]].intersection(municipality)
            domain_method = "osm_place_boundary_clipped_to_municipality"
        else:
            radius = 7000 if p["population"] >= 10000 else 5000 if p["population"] >= 5000 else 3500
            domain = voronoi[index].intersection(municipality).intersection(points[index].buffer(radius, quad_segs=64)).difference(protected)
            domain_method = "nearest_osm_place_voronoi_municipality_radius_excluding_known_cities"
        domain = polygons(domain)
        if domain.is_empty or not domain.is_valid:
            raise ValueError(f"Empty or invalid allocation domain for {p['name']}")
        models.append({"properties": p, "method": domain_method, "maximum": 400 if p["population"] >= 25000 else 200,
                       "rows": defaultdict(lambda: {"proxy": 0., "explicit": 0., "observed_levels": 0., "verified_area": 0., "verified_storeys": 0., "ids": set()}), "building_ids": set()})
        domains.append(domain)
    tree = STRtree(domains)
    for index, domain in enumerate(domains):
        for other in tree.query(domain, predicate="intersects"):
            if other > index and domain.intersection(domains[other]).area > .01:
                raise ValueError("Allocation domains overlap")
    diagnostics = defaultdict(int)
    buildings = json.loads((args.osm_cache / "osm-buildings.geojson").read_text())["features"]
    overrides = {}
    if args.housing_register:
        housing_register = json.loads(args.housing_register.read_text())
        overrides, housing_rows = compile_register(housing_register, buildings, args.housing_register.parent.parent)
        write_review(args.output, housing_rows, overrides, housing_register, buildings)
        diagnostics["housing_register_contours"] = len(housing_rows)
        diagnostics["housing_contours_with_applied_fields"] = len(overrides)
    if args.building_review:
        buildings, reviewed_ids = apply_building_review(buildings, json.loads(args.building_review.read_text()))
        diagnostics["excluded_by_documented_manual_review"] = len(reviewed_ids)
    buildings, duplicate_review = deduplicate_footprints(buildings, overrides)
    for feature in buildings:
        tags = feature["properties"]["tags"]
        identifier = f"{feature['properties']['osm_type']}/{feature['properties']['osm_id']}"
        params = housing_parameters(tags, overrides.get(identifier, {}), 1., proxy_parameters)
        if params is None:
            diagnostics["excluded_non_residential_or_unclassified"] += 1
            continue
        footprint = transform(forward, shape(feature["geometry"]))
        if not footprint.is_valid:
            diagnostics["excluded_invalid_footprint"] += 1
            continue
        if not 12 <= footprint.area <= 80_000:
            diagnostics["excluded_footprint_area_outside_12_80000_m2"] += 1
            continue
        osm_id = f"{feature['properties']['osm_type']}/{feature['properties']['osm_id']}"
        params = housing_parameters(tags, overrides.get(osm_id, {}), footprint.area, proxy_parameters)
        multiplier, explicit, observed_levels, verified_area, verified_storeys = params
        matched = False
        for index in tree.query(footprint, predicate="intersects"):
            clipped = polygons(footprint.intersection(domains[index]))
            if clipped.is_empty or clipped.area < .01:
                continue
            matched = True
            model = models[index]
            model["building_ids"].add(osm_id)
            minx, miny, maxx, maxy = clipped.bounds
            for ix in range(math.floor(minx / 100), math.floor(maxx / 100) + 1):
                for iy in range(math.floor(miny / 100), math.floor(maxy / 100) + 1):
                    part = clipped.intersection(box(ix * 100, iy * 100, (ix + 1) * 100, (iy + 1) * 100))
                    proxy = part.area * multiplier
                    if proxy <= 1e-8:
                        continue
                    row = model["rows"][ix, iy]
                    row["proxy"] += proxy
                    row["explicit"] += proxy if explicit else 0
                    row["observed_levels"] += proxy if observed_levels else 0
                    row["verified_area"] += proxy if verified_area else 0
                    row["verified_storeys"] += proxy if verified_storeys else 0
                    row["ids"].add(osm_id)
        diagnostics["assigned_buildings" if matched else "residential_outside_selected_domains"] += 1
    del buildings
    verified, provisional, summary, domain_features = [], [], [], []
    for index, model in enumerate(models):
        p, domain = model["properties"], domains[index]
        if not model["rows"]:
            raise ValueError(f"No eligible residential footprints for {p['name']}")
        rows = coarsen(model["rows"], model["maximum"])
        allocation = allocate(p["population"], {k: r["proxy"] for k, r in rows.items()})
        verified_control = p["population_quality"].startswith("official")
        target = verified if verified_control else provisional
        proxy_total = sum(r["proxy"] for r in rows.values())
        settlement_explicit_share = sum(r["explicit"] for r in rows.values()) / proxy_total
        review_status = "требует ручной проверки типологии зданий" if settlement_explicit_share < .7 else "модельная оценка; требуется проверка на местности"
        population_sum, cells = 0, 0
        areas, sizes, max_density = [], set(), 0
        for (ix, iy, span), row in sorted(rows.items()):
            units = allocation[ix, iy, span]
            if not units:
                continue
            geometry = polygons(box(ix * 100, iy * 100, (ix + span) * 100, (iy + span) * 100).intersection(domain))
            geographic = transform(inverse, geometry)
            area = area_km2(geographic)
            if area <= 0:
                raise ValueError("Non-positive cell area")
            people = units / 1000
            density = people / area
            share = row["explicit"] / row["proxy"]
            observed = row["observed_levels"] / row["proxy"]
            confidence = "средняя" if verified_control and share >= .7 and observed >= .5 and len(row["ids"]) >= 3 else "низкая"
            props = {"cell_id": f"primkrai_node{p['osm_id']}_{100 * span}_{ix}_{iy}", "settlement_key": f"osm_node/{p['osm_id']}",
                     "settlement_name": p["name"], "municipality": p["district"], "district": p["district"],
                     "population_estimate": people, "density_per_km2": round(density, 6), "cell_area_km2": round(area, 12),
                     "cell_area_m2": round(area * 1e6, 3),
                     "grid_metres": 100 * span, "cell_clipped": geometry.area < (100 * span) ** 2 - .01,
                     "building_count": len(row["ids"]), "residential_floor_proxy_m2": round(row["proxy"], 6),
                     "explicit_residential_share": round(share, 6), "observed_levels_proxy_share": round(observed, 6),
                     "explicit_residential_percent": round(share * 100, 2), "observed_storeys_percent": round(observed * 100, 2),
                     "verified_residential_area_percent": round(row["verified_area"] / row["proxy"] * 100, 2),
                     "verified_storeys_percent": round(row["verified_storeys"] / row["proxy"] * 100, 2),
                     "population_control": p["population"], "population_as_of": p.get("population_as_of"),
                     "population_quality": p["population_quality"], "population_source": p["population_source"],
                     "population_source_url": p.get("population_source_url"), "population_source_cell": p.get("source_cell", ""),
                     "population_source_sheet": p.get("source_sheet", ""), "confidence": confidence,
                     "model_review_status": review_status,
                     "domain_method": model["method"], "model_version": "primkrai-density-2.1.1",
                     "model_warning": "Расчётная модель, не официальная квартальная статистика; граница распределения OSM/модельная; неполнота зданий и предположения об этажности влияют на результат." if verified_control else p["population_warning"]}
            target.append({"type": "Feature", "properties": props, "geometry": mapping(geographic)})
            population_sum += units
            cells += 1
            areas.append(area)
            sizes.add(100 * span)
            max_density = max(max_density, density)
        if population_sum != p["population"] * 1000:
            raise ValueError("Population control mismatch")
        summary.append({"osm_node_id": p["osm_id"], "name": p["name"], "district": p["district"], "population": p["population"],
                        "population_as_of": p.get("population_as_of"), "population_quality": p["population_quality"],
                        "source_cell": p.get("source_cell", ""), "domain_method": model["method"], "cells": cells,
                        "grid_metres": ";".join(map(str, sorted(sizes))), "buildings": len(model["building_ids"]),
                        "explicit_residential_proxy_share": round(settlement_explicit_share, 6), "model_review_status": review_status,
                        "observed_levels_proxy_share": round(sum(r["observed_levels"] for r in rows.values()) / proxy_total, 6),
                        "modelled_cell_area_km2": sum(areas), "max_cell_density": round(max_density, 2)})
        domain_features.append({"type": "Feature", "properties": {"settlement_key": f"osm_node/{p['osm_id']}", "name": p["name"], "district": p["district"], "domain_method": model["method"]}, "geometry": mapping(transform(inverse, domain))})
    metadata = {"model_version": "primkrai-density-2.1.1", "model_status": "MODELLED_NOT_OFFICIAL_BLOCK_STATISTICS", "source_policy": args.source_policy, "crs": CRS,
                "osm_snapshot_timestamp": "2026-09-08T20:21:01Z", "minimum_population": 2500,
                "scope": "all mapped settlements with connected population control >=2500; independent of commercial capacity",
                "refinement": {"minimum_grid_metres": 100, "maximum_grid_metres": [200, 400], "split_buildings": 12, "split_proxy_m2_per_km2": 80000},
                "allocation": "verified whole-contour residential-premises area where available, otherwise building-footprint intersections times storeys times residential weight; largest remainder in 0.001-person units",
                "area_method": "WGS84 ellipsoid; actual clipped polygon area", "diagnostics": dict(diagnostics),
                "duplicate_footprint_review": duplicate_review,
                "warning": "Different control dates (2025 estimates and 2021 census); not an estimate of regional population at one date. Voronoi domains are modelled, not official settlement boundaries."}
    for name, features, official in [("settlement_density", verified, True), ("settlement_density_unverified", provisional, False)]:
        value = {"type": "FeatureCollection", "metadata": metadata | {"official_population_controls_only": official, "settlements": [s for s in summary if s["population_quality"].startswith("official") == official]}, "features": features}
        (args.output / f"{name}.geojson").write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    (args.output / "allocation-domains.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": domain_features}, ensure_ascii=False, separators=(",", ":")))
    with (args.output / "density-audit-by-settlement.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    print(json.dumps({"verified_cells": len(verified), "provisional_cells": len(provisional), "settlements": len(summary), "diagnostics": dict(diagnostics)}))


if __name__ == "__main__":
    main()
