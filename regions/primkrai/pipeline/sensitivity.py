"""Show redistribution under declared proxy assumptions; not confidence intervals."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    groups = defaultdict(list)
    for name in ["settlement_density", "settlement_density_unverified"]:
        for f in json.loads((args.data / f"{name}.geojson").read_text())["features"]:
            groups[f["properties"]["settlement_key"]].append(f["properties"])
    records = []
    for cells in groups.values():
        total = sum(c["residential_floor_proxy_m2"] for c in cells)
        original = [c["residential_floor_proxy_m2"] / total for c in cells]
        row = {"name": cells[0]["settlement_name"], "district": cells[0]["municipality"], "population_quality": cells[0]["population_quality"]}
        scenarios = {
            "remove_ambiguous_addressed_buildings": [c["residential_floor_proxy_m2"] * c["explicit_residential_share"] for c in cells],
            "assumed_storeys_half": [c["residential_floor_proxy_m2"] * (.5 + .5 * c["observed_levels_proxy_share"]) for c in cells],
            "assumed_storeys_double": [c["residential_floor_proxy_m2"] * (2 - c["observed_levels_proxy_share"]) for c in cells],
        }
        for name, weights in scenarios.items():
            denominator = sum(weights)
            row[f"reallocated_fraction_{name}"] = None if denominator == 0 else round(sum(abs(a - b / denominator) for a, b in zip(original, weights)) / 2, 6)
        records.append(row)
    with (args.data / "density-sensitivity.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    print(json.dumps({"settlements": len(records), "maximum_reallocated_fraction": {key: max(row[key] or 0 for row in records) for key in records[0] if key.startswith("reallocated")}}))


if __name__ == "__main__":
    main()
