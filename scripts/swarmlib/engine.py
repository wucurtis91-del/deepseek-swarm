"""The persistent controller: scheduling, decisions, acceptance, recovery, budget."""
from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import os
import re
import inspect
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from . import acceptance, artifacts, budget as budgetmod, config as cfgmod
from . import packet as packetmod, prompts, protocol, research as researchmod
from . import live, security, state as statemod, util

# How a call's purpose is shown in live progress.
LIVE_PHASES = {"call": "执行", "retry": "重试执行", "worker": "执行", "review": "复核", "reviewer": "复核", "compress": "压缩",
               "critic": "评审", "planner": "规划", "planner_retry": "规划"}


def count_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text or ""))


_FENCE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.S)


def split_selfcheck(text: str) -> tuple[str, dict | None]:
    for m in reversed(list(_FENCE.finditer(text or ""))):
        if m.group(1) == "selfcheck":
            try:
                check = json.loads(m.group(2))
            except ValueError:
                check = {"_invalid": m.group(2).strip()}
            return ((text or "")[:m.start()] + (text or "")[m.end():]).strip(), check
    return (text or "").strip(), None


def _signature(result: dict) -> str:
    text = ""
    acc = result.get("acceptance") or {}
    for criterion in acc.get("criteria") or []:
        if not criterion.get("met"):
            text = f"criterion:{criterion.get('id')}:{criterion.get('note','')}"
            break
    if not text:
        for check in acc.get("checks") or []:
            if not check.get("ok"):
                text = f"check:{check.get('name')}:{check.get('exit')}:{(check.get('output') or '')[-200:]}"
                break
    if not text:
        text = str(result.get("error") or (result.get("review") or {}).get("errors") or "")
    text = re.sub(r"\d+", "#", text)
    return text[:240]


class Engine:
    def __init__(self, store: statemod.RunStore, cfg: dict,
                 roles: dict[str, cfgmod.Role], backends: dict,
                 *, mode: str = "external", cancel_event: threading.Event | None = None,
                 log=None):
        self.store = store
        self.cfg = cfg
        self.roles = roles
        self.backends = backends
        self.mode = mode
        self.cancel_event = cancel_event or threading.Event()
        self.log = log or (lambda *a, **k: None)
        self.state: dict | None = None
        self.budget: budgetmod.Budget | None = None
        self.workspace: artifacts.Workspace | None = None
        self._committed_state: dict | None = None
        # Live progress in <run>/live/: written only once a model call starts.
        self.live = live.LiveBoard(
            store.dir, price=self._live_price,
            echo=lambda line: print(line, file=sys.stderr, flush=True),
            echo_every=float((cfg.get("loop") or {}).get("progress_every", 15)))

    def _live_price(self, usage: dict, model: str):
        return cfgmod.estimate_cost(usage, model, util.now(),
                                    self.cfg.get("prices", cfgmod.PRICES)).get("usd")

    def finish_live(self) -> None:
        """Close live progress when a command ends, with each task's current status."""
        tasks = (self.state or {}).get("tasks") or {}
        for tid in list(self.live.tasks):
            self.live.task_done(tid, (tasks.get(tid) or {}).get("status") or "done")
        self.live.finish()

    # ------------------------------------------------------------ setup
    def start(self, goal: dict, workspace_values: dict | None = None) -> dict:
        workspace_values = cfgmod.validate_workspace(workspace_values)
        ws = artifacts.Workspace(
            workspace_values.get("repo"), self.store.dir,
            isolate=workspace_values.get("isolate", "worktree"),
            require_git_for_code=bool(workspace_values.get("require_git_for_code", True)))
        budget_limits = cfgmod.normalize_budget(
            {**self.cfg.get("budget", {}), **(goal.get("budget") or {})})
        self.budget = budgetmod.Budget(budget_limits, self.cfg.get("prices", cfgmod.PRICES))
        state = {
            "schema": 1,
            "run_id": util.new_run_id(),
            "created_at": util.iso(),
            "updated_at": util.iso(),
            "seq": 0,
            "mode": self.mode,
            "status": "awaiting_decision",
            "goal": goal,
            "tasks": {},
            "evidence": {},
            "evidence_seq": 0,
            "facts": [],
            "hypotheses": {},
            "refuted": [],
            "open_questions": [],
            "recommendations": [],
            "round": 0,
            "decisions": [],
            "completion": None,
            "completion_rejection": None,
            "triggers": [{"kind": "initial_planning", "detail": "还没有任务，需要主持者做初始规划"}],
            "packet_seq": 0,
            "packet_watermark": 0,
            "pending_packet": None,
            "context_notes": [],
            "usage_calls": [],
            "budget": self.budget.snapshot(),
            "workspace_config": {
                "repo": str(ws.repo) if ws.repo else None,
                "isolate": workspace_values.get("isolate", "worktree"),
                "require_git_for_code": bool(workspace_values.get("require_git_for_code", True)),
            },
            "workspace": ws.describe(),
            "roles_summary": {name: role.fingerprint() for name, role in self.roles.items()},
            "execution_config": {
                "roles": {name: role.sanitized() for name, role in self.roles.items()},
                "loop": copy.deepcopy(self.cfg.get("loop", {})),
                "prices": copy.deepcopy(self.cfg.get("prices", cfgmod.PRICES)),
                "budget": copy.deepcopy(budget_limits),
            },
            "progress": {"last_decision_fp": None, "last_progress_fp": None,
                         "stalled_rounds": 0},
            "recovery": [],
            "next_acceptance": [],
            "stop_reason": None,
            "question": None,
            "acceptance_history": [],
        }
        research_enabled = bool(goal.get("research")
                                or self.cfg.get("loop", {}).get("research_mode", False))
        researchmod.ensure(state, research_enabled)
        self.state = state
        self.workspace = ws
        with self.store.lock():
            self.store.create(state)
            self.state = self.store.load()
            self._committed_state = copy.deepcopy(self.state)
            self._build_and_store_packet()
        return self.state

    def open(self) -> dict:
        self.state = self.store.load()
        self._committed_state = copy.deepcopy(self.state)
        self._ensure_research_state()
        wc = self.state.get("workspace_config") or {}
        wc = cfgmod.validate_workspace(wc)
        self.workspace = artifacts.Workspace(
            wc.get("repo"), self.store.dir,
            isolate=wc.get("isolate", "worktree"),
            require_git_for_code=bool(wc.get("require_git_for_code", True)))
        snap = self.state.get("budget") or {}
        self.budget = budgetmod.Budget(
            snap.get("limits") or self.cfg.get("budget", {}),
            self.cfg.get("prices", cfgmod.PRICES), snapshot=snap,
            started_at=snap.get("started_at"))
        return self.state

    def _research_enabled(self) -> bool:
        return bool(self.state.get("research_mode")
                    or self.cfg.get("loop", {}).get("research_mode", False)
                    or (self.state.get("goal") or {}).get("research", False))

    def _ensure_research_state(self) -> None:
        if not self._research_enabled():
            self.state["research_mode"] = False
            return
        researchmod.ensure(self.state, True)

    def _migrate_research_state(self) -> None:
        self._ensure_research_state()
        if self.state.get("research_mode"):
            researchmod.migrate(self.state)

    def _refresh_research(self) -> None:
        if self._research_enabled():
            researchmod.refresh(self.state, int(self.state.get("round", 0)))

    def _prepare_research_view(self) -> None:
        if not self._research_enabled():
            return
        self._check_staleness()
        cache = {}

        def validate_task(task_id):
            # Reuse the same artifact/patch/input/dependency-chain validator
            # used by completion, with one cache per refresh pass. The expected
            # status is the task's own: a needs_review/stale status may have been
            # set by the research layer itself, and must not be mistaken for a
            # broken artifact here.
            task = (self.state.get("tasks") or {}).get(task_id) or {}
            return acceptance._task_record_defects(
                self.state, task_id, self.store.dir, cache,
                {"status": task.get("status")})

        researchmod.refresh(self.state, int(self.state.get("round", 0)),
                            validator=validate_task)

    def _invalidate_research_evidence(self, evidence_id: str, reason: str) -> None:
        if self._research_enabled():
            researchmod.invalidate_evidence(
                self.state, evidence_id, reason, int(self.state.get("round", 0)))

    def commit(self, event_type: str, data: dict | None = None, summary: str = "") -> None:
        self.state["budget"] = self.budget.snapshot()
        old = (copy.deepcopy(self._committed_state)
               if self._committed_state is not None else copy.deepcopy(self.state))
        self.state = self.store.commit(old, self.state, event_type, data, summary)
        self._committed_state = copy.deepcopy(self.state)

    def _sync_budget(self) -> None:
        self.state["budget"] = self.budget.snapshot()

    # ------------------------------------------------------------ packet
    def _maybe_compress(self, packet: dict) -> dict:
        ccfg = self.cfg.get("loop", {}).get("packet_compress") or {}
        if not ccfg.get("enabled"):
            return packet
        chars = int((packet.get("packet_meta") or {}).get("chars") or 0)
        if chars < int(ccfg.get("min_chars", 60000)):
            return packet
        role = self.roles.get("compressor")
        if role is None or not role.enabled or "compressor" not in self.backends:
            packet["packet_meta"]["compression"] = "unavailable: compressor role disabled"
            return packet
        prompt = (prompts.COMPRESS_RULES + "\n\n## 决策材料\n"
                  + json.dumps(packet, ensure_ascii=False, default=str))
        usage: list = []
        res = self._call_model("compressor", prompt, cwd=self.store.dir,
                               tools="none", purpose="compress", usage_sink=usage)
        self.state.setdefault("usage_calls", []).extend(usage)
        self._sync_budget()
        if not res.get("ok"):
            packet["packet_meta"]["compression"] = f"failed: {res.get('error')}"[:300]
            return packet
        candidate = None
        for obj in util.json_objects(res.get("text", "")):
            if isinstance(obj, dict) and "goal" in obj and "decision_needed" in obj:
                candidate = obj
                break
        if not candidate:
            packet["packet_meta"]["compression"] = "rejected: no packet JSON"
            return packet
        required = ("goal", "state_summary", "new_evidence", "unresolved",
                    "disagreements", "budget", "decision_needed")
        if "research" in packet:
            required += ("research",)
        if any(k not in candidate for k in required):
            packet["packet_meta"]["compression"] = "rejected: missing required sections"
            return packet
        try:
            goal_map = {c["id"]: c for c in packet["goal"]["completion_criteria"]}
            c_goal_map = {c["id"]: c for c in candidate["goal"]["completion_criteria"]}
            ev_map = {e["id"]: e for e in packet["new_evidence"]}
            c_ev_map = {e["id"]: e for e in candidate["new_evidence"]}
            unresolved = {u.get("task") or u.get("open_question"): u for u in packet["unresolved"]}
            c_unresolved = {u.get("task") or u.get("open_question"): u
                            for u in candidate.get("unresolved", [])}
            refs = {a["ref"]: a for a in packet.get("key_artifacts", [])}
            c_refs = {a["ref"]: a for a in candidate.get("key_artifacts", [])}
            refuted = {r.get("text") or r.get("id"): r for r in packet.get("refuted", [])}
            c_refuted = {r.get("text") or r.get("id"): r for r in candidate.get("refuted", [])}
        except (KeyError, TypeError):
            packet["packet_meta"]["compression"] = "rejected: wrong types"
            return packet
        if set(goal_map) - set(c_goal_map) or set(ev_map) - set(c_ev_map) \
                or set(unresolved) - set(c_unresolved) or set(refs) - set(c_refs) \
                or set(refuted) - set(c_refuted):
            packet["packet_meta"]["compression"] = "rejected: lost ids/evidence/unresolved items"
            return packet
        for cid, crit in goal_map.items():
            other = c_goal_map[cid]
            if other.get("status") != crit.get("status") or \
                    list(other.get("evidence") or []) != list(crit.get("evidence") or []):
                packet["packet_meta"]["compression"] = "rejected: goal criterion status/evidence changed"
                return packet
        for eid, ev in ev_map.items():
            other = c_ev_map[eid]
            if bool(other.get("verified")) != bool(ev.get("verified")) or \
                    bool(other.get("completion_usable")) != bool(ev.get("completion_usable")):
                packet["packet_meta"]["compression"] = "rejected: evidence flags changed"
                return packet
        for key, item in unresolved.items():
            other = c_unresolved[key]
            if item.get("status") and other.get("status") != item.get("status"):
                packet["packet_meta"]["compression"] = "rejected: unresolved status changed"
                return packet
        for key, item in refuted.items():
            other = c_refuted[key]
            if item.get("reason") and other.get("reason") != item.get("reason"):
                packet["packet_meta"]["compression"] = "rejected: refutation reason changed"
                return packet
        if "research" in packet:
            candidate_research = candidate.get("research")
            if not isinstance(packet.get("research"), dict) \
                    or not isinstance(candidate_research, dict):
                packet["packet_meta"]["compression"] = \
                    "rejected: research section missing or empty"
                return packet
            try:
                if not candidate_research.get("enabled") or \
                        researchmod.packet_signature(packet["research"]) != \
                        researchmod.packet_signature(candidate_research):
                    packet["packet_meta"]["compression"] = \
                        "rejected: research ids/status/references changed"
                    return packet
            except (KeyError, TypeError, AttributeError):
                packet["packet_meta"]["compression"] = "rejected: research shape changed"
                return packet
        new_chars = len(util.canonical(candidate))
        original_chars = len(util.canonical(packet))
        if new_chars >= original_chars:
            packet["packet_meta"]["compression"] = "rejected: not smaller"
            return packet
        candidate["packet_meta"] = dict(packet["packet_meta"])
        candidate["packet_meta"].update({
            "compressed": True, "original_chars": original_chars, "chars": new_chars,
            "est_tokens": util.estimate_tokens(util.canonical(candidate)),
        })
        final_chars = len(util.canonical(candidate))
        if final_chars >= original_chars:
            packet["packet_meta"]["compression"] = "rejected: not smaller"
            return packet
        candidate["packet_meta"]["chars"] = final_chars
        return candidate

    def _build_and_store_packet(self) -> dict:
        if self.state.get("status") in ("complete", "paused", "needs_input", "cancelled",
                                        "paused_budget", "paused_no_progress"):
            return {}
        if self._research_enabled():
            self._prepare_research_view()
        packet = packetmod.build_packet(
            self.state, int(self.cfg.get("loop", {}).get("packet_max_chars", 30000)),
            run_dir=self.store.dir)
        packet = self._maybe_compress(packet)
        seq = int(self.state.get("packet_seq", 0)) + 1
        path = self.store.packet_path(seq)
        util.atomic_write_json(path, packet)
        self.state["packet_seq"] = seq
        self.state["pending_packet"] = str(path.relative_to(self.store.dir))
        self.state["packet_watermark"] = self.state.get("evidence_seq", 0)
        if self.state.get("status") not in ("complete", "paused", "needs_input", "cancelled"):
            self.state["status"] = "awaiting_decision"
        self.commit("packet_written", {"packet": str(path.relative_to(self.store.dir)),
                                       "chars": packet["packet_meta"]["chars"]},
                    summary=f"packet {seq}")
        return packet

    def current_packet(self) -> dict | None:
        rel = self.state.get("pending_packet")
        if not rel:
            return None
        path = self.store.dir / rel
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------ budget/model calls
    def _estimate_call_cost(self, model: str, input_tokens: int, output_tokens: int):
        price = cfgmod.price_for(model, self.cfg.get("prices", cfgmod.PRICES))
        if not price:
            return None
        synthetic = {"input_tokens": input_tokens, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 0, "output_tokens": output_tokens}
        est = cfgmod.estimate_cost(synthetic, model, util.now(), self.cfg.get("prices", cfgmod.PRICES))
        return est.get("usd")

    def _call_model(self, role_name: str, prompt: str, *, cwd, tools=None,
                    max_turns=None, timeout=None, add_dirs=(), purpose="call",
                    model=None, task=None, max_output_tokens=None,
                    usage_sink: list | None = None, live_task: str | None = None) -> dict:
        role = self.roles[role_name]
        model = model or (task or {}).get("model") or role.model
        if model != role.model:
            prefix = cfgmod.KNOWN_PROVIDERS.get(role.provider, {}).get("model_prefix")
            if prefix and not model.lower().startswith(prefix):
                return {"ok": False, "error": f"任务模型 {model} 与角色 {role_name} 的 provider "
                                              f"{role.provider} 不一致；拒绝跨 provider 调用。",
                        "error_kind": "config", "text": "", "usage": {},
                        "usage_missing": True, "seconds": 0.0, "role": role_name,
                        "model_requested": model, "identity": "mismatch",
                        "model_observed": [], "provider": role.provider,
                        "endpoint": role.endpoint}
        out_cap = (max_output_tokens
                   or (task or {}).get("max_output_tokens")
                   or role.max_output_tokens)
        if self._should_cancel():
            return {"ok": False, "text": "", "error": "已取消", "error_kind": "cancelled",
                    "cancelled": True, "usage": {}, "usage_missing": True,
                    "seconds": 0.0, "role": role_name, "model_requested": model,
                    "identity": "unknown", "model_observed": [],
                    "provider": role.provider, "endpoint": role.endpoint}
        est_in = util.estimate_tokens(prompt)
        allowance = self.budget.allow_output(est_in, int(out_cap))
        if allowance is None:
            return {"ok": False, "error": "预算停止：max_tokens", "error_kind": "budget",
                    "budget_blocked": True, "budget_reason": "max_tokens", "text": "",
                    "usage": {}, "usage_missing": True, "seconds": 0.0,
                    "role": role_name, "model_requested": model, "identity": "unknown",
                    "model_observed": [], "provider": role.provider,
                    "endpoint": role.endpoint}
        est_out = allowance
        est_cost = self._estimate_call_cost(model, est_in, est_out)
        reservation, why = self.budget.reserve(
            role_name, est_in, est_out, est_cost,
            unknown_cost=(est_cost is None))
        if reservation is None:
            return {"ok": False, "error": f"预算停止：{why}", "error_kind": "budget",
                    "budget_blocked": True, "budget_reason": why, "text": "",
                    "usage": {}, "usage_missing": True, "seconds": 0.0,
                    "role": role_name, "model_requested": model, "identity": "unknown",
                    "model_observed": [], "provider": role.provider,
                    "endpoint": role.endpoint}
        usage_record = None
        res = None
        # live_task labels a call that belongs to a task but must not take its
        # settings (a reviewer keeps its own model).
        tracker = self.live.begin(live_task or (task or {}).get("id") or purpose,
                                  LIVE_PHASES.get(purpose, purpose), model,
                                  (task or {}).get("version"))
        try:
            backend = self.backends[role_name]
            extra = ({"on_line": tracker.on_line}
                     if "on_line" in inspect.signature(backend.call).parameters else {})
            res = backend.call(prompt, cwd=cwd, tools=tools, max_turns=max_turns,
                               timeout=timeout, add_dirs=add_dirs,
                               cancel_event=self.cancel_event, purpose=purpose,
                               model=model, max_output_tokens=est_out, **extra)
        except Exception as exc:  # a backend bug must not kill the batch
            res = {"ok": False, "text": "", "usage": {}, "usage_missing": True,
                   "error": f"{type(exc).__name__}: {exc}", "error_kind": "unknown",
                   "seconds": 0.0, "role": role_name, "model_requested": model,
                   "identity": "unknown", "model_observed": [],
                   "provider": role.provider, "endpoint": role.endpoint}
        finally:
            tracker.close(res)
        if res.get("identity") == "mismatch":
            res["ok"] = False
            res["error"] = ("模型身份不一致：请求 " + model + "，响应报告 "
                            + ",".join(res.get("model_observed") or []) +
                            "。没有静默改用其他模型。")
            res["error_kind"] = "identity"
        try:
            usage_record = self.budget.record(role_name, res, model, purpose=purpose)
            if usage_sink is not None:
                usage_sink.append(usage_record)
            res["usage_record"] = usage_record
        finally:
            # Record before releasing so a concurrent reservation cannot overshoot
            # the call limit while this slot is being converted into usage.
            self.budget.release(reservation)
        return res

    def _record_external_decision(self, model: str, usage_sink: list | None = None) -> dict:
        res = {"usage": {}, "usage_missing": True, "provider": "external",
               "endpoint": None, "identity": "unknown", "model_observed": [],
               "seconds": None, "total_cost_usd": None}
        record = self.budget.record("planner", res, model, external=True)
        if usage_sink is not None:
            usage_sink.append(record)
        return record

    # ------------------------------------------------------------ recovery
    def recover(self) -> list[dict]:
        notes = []
        running = [t for t in self.state["tasks"].values() if t.get("status") == "running"]
        for task in running:
            ctx = task.get("run_ctx") or {}
            changes = None
            if task.get("kind") == "code" and ctx:
                try:
                    changes = self.workspace.capture(ctx, task)
                except Exception as exc:
                    changes = {"kind": "capture_failed", "error": str(exc)}
            target = "unknown" if task.get("side_effects") else "interrupted"
            task["status"] = target
            task["last_error"] = ("进程在执行中被中断；不能假定它没有产生副作用，"
                                  "需要主持者决定修复或重规划。"
                                  if task.get("side_effects") else
                                  "进程在执行中被中断；没有副作用，但不能假定已完成。")
            task["run_ctx"] = None
            if changes:
                task["recovery_changes"] = {
                    "changed_files": changes.get("changed_files", []),
                    "scope_ok": changes.get("scope_ok"),
                    "outside_scope": changes.get("outside_scope", []),
                    "diff_path": self._save_patch(task, changes.get("diff"), "recovery"),
                }
            record = self.budget.record(
                "worker", {"usage": {}, "usage_missing": True, "seconds": None},
                self.roles["worker"].model, purpose="interrupted")
            self.state.setdefault("usage_calls", []).append(record)
            notes.append({"task": task["id"], "to": target,
                          "note": task["last_error"]})
        if running or self.state.get("status") == "executing":
            self.state["status"] = "awaiting_decision"
            self.state["recovery"] = (self.state.get("recovery") or []) + notes
            self.state["triggers"] = [{"kind": "recovered", "detail":
                                       "上次运行中断；正在执行的任务已标成 interrupted/unknown，未自动重放"}]
            self.commit("recovered", {"notes": notes}, summary="recover interrupted run")
        if self.store.cancel_requested():
            self._cancel_state("收到取消请求")
            self.commit("cancelled", {}, summary="cancelled")
        return notes

    def _save_patch(self, task: dict, diff_text: str | None, tag: str) -> str | None:
        if not diff_text:
            return None
        version = task.get("_current_attempt") or task.get("attempts", 1) or 1
        path = self.store.artifact_dir(task["id"]) / f"v{version}.{tag}.patch"
        util.atomic_write_text(path, diff_text)
        return str(path.relative_to(self.store.dir))

    # ------------------------------------------------------------ scheduling
    def _should_cancel(self) -> bool:
        return self.cancel_event.is_set() or self.store.cancel_requested()

    def _cancel_state(self, reason: str) -> None:
        self.state["status"] = "cancelled"
        self.state["stop_reason"] = reason
        for task in self.state["tasks"].values():
            if task.get("status") == "running":
                task["status"] = "unknown" if task.get("side_effects") else "interrupted"
                task["last_error"] = "运行被取消"
                task["run_ctx"] = None
        self._sync_budget()

    def _dep_usable(self, task: dict, dep_task: dict) -> bool:
        if dep_task.get("status") in ("running", "pending"):
            return False
        if task.get("kind") == "verify" or task.get("verify_of"):
            return bool(dep_task.get("artifact"))
        if dep_task.get("kind") == "code" and dep_task.get("status") != "completed":
            return False
        return dep_task.get("status") in ("completed", "unreviewed") and \
            not dep_task.get("stale")

    def _ready_tasks(self) -> list[dict]:
        ready = []
        for task in self.state["tasks"].values():
            if task.get("status") != "pending":
                continue
            deps = task.get("depends_on") or []
            if all(d in self.state["tasks"] and
                   self._dep_usable(task, self.state["tasks"][d]) for d in deps):
                ready.append(task)
        ready.sort(key=lambda t: (t.get("created_round", 0), t.get("id", "")))
        return ready

    def _mark_blocked(self) -> int:
        marked = 0
        # A dependency that was unverified can become completed after a repair.
        # Do not leave its dependants permanently blocked on the old status.
        for task in self.state["tasks"].values():
            if task.get("status") != "blocked":
                continue
            deps = task.get("depends_on") or []
            if deps and all(
                    d in self.state["tasks"] and self._dep_usable(task, self.state["tasks"][d])
                    for d in deps):
                task["status"] = "pending"
                task["last_error"] = None
        for task in self.state["tasks"].values():
            if task.get("status") != "pending":
                continue
            bad = []
            for dep in task.get("depends_on") or []:
                dep_task = self.state["tasks"].get(dep)
                if dep_task is None:
                    bad.append(f"{dep}（不存在）")
                    continue
                if dep_task.get("status") in ("running", "pending"):
                    continue
                if not self._dep_usable(task, dep_task):
                    bad.append(f"{dep}（{dep_task.get('status')}）")
            if bad and not any(self.state["tasks"].get(d, {}).get("status") in ("running", "pending")
                               for d in task.get("depends_on") or []):
                task["status"] = "blocked"
                task["last_error"] = "上游不可用：" + "、".join(bad)
                marked += 1
        return marked

    def _prepare_job(self, task: dict) -> dict:
        upstream_parts = []
        dep_versions = {}
        patches = []
        patch_seen = set()

        def collect_patches(dep_id: str) -> None:
            dep_task = self.state["tasks"].get(dep_id)
            if not dep_task or dep_id in patch_seen:
                return
            patch_seen.add(dep_id)
            for sub in dep_task.get("depends_on") or []:
                collect_patches(sub)
            changes = dep_task.get("changes") or {}
            if (dep_task.get("kind") == "code" and dep_task.get("status") == "completed"
                    and changes.get("patch_path")):
                patch_path = self.store.dir / changes["patch_path"]
                if not patch_path.exists():
                    raise artifacts.WorkspaceError(
                        f"上游 {dep_id} 的补丁文件不存在：{patch_path}")
                patches.append({"task": dep_id, "path": str(patch_path),
                                "sha256": changes.get("diff_sha256")})

        cap = int(self.cfg.get("loop", {}).get("upstream_chars", 6000))
        for dep in task.get("depends_on") or []:
            dep_task = self.state["tasks"][dep]
            art = dep_task.get("artifact")
            dep_versions[dep] = {
                "definition_version": dep_task.get("version"),
                "result_version": dep_task.get("result_version"),
                "sha256": (art or {}).get("sha256"),
                "status": dep_task.get("status"),
            }
            collect_patches(dep)
            if art:
                path = self.store.dir / art["path"]
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = "（产物文件不可读）"
                status_note = ("" if dep_task.get("status") == "completed"
                               else f"（状态 {dep_task.get('status')}，只能当参考）")
                upstream_parts.append(
                    f"### {dep} {status_note}\n版本：v{dep_task.get('result_version')}，"
                    f"sha256：{art.get('sha256')}\n{util.truncate(text, cap)}")
            else:
                upstream_parts.append(
                    f"### {dep}（没有产物）\n状态：{dep_task.get('status')}；"
                    f"错误：{dep_task.get('last_error') or '无'}")
        previous = task.get("previous")
        previous_patches = []
        if task.get("kind") == "code" and previous:
            inheritance = task.get("repair_inheritance") or previous.get("inheritance")
            if inheritance == "inherit_patch":
                prev_changes = previous.get("changes") or {}
                patch_path = prev_changes.get("patch_path")
                if patch_path:
                    previous_patches.append({
                        "task": task["id"],
                        "version": previous.get("version"),
                        "sha256": prev_changes.get("diff_sha256"),
                        "path": str(self.store.dir / patch_path),
                    })
        if previous:
            prev_art = previous.get("artifact")
            prev_review = previous.get("review") or {}
            prev_checks = previous.get("checks") or []
            parts = ["### 上一版产物（只作参考）"]
            if prev_art:
                try:
                    parts.append((self.store.dir / prev_art["path"]).read_text(encoding="utf-8")[:cap])
                except OSError:
                    parts.append("（上一版产物不可读）")
            if prev_review.get("errors"):
                parts.append("### 上一版复核意见\n" + "\n".join(prev_review["errors"][:8]))
            failed = [c for c in prev_checks if not c.get("ok")]
            if failed:
                parts.append("### 上一版没通过的检查\n" + "\n".join(
                    f"- {c['name']}（退出码 {c.get('exit')}）：{(c.get('output') or '')[-500:]}"
                    for c in failed[:4]))
            upstream_parts.append("\n".join(parts))
        input_hashes = {}
        base = None
        if task.get("input_files"):
            base = Path(self.state["workspace_config"].get("repo") or self.store.dir)
            for rel in task["input_files"]:
                path = (base / rel).resolve()
                if not path.is_file():
                    raise artifacts.WorkspaceError(f"输入文件不存在：{path}")
                input_hashes[rel] = {"sha256": util.sha256_file(path),
                                     "size": path.stat().st_size,
                                     "base": str(base)}
        ctx = self.workspace.prepare(task, task["attempts"] + 1, patches,
                                     previous_patches)
        previous_base = ((previous or {}).get("changes") or {}).get("base_commit")
        ctx["previous_base_changed"] = bool(
            previous_patches and previous_base and previous_base != ctx.get("base_commit"))
        return {"task": copy.deepcopy(task), "ctx": ctx, "dep_versions": dep_versions,
                "upstream": "\n\n".join(upstream_parts), "input_hashes": input_hashes,
                "attempt": task["attempts"] + 1}

    def _start_task(self, task: dict) -> dict | None:
        tid = task["id"]
        # A previous start commits and replaces self.state; a caller's task
        # reference from an older ready-list is stale after that commit.
        task = self.state["tasks"].get(tid)
        if task is None or task.get("status") != "pending":
            return None
        try:
            job = self._prepare_job(task)
        except artifacts.PatchConflict as exc:
            task["status"] = "patch_conflict"
            task["last_error"] = str(exc)
            self.commit("task_patch_conflict", {"task": task["id"]}, summary=str(exc)[:200])
            return None
        except artifacts.WorkspaceError as exc:
            task["status"] = "patch_conflict" if "补丁" in str(exc) else "exec_failed"
            task["last_error"] = str(exc)
            self.commit("task_workspace_error", {"task": task["id"]}, summary=str(exc)[:200])
            return None
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["status"] = "running"
        task["run_ctx"] = job["ctx"]
        task["dep_versions"] = job["dep_versions"]
        task["input_hashes"] = job["input_hashes"]
        task["hypothesis_versions"] = {
            hid: self.state["hypotheses"][hid]["version"]
            for hid in task.get("depends_on_hypotheses") or []
            if hid in (self.state.get("hypotheses") or {})
        }
        task["config_fingerprint"] = {
            "worker": self.roles["worker"].fingerprint(),
            "reviewer": self.roles["reviewer"].fingerprint(),
        }
        task["started_at"] = util.iso()
        task["last_error"] = None
        self.commit("task_started", {"task": task["id"], "attempt": task["attempts"]},
                    summary=f"start {task['id']} attempt {task['attempts']}")
        return job

    def _drain_futures(self, futures: dict) -> None:
        """Collect every in-flight job, including when the batch is pausing/cancelling.

        The pool waits for its workers on exit anyway; without applying their results
        here, an artifact could exist on disk while the state still says `running`.
        """
        while futures:
            done, _ = cf.wait(list(futures), return_when=cf.ALL_COMPLETED)
            for future in done:
                tid = futures.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:
                    result = self._internal_failure(tid, exc)
                self._apply_result(result)
                self.commit("task_result_drained",
                            {"task": tid, "status": result["status"]},
                            summary=f"{tid}: {result['status']} (drained)")

    def run_batch(self) -> int:
        executed = 0
        while True:
            if self._should_cancel():
                self._cancel_state("取消请求")
                self.commit("cancelled", {}, summary="cancelled")
                return executed
            can, why = self.budget.can_start_work()
            if not can:
                self.state["status"] = "paused"
                self.state["stop_reason"] = f"budget:{why}"
                self.state["triggers"] = [{"kind": "budget_exhausted", "detail": why}]
                return executed
            self._check_staleness()
            self._mark_blocked()
            ready = self._ready_tasks()
            if not ready:
                return executed
            side_effects = any(t.get("side_effects") for t in ready)
            isolated = self.workspace.is_git and self.workspace.isolate == "worktree"
            limit = int(self.cfg.get("loop", {}).get("concurrency", 4))
            if side_effects and not isolated:
                limit = 1
            futures = {}
            pool = cf.ThreadPoolExecutor(max_workers=max(1, limit))
            try:
                while True:
                    if self._should_cancel():
                        self._cancel_state("取消请求")
                        break
                    can, why = self.budget.can_start_work()
                    if not can:
                        self.state["status"] = "paused"
                        self.state["stop_reason"] = f"budget:{why}"
                        break
                    ready = self._ready_tasks()
                    for task in ready:
                        if len(futures) >= limit:
                            break
                        if task.get("status") != "pending":
                            continue
                        job = self._start_task(task)
                        if job is None:
                            continue
                        executed += 1
                        futures[pool.submit(self._execute_task, job)] = task["id"]
                    if not futures:
                        break
                    done, _ = cf.wait(list(futures), return_when=cf.FIRST_COMPLETED)
                    for future in done:
                        tid = futures.pop(future)
                        try:
                            result = future.result()
                        except BaseException as exc:
                            result = self._internal_failure(tid, exc)
                        self._apply_result(result)
                        self.commit("task_result",
                                    {"task": tid, "status": result["status"]},
                                    summary=f"{tid}: {result['status']}")
                    if self.state.get("status") in ("paused", "cancelled"):
                        break
            except KeyboardInterrupt:
                self._cancel_state("用户中断")
                self.commit("interrupted", {}, summary="keyboard interrupt")
            finally:
                if self.state.get("status") == "cancelled":
                    self.cancel_event.set()
                try:
                    self._drain_futures(futures)
                finally:
                    pool.shutdown(wait=True)
            if self.state.get("status") in ("paused", "cancelled"):
                return executed
        return executed

    def run_until_decision(self, *, recover: bool = True) -> None:
        if recover:
            self.recover()
        while True:
            if self._should_cancel():
                self._cancel_state("取消请求")
                self.commit("cancelled", {}, summary="cancelled")
                return
            if self.state.get("status") in ("complete", "paused", "needs_input", "cancelled"):
                return
            executed = self.run_batch()
            self._check_staleness()
            self._compute_triggers(executed)
            self._maybe_run_critic()
            self.state["progress"]["last_progress_fp"] = self._progress_fp()
            if self.state.get("status") in ("complete", "paused", "needs_input", "cancelled"):
                self.commit("stopped", {"status": self.state.get("status")},
                            summary=self.state.get("stop_reason") or "")
                return
            self._build_and_store_packet()
            return

    # ------------------------------------------------------------ task execution
    def _internal_failure(self, tid: str, exc: BaseException) -> dict:
        task = self.state["tasks"].get(tid) or {"id": tid, "attempts": 1}
        return {
            "task_id": tid, "status": "exec_failed",
            "error": security.redact(f"{type(exc).__name__}: {exc}")[:1000],
            "error_kind": "internal", "usage_records": [], "artifact": None,
            "checks": [], "negative": None, "changes": None, "review": None,
            "acceptance": None, "selfcheck": None,
            "traceback": security.redact(traceback.format_exc())[-2000:],
            "attempt": task.get("attempts", 1),
        }

    def _execute_task(self, job: dict) -> dict:
        task = job["task"]
        ctx = job["ctx"]
        tid = task["id"]
        attempt = job["attempt"]
        task["_current_attempt"] = attempt
        usage: list = []
        started = time.time()
        result = {
            "task_id": tid, "status": "exec_failed", "artifact": None,
            "checks": [], "negative": None, "changes": None, "review": None,
            "acceptance": None, "selfcheck": None, "usage_records": usage,
            "error": None, "error_kind": None, "attempt": attempt,
            "started_at": util.iso(), "ctx": ctx,
        }
        call_started = False
        try:
            prompt = prompts.worker_prompt(
                task, job["upstream"],
                workspace_note=self._workspace_note(task, ctx))
            max_tries = int(self.cfg.get("loop", {}).get("exec_retries", 1)) + 1
            worker_res = None
            for i in range(max_tries):
                worker_res = self._call_model(
                    "worker", prompt, cwd=ctx["cwd"], tools=task.get("tools"),
                    max_turns=task.get("max_turns"), timeout=task.get("timeout"),
                    add_dirs=[str(self.store.dir)], purpose="call" if i == 0 else "retry",
                    task=task, usage_sink=usage)
                if worker_res.get("ok"):
                    break
                if worker_res.get("budget_blocked"):
                    result.update(status="pending", error=worker_res.get("error"),
                                  error_kind="budget", budget_blocked=True)
                    return self._finish(result, started)
                kind = worker_res.get("error_kind")
                retryable = (kind == "transient" and not task.get("side_effects")
                             and i + 1 < max_tries)
                if not retryable:
                    break
                time.sleep(min(5, 1.5 * (i + 1)))
            call_started = True
            if not worker_res or not worker_res.get("ok"):
                status = "exec_failed"
                if task.get("side_effects") and worker_res and worker_res.get("error_kind") in (
                        "timeout", "unknown", "cancelled"):
                    status = "unknown"
                elif worker_res and worker_res.get("error_kind") == "cancelled":
                    status = "interrupted"
                result.update(status=status, error=(worker_res or {}).get("error"),
                              error_kind=(worker_res or {}).get("error_kind"))
                # A timed-out code worker may already have changed the worktree.
                if task.get("kind") == "code":
                    result["changes"] = self._capture(task, ctx, worker_res)
                return self._finish(result, started)
            body, selfcheck = split_selfcheck(worker_res.get("text", ""))
            if not body.strip():
                result.update(status="exec_failed", error="worker 返回了空正文",
                              error_kind="empty_output")
                return self._finish(result, started)
            result["selfcheck"] = selfcheck
            art_path = self.store.artifact_dir(tid) / f"v{attempt}.md"
            util.atomic_write_text(art_path, body)
            artifact = {"version": attempt, "path": str(art_path.relative_to(self.store.dir)),
                        "sha256": util.sha256_text(body), "chars": count_chars(body)}
            result["artifact"] = artifact
            changes = self._capture(task, ctx, worker_res)
            result["changes"] = changes
            if changes.get("patch_path"):
                artifact["patch_path"] = changes["patch_path"]
                artifact["diff_sha256"] = changes.get("diff_sha256")
            checks = []
            if task.get("kind") == "code" and changes:
                checks.append({
                    "name": "changes",
                    "ok": bool(changes.get("changed_files")),
                    "exit": 0 if changes.get("changed_files") else 1,
                    "expected_exit": 0, "seconds": 0.0,
                    "output": "" if changes.get("changed_files") else "没有任何实际文件改动",
                })
                checks.append({
                    "name": "scope", "ok": bool(changes.get("scope_ok", True)),
                    "exit": 0 if changes.get("scope_ok", True) else 1,
                    "expected_exit": 0, "seconds": 0.0,
                    "output": ("修改了允许范围外的文件：" + "、".join(changes.get("outside_scope") or []))
                              if not changes.get("scope_ok", True) else "",
                })
                if not changes.get("patch_path"):
                    checks.append({
                        "name": "patch_evidence", "ok": False, "exit": 1, "expected_exit": 0,
                        "seconds": 0.0,
                        "output": "代码任务没有 git 补丁证据；非 git 工作区不能作为完成证据",
                    })
                conflict = changes.get("patch_conflict")
                if conflict:
                    checks.append({"name": "patch_conflict", "ok": bool(conflict.get("ok")),
                                   "exit": 0 if conflict.get("ok") else 1, "expected_exit": 0,
                                   "seconds": 0.0,
                                   "output": conflict.get("reason") or conflict.get("output") or ""})
            checks.extend(acceptance.run_checks(task, Path(ctx["cwd"]), art_path, self.cfg,
                                                self.cancel_event))
            negative = None
            if all(c.get("ok") for c in checks):
                negative = acceptance.run_negative_check(
                    task, Path(ctx["cwd"]), art_path, self.cfg, self.workspace,
                    self.cancel_event)
            if task.get("max_chars") and count_chars(body) > task["max_chars"]:
                checks.append({"name": "length", "ok": False, "exit": 1, "expected_exit": 0,
                               "seconds": 0.0,
                               "output": f"正文 {count_chars(body)} 字，超过上限 {task['max_chars']} 字"})
            result["checks"] = checks
            result["negative"] = negative
            policy = task.get("acceptance_policy", "none")
            review = None
            review_skipped = None
            deterministic_ok = all(c.get("ok") for c in checks) and (
                negative is None or bool(negative.get("ok")))
            if policy in ("semantic", "combined") and deterministic_ok:
                def call_reviewer(review_prompt, purpose="review"):
                    return self._call_model(
                        "reviewer", review_prompt, cwd=Path(ctx["cwd"]),
                        tools=self.roles["reviewer"].tools,
                        add_dirs=[str(self.store.dir), str(self.store.dir / "artifacts")],
                        purpose=purpose, usage_sink=usage, live_task=task["id"])
                review = acceptance.semantic_review(
                    task, body, checks, changes, self.cfg.get("loop", {}), call_reviewer,
                    self.cancel_event)
            elif policy in ("semantic", "combined"):
                review_skipped = "程序检查未通过，跳过了复核（先修实现再复核）"
            if policy in ("semantic", "combined"):
                acc = acceptance.evaluate(task, checks, review, negative)
                if review is None:
                    acc["review_outcome"] = "skipped_after_check_failed" if review_skipped else "unavailable"
                    acc["review_skipped_reason"] = review_skipped
            elif policy == "deterministic":
                acc = acceptance.evaluate(task, checks, None, negative)
            else:
                acc = acceptance.evaluate(task, checks, None, negative)
                acc["review_outcome"] = "not_required"
            result["acceptance"] = acc
            if review is not None:
                result["review"] = {
                    "outcome": review.get("outcome"),
                    "pass": review.get("pass"),
                    "errors": (review.get("errors") or [])[:8],
                    "fix_instructions": review.get("fix_instructions", "")[:2000],
                }
            if policy == "none":
                status = "unreviewed"
            elif acc.get("passed"):
                status = "completed"
            elif any((not c.get("ok")) and c.get("name") in
                     ("patch_conflict", "cross_patch_conflict") for c in checks):
                status = "patch_conflict"
            elif acc.get("review_outcome") == "unavailable":
                status = "review_unavailable"
            elif acc.get("review_outcome") == "invalid":
                status = "review_invalid"
            else:
                status = "unverified"
            result["status"] = status
            if task.get("kind") == "code" and changes:
                result["changes"] = changes
            return self._finish(result, started)
        except Exception as exc:
            status = "unknown" if (task.get("side_effects") and call_started) else "exec_failed"
            result.update(status=status,
                          error=security.redact(f"{type(exc).__name__}: {exc}")[:1000],
                          error_kind="internal",
                          traceback=security.redact(traceback.format_exc())[-2000:])
            return self._finish(result, started)

    def _workspace_note(self, task: dict, ctx: dict) -> str:
        ws = self.state.get("workspace") or {}
        notes = []
        if ws.get("dirty_at_start"):
            notes.append("用户工作区有未提交改动；本任务在独立的 git worktree 中执行，"
                         "从 HEAD 建立，不包含这些改动，程序不会重置用户文件。")
        if task.get("kind") == "code":
            if ctx.get("isolated"):
                notes.append("本任务在独立的 git worktree 中执行；只修改 allowed_paths 内的文件。")
            else:
                notes.append("工作区没有使用 git 隔离；代码任务被串行化，改动会直接落在工作区。")
            notes.append(f"基线 commit：{ctx.get('base_commit')}；只修改 allowed_paths 内的文件。")
            if ctx.get("applied_patches"):
                notes.append("已应用上游补丁：" + "、".join(
                    p.get("task", "?") for p in ctx["applied_patches"]))
            if ctx.get("previous_patches"):
                notes.append("已继承上一版代码补丁：" + "、".join(
                    f"v{p.get('version')}" for p in ctx["previous_patches"]))
            if ctx.get("previous_base_changed"):
                notes.append("上一版补丁的基线 commit 与当前输入基线不同；最终 patch 按当前基线重新计算。")
        return "\n".join(notes)

    def _capture(self, task: dict, ctx: dict, worker_res: dict | None) -> dict:
        try:
            changes = self.workspace.capture(ctx, task)
        except Exception as exc:
            return {"kind": "capture_failed", "error": str(exc), "scope_ok": False}
        if changes.get("diff"):
            changes["patch_path"] = self._save_patch(task, changes["diff"], "change")
            conflict = self.workspace.check_patch_conflict(
                ctx.get("base_commit"), ctx.get("applied_patches", []), changes["diff"])
            changes["patch_conflict"] = conflict
        return changes

    def _cross_patch_conflict(self, task: dict, ctx: dict, changes: dict) -> dict | None:
        """Check an independent write task's patch against other completed write tasks.

        Worktrees make concurrent writes safe, but two patches can still touch the same
        lines. Detecting that here turns it into an explicit conflict instead of a
        surprise when a downstream task tries to apply both.
        """
        if task.get("kind") != "code" or not changes.get("diff"):
            return None
        base = ctx.get("base_commit")
        if not base or not changes.get("patch_path"):
            return None
        applied = {p.get("task") for p in ctx.get("applied_patches", [])}
        prior = []
        seen_paths = {str(p.get("path")) for p in ctx.get("applied_patches", [])}
        for other in (self.state.get("tasks") or {}).values():
            if other["id"] == task["id"] or other["id"] in applied:
                continue
            och = other.get("changes") or {}
            if (other.get("kind") == "code" and other.get("status") == "completed"
                    and och.get("patch_path") and och.get("base_commit") == base):
                for dep_patch in och.get("applied_patches") or []:
                    path = dep_patch.get("path")
                    if path and path not in seen_paths:
                        seen_paths.add(path)
                        prior.append({"task": dep_patch.get("task"), "path": path,
                                      "sha256": dep_patch.get("sha256")})
                own = str(self.store.dir / och["patch_path"])
                if own not in seen_paths:
                    seen_paths.add(own)
                    prior.append({"task": other["id"], "path": own,
                                  "sha256": och.get("diff_sha256")})
        if not prior:
            return None
        return self.workspace.check_patch_conflict(
            base, list(ctx.get("applied_patches", [])) + prior, changes["diff"])

    def _finish(self, result: dict, started: float) -> dict:
        result["seconds"] = round(time.time() - started, 2)
        result["finished_at"] = util.iso()
        return result

    # ------------------------------------------------------------ state application
    def _add_evidence(self, task: dict, kind: str, summary: str, *, verified: bool,
                      artifact: str | None = None, sha256: str | None = None,
                      criteria: list | None = None, check_name: str | None = None,
                      output: str | None = None, review_outcome: str | None = None,
                      extra: dict | None = None) -> str:
        self.state["evidence_seq"] = int(self.state.get("evidence_seq", 0)) + 1
        ev_id = f"ev-{self.state['evidence_seq']:04d}"
        changes = task.get("changes") or {}
        usable = bool(verified and kind == "artifact" and task.get("status") == "completed")
        if usable and task.get("kind") == "code":
            usable = bool(changes.get("patch_path") and changes.get("scope_ok"))
        record = {
            "id": ev_id, "seq": self.state["evidence_seq"], "at": util.iso(),
            "kind": kind, "task": task["id"], "version": task.get("result_version"),
            "definition_version": task.get("version"),
            "acceptance_signature": acceptance.criteria_signature(task.get("acceptance") or []),
            "task_status": task.get("status"), "task_kind": task.get("kind"),
            "verified": bool(verified), "artifact": artifact, "sha256": sha256,
            "completion_usable": usable,
            "patch_path": changes.get("patch_path"),
            "diff_sha256": changes.get("diff_sha256"),
            "changed_files": [c.get("path") for c in (changes.get("changed_files") or [])],
            "base_commit": changes.get("base_commit"),
            "input_commit": changes.get("input_commit"),
            "scope_ok": changes.get("scope_ok"),
            "criteria": criteria or [], "goal_criteria": task.get("satisfies") or [],
            "satisfies": task.get("satisfies") or [],
            "summary": security.redact(summary or "")[:800],
            "check_name": check_name, "review_outcome": review_outcome,
            "output_tail": security.redact(output or "")[-800:] if output else None,
        }
        if extra:
            record.update(extra)
        self.state["evidence"][ev_id] = record
        task.setdefault("evidence_ids", []).append(ev_id)
        if self._research_enabled():
            researchmod.observation(self.state, task, record)
        return ev_id

    def _apply_result(self, result: dict) -> None:
        tid = result["task_id"]
        task = self.state["tasks"].get(tid)
        if task is None:
            return
        run_ctx = task.get("run_ctx") or result.get("ctx") or {}
        task["status"] = result.get("status", "exec_failed")
        task["finished_at"] = result.get("finished_at")
        task["duration_seconds"] = result.get("seconds")
        task["run_ctx"] = None
        if result.get("budget_blocked"):
            task["status"] = "pending"
            task["last_error"] = result.get("error")
            task["budget_blocked"] = True
            self.state["status"] = "paused"
            self.state["stop_reason"] = "budget:" + str(result.get("error"))
            self.state["triggers"] = [{"kind": "budget_exhausted", "detail": str(result.get("error"))}]
            self._sync_budget()
            return
        artifact = result.get("artifact")
        if artifact:
            task["artifact"] = artifact
            task["result_version"] = artifact["version"]
            task.setdefault("artifact_history", []).append(artifact)
            task["chars"] = artifact.get("chars")
            task["critic"] = None
        task["selfcheck"] = result.get("selfcheck")
        if result.get("changes"):
            task["changes"] = result["changes"]
        if result.get("acceptance") is not None:
            task["acceptance_result"] = result["acceptance"]
        if result.get("review") is not None:
            task["review"] = result["review"]
        if result.get("error"):
            task["last_error"] = result["error"]
        else:
            task["last_error"] = None
        if result.get("error_kind"):
            task["error_kind"] = result["error_kind"]
        if result.get("traceback"):
            task["traceback_tail"] = result["traceback"]
        if (task["status"] == "completed" and task.get("kind") == "code"
                and task.get("changes")):
            cross = self._cross_patch_conflict(task, run_ctx, task["changes"])
            if cross is not None and not cross.get("ok"):
                task["changes"]["cross_patch_conflict"] = cross
                acc = task.setdefault("acceptance_result", {})
                acc.setdefault("checks", []).append({
                    "name": "cross_patch_conflict", "ok": False, "exit": 1,
                    "expected_exit": 0, "seconds": 0.0,
                    "output": cross.get("reason") or cross.get("output") or "",
                })
                acc["checks_ok"] = False
                acc["passed"] = False
                acc["verified"] = False
                task["status"] = "patch_conflict"
                task["last_error"] = "与另一个已完成写任务的补丁冲突，需要串行化或重规划"
        self.state.setdefault("usage_calls", []).extend(result.get("usage_records") or [])
        # Evidence records.
        art = task.get("artifact")
        criteria = [c["id"] for c in task.get("acceptance") or []]
        if art:
            self._add_evidence(
                task, "artifact",
                f"{tid} v{art['version']} 产物，{art.get('chars')} 字，状态 {task['status']}",
                verified=(task["status"] == "completed"), artifact=art["path"],
                sha256=art.get("sha256"), criteria=criteria,
                review_outcome=(task.get("acceptance_result") or {}).get("review_outcome"))
        for check in (task.get("acceptance_result") or {}).get("checks") or []:
            mapped = [c["id"] for c in task.get("acceptance") or [] if c.get("check") == check["name"]] or criteria
            self._add_evidence(
                task, "check", f"{tid} 检查 {check['name']}：{'通过' if check['ok'] else '未通过'}",
                verified=bool(check["ok"]),
                criteria=mapped, check_name=check["name"], output=check.get("output"))
        negative = (task.get("acceptance_result") or {}).get("negative")
        if negative:
            self._add_evidence(
                task, "negative_check",
                f"{tid} 负向检查：{'发现注入错误' if negative.get('ok') else '未发现注入错误'}",
                verified=bool(negative.get("ok")), criteria=criteria,
                check_name=negative.get("name"), output=negative.get("output"))
        review_outcome = (task.get("acceptance_result") or {}).get("review_outcome")
        if review_outcome not in (None, "not_required", "skipped_after_check_failed",
                                  "not_requested"):
            self._add_evidence(
                task, "review", f"{tid} 复核：{review_outcome}",
                verified=(review_outcome == "passed"), criteria=criteria,
                review_outcome=review_outcome,
                output="\n".join((task.get("review") or {}).get("errors") or []))
        if self._research_enabled():
            artifact_evidence = next(
                (self.state["evidence"][eid] for eid in reversed(task.get("evidence_ids") or [])
                 if eid in self.state["evidence"]
                 and self.state["evidence"][eid].get("kind") == "artifact"), None)
            if artifact_evidence:
                researchmod.key_result(self.state, task, artifact_evidence,
                                       int(self.state.get("round", 0)))
                prediction = task.get("research_prediction") or {}
                if prediction:
                    check = next((c for c in (task.get("acceptance_result") or {}).get("checks") or []
                                  if c.get("name") == prediction.get("check")), None)
                    if check and check.get("exit") != prediction.get("expect_exit"):
                        researchmod.prediction_mismatch(
                            self.state, task, artifact_evidence, check,
                            int(self.state.get("round", 0)))
        if task["status"] == "completed":
            self.state.setdefault("facts", []).append({
                "id": f"fact-{len(self.state.get('facts') or []) + 1}",
                "text": f"任务 {tid} 已按验收标准完成：{task.get('question') or task.get('prompt', '')[:120]}",
                "evidence": [task["evidence_ids"][-1]] if task.get("evidence_ids") else [],
                "verified": True, "at": util.iso(),
            })
        # Repeated error signature.
        sig = _signature(result)
        if task["status"] in ("unverified", "exec_failed", "review_unavailable",
                              "review_invalid", "interrupted", "unknown", "patch_conflict"):
            if sig and sig == task.get("error_signature"):
                task["repeat_count"] = int(task.get("repeat_count", 0)) + 1
            else:
                task["error_signature"] = sig
                task["repeat_count"] = 1
        else:
            task["error_signature"] = None
            task["repeat_count"] = 0
        self._sync_budget()

    # ------------------------------------------------------------ staleness
    def _progress_fp(self) -> str:
        tasks = self.state.get("tasks") or {}
        execution = {
            "completed": sorted(
                (t["id"], t.get("result_version"), (t.get("artifact") or {}).get("sha256"))
                for t in tasks.values() if t.get("status") == "completed"),
            "acceptance": sorted(
                (t["id"], acceptance.criteria_signature(t.get("acceptance") or []))
                for t in tasks.values()),
        }
        if self._research_enabled():
            return util.hash_obj({
                "execution": execution,
                "research": researchmod.progress_fp(self.state),
            })
        return util.hash_obj({"evidence_seq": self.state.get("evidence_seq", 0), **execution})

    def _check_staleness(self) -> None:
        changed = True
        guard = 0
        while changed and guard < 20:
            changed = False
            guard += 1
            for task in self.state["tasks"].values():
                if task.get("status") not in ("completed", "unverified", "review_unavailable",
                                              "review_invalid", "unreviewed", "stale"):
                    continue
                reasons = []
                for dep, recorded in (task.get("dep_versions") or {}).items():
                    dep_task = self.state["tasks"].get(dep)
                    if dep_task is None:
                        reasons.append(f"上游 {dep} 已被删除")
                        continue
                    art = dep_task.get("artifact") or {}
                    if (dep_task.get("result_version") != recorded.get("result_version")
                            or art.get("sha256") != recorded.get("sha256")
                            or dep_task.get("status") == "stale"):
                        reasons.append(
                            f"上游 {dep} 从 v{recorded.get('result_version')} 变成 "
                            f"v{dep_task.get('result_version')}（{dep_task.get('status')}）")
                for rel, recorded in (task.get("input_hashes") or {}).items():
                    base = Path(recorded.get("base") or self.state["workspace_config"].get("repo") or self.store.dir)
                    path = base / rel
                    current = util.sha256_file(path) if path.is_file() else None
                    if current != recorded.get("sha256"):
                        reasons.append(f"输入文件发生变化：{rel}")
                for role_name, recorded in (task.get("config_fingerprint") or {}).items():
                    role = self.roles.get(role_name)
                    if role is not None and recorded != role.fingerprint():
                        reasons.append(f"{role_name} 的模型/endpoint 配置与执行时不同")
                if reasons and task.get("status") != "stale":
                    task["status"] = "stale"
                    task["stale"] = True
                    task["stale_reason"] = "；".join(reasons[:5])
                    for ev in self.state["evidence"].values():
                        if ev.get("task") == task["id"]:
                            ev["verified"] = False
                            ev["stale"] = True
                            if self._research_enabled():
                                self._invalidate_research_evidence(
                                    ev["id"], task.get("stale_reason") or "任务失效")
                    changed = True
        if any(t.get("status") == "stale" for t in self.state["tasks"].values()):
            self.state["triggers"] = [{"kind": "stale_inputs",
                                       "detail": "有任务因上游或输入变化被标记 stale，旧结果不能直接沿用"}]
        if self._research_enabled():
            researchmod.refresh(self.state, int(self.state.get("round", 0)))

    # ------------------------------------------------------------ triggers
    def _compute_triggers(self, executed: int) -> None:
        tasks = self.state.get("tasks") or {}
        triggers = []
        if executed:
            triggers.append({"kind": "batch_completed", "detail": f"本轮完成 {executed} 个任务"})
        if not tasks:
            triggers.append({"kind": "initial_planning", "detail": "还没有任务"})
        bad = [t for t in tasks.values() if t.get("status") in (
            "unverified", "exec_failed", "review_unavailable", "review_invalid",
            "interrupted", "unknown", "blocked", "stale", "patch_conflict",
            "needs_review")]
        if bad:
            triggers.append({"kind": "task_failure",
                             "detail": "、".join(f"{t['id']}:{t['status']}" for t in bad[:12])})
        unavailable = [t["id"] for t in tasks.values() if t.get("status") == "review_unavailable"]
        if unavailable:
            triggers.append({"kind": "review_unavailable",
                             "detail": "、".join(unavailable) + "：要求复核但复核服务不可用，不能当作通过"})
        repeated = [t for t in tasks.values() if int(t.get("repeat_count") or 0) >= int(
            self.cfg.get("loop", {}).get("repeated_failure_limit", 3))]
        if repeated:
            triggers.append({"kind": "repeated_failure",
                             "detail": "、".join(f"{t['id']}({t['repeat_count']}次)" for t in repeated)})
        disagreements = [t["id"] for t in tasks.values()
                         if (t.get("acceptance_result") or {}).get("review_available")
                         and (t.get("acceptance_result") or {}).get("checks_ok") is not None
                         and (t.get("acceptance_result") or {}).get("review_passed") is not None
                         and (t["acceptance_result"]["checks_ok"]
                              != t["acceptance_result"]["review_passed"])]
        if disagreements:
            triggers.append({"kind": "disagreement",
                             "detail": "、".join(disagreements) + "：程序检查与复核结论不一致"})
        if self.state.get("completion_rejection"):
            triggers.append({"kind": "completion_rejected",
                             "detail": "；".join(self.state["completion_rejection"].get("errors") or [])[:500]})
        if self.state.get("recovery"):
            triggers.append({"kind": "recovered", "detail": "有中断任务，见 unresolved"})
        pending = [t for t in tasks.values() if t.get("status") == "pending"]
        running = [t for t in tasks.values() if t.get("status") == "running"]
        if pending or running:
            ready = self._ready_tasks()
            if not ready and not running:
                triggers.append({"kind": "deadlock",
                                 "detail": "有 pending 任务但依赖无法满足，需要 replan 或 cancel"})
        elif tasks and all(t.get("status") != "pending" for t in tasks.values()):
            triggers.append({"kind": "goal_maybe_complete",
                             "detail": "没有待执行任务；请核对完成条件，决定 complete 或继续派发"})
        if self.state.get("open_questions"):
            triggers.append({"kind": "open_questions",
                             "detail": f"{len(self.state['open_questions'])} 个未解决问题"})
        if self._research_enabled():
            research_pending = researchmod.pending(self.state)
            if research_pending:
                triggers.append({
                    "kind": "research_review_pending",
                    "detail": "、".join(p["id"] + ":" + str(p.get("trigger"))
                                       for p in research_pending[:8]),
                })
            reviewed = [h["id"] for h in (self.state.get("hypotheses") or {}).values()
                        if h.get("status") == "needs_review"]
            if reviewed:
                triggers.append({"kind": "research_recheck",
                                 "detail": "需要复审的假设：" + "、".join(reviewed[:12])})
        changed_roles = []
        stored = self.state.get("roles_summary") or {}
        for name in ("planner", "worker", "reviewer", "critic"):
            role = self.roles.get(name)
            if role is not None and stored.get(name) != role.fingerprint():
                changed_roles.append(name)
        if changed_roles:
            triggers.append({"kind": "config_changed",
                             "detail": "本次运行的模型/endpoint 配置与开始时不同：" + "、".join(changed_roles)
                                       + "；旧产物仍按原版本记录，需主持者判断是否重跑"})
        if self.state.get("stop_reason"):
            triggers.append({"kind": "stopped", "detail": self.state["stop_reason"]})
        self.state["triggers"] = triggers
        can, why = self.budget.can_start_work()
        if not can and (pending or running or self.state.get("status") not in (
                "complete", "paused", "needs_input", "cancelled")):
            self.state["status"] = "paused"
            self.state["stop_reason"] = f"budget:{why}"

    # ------------------------------------------------------------ critic
    def _maybe_run_critic(self) -> None:
        """Optional challenger, off by default. Never rewrites the task or decides truth."""
        ccfg = self.cfg.get("loop", {}).get("critic") or {}
        if not ccfg.get("enabled"):
            return
        role = self.roles.get("critic")
        if role is None or not role.enabled or "critic" not in self.backends:
            return
        threshold = int(ccfg.get("on_repeated_failures", 2))
        candidate = None
        for task in (self.state.get("tasks") or {}).values():
            acc = task.get("acceptance_result") or {}
            disagreement = (acc.get("review_available")
                            and acc.get("checks_ok") is not None
                            and acc.get("review_passed") is not None
                            and acc["checks_ok"] != acc["review_passed"])
            if task.get("status") in ("unverified", "review_unavailable", "review_invalid",
                                      "exec_failed", "unknown", "interrupted"):
                if (int(task.get("repeat_count") or 0) >= threshold or disagreement) \
                        and not task.get("critic") and task.get("artifact"):
                    candidate = task
                    break
        if candidate is None:
            return
        art = candidate.get("artifact") or {}
        try:
            text = (self.store.dir / art["path"]).read_text(encoding="utf-8")
        except OSError:
            text = ""
        evidence = [self.state["evidence"][e] for e in candidate.get("evidence_ids", [])[-8:]
                    if e in self.state["evidence"]]
        usage: list = []
        res = self._call_model(
            "critic", prompts.critic_prompt(candidate, text, evidence),
            cwd=self.store.dir, tools=role.tools,
            add_dirs=[str(self.store.dir)] + ([self.state["workspace_config"]["repo"]]
                                             if self.state["workspace_config"].get("repo") else []),
            purpose="critic", usage_sink=usage)
        self.state.setdefault("usage_calls", []).extend(usage)
        self._sync_budget()
        if not res.get("ok"):
            candidate["critic"] = {"ok": False, "error": res.get("error"),
                                   "at": util.iso()}
            return
        parsed = None
        for obj in util.json_objects(res.get("text", "")):
            if isinstance(obj, dict) and ("verdict" in obj or "assumptions" in obj):
                parsed = obj
                break
        candidate["critic"] = {
            "ok": True, "at": util.iso(), "verdict": (parsed or {}).get("verdict", "unknown"),
            "assumptions": (parsed or {}).get("assumptions") or [],
            "evidence_gaps": (parsed or {}).get("evidence_gaps") or [],
            "alternatives": (parsed or {}).get("alternatives") or [],
            "discriminating_check": (parsed or {}).get("discriminating_check"),
            "reason": (parsed or {}).get("reason", ""),
        }
        self._add_evidence(
            candidate, "critic",
            "独立质疑者：" + str((parsed or {}).get("verdict", "unknown")),
            verified=False,
            output=json.dumps(candidate["critic"], ensure_ascii=False)[:1500])
        self.state.setdefault("critic_findings", []).append({
            "task": candidate["id"], "version": candidate.get("result_version"),
            "verdict": candidate["critic"].get("verdict"),
            "assumptions": candidate["critic"].get("assumptions"),
            "discriminating_check": candidate["critic"].get("discriminating_check"),
        })

    # ------------------------------------------------------------ decisions
    def validate_decision(self, decision: dict) -> None:
        tasks = self.state["tasks"]
        if decision["action"] == "review" and not self._research_enabled():
            raise protocol.DecisionError("review 只在 research_mode 下可用")
        if decision.get("research") and not self._research_enabled():
            raise protocol.DecisionError("普通模式不接受 research；请用 research_mode 启动运行")
        if self._research_enabled():
            research = researchmod.ensure(self.state) or {}
            active = researchmod.pending(self.state)
            active_by_id = {p["id"]: p for p in active}
            known_by_id = {p["id"]: p
                           for p in self.state["research"]["pending_reviews"]}
            payload = decision.get("research") or {}
            review_ids = payload.get("review_ids") or []
            replay = bool(payload) and util.hash_obj(payload) in (
                research.get("review_fingerprints") or {})
            replay_review = replay and decision["action"] == "review"
            execution_actions = {"dispatch", "verify", "repair", "replan"}

            def resolve_observation(item):
                value = item.get("observation")
                observations = self.state["research"]["observations"]
                obs = observations.get(value)
                if not obs and value:
                    obs = next((o for o in observations.values()
                                if o.get("evidence") == value), None)
                if not obs and "evidence" in item:
                    obs = observations.get(item["evidence"]) or next(
                        (o for o in observations.values()
                         if o.get("evidence") == item["evidence"]), None)
                return obs
            if decision["action"] == "review" and not payload:
                raise protocol.DecisionError("review 必须给出 research")
            if replay and active and decision["action"] in execution_actions:
                # The research sub-object is already applied. It may be a
                # harmless idempotent replay, but it cannot authorize new work
                # while current pending results remain unreviewed.
                raise protocol.DecisionError(
                    "该 research payload 已应用过，不能作为新执行动作的审查许可；"
                    "请先按当前 pending 结果重新 review，再执行新的派发/修复/重规划")
            if replay_review:
                # Exact review replays are idempotent no-ops in
                # research.apply_review; do not re-judge references against a
                # state that has moved on.
                pass
            elif active and decision["action"] not in (
                    "request_context", "request_input", "pause"):
                if not review_ids:
                    if decision["action"] != "complete":
                        raise protocol.DecisionError(
                            "存在待审查研究结果；请用 review 或在 action 的 research.review_ids 中说明")
                elif not any(rid in active_by_id for rid in review_ids):
                    if decision["action"] != "complete":
                        raise protocol.DecisionError(
                            "research.review_ids 没有覆盖任何当前待审查结果；"
                            "已解决的 review id 不能代替新事件")
            if not replay:
                for rid in review_ids:
                    if rid not in known_by_id:
                        raise protocol.DecisionError(f"research.review_id 不存在：{rid}")
                    review_item = known_by_id[rid]
                    if review_item.get("status") != "pending":
                        continue
                    if review_item.get("kind") in ("key_result", "prediction_mismatch"):
                        obs_id = review_item.get("observation")
                        matched = []
                        for item in payload.get("interpretations") or []:
                            item_obs = resolve_observation(item)
                            if not item_obs or item_obs.get("id") != obs_id:
                                continue
                            if review_item.get("hypothesis_ids") \
                                    and item.get("hypothesis") not in review_item["hypothesis_ids"]:
                                continue
                            matched.append(item)
                        if not matched:
                            raise protocol.DecisionError(
                                f"待审查结果 {rid} 需要至少一条引用其 observation "
                                "及相关 hypothesis 的解释")
                        for item in matched:
                            h = (self.state.get("hypotheses") or {}).get(
                                item.get("hypothesis"))
                            if h and int(item.get("hypothesis_version") or 0) != int(
                                    h.get("version") or 0):
                                raise protocol.DecisionError(
                                    f"待审查结果 {rid} 的解释版本 "
                                    f"{item.get('hypothesis_version')} 与假设 "
                                    f"{item.get('hypothesis')} 当前版本 "
                                    f"{h.get('version')} 不一致")
                        if not payload.get("direction"):
                            raise protocol.DecisionError(
                                f"待审查结果 {rid} 需要给出后续 direction 决定")
                for item in payload.get("interpretations") or []:
                    obs = resolve_observation(item)
                    if not obs:
                        raise protocol.DecisionError(
                            f"research observation 不存在：{item.get('observation')}")
                    h = (self.state.get("hypotheses") or {}).get(
                        item.get("hypothesis"))
                    if not h:
                        raise protocol.DecisionError(
                            f"research hypothesis 不存在：{item.get('hypothesis')}")
                    relation = item.get("relation")
                    if obs.get("status") != "current" and relation != "inconclusive":
                        raise protocol.DecisionError(
                            f"observation {obs.get('id')} 已失效，不能再用 {relation} "
                            "解释；如需收尾请用 inconclusive 或审查 "
                            "evidence_invalidated 事件")
                    if int(item.get("hypothesis_version") or 0) != int(
                            h.get("version") or 0) and relation != "inconclusive":
                        raise protocol.DecisionError(
                            f"假设 {h.get('id')} 当前版本是 {h.get('version')}，"
                            f"解释引用的是 {item.get('hypothesis_version')}")
                for update in payload.get("hypothesis_updates") or []:
                    hid = update.get("id")
                    if hid in self.state["hypotheses"]:
                        version = self.state["hypotheses"][hid].get("version", 1)
                        if update.get("version") is not None \
                                and update["version"] != version:
                            raise protocol.DecisionError(
                                f"假设 {hid} 的当前版本是 {version}，"
                                f"提交版本是 {update['version']}")
        for ev in decision.get("evidence") or []:
            if ev not in self.state["evidence"]:
                raise protocol.DecisionError(f"决策引用了不存在的证据 ID：{ev}")
        for item in decision.get("refuted") or []:
            for ev in item.get("evidence") or []:
                if ev not in self.state["evidence"]:
                    raise protocol.DecisionError(f"refuted 引用了不存在的证据 ID：{ev}")
        for req in decision.get("context_requests") or []:
            if "artifact" in req:
                self._find_artifact(req["artifact"])
            elif "evidence" in req and req["evidence"] not in self.state["evidence"]:
                raise protocol.DecisionError(f"context_requests 引用了不存在的证据：{req['evidence']}")
            elif "research_page" in req:
                if not self._research_enabled():
                    raise protocol.DecisionError("research_page 只在 research_mode 下可用")
                try:
                    if int(req["research_page"]) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise protocol.DecisionError(
                        "context_requests.research_page 必须是非负整数")
        action = decision["action"]
        if action == "dispatch":
            ids = [t["id"] for t in decision["tasks"]]
            if len(ids) != len(set(ids)):
                raise protocol.DecisionError("dispatch 的 task id 重复")
            for tid in ids:
                if tid in tasks:
                    raise protocol.DecisionError(f"任务 {tid} 已存在；修改它请用 replan，修复请用 repair")
        elif action == "verify":
            target = tasks.get(decision["target_task"])
            if not target:
                raise protocol.DecisionError("verify 的目标任务不存在")
            if not target.get("artifact"):
                raise protocol.DecisionError("verify 的目标任务还没有产物")
            for t in decision["tasks"]:
                if t["id"] in tasks:
                    raise protocol.DecisionError(f"验证任务 id 已存在：{t['id']}")
        elif action == "repair":
            target = tasks.get(decision["target_task"])
            if not target:
                raise protocol.DecisionError("repair 的目标任务不存在")
            if target.get("status") in ("running", "completed", "cancelled", "pending"):
                raise protocol.DecisionError(
                    f"任务 {target['id']} 当前状态 {target.get('status')} 不能 repair")
            max_repairs = min(
                int(self.cfg.get("loop", {}).get("max_repairs_per_task", 3)),
                int(self.state["budget"]["limits"].get("max_repairs_per_task", 3)))
            if int(target.get("repairs", 0)) >= max_repairs:
                raise protocol.DecisionError(f"任务 {target['id']} 已达到最大修复次数 {max_repairs}")
            if decision.get("acceptance") is not None:
                old = acceptance.criteria_signature(target.get("acceptance") or [])
                new = acceptance.criteria_signature(decision["acceptance"])
                if old != new and not decision.get("reason"):
                    raise protocol.DecisionError("修改验收标准必须给出 reason")
        elif action == "replan":
            allowed_cancel = {"pending", "blocked", "stale", "budget_blocked", "unverified",
                              "review_unavailable", "review_invalid", "exec_failed",
                              "interrupted", "unknown", "patch_conflict", "unreviewed",
                              "cancelled", "needs_review"}
            for tid in decision.get("cancel") or []:
                if tid not in tasks:
                    raise protocol.DecisionError(f"cancel 引用了不存在的任务：{tid}")
                if tasks[tid].get("status") not in allowed_cancel:
                    raise protocol.DecisionError(
                        f"只能取消未完成或已失败的任务；{tid} 当前是 {tasks[tid].get('status')}")
            ids = [t["id"] for t in decision["tasks"]]
            if len(ids) != len(set(ids)):
                raise protocol.DecisionError("replan 的 task id 重复")
            for t in decision["tasks"]:
                if t["id"] in tasks and tasks[t["id"]].get("status") == "running":
                    raise protocol.DecisionError(f"任务 {t['id']} 正在运行，不能修改定义")
        elif action == "complete":
            if self._research_enabled() and decision.get("research"):
                # Validate against the state after this same decision's research
                # updates, on a throwaway copy.
                saved = self.state
                self.state = copy.deepcopy(saved)
                try:
                    self._apply_planner_notes(decision)
                    self._apply_research_notes(
                        decision, int(saved.get("round", 0)) + 1)
                    self._prepare_research_view()
                    errors = self._completion_gate_errors(decision)
                finally:
                    self.state = saved
            else:
                errors = self._completion_gate_errors(decision)
            decision["_completion_errors"] = errors

    def _completion_gate_errors(self, decision: dict) -> list[str]:
        ok, errors = acceptance.validate_completion(
            self.state, decision, self.store.dir)
        errors = list(errors or [])
        if self._research_enabled():
            pending = researchmod.pending(self.state)
            if pending:
                shown = "、".join(
                    f"{p['id']}({p.get('kind')})" for p in pending[:8])
                more = f" 等 {len(pending)} 项" if len(pending) > 8 else ""
                errors.append(
                    f"仍有未审查的研究结果：{shown}{more}；请先用 review 处理，"
                    "complete 不能跳过关键结果审查")
        return errors

    def apply_decision(self, decision: dict, *, external: bool = True) -> dict:
        """Apply a normalized decision, then execute until the next decision point."""
        self.recover()
        self._prepare_research_view()
        if self.state.get("status") == "cancelled":
            return {"status": "cancelled", "packet": None}
        if self.state.get("recovery"):
            self.state.setdefault("recovery_history", []).extend(self.state["recovery"])
            self.state["recovery"] = []
        self.validate_decision(decision)
        usage_sink = []
        if external:
            self._record_external_decision(
                self.roles["planner"].model, usage_sink)
        round_no = int(self.state.get("round", 0)) + 1
        max_rounds = int(self.state["budget"]["limits"].get("max_workflow_rounds", 30))
        if round_no > max_rounds:
            self.state["status"] = "paused"
            self.state["stop_reason"] = f"budget:max_workflow_rounds({max_rounds})"
            self.state["triggers"] = [{"kind": "budget_exhausted",
                                       "detail": self.state["stop_reason"]}]
            self.state.setdefault("usage_calls", []).extend(usage_sink)
            self._sync_budget()
            self.commit("stopped", {}, summary=self.state["stop_reason"])
            return {"status": "paused", "packet": None}
        self.state["round"] = round_no
        self.store.decision_path(round_no).parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write_json(self.store.decision_path(round_no), {
            "round": round_no, "at": util.iso(), "external": external,
            "decision": decision,
        })
        self.state.setdefault("decisions", []).append({
            "round": round_no, "action": decision["action"],
            "reason": decision["reason"], "target_task": decision.get("target_task"),
            "path": str(self.store.decision_path(round_no).relative_to(self.store.dir)),
            "external": external,
        })
        # Structured requests for more context defer the action.
        if decision.get("context_requests"):
            self._fulfill_context_requests(decision["context_requests"])
            self.state["decisions"][-1]["deferred_for_context"] = True
            self.state["status"] = "awaiting_decision"
            self.state["triggers"] = [{"kind": "context_requested",
                                       "detail": "已按请求附上上下文，请重新决策"}]
            self.state.setdefault("usage_calls", []).extend(usage_sink)
            self._sync_budget()
            self.commit("context_requested", {"requests": decision["context_requests"]},
                        summary="context requested")
            self._build_and_store_packet()
            return {"status": self.state["status"], "packet": self.state.get("pending_packet")}
        # Repeated identical decisions without progress are stopped, not looped.
        fp = protocol.decision_fingerprint(
            decision, ignore_reason=self._research_enabled())
        progress = self._progress_fp()
        prev = self.state.setdefault("progress", {})
        if (prev.get("last_progress_fp") is not None
                and progress == prev.get("last_progress_fp")
                and fp == prev.get("last_decision_fp")):
            prev["stalled_rounds"] = int(prev.get("stalled_rounds", 0)) + 1
        else:
            prev["stalled_rounds"] = 0
        prev["last_decision_fp"] = fp
        if int(prev.get("stalled_rounds", 0)) >= int(
                self.cfg.get("loop", {}).get("no_progress_limit", 3)):
            self.state["status"] = "paused"
            self.state["stop_reason"] = "no_progress: 同一决策重复且没有新增证据"
            self.state["triggers"] = [{"kind": "no_progress",
                                       "detail": self.state["stop_reason"]}]
            self.state.setdefault("usage_calls", []).extend(usage_sink)
            self._sync_budget()
            self.commit("no_progress", {}, summary=self.state["stop_reason"])
            return {"status": "paused", "packet": None}
        action = decision["action"]
        research_payload = (decision.get("research") or {}) \
            if self._research_enabled() else {}
        # Research judgments and completion are one transaction: a decision
        # that both updates research and completes must use the final state,
        # and a rejected decision must not leave either half applied.
        transactional = bool(research_payload) or action == "complete"
        original_state = self.state
        if transactional:
            self.state = copy.deepcopy(self.state)
        try:
            self._apply_planner_notes(decision)
            if research_payload:
                self._apply_research_notes(decision, round_no)
                # Propagate hypothesis version/validity updates to dependent
                # tasks and pending events before completion is judged.
                self._prepare_research_view()
            self.state["completion_rejection"] = None
            errors = []
            if action == "complete":
                # validate_decision just computed this against the final state,
                # including a research preview; reuse it instead of re-hashing
                # the same dependency tree.
                errors = list(decision.get("_completion_errors") or [])
            if errors:
                if transactional:
                    self.state = original_state
                self.state["completion_rejection"] = {
                    "at": util.iso(), "errors": errors,
                    "decision_round": round_no,
                }
                self.state["completion"] = None
                self.state["status"] = "awaiting_decision"
                self.state["triggers"] = [{"kind": "completion_rejected",
                                           "detail": "；".join(errors)}]
            elif action == "dispatch":
                self._add_tasks(decision["tasks"])
            elif action == "verify":
                self._add_tasks(decision["tasks"])
            elif action == "repair":
                self._repair_task(decision)
            elif action == "replan":
                self._replan(decision)
            elif action == "review":
                self.state["status"] = "awaiting_decision"
                self.state["triggers"] = [{"kind": "research_reviewed",
                                           "detail": "研究判断已记录，等待下一步决策"}]
            elif action == "complete":
                self.state["completion"] = {
                    "entries": decision["completion"], "validated": True,
                    "at": util.iso(), "summary": decision.get("summary", ""),
                }
                self.state["status"] = "complete"
                self.state["stop_reason"] = "goal_complete"
                self.state["triggers"] = []
                self.state.setdefault("facts", []).append({
                    "id": f"fact-{len(self.state.get('facts') or []) + 1}",
                    "text": "目标完成条件已逐项通过验证：" + "、".join(
                        e["criterion"] for e in decision["completion"]),
                    "evidence": [ev for e in decision["completion"] for ev in e["evidence"]],
                    "verified": True, "at": util.iso(),
                })
            elif action == "request_input":
                self.state["status"] = "needs_input"
                self.state["question"] = decision["question"]
                self.state["stop_reason"] = "needs_input"
            elif action == "pause":
                self.state["status"] = "paused"
                self.state["stop_reason"] = decision.get("reason")
        except Exception:
            if transactional:
                self.state = original_state
            raise
        self.state.setdefault("usage_calls", []).extend(usage_sink)
        self._sync_budget()
        self.state["progress"]["last_progress_fp"] = self._progress_fp()
        self.commit("decision_applied", {"round": round_no, "action": action},
                    summary=f"{round_no}: {action}")
        if self.state["status"] == "awaiting_decision" and action == "complete":
            self._build_and_store_packet()
        elif action == "review":
            self._build_and_store_packet()
            return {"status": self.state.get("status"),
                    "packet": self.state.get("pending_packet")}
        elif self.state["status"] in ("complete", "paused", "needs_input", "cancelled"):
            return {"status": self.state["status"], "packet": None}
        else:
            self.state["status"] = "executing"
            self.commit("executing", {}, summary="executing")
            self.run_until_decision(recover=False)
        return {"status": self.state.get("status"),
                "packet": self.state.get("pending_packet")}

    def _apply_research_notes(self, decision: dict, round_no: int = 0) -> None:
        payload = decision.get("research")
        if not payload or not self._research_enabled():
            return
        try:
            researchmod.apply_review(
                self.state, payload,
                round_no or int(decision.get("round", 0)) or 0)
        except ValueError as exc:
            raise protocol.DecisionError(str(exc)) from exc

    def _apply_planner_notes(self, decision: dict) -> None:
        for item in decision.get("hypotheses") or []:
            hid = item.get("id") or f"h-{len(self.state.setdefault('hypotheses', {})) + 1}"
            if hid not in self.state["hypotheses"]:
                self.state["hypotheses"][hid] = {
                    "id": hid, "text": item["text"], "status": "pending",
                    "created_round": decision.get("round"), "evidence": [],
                }
        for item in decision.get("refuted") or []:
            hid = item.get("id")
            text = item.get("text") or hid or ""
            if hid and hid in self.state.setdefault("hypotheses", {}):
                self.state["hypotheses"][hid]["status"] = "refuted"
                self.state["hypotheses"][hid]["reason"] = item["reason"]
            self.state.setdefault("refuted", []).append({
                "id": hid, "text": text, "reason": item["reason"],
                "evidence": item.get("evidence") or [], "at": util.iso(),
            })
        for question in decision.get("open_questions") or []:
            if question not in self.state.setdefault("open_questions", []):
                self.state["open_questions"].append(question)
        for suggestion in decision.get("recommendations") or []:
            if suggestion not in self.state.setdefault("recommendations", []):
                self.state["recommendations"].append(suggestion)
        if decision.get("next_acceptance"):
            self.state["next_acceptance"] = decision["next_acceptance"]

    def _add_tasks(self, new_tasks: list[dict]) -> None:
        tasks = copy.deepcopy(self.state["tasks"])
        for task in new_tasks:
            if task["id"] in tasks:
                raise protocol.DecisionError(f"任务 {task['id']} 已存在")
            tasks[task["id"]] = copy.deepcopy(task)
        self._resolve_task_hypotheses(tasks)
        limit = int(self.cfg.get("loop", {}).get("max_tasks", 100))
        if len(tasks) > limit:
            raise protocol.DecisionError(f"任务图超过上限 {limit}；请拆分或调整 max_tasks")
        self._resolve_task_deps(tasks)
        self.state["tasks"] = tasks

    def _resolve_task_hypotheses(self, tasks: dict) -> None:
        known = set((self.state.get("hypotheses") or {}).keys())
        for task in tasks.values():
            for field in ("tests_hypotheses", "depends_on_hypotheses"):
                for hid in task.get(field) or []:
                    if hid not in known:
                        raise protocol.DecisionError(
                            f"任务 {task['id']} 的 {field} 引用了不存在的假设 {hid}")
            prediction = task.get("research_prediction") or {}
            hid = prediction.get("hypothesis")
            if hid and hid not in known:
                raise protocol.DecisionError(
                    f"任务 {task['id']} 的 research_prediction 引用了不存在的假设 {hid}")

    def _resolve_task_deps(self, tasks: dict) -> None:
        for task in tasks.values():
            deps = task.get("depends_on") or []
            new_deps = []
            for pattern in deps:
                if any(ch in pattern for ch in "*?["):
                    new_deps.extend(protocol.resolve_dep_patterns(tasks, [pattern], task["id"]))
                else:
                    if pattern == task["id"]:
                        raise protocol.DecisionError(f"任务 {task['id']} 不能依赖自己")
                    if pattern not in tasks:
                        raise protocol.DecisionError(
                            f"任务 {task['id']} 依赖了不存在的任务 {pattern}")
                    new_deps.append(pattern)
            task["depends_on"] = list(dict.fromkeys(new_deps))
        protocol.validate_graph(tasks, self.state["goal"]["criteria"])

    def _repair_task(self, decision: dict) -> None:
        tid = decision["target_task"]
        task = self.state["tasks"][tid]
        old_version = task.get("version", 1)
        old_acceptance = copy.deepcopy(task.get("acceptance") or [])
        old_checks = copy.deepcopy(task.get("checks") or [])
        new_acceptance = decision.get("acceptance") if decision.get("acceptance") is not None else old_acceptance
        new_checks = decision.get("checks") if decision.get("checks") is not None else old_checks
        new_policy = decision.get("acceptance_policy") or task.get("acceptance_policy")
        if decision.get("acceptance") is not None:
            self.state.setdefault("acceptance_history", []).append({
                "task": tid, "version": old_version, "acceptance": old_acceptance,
                "criteria_signature": acceptance.criteria_signature(old_acceptance),
                "changed_at": util.iso(), "reason": decision.get("reason"),
            })
            for ev in self.state["evidence"].values():
                if ev.get("task") == tid:
                    ev["verified"] = False
                    ev["superseded"] = True
                    ev["superseded_reason"] = "主持者修改了验收标准，旧验收失效"
                    if self._research_enabled():
                        self._invalidate_research_evidence(
                            ev["id"], ev["superseded_reason"])
        task["previous"] = {
            "artifact": copy.deepcopy(task.get("artifact")),
            "review": copy.deepcopy(task.get("review")),
            "checks": copy.deepcopy((task.get("acceptance_result") or {}).get("checks") or []),
            "changes": copy.deepcopy(task.get("changes")),
            "status": task.get("status"),
            "version": old_version,
        }
        if task.get("kind") == "code":
            old_changes = task.get("changes") or {}
            if task.get("status") in ("unknown", "interrupted"):
                # A timed-out worker may have left a diff, but its side effects are
                # not trustworthy. The planner sees this explicit state and can
                # choose replan/check instead of silently treating it as a clean v2.
                inheritance = ("uncertain_patch" if old_changes.get("patch_path")
                               else "fresh_after_uncertain")
            elif old_changes.get("patch_path"):
                inheritance = "inherit_patch"
            else:
                inheritance = "fresh_no_patch"
            task["repair_inheritance"] = inheritance
            task["previous"]["inheritance"] = inheritance
            task["previous"]["patch_path"] = old_changes.get("patch_path")
        for ev in self.state["evidence"].values():
            if ev.get("task") == tid and not ev.get("superseded"):
                ev["verified"] = False
                ev["superseded"] = True
                ev["superseded_reason"] = f"任务进入 v{old_version + 1} 修复版本，旧验收不再有效"
                if self._research_enabled():
                    self._invalidate_research_evidence(
                        ev["id"], ev["superseded_reason"])
        task["version"] = old_version + 1
        task["prompt"] = (task.get("prompt", "").rstrip()
                          + "\n\n## 修复指示（来自主持者）\n" + decision["instructions"])
        task["acceptance"] = new_acceptance
        task["checks"] = new_checks
        task["acceptance_policy"] = new_policy
        task["repairs"] = int(task.get("repairs", 0)) + 1
        task["status"] = "pending"
        task["budget_blocked"] = False
        task["definition_hash"] = protocol.definition_hash(task)
        task["acceptance_result"] = None
        task["review"] = None
        task["last_error"] = None
        task["stale"] = False
        task["stale_reason"] = None
        protocol.validate_graph(self.state["tasks"], self.state["goal"]["criteria"])

    def _replan(self, decision: dict) -> None:
        tasks = copy.deepcopy(self.state["tasks"])
        changed_ids = set()
        for tid in decision.get("cancel") or []:
            tasks[tid]["status"] = "cancelled"
            tasks[tid]["last_error"] = decision.get("reason")
            changed_ids.add(tid)
        for new_task in decision.get("tasks") or []:
            tid = new_task["id"]
            if tid in tasks:
                old = tasks[tid]
                if protocol.definition_hash(old) == new_task.get("definition_hash"):
                    # Same definition: only dependencies may be updated.
                    old["depends_on"] = new_task.get("depends_on") or old.get("depends_on")
                    continue
                old_version = old.get("version", 1)
                if acceptance.criteria_signature(old.get("acceptance") or []) != \
                        acceptance.criteria_signature(new_task.get("acceptance") or []):
                    for ev in self.state["evidence"].values():
                        if ev.get("task") == tid:
                            ev["verified"] = False
                            ev["superseded"] = True
                            ev["superseded_reason"] = "replan 修改了验收标准"
                            if self._research_enabled():
                                self._invalidate_research_evidence(
                                    ev["id"], ev["superseded_reason"])
                    self.state.setdefault("acceptance_history", []).append({
                        "task": tid, "version": old_version,
                        "acceptance": old.get("acceptance") or [],
                        "changed_at": util.iso(), "reason": decision.get("reason"),
                    })
                new_task = copy.deepcopy(new_task)
                new_task["version"] = old_version + 1
                new_task["repairs"] = old.get("repairs", 0)
                new_task["attempts"] = old.get("attempts", 0)
                new_task["artifact"] = None
                new_task["result_version"] = 0
                new_task["previous"] = {
                    "artifact": old.get("artifact"), "review": old.get("review"),
                    "checks": (old.get("acceptance_result") or {}).get("checks") or [],
                    "version": old_version,
                }
                new_task.setdefault("artifact_history", old.get("artifact_history") or [])
                tasks[tid] = new_task
                changed_ids.add(tid)
            else:
                tasks[tid] = copy.deepcopy(new_task)
                changed_ids.add(tid)
        self._resolve_task_deps(tasks)
        self._resolve_task_hypotheses(tasks)
        limit = int(self.cfg.get("loop", {}).get("max_tasks", 100))
        if len(tasks) > limit:
            raise protocol.DecisionError(f"任务图超过上限 {limit}；请拆分或调整 max_tasks")
        self.state["tasks"] = tasks
        for ev in self.state["evidence"].values():
            if ev.get("task") in changed_ids and not ev.get("superseded"):
                ev["verified"] = False
                ev["superseded"] = True
                ev["superseded_reason"] = "replan 改变了任务定义或取消了任务"
                if self._research_enabled():
                    self._invalidate_research_evidence(
                        ev["id"], ev["superseded_reason"])
        # Anything that consumed a changed/cancelled task becomes stale.
        for task in self.state["tasks"].values():
            if task.get("status") in ("completed", "unverified", "unreviewed",
                                      "review_unavailable", "review_invalid"):
                if any(dep in changed_ids for dep in (task.get("dep_versions") or {})):
                    task["status"] = "stale"
                    task["stale"] = True
                    task["stale_reason"] = "上游任务在 replan 中发生了变化"
                    for ev in self.state["evidence"].values():
                        if ev.get("task") == task["id"]:
                            ev["verified"] = False
                            ev["stale"] = True
                            if self._research_enabled():
                                self._invalidate_research_evidence(
                                    ev["id"], task.get("stale_reason") or "任务失效")

    def _find_artifact(self, ref: str) -> tuple[dict, dict, Path]:
        m = re.fullmatch(r"([A-Za-z0-9_-]{1,64})@v(\d+)", ref or "")
        if not m:
            raise protocol.DecisionError(f"产物引用格式应为 task-id@vN：{ref!r}")
        tid, version = m.group(1), int(m.group(2))
        task = self.state["tasks"].get(tid)
        if not task:
            raise protocol.DecisionError(f"产物引用指向不存在的任务：{ref}")
        for art in [task.get("artifact")] + list(task.get("artifact_history") or []):
            if art and art.get("version") == version:
                path = self.store.dir / art["path"]
                if not path.exists():
                    raise protocol.DecisionError(f"产物文件不存在：{path}")
                return task, art, path
        raise protocol.DecisionError(f"任务 {tid} 没有 v{version} 产物")

    def _fulfill_context_requests(self, requests: list[dict]) -> list[dict]:
        notes = []
        existing = list(self.state.get("context_notes") or [])
        for req in requests:
            offset = int(req.get("offset", 0) or 0)
            limit = min(int(req.get("limit", 6000) or 6000), 20000)
            if "research_page" in req:
                page = int(req["research_page"])
                limit = min(limit, 8000)
                section = researchmod.packet_section(
                    self.state, max_chars=limit, offset=page)
                notes.append({
                    "request": {"research_page": page, "limit": limit},
                    "chars": len(util.canonical(section)),
                    "total_chars": None,
                    "offset": page,
                    "limit": limit,
                    "next_offset": section.get("next_offset"),
                    "has_more": section.get("next_offset") is not None,
                    "sha256": None,
                    "content": section,
                })
                existing = [n for n in existing
                            if "research_page" not in (n.get("request") or {})]
                continue
            if "artifact" in req:
                key = req["artifact"]
                task, art, path = self._find_artifact(key)
                full = path.read_text(encoding="utf-8")
                request = {"artifact": key, "offset": offset, "limit": limit}
                sha = art.get("sha256")
            else:
                key = req["evidence"]
                ev = self.state["evidence"][key]
                full = ev.get("output_tail") or ev.get("summary") or ""
                request = {"evidence": key, "offset": offset, "limit": limit}
                sha = None
            chunk = full[offset:offset + limit]
            next_offset = offset + len(chunk) if offset + len(chunk) < len(full) else None
            notes.append({
                "request": request, "chars": len(chunk), "total_chars": len(full),
                "offset": offset, "limit": limit, "next_offset": next_offset,
                "has_more": next_offset is not None, "sha256": sha,
                "content": chunk,
            })
            # A re-request replaces the old copy, so a packet that had to trim
            # the content can still be followed by a full/next page fetch.
            existing = [n for n in existing
                        if (n.get("request") or {}).get("artifact") != key
                        and (n.get("request") or {}).get("evidence") != key]
        self.state["context_notes"] = (existing + notes)[-6:]
        return notes

    # ------------------------------------------------------------ auto planner
    def run_auto(self, max_steps: int | None = None) -> dict:
        self.recover()
        if (self.state.get("status") == "paused"
                and str(self.state.get("stop_reason") or "").startswith("max_steps")):
            self.state["status"] = "awaiting_decision"
            self.state["stop_reason"] = None
            self.commit("resumed", {}, summary="resume after max_steps pause")
        limit = int(max_steps or self.cfg.get("loop", {}).get("max_steps", 50))
        for _ in range(max(1, limit)):
            if self.state.get("status") in ("complete", "paused", "needs_input", "cancelled"):
                break
            if self._should_cancel():
                self._cancel_state("取消请求")
                self.commit("cancelled", {}, summary="cancelled")
                break
            rounds = int(self.state.get("round", 0))
            max_rounds = int(self.state["budget"]["limits"].get("max_workflow_rounds", 30))
            if rounds >= max_rounds:
                self.state["status"] = "paused"
                self.state["stop_reason"] = f"budget:max_workflow_rounds({max_rounds})"
                self.commit("stopped", {}, summary=self.state["stop_reason"])
                break
            if self.state.get("status") == "executing":
                self.run_until_decision()
                continue
            packet = self.current_packet()
            if packet is None:
                packet = self._build_and_store_packet()
            if not packet:
                break
            decision, error = self._planner_decide(packet)
            if decision is None:
                self.state["status"] = "paused"
                self.state["stop_reason"] = error or "planner_invalid"
                self.state["triggers"] = [{"kind": "planner_error", "detail": self.state["stop_reason"]}]
                self.commit("planner_error", {"error": error}, summary=(error or "")[:300])
                break
            try:
                self.apply_decision(decision, external=False)
            except protocol.DecisionError as exc:
                self.state = self.store.load()
                self._committed_state = copy.deepcopy(self.state)
                self.state["status"] = "paused"
                self.state["stop_reason"] = f"planner_decision_rejected: {exc}"
                self.state["triggers"] = [{"kind": "planner_error",
                                           "detail": self.state["stop_reason"]}]
                self.commit("planner_error", {"error": str(exc)},
                            summary=self.state["stop_reason"][:300])
                break
        if self.state.get("status") == "awaiting_decision":
            self.state["status"] = "paused"
            self.state["stop_reason"] = f"max_steps({limit}) 用尽，需要人工检查或继续运行"
            self.state["triggers"] = [{"kind": "max_steps", "detail": self.state["stop_reason"]}]
            self.commit("stopped", {}, summary=self.state["stop_reason"])
        return self.status_summary()

    def _planner_decide(self, packet: dict) -> tuple[dict | None, str | None]:
        feedback = ""
        attempts = int(self.cfg.get("loop", {}).get("planner_retries", 1)) + 1
        for attempt in range(attempts):
            prompt = prompts.planner_prompt(packet, feedback)
            usage: list = []
            res = self._call_model(
                "planner", prompt, cwd=self.store.dir, tools=self.roles["planner"].tools,
                add_dirs=[str(self.store.dir)] +
                         ([self.state["workspace_config"]["repo"]]
                          if self.state["workspace_config"].get("repo") else []),
                purpose="planner" if attempt == 0 else "planner_retry", usage_sink=usage)
            self.state.setdefault("usage_calls", []).extend(usage)
            self._sync_budget()
            if not res.get("ok"):
                kind = res.get("error_kind")
                if res.get("budget_blocked"):
                    return None, res.get("error") or "budget"
                if kind in ("auth", "config", "identity"):
                    return None, f"planner_unavailable:{kind}:{res.get('error')}"
                if attempt + 1 < attempts:
                    feedback = f"上一次调用失败：{res.get('error')}"
                    continue
                return None, f"planner_unavailable:{res.get('error')}"
            if not (res.get("text") or "").strip():
                feedback = "planner 返回了空输出；必须只输出一个 JSON 决策对象。"
                continue
            candidate = None
            for obj in util.json_objects(res.get("text", "")):
                if isinstance(obj, dict) and "action" in obj:
                    candidate = obj
                    break
            if candidate is None:
                feedback = "没有找到包含 action 的 JSON 对象。"
                continue
            try:
                normalized = protocol.normalize_decision(
                    candidate, self.state["goal"]["criteria"],
                    int(self.state.get("round", 0)) + 1, set(self.state["tasks"]))
                self.validate_decision(normalized)
                return normalized, None
            except protocol.DecisionError as exc:
                feedback = str(exc)
                continue
        return None, f"planner_invalid:{feedback}"

    # ------------------------------------------------------------ status
    def status_summary(self) -> dict:
        tasks = self.state.get("tasks") or {}
        counts = {}
        for task in tasks.values():
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        packet = self.current_packet()
        return {
            "run_id": self.state.get("run_id"),
            "status": self.state.get("status"),
            "mode": self.state.get("mode"),
            "round": self.state.get("round", 0),
            "goal": (self.state.get("goal") or {}).get("text"),
            "completion_criteria": (self.state.get("goal") or {}).get("criteria"),
            "task_counts": counts,
            "budget": self.state.get("budget"),
            "packet": self.state.get("pending_packet"),
            "packet_round": (packet or {}).get("round"),
            "triggers": self.state.get("triggers"),
            "stop_reason": self.state.get("stop_reason"),
            "question": self.state.get("question"),
            "completion": self.state.get("completion"),
            "recovery": self.state.get("recovery"),
            "workspace": self.state.get("workspace"),
            "external_unmetered": (self.state.get("budget") or {}).get("external_unmetered", 0),
            "research": ({
                "enabled": True,
                "pending_reviews": len(researchmod.pending(self.state)),
                "hypotheses": {
                    h.get("id"): h.get("status")
                    for h in (self.state.get("hypotheses") or {}).values()
                    if not h.get("legacy")
                },
            } if self._research_enabled() else {"enabled": False}),
        }

    def artifacts_list(self) -> list[dict]:
        out = []
        for task in (self.state.get("tasks") or {}).values():
            history = list(task.get("artifact_history") or [])
            current = task.get("artifact")
            if current and not any(a.get("version") == current.get("version") for a in history):
                history.append(current)
            for art in history:
                if not art:
                    continue
                out.append({"task": task["id"], "version": art.get("version"),
                            "path": art.get("path"), "sha256": art.get("sha256"),
                            "chars": art.get("chars"), "status": task.get("status"),
                            "verified": task.get("status") == "completed",
                            "patch": art.get("patch_path") or (task.get("changes") or {}).get("patch_path")})
        return out
