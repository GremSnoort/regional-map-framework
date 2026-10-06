"""Offline, field-level housing evidence. Unconfirmed observations never override OSM."""
from __future__ import annotations
import csv
import hashlib
import json
import math
from pathlib import Path
from shapely.geometry import shape

FIELDS = {"use", "above_ground_storeys", "residential_premises_area_m2", "apartments"}
STATUSES = {"unknown", "reported", "conflicting", "confirmed"}
USES = {"residential", "mixed", "non_residential"}


def signature(feature):
    return hashlib.sha256(shape(feature["geometry"]).normalize().wkb).hexdigest()


def compile_register(register, features, root):
    """Validate snapshots, exact contour identity and evidence before compilation.

    Confirmed means reviewer approved a primary-source fact and its geometry link.
    Apartment counts are kept for review; never converted to floor area.
    """
    if register.get("schema_version") != 1:
        raise ValueError("Unsupported housing register version")
    sources = register["sources"]
    for identifier, source in sources.items():
        path = (root / source["snapshot"]).resolve()
        if root.resolve() not in path.parents or not source.get("url", "").startswith("https://"):
            raise ValueError(f"Invalid housing source: {identifier}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Changed housing evidence: {identifier}")
        if source["kind"] not in {"primary", "secondary", "primary_extract"}:
            raise ValueError("Invalid source kind")
    by_id = {f"{f['properties']['osm_type']}/{f['properties']['osm_id']}": f for f in features}
    overrides, rows, seen = {}, [], set()
    for record in register["buildings"]:
        identifier = record["osm_id"]
        if identifier in seen or identifier not in by_id:
            raise ValueError(f"Duplicate or missing housing contour: {identifier}")
        seen.add(identifier)
        feature = by_id[identifier]
        if signature(feature) != record["geometry_sha256"] or feature["properties"]["tags"] != record["expected_tags"]:
            raise ValueError(f"Stale housing contour: {identifier}")
        match = record["match"]
        if match["status"] not in {"candidate", "confirmed", "rejected"}:
            raise ValueError("Invalid housing match status")
        if match["status"] == "confirmed" and not (match.get("reviewer") and match.get("reviewed_at") and match.get("reason") and match.get("source_ids")):
            raise ValueError("Confirmed contour match needs reviewer, date, reason and evidence")
        if any(s not in sources for s in match.get("source_ids", [])):
            raise ValueError("Unknown matching source")
        if match["status"] == "confirmed" and not any(sources[s]["kind"] == "primary" for s in match["source_ids"]):
            raise ValueError("Confirmed contour link needs saved primary evidence")
        fields = record["fields"]
        if set(fields) != FIELDS:
            raise ValueError("Housing register requires every field, including unknowns")
        approved = {}
        for name, observation in fields.items():
            status, value = observation["status"], observation["value"]
            if status not in STATUSES or (status == "unknown" and value is not None):
                raise ValueError(f"Invalid housing observation: {identifier}/{name}")
            refs = observation.get("source_ids", [])
            if any(s not in sources for s in refs):
                raise ValueError("Unknown field source")
            if status != "unknown" and not (refs and observation.get("locator")):
                raise ValueError("Housing observation requires source and locator")
            if status != "confirmed":
                continue
            if not refs or any(sources[s]["kind"] != "primary" for s in refs):
                raise ValueError("Only saved primary sources may confirm housing fields")
            if not (observation.get("reviewer") and observation.get("reviewed_at") and observation.get("as_of")):
                raise ValueError("Confirmed housing field needs reviewer and dates")
            if name == "use":
                if value not in USES:
                    raise ValueError("Invalid confirmed building use")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("Confirmed housing measurement must be finite and positive")
                if name in {"apartments", "above_ground_storeys"} and (value != int(value) or value > (80 if name == "above_ground_storeys" else 10000)):
                    raise ValueError("Invalid housing count or above-ground storeys")
            if match["status"] == "confirmed":
                approved[name] = value
        # A floor-area figure must refer to residential premises of this entire
        # contour, not gross building area, room-only living area or a section.
        if "residential_premises_area_m2" in approved:
            area_field = fields["residential_premises_area_m2"]
            if approved.get("use") not in {"residential", "mixed"} or area_field.get("scope") != "whole_contour_residential_premises":
                raise ValueError("Residential area needs confirmed use and whole-contour scope")
        if approved.get("use") == "mixed" and "above_ground_storeys" in approved and "residential_premises_area_m2" not in approved:
            raise ValueError("Mixed-use storeys do not establish residential floor area")
        if "above_ground_storeys" in approved and approved.get("use") not in {"residential", "mixed"}:
            raise ValueError("Verified storeys need verified residential use")
        if "above_ground_storeys" in approved and fields["above_ground_storeys"].get("scope") != "uniform_above_ground_whole_contour":
            raise ValueError("Maximum storeys cannot be applied to a variable-height contour")
        approved.pop("apartments", None)
        if approved.get("use") == "mixed" and "residential_premises_area_m2" not in approved:
            approved.pop("use")
        if approved:
            overrides[identifier] = approved
        rows.append({"osm_id": identifier, "settlement": record["settlement"], "address": record["address"],
                     "match_status": match["status"], "applied_fields": ";".join(sorted(approved)),
                     **{f"{name}_{key}": fields[name][key] for name in sorted(FIELDS) for key in ("status", "value")},
                     "review_notes": record.get("notes", ""),
                     "source_urls": ";".join(sorted({sources[s]["url"] for o in fields.values() for s in o.get("source_ids", [])}))})
    return overrides, rows


def housing_parameters(tags, approved, footprint_area, fallback):
    """Return multiplier, explicit-use, OSM-storeys, area/floors verified flags."""
    if approved.get("use") == "non_residential":
        return None
    if "residential_premises_area_m2" in approved:
        return approved["residential_premises_area_m2"] / footprint_area, True, False, True, False
    if approved.get("use") == "residential":
        # Confirmed use does not imply five storeys. Retain the old floor
        # assumption; remove only its residential probability discount.
        base = fallback({k: v for k, v in tags.items() if k != "amenity"} | {"building": "house"})
        original = fallback(tags)
        if original:
            base = (original[0] / (1 if original[1] else .55), True, original[2])
    else:
        base = fallback(tags)
    if base is None:
        return None
    multiplier, explicit, osm_levels = base
    if "above_ground_storeys" in approved and approved.get("use") == "residential":
        return float(approved["above_ground_storeys"]), True, False, False, True
    return multiplier, explicit, osm_levels, False, False


def write_review(output, rows, overrides, register, features):
    with (output / "housing-review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(rows[0]) if rows else ["osm_id"])
        writer.writeheader()
        writer.writerows(rows)
    (output / "housing-review-summary.json").write_text(json.dumps({
        "registered_contours": len(rows), "confirmed_matches": sum(r["match_status"] == "confirmed" for r in rows),
        "contours_with_applied_fields": len(overrides),
        "source_addresses": len(register.get("address_inventory", [])),
        "addresses_without_candidates": sum(not r["candidate_ids"] for r in register.get("address_inventory", [])),
        "warning": "Candidate address matches and reported/conflicting fields are excluded from model overrides. Apartment counts are not occupancy."}, ensure_ascii=False, indent=2) + "\n")
    by_id = {f"{f['properties']['osm_type']}/{f['properties']['osm_id']}": f for f in features}
    records = {r["osm_id"]: r for r in register["buildings"]}
    polygons = []
    for row in rows:
        record = records[row["osm_id"]]
        properties = dict(row)
        labels = {"candidate": "кандидат; контур не проверен", "confirmed": "подтверждено", "rejected": "отклонено",
                  "unknown": "нет данных", "reported": "требует подтверждения", "conflicting": "противоречие"}
        for key in ["match_status", *[f"{name}_status" for name in FIELDS]]:
            properties[key + "_label"] = labels[row[key]]
        properties["osm_url"] = f"https://www.openstreetmap.org/{row['osm_id']}"
        properties["source_url"] = register["sources"][record["fields"]["use"]["source_ids"][0]]["url"] if record["fields"]["use"].get("source_ids") else ""
        properties["gis_url"] = f"https://dom.gosuslugi.ru/#!/passport/show?houseGuid={record['gis_house_guid']}" if record.get("gis_house_guid") else ""
        polygons.append({"type": "Feature", "geometry": by_id[row["osm_id"]]["geometry"], "properties": properties})
    (output / "housing_review.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": polygons}, ensure_ascii=False, separators=(",", ":")))
    inventory = register.get("address_inventory", [])
    with (output / "housing-address-review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ["settlement", "street", "number", "candidate_ids"])
        writer.writeheader()
        writer.writerows(r | {"candidate_ids": ";".join(r["candidate_ids"])} for r in inventory)
