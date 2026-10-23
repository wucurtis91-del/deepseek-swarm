"""Offline tests for the persistent loop. No paid API calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from swarmlib import backends, config, engine, protocol, state  # noqa: E402

ROLES = {
    "planner": {"backend": "fake", "provider": "anthropic", "model": "claude-opus-5-5",
                "auth": "local_login", "tools": "read"},
    "worker": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
               "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "none"},
    "reviewer": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
                 "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"},
    "critic": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
               "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "enabled": False},
}


def review_json(criteria, pass_=True, errors=None):
    return "```json\n" + json.dumps({
        "criteria": [{"id": c, "met": pass_, "note": "ok" if pass_ else "bad"}
                     for c in criteria],
        "errors": errors or [],
        "fix_instructions": "",
    }) + "\n```"


SELF = '\n```selfcheck\n{"met": [], "not_met": [], "uncertain": []}\n```'


def goal(*criteria):
    return config.normalize_goal({
        "goal": "test goal",
        "completion_criteria": list(criteria) or ["g1"],
    })


def task(tid="t1", **kw):
    base = {
        "id": tid, "kind": "text", "prompt": f"完成 {tid}",
        "question": f"{tid} 的未知", "produces": "一段结果",
        "decision_impact": "决定下一步", "tools": "none",
        "acceptance": [{"id": "c1", "text": "结果存在"}],
        "acceptance_policy": "semantic", "satisfies": [],
    }
    base.update(kw)
    return base


def dispatch(*tasks, **kw):
    return {"action": "dispatch", "reason": kw.get("reason", "派发"), "tasks": list(tasks)}


def complete(evidence, criterion="g1"):
    return {"action": "complete", "reason": "完成", "evidence": [evidence],
            "completion": [{"criterion": criterion, "met": True, "evidence": [evidence]}]}


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roles = config.validate_roles(ROLES, allow_fake=True)
        self.cfg = config.load_config(None)
        self.cfg["roles"] = ROLES
        self.goal = goal("g1")

    def start(self, fakes, *, cfg=None, mode="external", workspace=None, goal_obj=None,
              run_name="run"):
        store = state.RunStore(self.root / run_name)
        use_cfg = cfg or self.cfg
        roles = config.validate_roles(use_cfg["roles"], allow_fake=True)
        normalized = {}
        for name, value in fakes.items():
            if isinstance(value, str):
                normalized[name] = (lambda v=value, **kw: v)
            elif callable(value) or isinstance(value, dict) or isinstance(value, list):
                normalized[name] = value
            else:
                normalized[name] = [value]
        backends_map = backends.build_backends(
            roles, allow_fake=True, fake_responses=normalized)
        eng = engine.Engine(store, use_cfg, roles, backends_map, mode=mode)
        eng.start(goal_obj or self.goal, workspace or {"repo": None})
        return eng, store

    def reload(self, eng, store):
        with store.lock():
            eng.state = store.load()
        return eng

    def apply(self, eng, store, raw, external=True):
        norm = protocol.normalize_decision(
            raw, eng.state["goal"]["criteria"], eng.state.get("round", 0) + 1,
            set(eng.state["tasks"]))
        with store.lock():
            eng.state = store.load()
            return eng.apply_decision(norm, external=external)

    def artifact_evidence_id(self, eng):
        for eid, ev in eng.state["evidence"].items():
            if ev.get("completion_usable"):
                return eid
        self.fail("no completion-usable evidence")


class DynamicDirection(LoopCase):
    def test_first_result_changes_second_direction(self):
        calls = {"planner": 0, "worker": []}
        phase = {"probe": False, "follow": False}

        def planner(**kw):
            p = kw["prompt"]
            calls["planner"] += 1
            if not phase["probe"]:
                phase["probe"] = True
                return json.dumps(dispatch(task(
                    "probe", prompt="probe 这个问题", acceptance=[{"id": "c1", "text": "结果存在"}])))
            if "RESULT: A" in p and not phase["follow"]:
                phase["follow"] = True
                return json.dumps(dispatch(task(
                    "follow", prompt="follow up", depends_on=["probe"],
                    acceptance=[{"id": "c1", "text": "跟随结果"}], satisfies=["g1"])))
            import re
            m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,400}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})

        def worker(**kw):
            p = kw["prompt"]
            calls["worker"].append(p)
            if "probe 这个问题" in p:
                return "RESULT: A" + SELF
            return "FOLLOWED A" + SELF

        eng, store = self.start({"planner": planner, "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, mode="auto")
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=8)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["probe"]["status"], "completed")
        self.assertEqual(eng.state["tasks"]["follow"]["status"], "completed")
        self.assertIn("probe", eng.state["tasks"]["follow"]["depends_on"])
        follow_prompt = next(p for p in calls["worker"] if "follow up" in p)
        self.assertIn("RESULT: A", follow_prompt)
        self.assertGreaterEqual(calls["planner"], 3)


class RepairAndReplan(LoopCase):
    def test_local_error_goes_repair_and_keeps_old_artifact(self):
        state_box = {"n": 0}

        def planner(**kw):
            p = kw["prompt"]
            if '"total_tasks": 0' in p:
                return json.dumps(dispatch(task(
                    "impl", prompt="产出 OK", tools="none",
                    acceptance=[{"id": "c1", "text": "包含 OK", "check": "unit"}],
                    acceptance_policy="deterministic",
                    checks=[{"name": "unit", "argv": [
                        "python3", "-c",
                        "import sys;sys.exit(0 if 'OK' in open(sys.argv[1]).read() else 1)",
                        "{result}"]}],
                    satisfies=["g1"])))
            if '"status": "unverified"' in p or '"status": "review_unavailable"' in p:
                return json.dumps({"action": "repair", "target_task": "impl",
                                   "reason": "只是实现漏了 OK", "instructions": "正文必须包含 OK"})
            import re
            m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,300}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})

        def worker(**kw):
            state_box["n"] += 1
            return ("BAD" if state_box["n"] == 1 else "OK") + SELF

        eng, store = self.start({"planner": planner, "worker": worker,
                                 "reviewer": [], "critic": []}, mode="auto")
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=8)
        self.assertEqual(out["status"], "complete")
        t = eng.state["tasks"]["impl"]
        self.assertEqual((t["version"], t["repairs"], t["status"]), (2, 1, "completed"))
        self.assertEqual(len(t["artifact_history"]), 2)
        self.assertNotEqual(t["artifact_history"][0]["sha256"], t["artifact_history"][1]["sha256"])
        self.assertTrue((store.dir / "artifacts/impl/v1.md").exists())

    def test_direction_error_goes_replan_and_cancels_unrun_task(self):
        phase = {"bad": False, "replanned": False}

        def planner(**kw):
            p = kw["prompt"]
            if not phase["bad"]:
                phase["bad"] = True
                return json.dumps(dispatch(task(
                    "bad", acceptance=[{"id": "c1", "text": "must pass", "check": "unit"}],
                    acceptance_policy="deterministic",
                    checks=[{"name": "unit", "argv": ["python3", "-c", "sys.exit(1)"]}],
                    satisfies=["g1"])))
            if not phase["replanned"]:
                phase["replanned"] = True
                return json.dumps({"action": "replan", "reason": "方法不可行",
                                   "cancel": ["bad"],
                                   "tasks": [task("alt", acceptance=[{"id": "c1", "text": "ok"}],
                                                  satisfies=["g1"])]})
            import re
            m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,400}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})

        def worker(**kw):
            return "anything" + SELF

        eng, store = self.start({"planner": planner, "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, mode="auto")
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=10)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["bad"]["status"], "cancelled")
        self.assertEqual(eng.state["tasks"]["alt"]["status"], "completed")
        actions = [d["action"] for d in eng.state["decisions"]]
        self.assertIn("replan", actions)


class StrictReview(LoopCase):
    def _run(self, reviewer):
        eng, store = self.start({"planner": [], "worker": ["答案" + SELF],
                                 "reviewer": reviewer, "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        return eng, store

    def test_missing_criteria_cannot_pass(self):
        eng, _ = self._run(['```json\n{"pass": true}\n```'] * 4)
        t = eng.state["tasks"]["t1"]
        self.assertEqual(t["status"], "review_invalid")
        self.assertNotEqual(t["status"], "completed")
        self.assertEqual(t["acceptance_result"]["review_outcome"], "invalid")

    def test_errors_with_claimed_pass_cannot_pass(self):
        eng, _ = self._run([review_json(["c1"], pass_=True, errors=["real problem"])] * 2)
        t = eng.state["tasks"]["t1"]
        self.assertEqual(t["status"], "unverified")
        self.assertTrue(t["review"]["errors"])

    def test_review_unavailable_is_not_success(self):
        eng, _ = self._run([{"ok": False, "error": "503 unavailable", "error_kind": "transient",
                             "usage": {}, "usage_missing": True}] * 4)
        t = eng.state["tasks"]["t1"]
        self.assertEqual(t["status"], "review_unavailable")
        self.assertNotEqual(t["status"], "completed")

    def test_explicit_fail_is_reworkable_verdict(self):
        eng, _ = self._run([review_json(["c1"], pass_=False, errors=["wrong"])] * 2)
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "unverified")


class Batching(LoopCase):
    def test_multiple_completions_are_one_planner_decision(self):
        calls = {"planner": 0}

        def planner(**kw):
            p = kw["prompt"]
            calls["planner"] += 1
            if '"total_tasks": 0' in p:
                return json.dumps(dispatch(*[
                    task(f"t{i}", acceptance=[{"id": "c1", "text": "ok"}], satisfies=["g1"])
                    for i in range(3)]))
            import re
            m = re.search(r'"id": "(ev-\d{4})"[\s\S]{0,300}?"completion_usable": true', p)
            if m:
                return json.dumps(complete(m.group(1)))
            return json.dumps({"action": "pause", "reason": "waiting"})

        def worker(**kw):
            return "ok" + SELF

        eng, store = self.start({"planner": planner, "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, mode="auto")
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=6)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(sum(1 for t in eng.state["tasks"].values()
                             if t["status"] == "completed"), 3)
        self.assertEqual(calls["planner"], 2)     # initial dispatch + completion


class Recovery(LoopCase):
    def test_finished_tasks_are_not_rerun(self):
        calls = []

        def worker(**kw):
            calls.append(kw["prompt"])
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "completed")
        before = len(calls)
        with store.lock():
            eng.state = store.load()
            eng.run_until_decision()
        self.assertEqual(len(calls), before)
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "completed")

    def test_interrupted_write_task_is_not_replayed(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        norm = protocol.normalize_task(task(
            "w", kind="code", tools="edit", allowed_paths=["src/**"],
            acceptance=[{"id": "c1", "text": "ok", "check": "unit"}],
            acceptance_policy="deterministic",
            checks=[{"name": "unit", "argv": ["python3", "-c", "pass"]}]), self.goal["criteria"], 1)
        norm["status"] = "running"
        norm["attempts"] = 1
        norm["side_effects"] = True
        norm["run_ctx"] = None
        with store.lock():
            eng.state = store.load()
            eng.state["tasks"]["w"] = norm
            eng.state["status"] = "executing"
            eng.commit("test_inject_running", {})
        with store.lock():
            eng2 = engine.Engine(store, self.cfg, self.roles, {}, mode="external")
            eng2.open()
            notes = eng2.recover()
            eng2.commit("recovered_test", {})
        self.assertEqual(eng2.state["tasks"]["w"]["status"], "unknown")
        self.assertTrue(any(n["task"] == "w" for n in notes))
        self.assertEqual(eng2.state["status"], "awaiting_decision")
        self.assertTrue(any(t["kind"] == "recovered" for t in eng2.state["triggers"]))

    def test_upstream_change_marks_downstream_stale(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "结果" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(
            task("u", acceptance=[{"id": "c1", "text": "ok"}]),
            task("d", depends_on=["u"], acceptance=[{"id": "c1", "text": "ok"}])))
        self.assertEqual(eng.state["tasks"]["d"]["status"], "completed")
        with store.lock():
            eng.state = store.load()
            u = eng.state["tasks"]["u"]
            u["result_version"] = 2
            u["artifact"]["sha256"] = "f" * 64
            eng._check_staleness()
            eng.commit("test_upstream_changed", {})
        self.assertEqual(eng.state["tasks"]["d"]["status"], "stale")
        self.assertTrue(eng.state["tasks"]["d"].get("stale"))
        for ev in eng.state["evidence"].values():
            if ev.get("task") == "d":
                self.assertFalse(ev.get("verified"))


class Budgets(LoopCase):
    def test_concurrent_calls_respect_hard_call_limit(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["budget"] = dict(cfg["budget"], max_calls=1)
        started = []

        def worker(**kw):
            started.append(1)
            time.sleep(0.05)
            return "ok" + SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(task("a"), task("b")))
        self.assertLessEqual(len(started), 1)
        self.assertEqual(eng.state["status"], "paused")
        self.assertLessEqual(eng.state["budget"]["calls"], 1)

    def test_unknown_price_is_not_zero_cost(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["prices"] = {}
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []}, cfg=cfg)
        self.apply(eng, store, dispatch(task("t1")))
        b = eng.state["budget"]
        self.assertGreaterEqual(b["cost_unknown_calls"], 1)
        self.assertEqual(b["cost_coverage"], "partial")
        self.assertEqual(b["cost_usd_known"], 0)
        self.assertTrue(any(not u["cost_known"] for u in eng.state["usage_calls"]))

    def test_timeout_without_usage_is_unknown(self):
        def worker(**kw):
            return {"ok": False, "error": "timeout", "error_kind": "timeout",
                    "usage": {}, "usage_missing": True, "seconds": 30}

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, dispatch(task("t1")))
        t = eng.state["tasks"]["t1"]
        self.assertIn(t["status"], ("exec_failed", "interrupted", "unknown"))
        b = eng.state["budget"]
        self.assertGreaterEqual(b["usage_unknown_calls"], 1)
        self.assertGreaterEqual(b["cost_unknown_calls"], 1)

    def test_external_planner_is_unmetered_not_zero(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1")))
        b = eng.state["budget"]
        self.assertEqual(b["external_unmetered"], 1)
        self.assertEqual(b["cost_coverage"], "partial")


class CodeTasks(LoopCase):
    def _repo(self):
        repo = self.root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "src" / "x.py").write_text("def value():\n    return 1\n")
        (repo / "tests" / "test_x.py").write_text(
            "from src.x import value\nassert value() == 1\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                        "commit", "-qm", "init"], cwd=repo, check=True)
        return repo

    def _code_task(self, tid="impl", **kw):
        base = task(tid, kind="code", tools="edit",
                    allowed_paths=["src/**"],
                    acceptance=[{"id": "c1", "text": "测试通过", "check": "unit"}],
                    acceptance_policy="deterministic",
                    checks=[{"name": "unit", "argv": ["python3", "tests/test_x.py"]}],
                    satisfies=["g1"])
        base.update(kw)
        return base

    def test_scope_violation_fails_acceptance(self):
        repo = self._repo()
        eng, store = self.start({"planner": [], "worker": lambda **kw: (
            (Path(kw["cwd"]) / "outside.py").write_text("x=1"), "done" + SELF)[1],
            "reviewer": [], "critic": []}, workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(self._code_task()))
        t = eng.state["tasks"]["impl"]
        self.assertEqual(t["status"], "unverified")
        scope = next(c for c in t["acceptance_result"]["checks"] if c["name"] == "scope")
        self.assertFalse(scope["ok"])
        self.assertIn("outside.py", t["changes"]["outside_scope"])

    def test_negative_check_rejects_vacuous_tests(self):
        repo = self._repo()
        def worker(**kw):
            path = Path(kw["cwd"]) / "tests" / "test_vacuous.py"
            path.write_text("import unittest\n"
                            "class T(unittest.TestCase):\n"
                            "    def test_ok(self):\n"
                            "        self.assertTrue(True)\n")
            return "写了测试" + SELF
        t = self._code_task(negative_check={
            "path": "src/x.py", "find": "return 1", "replace": "return 2",
            "check": "unit", "note": "已知错误"})
        t["allowed_paths"] = ["src/**", "tests/**"]
        t["checks"] = [{"name": "unit", "argv": [
            "python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_vacuous.py"]}]
        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        self.apply(eng, store, dispatch(t))
        task_rec = eng.state["tasks"]["impl"]
        self.assertEqual(task_rec["status"], "unverified")
        neg = task_rec["acceptance_result"]["negative"]
        self.assertFalse(neg["ok"])
        self.assertIn("测试质量", neg["detail"])

    def test_two_conflicting_patches_are_detected(self):
        repo = self._repo()
        def worker(**kw):
            (Path(kw["cwd"]) / "src" / "x.py").write_text("def value():\n    return 7\n")
            return "changed" + SELF
        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []},
                                workspace={"repo": str(repo)})
        a, b = self._code_task("a"), self._code_task("b")
        for t in (a, b):
            t["checks"] = [{"name": "unit", "argv": ["python3", "-c", "pass"]}]
        self.apply(eng, store, dispatch(a, b))
        statuses = {t: eng.state["tasks"][t]["status"] for t in ("a", "b")}
        self.assertIn("patch_conflict", statuses.values())
        conflicted = [t for t in ("a", "b")
                      if (eng.state["tasks"][t].get("changes") or {}).get("cross_patch_conflict")]
        self.assertTrue(conflicted)
        rec = eng.state["tasks"][conflicted[-1]]
        self.assertFalse(rec["changes"]["cross_patch_conflict"]["ok"])


class ContextAndCritic(LoopCase):
    def test_context_request_is_structured_and_defers_action(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "RESULT: A" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1")))
        raw = {"action": "request_context", "reason": "need full body",
               "context_requests": [{"artifact": "t1@v1"}]}
        norm = protocol.normalize_decision(raw, eng.state["goal"]["criteria"], 2,
                                           set(eng.state["tasks"]))
        with store.lock():
            eng.state = store.load()
            out = eng.apply_decision(norm, external=True)
        self.assertEqual(out["status"], "awaiting_decision")
        packet = json.loads((store.dir / eng.state["pending_packet"]).read_text())
        self.assertTrue(any("RESULT: A" in (n.get("content") or "")
                            for n in packet["context_notes"]))
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "completed")  # action not applied

    def test_critic_is_optional_and_triggered_by_repeated_failure(self):
        cfg = config.load_config(None)
        cfg["roles"] = {**ROLES, "critic": {**ROLES["critic"], "enabled": True}}
        cfg["loop"] = dict(cfg["loop"], critic={"enabled": True, "on_repeated_failures": 2})
        roles = config.validate_roles(cfg["roles"], allow_fake=True)
        seen = []
        def critic(**kw):
            seen.append(kw["prompt"])
            return json.dumps({"assumptions": [{"text": "x", "why_untested": "y"}],
                               "evidence_gaps": [], "alternatives": [],
                               "discriminating_check": {"description": "run one case"},
                               "verdict": "uncertain", "reason": "test"})
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": critic},
                                cfg=cfg)
        with store.lock():
            eng.state = store.load()
            t = protocol.normalize_task(task("t1"), eng.state["goal"]["criteria"], 1)
            t["status"] = "unverified"
            t["repeat_count"] = 2
            t["artifact"] = {"version": 1, "path": "artifacts/t1/v1.md",
                             "sha256": "0" * 64, "chars": 2}
            (store.dir / "artifacts/t1").mkdir(parents=True, exist_ok=True)
            (store.dir / "artifacts/t1/v1.md").write_text("ok")
            eng.state["tasks"]["t1"] = t
            eng._maybe_run_critic()
            eng.commit("critic_test", {})
        self.assertTrue(seen)
        self.assertEqual(eng.state["tasks"]["t1"]["critic"]["verdict"], "uncertain")
        self.assertTrue(any(e["kind"] == "critic" for e in eng.state["evidence"].values()))


class StateSafety(LoopCase):
    def test_truncated_event_log_tail_is_ignored(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        with open(store.events_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 999, "type": "partial"')
        second = state.RunStore(store.dir)
        loaded = second.load()
        self.assertEqual(loaded["run_id"], eng.state["run_id"])
        self.assertTrue(any("截断" in n for n in second.load_notes))

    def test_corrupt_snapshot_is_rebuilt_from_event_log(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        run_id = eng.state["run_id"]
        store.state_path.write_text("{not json")
        loaded = state.RunStore(store.dir).load()
        self.assertEqual(loaded["run_id"], run_id)
        self.assertEqual(loaded["status"], "awaiting_decision")

    def test_incompatible_schema_is_refused(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        data = json.loads(store.state_path.read_text())
        data["schema"] = 999
        store.state_path.write_text(json.dumps(data))
        with self.assertRaises(state.StateError):
            state.RunStore(store.dir).load()

    def test_single_writer_lock(self):
        eng, store = self.start({"planner": [], "worker": [], "reviewer": [], "critic": []})
        errors = []
        with store.lock():
            with self.assertRaises(state.WriterLocked):
                with store.lock():
                    pass


class Roles(LoopCase):
    def test_provider_endpoint_model_cannot_be_mixed(self):
        bad = json.loads(json.dumps(ROLES))
        bad["planner"]["endpoint"] = "https://api.deepseek.com/anthropic"
        bad["planner"]["model"] = "claude-opus-5-5"
        bad["planner"]["backend"] = "claude_cli"
        with self.assertRaises(config.ConfigError):
            config.validate_roles(bad, allow_fake=True)
        bad2 = json.loads(json.dumps(ROLES))
        bad2["worker"]["backend"] = "claude_cli"
        bad2["worker"]["model"] = "claude-opus-5-5"
        with self.assertRaises(config.ConfigError):
            config.validate_roles(bad2, allow_fake=True)

    def test_config_types_are_checked(self):
        with self.assertRaises(config.ConfigError):
            config.normalize_loop({"concurrency": "four"})
        with self.assertRaises(config.ConfigError):
            config.normalize_budget({"max_calls": True})
        with self.assertRaises(config.ConfigError):
            config.normalize_goal({"goal": "x", "completion_criteria": []})

    def test_worker_and_planner_environments_do_not_leak(self):
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
        roles = config.validate_roles(real, allow_fake=True)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ds-secret-key"}, clear=False):
            w = backends.ClaudeBackend(roles["worker"], claude="/bin/true")
            p = backends.ClaudeBackend(roles["planner"], claude="/bin/true")
            wenv, penv = w._env(), p._env()
        self.assertEqual(wenv["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(wenv["ANTHROPIC_API_KEY"], "ds-secret-key")
        self.assertNotIn("ANTHROPIC_API_KEY", penv)
        self.assertNotIn("ANTHROPIC_BASE_URL", penv)


class IdentityAndOutput(LoopCase):
    def test_worker_identity_mismatch_is_not_ignored(self):
        store = state.RunStore(self.root / "run")
        b = backends.build_backends(self.roles, allow_fake=True,
                                    fake_responses={"planner": [], "worker": ["answer"],
                                                    "reviewer": review_json(["c1"]), "critic": []})
        b["worker"] = backends.FakeBackend(
            self.roles["worker"], responses=["answer"], model_observed=["claude-opus-5-5"])
        eng = engine.Engine(store, self.cfg, self.roles, b, mode="external")
        eng.start(self.goal, {"repo": None})
        self.apply(eng, store, dispatch(task("t1", satisfies=["g1"])))
        t = eng.state["tasks"]["t1"]
        self.assertEqual(t["status"], "exec_failed")
        self.assertIn("身份", t["last_error"])

    def test_empty_worker_output_is_an_explicit_failure(self):
        def worker(**kw):
            return {"ok": True, "text": "", "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0, "output_tokens": 1}}
        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": review_json(["c1"]), "critic": []})
        self.apply(eng, store, dispatch(task("t1")))
        self.assertEqual(eng.state["tasks"]["t1"]["status"], "exec_failed")
        self.assertIn("空", eng.state["tasks"]["t1"]["last_error"])


class Compression(LoopCase):
    def test_optional_model_compression_is_metered_and_preserves_ids(self):
        roles_cfg = {**ROLES, "compressor": {
            "backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
            "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "none",
            "enabled": True}}
        cfg = config.load_config(None)
        cfg["roles"] = roles_cfg
        cfg["loop"] = dict(cfg["loop"], packet_compress={"enabled": True, "min_chars": 1})
        roles = config.validate_roles(roles_cfg, allow_fake=True)

        def compressor(**kw):
            p = kw["prompt"]
            marker = "## 决策材料\n"
            obj = json.loads(p[p.find(marker) + len(marker):])
            obj["state_summary"] = "short"
            obj.pop("context_notes", None)
            return json.dumps(obj)

        store = state.RunStore(self.root / "run")
        b = backends.build_backends(roles, allow_fake=True,
                                    fake_responses={"planner": [], "worker": [],
                                                    "reviewer": review_json(["c1"]),
                                                    "critic": [], "compressor": compressor})
        eng = engine.Engine(store, cfg, roles, b, mode="external")
        eng.start(self.goal, {"repo": None})
        packet = {
            "goal": {"completion_criteria": [{"id": "g1"}]},
            "state_summary": {"x": "y" * 5000},
            "new_evidence": [], "unresolved": [], "disagreements": [],
            "budget": {}, "decision_needed": {}, "key_artifacts": [],
            "refuted": [], "packet_meta": {"chars": 6000},
        }
        out = eng._maybe_compress(packet)
        self.assertTrue(out["packet_meta"].get("compressed"))
        self.assertLess(out["packet_meta"]["chars"], 6000)
        self.assertTrue(any(u["role"] == "compressor" for u in eng.state["usage_calls"]))


class NoProgress(LoopCase):
    def test_repeated_completion_without_valid_evidence_stops(self):
        cfg = config.load_config(None)
        cfg["roles"] = ROLES
        cfg["loop"] = dict(cfg["loop"], no_progress_limit=2)
        phase = {"first": True}

        def planner(**kw):
            if phase["first"]:
                phase["first"] = False
                return json.dumps(dispatch(task("t1", satisfies=["g1"])))
            # ev-0002 is the review evidence, not a completion-usable artifact.
            return json.dumps(complete("ev-0002"))

        eng, store = self.start({"planner": planner, "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": review_json(["c1"]), "critic": []},
                                cfg=cfg, mode="auto")
        with store.lock():
            eng.state = store.load()
            out = eng.run_auto(max_steps=8)
        self.assertEqual(out["status"], "paused")
        self.assertIn("no_progress", out["stop_reason"])
        self.assertIsNone(eng.state["completion"])


class ProtocolParity(LoopCase):
    def test_external_and_auto_apply_the_same_decision_protocol(self):
        raw = dispatch(task("t1", satisfies=["g1"]))
        external, store1 = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                       "reviewer": review_json(["c1"]), "critic": []})
        self.apply(external, store1, raw)

        phase = {"n": 0}

        def planner(**kw):
            phase["n"] += 1
            if phase["n"] == 1:
                return json.dumps(raw)
            return json.dumps({"action": "pause", "reason": "stop after parity check"})

        auto, store2 = self.start({"planner": planner, "worker": lambda **kw: "ok" + SELF,
                                   "reviewer": review_json(["c1"]), "critic": []}, mode="auto",
                                  run_name="run-auto")
        with store2.lock():
            auto.state = store2.load()
            auto.run_auto(max_steps=2)
        a = external.state["tasks"]["t1"]
        b = auto.state["tasks"]["t1"]
        self.assertEqual(a["definition_hash"], b["definition_hash"])
        self.assertEqual(a["acceptance"], b["acceptance"])
        self.assertEqual(a["status"], b["status"])
        self.assertEqual(sorted(e["kind"] for e in external.state["evidence"].values()),
                         sorted(e["kind"] for e in auto.state["evidence"].values()))


if __name__ == "__main__":
    unittest.main()
