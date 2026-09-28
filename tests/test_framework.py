import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import manage


class FrameworkTest(unittest.TestCase):
    def test_core_files_exist(self):
        self.assertTrue(manage.core_records())

    def test_generic_point_layer_validation(self):
        spec = {"geometry_types": ["Point"], "required_properties": ["name"], "numeric_properties": ["score"]}
        payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.62, 55.75]}, "properties": {"name": "demo", "score": 1}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "objects.geojson"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(manage.validate_geojson(path, "objects", spec), 1)

    def test_path_ids_reject_traversal(self):
        with self.assertRaises(ValueError):
            manage.safe_id("../region")

    def test_region_can_attach_external_data(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            regions = temporary / "regions"
            registry = temporary / "registry.json"
            registry.write_text('{"schema_version":1,"default_region":null,"regions":[]}', encoding="utf-8")
            with patch.object(manage, "REGIONS", regions), patch.object(manage, "REGISTRY", registry):
                manage.init_region("demo", "Demo", 55.75, 37.62, "external")
                manage.add_layer("demo", "objects", "objects.geojson", "Objects", ["Point"], "points", True)
                data = temporary / "external-data"
                data.mkdir()
                payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.62, 55.75]}, "properties": {"name": "demo"}}]}
                (data / "objects.geojson").write_text(json.dumps(payload), encoding="utf-8")
                manage.attach_data("demo", data, "symlink")
                result = manage.inspect_region("demo")
                self.assertEqual(result["layers"], {"objects": 1})
                payload["features"][0]["properties"]["name"] = "changed"
                (data / "objects.geojson").write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    manage.inspect_region("demo")
                manage.accept_data("demo")
                invalid = temporary / "invalid-data"
                invalid.mkdir()
                with self.assertRaises(ValueError):
                    manage.attach_data("demo", invalid, "symlink")
                self.assertEqual((regions / "demo" / "data").resolve(), data.resolve())
                manage.inspect_region("demo")
                config_path = regions / "demo" / "region.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["source_note"] = "Проверенный синтетический источник для интеграционного теста."
                config_path.write_text(json.dumps(config), encoding="utf-8")
                (regions / "demo" / "SOURCES.md").write_text("# Test source\n\nSynthetic public-domain fixture. Snapshot: 2026-01-01. Licence: CC0. SHA-256 is controlled by publication manifest. Transformation: none. Quality: verified for integration testing.\n", encoding="utf-8")
                manage.accept_data("demo")
                manage.promote("demo")
                self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["lifecycle"], "production")
                manage.detach_data("demo")
                self.assertTrue(data.is_dir())

    def test_nested_pipeline_output_mapping(self):
        spec = {"file": "data/category/objects.geojson"}
        self.assertEqual(manage.output_for(spec), "pipeline/outputs/category/objects.geojson")

    def test_frontend_has_generic_advanced_renderers(self):
        source = (manage.ROOT / "core" / "map.js").read_text(encoding="utf-8")
        for contract in ("L.canvas(", "buildTable", "relatedBounds", "syncZoomLayers", "animate:false"):
            self.assertIn(contract, source)

    def test_bins_must_be_ordered(self):
        config = json.loads((manage.ROOT / "templates" / "region.example.json").read_text(encoding="utf-8"))
        config["region_id"] = "demo"
        config["layers"] = {"objects": {"file": "data/objects.geojson", "label": "Objects", "value_field": "score", "bins": [{"max": 10}, {"max": 5}]}}
        with self.assertRaises(ValueError):
            manage.validate_config(config, "demo")

    def test_analytical_plugin_contract_is_bound_to_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pipeline" / "plugins").mkdir(parents=True)
            (root / "sources").mkdir()
            (root / "sources" / "objects.geojson").write_text("{}", encoding="utf-8")
            (root / "METHOD.md").write_text("# Method\n", encoding="utf-8")
            plugin = {"plugin_id": "score", "version": "1.0.0", "description": "Score model", "inputs": ["sources/objects.geojson"], "output": "pipeline/outputs/score.geojson", "methodology": "METHOD.md", "parameters": {}, "quality_controls": {}}
            path = root / "pipeline" / "plugins" / "score.json"
            path.write_text(json.dumps(plugin), encoding="utf-8")
            self.assertEqual(manage.validate_plugins(root, {"outputs": [plugin["output"]]}), 1)
            with self.assertRaises(ValueError):
                manage.validate_plugins(root, {"outputs": []})

    def test_pipeline_publication_is_manifest_bound_and_nested(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            regions, registry = temporary / "regions", temporary / "registry.json"
            registry.write_text('{"schema_version":1,"default_region":null,"regions":[]}', encoding="utf-8")
            with patch.object(manage, "REGIONS", regions), patch.object(manage, "REGISTRY", registry):
                manage.init_region("demo", "Demo", 55.75, 37.62, "pipeline")
                manage.add_layer("demo", "objects", "category/objects.geojson", "Objects", ["Point"], "points", True)
                root = regions / "demo"
                script = root / "build_layer.py"
                script.write_text("""import json,os\nfrom pathlib import Path\nout=Path(os.environ['ANALYTICAL_OUTPUT_DIR'])/'category'/'objects.geojson'\nout.parent.mkdir(parents=True,exist_ok=True)\nout.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Point','coordinates':[37.62,55.75]},'properties':{'name':'demo'}}]}))\n""", encoding="utf-8")
                pipeline_path = root / "pipeline" / "pipeline.json"
                (root / "sources" / "plugin-input.txt").write_text("input", encoding="utf-8")
                (root / "METHOD.md").write_text("# Method\n", encoding="utf-8")
                plugin = {"plugin_id": "objects", "version": "1.0.0", "description": "Test model", "inputs": ["sources/plugin-input.txt"], "output": "pipeline/outputs/category/objects.geojson", "methodology": "METHOD.md", "parameters": {}, "quality_controls": {}}
                (root / "pipeline" / "plugins" / "objects.json").write_text(json.dumps(plugin), encoding="utf-8")
                pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
                pipeline["inputs"] = ["build_layer.py"]
                pipeline["steps"] = [{"name": "build", "command": ["{python}", "build_layer.py"]}]
                pipeline_path.write_text(json.dumps(pipeline), encoding="utf-8")
                runner = manage.ROOT / "pipeline_core" / "runner.py"
                subprocess.run([sys.executable, str(runner), "--region-root", str(root), "--write-lock"], check=True)
                locked = json.loads((root / "pipeline" / "inputs.lock.json").read_text(encoding="utf-8"))["inputs"]
                for name in ("pipeline/plugins/objects.json", "sources/plugin-input.txt", "METHOD.md"):
                    self.assertIn(name, locked)
                manage.build("demo")
                result = manage.inspect_region("demo")
                self.assertEqual(result["layers"], {"objects": 1})
                self.assertTrue((root / "data" / "category" / "objects.geojson").is_file())
                manifest = json.loads(manage.runtime_manifest(root).read_text(encoding="utf-8"))
                self.assertIn("upstream_manifest", manifest)


if __name__ == "__main__":
    unittest.main()
