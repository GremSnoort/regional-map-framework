#!/usr/bin/env python3
"""Build the shared indicative X5 expansion-priority layer from standard GeoJSON."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import transform, unary_union


def norm(value: str) -> str:
    return re.sub(r"[^а-яa-z0-9]+", "", (value or "").lower().replace("ё", "е"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    region = Path(os.environ["ANALYTICAL_REGION_ROOT"])
    output = Path(os.environ["ANALYTICAL_OUTPUT_DIR"])
    cfg = json.loads((region / args.config).read_text(encoding="utf-8"))
    source_dir = region / cfg["source_dir"] if cfg.get("source_dir") else Path(os.environ["ANALYTICAL_OUTPUT_DIR"])
    settlements = json.loads((source_dir / cfg.get("settlements", "settlements.geojson")).read_text(encoding="utf-8"))
    stores = json.loads((source_dir / cfg.get("stores", "stores.geojson")).read_text(encoding="utf-8"))
    roads = []
    for name in cfg.get("roads", ["major_roads.geojson", "federal_roads.geojson"]):
        payload = json.loads((source_dir / name).read_text(encoding="utf-8"))
        roads.extend(shape(item["geometry"]) for item in payload.get("features", []) if item.get("geometry"))
    road_union = unary_union(roads)
    lon_km = float(cfg.get("longitude_km_per_degree", 111.32 * math.cos(math.radians(float(cfg["central_latitude"])))))
    project = lambda geometry: transform(lambda x, y, z=None: (x * lon_km, y * 111.0), geometry)
    roads_km = project(road_union)
    minimum = int(cfg.get("minimum_population", 2500))
    base = int(cfg.get("base_people_per_store", 5000))
    low = int(cfg.get("scenario_people_per_store", {}).get("low", 10000))
    high = int(cfg.get("scenario_people_per_store", {}).get("high", 2500))
    fallback = float(cfg.get("store_fallback_radius_km", 1.0))
    below_base_score_cap = cfg.get("below_base_score_cap")
    direct = Counter(norm(item.get("properties", {}).get("locality", "")) for item in stores.get("features", []))
    conflicts = Counter(norm(item.get("locality", "")) for item in stores.get("metadata", {}).get("excluded_conflicts", []))
    store_points = [shape(item["geometry"]) for item in stores.get("features", []) if item.get("geometry")]
    result = []
    for item in settlements.get("features", []):
        p = item.get("properties", {}); population = int(p.get("population") or 0)
        if population < minimum or not item.get("geometry"):
            continue
        point = shape(item["geometry"]); key = norm(p.get("name", "")); existing = direct.get(key, 0); conflict = conflicts.get(key, 0)
        distances = [math.hypot((point.x-other.x)*lon_km, (point.y-other.y)*111.0) for other in store_points]
        nearest = min(distances, default=999.0)
        if not existing:
            existing = sum(distance <= fallback for distance in distances)
        cap = max(1, population // base); additional = max(0, cap-existing)
        cap_low = max(1, population // low); cap_high = max(1, population // high)
        road_km = project(point).distance(roads_km) if not roads_km.is_empty else 999.0
        quality = str(p.get("population_quality", "")); confidence = "высокая" if quality in {"official_current", "official_rounded_100"} else "средняя" if quality == "official_census_exact" else "низкая"
        effective = existing + .5*conflict
        score = round(min(100, min(30,30*population/20000) + 30*max(0,cap-effective)/cap + (15 if existing==0 and conflict==0 else 8 if existing==0 else 5) + (15 if road_km<=1 else 10 if road_km<=3 else 5 if road_km<=10 else 0) + (10 if confidence=="высокая" else 7 if confidence=="средняя" else 0)))
        if additional == 0: score = min(score,29)
        if confidence == "низкая": score = min(score,49)
        if population < base and below_base_score_cap is not None:
            score = min(score, int(below_base_score_cap))
        priority = "очень высокий" if score>=80 else "высокий" if score>=65 else "средний" if score>=50 else "требует проверки населения" if confidence=="низкая" else "низкий" if score>=30 else "ёмкость исчерпана моделью"
        props = {"settlement_key":f"osm_node/{p['osm_id']}","name":p.get("name"),"district":p.get("district"),"population":population,"population_as_of":p.get("population_as_of"),"population_quality":quality,"population_source":p.get("population_source"),"population_source_url":p.get("population_source_url"),"confidence":confidence,"existing_pyaterochka_cards":existing,"demographic_capacity_total":cap,"nearest_existing_store_km":round(nearest,1),"estimated_additional_capacity":additional,"people_per_existing_store":round(population/existing) if existing else None,"conflicting_store_cards":conflict,"estimated_additional_if_conflicts_active":max(0,cap-existing-conflict),"estimated_additional_low":max(0,cap_low-existing),"estimated_additional_high":max(0,cap_high-existing),"nearest_major_road_km":round(road_km,1),"priority_score":score,"priority":priority,"below_base_population":population<base,"capacity_interpretation":"минимальная кандидатная ёмкость 1 для пунктов от нижнего порога; выше базового значения — целочисленный демографический эквивалент","model_basis":f"база {base:,} жителей; нижний порог кандидата {minimum:,}".replace(","," "),"warning":"Не решение X5 и не прогноз открытия. Требуется анализ конкурентов, трафика, барьеров и помещения."}
        result.append({"type":"Feature","properties":props,"geometry":item["geometry"]})
    result.sort(key=lambda item:(-item["properties"]["priority_score"],-item["properties"]["population"],item["properties"]["name"] or ""))
    payload={"type":"FeatureCollection","metadata":{"generated_at":cfg.get("build_date"),"model_version":cfg.get("model_version","shared-expansion-1"),"model_status":"INDICATIVE_SCREENING_NOT_X5_DECISION","candidate_count":len(result),"minimum_population":minimum,"base_people_per_store":base,"source_url":cfg.get("method_source_url","")},"features":result}
    output.mkdir(parents=True,exist_ok=True)
    (output/cfg.get("output","expansion_priority.geojson")).write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    print(f"Built {len(result)} expansion candidates")


if __name__ == "__main__":
    main()
