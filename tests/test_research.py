"""Offline behavior tests for the minimal research mode.

These tests define the controller contract, not the scientific truth of a
model's interpretation. They use scripted fake backends and never call a paid API.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmlib import config, engine, packet as packetmod, protocol, state  # noqa: E402
from test_loop import LoopCase, SELF, complete, dispatch, task  # noqa: E402


def key_task(tid="probe", *, hypotheses=("h1",), depends_hypotheses=(), **kw):
    return task(
        tid, prompt=f"{tid}-task", tests_hypotheses=list(hypotheses),
        depends_on_hypotheses=list(depends_hypotheses), key_experiment=True,
        acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
        acceptance_policy="deterministic",
        checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(0)"]}],
        **kw,
    )


def text_task(tid, *, hypotheses=("h1",), depends_hypotheses=(), **kw):
    return task(
        tid, prompt=f"{tid}-task", tests_hypotheses=list(hypotheses),
        depends_on_hypotheses=list(depends_hypotheses),
        acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
        acceptance_policy="deterministic",
        checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(0)"]}],
        **kw,
    )


class ResearchCase(LoopCase):
    def setUp(self):
        super().setUp()
        self.cfg["loop"]["research_mode"] = True

    def dispatch_with_hypotheses(self, *tasks, hypotheses=None):
        decision = dispatch(*tasks)
        decision["research"] = {
            "hypothesis_updates": hypotheses or [{
                "id": "h1", "text": "H1", "scope": "initial scope",
                "core": True, "alternatives": [],
            }],
        }
        return decision

    def review(self, review_ids, interpretations=None, updates=None,
               direction=None):
        return {
            "action": "review", "reason": "interpret result",
            "research": {
                "review_ids": list(review_ids),
                "interpretations": interpretations or [],
                "hypothesis_updates": updates or [],
                "direction": direction,
            },
        }

    def pending(self, eng):
        return eng.state["research"]["pending_reviews"]

    def obs_for(self, eng, tid):
        return next(o for o in eng.state["research"]["observations"].values()
                    if o.get("task") == tid)

    def evidence_for(self, eng, tid):
        return next(e for e in eng.state["evidence"].values()
                    if e.get("task") == tid and e.get("kind") == "artifact")


class ResearchResultFlow(ResearchCase):
    def test_key_experiment_completion_enters_pending_review_without_against(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        self.assertEqual(eng.state["tasks"]["probe"]["status"], "completed")
        pending = self.pending(eng)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(pending[0]["hypothesis_ids"], ["h1"])
        self.assertIn("observation", pending[0])
        self.assertIn("evidence", pending[0])
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")

    def test_implementation_failure_does_not_become_hypothesis_refutation(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "bad" + SELF,
                                 "reviewer": [], "critic": []})
        bad = task(
            "probe", prompt="probe-task", tests_hypotheses=["h1"],
            key_experiment=True,
            acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
            acceptance_policy="deterministic",
            checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(1)"]}],
        )
        self.apply(eng, store, self.dispatch_with_hypotheses(bad))
        rec = eng.state["tasks"]["probe"]
        self.assertEqual(rec["status"], "unverified")
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")
        self.assertFalse(any(
            i.get("relation") == "against"
            for i in eng.state["research"]["interpretations"].values()))

    def test_prediction_mismatch_is_pending_but_not_controller_refutation(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        predicted = key_task()
        predicted["research_prediction"] = {
            "hypothesis": "h1", "check": "unit", "expect_exit": 1,
            "note": "program-visible prediction",
        }
        self.apply(eng, store, self.dispatch_with_hypotheses(predicted))
        pending = self.pending(eng)
        self.assertTrue(any(p.get("trigger") == "prediction_mismatch" for p in pending),
                        pending)
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")

    def test_review_only_records_judgment_without_fake_task(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        pending = self.pending(eng)
        rid = pending[0]["id"]
        obs = pending[0]["observation"]
        before = len(eng.state["tasks"])
        out = self.apply(eng, store, self.review(
            [rid], interpretations=[{
                "observation": obs, "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "inconclusive",
                "conditions": "pilot only", "rationale": "result is ambiguous",
                "source": "planner",
            }], direction={"choice": "continue", "hypothesis": "h1",
                            "reason": "need one calibration"}))
        self.assertNotEqual(out["status"], "paused")
        self.assertEqual(len(eng.state["tasks"]), before)
        self.assertEqual(self.pending(eng)[0]["status"], "resolved")
        self.assertEqual(len(eng.state["research"]["reviews"]), 1)
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "open")

    def test_single_hypothesis_experiment_and_pause_are_expressible(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task("seed")))
        rid = self.pending(eng)[0]["id"]
        # A single-hypothesis verification experiment is legal; two hypotheses
        # are not required.
        verify = key_task("calibrate", hypotheses=("h1",))
        verify["kind"] = "verify"
        verify["key_experiment"] = False
        verify["verify_of"] = "seed"
        verify["depends_on"] = ["seed"]
        out = self.apply(eng, store, {
            "action": "verify", "reason": "calibrate measurement",
            "target_task": "seed", "tasks": [verify],
            "research": {
                "review_ids": [rid],
                "interpretations": [{
                    "observation": self.pending(eng)[0]["observation"],
                    "hypothesis": "h1", "hypothesis_version": 1,
                    "relation": "inconclusive", "conditions": "measurement check",
                    "rationale": "need calibration", "source": "planner",
                }],
                "direction": {"choice": "experiment", "hypothesis": "h1",
                              "reason": "single-hypothesis calibration"},
            },
        })
        self.assertIn("calibrate", eng.state["tasks"])
        self.assertEqual(self.pending(eng)[0]["status"], "resolved")
        # A separate review can pause without changing the graph.
        out = self.apply(eng, store, {
            "action": "review", "reason": "pause for external data",
            "research": {"direction": {"choice": "pause", "hypothesis": "h1",
                                        "reason": "waiting for external data"}},
        })
        self.assertEqual(eng.state["research"]["reviews"][-1]["direction"]["choice"],
                         "pause")


class ResearchRevisions(ResearchCase):
    def test_supporting_evidence_invalidation_keeps_other_support(self):
        self._data("one.txt"); self._data("two.txt")
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        a = key_task("a", input_files=["one.txt"])
        b = key_task("b", input_files=["two.txt"])
        self.apply(eng, store, self.dispatch_with_hypotheses(a, b))
        obs_a = self.obs_for(eng, "a")
        obs_b = self.obs_for(eng, "b")
        rids = [p["id"] for p in self.pending(eng)]
        self.apply(eng, store, self.review(rids, interpretations=[
            {"observation": obs_a["id"], "hypothesis": "h1", "hypothesis_version": 1,
             "relation": "supports", "conditions": "one", "rationale": "a",
             "source": "planner"},
            {"observation": obs_b["id"], "hypothesis": "h1", "hypothesis_version": 1,
             "relation": "supports", "conditions": "two", "rationale": "b",
             "source": "planner"},
        ], direction={"choice": "continue", "hypothesis": "h1", "reason": "two supports"}))
        with store.lock():
            eng.state = store.load()
            old = copy.deepcopy(eng.state)
            # Invalidate only observation a through its evidence, as a real
            # input change would.
            ev = self.evidence_for(eng, "a")
            eng.state["evidence"][ev["id"]]["verified"] = False
            eng.state["evidence"][ev["id"]]["stale"] = True
            eng.state["research"]["observations"][obs_a["id"]]["status"] = "invalidated"
            eng.state = store.commit(old, eng.state, "test_invalidate_a", {})
        out = self.apply(eng, store, self.review(
            [self.pending(eng)[0]["id"]], interpretations=[], direction={
                "choice": "continue", "hypothesis": "h1", "reason": "recheck remaining"}))
        h = eng.state["hypotheses"]["h1"]
        active = [i for i in eng.state["research"]["interpretations"].values()
                  if i.get("hypothesis") == "h1" and i.get("active")]
        self.assertTrue(active, active)
        self.assertTrue(any(i.get("observation") == obs_b["id"] for i in active))
        self.assertNotEqual(h.get("status"), "contradicted")

    def test_invalidated_refutation_requires_review_and_keeps_history(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "against",
            "conditions": "one experiment", "rationale": "negative",
            "source": "planner",
        }], direction={"choice": "pause", "hypothesis": "h1",
                        "reason": "review before more spend"}))
        interp = next(i for i in eng.state["research"]["interpretations"].values()
                      if i.get("relation") == "against")
        with store.lock():
            eng.state = store.load()
            old = copy.deepcopy(eng.state)
            eng.state["evidence"][p["evidence"][0]]["verified"] = False
            eng.state["evidence"][p["evidence"][0]]["stale"] = True
            eng.state = store.commit(old, eng.state, "test_invalidate_refutation", {})
        eng._refresh_research()
        active = next(i for i in eng.state["research"]["interpretations"].values()
                      if i["id"] == interp["id"])
        self.assertFalse(active.get("active"))
        self.assertTrue(active.get("history"))
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")
        self.assertTrue(any(p.get("trigger") == "evidence_invalidated"
                            for p in self.pending(eng)))

    def test_legacy_refutation_is_reopened_when_its_evidence_invalidates(self):
        self.cfg["loop"]["research_mode"] = False
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, dispatch(task(
            "plain", acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
            acceptance_policy="deterministic",
            checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(0)"]}],
        )))
        ev = next(e for e in eng.state["evidence"].values() if e.get("kind") == "artifact")
        with store.lock():
            eng.state = store.load()
            old = copy.deepcopy(eng.state)
            eng.state["refuted"].append({
                "id": "old", "text": "old refutation",
                "reason": "legacy", "evidence": [ev["id"]],
            })
            eng.state = store.commit(old, eng.state, "test_legacy_refutation", {})
        self.cfg["loop"]["research_mode"] = True
        with store.lock():
            reopened = engine.Engine(store, self.cfg, eng.roles, eng.backends)
            reopened.open()
            reopened.state["evidence"][ev["id"]]["stale"] = True
            reopened.state["evidence"][ev["id"]]["verified"] = False
            reopened._refresh_research()
            self.assertTrue(any(p.get("kind") == "legacy_refutation_invalidated"
                                for p in reopened.state["research"]["pending_reviews"]))
            self.assertEqual(reopened.state["refuted"][0]["assessment_status"],
                             "needs_review")
            self.assertTrue(reopened.state["refuted"][0]["history"])

    def test_explicit_contradicted_status_is_not_automatic(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "against",
            "conditions": "one run", "rationale": "negative", "source": "planner",
        }], direction={"choice": "pause", "hypothesis": "h1", "reason": "inspect"}))
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")
        self.apply(eng, store, {
            "action": "review", "reason": "explicit assessment after review",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "H1", "status": "contradicted",
            }]},
        })
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "contradicted")
        self.assertEqual(eng.state["hypotheses"]["h1"]["version"], 1)

    def test_hypothesis_revision_marks_old_interpretation_for_review(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "supports",
            "conditions": "initial", "rationale": "support", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "continue"}))
        old = next(i for i in eng.state["research"]["interpretations"].values()
                   if i.get("hypothesis") == "h1")
        self.apply(eng, store, {
            "action": "review", "reason": "narrow definition",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "narrowed H1", "scope": "narrowed",
                "version": 1,
            }]},
        })
        updated = next(i for i in eng.state["research"]["interpretations"].values()
                       if i["id"] == old["id"])
        self.assertFalse(updated["active"])
        self.assertEqual(updated["status"], "needs_review")
        self.assertTrue(updated["history"])
        self.assertEqual(eng.state["hypotheses"]["h1"]["version"], 2)

    def test_reason_and_duplicate_observation_do_not_change_research_progress(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        first = self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "", "rationale": "first reason", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "first direction"})
        self.apply(eng, store, first)
        fp1 = eng._progress_fp()
        second = self.review([], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "", "rationale": "rewritten reason", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "rewritten direction"})
        self.apply(eng, store, second)
        self.assertEqual(eng._progress_fp(), fp1)

    def test_route_switch_does_not_invalidate_all_artifacts(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(
            key_task("a"), key_task("b", hypotheses=("h1",))))
        rids = [p["id"] for p in self.pending(eng)]
        self.apply(eng, store, self.review(rids, interpretations=[
            {"observation": self.obs_for(eng, "a")["id"], "hypothesis": "h1",
             "hypothesis_version": 1, "relation": "against",
             "conditions": "a", "rationale": "a", "source": "planner"},
            {"observation": self.obs_for(eng, "b")["id"], "hypothesis": "h1",
             "hypothesis_version": 1, "relation": "inconclusive",
             "conditions": "b", "rationale": "b", "source": "planner"},
        ], direction={"choice": "switch", "hypothesis": "h1",
                        "reason": "alternative route"}))
        self.assertEqual(eng.state["tasks"]["a"]["artifact"]["sha256"],
                         eng.state["tasks"]["a"]["artifact"]["sha256"])
        # The unrelated artifact remains a current observation; only explicit
        # changed/dependent items may be stale.
        self.assertEqual(self.obs_for(eng, "b")["status"], "current")
        self.assertEqual(eng.state["tasks"]["b"]["status"], "completed")

    def test_test_hypothesis_and_depend_hypothesis_are_distinct(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        test = text_task("test", hypotheses=("h1",))
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",))
        self.apply(eng, store, self.dispatch_with_hypotheses(test, dep))
        # Mark h1 superseded through an explicit validity update, without
        # invalidating the task that only tested it.
        self.apply(eng, store, {
            "action": "review", "reason": "supersede h1",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "H1", "scope": "initial scope",
                "status": "superseded"}]},
        })
        self.assertEqual(eng.state["tasks"]["test"]["status"], "completed")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "needs_review")
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "superseded")

    def test_same_observation_is_not_counted_as_independent(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[
            {"observation": p["observation"], "hypothesis": "h1",
             "hypothesis_version": 1, "relation": "supports",
             "conditions": "same", "rationale": "first", "source": "planner"},
            {"observation": p["observation"], "hypothesis": "h1",
             "hypothesis_version": 1, "relation": "supports",
             "conditions": "same", "rationale": "duplicate",
             "source": "planner", "independent": True},
        ], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "continue with support"}))
        interps = [i for i in eng.state["research"]["interpretations"].values()
                   if i.get("hypothesis") == "h1"]
        self.assertEqual(len(interps), 2)
        self.assertEqual(len({i["source_group"] for i in interps}), 1)
        self.assertEqual(eng.state["hypotheses"]["h1"]["evidence_for"],
                         p["evidence"])

    def _data(self, name):
        d = self.root / "run"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("data\n", encoding="utf-8")


class ResearchProtocolAndCompatibility(ResearchCase):
    def test_same_event_does_not_duplicate_pending_after_reload(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        self.assertEqual(len(self.pending(eng)), 1)
        with store.lock():
            reloaded = engine.Engine(store, self.cfg, eng.roles, eng.backends)
            reloaded.open()
            reloaded._refresh_research()
            self.assertEqual(len(reloaded.state["research"]["pending_reviews"]), 1)

    def test_same_review_submission_is_not_duplicated(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        decision = self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "same", "rationale": "inconclusive", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "continue"})
        self.apply(eng, store, decision)
        n_reviews = len(eng.state["research"]["reviews"])
        self.apply(eng, store, decision)
        self.assertEqual(len(eng.state["research"]["reviews"]), n_reviews)

    def test_packet_trimming_keeps_research_refs(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        packet = packetmod.build_packet(eng.state, max_chars=2000, run_dir=store.dir)
        self.assertIn("research", packet)
        self.assertIn("h1", [h["id"] for h in packet["research"]["active_hypotheses"]])
        self.assertIn(p["id"], [r["id"] for r in packet["research"]["pending_results"]])
        self.assertIn(p["evidence"][0],
                      [e for r in packet["research"]["pending_results"]
                       for e in r.get("evidence", [])])

    def test_old_run_loads_without_research_behavior(self):
        cfg = self.cfg
        cfg["loop"]["research_mode"] = False
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.assertFalse(eng.state.get("research_mode", False))
        self.assertNotIn("research", eng.state)
        self.apply(eng, store, dispatch(task(
            "plain", satisfies=["g1"],
            acceptance=[{"id": "c1", "text": "check", "check": "unit"}],
            acceptance_policy="deterministic",
            checks=[{"name": "unit", "argv": ["python3", "-c", "raise SystemExit(0)"]}],
        )))
        self.assertEqual(eng.state["tasks"]["plain"]["status"], "completed")

    def test_legacy_hypotheses_and_refuted_are_marked_unassessed(self):
        cfg = self.cfg
        cfg["loop"]["research_mode"] = False
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        with store.lock():
            eng.state = store.load()
            old = copy.deepcopy(eng.state)
            eng.state["hypotheses"]["old"] = {
                "id": "old", "text": "old hypothesis", "status": "refuted",
                "reason": "old claim", "evidence": [],
            }
            eng.state["refuted"].append({"id": "old", "text": "old hypothesis",
                                         "reason": "old claim", "evidence": []})
            eng.state = store.commit(old, eng.state, "test_legacy", {})
        cfg["loop"]["research_mode"] = True
        with store.lock():
            reopened = engine.Engine(store, cfg, eng.roles, eng.backends)
            reopened.open()
            reopened._migrate_research_state()
            h = reopened.state["hypotheses"]["old"]
            self.assertEqual(h["status"], "legacy")
            self.assertTrue(h["legacy"])
            self.assertEqual(h["assessment_status"], "unassessed")
            self.assertFalse(reopened.state["refuted"][0].get("active", True))
            self.assertTrue(reopened.state["refuted"][0]["legacy"])

    def test_key_result_requires_an_interpretation_and_direction(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, self.review([p["id"]], direction={
                "choice": "continue", "hypothesis": "h1", "reason": "no explanation"}))
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, self.review([p["id"]], interpretations=[{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "inconclusive",
                "conditions": "", "rationale": "ambiguous", "source": "planner",
            }]))

    def test_key_result_does_not_add_a_model_call(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        calls_before = len(eng.backends["worker"].calls)
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        self.assertEqual(len(eng.backends["worker"].calls), calls_before + 1)
        self.assertEqual(len(self.pending(eng)), 1)
        # Building the research pending state did not invoke planner/reviewer.
        self.assertEqual(len(eng.backends["worker"].calls), calls_before + 1)
        self.assertEqual(len(eng.backends["planner"].calls), 0)

    def test_completion_and_budget_still_work_in_research_mode(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        plain = text_task("plain", hypotheses=("h1",), satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(plain))
        self.assertEqual(eng.state["tasks"]["plain"]["status"], "completed")
        ev = next(eid for eid, rec in eng.state["evidence"].items()
                  if rec.get("task") == "plain" and rec.get("completion_usable"))
        out = self.apply(eng, store, complete(ev))
        self.assertEqual(out["status"], "complete")
        self.assertGreaterEqual(eng.state["budget"]["limits"]["max_calls"], 1)

    def test_review_action_only_does_not_create_tasks(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + SELF,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        before = len(eng.state["tasks"])
        out = self.apply(eng, store, self.review(
            [p["id"]], interpretations=[{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "inconclusive",
                "conditions": "same", "rationale": "ambiguous",
                "source": "planner",
            }], direction={"choice": "pause", "hypothesis": "h1",
                            "reason": "wait for user"}))
        self.assertNotEqual(out["status"], "paused")
        self.assertEqual(len(eng.state["tasks"]), before)
        self.assertEqual(eng.state["research"]["reviews"][-1]["direction"]["choice"],
                         "pause")


if __name__ == "__main__":
    unittest.main()
