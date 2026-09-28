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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGIONS = ROOT / "regions"
REGISTRY = ROOT / "registry.json"
CORE_LOCK = ROOT / "core.lock.json"
CORE_VERSION = "2.1.0"
GEOMETRY_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
CORE_FILES = (
    "index.html", "manage.py", "REGION_CONTRACT.md", "core/map.js", "core/map.css",
    "pipeline_core/runner.py", "pipeline_core/build_roads.py", "pipeline_core/build_adaptive_density.py", "pipeline_core/normalize_layer.py",
    "pipeline_core/requirements-lock.txt", "pipeline_core/requirements-minimal-lock.txt",
    "pipeline_core/selftest.py", "schemas/region.schema.json", "schemas/pipeline.schema.json",
    "schemas/analytics-plugin.schema.json",
    "templates/layer.example.json", "templates/region.example.json",
    "templates/standard_layer.example.json", "templates/analytics-plugin.example.json",
    "templates/adaptive-density.example.json",
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
        style = spec.get("style", {})
        for key in ("fill_max_zoom", "fill_opacity", "fill_opacity_above_max"):
            if key in style and (not isinstance(style[key], (int, float)) or isinstance(style[key], bool)):
                raise ValueError(f"Layer {layer_id}: style.{key} must be numeric")
        for key in ("fill_opacity", "fill_opacity_above_max"):
            if key in style and not 0 <= style[key] <= 1:
                raise ValueError(f"Layer {layer_id}: style.{key} must be between 0 and 1")
    if len(files) != len(set(files)):
        raise ValueError("Every layer must use a unique file")


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


def runtime_manifest(root: Path) -> Path:
    return root / ".runtime" / "publication.json"


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
    root = region_dir(region_id); config = read_json(root / "region.json"); validate_config(config, region_id)
    counts, records, missing = layer_records(root, config)
    if missing and not allow_missing:
        raise ValueError(f"Missing data files: {missing}. Attach data or build the pipeline first.")
    expected = config.get("expected") or {}
    differences = {key: {"expected": value, "actual": counts.get(key)} for key, value in expected.items() if counts.get(key) != value}
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
    registry = read_json(REGISTRY); registry["regions"] = sorted(set(registry.get("regions", [])) | {region_id}); registry["default_region"] = registry.get("default_region") or region_id; atomic_json(REGISTRY, registry)
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
    for item in read_json(REGISTRY).get("regions", []): inspect_region(item, allow_missing=allow_missing, require_provenance=not allow_missing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init-region", "add-layer", "attach-data", "accept-data", "detach-data", "build", "sync", "validate", "validate-all", "doctor", "promote", "self-test", "verify-core")); parser.add_argument("region", nargs="?")
    parser.add_argument("--title"); parser.add_argument("--center-lat", type=float); parser.add_argument("--center-lon", type=float); parser.add_argument("--data-mode", choices=("external", "pipeline"), default="external")
    parser.add_argument("--layer-id"); parser.add_argument("--file"); parser.add_argument("--label"); parser.add_argument("--geometry", action="append", default=[]); parser.add_argument("--renderer", choices=("auto", "points", "lines", "polygons", "choropleth", "density", "ranking"), default="auto"); parser.add_argument("--required", action="store_true")
    parser.add_argument("--data-dir", type=Path); parser.add_argument("--attach-mode", choices=("symlink", "junction", "copy"), default="symlink"); parser.add_argument("--allow-missing-data", action="store_true"); parser.add_argument("--write-core-lock", action="store_true")
    args = parser.parse_args()
    if args.command == "verify-core": verify_core(args.write_core_lock); return
    verify_core()
    if args.command == "self-test": subprocess.run([sys.executable, str(ROOT / "pipeline_core" / "selftest.py")], check=True); return
    if args.command == "validate-all": validate_all(args.allow_missing_data); return
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
