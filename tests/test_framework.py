import json
import os
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
from pipeline_core import regeneration
from pipeline_core.build_adaptive_density import adaptive_rows


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
                self.assertEqual(json.loads(session[2]), {"username": "map_user"})
                logout = request("/auth/logout", "POST", {}, cookie=cookie)
                self.assertIn("Max-Age=0", logout[1]["Set-Cookie"])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

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
            (project / "core/map.js").write_text("map", encoding="utf-8")
            (project / "core/map.css").write_text("css", encoding="utf-8")
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

                    allowed = ["/", "/index.html", "/core/map.js", "/core/map.css", "/registry.json", "/regions/demo/region.json", "/regions/demo/data/objects.geojson"]
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
        for contract in ("L.canvas(", "buildTable", "relatedBounds", "syncZoomLayers", "fill_max_zoom", "refreshRegeneration", "regenerationRequested", "location.reload()", "maplibreGL", "tiles.openfreemap.org/styles/positron", "animate:false"):
            self.assertIn(contract, source)
        self.assertNotIn("tile.openstreetmap.org", source)
        self.assertEqual(source.count("L.canvas("), 1)
        self.assertNotIn("map.createPane(", source)
        self.assertIn("z_index??a[1].order", source)
        self.assertIn("state.layer.bringToFront", source)

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

    def test_polygon_fill_zoom_contract(self):
        config = json.loads((manage.ROOT / "templates" / "region.example.json").read_text(encoding="utf-8"))
        config["region_id"] = "demo"
        config["layers"] = {"areas": {"file": "data/areas.geojson", "label": "Areas", "geometry_types": ["Polygon"], "style": {"fill_max_zoom": 9, "fill_opacity": 0.7, "fill_opacity_above_max": 0}}}
        manage.validate_config(config, "demo")
        config["layers"]["areas"]["style"]["fill_opacity_above_max"] = 1.5
        with self.assertRaises(ValueError):
            manage.validate_config(config, "demo")

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
