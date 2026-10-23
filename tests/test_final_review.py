"""Regression cases from the final offline review."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from swarmlib import acceptance, artifacts, backends, config, procs


class DependencyCache(unittest.TestCase):
    def test_shared_dependency_validates_each_edge_in_either_order(self):
        def record(deps=()):
            return {
                "status": "completed", "result_version": 2, "version": 1,
                "artifact": {"path": "result.md", "sha256": "current"},
                "acceptance_result": {"passed": True, "verified": True},
                "depends_on": list(deps),
                "dep_versions": {d: {"status": "completed", "result_version": 2,
                                     "definition_version": 1, "sha256": "current"}
                                 for d in deps},
            }

        for order in (("left", "right"), ("right", "left")):
            for field, stale in (("result_version", 1), ("definition_version", 0),
                                 ("sha256", "old"), ("status", "unverified")):
                with self.subTest(order=order, field=field):
                    tasks = {"root": record(), "left": record(["root"]),
                             "right": record(["root"]), "top": record(order)}
                    tasks["right"]["dep_versions"]["root"][field] = stale
                    errors = acceptance._task_record_defects(
                        {"tasks": tasks}, "top", None, {}, {"status": "completed"})
                    self.assertTrue(errors)
                    self.assertTrue(all(p == ["top", "right", "root"] for p, _ in errors),
                                    errors)


class CliResult(unittest.TestCase):
    def test_nonzero_exit_preserves_usage_but_fails(self):
        role = config.validate_roles(config.DEFAULT_ROLES)["planner"]
        backend = backends.ClaudeBackend(role, claude="/bin/true")
        payload = {"result": "apparently done", "is_error": False,
                   "usage": {"input_tokens": 100, "output_tokens": 20},
                   "model": role.model}
        proc = {"code": 1, "seconds": 0.1, "stdout": json.dumps(payload),
                "stderr": "process failed", "output": "process failed"}
        with mock.patch.object(procs, "run", return_value=proc):
            result = backend.call("test", cwd=".")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["usage"], payload["usage"])
        self.assertFalse(result["usage_missing"])


class ShellPlaceholders(unittest.TestCase):
    def test_quoted_and_bare_paths_remain_one_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a b.txt", "$(touch MARKER).txt", "a'b*.txt"):
                Path(tmp, name).write_text("contents")
                for command in ('cat {path}', 'cat "{path}"'):
                    with self.subTest(name=name, command=command):
                        shell, extra = procs.substitute_placeholders(command, {"{path}": name})
                        result = procs.run(shell, shell=True, cwd=tmp,
                                           env={**os.environ, **extra}, timeout=5)
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(result["stdout"], "contents")
                        self.assertFalse(Path(tmp, "MARKER").exists())


class CodeArtifacts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "original.txt").write_text("original\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=test", *args],
            cwd=cwd or self.repo, check=True, capture_output=True, text=True)

    def prepare(self, allowed):
        ws = artifacts.Workspace(self.repo, self.root / "run")
        task = {"id": "t", "kind": "code", "allowed_paths": allowed}
        ctx = ws.prepare(task, 1, [])
        return ws, task, ctx

    def test_worker_commit_does_not_erase_patch_evidence(self):
        ws, task, ctx = self.prepare(["original.txt"])
        cwd = Path(ctx["cwd"])
        (cwd / "original.txt").write_text("fixed\n")
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-qm", "worker fix", cwd=cwd)
        changes = ws.capture(ctx, task)
        self.assertIn("+fixed", changes["diff"])
        self.assertEqual(changes["changed_files"][0]["path"], "original.txt")
        self.assertEqual((self.repo / "original.txt").read_text(), "original\n")

    def test_rename_checks_source_and_destination_scope(self):
        ws, task, ctx = self.prepare(["allowed.txt"])
        cwd = Path(ctx["cwd"])
        self.git("mv", "original.txt", "allowed.txt", cwd=cwd)
        changes = ws.capture(ctx, task)
        self.assertFalse(changes["scope_ok"], changes)
        self.assertIn("original.txt", changes["outside_scope"])

    def test_dirty_paths_include_modified_untracked_and_renamed_files(self):
        self.git("mv", "original.txt", "renamed.txt")
        (self.repo / "new file.txt").write_text("untracked\n")
        ws = artifacts.Workspace(self.repo, self.root / "run")
        self.assertIn("new file.txt", ws.dirty)
        self.assertIn("renamed.txt", ws.dirty)
        self.assertTrue(ws.describe()["dirty_note"])


if __name__ == "__main__":
    unittest.main()
