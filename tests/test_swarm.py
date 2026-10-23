"""Offline tests for scripts/swarm.py. No API calls: a fake runner plays the workers.

Run from the repo root:  python3 -I -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import swarm  # noqa: E402

PASS = '```json\n{"pass": true, "criteria": [{"criterion": "a", "met": true, "note": "符合"}], "errors": []}\n```'
FAIL = '```json\n{"pass": false, "errors": ["错了"], "fix_instructions": "改"}\n```'
SELFCHECK = '\n```selfcheck\n{"met": [], "not_met": [], "uncertain": []}\n```'


def args(**kw):
    base = dict(model="deepseek-flash", review_model=None, review=True, tools="read", max_chars=None,
                retries=1, max_rework=2, cwd=None, max_turns=5, timeout=60, keep_sessions=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def ok(text):
    return {"ok": True, "text": text, "usage": {"input_tokens": 10, "output_tokens": 10}, "seconds": 0.1}


def err(msg="API Error: 429"):
    return {"ok": False, "error": msg, "seconds": 0.1}


class FakeRunner:
    """Answers each call from a per-kind script: worker, review or shrink."""

    def __init__(self, a, worker=(), review=(), shrink=()):
        self.args = a
        self.script = {"worker": list(worker), "review": list(review), "shrink": list(shrink)}
        self.calls = []

    def call(self, prompt, task, tools, model):
        if prompt.startswith(swarm.REVIEW_RULES):
            kind = "review"
        elif prompt.startswith(swarm.SHRINK_RULES):
            kind = "shrink"
        else:
            kind = "worker"
        self.calls.append((kind, prompt, tools))
        if not self.script[kind]:
            raise AssertionError(f"unexpected {kind} call")
        return self.script[kind].pop(0)

    def count(self, kind):
        return sum(1 for k, _, _ in self.calls if k == kind)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        patcher = mock.patch.object(swarm.time, "sleep")   # retries back off; skip the wait
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def task(self, **kw):
        t = {"id": "q1", "prompt": "做题", "acceptance": ["a"], "cwd": self.tmp.name}
        t.update(kw)
        return t


class ParseReview(unittest.TestCase):
    def test_string_false_does_not_pass(self):
        for raw in ('"false"', '"False"', '"no"', '"不通过"', '0', 'null', '"maybe"'):
            r = swarm.parse_review(f'```json\n{{"pass": {raw}, "errors": []}}\n```')
            self.assertFalse(r["pass"], raw)

    def test_explicit_yes_passes(self):
        for raw in ('true', '"true"', '" TRUE "', '"yes"', '"通过"', '1'):
            r = swarm.parse_review(f'```json\n{{"pass": {raw}, "errors": []}}\n```')
            self.assertTrue(r["pass"], raw)

    def test_pass_with_unmet_criterion_becomes_fail(self):
        text = '```json\n{"pass": true, "criteria": [{"criterion": "(b) 求 V_A", "met": "false"}], "errors": []}\n```'
        r = swarm.parse_review(text)
        self.assertFalse(r["pass"])
        self.assertIn("(b) 求 V_A", r["errors"][-1])

    def test_unparsable_is_marked(self):
        r = swarm.parse_review("我觉得挺好的")
        self.assertFalse(r["pass"])
        self.assertIn("unparsed", r)


class LoadTasks(unittest.TestCase):
    def load(self, tasks):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"tasks": tasks}, f)
        self.addCleanup(Path(f.name).unlink)
        return swarm.load_tasks(Path(f.name))

    def test_rejects_unsafe_ids(self):
        for bad in ("../x", "a/b", "a.b", "x.v1", "", "a b", 5, "x" * 65):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.load([{"id": bad, "prompt": "p"}])

    def test_accepts_safe_ids_and_defaults(self):
        tasks, _ = self.load([{"id": "q1_a-2", "prompt": "p"}, {"prompt": "p"}])
        self.assertEqual([t["id"] for t in tasks], ["q1_a-2", "task2"])

    def test_rejects_non_bool_review(self):
        for bad in ("false", 0, None):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.load([{"id": "a", "prompt": "p", "review": bad}])

    def test_rejects_bad_max_chars(self):
        for bad in (0, -5, "800", 1.5, True):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                self.load([{"id": "a", "prompt": "p", "max_chars": bad}])


class ReviewRetry(Base):
    def test_review_api_error_is_retried_without_rework(self):
        r = FakeRunner(args(), worker=[ok("答案" + SELFCHECK)], review=[err(), ok(PASS)])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["rounds"], 1)
        self.assertEqual(r.count("worker"), 1)
        self.assertEqual(r.count("review"), 2)

    def test_review_that_keeps_failing_is_unreviewed_not_reworked(self):
        msg = "API Error: Claude's response exceeded the 32000 output token maximum."
        r = FakeRunner(args(), worker=[ok("答案" + SELFCHECK)], review=[err(msg), err(msg)])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual(rec["status"], "unreviewed")
        self.assertEqual(rec["unreviewed_reason"], "复核失败")
        self.assertIn("32000", rec["review_error"])
        self.assertEqual(r.count("worker"), 1)           # no rework round was spent
        self.assertEqual((self.out / "q1.md").read_text(), "答案")
        self.assertFalse((self.out / "q1.v1.review.json").exists())

    def test_unparsable_verdict_is_asked_again(self):
        r = FakeRunner(args(), worker=[ok("答案")], review=[ok("看起来不错"), ok(PASS)])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["rounds"], 1)

    def test_real_fail_still_reworks(self):
        r = FakeRunner(args(), worker=[ok("v1"), ok("v2")], review=[ok(FAIL), ok(PASS)])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual((rec["status"], rec["rounds"]), ("ok", 2))
        self.assertEqual((self.out / "q1.md").read_text(), "v2")
        self.assertIn("## 复核意见", r.calls[2][1])      # the rework prompt carries the review

    def test_worker_error_is_retried(self):
        r = FakeRunner(args(), worker=[err(), ok("答案")], review=[ok(PASS)])
        self.assertEqual(swarm.run_task(self.task(), "", r, self.out)["status"], "ok")

    def test_negative_retries_does_not_crash(self):
        r = FakeRunner(args(retries=-1), worker=[ok("答案")], review=[ok(PASS)])
        self.assertEqual(swarm.run_task(self.task(), "", r, self.out)["status"], "ok")


class Unreviewed(Base):
    def test_no_acceptance(self):
        r = FakeRunner(args(), worker=[ok("草稿")])
        rec = swarm.run_task(self.task(acceptance=[]), "", r, self.out)
        self.assertEqual((rec["status"], rec["unreviewed_reason"]), ("unreviewed", "没有 acceptance"))
        self.assertNotIn("review_error", rec)

    def test_task_review_false_skips_reviewer_but_shows_criteria(self):
        r = FakeRunner(args(), worker=[ok("想法" + SELFCHECK)])
        rec = swarm.run_task(self.task(review=False), "", r, self.out)
        self.assertEqual((rec["status"], rec["unreviewed_reason"]), ("unreviewed", "任务设了 review: false"))
        self.assertEqual(r.count("review"), 0)
        self.assertIn("## 验收标准", r.calls[0][1])     # the worker still checks itself against them
        self.assertIsNotNone(rec["selfcheck"])

    def test_no_review_flag(self):
        r = FakeRunner(args(review=False), worker=[ok("草稿")])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual((rec["status"], rec["unreviewed_reason"]), ("unreviewed", "--no-review"))


class Length(Base):
    def test_count_ignores_whitespace(self):
        self.assertEqual(swarm.count_chars("a b\n c\t中 文"), 5)

    def test_over_long_result_is_compressed_before_review(self):
        r = FakeRunner(args(), worker=[ok("长" * 300 + SELFCHECK)], shrink=[ok("短" * 80)], review=[ok(PASS)])
        rec = swarm.run_task(self.task(max_chars=100), "", r, self.out)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual((rec["chars"], rec["shrinks"]), (80, 1))
        review_prompt = next(p for k, p, _ in r.calls if k == "review")
        self.assertIn("短" * 80, review_prompt)
        self.assertNotIn("长" * 300, review_prompt)
        self.assertIn("不要自己数字数", review_prompt)
        shrink_tools = next(t for k, _, t in r.calls if k == "shrink")
        self.assertEqual(shrink_tools, "none")
        self.assertIsNotNone(rec["selfcheck"])        # the worker's self-check survives shrinking

    def test_result_that_stays_too_long_fails_without_reviewer(self):
        r = FakeRunner(args(max_rework=0), worker=[ok("长" * 300)], shrink=[ok("长" * 200), ok("长" * 150)])
        rec = swarm.run_task(self.task(max_chars=100), "", r, self.out)
        self.assertEqual(rec["status"], "unverified")
        self.assertEqual(r.count("review"), 0)
        verdict = json.loads((self.out / "q1.v1.review.json").read_text())
        self.assertEqual(verdict["checked_by"], "script")
        self.assertIn("150", verdict["errors"][0])

    def test_short_result_skips_shrink(self):
        r = FakeRunner(args(max_chars=100), worker=[ok("短答")], review=[ok(PASS)])
        rec = swarm.run_task(self.task(), "", r, self.out)
        self.assertEqual((rec["status"], rec["shrinks"]), ("ok", 0))

    def test_worker_is_told_the_limit(self):
        p = swarm.worker_prompt(self.task(), "", limit=500)
        self.assertIn("不超过 500 字", p)
        self.assertIn("多想少说", p)
        self.assertNotIn("## 篇幅上限", swarm.worker_prompt(self.task(), ""))


class Files(Base):
    def test_rerun_does_not_delete_a_similar_id(self):
        (self.out / "x-1.md").write_text("keep")
        (self.out / "x.v1.md").write_text("stale")
        r = FakeRunner(args(), worker=[ok("新")])
        swarm.run_task(self.task(id="x", acceptance=[]), "", r, self.out)
        self.assertEqual((self.out / "x-1.md").read_text(), "keep")
        self.assertEqual((self.out / "x.md").read_text(), "新")


class RunnerEnv(unittest.TestCase):
    def test_output_token_cap_is_passed_to_workers(self):
        with mock.patch.object(swarm, "find_claude", return_value="/bin/true"), \
             mock.patch.object(swarm, "get_key", return_value="sk-test"):
            r = swarm.Runner(types.SimpleNamespace(max_output_tokens=12345))
            self.assertEqual(r.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "12345")
            r = swarm.Runner(types.SimpleNamespace())          # `check` has no such flag
            self.assertEqual(r.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], str(swarm.DEFAULT_MAX_OUTPUT_TOKENS))


class Summary(Base):
    def test_summary_lists_unreviewed_and_review_failures(self):
        tasks_file = self.out / "tasks.json"
        tasks_file.write_text(json.dumps({"tasks": [
            {"id": "good", "prompt": "p", "acceptance": ["a"], "cwd": self.tmp.name},
            {"id": "flaky", "prompt": "p", "acceptance": ["a"], "cwd": self.tmp.name},
            {"id": "draft", "prompt": "p", "cwd": self.tmp.name},
        ]}))

        class Scripted(FakeRunner):
            def call(self, prompt, task, tools, model):
                reviewing = prompt.startswith(swarm.REVIEW_RULES)
                if task["id"] == "flaky" and reviewing:
                    return err()
                return ok(PASS if reviewing else "答案")

        a = args(tasks=str(tasks_file), only=None, out=None, concurrency=3)
        with mock.patch.object(swarm, "Runner", lambda _a: Scripted(_a)), \
             mock.patch("builtins.print"):
            code = swarm.cmd_run(a)
        summary = json.loads((self.out / "tasks-results" / "summary.json").read_text())
        self.assertEqual(summary["passed"], ["good"])
        self.assertEqual(sorted(summary["unreviewed"]), ["draft", "flaky"])
        self.assertEqual(summary["review_failed"], ["flaky"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
