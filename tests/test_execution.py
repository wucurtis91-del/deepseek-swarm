"""Regression tests for the original one-shot execution layer and its known bugs."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import swarm  # noqa: E402
from test_swarm import FakeRunner, args, ok  # noqa: E402


class CheckEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.result = self.dir / "result.md"
        self.result.write_text("42")

    def check(self, **task):
        task.setdefault("check_timeout", 5)
        return swarm.run_check(task, self.dir, self.result)

    def test_does_not_inherit_unlisted_credentials(self):
        with mock.patch.dict(os.environ, {"MOCK_SECRET_TOKEN": "very-secret-value"}, clear=False):
            r = self.check(check_argv=["sh", "-c", 'printf %s "$MOCK_SECRET_TOKEN"'])
        self.assertTrue(r["pass"])
        self.assertNotIn("very-secret-value", r["output"])

    def test_redacts_explicit_check_env_values(self):
        r = self.check(check_argv=["sh", "-c", 'printf %s "$MY_TOKEN"'],
                       check_env={"MY_TOKEN": "abc123-very-secret"})
        self.assertNotIn("abc123-very-secret", r["output"])
        self.assertIn("[REDACTED]", r["output"])

    def test_hostile_filename_is_not_executed_by_shell(self):
        name = "$(touch AUDIT_MARKER).txt"
        (self.dir / name).write_text("hello")
        task = {"check_cmd": "cat {path}", "_placeholders": {"{path}": name},
                "check_timeout": 5}
        r = swarm.run_check(task, self.dir, self.result)
        self.assertTrue(r["pass"], r)
        self.assertFalse((self.dir / "AUDIT_MARKER").exists())
        self.assertEqual(r["output"], "hello")

    def test_filename_with_spaces_works_in_argv_form(self):
        name = "a b.txt"
        (self.dir / name).write_text("spaced")
        task = {"check_argv": ["cat", "{path}"], "_placeholders": {"{path}": name},
                "check_timeout": 5}
        r = swarm.run_check(task, self.dir, self.result)
        self.assertTrue(r["pass"], r)
        self.assertEqual(r["output"], "spaced")

    def test_result_placeholder_is_absolute_even_with_another_cwd(self):
        sub = self.dir / "work"
        sub.mkdir()
        task = {"check_cmd": "grep -q 42 {result}", "check_timeout": 5}
        r = swarm.run_check(task, sub, self.result)
        self.assertTrue(r["pass"], r)

    def test_inputs_expansion_keeps_check_placeholders_safe(self):
        name = "$(touch EXPANDED_MARKER).txt"
        (self.dir / name).write_text("safe")
        spec = {"tasks": [{
            "id": "f-{stem}", "inputs": "*.txt", "cwd": str(self.dir),
            "prompt": "read {path}", "check_cmd": "cat {path}",
        }]}
        path = self.dir / "tasks.json"
        path.write_text(json.dumps(spec))
        tasks, _ = swarm.load_tasks(path)
        self.assertEqual(tasks[0]["check_cmd"], "cat {path}")
        result = self.dir / "out.md"
        result.write_text("x")
        r = swarm.run_check(tasks[0], self.dir, result)
        self.assertTrue(r["pass"], r)
        self.assertFalse((self.dir / "EXPANDED_MARKER").exists())

    def test_timeout_kills_descendants_so_no_late_side_effect(self):
        task = {"check_cmd": "sh -c 'sleep 1; touch late_marker'", "check_timeout": 0.3}
        r = swarm.run_check(task, self.dir, self.result)
        self.assertFalse(r["pass"])
        time.sleep(1.2)
        self.assertFalse((self.dir / "late_marker").exists())


class ReviewStrictness(unittest.TestCase):
    def test_missing_criteria_cannot_be_a_pass(self):
        r = swarm.parse_review('```json\n{"pass": true, "errors": []}\n```', ["a"])
        self.assertFalse(r["pass"])
        self.assertFalse(r["valid"])

    def test_errors_with_pass_true_cannot_pass(self):
        text = ('```json\n{"pass": true, "criteria": [{"criterion": "a", "met": true}],'
                ' "errors": ["real problem"]}\n```')
        r = swarm.parse_review(text, ["a"])
        self.assertFalse(r["pass"])
        self.assertTrue(r["valid"])      # a contradiction is a usable fail verdict

    def test_explicit_fail_without_criteria_is_still_a_fail(self):
        r = swarm.parse_review('```json\n{"pass": false, "errors": ["bad"]}\n```', ["a"])
        self.assertFalse(r["pass"])
        self.assertTrue(r["valid"])

    def test_criteria_must_match_exactly(self):
        text = ('```json\n{"pass": true, "criteria": [{"criterion": "a", "met": true},'
                ' {"criterion": "extra", "met": true}], "errors": []}\n```')
        r = swarm.parse_review(text, ["a"])
        self.assertFalse(r["valid"])


class ExceptionIsolation(unittest.TestCase):
    def test_one_task_exception_does_not_destroy_the_batch(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tasks.json"
            path.write_text(json.dumps({"tasks": [
                {"id": "a", "prompt": "p", "cwd": d},
                {"id": "b", "prompt": "p", "cwd": d},
            ]}))

            class Scripted(FakeRunner):
                def call(self, prompt, task, tools, model):
                    if task["id"] == "a":
                        raise RuntimeError("boom")
                    return ok("结果-b")

            a = args(tasks=str(path), only=None, out=None, concurrency=2)
            with mock.patch.object(swarm, "Runner", lambda _a: Scripted(_a)), \
                 mock.patch("builtins.print"):
                code = swarm.cmd_run(a)
            summary = json.loads((Path(d) / "tasks-results" / "summary.json").read_text())
            self.assertEqual(code, 1)
            self.assertIn("a", summary["failed"])
            self.assertIn("b", summary["unreviewed"])
            self.assertEqual(summary["tasks"][0]["id"], "a")
            self.assertIn("boom", summary["tasks"][0]["error"])


class CCSwitch(unittest.TestCase):
    def test_null_and_invalid_rows_do_not_crash(self):
        with tempfile.TemporaryDirectory() as home:
            db = Path(home) / ".cc-switch" / "cc-switch.db"
            db.parent.mkdir()
            con = sqlite3.connect(db)
            con.execute("create table providers (app_type text, settings_config text)")
            con.executemany("insert into providers values (?,?)", [
                ("claude", None),
                ("claude", "null"),
                ("claude", '{"env": null}'),
                ("claude", "not json"),
            ])
            con.commit()
            con.close()
            with mock.patch("swarm.Path.home", return_value=Path(home)):
                self.assertIsNone(swarm.key_from_cc_switch())
            con = sqlite3.connect(db)
            con.execute("insert into providers values (?,?)", (
                "claude", '{"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",'
                          ' "ANTHROPIC_API_KEY": "sk-found"}}'))
            con.commit()
            con.close()
            with mock.patch("swarm.Path.home", return_value=Path(home)):
                self.assertEqual(swarm.key_from_cc_switch(), "sk-found")
            with tempfile.TemporaryDirectory() as empty:
                with mock.patch("swarm.Path.home", return_value=Path(empty)), \
                     mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(SystemExit):
                        swarm.get_key()


class OnlyStale(unittest.TestCase):
    def test_rerunning_upstream_marks_downstream_stale(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "tasks.json"
            path.write_text(json.dumps({"tasks": [
                {"id": "a", "prompt": "p", "cwd": d},
                {"id": "b", "prompt": "p", "cwd": d, "depends_on": ["a"]},
            ]}))
            version = {"n": 0}

            class Scripted(FakeRunner):
                def call(self, prompt, task, tools, model):
                    if task["id"] == "a":
                        version["n"] += 1
                        return ok(f"结果-a-{version['n']}")
                    return ok("结果-b")

            a = args(tasks=str(path), only=None, out=None, concurrency=2)
            with mock.patch.object(swarm, "Runner", lambda _a: Scripted(_a)), \
                 mock.patch("builtins.print"):
                self.assertEqual(swarm.cmd_run(a), 0)
            a2 = args(tasks=str(path), only="a", out=None, concurrency=2)
            with mock.patch.object(swarm, "Runner", lambda _a: Scripted(_a)), \
                 mock.patch("builtins.print"):
                code = swarm.cmd_run(a2)
            summary = json.loads((Path(d) / "tasks-results" / "summary.json").read_text())
            self.assertEqual(code, 1)
            self.assertEqual(summary["stale"], ["b"])
            b = next(t for t in summary["tasks"] if t["id"] == "b")
            self.assertTrue(b["stale"])
            self.assertTrue(b["stale_reasons"])
            self.assertNotIn("b", summary["passed"])


class PathsAndPrices(unittest.TestCase):
    def test_relative_output_directory_is_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                path = Path(d) / "tasks.json"
                path.write_text(json.dumps({"tasks": [
                    {"id": "a", "prompt": "p", "cwd": d,
                     "check_cmd": "grep -q x {result}"},
                ]}))

                class Scripted(FakeRunner):
                    def call(self, prompt, task, tools, model):
                        return ok("x")

                a = args(tasks=str(path), only=None, out="rel-out", concurrency=1)
                with mock.patch.object(swarm, "Runner", lambda _a: Scripted(_a)), \
                     mock.patch("builtins.print"):
                    code = swarm.cmd_run(a)
                self.assertEqual(code, 0)
                out = Path(d) / "rel-out"
                self.assertTrue((out / "summary.json").exists())
                self.assertTrue((out / "a.md").exists())
                rec = json.loads((out / "summary.json").read_text())["tasks"][0]
                self.assertEqual(rec["status"], "ok")
                self.assertEqual(rec["check"]["exit"], 0)
            finally:
                os.chdir(old)

    def test_unknown_model_price_is_not_guessed(self):
        import datetime as dt
        self.assertIsNone(swarm.estimate_cost(
            {"input_tokens": 100, "output_tokens": 100}, "some-unknown-model",
            dt.datetime.now(dt.timezone.utc)))
        self.assertIsNotNone(swarm.estimate_cost(
            {"input_tokens": 100, "output_tokens": 100}, "deepseek-flash",
            dt.datetime.now(dt.timezone.utc)))


if __name__ == "__main__":
    unittest.main()
