from pathlib import Path
import json
import shutil
import tempfile
import unittest

from patch_frontend import INDEX_MARKER, MANIFEST_NAME, MARKER, ORIGINAL, REPLACEMENT, install


class PatchFrontendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.build = Path(self.directory.name) / "build"
        self.bundle = self.build / "_app/immutable/chunks/chat.js"
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_text("prefix " + ORIGINAL + " suffix", encoding="utf-8")
        self.page = self.build / "_app/immutable/nodes/page.js"
        self.page.parent.mkdir(parents=True)
        self.page.write_text('import {Chat} from "../chunks/chat.js"; export {Chat};', encoding="utf-8")
        self.app = self.build / "_app/immutable/entry/app.js"
        self.app.parent.mkdir(parents=True)
        self.app.write_text(
            'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["../nodes/page.js",'
            '"../chunks/chat.js","../assets/style.css"])))=>i.map(i=>d[i]);'
            'const load=()=>import("../nodes/page.js");'
            'const note="../chunks/chat.js";', encoding="utf-8")
        self.other = self.build / "_app/immutable/chunks/other.js"
        self.other.write_text('const data="./chat.js";', encoding="utf-8")
        self.index = self.build / "index.html"
        self.original_index = (
            '<html><head><!-- Open WebUI license -->'
            '<link rel="modulepreload" href="/_app/immutable/entry/app.js"></head>'
            '<body><script>import("/_app/immutable/entry/app.js");</script></body></html>')
        self.index.write_text(self.original_index, encoding="utf-8")
        self.version = self.build / "_app/version.json"
        self.version.write_text('{"version":"upstream-build","extra":"keep"}', encoding="utf-8")
        self.original_version = self.version.read_bytes()
        self.assets = Path(self.directory.name) / "source-assets"
        self.assets.mkdir()
        for name in ("jira-confirmation.js", "jira-confirmation.css"):
            shutil.copyfile(Path(__file__).parent / name, self.assets / name)

    def snapshot(self):
        return {path.relative_to(self.build).as_posix(): path.read_bytes()
                for path in self.build.rglob("*") if path.is_file()}

    def mapping(self):
        return json.loads((self.build / "assets" / MANIFEST_NAME).read_text(encoding="utf-8"))["mapping"]

    def test_installs_only_jira_branch_and_preserves_native_confirmation(self):
        result = install(self.build, self.assets)
        self.assertEqual(result["original_bundle"], "_app/immutable/chunks/chat.js")
        self.assertIn(f'.jira-{result["assets_version"]}.js', result["bundle"])
        self.assertIn(ORIGINAL, self.bundle.read_text(encoding="utf-8"))
        source = (self.build / result["bundle"]).read_text(encoding="utf-8")
        self.assertIn(REPLACEMENT, source)
        self.assertIn('else{c(Le,C),c(Ve,!1),c(Je,!0)', source)
        self.assertIn('catch(()=>C({error:"jira_confirmation_unavailable"}))', source)
        self.assertIn("<!-- Open WebUI license -->", self.index.read_text(encoding="utf-8"))
        self.assertTrue((self.build / "assets/jira-confirmation.js").is_file())

    def test_versions_import_ancestors_and_vite_table_without_rewriting_data(self):
        originals = {path: path.read_bytes() for path in (self.bundle, self.page, self.app, self.other)}
        result = install(self.build, self.assets)
        mapping = self.mapping()
        self.assertEqual(set(mapping), {
            "_app/immutable/chunks/chat.js", "_app/immutable/nodes/page.js", "_app/immutable/entry/app.js"})
        self.assertEqual(result["versioned_modules"], "3")
        for path, contents in originals.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertNotIn(self.other.relative_to(self.build).as_posix(), mapping)
        chat_name = Path(mapping["_app/immutable/chunks/chat.js"]).name
        page_name = Path(mapping["_app/immutable/nodes/page.js"]).name
        app_source = (self.build / mapping["_app/immutable/entry/app.js"]).read_text(encoding="utf-8")
        self.assertIn(f'm.f=["../nodes/{page_name}","../chunks/{chat_name}"', app_source)
        self.assertIn(f'import("../nodes/{page_name}")', app_source)
        self.assertIn('const note="../chunks/chat.js";', app_source)
        self.assertIn('"../assets/style.css"', app_source)
        page_source = (self.build / mapping["_app/immutable/nodes/page.js"]).read_text(encoding="utf-8")
        self.assertIn(f'from "../chunks/{chat_name}"', page_source)
        index = self.index.read_text(encoding="utf-8")
        app_url = "/" + mapping["_app/immutable/entry/app.js"]
        self.assertIn(f'href="{app_url}"', index)
        self.assertIn(f'import("{app_url}")', index)
        self.assertNotIn('"/_app/immutable/entry/app.js"', index)
        self.assertEqual(self.version.read_bytes(), self.original_version)

    def test_repeated_install_is_identical(self):
        install(self.build, self.assets)
        expected = self.snapshot()
        install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())
        self.assertEqual(self.index.read_text(encoding="utf-8").count(INDEX_MARKER), 1)

    def test_optional_task_plan_assets_install_and_each_update_changes_digest(self):
        names = ("task-plan-status.js", "task-plan-status.css")
        for name in names:
            shutil.copyfile(Path(__file__).parent / name, self.assets / name)
        previous = install(self.build, self.assets)
        for name in names:
            copied = self.build / "assets" / name
            self.assertEqual(copied.read_bytes(), (self.assets / name).read_bytes())
            self.assertIn(f'/assets/{name}?v={previous["assets_version"]}', self.index.read_text(encoding="utf-8"))
        for name in names:
            source = self.assets / name
            source.write_bytes(source.read_bytes() + b"\n/* updated optional asset */\n")
            current = install(self.build, self.assets)
            self.assertNotEqual(previous["assets_version"], current["assets_version"])
            index = self.index.read_text(encoding="utf-8")
            for asset in names:
                self.assertIn(f'/assets/{asset}?v={current["assets_version"]}', index)
            self.assertEqual((self.build / "assets" / name).read_bytes(), source.read_bytes())
            previous = current
        expected = self.snapshot()
        install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())
        self.assertEqual(self.version.read_bytes(), self.original_version)

    def test_incomplete_optional_task_plan_assets_fail_before_mutation(self):
        shutil.copyfile(Path(__file__).parent / "task-plan-status.js", self.assets / "task-plan-status.js")
        expected = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Incomplete task plan assets"):
            install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())

    def test_asset_update_loads_new_graph_and_retains_previous_graph(self):
        first = install(self.build, self.assets)
        old_mapping = self.mapping()
        old_sources = {path: (self.build / path).read_bytes() for path in old_mapping.values()}
        js = self.assets / "jira-confirmation.js"
        js.write_bytes(js.read_bytes() + b"\n// new editor revision\n")
        second = install(self.build, self.assets)
        self.assertNotEqual(first["assets_version"], second["assets_version"])
        self.assertEqual(set(old_mapping), set(self.mapping()))
        self.assertIn(second["assets_version"], self.index.read_text(encoding="utf-8"))
        self.assertNotIn(first["assets_version"], self.index.read_text(encoding="utf-8"))
        for path, contents in old_sources.items():
            self.assertEqual((self.build / path).read_bytes(), contents)
        self.assertEqual(self.version.read_bytes(), self.original_version)
        expected = self.snapshot()
        install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())

    def test_legacy_in_place_patch_is_upgraded_without_modifying_old_modules(self):
        self.bundle.write_text("prefix " + REPLACEMENT + " suffix", encoding="utf-8")
        original = self.bundle.read_bytes()
        self.index.write_text(self.original_index.replace("</head>", INDEX_MARKER + '\n<script src="/assets/jira-confirmation.js?v=old"></script>\n<!-- /owui-jira-review-assets -->\n</head>'), encoding="utf-8")
        result = install(self.build, self.assets)
        self.assertEqual(self.bundle.read_bytes(), original)
        self.assertIn(REPLACEMENT, (self.build / result["bundle"]).read_text(encoding="utf-8"))
        self.assertNotIn("?v=old", self.index.read_text(encoding="utf-8"))

    def test_preserves_base_url_and_import_query_suffix(self):
        self.index.write_text(self.original_index.replace('/_app/', '/owui/_app/'), encoding="utf-8")
        self.page.write_text('export {Chat} from "../chunks/chat.js?mode=1#fragment";', encoding="utf-8")
        install(self.build, self.assets)
        mapping = self.mapping()
        self.assertIn('/owui/' + mapping["_app/immutable/entry/app.js"], self.index.read_text(encoding="utf-8"))
        page_source = (self.build / mapping["_app/immutable/nodes/page.js"]).read_text(encoding="utf-8")
        self.assertIn(Path(mapping["_app/immutable/chunks/chat.js"]).name + '?mode=1#fragment', page_source)

    def test_reverse_import_cycle_is_versioned_as_one_closed_graph(self):
        self.bundle.write_text('import "../nodes/page.js";' + ORIGINAL, encoding="utf-8")
        install(self.build, self.assets)
        mapping = self.mapping()
        source = (self.build / mapping["_app/immutable/chunks/chat.js"]).read_text(encoding="utf-8")
        self.assertIn(Path(mapping["_app/immutable/nodes/page.js"]).name, source)

    def test_unknown_upstream_fails_before_mutation(self):
        self.bundle.write_text("different upstream bundle", encoding="utf-8")
        expected = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Expected one"):
            install(self.build, self.assets)
        self.assertEqual(self.index.read_text(encoding="utf-8"), self.original_index)
        self.assertFalse((self.build / "assets").exists())
        self.assertEqual(expected, self.snapshot())

    def test_duplicate_branch_fails_before_mutation(self):
        self.bundle.write_text(ORIGINAL * 2, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Expected one confirmation branch"):
            install(self.build, self.assets)
        self.assertEqual(self.index.read_text(encoding="utf-8"), self.original_index)

    def test_changed_installed_patch_is_rejected(self):
        self.bundle.write_text(MARKER + " changed implementation", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            install(self.build, self.assets)
        self.assertEqual(self.index.read_text(encoding="utf-8"), self.original_index)

    def test_malformed_index_is_rejected_before_bundle_changes(self):
        self.index.write_text("<html><body></body></html>", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "head closing"):
            install(self.build, self.assets)
        self.assertIn(ORIGINAL, self.bundle.read_text(encoding="utf-8"))

    def test_missing_reachable_entry_fails_before_any_writes(self):
        self.index.write_text('<html><head></head><body></body></html>', encoding="utf-8")
        expected = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "no reachable frontend entry"):
            install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())

    def test_manifest_cannot_write_outside_build(self):
        install(self.build, self.assets)
        manifest_path = self.build / "assets" / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["mapping"] = {"_app/immutable/../../../outside.js": "outside.jira-" + manifest["digest"] + ".js"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        expected = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "Invalid Jira review module mapping"):
            install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())

    def test_unexpected_frontend_version_fails_before_graph_update(self):
        install(self.build, self.assets)
        self.version.write_text('{"version":"unexpected-new-upstream"}', encoding="utf-8")
        expected = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "differs from the Jira module manifest"):
            install(self.build, self.assets)
        self.assertEqual(expected, self.snapshot())


if __name__ == "__main__":
    unittest.main()
