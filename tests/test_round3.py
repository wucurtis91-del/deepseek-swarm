"""Regression tests for the third review round.

The first class covers completion over the full dependency chain. The second
class pins the supported isolation policy: code worktree only, with non-git
snapshot behavior left explicit.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmlib import acceptance, config  # noqa: E402
from test_loop import LoopCase, SELF, complete, dispatch, task  # noqa: E402
from test_round2 import code_task  # noqa: E402


class CompletionDependencyChain(LoopCase):
    def _text_task(self, tid, *, depends=None, satisfies=None, input_files=None):
        return task(
            tid, prompt=f"{tid}-task", depends_on=depends or [],
            satisfies=satisfies or [], input_files=input_files or [],
            acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
            acceptance_policy="deterministic",
            checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(0)"]}],
        )

    def _data(self, name="data.txt", text="original\n"):
        run_dir = self.root / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(text, encoding="utf-8")
        return run_dir / name

    def _evidence_for(self, eng, tid):
        return next(eid for eid, rec in eng.state["evidence"].items()
                    if rec.get("task") == tid and rec.get("completion_usable"))

    def _reject(self, eng, store, ev):
        out = self.apply(eng, store, complete(ev))
        self.assertNotEqual(out["status"], "complete")
        rejection = eng.state.get("completion_rejection") or {}
        self.assertTrue(rejection.get("errors"))
        return rejection["errors"]

    def test_two_level_ancestor_input_change_is_rejected_with_path(self):
        data = self._data()
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        a = self._text_task("a", input_files=["data.txt"])
        b = self._text_task("b", depends=["a"], satisfies=["g1"])
        self.apply(eng, store, dispatch(a, b))
        self.assertEqual(eng.state["tasks"]["a"]["status"], "completed")
        self.assertEqual(eng.state["tasks"]["b"]["status"], "completed")
        ev = self._evidence_for(eng, "b")
        data.write_text("changed\n", encoding="utf-8")
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("b -> a" in e for e in errors), errors)
        self.assertTrue(any("data.txt" in e for e in errors), errors)

    def test_three_level_ancestor_input_change_reports_full_path(self):
        data = self._data()
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        a = self._text_task("a", input_files=["data.txt"])
        b = self._text_task("b", depends=["a"])
        c = self._text_task("c", depends=["b"], satisfies=["g1"])
        self.apply(eng, store, dispatch(a, b, c))
        ev = self._evidence_for(eng, "c")
        data.write_text("changed\n", encoding="utf-8")
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("c -> b -> a" in e for e in errors), errors)
        self.assertTrue(any("data.txt" in e for e in errors), errors)

    def test_shared_ancestor_is_checked_once(self):
        shared = self._data("shared.txt")
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        root = self._text_task("root", input_files=["shared.txt"])
        left = self._text_task("left", depends=["root"])
        right = self._text_task("right", depends=["root"])
        top = self._text_task("top", depends=["left", "right"], satisfies=["g1"])
        self.apply(eng, store, dispatch(root, left, right, top))
        ev = self._evidence_for(eng, "top")
        shared.write_text("changed\n", encoding="utf-8")
        calls = {"shared": 0}
        original = acceptance._file_sha

        def counting(path):
            if Path(path).name == "shared.txt":
                calls["shared"] += 1
            return original(path)

        with mock.patch.object(acceptance, "_file_sha", side_effect=counting):
            errors = self._reject(eng, store, ev)
        self.assertTrue(any("shared.txt" in e for e in errors), errors)
        self.assertEqual(calls["shared"], 1, calls)

    def test_ancestor_code_patch_change_is_rejected_with_path(self):
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "test_a.py").write_text(
            "from a import value\nassert value == 2\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "init"], cwd=repo, check=True)

        def worker(**kw):
            (Path(kw["cwd"]) / "a.py").write_text("value = 2\n", encoding="utf-8")
            return "a" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        a = code_task("a", "a.py", prompt="a-task") | {"satisfies": []}
        a["checks"] = [{"name": "unit", "argv": ["python3", "test_a.py"]}]
        b = self._text_task("b", depends=["a"], satisfies=["g1"])
        self.apply(eng, store, dispatch(a, b))
        rec = eng.state["tasks"]["a"]
        self.assertEqual(rec["status"], "completed", rec.get("last_error"))
        ev = self._evidence_for(eng, "b")
        patch = store.dir / rec["changes"]["patch_path"]
        patch.write_text("tampered patch\n", encoding="utf-8")
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("b -> a" in e for e in errors), errors)
        self.assertTrue(any("补丁" in e for e in errors), errors)


class IsolatePolicy(LoopCase):
    def _repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        return repo

    def test_isolate_none_is_rejected_before_run_state_exists(self):
        repo = self._repo()
        with self.assertRaises(config.ConfigError):
            self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                        "reviewer": [], "critic": []},
                       workspace={"repo": str(repo), "isolate": "none"})
        self.assertFalse((self.root / "run" / "state.json").exists())

    def test_loop_workspace_isolate_none_is_rejected(self):
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps({
            "loop": {"workspace": {"isolate": "none", "require_git_for_code": True}},
        }), encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load_config(str(cfg))

    def test_cli_no_longer_offers_isolate_none(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "swarm_loop.py"
        result = subprocess.run(
            [sys.executable, "-I", str(script), "start", str(self.root / "run"),
             "--goal-text", "x", "--criteria", "g1", "--isolate", "none"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid choice", result.stderr)

    def test_cli_goal_workspace_none_fails_before_backends(self):
        goal = self.root / "goal.json"
        goal.write_text(json.dumps({
            "goal": "x", "completion_criteria": ["g1"],
            "workspace": {"repo": str(self.root / "repo"), "isolate": "none"},
        }), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "swarm_loop.py"
        result = subprocess.run(
            [sys.executable, "-I", str(script), "start", str(self.root / "run"),
             "--goal", str(goal)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isolate", result.stderr)
        self.assertFalse((self.root / "run" / "state.json").exists())

    def test_non_git_code_still_uses_explicit_snapshot_behavior(self):
        repo = self._repo()
        (repo / "a.py").write_text("value = 1\n", encoding="utf-8")

        def worker(**kw):
            (Path(kw["cwd"]) / "a.py").write_text("value = 2\n", encoding="utf-8")
            return "a" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo),
                                           "require_git_for_code": False})
        self.apply(eng, store, dispatch(code_task("a", "a.py")))
        rec = eng.state["tasks"]["a"]
        self.assertEqual(rec["status"], "unverified")
        self.assertEqual(rec["changes"]["kind"], "snapshot")
        self.assertFalse((store.dir / "worktrees").exists())


if __name__ == "__main__":
    unittest.main()
