import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import auth
import manage
import serve
from pipeline_core import contacts, regeneration
from pipeline_core.build_adaptive_density import adaptive_rows


class FrameworkTest(unittest.TestCase):
    def test_core_files_exist(self):
        self.assertTrue(manage.core_records())

    def test_production_deployment_templates_are_safe_by_default(self):
        env = (manage.ROOT / "deploy/regional-map-framework.env.example").read_text(encoding="utf-8")
        unit = (manage.ROOT / "deploy/systemd/regional-map-framework.service").read_text(encoding="utf-8")
        nginx = (manage.ROOT / "deploy/nginx/regional-map-framework.conf").read_text(encoding="utf-8")
        workflow = (manage.ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        deploy = (manage.ROOT / "deploy/server/rmf-deploy").read_text(encoding="utf-8")
        gateway = (manage.ROOT / "deploy/server/rmf-deploy-gateway").read_text(encoding="utf-8")
        self.assertIn("RMF_COOKIE_SECURE=1", env)
        self.assertIn("RMF_CONTENT_ROOT=/srv/regional-map-deployment", env)
        self.assertIn("RMF_RUNTIME_ROOT=/var/lib/regional-map-framework/regions", env)
        self.assertIn("RMF_ALLOW_REGENERATION=0", env)
        self.assertIn("RMF_ALLOW_CONTACT_COLLECTION=0", env)
        self.assertIn("RMF_ADMIN_USERS=map_admin", env)
        self.assertIn("--bind 127.0.0.1", unit)
        self.assertIn("ExecStartPre=/opt/regional-map-framework/current/.venv/bin/python /opt/regional-map-framework/current/manage.py deployment-check", unit)
        self.assertIn("ExecStart=/opt/regional-map-framework/current/.venv/bin/python", unit)
        self.assertNotIn("ExecStartPre=+", unit)
        self.assertIn("User=regional-map", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("proxy_pass http://127.0.0.1:8000", nginx)
        self.assertIn("node --check core/gallery.js", workflow)
        self.assertIn("node --check core/contacts.js", workflow)
        self.assertIn("node --check core/map.js", workflow)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr", nginx)
        self.assertNotRegex(nginx, r"(?m)^\s*(root|alias|try_files)\s")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+(push|pull_request):")
        self.assertIn("github.actor == 'GremSnoort'", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@v\d")
        self.assertIn("merge-base --is-ancestor", deploy)
        self.assertIn("deployment-check", deploy)
        self.assertIn("rolling back", deploy)
        self.assertIn("runuser -u regional-map", deploy)
        self.assertIn("u=rwX,go=rX", deploy)
        self.assertIn("--only-binary=:all:", deploy)
        self.assertIn('PYTHONPATH="$target"', deploy)
        self.assertIn('"$release_python" -m unittest discover -s "$target/tests"', deploy)
        self.assertNotIn('-t "$target"', deploy)
        self.assertIn("SSH_ORIGINAL_COMMAND", gateway)
        if os.name != "nt":
            self.assertTrue(os.access(manage.ROOT / "deploy/server/rmf-deploy", os.X_OK))
            self.assertTrue(os.access(manage.ROOT / "deploy/server/rmf-deploy-gateway", os.X_OK))
            subprocess.run(["bash", "-n", str(manage.ROOT / "deploy/server/rmf-deploy")], check=True)
            subprocess.run(["bash", "-n", str(manage.ROOT / "deploy/server/rmf-deploy-gateway")], check=True)

    def test_server_can_read_content_outside_the_code_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            content = temporary / "content"
            (content / "regions/demo/data").mkdir(parents=True)
            (content / "registry.json").write_text(json.dumps({"schema_version": 1, "default_region": "demo", "regions": ["demo"]}), encoding="utf-8")
            (content / "regions/demo/region.json").write_text(json.dumps({"region_id": "demo", "layers": {"objects": {"file": "data/objects.geojson"}}}), encoding="utf-8")
            (content / "regions/demo/data/objects.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
            with patch.dict(os.environ, {"RMF_CONTENT_ROOT": str(content)}):
                self.assertEqual(serve.public_file("/registry.json"), (content / "registry.json").resolve())
                self.assertEqual(serve.public_file("/regions/demo/region.json"), (content / "regions/demo/region.json").resolve())
                self.assertEqual(serve.public_file("/regions/demo/data/objects.geojson"), (content / "regions/demo/data/objects.geojson").resolve())

    def test_runtime_state_can_live_outside_region_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory); region = temporary / "content/regions/demo"
            external_runtime = temporary / "private-runtime"
            with patch.object(manage, "RUNTIME_ROOT", external_runtime):
                self.assertEqual(manage.runtime_manifest(region), external_runtime / "demo/publication.json")
            with patch.dict(os.environ, {"RMF_RUNTIME_ROOT": str(external_runtime)}):
                self.assertEqual(serve.runtime_dir(region), external_runtime / "demo")

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

    def test_registry_and_deployment_preflight_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory); regions = temporary / "regions"; regions.mkdir()
            registry = temporary / "registry.json"
            with patch.object(manage, "REGIONS", regions), patch.object(manage, "REGISTRY", registry), patch.object(manage, "CONTENT_ROOT", temporary):
                registry.write_text(json.dumps({"schema_version": 1, "default_region": "missing", "regions": []}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "default_region"):
                    manage.validate_registry()
                registry.write_text(json.dumps({"schema_version": 1, "default_region": None, "regions": []}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "at least one region"):
                    manage.deployment_check()
                if os.name != "nt":
                    target = temporary / "actual-registry.json"
                    target.write_text(json.dumps({"schema_version": 1, "default_region": None, "regions": []}), encoding="utf-8")
                    registry.unlink(); registry.symlink_to(target)
                    with self.assertRaisesRegex(ValueError, "not a symlink"):
                        manage.validate_registry()

    def test_deployment_bundle_rejects_unsafe_metadata_and_contract_symlinks(self):
        with self.assertRaisesRegex(ValueError, "Invalid file metadata"):
            manage.validate_file_record({"path": "content/file", "bytes": True, "sha256": "0" * 64}, "content/file")
        if os.name != "nt":
            with tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory); region = temporary / "region"; outside = temporary / "outside"
                region.mkdir(); outside.mkdir()
                (region / "region.json").write_text("{}", encoding="utf-8")
                (region / "SOURCES.md").write_text("source", encoding="utf-8")
                (outside / "secret.env").write_text("SECRET=not-for-deployment", encoding="utf-8")
                (region / "pipeline").symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "directory must not be a symlink"):
                    manage.deployment_contract_paths(region)

    def test_allow_missing_data_checks_only_available_layer_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            registry = temporary / "registry.json"
            registry.write_text('{"schema_version":1,"default_region":null,"regions":[]}', encoding="utf-8")
            with patch.object(manage, "REGIONS", temporary / "regions"), patch.object(manage, "REGISTRY", registry):
                manage.init_region("demo", "Demo", 55.75, 37.62, "external")
                for layer in ("objects", "missing"):
                    manage.add_layer("demo", layer, f"{layer}.geojson", layer, ["Point"], "points", True)
                root = temporary / "regions/demo"
                config = manage.read_json(root / "region.json")
                config["expected"] = {"objects": 1, "missing": 2}
                manage.atomic_json(root / "region.json", config)
                manage.validate_all(True)
                with self.assertRaisesRegex(ValueError, "Missing data files"):
                    manage.inspect_region("demo", emit=False)
                payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.62, 55.75]}, "properties": {"name": "demo"}}]}
                manage.atomic_json(root / "data/objects.geojson", payload)
                result = manage.inspect_region("demo", allow_missing=True, require_provenance=False, emit=False)
                self.assertEqual(result["layers"], {"objects": 1})
                self.assertEqual(result["missing"], ["data/missing.geojson"])
                config["expected"]["objects"] = 2
                manage.atomic_json(root / "region.json", config)
                with self.assertRaisesRegex(ValueError, "Layer counts differ"):
                    manage.validate_all(True)
                config["expected"]["objects"] = 0
                manage.atomic_json(root / "data/objects.geojson", {"type": "FeatureCollection", "features": []})
                manage.atomic_json(root / "region.json", config)
                self.assertEqual(manage.inspect_region("demo", allow_missing=True, require_provenance=False, emit=False)["layers"], {"objects": 0})
                config["expected"]["objects"] = 1
                manage.atomic_json(root / "region.json", config)
                with self.assertRaisesRegex(ValueError, "Layer counts differ"):
                    manage.validate_all(True)

    def test_allow_missing_data_keeps_production_and_expected_ids_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            registry = temporary / "registry.json"
            registry.write_text('{"schema_version":1,"default_region":null,"regions":[]}', encoding="utf-8")
            with patch.object(manage, "REGIONS", temporary / "regions"), patch.object(manage, "REGISTRY", registry):
                manage.init_region("demo", "Demo", 55.75, 37.62, "external")
                manage.add_layer("demo", "objects", "objects.geojson", "Objects", ["Point"], "points", True)
                root = temporary / "regions/demo"
                config = manage.read_json(root / "region.json")
                config["expected"] = {"objects": 1, "unknown": 1}
                manage.atomic_json(root / "region.json", config)
                with self.assertRaisesRegex(ValueError, "Layer counts differ"):
                    manage.validate_all(True)
                del config["expected"]["unknown"]
                config["lifecycle"] = "production"
                manage.atomic_json(root / "region.json", config)
                with self.assertRaisesRegex(ValueError, "Production package is incomplete"):
                    manage.validate_all(True)

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
                manage.deployment_check()
                bundle = temporary / "deployment-bundle"
                manage.build_deployment_bundle(bundle)
                self.assertTrue((bundle / "deployment-manifest.json").is_file())
                self.assertFalse(any((bundle / "content").rglob("*.geojson")))
                deployment_data = temporary / "deployment-data" / "demo"
                shutil.copytree(data, deployment_data)
                manage.verify_deployment_bundle(bundle, temporary / "deployment-data")
                installed = temporary / "installed-content"
                shutil.copytree(bundle / "content", installed)
                installed_runtime = temporary / "installed-runtime"
                with patch.object(manage, "CONTENT_ROOT", installed), patch.object(manage, "REGIONS", installed / "regions"), patch.object(manage, "REGISTRY", installed / "registry.json"), patch.object(manage, "RUNTIME_ROOT", installed_runtime):
                    manage.attach_data("demo", deployment_data, "symlink")
                    manage.deployment_check()
                draft_bundle = temporary / "draft-bundle"
                shutil.copytree(bundle, draft_bundle)
                draft_path = draft_bundle / "content/regions/demo/region.json"
                draft = json.loads(draft_path.read_text(encoding="utf-8")); draft["lifecycle"] = "draft"
                draft_path.write_text(json.dumps(draft), encoding="utf-8")
                draft_manifest_path = draft_bundle / "deployment-manifest.json"
                draft_manifest = json.loads(draft_manifest_path.read_text(encoding="utf-8"))
                relative = "content/regions/demo/region.json"
                draft_manifest["files"][relative] = manage.record(draft_path, draft_bundle)
                draft_manifest_path.write_text(json.dumps(draft_manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "lifecycle is not production"):
                    manage.verify_deployment_bundle(draft_bundle)
                invalid_counts_bundle = temporary / "invalid-counts-bundle"
                shutil.copytree(bundle, invalid_counts_bundle)
                invalid_counts_path = invalid_counts_bundle / "deployment-manifest.json"
                invalid_counts = json.loads(invalid_counts_path.read_text(encoding="utf-8"))
                invalid_counts["data"]["demo"]["validation"]["unexpected_layer"] = 1
                invalid_counts_path.write_text(json.dumps(invalid_counts), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Invalid validation counts"):
                    manage.verify_deployment_bundle(invalid_counts_bundle)
                if os.name != "nt":
                    linked_data = temporary / "linked-deployment-data"
                    linked_data.symlink_to(temporary / "deployment-data", target_is_directory=True)
                    with self.assertRaisesRegex(ValueError, "data root must be a regular directory"):
                        manage.verify_deployment_bundle(bundle, linked_data)
                (bundle / "content/regions/demo/SOURCES.md").write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "checksum differs"):
                    manage.verify_deployment_bundle(bundle)
                manage.detach_data("demo")
                self.assertTrue(data.is_dir())

    def test_nested_pipeline_output_mapping(self):
        spec = {"file": "data/category/objects.geojson"}
        self.assertEqual(manage.output_for(spec), "pipeline/outputs/category/objects.geojson")

    def test_auth_passwords_sessions_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RMF_SESSION_SECONDS": "3600"}):
            path = Path(directory) / "users.json"
            auth.set_user("map_user", "a-long-test-password", path)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("a-long-test-password", raw)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(auth.verify_password("map_user", "a-long-test-password", path))
            self.assertFalse(auth.verify_password("map_user", "wrong-password", path))
            self.assertFalse(auth.verify_password("missing", "a-long-test-password", path))
            token = auth.create_session("map_user", path, now=1000)
            self.assertEqual(auth.verify_session(token, path, now=1100), "map_user")
            self.assertIsNone(auth.verify_session(token + "x", path, now=1100))
            self.assertIsNone(auth.verify_session(token, path, now=5000))
            auth.set_user("map_user", "another-long-password", path)
            self.assertIsNone(auth.verify_session(token, path, now=1100))

    def test_http_auth_protects_static_files_and_api(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RMF_AUTH_FILE": str(Path(directory) / "users.json"), "RMF_COOKIE_SECURE": "1"}, clear=False):
            auth.set_user("map_user", "http-test-password", Path(directory) / "users.json")
            serve.FAILURES.clear()
            server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            opener = urllib.request.build_opener(NoRedirect); base = f"http://127.0.0.1:{server.server_address[1]}"
            def request(path, method="GET", data=None, cookie=None, origin=None):
                headers = {}
                if cookie: headers["Cookie"] = cookie
                if origin: headers["Origin"] = origin
                if data is not None:
                    data = urllib.parse.urlencode(data).encode(); headers["Content-Type"] = "application/x-www-form-urlencoded"
                item = urllib.request.Request(base + path, method=method, data=data, headers=headers)
                try:
                    with opener.open(item, timeout=5) as response: return response.status, dict(response.headers), response.read()
                except urllib.error.HTTPError as error:
                    return error.code, dict(error.headers), error.read()
            try:
                health = request("/healthz", method="HEAD")
                self.assertEqual(health[0], 200)
                self.assertEqual(health[2], b"")
                self.assertEqual(request("/", cookie="x@=y")[0], 303)
                anonymous = request("/registry.json")
                self.assertEqual(anonymous[0], 303)
                self.assertTrue(anonymous[1]["Location"].startswith("/login?next="))
                self.assertEqual(request("/api/session")[0], 401)
                self.assertEqual(request("/auth/login", "POST", {"username": "map_user", "password": "http-test-password"}, origin="https://evil.example")[0], 403)
                login = request("/auth/login", "POST", {"username": "map_user", "password": "http-test-password", "next": "/?region=demo"})
                self.assertEqual(login[0], 303)
                cookie_header = login[1]["Set-Cookie"]
                for flag in ("HttpOnly", "SameSite=Strict", "Secure"):
                    self.assertIn(flag, cookie_header)
                self.assertNotIn("Python", login[1]["Server"])
                self.assertIn("private", login[1]["Cache-Control"])
                self.assertIn("default-src 'self'", login[1]["Content-Security-Policy"])
                self.assertIn("worker-src 'self' blob: https://unpkg.com", login[1]["Content-Security-Policy"])
                self.assertEqual(login[1]["Referrer-Policy"], "strict-origin-when-cross-origin")
                cookie = cookie_header.split(";", 1)[0]
                session = request("/api/session", cookie=cookie)
                self.assertEqual(json.loads(session[2]), {"username": "map_user", "is_admin": False})
                logout = request("/auth/logout", "POST", {}, cookie=cookie)
                self.assertIn("Max-Age=0", logout[1]["Set-Cookie"])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_contact_http_api_separates_readers_and_administrators(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory); content = temporary / "content"; root = content / "regions/demo"; runtime = temporary / "runtime"; credentials = temporary / "users.json"
            (root / "contacts/seeds").mkdir(parents=True); (content / "registry.json").write_text(json.dumps({"schema_version": 1, "default_region": "demo", "regions": ["demo"]}), encoding="utf-8")
            (root / "region.json").write_text(json.dumps({"region_id": "demo"}), encoding="utf-8")
            spec = {"schema_version": 1, "region_id": "demo", "enabled": True, "min_interval_seconds": 3600, "request_delay_seconds": 1, "sources": [{"source_id": "seed", "type": "local_json", "path": "contacts/seeds/contacts.json"}]}
            (root / "contacts/sources.json").write_text(json.dumps(spec), encoding="utf-8"); (root / "contacts/seeds/contacts.json").write_text(json.dumps({"contacts": [{"name": "Agency", "kind": "agency", "phones": ["+79990000000"]}]}), encoding="utf-8")
            auth.set_user("map_admin", "admin-test-password", credentials); auth.set_user("map_viewer", "viewer-test-password", credentials)
            environment = {"RMF_AUTH_FILE": str(credentials), "RMF_COOKIE_SECURE": "1", "RMF_CONTENT_ROOT": str(content), "RMF_RUNTIME_ROOT": str(runtime), "RMF_ADMIN_USERS": "map_admin", "RMF_ALLOW_CONTACT_COLLECTION": "0"}
            with patch.dict(os.environ, environment, clear=False), patch.object(manage, "REGIONS", content / "regions"), patch.object(manage, "RUNTIME_ROOT", runtime):
                contacts.collect("demo"); contacts.publish("demo", "map_admin")
                server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); opener = urllib.request.build_opener(NoRedirect); base = f"http://127.0.0.1:{server.server_address[1]}"
                def login(username, password):
                    data = urllib.parse.urlencode({"username": username, "password": password}).encode(); request = urllib.request.Request(base + "/auth/login", method="POST", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
                    try: opener.open(request, timeout=5)
                    except urllib.error.HTTPError as error: return error.headers["Set-Cookie"].split(";", 1)[0]
                def request(path, cookie, method="GET"):
                    item = urllib.request.Request(base + path, method=method, headers={"Cookie": cookie})
                    try:
                        with opener.open(item, timeout=5) as response: return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as error: return error.code, json.loads(error.read())
                try:
                    viewer, admin = login("map_viewer", "viewer-test-password"), login("map_admin", "admin-test-password")
                    public = request("/api/contacts?region=demo", viewer); self.assertEqual(public[0], 200); self.assertNotIn("published_by", public[1]); self.assertEqual(len(public[1]["contacts"]), 1)
                    self.assertEqual(request("/api/contacts/status?region=demo", viewer)[0], 403); self.assertEqual(request("/api/contacts/publish?region=demo", viewer, "POST")[0], 403)
                    self.assertEqual(request("/api/contacts/status?region=demo", admin)[0], 200); self.assertEqual(request("/api/contacts/collect?region=demo", admin, "POST")[0], 403)
                finally: server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_http_static_allowlist_blocks_project_files(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RMF_AUTH_FILE": str(Path(directory) / "users.json"), "RMF_COOKIE_SECURE": "1"}, clear=False):
            temporary = Path(directory)
            project = temporary / "project"
            (project / "core").mkdir(parents=True)
            (project / "regions/demo/data").mkdir(parents=True)
            (project / "regions/unregistered/data").mkdir(parents=True)
            (project / ".runtime").mkdir()
            (project / ".git").mkdir()
            (project / "index.html").write_text("index", encoding="utf-8")
            (project / "map.html").write_text("map page", encoding="utf-8")
            (project / "contacts.html").write_text("contacts page", encoding="utf-8")
            (project / "core/gallery.js").write_text("gallery", encoding="utf-8")
            (project / "core/gallery.css").write_text("gallery css", encoding="utf-8")
            (project / "core/map.js").write_text("map", encoding="utf-8")
            (project / "core/map.css").write_text("css", encoding="utf-8")
            (project / "core/table-export.js").write_text("export module", encoding="utf-8")
            (project / "core/contacts.js").write_text("contacts", encoding="utf-8")
            (project / "core/contacts.css").write_text("contacts css", encoding="utf-8")
            registered = ["demo"]
            (project / "registry.json").write_text(json.dumps({"schema_version": 1, "default_region": "demo", "regions": registered}), encoding="utf-8")
            region = {"region_id": "demo", "layers": {"objects": {"file": "data/objects.geojson"}}}
            (project / "regions/demo/region.json").write_text(json.dumps(region), encoding="utf-8")
            (project / "regions/demo/data/objects.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
            (project / "regions/demo/data/undeclared.geojson").write_text("secret", encoding="utf-8")
            (project / "regions/demo/SOURCES.md").write_text("private provenance", encoding="utf-8")
            (project / "regions/unregistered/region.json").write_text(json.dumps(region), encoding="utf-8")
            (project / "regions/unregistered/pipeline").mkdir()
            unregistered_regeneration = {"schema_version": 1, "region_id": "unregistered", "enabled": True, "min_interval_seconds": 300, "outputs": ["objects.geojson"], "commands": [{"command": ["true"]}]}
            (project / "regions/unregistered/pipeline/regeneration.json").write_text(json.dumps(unregistered_regeneration), encoding="utf-8")
            (project / ".runtime/users.json").write_text("credential material", encoding="utf-8")
            (project / ".git/config").write_text("git secrets", encoding="utf-8")
            (project / "serve.py").write_text("server source", encoding="utf-8")
            if os.name != "nt":
                external_data = temporary / "external-data"
                external_data.mkdir()
                (external_data / "objects.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
                (project / "regions/linked").mkdir()
                (project / "regions/linked/data").symlink_to(external_data, target_is_directory=True)
                linked_region = {"region_id": "linked", "layers": {"objects": {"file": "data/objects.geojson"}}}
                (project / "regions/linked/region.json").write_text(json.dumps(linked_region), encoding="utf-8")
                registered.append("linked")
                (project / "registry.json").write_text(json.dumps({"schema_version": 1, "default_region": "demo", "regions": registered}), encoding="utf-8")
                region["layers"]["leak"] = {"file": "data/leak.geojson"}
                (project / "regions/demo/region.json").write_text(json.dumps(region), encoding="utf-8")
                (project / "regions/demo/data/leak.geojson").symlink_to(project / ".runtime/users.json")
            auth.set_user("map_user", "http-test-password", temporary / "users.json")
            serve.FAILURES.clear()

            with patch.object(serve, "ROOT", project):
                server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                opener = urllib.request.build_opener(NoRedirect)
                base = f"http://127.0.0.1:{server.server_address[1]}"

                def request(path, method="GET", cookie=None):
                    headers = {"Cookie": cookie} if cookie else {}
                    item = urllib.request.Request(base + path, method=method, headers=headers)
                    try:
                        with opener.open(item, timeout=5) as response:
                            return response.status, dict(response.headers), response.read()
                    except urllib.error.HTTPError as error:
                        return error.code, dict(error.headers), error.read()

                try:
                    login_data = urllib.parse.urlencode({"username": "map_user", "password": "http-test-password"}).encode()
                    login_request = urllib.request.Request(base + "/auth/login", method="POST", data=login_data, headers={"Content-Type": "application/x-www-form-urlencoded"})
                    try:
                        opener.open(login_request, timeout=5)
                    except urllib.error.HTTPError as error:
                        login = error
                    self.assertEqual(login.code, 303)
                    cookie = login.headers["Set-Cookie"].split(";", 1)[0]

                    allowed = ["/", "/index.html", "/map.html", "/contacts.html", "/core/gallery.js", "/core/gallery.css", "/core/map.js", "/core/map.css", "/core/table-export.js", "/core/contacts.js", "/core/contacts.css", "/registry.json", "/regions/demo/region.json", "/regions/demo/data/objects.geojson"]
                    if os.name != "nt":
                        allowed.extend(["/regions/linked/region.json", "/regions/linked/data/objects.geojson"])
                    for path in allowed:
                        self.assertEqual(request(path, cookie=cookie)[0], 200, path)
                    head = request("/regions/demo/data/objects.geojson", method="HEAD", cookie=cookie)
                    self.assertEqual(head[0], 200)
                    self.assertEqual(head[2], b"")
                    self.assertEqual(request("/api/regeneration?region=unregistered", cookie=cookie)[0], 404)

                    forbidden = ["/.runtime/users.json", "/.git/config", "/serve.py", "/regions/demo/SOURCES.md", "/regions/demo/data/undeclared.geojson", "/regions/unregistered/region.json", "/%2e%2e/serve.py", "/regions/demo/data/%2e%2e/region.json"]
                    if os.name != "nt":
                        forbidden.append("/regions/demo/data/leak.geojson")
                    for path in forbidden:
                        self.assertEqual(request(path, cookie=cookie)[0], 404, path)
                        self.assertEqual(request(path, method="HEAD", cookie=cookie)[0], 404, f"HEAD {path}")
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_auth_rejects_unsafe_inputs_and_redirects(self):
        with self.assertRaises(ValueError):
            auth.validate_username("../admin")
        with self.assertRaises(ValueError):
            auth.validate_password("short")
        self.assertEqual(serve.safe_next("https://example.test"), "/")
        self.assertEqual(serve.safe_next("//example.test"), "/")
        self.assertEqual(serve.safe_next("/\\evil.example"), "/")
        self.assertEqual(serve.safe_next("/%5cevil.example"), "/")
        self.assertEqual(serve.safe_next("/%255cevil.example"), "/")
        self.assertEqual(serve.safe_next("/?region=demo"), "/?region=demo")
        self.assertTrue(serve.loopback_bind("127.0.0.1"))
        self.assertTrue(serve.loopback_bind("::1"))
        self.assertFalse(serve.loopback_bind("0.0.0.0"))
        fake = type("Request", (), {"client_address": ("127.0.0.1", 1), "headers": {"X-Forwarded-For": "203.0.113.9, 127.0.0.1"}})()
        with patch.dict(os.environ, {"RMF_TRUST_PROXY": "1"}):
            self.assertEqual(serve.client_ip(fake), "203.0.113.9")
        if os.name != "nt":
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "users.json"; auth.set_user("map_user", "symlink-test-password", target)
                link = Path(directory) / "linked.json"; link.symlink_to(target)
                with patch.dict(os.environ, {"RMF_AUTH_FILE": str(link)}), self.assertRaisesRegex(ValueError, "symlink"):
                    auth.load_store()

    def test_frontend_has_generic_advanced_renderers(self):
        source = (manage.ROOT / "core" / "map.js").read_text(encoding="utf-8")
        styles = (manage.ROOT / "core" / "map.css").read_text(encoding="utf-8")
        gallery_styles = (manage.ROOT / "core" / "gallery.css").read_text(encoding="utf-8")
        gallery = (manage.ROOT / "core" / "gallery.js").read_text(encoding="utf-8")
        homepage = (manage.ROOT / "index.html").read_text(encoding="utf-8")
        map_page = (manage.ROOT / "map.html").read_text(encoding="utf-8")
        self.assertIn("region-gallery", homepage)
        self.assertIn("/map.html?region=", gallery)
        self.assertIn('class="home-link" href="/"', map_page)
        for contract in ("L.canvas(", "buildTable", "relatedBounds", "syncZoomLayers", "fill_max_zoom", "refreshRegeneration", "regenerationRequested", "location.reload()", "maplibreGL", "tiles.openfreemap.org/styles/positron", "animate:false", "densityLegendSignature", "map.hasLayer(state.layer)", "Общая шкала моделей плотности", "map.on('zoomend overlayadd overlayremove',renderLegend)", "externalMapLink", "https://yandex.ru/maps/", "related_filter_min_exclusive", "bounds.getCenter()"):
            self.assertIn(contract, source)
        self.assertNotIn("nearest_layer", source)
        self.assertNotIn("map.distance(origin", source)
        self.assertNotIn("tile.openstreetmap.org", source)
        self.assertEqual(source.count("L.canvas("), 1)
        self.assertNotIn("map.createPane(", source)
        self.assertIn("z_index??a[1].order", source)
        self.assertIn("state.layer.bringToFront", source)
        self.assertIn("max-height:calc(100vh - 88px)", styles)
        self.assertIn("overflow-y:auto", styles)
        self.assertIn('class="mobile-map-nav"', map_page)
        self.assertIn('id="mobile-sheet"', map_page)
        self.assertIn("setupMobileUi", source)
        self.assertIn("window.matchMedia('(max-width:820px)')", source)
        self.assertIn("state.tablePanel=panel", source)
        self.assertIn('data-label=', source)
        self.assertIn("height:100dvh", styles)
        self.assertIn("safe-area-inset-bottom", styles)
        self.assertIn(".mobile-map-header,.mobile-map-nav,.mobile-sheet,.mobile-sheet-backdrop{display:none}", styles)
        self.assertIn(".brand-compact{display:none}", gallery_styles)
        self.assertIn("viewport-fit=cover", map_page)
        self.assertIn("viewport-fit=cover", homepage)
        self.assertIn("element.inert=true", source)
        self.assertIn("MutationObserver", source)
        self.assertIn(".mobile-sheet-backdrop{display:none!important}", styles)
        self.assertEqual(gallery_styles.count("@media(max-width:650px)"), 1)

    def test_regeneration_contract_is_opt_in(self):
        source = (manage.ROOT / "pipeline_core" / "regeneration.py").read_text(encoding="utf-8")
        server = (manage.ROOT / "serve.py").read_text(encoding="utf-8")
        self.assertIn('layer.get("regenerable") is True', source)
        self.assertIn("manage.validate_regeneration", source)
        self.assertIn("timeout=timeout", source)
        self.assertIn("RMF_ALLOW_REGENERATION", server)
        self.assertIn("HTTPStatus.TOO_MANY_REQUESTS", server)
        self.assertIn(".runtime/regeneration.log", server)
        self.assertNotIn("subprocess.DEVNULL", server)

    def regeneration_fixture(self, temporary, command):
        regions = temporary / "regions"
        root = regions / "demo"
        (root / "data").mkdir(parents=True)
        (root / "pipeline").mkdir()
        payload = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.62, 55.75]}, "properties": {"name": "old"}}]}
        (root / "data/objects.geojson").write_text(json.dumps(payload), encoding="utf-8")
        config = {"schema_version": 1, "region_id": "demo", "layers": {"objects": {"file": "data/objects.geojson", "label": "Objects", "geometry_types": ["Point"], "regenerable": True}}}
        (root / "region.json").write_text(json.dumps(config), encoding="utf-8")
        spec = {"schema_version": 1, "region_id": "demo", "enabled": True, "min_interval_seconds": 300, "outputs": ["objects.geojson"], "commands": [{"command": command}]}
        (root / "pipeline/regeneration.json").write_text(json.dumps(spec), encoding="utf-8")
        return regions, root

    def run_regeneration(self, regions):
        with patch.object(manage, "REGIONS", regions), patch.object(sys, "argv", ["regeneration.py", "--region", "demo"]):
            regeneration.main()

    def test_regeneration_requires_fresh_output_and_preserves_success_time(self):
        with tempfile.TemporaryDirectory() as directory:
            regions, root = self.regeneration_fixture(Path(directory), ["{python}", "-c", "pass"])
            (root / ".runtime").mkdir()
            (root / ".runtime/regeneration.json").write_text(json.dumps({"last_success_at": "2026-01-01T10:00:00+00:00"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not create"):
                self.run_regeneration(regions)
            current = json.loads((root / "data/objects.geojson").read_text(encoding="utf-8"))
            state = json.loads((root / ".runtime/regeneration.json").read_text(encoding="utf-8"))
            self.assertEqual(current["features"][0]["properties"]["name"], "old")
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["last_success_at"], "2026-01-01T10:00:00+00:00")
            self.assertFalse((root / ".runtime/.regeneration.lock").exists())

    def test_corrupt_runtime_state_never_leaves_regeneration_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            regions, root = self.regeneration_fixture(Path(directory), ["{python}", "-c", "pass"])
            (root / ".runtime").mkdir()
            (root / ".runtime/regeneration.json").write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not create"):
                self.run_regeneration(regions)
            self.assertFalse((root / ".runtime/.regeneration.lock").exists())
            state = json.loads((root / ".runtime/regeneration.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")

    def test_regeneration_restores_data_if_publish_swap_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            regions, root = self.regeneration_fixture(Path(directory), ["{python}", "pipeline/generate.py"])
            generator = "import json,os\nfrom pathlib import Path\np=Path(os.environ[\"RMF_OUTPUT_DIR\"])/\"objects.geojson\"\np.write_text(json.dumps({\"type\":\"FeatureCollection\",\"features\":[{\"type\":\"Feature\",\"geometry\":{\"type\":\"Point\",\"coordinates\":[37.63,55.76]},\"properties\":{\"name\":\"new\"}}]}))\n"
            (root / "pipeline/generate.py").write_text(generator, encoding="utf-8")
            real_replace = os.replace
            def fail_publish_replace(source, target):
                if Path(target) == root / "data" and Path(source) != root / ".data.regeneration.backup":
                    raise OSError("simulated publish failure")
                return real_replace(source, target)
            with patch.object(regeneration.os, "replace", side_effect=fail_publish_replace):
                with self.assertRaisesRegex(OSError, "simulated publish failure"):
                    self.run_regeneration(regions)
            current = json.loads((root / "data/objects.geojson").read_text(encoding="utf-8"))
            self.assertEqual(current["features"][0]["properties"]["name"], "old")
            self.assertFalse((root / ".data.regeneration.backup").exists())

    def test_regeneration_configuration_is_validated_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            regions, root = self.regeneration_fixture(Path(directory), ["{python}", "-c", "pass"])
            config = json.loads((root / "region.json").read_text(encoding="utf-8"))
            self.assertEqual(manage.validate_regeneration(root, config), 1)
            spec_path = root / "pipeline/regeneration.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            missing_interval = {key: value for key, value in spec.items() if key != "min_interval_seconds"}
            spec_path.write_text(json.dumps(missing_interval), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields differ from schema"):
                manage.validate_regeneration(root, config)
            spec["unexpected"] = True
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields differ from schema"):
                manage.validate_regeneration(root, config)
            del spec["unexpected"]
            spec["commands"][0]["unexpected"] = True
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid regeneration command"):
                manage.validate_regeneration(root, config)
            del spec["commands"][0]["unexpected"]
            spec["min_interval_seconds"] = 299
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, ">= 300"):
                manage.validate_regeneration(root, config)
            spec["min_interval_seconds"] = 300
            spec["outputs"] = ["not-declared.geojson"]
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "regenerable layer"):
                manage.validate_regeneration(root, config)

    def test_regeneration_lock_reservation_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".runtime/.regeneration.lock"
            self.assertTrue(serve.reserve_lock(lock))
            self.assertFalse(serve.reserve_lock(lock))
            lock.write_text("99999999", encoding="ascii")
            self.assertTrue(serve.reserve_lock(lock))
            self.assertEqual(serve.interval({"min_interval_seconds": "broken"}), 300)
            self.assertIsNone(serve.elapsed_since("broken"))
            with patch.dict(os.environ, {"RMF_REGENERATION_TIMEOUT_SECONDS": "1"}):
                self.assertEqual(regeneration.command_timeout(), 60)
            with patch.dict(os.environ, {"RMF_REGENERATION_TIMEOUT_SECONDS": "broken"}):
                self.assertEqual(regeneration.command_timeout(), 3600)

    def test_contacts_pipeline_normalizes_deduplicates_and_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory); regions = temporary / "regions"; root = regions / "demo"
            (root / "contacts/seeds").mkdir(parents=True)
            (root / "region.json").write_text(json.dumps({"region_id": "demo"}), encoding="utf-8")
            spec = {"schema_version": 1, "region_id": "demo", "enabled": True, "min_interval_seconds": 3600, "request_delay_seconds": 1, "sources": [{"source_id": "reviewed_seed", "type": "local_json", "path": "contacts/seeds/contacts.json"}]}
            (root / "contacts/sources.json").write_text(json.dumps(spec), encoding="utf-8")
            seed = {"contacts": [
                {"name": "ООО Агентство Дом", "kind": "agency", "coverage": ["Регион"], "phones": ["8 (999) 111-22-33"], "websites": ["https://dom.example/about"]},
                {"name": "Дом", "kind": "agency", "coverage": ["Регион"], "phones": ["+7 999 111 22 33"], "emails": ["OFFICE@DOM.EXAMPLE"]},
                {"name": "Публичный брокер", "kind": "broker", "coverage": ["Город"], "emails": ["broker@example.test"]}
            ]}
            (root / "contacts/seeds/contacts.json").write_text(json.dumps(seed), encoding="utf-8")
            runtime = temporary / "runtime"
            with patch.object(manage, "REGIONS", regions), patch.object(manage, "RUNTIME_ROOT", runtime):
                state = contacts.collect("demo")
                self.assertEqual(state["status"], "succeeded")
                candidate = json.loads((runtime / "demo/contacts/candidates.json").read_text(encoding="utf-8"))
                self.assertEqual(len(candidate["contacts"]), 2)
                agency = next(item for item in candidate["contacts"] if item["kind"] == "agency")
                self.assertEqual(agency["phones"], ["+79991112233"]); self.assertEqual(agency["emails"], ["office@dom.example"]); self.assertEqual(len(agency["sources"]), 1)
                published = contacts.publish("demo", "map_admin")
                self.assertEqual(published["published_by"], "map_admin")
                self.assertTrue(all(item["review_status"] == "published" for item in published["contacts"]))
                candidate_again = json.loads(json.dumps(published)); candidate_again.pop("published_at"); candidate_again.pop("published_by")
                for item in candidate_again["contacts"]:
                    item["review_status"] = "needs_review"
                    for source in item["sources"]: source["retrieved_at"] = "2099-01-01T00:00:00+00:00"
                self.assertEqual(contacts.comparison(published, candidate_again)["unchanged"], 2)
                old_id = candidate_again["contacts"][0]["contact_id"]; candidate_again["contacts"][0]["contact_id"] = "contact_ffffffffffffffff"; candidate_again["contacts"][0]["websites"].append("https://new.example/")
                contacts.preserve_published_ids(candidate_again["contacts"], published)
                self.assertEqual(candidate_again["contacts"][0]["contact_id"], old_id)
                self.assertFalse((runtime / "demo/contacts/.collection.lock").exists())

    def test_contacts_configuration_and_remote_source_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "contacts").mkdir(); config = {"region_id": "demo"}
            valid = {"schema_version": 1, "region_id": "demo", "enabled": True, "min_interval_seconds": 3600, "request_delay_seconds": 2, "sources": [{"source_id": "agency", "type": "website", "name": "Agency", "urls": ["https://example.com/contacts"], "allowed_hosts": ["example.com"], "respect_robots_txt": True}]}
            (root / "contacts/sources.json").write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(manage.validate_contacts(root, config)["region_id"], "demo")
            valid["sources"][0]["urls"] = ["https://other.example/contacts"]
            (root / "contacts/sources.json").write_text(json.dumps(valid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not allowlisted"): manage.validate_contacts(root, config)
            valid["sources"][0]["urls"] = ["http://example.com/contacts"]
            (root / "contacts/sources.json").write_text(json.dumps(valid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "HTTPS"): manage.validate_contacts(root, config)
            self.assertIsNone(contacts.normalize_url("file:///etc/passwd"))
            with patch("pipeline_core.contacts.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
                with self.assertRaisesRegex(ValueError, "non-public"): contacts.checked_url("https://example.com/", ["example.com"])

    def test_contacts_frontend_and_admin_boundary_are_explicit(self):
        page = (manage.ROOT / "contacts.html").read_text(encoding="utf-8")
        source = (manage.ROOT / "core/contacts.js").read_text(encoding="utf-8")
        server = (manage.ROOT / "serve.py").read_text(encoding="utf-8")
        self.assertIn('id="admin-panel"', page); self.assertIn('id="approval-check"', page)
        self.assertIn("/api/contacts/collect", source); self.assertIn("/api/contacts/publish", source)
        self.assertIn("RMF_ADMIN_USERS", server); self.assertIn("administrator access required", server)
        with patch.dict(os.environ, {"RMF_ADMIN_USERS": "map_admin,other"}):
            self.assertTrue(serve.is_admin("map_admin")); self.assertFalse(serve.is_admin("viewer"))

    def test_polygon_fill_zoom_contract(self):
        config = json.loads((manage.ROOT / "templates" / "region.example.json").read_text(encoding="utf-8"))
        config["region_id"] = "demo"
        config["layers"] = {"areas": {"file": "data/areas.geojson", "label": "Areas", "geometry_types": ["Polygon"], "style": {"fill_max_zoom": 9, "fill_opacity": 0.7, "fill_opacity_above_max": 0}}}
        manage.validate_config(config, "demo")
        config["layers"]["areas"]["style"]["fill_opacity_above_max"] = 1.5
        with self.assertRaises(ValueError):
            manage.validate_config(config, "demo")

    def test_external_map_link_contract(self):
        config = json.loads((manage.ROOT / "templates" / "region.example.json").read_text(encoding="utf-8"))
        config["region_id"] = "demo"
        config["layers"] = {
            "priority": {"file": "data/priority.geojson", "label": "Priority", "geometry_types": ["Point"]},
            "density": {
                "file": "data/density.geojson", "label": "Density", "renderer": "density", "geometry_types": ["Polygon"],
                "external_map_link": {"provider": "yandex_maps", "related_layer": "priority", "feature_join_field": "settlement_name", "related_join_field": "name", "related_filter_field": "capacity", "related_filter_min_exclusive": 0},
            },
        }
        manage.validate_config(config, "demo")
        valid_link = dict(config["layers"]["density"]["external_map_link"])
        config["layers"]["density"]["external_map_link"] = {"provider": "yandex_maps", "related_layer": "priority"}
        with self.assertRaisesRegex(ValueError, "relation is invalid"):
            manage.validate_config(config, "demo")
        config["layers"]["density"]["external_map_link"] = valid_link
        config["layers"]["density"]["external_map_link"]["related_layer"] = "missing"
        with self.assertRaisesRegex(ValueError, "relation is invalid"):
            manage.validate_config(config, "demo")
        config["layers"]["density"]["external_map_link"] = {"provider": "yandex_maps", "feature_join_field": "settlement_name"}
        with self.assertRaisesRegex(ValueError, "relation fields require related_layer"):
            manage.validate_config(config, "demo")
        config["layers"]["density"]["external_map_link"] = {"provider": "yandex_maps", "label": "Open cell centre"}
        manage.validate_config(config, "demo")
        config["layers"]["density"]["external_map_link"] = None
        with self.assertRaisesRegex(ValueError, "external_map_link is malformed"):
            manage.validate_config(config, "demo")
        config["layers"]["density"]["external_map_link"] = False
        manage.validate_config(config, "demo")

    def test_external_map_link_schema_declares_relation_dependencies(self):
        schema = json.loads((manage.ROOT / "schemas" / "region.schema.json").read_text(encoding="utf-8"))
        link_schema = schema["properties"]["layers"]["additionalProperties"]["properties"]["external_map_link"]["oneOf"][1]
        rules = link_schema["allOf"]
        self.assertIn(
            {"if": {"required": ["related_layer"]}, "then": {"required": ["feature_join_field", "related_join_field"]}},
            rules,
        )
        relation_fields = {"feature_join_field", "related_join_field", "related_filter_field", "related_filter_min_exclusive"}
        reverse_rule = next(rule for rule in rules if rule.get("then") == {"required": ["related_layer"]})
        self.assertEqual(
            {branch["required"][0] for branch in reverse_rule["if"]["anyOf"]},
            relation_fields,
        )
        self.assertIn(
            {"if": {"required": ["related_filter_min_exclusive"]}, "then": {"required": ["related_filter_field"]}},
            rules,
        )

    def test_adaptive_density_refines_only_complex_blocks(self):
        rows = {
            (0, 0): {"proxy": 1000.0, "buildings": 2, "explicit": 1000.0},
            (1, 0): {"proxy": 1000.0, "buildings": 2, "explicit": 1000.0},
            (4, 0): {"proxy": 9000.0, "buildings": 20, "explicit": 9000.0},
            (5, 0): {"proxy": 9000.0, "buildings": 20, "explicit": 9000.0},
        }
        result = adaptive_rows(rows, 100, 400, {"split_buildings": 12, "split_proxy_m2_per_km2": 80000})
        self.assertIn((0, 0, 4), result)
        self.assertTrue(any(span < 4 and ix >= 4 for ix, _iy, span in result))
        self.assertEqual(sum(item["proxy"] for item in result.values()), 20000.0)

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
