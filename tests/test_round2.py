"""Regression tests for the second review round.

Each test is written against the behavior that was wrong, not against an implementation
detail. They are expected to fail before the corresponding fix.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmlib import backends, config, engine, packet as packetmod, protocol, state, util  # noqa: E402
from test_loop import LoopCase, ROLES, SELF, complete, dispatch, review_json, task  # noqa: E402
import harness  # noqa: E402


def code_task(tid, path, *, depends=None, prompt=None):
    return task(
        tid, kind="code", tools="edit", prompt=prompt or f"{tid}-task",
        allowed_paths=[path],
        acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
        acceptance_policy="deterministic",
        checks=[{"name": "unit", "argv": ["python3", "-c", "pass"]}],
        depends_on=depends or [],
    )


class CommitRecovery(LoopCase):
    def test_corrupt_snapshot_recovers_tasks_evidence_budget_and_decisions(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        before = copy.deepcopy(eng.state)
        self.assertTrue(before["tasks"]["t1"]["artifact"])
        self.assertGreaterEqual(before["evidence_seq"], 2)
        self.assertGreaterEqual(before["round"], 1)
        # A fresh run with only a handful of events must recover from the journal,
        # not from a periodic full snapshot.
        store.state_path.write_text("{broken", encoding="utf-8")
        after = state.RunStore(store.dir).load()
        for key in ("round", "status", "tasks", "evidence", "decisions", "budget",
                    "evidence_seq", "facts"):
            self.assertEqual(after[key], before[key], key)

    def test_event_patches_are_not_empty(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
        meaningful = [e for e in events if e.get("seq", 0) > 0 and not e.get("state")]
        self.assertTrue(meaningful)
        self.assertTrue(any(e.get("set") or e.get("del") for e in meaningful))

    def test_truncated_tail_can_be_recovered_then_appended_and_reloaded(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        with open(store.events_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 999, "type": "truncated')
        s = state.RunStore(store.dir)
        loaded = s.load()
        self.assertTrue(any("截断" in n or "不完整" in n for n in s.load_notes))
        old = copy.deepcopy(loaded)
        loaded["tail_marker"] = "continued"
        with s.lock():
            s.commit(old, loaded, "tail_marker", {})
        # The appended event itself must survive a damaged snapshot, proving the
        # repaired tail was not just silently skipped.
        s.state_path.write_text("{broken", encoding="utf-8")
        again = state.RunStore(store.dir).load()
        self.assertEqual(again.get("tail_marker"), "continued")
        self.assertEqual(again["tasks"]["t1"]["status"], "completed")


class PatchBaseline(LoopCase):
    def _repo(self):
        repo = self.root / "repo"
        repo.mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
            (repo / name).write_text(name + "-base\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "init"], cwd=repo, check=True)
        return repo

    def _worker(self, **kw):
        p = kw["prompt"]
        target = next((name for name in ("a", "b", "c", "d") if f"{name}-task" in p), None)
        if target:
            (Path(kw["cwd"]) / f"{target}.txt").write_text(f"{target}-changed\n")
        return "changed" + SELF

    def test_chain_builds_on_upstream_without_duplicating_patches(self):
        repo = self._repo()
        eng, store = self.start({"planner": [], "worker": self._worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        a = code_task("a", "a.txt")
        b = code_task("b", "b.txt", depends=["a"])
        c = code_task("c", "c.txt", depends=["b"])
        self.apply(eng, store, dispatch(a, b, c))
        for tid, own in (("a", "a.txt"), ("b", "b.txt"), ("c", "c.txt")):
            rec = eng.state["tasks"][tid]
            self.assertEqual(rec["status"], "completed", (tid, rec.get("last_error")))
            self.assertEqual([x["path"] for x in rec["changes"]["changed_files"]], [own])
            patch = (store.dir / rec["changes"]["patch_path"]).read_text()
            self.assertIn(own, patch)
            for other in ("a.txt", "b.txt", "c.txt"):
                if other != own:
                    self.assertNotIn(other, patch)
        self.assertTrue(eng.state["tasks"]["b"]["changes"].get("input_commit"))
        self.assertNotEqual(eng.state["tasks"]["b"]["changes"].get("input_commit"),
                            eng.state["tasks"]["b"]["changes"].get("base_commit"))

    def test_branch_merge_patch_contains_only_its_own_delta(self):
        repo = self._repo()
        eng, store = self.start({"planner": [], "worker": self._worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        a = code_task("a", "a.txt")
        b = code_task("b", "b.txt")
        d = code_task("d", "d.txt", depends=["a", "b"])
        self.apply(eng, store, dispatch(a, b, d))
        rec = eng.state["tasks"]["d"]
        self.assertEqual(rec["status"], "completed", rec.get("last_error"))
        self.assertEqual([x["path"] for x in rec["changes"]["changed_files"]], ["d.txt"])
        patch = (store.dir / rec["changes"]["patch_path"]).read_text()
        self.assertNotIn("a.txt", patch)
        self.assertNotIn("b.txt", patch)


class DrainInFlight(LoopCase):
    def test_two_tasks_started_from_one_ready_list_keep_their_own_state(self):
        calls = []

        def worker(**kw):
            calls.append(kw["prompt"])
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("a"), task("b")))
        self.assertEqual(len(calls), 2)
        for tid in ("a", "b"):
            self.assertEqual(eng.state["tasks"][tid]["attempts"], 1, tid)
            self.assertEqual(eng.state["tasks"][tid]["status"], "completed", tid)

    def test_paused_batch_still_persists_in_flight_results(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["budget"] = dict(cfg["budget"], max_calls=1)

        def worker(**kw):
            time.sleep(0.25)
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(task("a"), task("b")))
        self.assertEqual(eng.state["status"], "paused")
        self.assertFalse(any(t.get("status") == "running"
                             for t in eng.state["tasks"].values()),
                         {k: v.get("status") for k, v in eng.state["tasks"].items()})
        started = [t for t in eng.state["tasks"].values()
                   if t.get("artifact") or t.get("status") not in ("pending", "budget_blocked")]
        self.assertTrue(started)
        self.assertLessEqual(eng.state["budget"]["calls"], 1)

    def test_cancelled_batch_does_not_leave_running_rows(self):
        def worker(**kw):
            time.sleep(0.2)
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []})
        import threading
        timer = threading.Timer(0.05, eng.cancel_event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.apply(eng, store, dispatch(task("a")))
        self.assertEqual(eng.state["status"], "cancelled")
        self.assertFalse(any(t.get("status") == "running"
                             for t in eng.state["tasks"].values()),
                         {k: v.get("status") for k, v in eng.state["tasks"].items()})


class BudgetReservation(LoopCase):
    def test_per_role_limit_counts_in_flight_reservations(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["budget"] = dict(cfg["budget"], max_calls=20,
                             max_calls_per_role={"worker": 1})
        calls = []

        def worker(**kw):
            calls.append(1)
            time.sleep(0.05)
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(task("a"), task("b")))
        self.assertEqual(len(calls), 1)
        self.assertEqual(eng.state["budget"]["roles"]["worker"]["calls"], 1)

    def test_output_cap_is_coordinated_with_remaining_token_budget(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["budget"] = dict(cfg["budget"], max_tokens=20000)
        caps = []

        def worker(**kw):
            caps.append(kw.get("max_output_tokens"))
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(task("t1", acceptance=[], acceptance_policy="none")))
        self.assertTrue(caps)
        self.assertIsNotNone(caps[0])
        self.assertGreater(caps[0], 0)
        self.assertLess(caps[0], 64000)
        b = eng.state["budget"]
        self.assertEqual(b.get("token_limit_semantics"), "estimated")

    def test_unknown_price_calls_are_bounded_under_concurrency(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["prices"] = {}
        cfg["budget"] = dict(cfg["budget"], max_calls=20, max_unknown_calls=1)
        calls = []

        def worker(**kw):
            calls.append(1)
            time.sleep(0.05)
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(
            task("a", acceptance=[], acceptance_policy="none"),
            task("b", acceptance=[], acceptance_policy="none")))
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(eng.state["budget"]["cost_unknown_calls"], 1)


class TaskModel(LoopCase):
    def test_task_model_is_used_by_the_actual_backend(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, dispatch(task(
            "t1", acceptance=[], acceptance_policy="none", model="deepseek-v4-pro")))
        call = eng.backends["worker"].calls[0]
        self.assertEqual(call["model"], "deepseek-v4-pro")
        usage = [u for u in eng.state["usage_calls"] if u["role"] == "worker"][0]
        self.assertEqual(usage["model"], "deepseek-v4-pro")

    def test_real_cli_command_receives_effective_model_and_cap(self):
        real = {
            "planner": {"backend": "claude_cli", "provider": "anthropic",
                        "model": "claude-opus-5-5", "auth": "local_login", "tools": "read"},
            "worker": {"backend": "claude_cli", "provider": "deepseek",
                       "model": "deepseek-flash", "auth": "api_key",
                       "api_key_env": "DEEPSEEK_API_KEY",
                       "endpoint": "https://api.deepseek.com/anthropic", "tools": "read"},
            "reviewer": {"backend": "claude_cli", "provider": "deepseek",
                         "model": "deepseek-flash", "auth": "api_key",
                         "api_key_env": "DEEPSEEK_API_KEY",
                         "endpoint": "https://api.deepseek.com/anthropic", "tools": "read"},
            "critic": {"backend": "claude_cli", "provider": "deepseek",
                       "model": "deepseek-flash", "auth": "api_key",
                       "api_key_env": "DEEPSEEK_API_KEY",
                       "endpoint": "https://api.deepseek.com/anthropic", "tools": "read"},
        }
        roles = config.validate_roles(real, allow_fake=False)
        payload = {
            "is_error": False, "result": "ok",
            "modelUsage": {"deepseek-v4-pro": {}},
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "output_tokens": 5},
        }
        fake = {"ok": True, "code": 0, "stdout": json.dumps(payload), "stderr": "",
                "output": json.dumps(payload), "seconds": 0.1, "timed_out": False,
                "cancelled": False}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}):
            backend = backends.ClaudeBackend(roles["worker"], claude="/bin/true")
            with mock.patch.object(backends.procs, "run", return_value=fake) as run:
                res = backend.call("prompt", cwd=d, tools="none",
                                   model="deepseek-v4-pro", max_output_tokens=1234)
            cmd = run.call_args.args[0]
            env = run.call_args.kwargs["env"]
        self.assertTrue(res["ok"], res)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek-v4-pro")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "1234")
        self.assertEqual(res["model_requested"], "deepseek-v4-pro")

    def test_cross_provider_task_model_is_rejected_before_call(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, dispatch(task(
            "t1", acceptance=[], acceptance_policy="none", model="claude-opus-5-5")))
        rec = eng.state["tasks"]["t1"]
        self.assertEqual(rec["status"], "exec_failed")
        self.assertIn("provider", rec["last_error"])
        self.assertFalse(eng.backends["worker"].calls)


class ContextPaging(LoopCase):
    def _artifact(self, eng, store, size=6000):
        text = "".join(f"{i:06d}\n" for i in range(size // 7 + 1))[:size]
        path = store.dir / "artifacts/t1/v1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        task_rec = protocol.normalize_task(task("t1"), eng.state["goal"]["criteria"], 1)
        task_rec["artifact"] = {"version": 1, "path": "artifacts/t1/v1.md",
                                "sha256": "x" * 64, "chars": size}
        task_rec["result_version"] = 1
        eng.state["tasks"]["t1"] = task_rec
        return text

    def test_offset_limit_returns_two_different_chunks(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        text = self._artifact(eng, store)
        n1 = eng._fulfill_context_requests([{
            "artifact": "t1@v1", "offset": 0, "limit": 1000}])[0]
        n2 = eng._fulfill_context_requests([{
            "artifact": "t1@v1", "offset": 1000, "limit": 1000}])[0]
        self.assertEqual(len(n1["content"]), 1000)
        self.assertEqual(len(n2["content"]), 1000)
        self.assertEqual(n1["content"], text[:1000])
        self.assertEqual(n2["content"], text[1000:2000])
        self.assertNotEqual(n1["content"], n2["content"])
        self.assertEqual(n1["next_offset"], 1000)

    def test_requested_content_survives_a_small_packet_budget(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        self._artifact(eng, store)
        eng._fulfill_context_requests([{"artifact": "t1@v1", "offset": 0, "limit": 800}])
        packet = packetmod.build_packet(eng.state, 500, run_dir=store.dir)
        notes = packet.get("context_notes") or []
        self.assertTrue(notes)
        self.assertEqual(len(notes[0].get("content") or ""), 800)
        self.assertTrue(packet["packet_meta"].get("context_over_budget"))


class HarnessFairness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reading_loop_goal_does_not_contain_reference_answers(self):
        r = harness.run_loop_reading(self.root / "reading")
        goal = r["state"]["goal"]
        blob = json.dumps(goal, ensure_ascii=False)
        for answer in ("2026-11-05", "2026-10-30", "2026-10-25", "Lin", "42"):
            self.assertNotIn(answer, blob)

    def test_diagnostic_goal_does_not_reveal_the_expected_cause(self):
        r = harness.run_loop_diagnostic(self.root / "diag")
        blob = json.dumps(r["state"]["goal"], ensure_ascii=False)
        self.assertNotIn("cause=errors", blob)
        self.assertNotIn("errors exceed threshold", blob)

    def test_legacy_diagnostic_tasks_depend_on_extract(self):
        r = harness.run_legacy_diagnostic(self.root / "legacy-diag")
        spec = json.loads((self.root / "legacy-diag" / "legacy-tasks.json").read_text())
        by_id = {t["id"]: t for t in spec["tasks"]}
        self.assertIn("extract", by_id["diag-errors"].get("depends_on", []))
        self.assertIn("extract", by_id["diag-latency"].get("depends_on", []))

    def test_reference_grader_is_not_in_the_worker_workspace(self):
        root = self.root / "reading"
        harness.setup_reading(root)
        self.assertFalse((root / "verify_reading.py").exists())
        grader = self.root / "graders" / "verify_reading.py"
        harness.write_reading_grader(grader)
        self.assertTrue(grader.exists())


    def test_real_budget_is_one_ledger_for_the_whole_run(self):
        b = harness.RealBudget(max_usd=0.01, max_calls=2)
        ok, _ = b.can_start(1, 0.004)
        self.assertTrue(ok)
        b.note({"calls": 1, "cost_usd_known": 0.004, "cost_unknown_calls": 0})
        b.note({"calls": 1, "cost_usd_known": 0.004, "cost_unknown_calls": 0})
        ok, why = b.can_start(1, 0.0)
        self.assertFalse(ok)
        self.assertIn("max_calls", why)
        b.note({"calls": 1, "cost_usd_known": 0.004, "cost_unknown_calls": 0})
        snap = b.snapshot()
        self.assertEqual(snap["calls"], 3)
        self.assertTrue(snap["overshoot"])
        self.assertEqual(snap["remaining"]["calls"], 0)


    def test_real_run_skips_approaches_when_global_budget_is_exhausted(self):
        b = harness.RealBudget(max_usd=1.0, max_calls=0)
        rows = harness.run_case("reading", "real", self.root / "budgeted",
                                real_cfg={"budget": {}}, run_budget=b)
        self.assertTrue(rows)
        self.assertTrue(all(r.get("skipped") for r in rows))
        self.assertIn("global max_calls", rows[0]["skip_reason"])


class TailRepeatedRepair(LoopCase):
    def test_three_commits_after_tail_damage_preserve_every_event(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        with open(store.events_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 999, "type": "truncated')

        s = state.RunStore(store.dir)
        with s.lock():
            st = s.load()
            self.assertTrue(any("截断" in n or "不完整" in n for n in s.load_notes))
            for i in range(3):
                old = copy.deepcopy(st)
                st["tail_marker"] = i
                st["tasks"][f"tail-{i}"] = {
                    "id": f"tail-{i}", "kind": "text", "version": 1,
                    "status": "pending", "depends_on": [], "prompt": "tail",
                    "acceptance": [], "acceptance_policy": "none",
                }
                st["evidence_seq"] = int(st.get("evidence_seq", 0)) + 1
                eid = f"ev-tail-{st['evidence_seq']}"
                st["evidence"][eid] = {
                    "id": eid, "seq": st["evidence_seq"], "kind": "artifact",
                    "task": f"tail-{i}", "verified": False, "completion_usable": False,
                }
                st["budget"].setdefault("tail_markers", []).append(i)
                st["decisions"].append({
                    "round": 800 + i, "action": "pause", "reason": "tail",
                    "target_task": None, "path": None, "external": True,
                })
                st = s.commit(old, st, "tail_repeated", {"i": i})
            # The same store now has no damaged tail to repair. Calling repair again
            # must not remove any of the three events just appended.
            s._repair_tail()
            s._repair_tail()
            expected = copy.deepcopy(st)

        again = state.RunStore(store.dir).load()
        for key in ("tail_marker", "tasks", "evidence", "budget", "decisions",
                    "evidence_seq", "round"):
            self.assertEqual(again[key], expected[key], key)

        events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
        self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        prev = ""
        for event in events:
            body = dict(event)
            sha = body.pop("sha")
            self.assertEqual(event["prev"], prev)
            self.assertEqual(sha, util.sha256_text(util.canonical(body)))
            prev = sha

        # The journal alone is now the recovery source; no periodic full snapshot
        # exists for these small runs.
        store.state_path.write_text("{broken", encoding="utf-8")
        rebuilt = state.RunStore(store.dir).load()
        for key in ("tail_marker", "tasks", "evidence", "budget", "decisions",
                    "evidence_seq", "round"):
            self.assertEqual(rebuilt[key], expected[key], key)


class RepairInheritance(LoopCase):
    BASE_ADD = "def add(a, b):\n    return -1"
    BASE_SUB = "def sub(a, b):\n    return -1"
    BAD_ADD = "def add(a, b):\n    return 0"
    BAD_SUB = "def sub(a, b):\n    return 0"
    OK_ADD = "def add(a, b):\n    return a + b"
    OK_SUB = "def sub(a, b):\n    return a - b"

    def _repo(self):
        repo = self.root / "repo"
        repo.mkdir(parents=True)
        (repo / "feature.py").write_text(
            self.BASE_ADD + "\n\n" + self.BASE_SUB + "\n", encoding="utf-8")
        (repo / "test_feature.py").write_text(
            "from feature import add, sub\n"
            "assert add(2, 3) == 5, add(2, 3)\n"
            "assert sub(5, 2) == 3, sub(5, 2)\n"
            "print('ok')\n", encoding="utf-8")
        (repo / "test_b.py").write_text(
            "from feature import add, sub\n"
            "assert add(2, 3) == 5\n"
            "assert sub(5, 2) == 3\n"
            "print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "init"], cwd=repo, check=True)
        return repo

    def _worker(self, first_both_bad=False):
        def worker(**kw):
            path = Path(kw["cwd"]) / "feature.py"
            if "use-task" in kw["prompt"]:
                (Path(kw["cwd"]) / "b.py").write_text(
                    "from feature import add, sub\nVALUE = add(2, 3) + sub(5, 2)\n",
                    encoding="utf-8")
                return "use done" + SELF
            text = path.read_text(encoding="utf-8") if path.exists() else (
                self.BASE_ADD + "\n\n" + self.BASE_SUB + "\n")
            if "修复指示" in kw["prompt"]:
                if "fix add" in kw["prompt"]:
                    text = text.replace(self.BASE_ADD, self.OK_ADD)
                    text = text.replace(self.BAD_ADD, self.OK_ADD)
                if "fix sub" in kw["prompt"]:
                    text = text.replace(self.BASE_SUB, self.OK_SUB)
                    text = text.replace(self.BAD_SUB, self.OK_SUB)
            elif not first_both_bad:
                text = text.replace(self.BASE_ADD, self.OK_ADD)
            else:
                text = text.replace(self.BASE_ADD, self.BAD_ADD)
                text = text.replace(self.BASE_SUB, self.BAD_SUB)
            path.write_text(text, encoding="utf-8")
            return "impl done" + SELF
        return worker

    def _planner(self, task_def, *, repairs, finish_task=None, holder=None):
        phase = {"step": 0, "repairs": repairs}

        def planner(**kw):
            p = kw["prompt"]
            if phase["step"] == 0:
                phase["step"] = 1
                return json.dumps(dispatch(*task_def))
            if phase["repairs"]:
                instruction = "fix add" if len(phase["repairs"]) == 2 and \
                    int(phase.setdefault("done", 0)) == 0 else "fix sub"
                phase.setdefault("done", 0)
                phase["done"] += 1
                phase["repairs"].pop(0)
                return json.dumps({"action": "repair", "target_task": "impl",
                                   "reason": "修实现", "instructions": instruction})
            if finish_task and holder and holder.get("eng"):
                eid = next((eid for eid, rec in holder["eng"].state["evidence"].items()
                            if rec.get("task") == finish_task
                            and rec.get("completion_usable")), None)
                if eid:
                    return json.dumps(complete(eid))
                return json.dumps({"action": "pause", "reason": "waiting"})
            if finish_task:
                pattern = (rf'"id": "(ev-\d{{4}})"[\s\S]{{0,500}}?'
                           rf'"task": "{finish_task}"[\s\S]{{0,800}}?'
                           r'"completion_usable": true')
                m = re.search(pattern, p)
            else:
                m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,600}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})
        return planner

    def _impl_task(self):
        return code_task("impl", "feature.py", prompt="impl-task") | {
            "checks": [{"name": "unit", "argv": ["python3", "test_feature.py"]}],
            "satisfies": ["g1"],
        }

    def _assert_patch_applies_and_tests(self, repo, patch_path):
        verify = self.root / "verify"
        shutil.copytree(repo, verify)
        subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_path)],
                       cwd=verify, check=True)
        subprocess.run(["python3", "test_feature.py"], cwd=verify, check=True)

    def test_repair_starts_from_previous_code_result_and_patch_is_full_delta(self):
        repo = self._repo()
        eng, store = self.start(
            {"planner": self._planner([self._impl_task()], repairs=["fix sub"]),
             "worker": self._worker(), "reviewer": [], "critic": []},
            mode="auto", workspace={"repo": str(repo)})
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=12)
        self.assertEqual(out["status"], "complete")
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "completed", rec.get("last_error"))
        self.assertEqual((rec["version"], rec["repairs"]), (2, 1))
        patch_path = store.dir / rec["changes"]["patch_path"]
        patch = patch_path.read_text(encoding="utf-8")
        self.assertIn("return a + b", patch)
        self.assertIn("return a - b", patch)
        self.assertEqual([c["path"] for c in rec["changes"]["changed_files"]], ["feature.py"])
        self.assertTrue((store.dir / "artifacts/impl/v1.change.patch").exists())
        self._assert_patch_applies_and_tests(repo, patch_path)

    def test_two_consecutive_repairs_keep_both_prior_fixes(self):
        repo = self._repo()
        eng, store = self.start(
            {"planner": self._planner([self._impl_task()],
                                      repairs=["fix add", "fix sub"]),
             "worker": self._worker(first_both_bad=True), "reviewer": [], "critic": []},
            mode="auto", workspace={"repo": str(repo)})
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=16)
        self.assertEqual(out["status"], "complete")
        rec = eng.state["tasks"]["impl"]
        self.assertEqual((rec["version"], rec["repairs"]), (3, 2))
        patch_path = store.dir / rec["changes"]["patch_path"]
        patch = patch_path.read_text(encoding="utf-8")
        self.assertIn("return a + b", patch)
        self.assertIn("return a - b", patch)
        self.assertTrue((store.dir / "artifacts/impl/v1.change.patch").exists())
        self.assertTrue((store.dir / "artifacts/impl/v2.change.patch").exists())
        self._assert_patch_applies_and_tests(repo, patch_path)

    def test_downstream_code_task_sees_the_repaired_result(self):
        repo = self._repo()
        impl = self._impl_task()
        impl["satisfies"] = []
        use = code_task("use", "b.py", depends=["impl"], prompt="use-task") | {
            "checks": [{"name": "unit", "argv": ["python3", "test_b.py"]}],
            "satisfies": ["g1"],
        }
        holder = {}
        eng, store = self.start(
            {"planner": self._planner([impl, use], repairs=["fix sub"],
                                      finish_task="use", holder=holder),
             "worker": self._worker(), "reviewer": [], "critic": []},
            mode="auto", workspace={"repo": str(repo)})
        holder["eng"] = eng
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=16)
        self.assertEqual(out["status"], "complete")
        impl = eng.state["tasks"]["impl"]
        use = eng.state["tasks"]["use"]
        self.assertEqual(use["status"], "completed", use.get("last_error"))
        self.assertEqual([c["path"] for c in use["changes"]["changed_files"]], ["b.py"])
        self.assertNotEqual(use["changes"]["input_commit"], use["changes"]["base_commit"])
        patch = (store.dir / use["changes"]["patch_path"]).read_text(encoding="utf-8")
        self.assertNotIn("feature.py", patch)
        self.assertEqual(impl["repairs"], 1)

    def test_conflicting_previous_patch_becomes_patch_conflict_without_worker_call(self):
        repo = self._repo()
        worker = self._worker()
        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(self._impl_task()))
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "unverified")
        patch_file = store.dir / rec["changes"]["patch_path"]
        patch_file.write_text("this is not a patch\n", encoding="utf-8")
        calls_before = len(eng.backends["worker"].calls)
        out = self.apply(eng, store, {"action": "repair", "target_task": "impl",
                                      "reason": "修实现", "instructions": "fix sub"})
        self.assertNotEqual(out["status"], "complete")
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "patch_conflict")
        self.assertIn("上一版", rec.get("last_error", ""))
        self.assertEqual(len(eng.backends["worker"].calls), calls_before)

    def test_changed_base_is_recorded_and_patch_uses_current_baseline(self):
        repo = self._repo()
        eng, store = self.start({"planner": [], "worker": self._worker(),
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(self._impl_task()))
        rec = eng.state["tasks"]["impl"]
        previous_base = rec["changes"]["base_commit"]
        (repo / "new-base.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "new base"], cwd=repo, check=True)
        new_base = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                           cwd=repo, text=True).strip()
        eng.workspace.base_commit = new_base
        out = self.apply(eng, store, {"action": "repair", "target_task": "impl",
                                      "reason": "修实现", "instructions": "fix sub"})
        self.assertNotEqual(out["status"], "complete")
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "completed", rec.get("last_error"))
        self.assertTrue(rec["changes"].get("previous_base_changed"))
        self.assertNotEqual(rec["changes"]["base_commit"], previous_base)
        self._assert_patch_applies_and_tests(repo, store.dir / rec["changes"]["patch_path"])

    def test_out_of_scope_previous_change_stays_explicit_after_repair(self):
        repo = self._repo()

        def worker(**kw):
            path = Path(kw["cwd"]) / "feature.py"
            text = path.read_text(encoding="utf-8")
            if "修复指示" not in kw["prompt"]:
                path.write_text(text.replace(self.BASE_ADD, self.OK_ADD),
                                encoding="utf-8")
                (Path(kw["cwd"]) / "extra.txt").write_text("outside\n", encoding="utf-8")
            else:
                path.write_text(text.replace(self.BASE_SUB, self.OK_SUB),
                                encoding="utf-8")
            return "done" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(self._impl_task()))
        rec = eng.state["tasks"]["impl"]
        self.assertIn("extra.txt", rec["changes"].get("outside_scope") or [])
        self.apply(eng, store, {"action": "repair", "target_task": "impl",
                                "reason": "修实现", "instructions": "fix sub"})
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "unverified")
        self.assertIn("extra.txt", rec["changes"].get("outside_scope") or [])
        self.assertFalse(rec["changes"].get("scope_ok"))

    def test_interrupted_previous_result_is_not_silently_inherited(self):
        repo = self._repo()
        eng, store = self.start({"planner": [], "worker": self._worker(),
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(self._impl_task()))
        with store.lock():
            eng.state = store.load()
            old = copy.deepcopy(eng.state)
            eng.state["tasks"]["impl"]["status"] = "unknown"
            eng.state["tasks"]["impl"]["side_effects"] = True
            eng.state = store.commit(old, eng.state, "test_interrupted", {})
        self.apply(eng, store, {"action": "repair", "target_task": "impl",
                                "reason": "检查后修复", "instructions": "fix sub"})
        rec = eng.state["tasks"]["impl"]
        self.assertEqual(rec["status"], "unverified")
        self.assertEqual(rec["repair_inheritance"], "uncertain_patch")
        self.assertEqual(rec["changes"].get("previous_patches"), [])
        patch = (store.dir / rec["changes"]["patch_path"]).read_text(encoding="utf-8")
        self.assertNotIn("return a + b", patch)


class CompletionRefresh(LoopCase):
    def _text_run(self, run_name="text", **task_kw):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []},
                                run_name=run_name)
        t = task("t1", satisfies=["g1"], **task_kw)
        self.apply(eng, store, dispatch(t))
        ev = next(eid for eid, rec in eng.state["evidence"].items()
                  if rec.get("task") == "t1" and rec.get("completion_usable"))
        return eng, store, ev

    def _reject(self, eng, store, ev):
        out = self.apply(eng, store, complete(ev))
        self.assertNotEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "completed")
        self.assertTrue(eng.state.get("completion_rejection"))
        return eng.state["completion_rejection"]["errors"]

    def test_deleted_artifact_cannot_be_used_for_completion(self):
        eng, store, ev = self._text_run()
        rec = eng.state["evidence"][ev]
        (store.dir / rec["artifact"]).unlink()
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("产物" in e or "不存在" in e for e in errors), errors)

    def test_modified_artifact_or_patch_cannot_be_used_for_completion(self):
        eng, store, ev = self._text_run()
        rec = eng.state["evidence"][ev]
        (store.dir / rec["artifact"]).write_text("tampered", encoding="utf-8")
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("哈希" in e or "变化" in e for e in errors), errors)

        repo = self.root / "repo"
        repo.mkdir()
        (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "test_feature.py").write_text(
            "from feature import value\nassert value == 2\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "init"], cwd=repo, check=True)

        def code_worker(**kw):
            (Path(kw["cwd"]) / "feature.py").write_text("value = 2\n", encoding="utf-8")
            return "code" + SELF

        ceng, cstore = self.start({"planner": [], "worker": code_worker,
                                   "reviewer": [], "critic": []},
                                  workspace={"repo": str(repo)}, run_name="code")
        self.apply(ceng, cstore, dispatch(code_task("impl", "feature.py", prompt="impl") | {
            "checks": [{"name": "unit", "argv": ["python3", "test_feature.py"]}],
            "satisfies": ["g1"],
        }))
        crec = ceng.state["tasks"]["impl"]
        cev = next(eid for eid, rec in ceng.state["evidence"].items()
                   if rec.get("task") == "impl" and rec.get("completion_usable"))
        (cstore.dir / crec["changes"]["patch_path"]).write_text(
            "tampered patch\n", encoding="utf-8")
        out = self.apply(ceng, cstore, complete(cev))
        self.assertNotEqual(out["status"], "complete")
        self.assertTrue(any("补丁" in e or "哈希" in e
                            for e in ceng.state["completion_rejection"]["errors"]))

    def test_modified_input_file_and_upstream_version_cannot_be_completed(self):
        (self.root / "text").mkdir(exist_ok=True)
        (self.root / "text" / "notes.txt").write_text("original\n", encoding="utf-8")
        eng, store, ev = self._text_run(input_files=["notes.txt"])
        (self.root / "text" / "notes.txt").write_text("changed\n", encoding="utf-8")
        errors = self._reject(eng, store, ev)
        self.assertTrue(any("输入" in e or "哈希" in e for e in errors), errors)

        def planner(**kw):
            p = kw["prompt"]
            if '"total_tasks": 0' in p:
                return json.dumps(dispatch(
                    task("a", acceptance=[{"id": "c1", "text": "a"}],
                         prompt="a-task"),
                    task("b", acceptance=[{"id": "c1", "text": "b"}],
                         prompt="b-task", depends_on=["a"], satisfies=["g1"])))
            m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,600}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})

        ueng, ustore = self.start({"planner": [], "worker": lambda **kw: "result" + SELF,
                                   "reviewer": review_json(["c1"]), "critic": []},
                                  run_name="upstream")
        self.apply(ueng, ustore, dispatch(
            task("a", acceptance=[{"id": "c1", "text": "a"}], prompt="a-task"),
            task("b", acceptance=[{"id": "c1", "text": "b"}], prompt="b-task",
                 depends_on=["a"], satisfies=["g1"])))
        uev = next(eid for eid, rec in ueng.state["evidence"].items()
                   if rec.get("task") == "b" and rec.get("completion_usable"))
        with ustore.lock():
            ueng.state = ustore.load()
            old = copy.deepcopy(ueng.state)
            ueng.state["tasks"]["a"]["result_version"] = 99
            ueng.state = ustore.commit(old, ueng.state, "test_upstream_version", {})
        out = self.apply(ueng, ustore, complete(uev))
        self.assertNotEqual(out["status"], "complete")
        self.assertTrue(any("上游" in e or "版本" in e
                            for e in ueng.state["completion_rejection"]["errors"]))

    def test_unchanged_result_still_completes(self):
        eng, store, ev = self._text_run()
        out = self.apply(eng, store, complete(ev))
        self.assertEqual(out["status"], "complete")

    def test_auto_mode_uses_the_same_refresh_check(self):
        holder = {}
        phase = {"calls": 0}

        def planner(**kw):
            phase["calls"] += 1
            if phase["calls"] == 1:
                return json.dumps(dispatch(task("t1", satisfies=["g1"])))
            eid = next((eid for eid, rec in holder["eng"].state["evidence"].items()
                        if rec.get("task") == "t1" and rec.get("completion_usable")), None)
            if eid:
                rec = holder["eng"].state["evidence"][eid]
                (holder["store"].dir / rec["artifact"]).unlink(missing_ok=True)
                return json.dumps(complete(eid))
            return json.dumps({"action": "pause", "reason": "waiting"})

        eng, store = self.start({"planner": planner, "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []},
                                mode="auto", run_name="auto")
        holder.update(eng=eng, store=store)
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=8)
        self.assertEqual(out["status"], "paused")
        self.assertTrue(eng.state.get("completion_rejection"))


if __name__ == "__main__":
    unittest.main()
