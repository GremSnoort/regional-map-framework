#!/usr/bin/env python3
"""Create, build, validate and publish standalone regional map packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import auth

ROOT = Path(__file__).resolve().parent
CONTENT_ROOT = Path(os.environ.get("RMF_CONTENT_ROOT", ROOT)).expanduser().resolve()
RUNTIME_ROOT = Path(os.environ["RMF_RUNTIME_ROOT"]).expanduser().resolve() if os.environ.get("RMF_RUNTIME_ROOT") else None
REGIONS = CONTENT_ROOT / "regions"
REGISTRY = CONTENT_ROOT / "registry.json"
CORE_LOCK = ROOT / "core.lock.json"
CORE_VERSION = "2.1.0"
GEOMETRY_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
CORE_FILES = (
    "index.html", "map.html", "contacts.html", "manage.py", "serve.py", "auth.py", "REGION_CONTRACT.md", "CONTACTS.md", "DEPLOYMENT.md",
    "core/gallery.js", "core/gallery.css", "core/map.js", "core/map.css", "core/table-export.js", "core/contacts.js", "core/contacts.css",
    ".github/workflows/validate.yml", ".github/workflows/deploy.yml",
    "deploy/regional-map-framework.env.example", "deploy/systemd/regional-map-framework.service", "deploy/nginx/regional-map-framework.conf",
    "deploy/server/rmf-deploy", "deploy/server/rmf-deploy-gateway",
    "pipeline_core/runner.py", "pipeline_core/regeneration.py", "pipeline_core/contacts.py", "pipeline_core/build_roads.py", "pipeline_core/build_adaptive_density.py", "pipeline_core/normalize_layer.py",
    "pipeline_core/requirements-lock.txt", "pipeline_core/requirements-minimal-lock.txt",
    "pipeline_core/selftest.py", "schemas/deployment-bundle.schema.json", "schemas/registry.schema.json", "schemas/region.schema.json", "schemas/pipeline.schema.json",
    "schemas/analytics-plugin.schema.json", "schemas/regeneration.schema.json", "schemas/contacts-sources.schema.json", "schemas/contact-catalog.schema.json",
    "templates/layer.example.json", "templates/region.example.json",
    "templates/standard_layer.example.json", "templates/analytics-plugin.example.json",
    "templates/adaptive-density.example.json", "templates/regeneration.example.json", "templates/contacts-sources.example.json", "templates/contacts-seed.example.json",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record(path: Path, base: Path) -> dict:
    return {"path": str(path.relative_to(base)), "bytes": path.stat().st_size, "sha256": sha256(path)}


def config_sha256(config: dict) -> str:
    """Bind data to its contract while allowing draft → production promotion."""
    normalized = {key: value for key, value in config.items() if key != "lifecycle"}
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def core_records() -> dict:
    missing = [name for name in CORE_FILES if not (ROOT / name).is_file()]
    if missing:
        raise ValueError(f"Missing core files: {missing}")
    return {name: record(ROOT / name, ROOT) for name in CORE_FILES}


def verify_core(write: bool = False) -> None:
    current = {"schema_version": 1, "core_version": CORE_VERSION, "files": core_records()}
    if write:
        atomic_json(CORE_LOCK, current); print("Core lock written"); return
    if not CORE_LOCK.is_file() or read_json(CORE_LOCK) != current:
        raise ValueError("Shared core differs from core.lock.json; review it and use verify-core --write-core-lock")
    print("Core verified")


def safe_id(value: str, label: str = "id") -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ValueError(f"{label} must contain lowercase ASCII letters, digits, _ or -")
    return value


def validate_registry_payload(registry: dict, require_regions: bool = False) -> dict:
    if set(registry) != {"schema_version", "default_region", "regions"} or registry.get("schema_version") != 1:
        raise ValueError("registry.json has unsupported or unknown fields")
    regions = registry.get("regions")
    if not isinstance(regions, list) or any(not isinstance(item, str) for item in regions):
        raise ValueError("registry.json regions must be an array of region IDs")
    for item in regions:
        safe_id(item, "registry region_id")
    if len(regions) != len(set(regions)):
        raise ValueError("registry.json contains duplicate region IDs")
    default = registry.get("default_region")
    if default is not None and (not isinstance(default, str) or default not in regions):
        raise ValueError("registry.json default_region must be null or belong to regions")
    if require_regions and (not regions or default is None):
        raise ValueError("deployment requires at least one region and a default_region")
    return registry


def validate_registry(require_regions: bool = False) -> dict:
    if REGISTRY.is_symlink():
        raise ValueError("registry.json must be a regular file, not a symlink")
    return validate_registry_payload(read_json(REGISTRY), require_regions)


def region_dir(region_id: str) -> Path:
    return REGIONS / safe_id(region_id, "region_id")


def data_relative(filename: str) -> Path:
    path = Path(filename)
    if path.is_absolute() or not path.parts or path.parts[0] != "data" or ".." in path.parts or path.suffix.lower() != ".geojson":
        raise ValueError(f"Layer file must be a safe GeoJSON path below data/: {filename}")
    return Path(*path.parts[1:])


def output_for(spec: dict) -> str:
    return str(Path("pipeline/outputs") / data_relative(spec["file"]))


def validate_config(config: dict, region_id: str) -> None:
    required = {"schema_version", "core_version", "region_id", "lifecycle", "data_mode", "title", "page_title", "view", "layers"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"region.json misses fields: {sorted(missing)}")
    if config["schema_version"] != 2 or config["core_version"] != CORE_VERSION or config["region_id"] != region_id:
        raise ValueError("Unsupported schema/core version or mismatched region_id")
    if config["lifecycle"] not in {"draft", "production"} or config["data_mode"] not in {"external", "pipeline"}:
        raise ValueError("lifecycle/data_mode is invalid")
    view = config["view"]
    center = view.get("center")
    if not isinstance(center, list) or len(center) != 2 or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in center):
        raise ValueError("view.center must contain numeric latitude and longitude")
    if not -90 <= center[0] <= 90 or not -180 <= center[1] <= 180:
        raise ValueError("view.center is outside WGS 84 bounds")
    if not all(isinstance(view.get(key), (int, float)) for key in ("zoom", "min_zoom", "max_zoom")) or not view["min_zoom"] <= view["zoom"] <= view["max_zoom"]:
        raise ValueError("Invalid zoom range")
    if not isinstance(config["layers"], dict):
        raise ValueError("layers must be an object")
    files = []
    for layer_id, spec in config["layers"].items():
        safe_id(layer_id, "layer_id")
        if not isinstance(spec, dict) or not isinstance(spec.get("label"), str) or not spec["label"]:
            raise ValueError(f"Layer {layer_id} must have a label")
        data_relative(spec.get("file", "")); files.append(spec["file"])
        renderer = spec.get("renderer", "auto")
        if renderer not in {"auto", "points", "lines", "polygons", "choropleth", "density", "ranking"}:
            raise ValueError(f"Layer {layer_id} has unsupported renderer: {renderer}")
        geometry_types = spec.get("geometry_types", [])
        property_lists = [spec.get("required_properties", []), spec.get("numeric_properties", []), spec.get("unique_by", [])]
        if not isinstance(geometry_types, list) or not geometry_types or not set(geometry_types) <= GEOMETRY_TYPES:
            raise ValueError(f"Layer {layer_id} has invalid geometry_types")
        if any(not isinstance(items, list) or len(items) != len(set(items)) or not all(isinstance(item, str) and item for item in items) for items in property_lists):
            raise ValueError(f"Layer {layer_id} contracts must be arrays")
        if spec.get("min_zoom") is not None and spec.get("max_zoom") is not None and spec["min_zoom"] > spec["max_zoom"]:
            raise ValueError(f"Layer {layer_id} has min_zoom greater than max_zoom")
        bins = spec.get("bins", [])
        if not isinstance(bins, list) or any(not isinstance(item, dict) or not isinstance(item.get("color"), str) or not item["color"] or (item.get("max") is not None and (not isinstance(item.get("max"), (int, float)) or isinstance(item.get("max"), bool))) for item in bins):
            raise ValueError(f"Layer {layer_id} bins are malformed")
        finite_maxima = [item.get("max") for item in bins if item.get("max") is not None]
        if finite_maxima != sorted(finite_maxima) or sum(item.get("max") is None for item in bins) > 1 or any(item.get("max") is None for item in bins[:-1]):
            raise ValueError(f"Layer {layer_id} bins must be ascending with an optional final null")
        if bins and not spec.get("value_field"):
            raise ValueError(f"Layer {layer_id} with bins must declare value_field")
        if spec.get("categories") and not spec.get("category_field"):
            raise ValueError(f"Layer {layer_id} with categories must declare category_field")
        categories = spec.get("categories", {})
        if not isinstance(categories, dict) or any(not isinstance(item, dict) or not isinstance(item.get("color"), str) or not item["color"] for item in categories.values()):
            raise ValueError(f"Layer {layer_id} categories are malformed")
        if not isinstance(spec.get("popup_fields", []), list) or not isinstance(spec.get("table", {}), dict) or not isinstance(spec.get("style", {}), dict):
            raise ValueError(f"Layer {layer_id} display contract is malformed")
        link = spec.get("external_map_link")
        if "external_map_link" in spec and link is not False:
            allowed = {"provider", "label", "zoom", "related_layer", "feature_join_field", "related_join_field", "related_filter_field", "related_filter_min_exclusive"}
            if not isinstance(link, dict) or link.get("provider") != "yandex_maps" or set(link) - allowed:
                raise ValueError(f"Layer {layer_id}: external_map_link is malformed")
            if "label" in link and (not isinstance(link["label"], str) or not link["label"]):
                raise ValueError(f"Layer {layer_id}: external_map_link.label must be a non-empty string")
            if "zoom" in link and (not isinstance(link["zoom"], (int, float)) or isinstance(link["zoom"], bool) or not 0 <= link["zoom"] <= 23):
                raise ValueError(f"Layer {layer_id}: external_map_link.zoom must be between 0 and 23")
            related_layer = link.get("related_layer")
            relation_fields = ("feature_join_field", "related_join_field")
            if related_layer is not None:
                safe_id(related_layer, "external_map_link.related_layer")
                if related_layer not in config["layers"] or any(not isinstance(link.get(key), str) or not link[key] for key in relation_fields):
                    raise ValueError(f"Layer {layer_id}: external_map_link relation is invalid")
            elif any(key in link for key in (*relation_fields, "related_filter_field", "related_filter_min_exclusive")):
                raise ValueError(f"Layer {layer_id}: external_map_link relation fields require related_layer")
            if "related_filter_field" in link and (not isinstance(link["related_filter_field"], str) or not link["related_filter_field"]):
                raise ValueError(f"Layer {layer_id}: external_map_link filter is invalid")
            if "related_filter_min_exclusive" in link and ("related_filter_field" not in link or not isinstance(link["related_filter_min_exclusive"], (int, float)) or isinstance(link["related_filter_min_exclusive"], bool)):
                raise ValueError(f"Layer {layer_id}: external_map_link threshold is invalid")
        style = spec.get("style", {})
        if "regenerable" in spec and not isinstance(spec["regenerable"], bool):
            raise ValueError(f"Layer {layer_id}: regenerable must be boolean")
        for key in ("fill_max_zoom", "fill_opacity", "fill_opacity_above_max"):
            if key in style and (not isinstance(style[key], (int, float)) or isinstance(style[key], bool)):
                raise ValueError(f"Layer {layer_id}: style.{key} must be numeric")
        for key in ("fill_opacity", "fill_opacity_above_max"):
            if key in style and not 0 <= style[key] <= 1:
                raise ValueError(f"Layer {layer_id}: style.{key} must be between 0 and 1")
    if len(files) != len(set(files)):
        raise ValueError("Every layer must use a unique file")


def validate_regeneration(root: Path, config: dict) -> int:
    path = root / "pipeline" / "regeneration.json"
    if not path.is_file():
        return 0
    spec = read_json(path)
    required = {"schema_version", "region_id", "enabled", "min_interval_seconds", "outputs", "commands"}
    if set(spec) != required:
        raise ValueError(f"Regeneration fields differ from schema: missing={sorted(required - set(spec))}, unexpected={sorted(set(spec) - required)}")
    if spec.get("schema_version") != 1 or spec.get("region_id") != config["region_id"] or not isinstance(spec.get("enabled"), bool):
        raise ValueError("Invalid regeneration identity or enabled flag")
    interval = spec.get("min_interval_seconds", 300)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 300:
        raise ValueError("Regeneration min_interval_seconds must be an integer >= 300")
    outputs = spec.get("outputs")
    if not isinstance(outputs, list) or not outputs or not all(isinstance(item, str) and item for item in outputs) or len(outputs) != len(set(outputs)):
        raise ValueError("Regeneration outputs must be a non-empty unique string list")
    allowed = {str(data_relative(layer["file"])) for layer in config["layers"].values() if layer.get("regenerable") is True}
    if not set(outputs) <= allowed:
        raise ValueError("Every regeneration output must belong to a regenerable layer")
    commands = spec.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("Regeneration commands must be a non-empty list")
    for step in commands:
        if not isinstance(step, dict) or not set(step) <= {"name", "cwd", "command"} or "command" not in step or ("name" in step and not isinstance(step["name"], str)) or not isinstance(step.get("cwd", "."), str) or not isinstance(step.get("command"), list) or not step["command"] or not all(isinstance(item, str) and item for item in step["command"]):
            raise ValueError("Invalid regeneration command")
        cwd = (root / step.get("cwd", ".")).resolve()
        if cwd != root and root not in cwd.parents:
            raise ValueError("Invalid regeneration working directory")
    return len(outputs)


def validate_contacts(root: Path, config: dict) -> dict | None:
    path = root / "contacts" / "sources.json"
    if not path.is_file():
        return None
    spec = read_json(path)
    required = {"schema_version", "region_id", "enabled", "min_interval_seconds", "request_delay_seconds", "sources"}
    if set(spec) != required:
        raise ValueError(f"Contact source fields differ from schema: missing={sorted(required-set(spec))}, unexpected={sorted(set(spec)-required)}")
    if spec.get("schema_version") != 1 or spec.get("region_id") != config["region_id"] or not isinstance(spec.get("enabled"), bool):
        raise ValueError("Invalid contact source identity or enabled flag")
    if not isinstance(spec.get("min_interval_seconds"), int) or isinstance(spec["min_interval_seconds"], bool) or spec["min_interval_seconds"] < 3600:
        raise ValueError("Contact collection interval must be an integer >= 3600")
    delay = spec.get("request_delay_seconds")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 1 <= delay <= 60:
        raise ValueError("Contact request delay must be between 1 and 60 seconds")
    sources = spec.get("sources")
    if not isinstance(sources, list) or (spec["enabled"] and not sources):
        raise ValueError("Enabled contact collection requires at least one source")
    identifiers = set()
    common = {"source_id", "type"}
    allowed_by_type = {
        "local_json": common | {"path"},
        "remote_json": common | {"url", "allowed_hosts", "contacts_key"},
        "website": common | {"name", "kind", "coverage", "specializations", "addresses", "urls", "allowed_hosts", "respect_robots_txt", "request_delay_seconds"},
    }
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            raise ValueError("Every contact source must be an object with source_id")
        source_id = safe_id(source["source_id"], "contact source_id")
        if source_id in identifiers: raise ValueError(f"Duplicate contact source_id: {source_id}")
        identifiers.add(source_id); kind = source.get("type")
        if kind not in allowed_by_type or set(source) - allowed_by_type[kind]:
            raise ValueError(f"Contact source {source_id} has unsupported type or fields")
        if kind == "local_json":
            relative = Path(source.get("path", ""))
            if relative.is_absolute() or not relative.parts or relative.parts[0] != "contacts" or ".." in relative.parts or relative.suffix.lower() != ".json":
                raise ValueError(f"Contact source {source_id} has an unsafe local path")
            local = root / relative
            if local.is_symlink() or not local.is_file(): raise ValueError(f"Contact source {source_id} local JSON file is missing or unsafe")
        else:
            urls = source.get("urls") if kind == "website" else [source.get("url")]
            hosts = source.get("allowed_hosts")
            if not isinstance(urls, list) or not urls or len(urls) != len(set(urls)) or not all(isinstance(item, str) and item.startswith("https://") for item in urls):
                raise ValueError(f"Contact source {source_id} requires unique HTTPS URLs")
            if not isinstance(hosts, list) or not hosts or len(hosts) != len(set(hosts)) or not all(isinstance(item, str) and re.fullmatch(r"[a-z0-9.-]+", item) for item in hosts):
                raise ValueError(f"Contact source {source_id} requires explicit allowed_hosts")
            allowed = {item.lower().rstrip(".") for item in hosts}
            for value in urls:
                try: host = urllib.parse.urlsplit(value).hostname
                except ValueError: host = None
                if not host or host.lower().rstrip(".") not in allowed:
                    raise ValueError(f"Contact source {source_id} URL host is not allowlisted")
        if kind == "remote_json" and "contacts_key" in source and (not isinstance(source["contacts_key"], str) or not source["contacts_key"]):
            raise ValueError(f"Remote JSON source {source_id}.contacts_key must be a non-empty string")
        if kind == "website":
            if not isinstance(source.get("name"), str) or not source["name"].strip(): raise ValueError(f"Website source {source_id} requires name")
            if source.get("kind", "other") not in {"agency", "realtor", "broker", "developer", "property_manager", "other"}: raise ValueError(f"Website source {source_id} has invalid kind")
            for field in ("coverage", "specializations", "addresses"):
                if field in source and (not isinstance(source[field], list) or not all(isinstance(item, str) and item.strip() for item in source[field])): raise ValueError(f"Website source {source_id}.{field} must be an array of strings")
            if "respect_robots_txt" in source and not isinstance(source["respect_robots_txt"], bool): raise ValueError(f"Website source {source_id}.respect_robots_txt must be boolean")
            if "request_delay_seconds" in source and (not isinstance(source["request_delay_seconds"], (int, float)) or isinstance(source["request_delay_seconds"], bool) or not 1 <= source["request_delay_seconds"] <= 60): raise ValueError(f"Website source {source_id} request delay is invalid")
    return spec


def valid_coordinates(value) -> bool:
    if isinstance(value, list) and len(value) >= 2 and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2]):
        return -180 <= value[0] <= 180 and -90 <= value[1] <= 90
    return isinstance(value, list) and bool(value) and all(valid_coordinates(item) for item in value)


def validate_geojson(path: Path, layer_id: str, spec: dict) -> int:
    document = read_json(path)
    if document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
        raise ValueError(f"Not a GeoJSON FeatureCollection: {path}")
    allowed = set(spec.get("geometry_types") or ["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"])
    required, numeric = set(spec.get("required_properties") or []), set(spec.get("numeric_properties") or [])
    identities = set(); identity_fields = spec.get("unique_by") or []
    for index, feature in enumerate(document["features"]):
        geometry, properties = feature.get("geometry") or {}, feature.get("properties")
        if feature.get("type") != "Feature" or geometry.get("type") not in allowed:
            raise ValueError(f"{layer_id}[{index}] has an invalid feature or geometry type")
        if not valid_coordinates(geometry.get("coordinates")):
            raise ValueError(f"{layer_id}[{index}] has invalid coordinates")
        if not isinstance(properties, dict) or required - set(properties):
            raise ValueError(f"{layer_id}[{index}] misses properties: {sorted(required - set(properties or {}))}")
        for name in numeric:
            value = properties.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{layer_id}[{index}].{name} must be numeric")
        if identity_fields:
            identity = tuple(str(properties.get(name, "")).strip().casefold() for name in identity_fields)
            if identity in identities:
                raise ValueError(f"{layer_id}[{index}] duplicates unique_by={identity_fields}: {identity}")
            identities.add(identity)
    return len(document["features"])


def layer_records(root: Path, config: dict) -> tuple[dict, dict, list[str]]:
    counts, records, missing = {}, {}, []
    for layer_id, spec in config["layers"].items():
        path = root / spec["file"]
        if not path.is_file():
            missing.append(spec["file"]); continue
        counts[layer_id] = validate_geojson(path, layer_id, spec)
        records[spec["file"]] = record(path, root)
    return counts, records, missing


def runtime_dir(root: Path) -> Path:
    return RUNTIME_ROOT / root.name if RUNTIME_ROOT else root / ".runtime"


def runtime_manifest(root: Path) -> Path:
    return runtime_dir(root) / "publication.json"


def pipeline_manifest(root: Path) -> Path:
    pipeline = read_json(root / "pipeline" / "pipeline.json")
    return root / "pipeline" / pipeline.get("manifest_file", "build_manifest.json")


def validate_provenance(root: Path, config: dict, records: dict, require: bool) -> list[str]:
    issues = []
    path = runtime_manifest(root)
    if not path.is_file():
        issues.append("publication manifest is missing")
        return issues
    manifest = read_json(path)
    if manifest.get("schema_version") != 1 or manifest.get("region_id") != config["region_id"] or manifest.get("core_version") != CORE_VERSION or manifest.get("data_mode") != config["data_mode"]:
        issues.append("publication manifest identity differs")
    if manifest.get("configuration_sha256") != config_sha256(config):
        issues.append("region configuration differs from publication manifest")
    if manifest.get("outputs") != records:
        issues.append("published data differ from publication manifest")
    if config["data_mode"] == "pipeline":
        runner_manifest = pipeline_manifest(root)
        upstream = manifest.get("upstream_manifest")
        if not runner_manifest.is_file() or upstream != record(runner_manifest, root):
            issues.append("runner manifest differs from publication manifest")
        pipeline = read_json(root / "pipeline" / "pipeline.json")
        expected = {output_for(spec) for spec in config["layers"].values()}
        if set(pipeline.get("outputs", [])) != expected:
            issues.append("pipeline outputs do not exactly match declared layers")
    if require and issues:
        raise ValueError("; ".join(issues))
    return issues


def inspect_region(region_id: str, allow_missing: bool = False, require_provenance: bool = True, emit: bool = True) -> dict:
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id); validate_regeneration(root, config); validate_contacts(root, config)
    counts, records, missing = layer_records(root, config)
    if missing and not allow_missing:
        raise ValueError(f"Missing data files: {missing}. Attach data or build the pipeline first.")
    expected = config.get("expected") or {}
    # A code-only checkout has no datasets. In allow-missing mode compare
    # counts only for available layers, retaining strict checks for unknown IDs.
    differences = {key: {"expected": value, "actual": counts.get(key)} for key, value in expected.items()
                   if (key in counts or not (allow_missing and key in config["layers"])) and counts.get(key) != value}
    if differences:
        raise ValueError(f"Layer counts differ: {differences}")
    empty = [layer_id for layer_id, spec in config["layers"].items() if spec.get("required_for_production") and counts.get(layer_id, 0) == 0]
    provenance = validate_provenance(root, config, records, require_provenance and not missing)
    if config["lifecycle"] == "production" and (missing or empty or provenance):
        raise ValueError(f"Production package is incomplete; missing={missing}, required_empty={empty}, provenance={provenance}")
    result = {"region_id": region_id, "lifecycle": config["lifecycle"], "data_mode": config["data_mode"], "layers": counts, "missing": missing, "required_empty": empty, "provenance_issues": provenance}
    if emit: print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def write_publication_manifest(root: Path, config: dict, mode_details: dict | None = None) -> dict:
    counts, records, missing = layer_records(root, config)
    if missing:
        raise ValueError(f"Cannot accept incomplete publication: {missing}")
    manifest = {"schema_version": 1, "core_version": CORE_VERSION, "region_id": config["region_id"], "data_mode": config["data_mode"], "configuration_sha256": config_sha256(config), "outputs": records, "validation": counts}
    if config["data_mode"] == "pipeline":
        runner_manifest = pipeline_manifest(root)
        if not runner_manifest.is_file(): raise ValueError("Runner manifest is missing")
        manifest["upstream_manifest"] = record(runner_manifest, root)
    if mode_details: manifest["attachment"] = mode_details
    atomic_json(runtime_manifest(root), manifest)
    return manifest


def init_region(region_id: str, title: str, latitude: float, longitude: float, data_mode: str) -> None:
    root = region_dir(region_id)
    if root.exists(): raise ValueError(f"Region already exists: {region_id}")
    (root / "sources").mkdir(parents=True); (root / "pipeline" / "outputs").mkdir(parents=True); (root / "pipeline" / "plugins").mkdir()
    config = {"schema_version": 2, "core_version": CORE_VERSION, "region_id": region_id, "lifecycle": "draft", "data_mode": data_mode, "title": title, "page_title": f"{title} — аналитическая карта", "subtitle": "Подключаемые географические, объектные и аналитические слои.", "source_note": "Источники ещё не подключены.", "view": {"center": [latitude, longitude], "zoom": 7, "min_zoom": 3, "max_zoom": 19}, "layers": {}, "expected": {}}
    pipeline = {"schema_version": 1, "region_id": region_id, "dependency_lock": "pipeline/requirements-lock.txt", "inputs": [], "outputs": [], "steps": []}
    atomic_json(root / "region.json", config); atomic_json(root / "pipeline" / "pipeline.json", pipeline)
    shutil.copy2(ROOT / "pipeline_core" / "requirements-minimal-lock.txt", root / "pipeline" / "requirements-lock.txt")
    (root / "SOURCES.md").write_text(f"# Источники: {title}\n\nЗаполните источник, дату снимка, лицензию, SHA-256 и преобразования каждого слоя.\n", encoding="utf-8")
    (root / "sources" / "README.md").write_text("# Локальные исходники\n\nФайлы этого каталога игнорируются Git. Не удаляйте этот README.\n", encoding="utf-8")
    (root / "pipeline" / "plugins" / "README.md").write_text("# Аналитические plugins\n\nКаждый JSON обязан соответствовать schemas/analytics-plugin.schema.json.\n", encoding="utf-8")
    registry = validate_registry(); registry["regions"] = sorted(set(registry["regions"]) | {region_id}); registry["default_region"] = registry.get("default_region") or region_id; atomic_json(REGISTRY, registry)
    print(f"Initialized {region_id}: {root}")


def add_layer(region_id: str, layer_id: str, filename: str, label: str, geometry_types: list[str], renderer: str, required: bool) -> None:
    root = region_dir(region_id); config_path = root / "region.json"; config = read_json(config_path); validate_config(config, region_id); safe_id(layer_id, "layer_id")
    if layer_id in config["layers"]: raise ValueError(f"Layer already exists: {layer_id}")
    relative = data_relative("data/" + filename)
    config["layers"][layer_id] = {"file": str(Path("data") / relative), "label": label, "renderer": renderer, "geometry_types": geometry_types, "required_properties": ["name"], "numeric_properties": [], "required_for_production": required, "default_visible": True, "title_field": "name", "popup_fields": ["name"]}
    config_before = config_path.read_bytes(); pipeline_path = root / "pipeline" / "pipeline.json"; pipeline_before = pipeline_path.read_bytes()
    try:
        atomic_json(config_path, config)
        if config["data_mode"] == "pipeline":
            pipeline = json.loads(pipeline_before); pipeline["outputs"] = sorted(set(pipeline.get("outputs", [])) | {output_for(config["layers"][layer_id])}); atomic_json(pipeline_path, pipeline)
    except BaseException:
        config_path.write_bytes(config_before); pipeline_path.write_bytes(pipeline_before); raise
    print(f"Added layer {layer_id}; expected at {root / 'data' / relative}")


def attach_data(region_id: str, source: Path, mode: str) -> None:
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id)
    if config["data_mode"] != "external": raise ValueError("attach-data requires data_mode=external")
    source = source.expanduser().resolve()
    if not source.is_dir(): raise ValueError(f"Data directory does not exist: {source}")
    target = root / "data"; is_junction = bool(getattr(target, "is_junction", lambda: False)()); previous_kind = "junction" if is_junction else "symlink" if target.is_symlink() else None; previous_link = target.resolve() if previous_kind else None; manifest_path = runtime_manifest(root); previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    if target.is_symlink(): target.unlink()
    elif is_junction: os.rmdir(target)
    elif target.exists():
        if any(target.iterdir()): raise ValueError(f"Refusing to replace non-empty directory: {target}")
        target.rmdir()
    try:
        if mode == "copy": shutil.copytree(source, target)
        elif mode == "symlink": target.symlink_to(source, target_is_directory=True)
        else:
            if os.name == "nt": subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)], check=True)
            else: target.symlink_to(source, target_is_directory=True)
        write_publication_manifest(root, config, {"mode": mode, "source": str(source)})
        inspect_region(region_id)
    except BaseException:
        if target.is_symlink(): target.unlink()
        elif target.exists() and mode == "copy": shutil.rmtree(target)
        if previous_link is not None:
            if previous_kind == "junction" and os.name == "nt": subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(previous_link)], check=True)
            else: target.symlink_to(previous_link, target_is_directory=True)
        if previous_manifest is None: manifest_path.unlink(missing_ok=True)
        else: manifest_path.parent.mkdir(parents=True, exist_ok=True); manifest_path.write_bytes(previous_manifest)
        raise
    print(f"Attached data using {mode}: {target}")


def accept_data(region_id: str) -> None:
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id)
    if config["data_mode"] != "external": raise ValueError("accept-data is only for external mode")
    previous = read_json(runtime_manifest(root)).get("attachment", {}) if runtime_manifest(root).is_file() else {}
    write_publication_manifest(root, config, previous); inspect_region(region_id); print("External data snapshot accepted")


def detach_data(region_id: str) -> None:
    root = region_dir(region_id); target = root / "data"; manifest = read_json(runtime_manifest(root)) if runtime_manifest(root).is_file() else {}; mode = (manifest.get("attachment") or {}).get("mode")
    if target.is_symlink(): target.unlink()
    elif os.name == "nt" and mode == "junction" and target.exists(): os.rmdir(target)
    else: raise ValueError("data is not an attached link; copied data are intentionally not deleted")
    runtime_manifest(root).unlink(missing_ok=True); print(f"Detached {target}; external files were not changed")


def validate_pipeline_binding(root: Path, config: dict) -> None:
    pipeline = read_json(root / "pipeline" / "pipeline.json")
    expected = {output_for(spec) for spec in config["layers"].values()}
    if set(pipeline.get("outputs", [])) != expected: raise ValueError(f"pipeline outputs must exactly match layers: expected={sorted(expected)}")
    if not pipeline.get("steps"): raise ValueError("Pipeline has no build steps")
    validate_plugins(root, pipeline)


def validate_plugins(root: Path, pipeline: dict) -> int:
    plugin_dir = root / "pipeline" / "plugins"
    plugins = [] if not plugin_dir.is_dir() else sorted(plugin_dir.glob("*.json"))
    identifiers = set(); outputs = set(pipeline.get("outputs", []))
    for path in plugins:
        plugin = read_json(path); required = {"plugin_id", "version", "description", "inputs", "output", "methodology", "parameters", "quality_controls"}; missing = required - set(plugin)
        if missing: raise ValueError(f"{path} misses plugin fields: {sorted(missing)}")
        if set(plugin) != required: raise ValueError(f"{path} contains unsupported plugin fields: {sorted(set(plugin)-required)}")
        plugin_id = safe_id(plugin["plugin_id"], "plugin_id")
        if not isinstance(plugin["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", plugin["version"]): raise ValueError(f"Plugin {plugin_id} version must be semantic x.y.z")
        if not isinstance(plugin["description"], str) or not plugin["description"].strip(): raise ValueError(f"Plugin {plugin_id} description is empty")
        if not isinstance(plugin["inputs"], list) or not plugin["inputs"] or len(plugin["inputs"]) != len(set(plugin["inputs"])) or not all(isinstance(item, str) and item for item in plugin["inputs"]): raise ValueError(f"Plugin {plugin_id} inputs are invalid")
        if not isinstance(plugin["parameters"], dict) or not isinstance(plugin["quality_controls"], dict): raise ValueError(f"Plugin {plugin_id} parameters/quality_controls must be objects")
        if plugin_id in identifiers: raise ValueError(f"Duplicate analytical plugin_id: {plugin_id}")
        identifiers.add(plugin_id)
        if plugin["output"] not in outputs: raise ValueError(f"Plugin {plugin_id} output is not declared by pipeline")
        for name in [*plugin["inputs"], plugin["methodology"]]:
            resolved = (root / name).resolve()
            if root not in resolved.parents or not resolved.is_file(): raise ValueError(f"Plugin {plugin_id} input/methodology is missing or unsafe: {name}")
    return len(plugins)


def sync(region_id: str) -> None:
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id)
    if config["data_mode"] != "pipeline": raise ValueError("sync requires data_mode=pipeline")
    validate_pipeline_binding(root, config)
    subprocess.run([sys.executable, str(ROOT / "pipeline_core" / "runner.py"), "--region-root", str(root), "--full", "--validate-only"], check=True)
    with tempfile.TemporaryDirectory(prefix=f".{region_id}-publish-", dir=root) as temporary:
        stage = Path(temporary) / "data"; stage.mkdir()
        for spec in config["layers"].values():
            relative = data_relative(spec["file"]); source = root / "pipeline" / "outputs" / relative; target = stage / relative
            if not source.is_file(): raise ValueError(f"Pipeline output is missing: {source}")
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
        target, backup = root / "data", root / ".data.backup"
        if target.is_symlink(): raise ValueError("Detach external data before pipeline publication")
        if backup.exists(): shutil.rmtree(backup)
        try:
            if target.exists(): os.replace(target, backup)
            os.replace(stage, target)
            write_publication_manifest(root, config)
        except BaseException:
            if target.exists(): shutil.rmtree(target)
            if backup.exists(): os.replace(backup, target)
            raise
        if backup.exists(): shutil.rmtree(backup)
    inspect_region(region_id); print("Pipeline outputs published transactionally")


def build(region_id: str) -> None:
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id); validate_pipeline_binding(root, config)
    subprocess.run([sys.executable, str(ROOT / "pipeline_core" / "runner.py"), "--region-root", str(root), "--full"], check=True); sync(region_id)


def readiness(region_id: str) -> dict:
    root = region_dir(region_id); config = read_json(root / "region.json"); result = inspect_region(region_id, allow_missing=True, require_provenance=False, emit=False); issues = []
    if not config["layers"]: issues.append("no layers declared")
    if result["missing"]: issues.append(f"missing layers: {result['missing']}")
    if result["required_empty"]: issues.append(f"required layers are empty: {result['required_empty']}")
    if result["layers"] and sum(result["layers"].values()) == 0: issues.append("all declared layers are empty")
    if result["provenance_issues"]: issues.extend(result["provenance_issues"])
    if not str(config.get("source_note", "")).strip() or config.get("source_note") == "Источники ещё не подключены.": issues.append("source provenance is not documented")
    sources = root / "SOURCES.md"
    if not sources.is_file() or len(sources.read_text(encoding="utf-8").strip()) < 100: issues.append("SOURCES.md is incomplete")
    if config["data_mode"] == "pipeline":
        try: validate_pipeline_binding(root, config)
        except (ValueError, FileNotFoundError) as error: issues.append(str(error))
    return {**result, "issues": issues, "ready_to_promote": config["lifecycle"] == "draft" and not issues, "ready_for_production": config["lifecycle"] == "production" and not issues}


def doctor(region_id: str) -> None:
    print(json.dumps(readiness(region_id), ensure_ascii=False, indent=2))


def promote(region_id: str) -> None:
    root = region_dir(region_id); path = root / "region.json"; config = read_json(path)
    state = readiness(region_id)
    if state["issues"]: raise ValueError("Region is not ready for production: " + "; ".join(state["issues"]))
    config["lifecycle"] = "production"; atomic_json(path, config)
    try: inspect_region(region_id)
    except BaseException:
        config["lifecycle"] = "draft"; atomic_json(path, config); raise
    print(f"Promoted {region_id} to production")


def validate_all(allow_missing: bool) -> None:
    for item in validate_registry()["regions"]: inspect_region(item, allow_missing=allow_missing, require_provenance=not allow_missing)


def deployment_check() -> None:
    registry = validate_registry(require_regions=True); results = {}; failures = {}
    for region_id in registry["regions"]:
        root = region_dir(region_id); unsafe = []
        if root.is_symlink(): unsafe.append("region directory must not be a symlink")
        if (root / "region.json").is_symlink(): unsafe.append("region.json must not be a symlink")
        if unsafe:
            results[region_id] = {"issues": unsafe, "ready_for_production": False}; failures[region_id] = unsafe; continue
        state = readiness(region_id); results[region_id] = state
        issues = list(state["issues"])
        if not state["ready_for_production"]:
            config = read_json(region_dir(region_id) / "region.json")
            if config.get("lifecycle") != "production": issues.append("lifecycle is not production")
        if issues: failures[region_id] = sorted(set(issues))
    report = {"content_root": str(CONTENT_ROOT), "default_region": registry["default_region"], "regions": results, "ready": not failures}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures: raise ValueError("Deployment is not ready: " + json.dumps(failures, ensure_ascii=False))


def deployment_contract_paths(root: Path) -> list[Path]:
    paths = [root / "region.json", root / "SOURCES.md"]
    for directory in (root / "sources", root / "pipeline", root / "contacts"):
        if directory.is_symlink():
            raise ValueError(f"Deployment contract directory must not be a symlink: {directory}")
    source_readme = root / "sources" / "README.md"
    if source_readme.is_file(): paths.append(source_readme)
    pipeline = root / "pipeline"
    if pipeline.is_dir():
        for path in pipeline.rglob("*"):
            relative = path.relative_to(pipeline)
            if any(part in {"cache", "outputs", "__pycache__"} for part in relative.parts) or path.suffix in {".pyc", ".pyo"}: continue
            if path.is_symlink(): raise ValueError(f"Deployment contract must not contain symlinks: {path}")
            if path.is_file(): paths.append(path)
    contacts = root / "contacts"
    if contacts.is_dir():
        for path in contacts.rglob("*"):
            relative = path.relative_to(contacts)
            if any(part in {"__pycache__", ".runtime"} for part in relative.parts) or path.suffix in {".pyc", ".pyo"}: continue
            if path.is_symlink(): raise ValueError(f"Deployment contact contract must not contain symlinks: {path}")
            if path.is_file(): paths.append(path)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe deployment contract file: {path}")
    return sorted(set(paths))


def build_deployment_bundle(output: Path) -> None:
    deployment_check(); registry = validate_registry(require_regions=True)
    output = output.expanduser().absolute()
    if output.exists(): raise ValueError(f"Refusing to replace existing deployment bundle: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        content = staging / "content"; content.mkdir()
        shutil.copy2(REGISTRY, content / "registry.json")
        data = {}
        for region_id in registry["regions"]:
            root = region_dir(region_id); destination = content / "regions" / region_id
            for source in deployment_contract_paths(root):
                relative = source.relative_to(root); target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
            publication = read_json(runtime_manifest(root))
            data[region_id] = {
                "data_mode": publication["data_mode"],
                "configuration_sha256": publication["configuration_sha256"],
                "outputs": publication["outputs"],
                "validation": publication["validation"],
            }
        files = {str(path.relative_to(staging)): record(path, staging) for path in sorted(content.rglob("*")) if path.is_file() and not path.is_symlink()}
        manifest = {
            "schema_version": 1,
            "core_version": CORE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "default_region": registry["default_region"],
            "regions": registry["regions"],
            "files": files,
            "data": data,
        }
        atomic_json(staging / "deployment-manifest.json", manifest)
        verify_deployment_bundle(staging)
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True); raise
    print(f"Deployment bundle written without datasets: {output}")


def validate_file_record(value: dict, expected_path: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256"}:
        raise ValueError(f"Invalid file record: {expected_path}")
    if value.get("path") != expected_path or not isinstance(value.get("bytes"), int) or isinstance(value["bytes"], bool) or value["bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
        raise ValueError(f"Invalid file metadata: {expected_path}")


def verify_deployment_bundle(bundle: Path, data_root: Path | None = None) -> None:
    bundle = bundle.expanduser().absolute()
    if not bundle.is_dir() or bundle.is_symlink(): raise ValueError(f"Deployment bundle is not a regular directory: {bundle}")
    bundle = bundle.resolve()
    symlinks = [str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_symlink()]
    if symlinks: raise ValueError(f"Deployment bundle contains symlinks: {symlinks}")
    manifest_path = bundle / "deployment-manifest.json"; manifest = read_json(manifest_path)
    required = {"schema_version", "core_version", "created_at", "default_region", "regions", "files", "data"}
    if set(manifest) != required or manifest.get("schema_version") != 1 or manifest.get("core_version") != CORE_VERSION:
        raise ValueError("Deployment manifest has unsupported or unknown fields")
    try: created = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError): raise ValueError("Deployment manifest created_at is invalid") from None
    if created.tzinfo is None: raise ValueError("Deployment manifest created_at must include a timezone")
    registry = validate_registry_payload(read_json(bundle / "content" / "registry.json"), require_regions=True)
    if manifest["regions"] != registry["regions"] or manifest["default_region"] != registry["default_region"]:
        raise ValueError("Deployment manifest differs from content/registry.json")
    files = manifest.get("files")
    if not isinstance(files, dict): raise ValueError("Deployment manifest files must be an object")
    actual_paths = {str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file() and path != manifest_path}
    if set(files) != actual_paths: raise ValueError(f"Deployment contract file set differs: expected={sorted(files)}, actual={sorted(actual_paths)}")
    for relative, expected in files.items():
        validate_file_record(expected, relative)
        if record(bundle / relative, bundle) != expected: raise ValueError(f"Deployment contract checksum differs: {relative}")
    data = manifest.get("data")
    if not isinstance(data, dict) or set(data) != set(registry["regions"]): raise ValueError("Deployment data manifest differs from registry")
    for region_id in registry["regions"]:
        config = read_json(bundle / "content" / "regions" / region_id / "region.json"); validate_config(config, region_id)
        if config["lifecycle"] != "production": raise ValueError(f"Deployment region lifecycle is not production: {region_id}")
        spec = data[region_id]
        if not isinstance(spec, dict) or set(spec) != {"data_mode", "configuration_sha256", "outputs", "validation"}: raise ValueError(f"Invalid deployment data entry: {region_id}")
        if spec["data_mode"] != config["data_mode"] or spec["configuration_sha256"] != config_sha256(config): raise ValueError(f"Deployment data identity differs: {region_id}")
        outputs = spec.get("outputs")
        if not isinstance(outputs, dict) or set(outputs) != {layer["file"] for layer in config["layers"].values()}: raise ValueError(f"Deployment data outputs differ from layers: {region_id}")
        for relative, expected in outputs.items(): validate_file_record(expected, relative)
        validation = spec.get("validation")
        if not isinstance(validation, dict) or set(validation) != set(config["layers"]) or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in validation.values()): raise ValueError(f"Invalid validation counts: {region_id}")
    if data_root is not None:
        data_root = data_root.expanduser().absolute()
        if data_root.is_symlink() or not data_root.is_dir(): raise ValueError(f"Deployment data root must be a regular directory: {data_root}")
        data_root = data_root.resolve()
        for region_id, spec in data.items():
            region_data = data_root / region_id
            if region_data.is_symlink() or not region_data.is_dir(): raise ValueError(f"Missing or unsafe deployment region data directory: {region_data}")
            for relative, expected in spec["outputs"].items():
                nested = data_relative(relative); target = region_data / nested
                current = region_data; unsafe = False
                for part in nested.parts:
                    current = current / part
                    if current.is_symlink(): unsafe = True; break
                if unsafe or not target.is_file(): raise ValueError(f"Missing or unsafe deployment dataset: {target}")
                actual = {"path": relative, "bytes": target.stat().st_size, "sha256": sha256(target)}
                if actual != expected: raise ValueError(f"Deployment dataset checksum differs: {target}")
    print(json.dumps({"bundle": str(bundle), "regions": registry["regions"], "contract_files": len(files), "data_verified": data_root is not None}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-region", "add-layer", "attach-data", "accept-data", "detach-data", "build", "sync", "validate", "validate-all", "deployment-check", "bundle-build", "bundle-verify", "doctor", "promote", "self-test", "verify-core", "auth-set-user", "auth-delete-user", "auth-list-users", "contacts-collect", "contacts-publish", "contacts-status")); parser.add_argument("region", nargs="?")
    parser.add_argument("--title"); parser.add_argument("--center-lat", type=float); parser.add_argument("--center-lon", type=float); parser.add_argument("--data-mode", choices=("external", "pipeline"), default="external")
    parser.add_argument("--layer-id"); parser.add_argument("--file"); parser.add_argument("--label"); parser.add_argument("--geometry", action="append", default=[]); parser.add_argument("--renderer", choices=("auto", "points", "lines", "polygons", "choropleth", "density", "ranking"), default="auto"); parser.add_argument("--required", action="store_true")
    parser.add_argument("--data-dir", type=Path); parser.add_argument("--attach-mode", choices=("symlink", "junction", "copy"), default="symlink"); parser.add_argument("--allow-missing-data", action="store_true"); parser.add_argument("--write-core-lock", action="store_true"); parser.add_argument("--bundle-dir", type=Path)
    args = parser.parse_args()
    if args.command == "verify-core": verify_core(args.write_core_lock); return
    verify_core()
    if args.command == "auth-list-users":
        for username in auth.list_users(): print(username)
        return
    if args.command in {"auth-set-user", "auth-delete-user"}:
        if not args.region: parser.error("username is required")
        if args.command == "auth-set-user": auth.set_user_prompt(args.region)
        else: auth.delete_user(args.region)
        return
    if args.command in {"contacts-collect", "contacts-publish", "contacts-status"}:
        if not args.region: parser.error("region is required")
        from pipeline_core import contacts
        if args.command == "contacts-collect": result = contacts.collect(args.region)
        elif args.command == "contacts-publish": result = contacts.publish(args.region, "console-admin")
        else:
            root = region_dir(args.region); target = contacts.paths(root)
            result = read_json(target["state"]) if target["state"].is_file() else {"status": "never"}
        print(json.dumps(result, ensure_ascii=False, indent=2)); return
    if args.command == "self-test": subprocess.run([sys.executable, str(ROOT / "pipeline_core" / "selftest.py")], check=True); return
    if args.command == "validate-all": validate_all(args.allow_missing_data); return
    if args.command == "deployment-check": deployment_check(); return
    if args.command == "bundle-build":
        if args.bundle_dir is None: parser.error("bundle-build requires --bundle-dir")
        build_deployment_bundle(args.bundle_dir); return
    if args.command == "bundle-verify":
        if args.bundle_dir is None: parser.error("bundle-verify requires --bundle-dir")
        verify_deployment_bundle(args.bundle_dir, args.data_dir); return
    if not args.region: parser.error("region is required")
    if args.command == "init-region":
        if args.title is None or args.center_lat is None or args.center_lon is None: parser.error("init-region requires --title, --center-lat and --center-lon")
        init_region(args.region, args.title, args.center_lat, args.center_lon, args.data_mode)
    elif args.command == "add-layer":
        if not args.layer_id or not args.file or not args.label: parser.error("add-layer requires --layer-id, --file and --label")
        add_layer(args.region, args.layer_id, args.file, args.label, args.geometry or ["Point"], args.renderer, args.required)
    elif args.command == "attach-data":
        if args.data_dir is None: parser.error("attach-data requires --data-dir")
        attach_data(args.region, args.data_dir, args.attach_mode)
    elif args.command == "accept-data": accept_data(args.region)
    elif args.command == "detach-data": detach_data(args.region)
    elif args.command == "build": build(args.region)
    elif args.command == "sync": sync(args.region)
    elif args.command == "doctor": doctor(args.region)
    elif args.command == "promote": promote(args.region)
    else: inspect_region(args.region, allow_missing=args.allow_missing_data)


if __name__ == "__main__": main()
