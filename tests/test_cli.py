"""End-to-end CLI smoke tests using only fake backends."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts" / "swarm_loop.py"


def run(*args, cwd=None):
    return subprocess.run([sys.executable, str(LOOP), *args], cwd=cwd or ROOT,
                          capture_output=True, text=True)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "goal.json").write_text(json.dumps({
            "goal": "cli", "completion_criteria": ["one answer"]}) + "\n")
        (self.dir / "config.json").write_text(json.dumps({"roles": {
            "planner": {"backend": "fake", "provider": "anthropic",
                        "model": "claude-opus-5-5", "auth": "local_login"},
            "worker": {"backend": "fake", "provider": "deepseek",
                       "model": "deepseek-flash", "auth": "api_key",
                       "api_key_env": "DEEPSEEK_API_KEY", "tools": "none"},
            "reviewer": {"backend": "fake", "provider": "deepseek",
                         "model": "deepseek-flash", "auth": "api_key",
                         "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"},
            "critic": {"backend": "fake", "provider": "deepseek",
                       "model": "deepseek-flash", "auth": "api_key",
                       "api_key_env": "DEEPSEEK_API_KEY", "enabled": False},
        }}) + "\n")
        self.review = ("```json\n" + json.dumps({
            "criteria": [{"id": "c1", "met": True, "note": "ok"}],
            "errors": [], "fix_instructions": ""}) + "\n```")
        self.worker = "answer\n```selfcheck\n{\"met\":[\"c1\"],\"not_met\":[],\"uncertain\":[]}\n```"
        self.dispatch = {"action": "dispatch", "reason": "start", "tasks": [{
            "id": "t1", "kind": "text", "prompt": "answer", "question": "q",
            "produces": "p", "decision_impact": "d", "tools": "none",
            "acceptance": [{"id": "c1", "text": "answer exists"}],
            "acceptance_policy": "semantic", "satisfies": ["g1"]}]}

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj, ensure_ascii=False) + "\n")
        return path

    def test_start_persists_config_independent_of_source_file(self):
        config_path = self.dir / "config.json"
        cfg = json.loads(config_path.read_text())
        cfg["roles"]["worker"]["model"] = "deepseek-v4-pro"
        cfg["loop"] = {"concurrency": 2}
        self.write("config.json", cfg)
        run_dir = self.dir / "saved"
        r = run("start", str(run_dir), "--goal", str(self.dir / "goal.json"),
                "--config", str(config_path), "--allow-fake-backend")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(saved["execution_config"]["roles"]["worker"]["model"],
                         "deepseek-v4-pro")
        self.assertEqual(saved["execution_config"]["loop"]["concurrency"], 2)
        config_path.unlink()
        r = run("next", str(run_dir), "--allow-fake-backend", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("config_changed", r.stdout)
        fake = self.write("saved-fake.json", {"worker": [self.worker],
                                               "reviewer": [self.review]})
        decision = self.write("saved-decision.json", self.dispatch)
        r = run("decide", str(run_dir), str(decision), "--allow-fake-backend",
                "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(saved["tasks"]["t1"]["status"], "completed")
        self.assertEqual(saved["tasks"]["t1"]["config_fingerprint"]["worker"]["model"],
                         "deepseek-v4-pro")
        # An explicit config still overrides the startup snapshot for this command.
        cfg["roles"]["worker"]["model"] = "deepseek-flash"
        override = self.write("override.json", cfg)
        self.dispatch["tasks"][0]["id"] = "t2"
        decision = self.write("override-decision.json", self.dispatch)
        r = run("decide", str(run_dir), str(decision), "--config", str(override),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(saved["tasks"]["t2"]["status"], "completed")
        self.assertEqual(saved["tasks"]["t2"]["config_fingerprint"]["worker"]["model"],
                         "deepseek-flash")

    def test_external_mode_round_trip(self):
        run_dir = self.dir / "run"
        fake = self.write("fake.json", {"planner": [], "worker": [self.worker],
                                        "reviewer": [self.review], "critic": []})
        r = run("start", str(run_dir), "--goal", str(self.dir / "goal.json"),
                "--config", str(self.dir / "config.json"),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        decision = self.write("decision.json", self.dispatch)
        r = run("decide", str(run_dir), str(decision),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["tasks"]["t1"]["status"], "completed")
        packet = json.loads((run_dir / state["pending_packet"]).read_text())
        ev = next(e["id"] for e in packet["new_evidence"] if e["completion_usable"])
        complete = self.write("complete.json", {
            "action": "complete", "reason": "done", "evidence": [ev],
            "completion": [{"criterion": "g1", "met": True, "evidence": [ev]}],
        })
        r = run("decide", str(run_dir), str(complete),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["budget"]["external_unmetered"], 2)

    def test_auto_mode_round_trip(self):
        run_dir = self.dir / "auto"
        ev = "ev-0001"
        planner = [json.dumps(self.dispatch), json.dumps({
            "action": "complete", "reason": "done", "evidence": [ev],
            "completion": [{"criterion": "g1", "met": True, "evidence": [ev]}],
        })]
        fake = self.write("fake-auto.json", {"planner": planner, "worker": [self.worker],
                                             "reviewer": [self.review], "critic": []})
        r = run("start", str(run_dir), "--goal", str(self.dir / "goal.json"),
                "--config", str(self.dir / "config.json"), "--mode", "auto",
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("auto", str(run_dir),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["status"], "complete")

    def test_invalid_decision_exits_2_and_keeps_state(self):
        run_dir = self.dir / "bad"
        fake = self.write("fake-bad.json", {"planner": [], "worker": [self.worker],
                                            "reviewer": [self.review], "critic": []})
        r = run("start", str(run_dir), "--goal", str(self.dir / "goal.json"),
                "--config", str(self.dir / "config.json"),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        bad = self.write("bad.json", {"action": "dispatch", "reason": "x", "tasks": []})
        r = run("decide", str(run_dir), str(bad), "--config", str(self.dir / "config.json"),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 2)
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["status"], "awaiting_decision")
        self.assertEqual(state["tasks"], {})


    def test_cancel_sets_cancelled_state(self):
        run_dir = self.dir / "cancel"
        fake = self.write("fake-cancel.json", {"planner": [], "worker": [self.worker],
                                               "reviewer": [self.review], "critic": []})
        r = run("start", str(run_dir), "--goal", str(self.dir / "goal.json"),
                "--config", str(self.dir / "config.json"),
                "--allow-fake-backend", "--fake-script", str(fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("cancel", str(run_dir), "--config", str(self.dir / "config.json"),
                "--allow-fake-backend")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
