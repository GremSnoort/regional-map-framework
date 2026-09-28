#!/usr/bin/env python3
"""Shared declarative runner for region-owned analytical build steps."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


def acquire_run_lock(path: Path):
    """Hold one byte of a regional lock file on Unix and Windows."""
    stream = path.open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if fcntl is not None:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except (BlockingIOError, OSError):
        stream.close()
        raise SystemExit(f"Another build is already running for {path.parent.parent.name}")
    return stream


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def record(path: Path, root: Path) -> dict:
    return {"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": digest(path)}


def atomic_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def safe_path(root: Path, relative: str, kind: str) -> Path:
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{kind} escapes regional directory: {relative}")
    return path


def locked_dependencies(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            raise ValueError(f"{path}:{number}: dependency must be exactly pinned")
        name, expected = line.split("==", 1)
        key = name.strip().lower().replace("_", "-")
        actual = importlib.metadata.version(key)
        if actual != expected.strip():
            raise ValueError(f"Dependency mismatch for {key}: locked={expected.strip()}, installed={actual}")
        result[key] = actual
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region-root", required=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--write-lock", action="store_true")
    args = parser.parse_args()
    region = Path(args.region_root).resolve()
    pipeline_dir = region / "pipeline"
    run_lock = acquire_run_lock(pipeline_dir / ".runner.lock")
    config_path = pipeline_dir / "pipeline.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported pipeline schema")
    if config.get("region_id") != region.name:
        raise ValueError("pipeline region_id does not match regional directory")
    if not isinstance(config.get("inputs", []), list) or not isinstance(config.get("steps", []), list):
        raise ValueError("pipeline inputs and steps must be arrays")
    if len(config.get("inputs", [])) != len(set(config.get("inputs", []))):
        raise ValueError("pipeline inputs must be unique")
    for index, step in enumerate(config.get("steps", [])):
        if not isinstance(step, dict) or not isinstance(step.get("name"), str) or not step["name"]:
            raise ValueError(f"pipeline step {index} must have a non-empty name")
        command = step.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError(f"pipeline step {index} must have a non-empty command")
        step_inputs = step.get("inputs", [])
        if not isinstance(step_inputs, list) or len(step_inputs) != len(set(step_inputs)) or not all(isinstance(item, str) and item for item in step_inputs):
            raise ValueError(f"pipeline step {index} inputs must be unique non-empty paths")
    dependency_path = safe_path(region, config["dependency_lock"], "dependency lock") if config.get("dependency_lock") else None
    dependencies = locked_dependencies(dependency_path)
    input_paths = set(config.get("inputs", [])) | {str(config_path.relative_to(region))}
    if dependency_path:
        input_paths.add(str(dependency_path.relative_to(region)))
    for step in config.get("steps", []):
        cwd = safe_path(region, step.get("cwd", "."), "step cwd")
        for declared in step.get("inputs", []):
            input_paths.add(str(safe_path(region, declared, "step input").relative_to(region)))
        for argument in step.get("command", []):
            candidate = (cwd / argument).resolve()
            if argument != "{python}" and candidate.is_file() and region in candidate.parents:
                input_paths.add(str(candidate.relative_to(region)))
        command = step.get("command", [])
        executables = [Path(item).name for item in command]
        if "normalize_layer.py" in executables:
            try:
                config_argument = command[command.index("--config") + 1]
            except (ValueError, IndexError) as error:
                raise ValueError("normalize_layer.py step must contain --config <regional-config>") from error
            adapter_config = safe_path(region, config_argument, "normalizer config")
            if not adapter_config.is_file():
                raise ValueError(f"Normalizer config is missing: {config_argument}")
            input_paths.add(str(adapter_config.relative_to(region)))
            adapter = json.loads(adapter_config.read_text(encoding="utf-8"))
            source_name = adapter.get("source")
            if not isinstance(source_name, str) or not source_name:
                raise ValueError(f"Normalizer config has no source: {config_argument}")
            source_path = safe_path(region, source_name, "normalizer source")
            input_paths.add(str(source_path.relative_to(region)))
    plugin_dir = region / "pipeline" / "plugins"
    if plugin_dir.is_dir():
        for plugin_path in sorted(plugin_dir.glob("*.json")):
            input_paths.add(str(plugin_path.relative_to(region)))
            plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
            declared = plugin.get("inputs", [])
            methodology = plugin.get("methodology")
            if not isinstance(declared, list) or not all(isinstance(item, str) and item for item in declared):
                raise ValueError(f"Analytical plugin inputs must be non-empty paths: {plugin_path}")
            if not isinstance(methodology, str) or not methodology:
                raise ValueError(f"Analytical plugin methodology is missing: {plugin_path}")
            for item in [*declared, methodology]:
                input_paths.add(str(safe_path(region, item, "analytical plugin input").relative_to(region)))
    inputs = [safe_path(region, item, "input") for item in sorted(input_paths)]
    output_names = config.get("outputs", [])
    output_root = (pipeline_dir / "outputs").resolve()
    if not output_names or any(not isinstance(item, str) for item in output_names):
        raise ValueError("Every pipeline output must be below pipeline/outputs/")
    if len(output_names) != len(set(output_names)):
        raise ValueError("pipeline outputs must be unique")
    outputs = [safe_path(region, item, "output") for item in output_names]
    if any(output_root not in path.parents for path in outputs):
        raise ValueError("Every resolved pipeline output must be below pipeline/outputs/")
    expected_collect_targets = {str(Path(item).relative_to("pipeline/outputs")) for item in output_names}
    collect_targets = set(config.get("collect", {}))
    if collect_targets and not collect_targets <= expected_collect_targets:
        raise ValueError("collect contains a target not declared in pipeline outputs")
    missing = [str(path) for path in inputs if not path.is_file()]
    if missing:
        raise ValueError("Missing regional inputs: " + ", ".join(missing))
    current = {str(path.relative_to(region)): record(path, region) for path in inputs}
    lock_path = pipeline_dir / config.get("lock_file", "inputs.lock.json")
    manifest_path = pipeline_dir / config.get("manifest_file", "build_manifest.json")
    if args.write_lock:
        atomic_json(lock_path, {"schema_version": 1, "inputs": current})
        if not args.validate_only:
            print("Regional input lock written")
            return
    locked = json.loads(lock_path.read_text(encoding="utf-8"))
    if locked.get("schema_version") != 1 or locked.get("inputs") != current:
        raise ValueError("Regional inputs differ from inputs.lock.json; review and use --write-lock")
    active_steps = [step.get("name", "unnamed") for step in config.get("steps", []) if args.full or not step.get("full_only")]
    if args.validate_only:
        absent = [str(path) for path in outputs if not path.is_file()]
        if absent:
            raise ValueError("Missing regional outputs: " + ", ".join(absent))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual_outputs = {str(path.relative_to(region)): record(path, region) for path in outputs}
        upstream = {}
        for name in config.get("upstream_manifests", []):
            path = safe_path(region, name, "upstream manifest")
            if not path.is_file():
                raise ValueError(f"Upstream manifest is missing: {name}")
            upstream[name] = record(path, region)
        if manifest.get("schema_version") != 1 or manifest.get("region_id") != config["region_id"] or manifest.get("inputs") != current or manifest.get("outputs") != actual_outputs or manifest.get("upstream_manifests", {}) != upstream or manifest.get("commands", []) != [step.get("command", []) for step in config.get("steps", []) if args.full or not step.get("full_only")]:
            raise ValueError("Regional pipeline manifest differs from current inputs or outputs")
        print(json.dumps({"region_id": config["region_id"], "outputs": len(outputs), "validated": True}, ensure_ascii=False))
        return
    if not args.validate_only:
        output_dir = pipeline_dir / "outputs"
        with tempfile.TemporaryDirectory(prefix=".regional-build-", dir=pipeline_dir) as temporary:
            staged = Path(temporary) / "outputs"
            if output_dir.exists() and not args.full:
                shutil.copytree(output_dir, staged)
            else:
                staged.mkdir()
            environment = os.environ.copy()
            environment.update({
                "ANALYTICAL_REGION_ROOT": str(region), "ANALYTICAL_PIPELINE_DIR": str(pipeline_dir),
                "ANALYTICAL_OUTPUT_DIR": str(staged),
            })
            for step in config.get("steps", []):
                if step.get("full_only") and not args.full:
                    continue
                command = step.get("command") or []
                if not command or not all(isinstance(item, str) and item for item in command):
                    raise ValueError(f"Invalid command in step {step.get('name', '<unnamed>')}")
                argv = [sys.executable if item == "{python}" else item for item in command]
                cwd = safe_path(region, step.get("cwd", "."), "step cwd")
                step_env = environment | {str(key): str(value) for key, value in step.get("env", {}).items()}
                print(f"RUN {step.get('name', argv[0])}", flush=True)
                subprocess.run(argv, cwd=cwd, env=step_env, check=True)
            for target_name, source_name in config.get("collect", {}).items():
                target_relative = Path(target_name)
                target = (staged / target_relative).resolve()
                if target_relative.is_absolute() or ".." in target_relative.parts or staged.resolve() not in target.parents:
                    raise ValueError(f"Collected target escapes staging outputs: {target_name}")
                source = safe_path(region, source_name, "collected source")
                if not source.is_file():
                    raise ValueError(f"Collected source is missing: {source_name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            staged_outputs = [staged / Path(item).relative_to("pipeline/outputs") for item in output_names]
            absent = [str(path) for path in staged_outputs if not path.is_file()]
            if absent:
                raise ValueError("Missing staged regional outputs: " + ", ".join(absent))
            output_records = {}
            for name, path in zip(output_names, staged_outputs):
                output_records[name] = {"path": name, "bytes": path.stat().st_size, "sha256": digest(path)}
            manifest = {
                "schema_version": 1,
                "region_id": config["region_id"],
                "scope": "full" if args.full else "standard",
                "finished_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "python": sys.version.split()[0], "dependencies": dependencies,
                "inputs": current, "outputs": output_records, "steps": active_steps,
                "commands": [step.get("command", []) for step in config.get("steps", []) if args.full or not step.get("full_only")],
                "upstream_manifests": {},
            }
            for name in config.get("upstream_manifests", []):
                path = safe_path(region, name, "upstream manifest")
                if not path.is_file():
                    raise ValueError(f"Upstream manifest is missing: {name}")
                manifest["upstream_manifests"][name] = record(path, region)
            backup = pipeline_dir / ".outputs.backup"
            previous_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
            if backup.exists():
                shutil.rmtree(backup)
            try:
                if output_dir.exists():
                    os.replace(output_dir, backup)
                os.replace(staged, output_dir)
                atomic_json(manifest_path, manifest)
            except BaseException:
                if output_dir.exists():
                    shutil.rmtree(output_dir)
                if backup.exists():
                    os.replace(backup, output_dir)
                if previous_manifest is None:
                    manifest_path.unlink(missing_ok=True)
                else:
                    temporary_manifest = pipeline_dir / ".manifest.rollback"
                    temporary_manifest.write_bytes(previous_manifest)
                    os.replace(temporary_manifest, manifest_path)
                raise
            if backup.exists():
                shutil.rmtree(backup)
    print(json.dumps({"region_id": config["region_id"], "outputs": len(outputs)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
