"""Smoke tests for the offline research fixtures (no model calls)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from research import fixtures  # noqa: E402


class ResearchFixtures(unittest.TestCase):
    def test_four_fixed_replay_cases_are_present(self):
        self.assertEqual(len(fixtures.FIXED_CASES), 4)
        replay = fixtures.EvidenceReplay(fixtures.FIXED_CASES[0])
        events = []
        while True:
            event = replay.next()
            if event is None:
                break
            events.append(event["id"])
        self.assertEqual(events, [e["id"] for e in fixtures.FIXED_CASES[0]["evidence_order"]])
        self.assertEqual(replay.remaining(), 0)

    def test_choice_environment_charges_and_discriminates(self):
        env = fixtures.ChoiceEnvironment(truth_h1=True)
        cheap = env.run("observe_1")
        test = env.run("transfer_3")
        self.assertEqual(set(cheap), {"option", "observation", "cost"})
        self.assertEqual(set(test), {"option", "observation", "cost"})
        self.assertEqual(test["observation"], "X")
        self.assertEqual(env.spent(), cheap["cost"] + test["cost"])
        truth = env.evaluator_truth()
        self.assertTrue(truth["h1"])
        self.assertEqual(len(truth["runs"]), 2)
        self.assertFalse(truth["runs"][0]["discriminates"])
        self.assertTrue(truth["runs"][1]["discriminates"])

    def test_arms_expose_goal_labels_without_calling_a_model(self):
        goal = {"goal": "x", "completion_criteria": ["g1"]}
        for name in ("base", "reflection", "research"):
            out = fixtures.ChoiceEnvironment().arm_goal(name, goal)
            self.assertEqual(out["research"], name == "research")
            self.assertIn("_planner_suffix", out)


if __name__ == "__main__":
    unittest.main()
