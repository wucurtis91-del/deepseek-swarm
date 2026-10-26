"""Regression tests derived from the research-mode audit.

Each test is an observable state transition, not a helper-fingerprint assertion.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarmlib import engine, packet as packetmod, protocol, research, util  # noqa: E402
from test_loop import complete, dispatch  # noqa: E402
from test_research import ResearchCase, key_task, text_task  # noqa: E402


class CompletionOrdering(ResearchCase):
    def test_complete_plus_explicit_status_update_rolls_back_whole_decision(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        ev = self.evidence_for(eng, "dep")["id"]
        decision = complete(ev)
        decision["research"] = {"hypothesis_updates": [{
            "id": "h1", "text": "H1", "scope": "initial scope",
            "status": "superseded"}]}
        out = self.apply(eng, store, decision)
        self.assertNotEqual(out["status"], "complete")
        # A rejected combined decision must not leave the status update or
        # dependency propagation half-applied.
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "completed")
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "open")
        self.assertEqual(research.pending(eng.state), [])

    def test_complete_plus_non_invalidating_research_update_still_completes(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        plain = text_task("plain", hypotheses=(), satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(plain))
        decision = complete(self.evidence_for(eng, "plain")["id"])
        decision["research"] = {"direction": {
            "choice": "pause", "hypothesis": "h1", "reason": "goal reached"}}
        out = self.apply(eng, store, decision)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(eng.state["research"]["reviews"][-1]["choice"], "pause")


class PendingCoverage(ResearchCase):
    def test_resolved_review_cannot_cover_a_new_pending_result(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task("a")))
        first = self.pending(eng)[0]
        self.apply(eng, store, self.review([first["id"]], interpretations=[{
            "observation": first["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "a", "rationale": "a", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1", "reason": "a"}))
        self.apply(eng, store, dispatch(key_task("b", satisfies=["g1"])))
        second = research.pending(eng.state)[0]
        self.assertNotEqual(first["id"], second["id"])
        decision = complete(self.evidence_for(eng, "b")["id"])
        decision["research"] = {"review_ids": [first["id"]]}
        out = self.apply(eng, store, decision)
        self.assertNotEqual(out["status"], "complete")
        self.assertTrue(any(p["id"] == second["id"] and p["status"] == "pending"
                            for p in eng.state["research"]["pending_reviews"]))

    def test_wrong_hypothesis_version_cannot_resolve_review(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, self.review([p["id"]], interpretations=[{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 999, "relation": "supports",
                "conditions": "", "rationale": "wrong version", "source": "planner",
            }], direction={"choice": "continue", "hypothesis": "h1",
                            "reason": "wrong version"}))
        self.assertEqual(self.pending(eng)[0]["id"], p["id"])

    def test_exact_replay_does_not_cover_a_later_pending_event(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task("a")))
        first = self.pending(eng)[0]
        decision = self.review([first["id"]], interpretations=[{
            "observation": first["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "a", "rationale": "a", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1", "reason": "a"})
        self.apply(eng, store, decision)
        reviews = len(eng.state["research"]["reviews"])
        self.apply(eng, store, dispatch(key_task("b")))
        second = research.pending(eng.state)[0]
        self.assertNotEqual(first["id"], second["id"])
        out = self.apply(eng, store, decision)
        self.assertNotEqual(out["status"], "paused")
        self.assertEqual(len(eng.state["research"]["reviews"]), reviews)
        self.assertEqual(research.pending(eng.state)[0]["id"], second["id"])

    def test_superseded_interpretation_cannot_resolve_a_new_result(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        stale = {
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "supports",
            "conditions": "same", "rationale": "old support",
            "source": "planner",
        }
        self.apply(eng, store, self.review([p["id"]], interpretations=[stale],
                                           direction={"choice": "continue",
                                                      "hypothesis": "h1",
                                                      "reason": "first"}))
        old = next(i for i in eng.state["research"]["interpretations"].values()
                   if i.get("active") and i.get("relation") == "supports")
        self.apply(eng, store, {
            "action": "review", "reason": "correct",
            "research": {"interpretations": [{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "against",
                "conditions": "same", "rationale": "corrected",
                "source": "planner", "supersedes": [old["id"]],
            }], "direction": {"choice": "continue", "hypothesis": "h1",
                              "reason": "corrected"}},
        })
        self.assertFalse(eng.state["research"]["interpretations"][old["id"]]["active"])
        event = None
        with store.lock():
            eng.state = store.load()
            old_state = copy.deepcopy(eng.state)
            event = research._add_pending(
                eng.state, kind="key_result", event_key="new-key-event",
                task=eng.state["tasks"]["probe"],
                evidence={"id": next(e["id"] for e in eng.state["evidence"].values()
                                      if e.get("task") == "probe")},
                observation={"id": p["observation"]},
                hypothesis_ids=["h1"], reason="new key result", round_no=2,
                trigger="key_experiment_completed")
            eng.state = store.commit(old_state, eng.state, "test_new_pending", {})
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, self.review([event["id"]],
                                               interpretations=[stale],
                                               direction={"choice": "continue",
                                                          "hypothesis": "h1",
                                                          "reason": "old support"}))
        self.assertIn(event["id"], [q["id"] for q in research.pending(eng.state)])


class EvidenceRefresh(ResearchCase):
    def test_deleted_artifact_cannot_be_used_as_support(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        obs = eng.state["research"]["observations"][p["observation"]]
        (store.dir / obs["artifact"]).unlink()
        with self.assertRaises(protocol.DecisionError):
            self.apply(eng, store, self.review([p["id"]], interpretations=[{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "supports",
                "conditions": "", "rationale": "must not support",
                "source": "planner",
            }], direction={"choice": "continue", "hypothesis": "h1",
                            "reason": "invalid artifact"}))
        self.assertEqual(eng.state["hypotheses"]["h1"]["status"], "open")
        self.assertFalse(any(i.get("active") and i.get("relation") == "supports"
                             for i in eng.state["research"]["interpretations"].values()))

    def test_invalidated_event_can_be_closed_without_resubmitting_support(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        obs = eng.state["research"]["observations"][p["observation"]]
        (store.dir / obs["artifact"]).unlink()
        decision = {
            "action": "review", "reason": "acknowledge invalid measurement",
            "research": {"direction": {
                "choice": "experiment", "hypothesis": "h1",
                "reason": "rerun with a valid instrument"}},
        }
        # Discovery happens before the review decision, then the invalidation
        # event can be reviewed without pretending the invalid observation
        # supports H1.
        eng._prepare_research_view()
        invalid = next(q for q in research.pending(eng.state)
                       if q["kind"] == "evidence_invalidated")
        self.apply(eng, store, {
            "action": "review", "reason": "close invalidation",
            "research": {
                "review_ids": [invalid["id"]],
                "direction": {"choice": "experiment", "hypothesis": "h1",
                              "reason": "rerun with a valid instrument"},
            },
        })
        self.assertFalse(any(q["id"] == invalid["id"]
                             for q in research.pending(eng.state)))
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "supported")
        self.assertFalse(any(i.get("active")
                             for i in eng.state["research"]["interpretations"].values()))


class HypothesisVersions(ResearchCase):
    def test_dependent_task_is_not_completed_after_hypothesis_revision(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        dep = text_task("dep", hypotheses=(), depends_hypotheses=("h1",),
                        satisfies=["g1"])
        self.apply(eng, store, self.dispatch_with_hypotheses(dep))
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "completed")
        self.apply(eng, store, {
            "action": "review", "reason": "redefine H1",
            "research": {"hypothesis_updates": [{
                "id": "h1", "text": "a different H1", "scope": "new scope",
            }]},
        })
        for p in [p for p in research.pending(eng.state)
                  if p["kind"] == "hypothesis_revised"]:
            self.apply(eng, store, self.review([p["id"]], direction={
                "choice": "continue", "hypothesis": "h1",
                "reason": "acknowledge revision"}))
        out = self.apply(eng, store, complete(self.evidence_for(eng, "dep")["id"]))
        self.assertNotEqual(out["status"], "complete")
        self.assertEqual(eng.state["tasks"]["dep"]["status"], "needs_review")


class InterpretationCorrection(ResearchCase):
    def test_superseding_support_with_against_does_not_leave_contested_history(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "supports",
            "conditions": "same", "rationale": "first", "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1", "reason": "support"}))
        old = next(i for i in eng.state["research"]["interpretations"].values()
                   if i.get("active") and i.get("relation") == "supports")
        self.apply(eng, store, {
            "action": "review", "reason": "correct the interpretation",
            "research": {"interpretations": [{
                "observation": p["observation"], "hypothesis": "h1",
                "hypothesis_version": 1, "relation": "against",
                "conditions": "same", "rationale": "corrected",
                "source": "planner", "supersedes": [old["id"]],
            }], "direction": {"choice": "experiment", "hypothesis": "h1",
                              "reason": "correct prior reading"}},
        })
        self.assertFalse(next(i for i in eng.state["research"]["interpretations"].values()
                              if i["id"] == old["id"])["active"])
        active = [i for i in eng.state["research"]["interpretations"].values()
                  if i.get("active")]
        self.assertEqual([i["relation"] for i in active], ["against"])
        self.assertNotEqual(eng.state["hypotheses"]["h1"]["status"], "contested")


class NoProgress(ResearchCase):
    def test_reworded_research_reviews_stall_at_the_configured_threshold(self):
        self.cfg["loop"]["no_progress_limit"] = 2
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(
            text_task("a", hypotheses=(), key_experiment=False)))
        statuses = []
        for i in range(7):
            out = self.apply(eng, store, {
                "action": "review", "reason": f"same decision wording {i}",
                "research": {"direction": {
                    "choice": "continue", "hypothesis": "h1",
                    "reason": f"same direction wording {i}"}},
            })
            statuses.append(out["status"])
        self.assertTrue(any(s == "paused" for s in statuses), statuses)
        self.assertIn("no_progress", eng.state.get("stop_reason", ""))

    def test_reworded_rationale_does_not_reset_the_stall_counter(self):
        self.cfg["loop"]["no_progress_limit"] = 2
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = self.pending(eng)[0]
        self.apply(eng, store, self.review([p["id"]], interpretations=[{
            "observation": p["observation"], "hypothesis": "h1",
            "hypothesis_version": 1, "relation": "inconclusive",
            "conditions": "same", "rationale": "first wording",
            "source": "planner",
        }], direction={"choice": "continue", "hypothesis": "h1",
                        "reason": "first direction"}))
        statuses = []
        for i in range(5):
            out = self.apply(eng, store, {
                "action": "review", "reason": f"rewritten decision {i}",
                "research": {
                    "interpretations": [{
                        "observation": p["observation"], "hypothesis": "h1",
                        "hypothesis_version": 1, "relation": "inconclusive",
                        "conditions": "same",
                        "rationale": f"rewritten rationale {i}",
                        "source": "planner",
                    }],
                    "direction": {"choice": "continue", "hypothesis": "h1",
                                  "reason": f"rewritten direction {i}"},
                },
            })
            statuses.append(out["status"])
        self.assertTrue(any(s == "paused" for s in statuses), statuses)
        self.assertIn("no_progress", eng.state.get("stop_reason", ""))


class PacketPagination(unittest.TestCase):
    def _state_with_hypotheses(self, count=12):
        st = {"research_mode": True, "hypotheses": {}, "tasks": {}, "evidence": {}}
        research.ensure(st)
        for n in range(count):
            st["hypotheses"][f"h{n}"] = {
                "id": f"h{n}", "version": 1, "status": "open",
                "text": "mechanism " + "x" * 120,
                "scope": "s" * 80, "core": True, "alternatives": [],
                "evidence_for": [], "evidence_against": [],
            }
        return st

    def test_empty_pending_oversize_hypotheses_reports_budget(self):
        st = self._state_with_hypotheses(12)
        section = research.packet_section(st, max_chars=1200)
        body = util.canonical(section)
        self.assertGreater(len(body), 1200)
        self.assertEqual(section["pending_total"], 0)
        self.assertEqual(section["pending_offset"], 0)
        self.assertEqual(section["pending_returned"], 0)
        self.assertIsNone(section["next_offset"])
        self.assertTrue(section["limits"].get("over_budget"))
        self.assertGreaterEqual(section["limits"]["min_chars"], len(body))
        self.assertTrue(section["limits"].get("truncated"))
        self.assertEqual([h["id"] for h in section["active_hypotheses"]],
                         sorted(f"h{n}" for n in range(12)))

    def test_page_end_remaining_structure_still_reports_budget(self):
        st = self._state_with_hypotheses(12)
        for n in range(2):
            research._add_pending(
                st, kind="key_result", event_key=str(n), task={},
                evidence={"id": f"ev-{n}"}, observation={"id": f"obs-{n}"},
                hypothesis_ids=["h0"], reason="key result", round_no=1,
                trigger="key_experiment_completed")
        section = research.packet_section(st, max_chars=1200, offset=2)
        body = util.canonical(section)
        self.assertGreater(len(body), 1200)
        self.assertEqual(section["pending_total"], 2)
        self.assertEqual(section["pending_offset"], 2)
        self.assertEqual(section["pending_returned"], 0)
        self.assertIsNone(section["next_offset"])
        self.assertTrue(section["limits"].get("over_budget"))
        self.assertGreaterEqual(section["limits"]["min_chars"], len(body))
        self.assertTrue(section["limits"].get("truncated"))
        self.assertEqual(len(section["active_hypotheses"]), 12)

    def test_empty_pending_within_budget_returns_normally(self):
        st = self._state_with_hypotheses(1)
        section = research.packet_section(st, max_chars=10000)
        body = util.canonical(section)
        self.assertLessEqual(len(body), 10000)
        self.assertEqual(section["pending_total"], 0)
        self.assertEqual(section["pending_offset"], 0)
        self.assertEqual(section["pending_returned"], 0)
        self.assertIsNone(section["next_offset"])
        self.assertFalse(section["limits"].get("over_budget"))
        self.assertFalse(section["limits"].get("truncated"))
        self.assertEqual(section["active_hypotheses"][0]["id"], "h0")

    def test_thirty_pending_items_fit_and_page_through_small_budget(self):
        st = {"research_mode": True, "hypotheses": {}, "tasks": {}, "evidence": {}}
        research.ensure(st)
        for n in range(30):
            research._add_pending(
                st, kind="key_result", event_key=str(n), task={},
                evidence={"id": f"ev-{n}"}, observation={"id": f"obs-{n}"},
                hypothesis_ids=["h1"], reason="key result", round_no=1,
                trigger="key_experiment_completed")
        seen = []
        offset = 0
        while True:
            section = research.packet_section(st, max_chars=1200, offset=offset)
            body = util.canonical(section)
            limits = section["limits"]
            if limits.get("over_budget"):
                self.assertGreaterEqual(limits["min_chars"], len(body))
            else:
                self.assertLessEqual(len(body), 1200)
            page = section["pending_results"]
            seen.extend(p["id"] for p in page)
            if section.get("next_offset") is None:
                break
            offset = section["next_offset"]
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)

    def test_research_section_reports_totals_and_cursor(self):
        st = {"research_mode": True, "hypotheses": {}, "tasks": {}, "evidence": {}}
        research.ensure(st)
        for n in range(30):
            research._add_pending(
                st, kind="key_result", event_key=str(n), task={},
                evidence={"id": f"ev-{n}"}, observation={"id": f"obs-{n}"},
                hypothesis_ids=["h1"], reason="key result", round_no=1,
                trigger="key_experiment_completed")
        section = research.packet_section(st, max_chars=1200)
        self.assertEqual(section["pending_total"], 30)
        self.assertGreater(section["pending_returned"], 0)
        self.assertIsNotNone(section["next_offset"])

    def test_minimum_structure_oversize_reports_required_space(self):
        st = {"research_mode": True, "hypotheses": {}, "tasks": {}, "evidence": {}}
        research.ensure(st)
        research._add_pending(
            st, kind="key_result", event_key="one", task={},
            evidence={"id": "ev-1"}, observation={"id": "obs-1"},
            hypothesis_ids=["h1"], reason="key result", round_no=1,
            trigger="key_experiment_completed")
        section = research.packet_section(st, max_chars=1)
        body = util.canonical(section)
        self.assertTrue(section["limits"].get("over_budget"))
        self.assertGreaterEqual(section["limits"]["min_chars"], len(body))
        self.assertTrue(section["limits"].get("truncated"))


class CompressionProtection(ResearchCase):
    def _packet(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        return eng, packetmod.build_packet(eng.state, run_dir=store.dir)

    def _compress(self, eng, packet, transform):
        candidate = copy.deepcopy(packet)
        transform(candidate)
        eng.cfg["loop"]["packet_compress"] = {"enabled": True, "min_chars": 1}
        eng.roles["compressor"] = replace(eng.roles["reviewer"],
                                          name="compressor", enabled=True)
        eng.backends["compressor"] = object()
        with mock.patch.object(eng, "_call_model", return_value={
                "ok": True, "text": json.dumps(candidate)}):
            return eng._maybe_compress(packet)

    def test_empty_null_or_tampered_research_is_rejected(self):
        eng, packet = self._packet()
        for transform in (
            lambda p: p.__setitem__("research", {}),
            lambda p: p.__setitem__("research", None),
            lambda p: p["research"]["pending_results"][0].__setitem__(
                "evidence", ["ev-tampered"]),
        ):
            out = self._compress(eng, packet, transform)
            self.assertFalse(out["packet_meta"].get("compressed"), out.get("packet_meta"))
            self.assertIn("research", out)

    def test_normal_smaller_compression_still_works(self):
        eng, packet = self._packet()
        out = self._compress(eng, packet, lambda p: p.__setitem__(
            "state_summary", {"short": "yes"}))
        self.assertTrue(out["packet_meta"].get("compressed"), out.get("packet_meta"))


class ResearchContext(ResearchCase):
    def test_research_page_context_request_returns_the_pending_page(self):
        eng, store = self.start({"planner": [], "worker": lambda **kw: "ok" + __import__(
            "test_loop").SELF, "reviewer": [], "critic": []})
        self.apply(eng, store, self.dispatch_with_hypotheses(key_task()))
        p = research.pending(eng.state)[0]
        with store.lock():
            eng.state = store.load()
        notes = eng._fulfill_context_requests([
            {"research_page": 0, "limit": 4000}])
        self.assertEqual(notes[0]["request"]["research_page"], 0)
        section = notes[0]["content"]
        self.assertEqual(section["pending_results"][0]["id"], p["id"])


class FixtureVisibility(unittest.TestCase):
    def test_public_case_has_no_future_or_scoring_material(self):
        from research import fixtures
        for case in fixtures.FIXED_CASES:
            public = fixtures.public_case(case)
            self.assertEqual(
                set(public),
                {"id", "title", "description", "initial", "legal_operations"},
                case["id"])
            for key in ("evaluator_only", "reasonable_decisions", "error_patterns",
                        "evidence_order"):
                self.assertNotIn(key, public, case["id"])
            self.assertIn("initial", public)
            self.assertIn("legal_operations", public)

    def test_choice_result_has_no_evaluator_labels(self):
        from research import fixtures
        env = fixtures.ChoiceEnvironment()
        for option in fixtures.ChoiceEnvironment.OPTIONS:
            self.assertNotIn("valid", fixtures.ChoiceEnvironment.OPTIONS[option])
            self.assertNotIn("discriminates", fixtures.ChoiceEnvironment.OPTIONS[option])
            result = env.run(option)
            self.assertEqual(set(result), {"option", "observation", "cost"})
            self.assertNotIn("valid", result)
            self.assertNotIn("discriminates", result)
            self.assertNotIn("false contradiction", result["observation"].lower())
            self.assertNotIn("known-bad",
                             fixtures.ChoiceEnvironment.OPTIONS[option]["description"].lower())

    def test_replayed_evidence_hides_evaluator_strength(self):
        from research import fixtures
        replay = fixtures.EvidenceReplay(fixtures.FIXED_CASES[0])
        event = replay.next()
        self.assertEqual(set(event), {"id", "kind", "result", "source"})
        self.assertNotIn("strength", event)


if __name__ == "__main__":
    unittest.main()
