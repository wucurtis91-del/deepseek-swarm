"""Regression tests for the research-mode follow-up audit.

Each test exercises an observable controller transition and was written before
the corresponding fix.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmlib import engine, protocol, research  # noqa: E402
from test_loop import complete, dispatch  # noqa: E402
from test_research import ResearchCase, key_task, text_task  # noqa: E402


class CompletionPropagation(ResearchCase):
    def test_explicit_status_update_in_complete_is_propagated_then_rolled_back(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        decision = complete(self.evidence_for(eng, "dep")["id"])
        decision["research"] = {"hypothesis_updates": [{
            "id": "h1", "text": "H1", "scope": "initial scope",
            "status": "superseded",
        }]}
        out = self.apply(eng, store, decision)
        self.assertNotEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "completed")
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "open")
        self.assertEqual(research.pending(eng.state), [])
        self.assertIsNone(eng.state.get("completion"))

    def test_direction_switch_in_complete_is_investment_only_and_stable(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        decision = complete(self.evidence_for(eng, "dep")["id"])
        decision["research"] = {"direction": {
            "choice": "switch", "hypothesis": "h1",
            "reason": "prioritize a cheaper route"}}
        out = self.apply(eng, store, decision)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "completed")
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "open")
        self.assertEqual(research.pending(eng.state), [])
        # Reopening and refreshing must not create a new invalidation caused by
        # this decision's own research update.
        with store.lock():
            reopened = engine.Engine(store, self.cfg, eng.roles, eng.backends)
            reopened.open()
            reopened._prepare_research_view()
        self.assertEqual(reopened.state["status"], "complete")
        self.assertEqual(reopened.state["tasks"]["dep"]["status"], "completed")
        self.assertEqual(research.pending(reopened.state), [])


class ReplayExecutionPermit(ResearchCase):
    def test_old_research_payload_does_not_permit_new_dispatch(self):
        calls = []

        def worker(**kw):
            calls.append(kw.get("prompt", ""))
            return "ok" + __import__("test_loop").SELF

        eng, store = self.start({"planner": [], "worker": worker,
                                 "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task("a")))
        first = research.pending(eng.state)[0]
        review = self.review([first["id"]], interpretations=[{
            "observation": first["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "pilot", "rationale": "pilot", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "more data"})
        self.apply(eng, store, review)
        self.apply(eng, store, dispatch(key_task("b")))
        pending_b = [p["id"] for p in research.pending(eng.state)]
        self.assertTrue(pending_b)
        reviews_before = len(eng.state["research"]["reviews"])
        calls_before = len(calls)
        new_dispatch = dispatch(text_task("c"))
        new_dispatch["research"] = copy.deepcopy(review["research"])
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, new_dispatch)
        self.assertEqual(len(calls), calls_before)
        self.assertNotIn("c", eng.state["tasks"])
        self.assertEqual([p["id"] for p in research.pending(eng.state)],
                         pending_b)
        # The exact research payload is still an idempotent review replay; it
        # does not consume the new event.
        out = self.apply(eng, store, review)
        self.assertNotEqual(out["status"], "paused")
        self.assertEqual(len(eng.state["research"]["reviews"]), reviews_before)
        self.assertEqual([p["id"] for p in research.pending(eng.state)],
                         pending_b)

    def test_batch_review_of_current_pending_still_works(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(
            key_task("a"), key_task("b")))
        pending = list(self.pending(eng))
        self.assertEqual(len(pending), 2)
        self.apply(eng, store, self.review(
            [p["id"] for p in pending],
            interpretations=[{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "inconclusive",
                "conditions": p["id"], "rationale": p["id"],
                "source": "planner",
            } for p in pending],
            direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "batch review"}))
        self.assertEqual(research.pending(eng.state), [])


class DirectionValiditySeparation(ResearchCase):
    def test_cost_switch_does_not_change_hypothesis_or_dependent_task(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(
            key_task("test"),
            text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                      satisfies=["g1"])))
        p = research.pending(eng.state)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "supports",
            "conditions": "pilot", "rationale": "valid support",
            "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "supported"}))
        before = {
            "hypothesis": eng.state["hypotheses"]["h1"]["status"],
            "dep": eng.state["tasks"]["dep"]["status"],
        }
        self.apply(eng, store, self.review([], direction={
            "choice": "switch", "hypothesis": "h1",
            "reason": "H1 still supported; prioritize a cheaper route only"}))
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"],
                         before["hypothesis"])
        self.assertEqual(eng.state["tasks"]["dep"]["status"], before["dep"])
        self.assertEqual(research.pending(eng.state), [])
        self.assertTrue(any(i.get("active") and i.get("relation") == "supports"
                            for i in eng.state["research"]["interpretations"].values()))

    def test_explicit_definition_change_still_marks_dependent_task(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        self.apply(eng, store, {
            "action": "review", "reason": "explicit definition change",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "new H1", "scope": "new scope",
            }]},
        })
        self.assertEqual(eng.state["hypotheses"]["h1"]["version"], 2)
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "needs_review")
        self.assertTrue(any(p["kind"] == "hypothesis_dependency_stale"
                            for p in research.pending(eng.state)))

    def test_explicit_status_change_still_marks_dependent_task(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        self.apply(eng, store, {
            "action": "review", "reason": "explicit validity change",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "H1", "scope": "initial scope",
                "status": "superseded",
            }]},
        })
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "superseded")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "needs_review")
        self.assertTrue(any(p["kind"] == "hypothesis_dependency_stale"
                            for p in research.pending(eng.state)))

    def test_any_explicit_status_change_reopens_dependent_task(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        self.apply(eng, store, {
            "action": "review", "reason": "explicit uncertainty",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "H1", "scope": "initial scope",
                "status": "inconclusive",
            }]},
        })
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "inconclusive")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "needs_review")
        self.assertTrue(any(p["kind"] == "hypothesis_dependency_stale"
                            for p in research.pending(eng.state)))


class CancelNeedsReview(ResearchCase):
    def test_cancel_needs_review_task_and_complete_with_replacement(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        self.apply(eng, store, {
            "action": "review", "reason": "revise hypothesis",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "new H1", "scope": "new scope",
            }]},
        })
        pending = list(research.pending(eng.state))
        self.assertTrue(pending)
        self.apply(eng, store, self.review(
            [p["id"] for p in pending],
            direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "use the new version"}))
        old_artifact = store.dir / eng.state["tasks"]["dep"]["artifact"]["path"]
        self.apply(eng, store, {
            "action": "replan", "reason": "discard obsolete dependent task",
            "cancel": ["dep"],
            "tasks": [text_task("replacement", hypotheses=(),
                                depends_hypotheses=("h1",), satisfies=["g1"])],
        })
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "cancelled")
        self.assertEqual(eng.state["tasks"]["replacement"]["status"], "completed")
        self.assertTrue(old_artifact.exists())
        old_ev = self.evidence_for(eng, "dep")
        self.assertTrue(old_ev.get("superseded"))
        self.assertFalse(old_ev.get("verified"))
        invalidated = [p for p in research.pending(eng.state)
                       if p["kind"] == "evidence_invalidated"]
        self.assertTrue(invalidated)
        self.apply(eng, store, {
            "action": "review", "reason": "close canceled evidence",
            "research": {"review_ids": [p["id"] for p in invalidated],
                          "direction": {"choice": "continue", "hypothesis": "h1",
                                        "reason": "replacement result stands"}},
        })
        out = self.apply(eng, store,
                         complete(self.evidence_for(eng, "replacement")["id"]))
        self.assertEqual(out["status"], "complete")


class NeutralFixtureNames(unittest.TestCase):
    BLOCKED = (
        "wrong_direction", "valid_contradiction", "correct_direction",
        "implementation_difficulty", "measurement_error",
        "false_contradiction", "ambiguous", "competing", "invalid_test",
        "discriminating_test", "错误", "反证", "测量", "可行", "有效方向",
    )

    def test_public_case_metadata_is_neutral_and_mapped(self):
        from research import fixtures
        seen = set()
        for case in fixtures.FIXED_CASES:
            public = fixtures.public_case(case)
            text = json.dumps(public, ensure_ascii=False).lower()
            for token in self.BLOCKED:
                self.assertNotIn(token.lower(), text, case["id"])
            self.assertRegex(public["id"], r"^case-\d+$")
            self.assertNotEqual(public["id"], case["id"])
            self.assertEqual(fixtures.EVALUATOR_CASE_MAP[public["id"]],
                             case["id"])
            self.assertNotRegex(text, r"\be[123]\b")
            for outcome in ("ambiguous", "contradict", "invalid", "noisy"):
                self.assertNotIn(outcome, text, case["id"])
            seen.add(public["id"])
        self.assertEqual(len(seen), len(fixtures.FIXED_CASES))

    def test_choice_options_are_neutral_but_labels_stay_evaluator_side(self):
        from research import fixtures
        env = fixtures.ChoiceEnvironment()
        self.assertNotIn("invalid_test", fixtures.ChoiceEnvironment.OPTIONS)
        self.assertNotIn("discriminating_test", fixtures.ChoiceEnvironment.OPTIONS)
        for option, spec in fixtures.ChoiceEnvironment.OPTIONS.items():
            text = (option + " " + spec["description"]).lower()
            for token in self.BLOCKED:
                self.assertNotIn(token.lower(), text, option)
            result = env.run(option)
            self.assertEqual(set(result), {"option", "observation", "cost"})
        labels = fixtures.ChoiceEnvironment.EVALUATOR_OPTIONS
        self.assertEqual(set(labels), set(fixtures.ChoiceEnvironment.OPTIONS))
        self.assertTrue(any(not v["valid"] for v in labels.values()))
        self.assertTrue(any(v["discriminates"] for v in labels.values()))
        self.assertFalse(any("valid" in (o + " " + s["description"]).lower()
                             for o, s in fixtures.ChoiceEnvironment.OPTIONS.items()))


if __name__ == "__main__":
    unittest.main()
