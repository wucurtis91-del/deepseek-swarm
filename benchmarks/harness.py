#!/usr/bin/env python3
"""Reproducible benchmark harness for deepseek-swarm.

Default mode is offline and uses scripted fake backends. That proves the control
logic and the metric plumbing, not that a real model is smarter or cheaper.
Real-model runs require --real, a role config, an explicit spend acknowledgement
and a budget.

    python3 benchmarks/harness.py --mode fake
    python3 benchmarks/harness.py --mode fake --include-claude-alone
    python3 benchmarks/harness.py --mode real --config roles.json \
        --i-understand-this-spends --max-usd 3 --max-calls 60

Cases:
  reading     long material -> extract facts, independently checked
  code        fix a bug in a git repo, fixed tests + scope + negative test
  diagnostic  first result changes which hypothesis is tested next
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import swarm  # noqa: E402
from swarmlib import backends, budget as budget_mod, config, engine, protocol, state  # noqa: E402
from swarmlib import util  # noqa: E402

FAKE_ROLES = {
    "planner": {"backend": "fake", "provider": "anthropic", "model": "claude-opus-5-5",
                "auth": "local_login", "tools": "read"},
    "worker": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
               "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"},
    "reviewer": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
                 "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "tools": "read"},
    "critic": {"backend": "fake", "provider": "deepseek", "model": "deepseek-flash",
               "auth": "api_key", "api_key_env": "DEEPSEEK_API_KEY", "enabled": False},
}

SELF = '\n```selfcheck\n{"met": [], "not_met": [], "uncertain": []}\n```'
REVIEW_OK = "```json\n" + json.dumps({"criteria": [{"id": "c1", "met": True, "note": "ok"}],
                                       "errors": [], "fix_instructions": ""}) + "\n```"


def fake_usage() -> dict:
    return {"input_tokens": 800, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 200, "total_tokens": 1000}


def review_for(criteria: list[str]) -> str:
    return "```json\n" + json.dumps({
        "criteria": [{"id": c, "met": True, "note": "ok"} for c in criteria],
        "errors": [], "fix_instructions": "",
    }) + "\n```"


# ---------------------------------------------------------------- real budget

class RealBudget:
    """One budget and one usage ledger for the whole real benchmark run."""

    def __init__(self, max_usd: float | None, max_calls: int | None):
        self.max_usd = max_usd
        self.max_calls = max_calls
        self.calls = 0
        self.cost_usd = 0.0
        self.cost_unknown_calls = 0
        self.external_unmetered = 0
        self.overshoot = []

    def remaining(self) -> dict:
        return {
            "calls": None if self.max_calls is None else max(0, self.max_calls - self.calls),
            "usd": None if self.max_usd is None else max(0.0, self.max_usd - self.cost_usd),
        }

    def can_start(self, est_calls: int = 1, est_usd: float = 0.0) -> tuple[bool, str]:
        if self.max_calls is not None and self.calls + est_calls > self.max_calls:
            return False, f"global max_calls ({self.max_calls})"
        if self.max_usd is not None and self.cost_usd + est_usd > self.max_usd:
            return False, f"global max_usd ({self.max_usd})"
        return True, ""

    def note(self, metrics: dict) -> None:
        self.calls += int(metrics.get("calls") or 0)
        self.cost_usd += float(metrics.get("cost_usd_known") or 0)
        self.cost_unknown_calls += int(metrics.get("cost_unknown_calls") or 0)
        self.external_unmetered += int(metrics.get("external_planner_unmetered") or 0)
        if self.max_calls is not None and self.calls > self.max_calls:
            self.overshoot.append(f"calls {self.calls}>{self.max_calls}")
        if self.max_usd is not None and self.cost_usd > self.max_usd:
            self.overshoot.append(f"usd {self.cost_usd:.4f}>{self.max_usd}")

    def snapshot(self) -> dict:
        return {
            "max_usd": self.max_usd, "max_calls": self.max_calls,
            "calls": self.calls, "cost_usd_known": round(self.cost_usd, 6),
            "cost_unknown_calls": self.cost_unknown_calls,
            "external_unmetered": self.external_unmetered,
            "remaining": self.remaining(),
            "overshoot": self.overshoot,
            "note": ("The loop/Claude-alone paths get the remaining global budget. "
                     "The legacy path is bounded by an estimated upper bound and is skipped "
                     "if that bound exceeds the remaining call budget; its actual usage is "
                     "recorded afterwards and any overshoot is reported."),
        }


# ---------------------------------------------------------------- case setup

READING_FACTS = {
    "alpha": {"deadline": "2026-11-05", "budget_kusd": "42", "owner": "Lin"},
    "beta": {"risk_date": "2026-10-30", "mitigation_date": "2026-10-25"},
}


def setup_reading(root: Path) -> None:
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "alpha.txt").write_text(
        "Project NEBULA\nDeadline: 2026-11-05\nBudget: 42k USD\nOwner: Lin\n")
    (docs / "beta.txt").write_text(
        "Risk review\nVendor contract expires 2026-10-30.\n"
        "Mitigation: renew the contract by 2026-10-25.\n")


def write_reading_grader(path: Path) -> Path:
    """The reference answers live here, outside the worker-visible case directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import sys\n"
        "FACTS={'alpha':{'deadline':'2026-11-05','budget_kusd':'42','owner':'Lin'},"
        "'beta':{'risk_date':'2026-10-30','mitigation_date':'2026-10-25'}}\n"
        "text=open(sys.argv[1],encoding='utf-8').read()\n"
        "missing=[k for k,v in FACTS[sys.argv[2]].items() if f'{k}={v}' not in text]\n"
        "print('missing:', missing)\n"
        "sys.exit(1 if missing else 0)\n")
    return path


def reading_goal() -> dict:
    # Criteria describe what must be established, not the answer values.
    return config.normalize_goal({
        "goal": ("Read docs/alpha.txt and docs/beta.txt and extract the project deadline, "
                 "budget, owner, vendor risk date and mitigation date from the source lines."),
        "completion_criteria": [
            {"id": "g1", "text": "alpha deadline is extracted exactly from the source"},
            {"id": "g2", "text": "alpha budget is extracted exactly from the source"},
            {"id": "g3", "text": "alpha owner is extracted exactly from the source"},
            {"id": "g4", "text": "beta risk date is extracted exactly from the source"},
            {"id": "g5", "text": "beta mitigation date is extracted exactly from the source"},
        ],
    })


def diagnostic_goal() -> dict:
    return config.normalize_goal({
        "goal": ("Read data/metrics.txt and decide from the measured values whether errors "
                 "or latency is the supported cause. Output the supported cause as cause=<value>."),
        "completion_criteria": ["the cause is supported by the measured data"],
    })


def reading_answer(doc: str) -> str:
    lines = [f"{k}={v}" for k, v in READING_FACTS[doc].items()]
    return "\n".join(lines) + SELF


def setup_code(root: Path) -> dict:
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\nassert add(2, 3) == 5, add(2, 3)\nprint('ok')\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                    "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=x",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return {"repo": repo,
            "test_sha256": hashlib.sha256((repo / "test_calc.py").read_bytes()).hexdigest()}


def fix_code(cwd: Path) -> str:
    (Path(cwd) / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return "fixed calc.py" + SELF


def setup_diagnostic(root: Path) -> None:
    data = root / "data"
    data.mkdir(parents=True)
    (data / "metrics.txt").write_text("error_rate=0.12\nlatency_ms=420\n")


def diag_extract_answer() -> str:
    return "error_rate=0.12\nlatency_ms=420" + SELF


# ---------------------------------------------------------------- verifiers

def verify_reading(root: Path, outputs: dict[str, str]) -> list[dict]:
    checks = []
    for doc, facts in READING_FACTS.items():
        text = outputs.get(doc, "")
        missing = [k for k, v in facts.items() if f"{k}={v}" not in text]
        checks.append({"name": f"facts:{doc}", "ok": not missing,
                       "detail": "missing " + ",".join(missing) if missing else "all facts"})
    return checks


def verify_code(root: Path, meta: dict, patch: Path | None = None) -> list[dict]:
    verify = root / "verify"
    if verify.exists():
        shutil.rmtree(verify)
    shutil.copytree(meta["repo"], verify)
    checks = []
    if patch is not None:
        res = subprocess.run(["git", "apply", str(patch)], cwd=verify,
                             capture_output=True, text=True)
        checks.append({"name": "patch_applies", "ok": res.returncode == 0,
                       "detail": (res.stderr or res.stdout)[-300:]})
    res = subprocess.run([sys.executable, "test_calc.py"], cwd=verify,
                         capture_output=True, text=True)
    checks.append({"name": "fixed_tests", "ok": res.returncode == 0,
                   "detail": (res.stdout + res.stderr)[-300:]})
    test_sha = hashlib.sha256((verify / "test_calc.py").read_bytes()).hexdigest()
    checks.append({"name": "test_file_unchanged", "ok": test_sha == meta["test_sha256"],
                   "detail": ""})
    return checks


def verify_diagnostic(root: Path, outputs: dict[str, str]) -> list[dict]:
    final = outputs.get("final", "")
    return [{"name": "cause=errors", "ok": "cause=errors" in final,
             "detail": final[-200:]}]


# ---------------------------------------------------------------- fake runner

class ScriptedRunner:
    """Stand-in for swarm.Runner, used only for the legacy one-shot baseline."""

    def __init__(self, worker, usage=None):
        self.args = types.SimpleNamespace(
            model="deepseek-flash", review_model=None, review=True, tools="read",
            max_chars=None, retries=0, max_rework=1, cwd=None, max_turns=5,
            timeout=60, keep_sessions=False, max_tasks=50)
        self.worker = worker
        self.usage = usage or fake_usage()

    def call(self, prompt, task, tools, model):
        if prompt.startswith(swarm.REVIEW_RULES):
            return {"ok": True, "text": REVIEW_OK, "usage": dict(self.usage), "seconds": 0.01}
        if prompt.startswith(swarm.SHRINK_RULES):
            return {"ok": True, "text": prompt.split("## 要压缩的结果\n")[-1],
                    "usage": dict(self.usage), "seconds": 0.01}
        text = self.worker(task, prompt)
        return {"ok": True, "text": text, "usage": dict(self.usage), "seconds": 0.01}


def legacy_metrics(records: dict, wall: float) -> dict:
    recs = list(records.values())
    known = sum(r.get("cost_usd", 0) for r in recs)
    unknown = sum(int(r.get("cost_unknown_calls", 0)) for r in recs)
    return {
        "calls": sum(int(r.get("calls", 0)) for r in recs),
        "tokens": {k: sum(int((r.get("tokens") or {}).get(k, 0)) for r in recs)
                   for k in ("input", "cache_read", "output")},
        "cost_usd_known": round(known, 6),
        "cost_coverage": "full" if not unknown else ("partial" if known else "none"),
        "cost_unknown_calls": unknown,
        "wall_seconds": round(wall, 2),
        "worker_rounds": sum(int(r.get("rounds", 0)) for r in recs),
        "rework": sum(max(0, int(r.get("rounds", 0)) - 1) for r in recs),
        "shrinks": sum(int(r.get("shrinks", 0)) for r in recs),
        "statuses": {r["id"]: r.get("status") for r in recs},
    }


def loop_metrics(state_obj: dict, wall: float) -> dict:
    b = state_obj.get("budget") or {}
    roles = b.get("roles") or {}
    return {
        "calls": b.get("calls", 0),
        "calls_by_role": {r: (roles.get(r) or {}).get("calls", 0)
                          for r in ("planner", "worker", "reviewer", "critic", "compressor")},
        "external_planner_unmetered": b.get("external_unmetered", 0),
        "tokens": {r: {
            "input": (roles.get(r) or {}).get("input_tokens", 0),
            "cache_read": (roles.get(r) or {}).get("cache_read_input_tokens", 0),
            "output": (roles.get(r) or {}).get("output_tokens", 0),
        } for r in ("planner", "worker", "reviewer", "critic", "compressor")},
        "cost_usd_known": b.get("cost_usd_known", 0),
        "cost_coverage": b.get("cost_coverage"),
        "cost_unknown_calls": b.get("cost_unknown_calls", 0),
        "wall_seconds": round(wall, 2),
        "workflow_rounds": state_obj.get("round", 0),
        "worker_rounds": sum(int(t.get("attempts", 0))
                             for t in (state_obj.get("tasks") or {}).values()),
        "rework": sum(int(t.get("repairs", 0))
                      for t in (state_obj.get("tasks") or {}).values()),
        "statuses": {tid: t.get("status")
                     for tid, t in (state_obj.get("tasks") or {}).items()},
    }


def solo_metrics(result: dict, model: str, wall: float) -> dict:
    usage = result.get("usage") or {}
    cost = config.estimate_cost(usage, model, util.now())
    return {
        "calls": 1,
        "tokens": {"input": usage.get("input_tokens") or 0,
                   "cache_read": usage.get("cache_read_input_tokens") or 0,
                   "output": usage.get("output_tokens") or 0},
        "cost_usd_known": cost.get("usd") or 0,
        "cost_coverage": "full" if cost.get("known") else "none",
        "cost_unknown_calls": 0 if cost.get("known") else 1,
        "wall_seconds": round(wall, 2),
        "rework": 0,
    }


# ---------------------------------------------------------------- fake approaches

def run_legacy(root: Path, tasks_spec: dict, worker) -> dict:
    out = root / "legacy-results"
    out.mkdir(parents=True, exist_ok=True)
    path = root / "legacy-tasks.json"
    path.write_text(json.dumps(tasks_spec, ensure_ascii=False, indent=2))
    tasks, shared = swarm.load_tasks(path)
    runner = ScriptedRunner(worker)
    start = time.time()
    records = swarm.run_all(tasks, shared, runner, out, 2, {})
    wall = time.time() - start
    return {"out": out, "records": records, "metrics": legacy_metrics(records, wall)}


def run_legacy_reading(root: Path) -> dict:
    setup_reading(root)
    grader = write_reading_grader(root.parent / "graders" / "verify_reading.py")
    spec = {"tasks": [
        {"id": "doc-alpha", "prompt": "Read docs/alpha.txt and output deadline, budget_kusd, owner.",
         "cwd": str(root), "tools": "read", "max_chars": 300, "review": False,
         "check_cmd": f"{sys.executable} {grader} {{result}} alpha"},
        {"id": "doc-beta", "prompt": "Read docs/beta.txt and output risk_date, mitigation_date.",
         "cwd": str(root), "tools": "read", "max_chars": 300, "review": False,
         "check_cmd": f"{sys.executable} {grader} {{result}} beta"},
    ]}
    return run_legacy(root, spec, lambda task, prompt: reading_answer(
        "alpha" if "alpha" in task["id"] else "beta"))


def run_legacy_code(root: Path) -> dict:
    meta = setup_code(root)
    spec = {"tasks": [{
        "id": "fix", "prompt": "Fix calc.py so the existing test passes.",
        "cwd": str(meta["repo"]), "tools": "edit", "max_chars": 200, "review": False,
        "check_cmd": f"{sys.executable} test_calc.py",
    }]}
    result = run_legacy(root, spec, lambda task, prompt: fix_code(Path(task["cwd"])))
    result["meta"] = meta
    return result


def run_legacy_diagnostic(root: Path) -> dict:
    setup_diagnostic(root)
    spec = {"tasks": [
        {"id": "extract", "prompt": "Read data/metrics.txt and output error_rate and latency_ms.",
         "cwd": str(root), "tools": "read", "review": False},
        {"id": "diag-errors", "prompt": "Given error_rate, decide whether errors are the cause.",
         "cwd": str(root), "tools": "none", "review": False,
         "depends_on": ["extract"]},
        {"id": "diag-latency", "prompt": "Given latency_ms, decide whether latency is the cause.",
         "cwd": str(root), "tools": "none", "review": False,
         "depends_on": ["extract"]},
        {"id": "final", "prompt": "Choose the supported cause and output cause=errors or cause=latency.",
         "cwd": str(root), "tools": "none", "review": False,
         "depends_on": ["diag-errors", "diag-latency"]},
    ]}

    def worker(task, prompt):
        tid = task["id"]
        if tid == "extract":
            return diag_extract_answer()
        if tid == "diag-errors":
            return "cause=errors" + SELF
        if tid == "diag-latency":
            return "cause=latency" + SELF
        return "cause=errors" + SELF

    return run_legacy(root, spec, worker)


def _packet_from_prompt(prompt: str) -> dict:
    marker = "## 决策材料\n"
    idx = prompt.find(marker)
    if idx < 0:
        return {}
    text = prompt[idx + len(marker):]
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _completion_evidence(packet: dict, task_id: str) -> str | None:
    for ev in packet.get("new_evidence") or []:
        if ev.get("task") == task_id and ev.get("completion_usable"):
            return ev.get("id")
    return None


def run_loop(root: Path, *, goal_obj: dict, planner, worker, reviewer=None,
             workspace=None, cfg=None, mode="fake", real_cfg=None, max_steps=10) -> dict:
    store = state.RunStore(root / "loop-run")
    if mode == "fake":
        use_cfg = cfg or config.load_config(None)
        use_cfg["roles"] = FAKE_ROLES
        roles = config.validate_roles(use_cfg["roles"], allow_fake=True)
        reviewer_value = reviewer or review_for(["c1"])
        if isinstance(reviewer_value, str):
            reviewer_value = (lambda v=reviewer_value, **kw: v)
        fakes = {"planner": planner, "worker": worker,
                 "reviewer": reviewer_value, "critic": []}
        backend_map = backends.build_backends(roles, allow_fake=True, fake_responses=fakes)
        eng = engine.Engine(store, use_cfg, roles, backend_map, mode="auto")
    else:
        use_cfg = real_cfg
        roles = config.validate_roles(use_cfg["roles"], allow_fake=False)
        backend_map = backends.build_backends(roles, allow_fake=False)
        eng = engine.Engine(store, use_cfg, roles, backend_map, mode="auto")
    start = time.time()
    eng.start(goal_obj, workspace or {"repo": None})
    with store.lock():
        eng.state = store.load()
        summary = eng.run_auto(max_steps=max_steps)
    wall = time.time() - start
    return {"run_dir": store.dir, "state": eng.state, "summary": summary,
            "metrics": loop_metrics(eng.state, wall)}


def run_loop_reading(root: Path) -> dict:
    setup_reading(root)
    goal_obj = reading_goal()
    grader = write_reading_grader(root.parent / "graders" / "verify_reading.py")
    phase = {"n": 0}

    def planner(**kw):
        packet = _packet_from_prompt(kw["prompt"])
        phase["n"] += 1
        if phase["n"] == 1:
            return json.dumps({"action": "dispatch", "reason": "extract both docs",
                               "tasks": [
                {"id": "doc-alpha", "kind": "analysis", "prompt": "Read docs/alpha.txt and output facts.",
                 "question": "alpha facts", "produces": "fact lines", "decision_impact": "completion",
                 "tools": "read", "acceptance": [{"id": "c1", "text": "facts", "check": "unit"}],
                 "acceptance_policy": "deterministic",
                 "checks": [{"name": "unit", "argv": [
                     sys.executable, str(grader), "{result}", "alpha"]}],
                 "satisfies": ["g1", "g2", "g3"]},
                {"id": "doc-beta", "kind": "analysis", "prompt": "Read docs/beta.txt and output facts.",
                 "question": "beta facts", "produces": "fact lines", "decision_impact": "completion",
                 "tools": "read", "acceptance": [{"id": "c1", "text": "facts", "check": "unit"}],
                 "acceptance_policy": "deterministic",
                 "checks": [{"name": "unit", "argv": [
                     sys.executable, str(grader), "{result}", "beta"]}],
                 "satisfies": ["g4", "g5"]},
            ]})
        a = _completion_evidence(packet, "doc-alpha")
        b = _completion_evidence(packet, "doc-beta")
        if a and b:
            return json.dumps({"action": "complete", "reason": "facts verified", "evidence": [a, b],
                               "completion": [
                {"criterion": "g1", "met": True, "evidence": [a]},
                {"criterion": "g2", "met": True, "evidence": [a]},
                {"criterion": "g3", "met": True, "evidence": [a]},
                {"criterion": "g4", "met": True, "evidence": [b]},
                {"criterion": "g5", "met": True, "evidence": [b]},
            ]})
        return json.dumps({"action": "pause", "reason": "waiting"})

    def worker(**kw):
        p = kw["prompt"]
        return reading_answer("alpha" if "alpha.txt" in p else "beta")

    return run_loop(root, goal_obj=goal_obj, planner=planner, worker=worker,
                    workspace={"repo": str(root)})


def run_loop_code(root: Path) -> dict:
    meta = setup_code(root)
    goal_obj = config.normalize_goal({"goal": "Fix calc.py",
                                      "completion_criteria": ["tests pass"]})
    phase = {"n": 0}

    def planner(**kw):
        packet = _packet_from_prompt(kw["prompt"])
        phase["n"] += 1
        if phase["n"] == 1:
            return json.dumps({"action": "dispatch", "reason": "fix the bug", "tasks": [{
                "id": "fix", "kind": "code", "prompt": "Fix calc.py so test_calc.py passes.",
                "question": "why is add wrong", "produces": "patch", "decision_impact": "completion",
                "tools": "edit", "allowed_paths": ["calc.py"],
                "acceptance": [{"id": "c1", "text": "tests pass", "check": "unit"}],
                "acceptance_policy": "deterministic",
                "checks": [{"name": "unit", "argv": [sys.executable, "test_calc.py"]}],
                "negative_check": {"path": "calc.py", "find": "return a + b",
                                   "replace": "return a - b", "check": "unit",
                                   "note": "known regression"},
                "satisfies": ["g1"]}]})
        ev = _completion_evidence(packet, "fix")
        if ev:
            return json.dumps({"action": "complete", "reason": "tests and patch verified",
                               "evidence": [ev],
                               "completion": [{"criterion": "g1", "met": True, "evidence": [ev]}]})
        return json.dumps({"action": "pause", "reason": "waiting"})

    def worker(**kw):
        return fix_code(Path(kw["cwd"]))

    result = run_loop(root, goal_obj=goal_obj, planner=planner, worker=worker,
                      workspace={"repo": str(meta["repo"])})
    result["meta"] = meta
    return result


def run_loop_diagnostic(root: Path) -> dict:
    setup_diagnostic(root)
    goal_obj = diagnostic_goal()
    phase = {"n": 0}

    def planner(**kw):
        packet = _packet_from_prompt(kw["prompt"])
        phase["n"] += 1
        if phase["n"] == 1:
            return json.dumps({"action": "dispatch", "reason": "measure first", "tasks": [{
                "id": "extract", "kind": "analysis", "prompt": "Read data/metrics.txt and output metrics.",
                "question": "what are the metrics", "produces": "metric lines",
                "decision_impact": "choose one hypothesis", "tools": "read",
                "acceptance": [{"id": "c1", "text": "metrics", "check": "unit"}],
                "acceptance_policy": "deterministic",
                "checks": [{"name": "unit", "argv": [
                    "grep", "-q", "error_rate=0.12", "{result}"]}],
                "satisfies": []}]})
        excerpt = " ".join(a.get("excerpt", "") for a in packet.get("key_artifacts") or [])
        if "error_rate=0.12" in excerpt and phase["n"] == 2:
            return json.dumps({"action": "dispatch", "reason": "errors exceed threshold", "tasks": [{
                "id": "diag-errors", "kind": "analysis", "prompt": "Test whether errors explain the observed metrics; output the supported cause line.",
                "question": "is the cause errors", "produces": "cause line",
                "decision_impact": "completion", "tools": "none",
                "depends_on": ["extract"],
                "acceptance": [{"id": "c1", "text": "cause"}],
                "acceptance_policy": "semantic", "satisfies": ["g1"]}]})
        ev = _completion_evidence(packet, "diag-errors")
        if ev:
            return json.dumps({"action": "complete", "reason": "diagnosis verified",
                               "evidence": [ev],
                               "completion": [{"criterion": "g1", "met": True, "evidence": [ev]}]})
        return json.dumps({"action": "pause", "reason": "waiting"})

    def worker(**kw):
        p = kw["prompt"]
        if "metrics.txt" in p:
            return diag_extract_answer()
        return "cause=errors" + SELF

    return run_loop(root, goal_obj=goal_obj, planner=planner, worker=worker,
                    reviewer=review_for(["c1"]), workspace={"repo": str(root)})


def run_solo_reading(root: Path) -> dict:
    setup_reading(root)
    role = config.validate_roles(FAKE_ROLES, allow_fake=True)["planner"]
    backend = backends.FakeBackend(role, responses=[
        "\n".join([f"{k}={v}" for k, v in dict(READING_FACTS["alpha"], **READING_FACTS["beta"]).items()])])
    start = time.time()
    res = backend.call("Read docs/alpha.txt and docs/beta.txt and extract all facts.",
                       cwd=root, tools="read", purpose="solo")
    wall = time.time() - start
    outputs = {doc: res.get("text", "") for doc in READING_FACTS}
    return {"text": res.get("text", ""), "metrics": solo_metrics(res, role.model, wall)}


def run_solo_code(root: Path) -> dict:
    meta = setup_code(root)
    role = config.validate_roles(FAKE_ROLES, allow_fake=True)["planner"]
    def responder(**kw):
        return fix_code(Path(kw["cwd"]))
    backend = backends.FakeBackend(role, responses=responder)
    start = time.time()
    res = backend.call("Fix calc.py.", cwd=meta["repo"], tools="edit", purpose="solo")
    wall = time.time() - start
    return {"text": res.get("text", ""), "meta": meta,
            "metrics": solo_metrics(res, role.model, wall)}


def run_solo_diagnostic(root: Path) -> dict:
    setup_diagnostic(root)
    role = config.validate_roles(FAKE_ROLES, allow_fake=True)["planner"]
    backend = backends.FakeBackend(role, responses=["cause=errors"])
    start = time.time()
    res = backend.call("Read data/metrics.txt and diagnose the cause.", cwd=root,
                       tools="read", purpose="solo")
    wall = time.time() - start
    return {"text": res.get("text", ""), "metrics": solo_metrics(res, role.model, wall)}


# ---------------------------------------------------------------- real mode

def _run_legacy_real(root: Path, spec: dict) -> dict:
    path = root / "legacy-tasks.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2))
    out = root / "legacy-results"
    cmd = [sys.executable, str(ROOT / "scripts/swarm.py"), "run", str(path),
           "--out", str(out), "--concurrency", "2", "--max-rework", "1"]
    start = time.time()
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    wall = time.time() - start
    summary = {}
    try:
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    records = {t["id"]: t for t in summary.get("tasks", [])}
    return {"out": out, "records": records, "metrics": legacy_metrics(records, wall),
            "exit_code": proc.returncode,
            "stderr_tail": (proc.stderr or "")[-1000:]}


def run_legacy_reading_real(root: Path) -> dict:
    setup_reading(root)
    return _run_legacy_real(root, {"tasks": [
        {"id": "doc-alpha", "prompt": "Read docs/alpha.txt and output exactly deadline=..., budget_kusd=..., owner=...",
         "cwd": str(root), "tools": "read", "max_chars": 300, "review": False},
        {"id": "doc-beta", "prompt": "Read docs/beta.txt and output exactly risk_date=..., mitigation_date=...",
         "cwd": str(root), "tools": "read", "max_chars": 300, "review": False},
    ]})


def run_legacy_code_real(root: Path) -> dict:
    meta = setup_code(root)
    result = _run_legacy_real(root, {"tasks": [{
        "id": "fix", "prompt": "Fix calc.py so the existing test passes.",
        "cwd": str(meta["repo"]), "tools": "edit", "max_chars": 200, "review": False,
        "check_cmd": f"{sys.executable} test_calc.py"}]})
    result["meta"] = meta
    return result


def run_legacy_diagnostic_real(root: Path) -> dict:
    setup_diagnostic(root)
    return _run_legacy_real(root, {"tasks": [
        {"id": "extract", "prompt": "Read data/metrics.txt and output error_rate and latency_ms.",
         "cwd": str(root), "tools": "read", "review": False},
        {"id": "diag-errors", "prompt": "Test whether errors explain the observed metrics; output the supported cause line.",
         "cwd": str(root), "tools": "none", "review": False, "depends_on": ["extract"]},
        {"id": "diag-latency", "prompt": "Test whether latency explains the observed metrics; output the supported cause line.",
         "cwd": str(root), "tools": "none", "review": False, "depends_on": ["extract"]},
        {"id": "final", "prompt": "Using the two diagnoses, output the supported cause line.",
         "cwd": str(root), "tools": "none", "review": False,
         "depends_on": ["diag-errors", "diag-latency"]},
    ]})


def run_solo_real(root: Path, case: str, real_cfg: dict) -> dict:
    roles = config.validate_roles(real_cfg["roles"], allow_fake=False)
    role = roles["planner"]
    backend = backends.ClaudeBackend(role)
    if case == "reading":
        setup_reading(root)
        prompt = "Read docs/alpha.txt and docs/beta.txt. Output lines deadline=..., budget_kusd=..., owner=..., risk_date=..., mitigation_date=..."
        cwd, tools = root, "read"
    elif case == "code":
        meta = setup_code(root)
        prompt = "Fix calc.py so test_calc.py passes. Only edit calc.py."
        cwd, tools = meta["repo"], "edit"
    else:
        setup_diagnostic(root)
        prompt = "Read data/metrics.txt and decide the cause; output cause=errors or cause=latency."
        cwd, tools = root, "read"
    start = time.time()
    res = backend.call(prompt, cwd=cwd, tools=tools, max_turns=10, purpose="solo")
    wall = time.time() - start
    out = {"text": res.get("text", ""), "error": res.get("error"),
           "metrics": solo_metrics(res, role.model, wall)}
    if case == "code":
        out["meta"] = meta
    return out


# ---------------------------------------------------------------- orchestration

CASES = {
    "reading": {
        "setup": setup_reading,
        "legacy": run_legacy_reading,
        "legacy_real": run_legacy_reading_real,
        "loop": run_loop_reading,
        "solo": run_solo_reading,
        "verify_legacy": lambda root, r: verify_reading(
            root, {d: (r["out"] / f"doc-{d}.md").read_text(encoding="utf-8")
                   if (r["out"] / f"doc-{d}.md").exists() else ""
                   for d in ("alpha", "beta")}),
        "verify_loop": lambda root, r: verify_reading(
            root, {d: (r["run_dir"] / "artifacts" / f"doc-{d}" / "v1.md").read_text(encoding="utf-8")
                   if (r["run_dir"] / "artifacts" / f"doc-{d}" / "v1.md").exists() else ""
                   for d in ("alpha", "beta")}),
        "verify_solo": lambda root, r: verify_reading(
            root, {d: r["text"] for d in ("alpha", "beta")}),
    },
    "code": {
        "setup": setup_code,
        "legacy": run_legacy_code,
        "legacy_real": run_legacy_code_real,
        "loop": run_loop_code,
        "solo": run_solo_code,
        "verify_legacy": lambda root, r: verify_code(root, r["meta"]),
        "verify_loop": lambda root, r: verify_code(
            root, r["meta"],
            (r["run_dir"] / "artifacts" / "fix" / "v1.change.patch")
            if (r["run_dir"] / "artifacts" / "fix" / "v1.change.patch").exists() else None),
        "verify_solo": lambda root, r: verify_code(root, r["meta"]),
    },
    "diagnostic": {
        "setup": setup_diagnostic,
        "legacy": run_legacy_diagnostic,
        "legacy_real": run_legacy_diagnostic_real,
        "loop": run_loop_diagnostic,
        "solo": run_solo_diagnostic,
        "verify_legacy": lambda root, r: verify_diagnostic(
            root, {"final": (r["out"] / "final.md").read_text(encoding="utf-8")
                   if (r["out"] / "final.md").exists() else ""}),
        "verify_loop": lambda root, r: verify_diagnostic(
            root, {"final": (r["run_dir"] / "artifacts" / "diag-errors" / "v1.md").read_text(
                encoding="utf-8") if (r["run_dir"] / "artifacts" / "diag-errors" / "v1.md").exists() else ""}),
        "verify_solo": lambda root, r: verify_diagnostic(root, {"final": r["text"]}),
    },
}


LEGACY_CALL_BOUND = {"reading": 8, "code": 4, "diagnostic": 12}


def _skipped_row(case_name: str, approach: str, mode: str, reason: str) -> dict:
    return {
        "case": case_name, "approach": approach, "mode": mode,
        "skipped": True, "skip_reason": reason,
        "goal_completed": False, "quality_passed": False,
        "independent_checks": [], "metrics": {},
        "raw": {}, "elapsed_seconds": 0.0,
    }


def run_case(case_name: str, mode: str, root: Path, real_cfg=None,
             include_solo=False, run_budget: "RealBudget | None" = None) -> list[dict]:
    spec = CASES[case_name]
    rows = []
    for approach in ("legacy", "loop") + (("claude_alone",) if include_solo else ()):
        if mode == "real" and run_budget is not None:
            if approach == "legacy":
                ok, why = run_budget.can_start(LEGACY_CALL_BOUND[case_name], 0.0)
            else:
                ok, why = run_budget.can_start(1, 0.0)
            rem = run_budget.remaining()
            if rem.get("usd") is not None and rem["usd"] <= 0:
                ok, why = False, "global max_usd exhausted"
            if not ok:
                rows.append(_skipped_row(case_name, approach, mode, why))
                continue
        base = root / f"{case_name}-{approach}"
        base.mkdir(parents=True, exist_ok=True)
        started = time.time()
        verify_key = "verify_solo" if approach == "claude_alone" else f"verify_{approach}"
        if mode == "fake":
            if approach == "legacy":
                run = spec["legacy"](base)
            elif approach == "loop":
                run = spec["loop"](base)
            else:
                run = spec["solo"](base)
            verify = spec[verify_key](base, run)
        else:
            if approach == "legacy":
                run = spec["legacy_real"](base)
            elif approach == "loop":
                setup = spec["setup"](base)
                goal_obj, _planner, _worker = _real_loop_script(case_name, base, setup, real_cfg)
                workspace = ({"repo": str(setup["repo"])} if case_name == "code"
                             else {"repo": str(base)})
                loop_cfg = copy.deepcopy(real_cfg)
                remaining = run_budget.remaining() if run_budget else {"calls": None, "usd": None}
                if remaining["calls"] is not None:
                    loop_cfg["budget"]["max_calls"] = remaining["calls"]
                if remaining["usd"] is not None:
                    loop_cfg["budget"]["max_usd"] = remaining["usd"]
                run = run_loop(base, goal_obj=goal_obj, planner=None, worker=None,
                               workspace=workspace, mode="real", real_cfg=loop_cfg,
                               max_steps=12)
            else:
                run = run_solo_real(base, case_name, real_cfg)
            verify = spec[verify_key](base, run)
        if mode == "real" and run_budget is not None:
            run_budget.note(run["metrics"])
        checks_ok = all(c["ok"] for c in verify)
        status = (run.get("summary") or {}).get("status") if approach == "loop" else None
        goal_completed = checks_ok and (
            status == "complete" if approach == "loop" else True)
        rows.append({
            "case": case_name, "approach": approach, "mode": mode,
            "goal_completed": bool(goal_completed),
            "quality_passed": bool(checks_ok),
            "independent_checks": verify,
            "metrics": run["metrics"],
            "raw": {
                "summary": {k: v for k, v in (run.get("summary") or {}).items()
                            if k != "workspace"},
                "statuses": (run.get("state") or {}).get("tasks", {}).keys()
                if approach == "loop" else run.get("records", {}).keys(),
                "exit_code": run.get("exit_code"),
                "error": run.get("error"),
            },
            "elapsed_seconds": round(time.time() - started, 2),
        })
    return rows


def _real_loop_script(case_name: str, root: Path, setup, real_cfg):
    """For real mode the planner is a model; the case supplies only the goal.

    A real planner must create tasks. The harness leaves the prompt/goal to the
    model, but keeps the grading independent.
    """
    if case_name == "reading":
        return reading_goal(), None, None
    if case_name == "code":
        goal_obj = config.normalize_goal({
            "goal": "Fix calc.py so the fixed test test_calc.py passes. Only modify calc.py.",
            "completion_criteria": ["tests pass"]})
        return goal_obj, None, None
    goal_obj = diagnostic_goal()
    return goal_obj, None, None


def write_report(rows: list[dict], out_dir: Path, meta: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    payload = {"harness_version": "1", "generated_at": util.iso(), **meta, "results": rows}
    util.atomic_write_json(json_path, payload)
    lines = [
        f"# deepseek-swarm benchmark ({meta.get('mode')})",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Commit: `{meta.get('git_commit')}`",
        f"- Python: {meta.get('python')}",
        f"- Mode: **{meta.get('mode')}**" + (
            " — scripted fake backends; this proves control flow and metric plumbing, "
            "not real-model quality or savings." if meta.get("mode") == "fake" else
            " — real model calls with an explicit budget."),
        "",
        "| Case | Approach | Quality | Goal complete | Calls | Planner | Worker | Reviewer | "
        "Cost (known) | Coverage | Wall s | Rework |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        m = r["metrics"] or {}
        roles = m.get("calls_by_role") or {}
        quality = ("SKIPPED: " + r.get("skip_reason", "")) if r.get("skipped") else (
            "pass" if r["quality_passed"] else "FAIL")
        lines.append(
            f"| {r['case']} | {r['approach']} | {quality} | "
            f"{'yes' if r['goal_completed'] else 'no'} | {m.get('calls', '-')} | "
            f"{roles.get('planner', '-')} | {roles.get('worker', '-')} | "
            f"{roles.get('reviewer', '-')} | ${m.get('cost_usd_known', '-')} | "
            f"{m.get('cost_coverage', '-')} | {m.get('wall_seconds', '-')} | {m.get('rework', '-')} |")
    lines += [
        "",
        "## Reading the table",
        "",
        "- Quality is decided by independent checks run by this harness, never by a worker's own success claim.",
        "- Fake mode uses scripted responses. It cannot show that a real model is smarter or cheaper.",
        "- Costs are estimates from the configured price table; unknown prices are shown as unknown, not zero.",
        "- A dynamic loop can use more planner calls on a simple task; do not assume savings. "
        "Use real mode with an explicit budget to measure.",
        "",
    ]
    util.atomic_write_text(md_path, "\n".join(lines))
    return json_path, md_path


def git_commit() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return commit + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="Reproducible deepseek-swarm benchmark")
    ap.add_argument("--mode", choices=("fake", "real"), default="fake")
    ap.add_argument("--cases", default="reading,code,diagnostic")
    ap.add_argument("--include-claude-alone", action="store_true",
                    help="also run the optional Claude-alone baseline")
    ap.add_argument("--out", default=str(ROOT / "benchmarks" / "results"))
    ap.add_argument("--config", help="real mode: role/budget config")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="real mode: soft USD budget (required with --real)")
    ap.add_argument("--max-calls", type=int, default=None)
    ap.add_argument("--i-understand-this-spends", action="store_true",
                    help="required for --mode real")
    args = ap.parse_args()

    real_cfg = None
    if args.mode == "real":
        if not args.config or not args.i_understand_this_spends or args.max_usd is None:
            print("real mode requires --config, --max-usd and --i-understand-this-spends. "
                  "No API calls were made.", file=sys.stderr)
            return 2
        real_cfg = config.load_config(args.config)
        real_cfg["budget"] = config.normalize_budget({
            **real_cfg["budget"], "max_usd": args.max_usd,
            **({"max_calls": args.max_calls} if args.max_calls else {}),
        })
        config.validate_roles(real_cfg["roles"], allow_fake=False)

    names = [c.strip() for c in args.cases.split(",") if c.strip()]
    unknown = [c for c in names if c not in CASES]
    if unknown:
        print(f"unknown cases: {unknown}", file=sys.stderr)
        return 2
    rows = []
    run_budget = RealBudget(args.max_usd, args.max_calls) if args.mode == "real" else None
    with tempfile.TemporaryDirectory(prefix="swarm-bench-") as tmp:
        root = Path(tmp)
        for name in names:
            rows.extend(run_case(name, args.mode, root, real_cfg,
                                 include_solo=args.include_claude_alone,
                                 run_budget=run_budget))
    meta = {
        "mode": args.mode,
        "git_commit": git_commit(),
        "python": sys.version.split()[0],
        "config": {
            "roles": real_cfg["roles"] if real_cfg else FAKE_ROLES,
            "budget": real_cfg["budget"] if real_cfg else config.DEFAULT_BUDGET,
            "prices": (real_cfg or {}).get("prices", config.PRICES),
        },
        "randomness": "none in fake mode; real mode uses provider defaults",
        "real_budget": run_budget.snapshot() if run_budget else None,
        "notes": [
            "fake mode validates controller behavior and metrics only",
            "quality checks are independent of the worker's self-report",
            "no savings ratio is assumed",
        ],
    }
    json_path, md_path = write_report(rows, Path(args.out), meta)
    print(f"results: {json_path}")
    print(f"report:  {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
