"""Live progress: stream parsing, the status file and page, watch, and both runners.

No API calls. Run from the repo root:  python3 -I -m unittest discover -s tests -v
"""
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import swarm  # noqa: E402
from swarmlib import live, procs  # noqa: E402


def ev(event):
    return json.dumps({"type": "stream_event", "event": event}, ensure_ascii=False)


def stream(final_text="并联等效 2 Ω。", thinking=("先算", "1/6+1/3")):
    """A two-message call shaped like the real CLI output: think, Read, then answer."""
    yield ev({"type": "message_start", "message": {"usage": {"input_tokens": 600, "output_tokens": 0}}})
    yield ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}})
    for part in thinking:
        yield ev({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "thinking_delta", "thinking": part}})
    yield ev({"type": "content_block_stop", "index": 0})
    yield ev({"type": "content_block_start", "index": 1,
              "content_block": {"type": "tool_use", "name": "Read", "input": {}}})
    yield ev({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "/w/notes.txt"}'}})
    yield ev({"type": "content_block_stop", "index": 1})
    yield ev({"type": "message_delta", "usage": {"input_tokens": 600, "output_tokens": 40}})
    yield json.dumps({"type": "user", "message": {"content": []}})
    yield ev({"type": "message_start", "message": {"usage": {"input_tokens": 200,
                                                             "cache_read_input_tokens": 500}}})
    yield ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    yield ev({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": final_text}})
    yield ev({"type": "message_delta", "usage": {"input_tokens": 200, "cache_read_input_tokens": 500,
                                                 "output_tokens": 12}})
    yield json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": final_text,
                      "num_turns": 2, "usage": {"input_tokens": 800, "cache_read_input_tokens": 500,
                                                "output_tokens": 52}}, ensure_ascii=False)


def price(usage, model):
    return (usage.get("input_tokens", 0) * 0.3 + usage.get("cache_read_input_tokens", 0) * 0.006
            + usage.get("output_tokens", 0) * 1.2) / 1e6


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.board = live.LiveBoard(self.dir, price=price, flush_every=60)

    def status(self):
        return json.loads((self.dir / "live" / "status.json").read_text())


class StreamParsing(Board):
    def test_thinking_tools_and_text_are_tracked(self):
        tr = self.board.begin("q1", "执行", "deepseek-flash", 1)
        lines = list(stream())
        for line in lines[:4]:                      # still thinking
            tr.on_line(line)
        self.assertEqual(tr.activity, "思考中")
        self.assertEqual(tr.thinking_chars, len("先算1/6+1/3"))
        self.assertIn("1/6+1/3", tr.tail)
        usage, estimated = tr.usage()
        self.assertTrue(estimated)
        self.assertEqual(usage["input_tokens"], 600)
        self.assertGreater(usage["output_tokens"], 0)  # estimated from what streamed so far
        for line in lines[4:-1]:
            tr.on_line(line)
        self.assertEqual((tr.tool_calls, tr.last_tool), (1, "Read notes.txt"))
        self.assertEqual(tr.text_chars, len("并联等效 2 Ω。"))
        usage, estimated = tr.usage()
        self.assertFalse(estimated)                  # both messages reported their usage
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (800, 52))
        tr.on_line(lines[-1])
        tr.close()
        snap = self.status()
        row = snap["tasks"][0]
        self.assertEqual((row["id"], row["state"], row["calls"]), ("q1", "idle", 1))
        self.assertAlmostEqual(snap["totals"]["cost_usd"], price({"input_tokens": 800,
                               "cache_read_input_tokens": 500, "output_tokens": 52}, ""), places=6)

    def test_garbage_lines_are_ignored(self):
        tr = self.board.begin("q1", "执行", "m")
        for line in ("not json", "[]", "{}", '{"type": "stream_event"}'):
            tr.on_line(line)
        tr.close({"usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertEqual(self.status()["tasks"][0]["calls"], 1)

    def test_final_result(self):
        text = "\n".join(stream("答"))
        self.assertEqual(live.final_result(text)["result"], "答")
        self.assertEqual(live.final_result('{"result": "x", "is_error": false}')["result"], "x")
        self.assertIsNone(live.final_result(ev({"type": "message_start"})))
        self.assertIsNone(live.final_result(""))
        self.assertIsNone(live.final_result("API Error: 500"))


class Snapshot(Board):
    def test_states_cost_and_finish(self):
        self.board.register(["a", "b", "c"])
        tr = self.board.begin("a", "执行", "m", 1)
        tr.on_line(ev({"type": "message_start", "message": {"usage": {"input_tokens": 1000}}}))
        snap = self.board.snapshot()
        self.assertEqual((snap["totals"]["running"], snap["totals"]["waiting"]), (1, 2))
        self.assertTrue(snap["totals"]["cost_estimated"])
        self.assertGreater(snap["totals"]["cost_usd"], 0)
        tr.close()
        self.board.task_done("a", "ok")
        self.board.note("b", "运行检查命令")
        self.board.finish()
        snap = self.status()
        self.assertTrue(snap["finished"])
        states = {r["id"]: r["state"] for r in snap["tasks"]}
        self.assertEqual(states, {"a": "done", "b": "running", "c": "waiting"})
        self.assertIn("完成 1/3", live.progress_line(snap))

    def test_unknown_price_is_reported_not_guessed(self):
        board = live.LiveBoard(self.dir, price=lambda u, m: None, flush_every=60)
        tr = board.begin("a", "执行", "mystery-model")
        tr.close({"usage": {"input_tokens": 10, "output_tokens": 10}})
        snap = board.snapshot()
        self.assertEqual(snap["totals"]["cost_unknown_calls"], 1)
        self.assertIn("费用未知", live.progress_line(snap))

    def test_page_escapes_and_redacts(self):
        tr = self.board.begin("x", "执行", "m")
        tr.on_line(ev({"type": "content_block_start", "content_block": {"type": "thinking"}}))
        tr.on_line(ev({"type": "content_block_delta", "delta": {
            "type": "thinking_delta", "thinking": "<script>alert(1)</script> key sk-abcdefghijklmnop1234"}}))
        self.board.flush()
        page = (self.dir / "live" / "index.html").read_text()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("sk-abcdefghijklmnop1234", page)
        self.assertNotIn("sk-abcdefghijklmnop1234", (self.dir / "live" / "status.json").read_text())
        self.assertIn('http-equiv="refresh"', page)
        tr.close()
        self.board.finish()
        self.assertIn('content="5"', (self.dir / "live" / "index.html").read_text())

    def test_nothing_is_written_before_a_call(self):
        live.LiveBoard(self.dir).finish()
        self.assertFalse((self.dir / "live").exists())

    def test_ticker_echoes_progress(self):
        lines = []
        board = live.LiveBoard(self.dir, price=price, echo=lines.append, echo_every=0.05, flush_every=0.02)
        tr = board.begin("a", "执行", "m")
        for _ in range(100):
            if lines:
                break
            __import__("time").sleep(0.02)
        tr.close()
        board.finish()
        self.assertTrue(lines and lines[0].startswith("[进度"))


class Watch(Board):
    def test_once_and_path_forms(self):
        tr = self.board.begin("q1", "复核", "m", 2)
        tr.on_line(ev({"type": "content_block_start", "content_block": {"type": "thinking"}}))
        tr.on_line(ev({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "核对第二问"}}))
        self.board.flush()
        for path in (self.dir, self.dir / "live", self.dir / "live" / "status.json"):
            out = io.StringIO()
            self.assertEqual(live.watch(path, once=True, thinking=True, out=out), 0)
            text = out.getvalue()
            self.assertIn("q1：复核 第2轮 · 思考中", text)
            self.assertIn("最近在想：…核对第二问", text)
        tasks = self.dir.parent / (self.dir.name + ".json")
        results = self.dir.parent / (self.dir.name + "-results")
        try:
            (results / "live").mkdir(parents=True)
            (results / "live" / "status.json").write_text((self.dir / "live" / "status.json").read_text())
            tasks.write_text("{}")
            out = io.StringIO()
            self.assertEqual(live.watch(tasks, once=True, out=out), 0)   # a tasks file finds its results
        finally:
            tasks.unlink(missing_ok=True)
            __import__("shutil").rmtree(results, ignore_errors=True)
        tr.close()

    def test_missing_status_is_explained(self):
        out = io.StringIO()
        self.assertEqual(live.watch(self.dir / "nowhere", once=True, out=out), 1)
        self.assertIn("找不到实时进度文件", out.getvalue())


class StreamingProcess(unittest.TestCase):
    def test_lines_arrive_through_the_callback(self):
        seen = []
        code = "import sys,time\nfor i in range(3):\n print(sys.stdin.readline().strip()+str(i), flush=True); time.sleep(0.05)"
        res = procs.run([sys.executable, "-c", code], input_text="x\n", timeout=20,
                        on_stdout_line=seen.append)
        self.assertEqual(res["code"], 0)
        self.assertEqual([s.strip() for s in seen], ["x0", "1", "2"])
        self.assertEqual(res["stdout"], "".join(seen))

    def test_callback_errors_do_not_break_the_run(self):
        def boom(line):
            raise RuntimeError("display bug")
        res = procs.run([sys.executable, "-c", "print('a'); import sys; print('b', file=sys.stderr)"],
                        timeout=20, on_stdout_line=boom)
        self.assertEqual((res["code"], res["stdout"].strip(), res["stderr"].strip()), (0, "a", "b"))

    def test_timeout_still_stops_a_streaming_command(self):
        res = procs.run([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"],
                        timeout=1, on_stdout_line=lambda line: None)
        self.assertTrue(res["timed_out"])
        self.assertIn("start", res["stdout"])


class OneShotRunner(unittest.TestCase):
    def test_batch_writes_live_progress(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = Path(d) / "tasks.json"
            tasks.write_text(json.dumps({"tasks": [
                {"id": "a", "prompt": "p", "cwd": d}, {"id": "b", "prompt": "p", "cwd": d,
                                                       "depends_on": ["a"]}]}))
            seen = []

            class StreamingRunner:
                def __init__(self, args):
                    self.args = args

                def call(self, prompt, task, tools, model, tracker=None):
                    if tracker is not None:
                        for line in stream(f"结果-{task['id']}"):
                            tracker.on_line(line)
                        seen.append(task["id"])
                    return {"ok": True, "text": f"结果-{task['id']}", "seconds": 0.1,
                            "usage": {"input_tokens": 800, "output_tokens": 52}}

            a = types.SimpleNamespace(
                tasks=str(tasks), only=None, out=None, concurrency=2, model="deepseek-flash",
                review_model=None, review=True, tools="read", max_chars=None, retries=0,
                max_rework=0, cwd=None, max_turns=5, timeout=60, keep_sessions=False,
                max_tasks=50, progress_every=0)
            with mock.patch.object(swarm, "Runner", StreamingRunner), mock.patch("builtins.print"):
                self.assertEqual(swarm.cmd_run(a), 0)
            self.assertEqual(seen, ["a", "b"])
            snap = json.loads((Path(d) / "tasks-results" / "live" / "status.json").read_text())
            self.assertTrue(snap["finished"])
            self.assertEqual({r["id"]: (r["state"], r["status"]) for r in snap["tasks"]},
                             {"a": ("done", "unreviewed"), "b": ("done", "unreviewed")})
            self.assertGreater(snap["totals"]["cost_usd"], 0)
            self.assertTrue((Path(d) / "tasks-results" / "live" / "index.html").exists())

    def test_runner_command_streams(self):
        with mock.patch.object(swarm, "find_claude", return_value="/bin/true"), \
             mock.patch.object(swarm, "get_key", return_value="sk-test"):
            r = swarm.Runner(types.SimpleNamespace(max_output_tokens=1000, max_turns=3, timeout=5,
                                                   keep_sessions=False, cwd="/tmp"))
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            for line in stream("ok"):
                kw["on_stdout_line"](line + "\n")
            return {"code": 0, "stdout": "\n".join(stream("ok")), "stderr": "", "seconds": 0.1}

        board = live.LiveBoard(tempfile.mkdtemp(), price=price, flush_every=60)
        tracker = board.begin("t", "执行", "deepseek-flash")
        with mock.patch.object(swarm._procs, "run", fake_run):
            res = r.call("hi", {"cwd": "/tmp"}, "none", "deepseek-flash", tracker=tracker)
        self.assertTrue(res["ok"])
        self.assertEqual(res["text"], "ok")
        self.assertIn("stream-json", captured["cmd"])
        self.assertIn("--include-partial-messages", captured["cmd"])
        self.assertEqual(tracker.tool_calls, 1)


class LoopRunner(unittest.TestCase):
    def test_fake_loop_run_writes_live_progress(self):
        demo = ROOT / "examples" / "loop" / "fake"
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "run"
            base = [sys.executable, "-I", str(ROOT / "scripts" / "swarm_loop.py")]
            fake = ["--allow-fake-backend", "--fake-script", str(demo / "fake-script.json")]
            subprocess.run(base + ["start", str(run), "--goal", str(demo / "goal.json"),
                                   "--config", str(demo / "config.json"), *fake],
                           cwd=ROOT, check=True, capture_output=True)
            subprocess.run(base + ["decide", str(run), str(demo / "decision.dispatch.json"), *fake],
                           cwd=ROOT, check=True, capture_output=True)
            snap = json.loads((run / "live" / "status.json").read_text())
            self.assertTrue(snap["finished"])
            self.assertEqual([r["id"] for r in snap["tasks"]], ["facts"])   # reviewer call counted under its task
            self.assertEqual(snap["tasks"][0]["calls"], 2)
            out = subprocess.run(base + ["watch", str(run), "--once"], cwd=ROOT, check=True,
                                 capture_output=True, text=True).stdout
            self.assertIn("facts：已结束", out)


if __name__ == "__main__":
    unittest.main()
