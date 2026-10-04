"""Exercise real temporary fixture files; no OWUI, KB or model request occurs."""

import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from supervisor import prepare_demo_live, smoke_demo_live


def prepare(workspace, owner="fixture-owner", reset=False):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(prepare_demo_live.fixture_source(str(workspace), owner, reset), {})
    return json.loads(output.getvalue())


def symlink_or_skip(case, target, destination, *, directory=False):
    try:
        os.symlink(target, destination, target_is_directory=directory)
    except (PermissionError, NotImplementedError) as error:
        case.skipTest("This host cannot create symbolic links: " + type(error).__name__)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314 or error.errno in {errno.EPERM, errno.EACCES}:
            case.skipTest("This host cannot create symbolic links: " + type(error).__name__)
        raise


class DemoFixtureBoundaryTests(unittest.TestCase):
    def test_fixture_really_runs_three_methods_and_thirteen_subcases_with_failure(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-fixture-test-") as folder:
            report = prepare(folder)
            self.assertNotEqual(report["initial_exit_code"], 0)
            self.assertIn("Ran 3 tests", report["initial_test_output"])
            directory = Path(folder) / prepare_demo_live.DIRECTORY
            self.assertEqual((directory / "test_demo_window_validator.py").read_bytes(),
                             prepare_demo_live.TESTS.encode())
            # Count actual addSubTest callbacks, including successful subcases,
            # rather than trusting the helper's reported input count.
            source = """import io,json,unittest
class CountingResult(unittest.TextTestResult):
    subcases=0
    def addSubTest(self,test,subtest,error):
        self.subcases+=1
        super().addSubTest(test,subtest,error)
suite=unittest.defaultTestLoader.loadTestsFromName('test_demo_window_validator')
result=unittest.TextTestRunner(stream=io.StringIO(),resultclass=CountingResult).run(suite)
print(json.dumps({'methods':result.testsRun,'subcases':result.subcases,'failures':len(result.failures),'errors':len(result.errors)}))
"""
            actual = subprocess.run([sys.executable, "-c", source], cwd=directory,
                                    capture_output=True, text=True, timeout=15, check=True)
            counted = json.loads(actual.stdout)
            self.assertEqual(counted, {"methods": 3, "subcases": 13, "failures": 10, "errors": 0})

    def test_reset_refuses_unmarked_existing_directory_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-unmarked-test-") as folder:
            directory = Path(folder) / prepare_demo_live.DIRECTORY
            directory.mkdir()
            existing = directory / "demo_window_validator.py"
            existing.write_bytes(b"existing user code\n")
            with self.assertRaisesRegex(AssertionError, "not this marked demo fixture"):
                prepare(folder, reset=True)
            self.assertEqual(existing.read_bytes(), b"existing user code\n")
            self.assertFalse((directory / prepare_demo_live.MARKER).exists())
            self.assertFalse((directory / "test_demo_window_validator.py").exists())

    def test_reset_refuses_another_owner_and_keeps_fixture_bytes(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-owner-test-") as folder:
            prepare(folder, owner="owner-one")
            directory = Path(folder) / prepare_demo_live.DIRECTORY
            before = {name: (directory / name).read_bytes() for name in (
                "demo_window_validator.py", "test_demo_window_validator.py", prepare_demo_live.MARKER)}
            with self.assertRaisesRegex(AssertionError, "ownership mismatch"):
                prepare(folder, owner="owner-two", reset=True)
            self.assertEqual(before, {name: (directory / name).read_bytes() for name in before})

    def test_reset_restores_only_owned_files_and_preserves_unrelated_data(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-reset-test-") as folder:
            prepare(folder)
            directory = Path(folder) / prepare_demo_live.DIRECTORY
            implementation = directory / "demo_window_validator.py"
            implementation.write_bytes(b"user changed this demo implementation\n")
            unrelated = directory / "unrelated.py"
            unrelated.write_bytes(b"do not change this file\n")
            outside = Path(folder) / "other-project"
            outside.mkdir()
            outside_file = outside / "work.txt"
            outside_file.write_bytes(b"retain other project\n")
            reset = prepare(folder, reset=True)
            self.assertNotEqual(reset["initial_exit_code"], 0)
            self.assertEqual(implementation.read_bytes(), prepare_demo_live.IMPLEMENTATION.encode())
            self.assertEqual(unrelated.read_bytes(), b"do not change this file\n")
            self.assertEqual(outside_file.read_bytes(), b"retain other project\n")

    def test_reset_refuses_symlinked_directory_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-dirlink-test-") as folder:
            root = Path(folder)
            target = root / "other-project"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_bytes(b"keep outside demo\n")
            symlink_or_skip(self, target, root / prepare_demo_live.DIRECTORY, directory=True)
            with self.assertRaisesRegex(AssertionError, "must not be a symlink"):
                prepare(folder, reset=True)
            self.assertEqual(sentinel.read_bytes(), b"keep outside demo\n")
            self.assertEqual([path.name for path in target.iterdir()], ["keep.txt"])

    def test_reset_refuses_each_symlinked_owned_file_before_any_overwrite(self):
        for name in ("demo_window_validator.py", "test_demo_window_validator.py", prepare_demo_live.MARKER):
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="owui-demo-filelink-test-") as folder:
                prepare(folder)
                root = Path(folder)
                directory = root / prepare_demo_live.DIRECTORY
                target = root / "external-file.txt"
                target.write_bytes((directory / name).read_bytes())
                before_target = target.read_bytes()
                (directory / name).unlink()
                symlink_or_skip(self, target, directory / name)
                before_others = {other: (directory / other).read_bytes()
                                 for other in ("demo_window_validator.py", "test_demo_window_validator.py", prepare_demo_live.MARKER)
                                 if other != name}
                with self.assertRaises(AssertionError):
                    prepare(folder, reset=True)
                self.assertEqual(target.read_bytes(), before_target)
                self.assertEqual(before_others, {other: (directory / other).read_bytes() for other in before_others})

    def test_embedded_knowledge_document_exactly_matches_repository_bytes(self):
        source = Path(__file__).resolve().parent.parent / "docs" / "fixtures" / "demo-param-001.md"
        if not source.is_file():
            self.skipTest("Repository docs are not mounted here; run this parity check in the full checkout")
        self.assertEqual(source.read_bytes(), prepare_demo_live.DOCUMENT.encode("utf-8"))


class DemoFixedTestIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_reads_real_bytes_and_rejects_changed_or_unrecorded_tests(self):
        with tempfile.TemporaryDirectory(prefix="owui-demo-sha-test-") as folder:
            root = Path(folder)
            prepared = prepare(root)
            record = root / "prepare-report.json"
            record.write_text(json.dumps(prepared), encoding="utf-8")

            async def actual_file_reader(command, cwd, wait):
                parts = shlex.split(command)
                self.assertEqual(parts[:2], ["python", "-c"])
                self.assertEqual(cwd, str(root))
                output = io.StringIO()
                try:
                    with contextlib.redirect_stdout(output):
                        exec(parts[2], {})
                except AssertionError:
                    return {"exit_code": 1, "output": []}
                return {"exit_code": 0, "output": [{"data": output.getvalue()}]}

            user = SimpleNamespace(id="fixture-owner")
            terminal = {"run_command": {"callable": actual_file_reader}}
            bridge = ModuleType("claude_agent_pipe")
            bridge._unwrap_owui_tool_result = lambda value: value

            def local_private_record(path):
                self.assertEqual(str(path), "/app/backend/data/demo-live-prepare.json")
                return record

            with patch.dict(sys.modules, {"claude_agent_pipe": bridge}), \
                    patch.object(prepare_demo_live, "terminal_context", AsyncMock(return_value=(user, terminal, str(root)))), \
                    patch.object(smoke_demo_live, "Path", side_effect=local_private_record):
                initial = await smoke_demo_live.verify_fixed_test_sha()
                self.assertTrue(initial["passed"])
                self.assertEqual(initial["actual_tests_sha256"], hashlib.sha256(prepare_demo_live.TESTS.encode()).hexdigest())
                test_file = root / prepare_demo_live.DIRECTORY / "test_demo_window_validator.py"
                test_file.write_bytes(test_file.read_bytes() + b"\n# changed after preparation\n")
                changed = await smoke_demo_live.verify_fixed_test_sha()
                self.assertFalse(changed["passed"])
                self.assertFalse(changed["tests_unchanged"])
                self.assertNotEqual(changed["actual_tests_sha256"], changed["prepared_tests_sha256"])
                test_file.write_bytes(prepare_demo_live.TESTS.encode())
                record.unlink()
                unrecorded = await smoke_demo_live.verify_fixed_test_sha()
                self.assertFalse(unrecorded["passed"])
                self.assertFalse(unrecorded["tests_unchanged"])
                self.assertIsNone(unrecorded["prepared_tests_sha256"])


if __name__ == "__main__":
    unittest.main()
