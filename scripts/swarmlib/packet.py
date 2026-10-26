"""Deterministic decision packet construction. No model calls."""
from __future__ import annotations

import copy
from pathlib import Path

from . import util
from . import research as researchmod

BAD_STATUSES = ("unverified", "exec_failed", "review_unavailable", "review_invalid",
                "interrupted", "unknown", "blocked", "stale", "patch_conflict",
                "budget_blocked", "needs_review")


def _task_counts(tasks: dict) -> dict:
    counts = {}
    for task in tasks.values():
        counts[task["status"]] = counts.get(task["status"], 0) + 1
    return counts


def _artifact_ref(task: dict) -> str:
    return f"{task['id']}@v{task.get('result_version', 0)}"


def _goal_criteria(state: dict) -> list[dict]:
    tasks = state.get("tasks") or {}
    completion = {e["criterion"]: e for e in (state.get("completion") or {}).get("entries", [])}
    out = []
    for crit in state["goal"]["criteria"]:
        linked = [t for t in tasks.values()
                  if crit["id"] in (t.get("satisfies") or []) and t.get("artifact")]
        completed = [t for t in linked if t.get("status") == "completed"]
        entry = completion.get(crit["id"])
        out.append({
            "id": crit["id"], "text": crit["text"],
            "status": ("met" if entry and entry.get("met") else
                       "evidence_available" if completed else "unknown"),
            "evidence": [e for t in completed for e in (t.get("evidence_ids") or [])],
            "evidence_note": ("由完成条件验证记录给出" if entry else
                              ("有已完成任务，但尚未通过 complete 决策逐项验收" if completed
                               else "还没有可用于该完成条件的已验收任务")),
        })
    return out


def _new_evidence(state: dict) -> list[dict]:
    watermark = int(state.get("packet_watermark") or 0)
    evidence = state.get("evidence") or {}
    items = [e for e in evidence.values() if int(e.get("seq", 0)) > watermark]
    items.sort(key=lambda e: e.get("seq", 0))
    return [{
        "id": e["id"], "kind": e.get("kind"), "task": e.get("task"),
        "summary": e.get("summary", ""), "verified": bool(e.get("verified")),
        "completion_usable": bool(e.get("completion_usable")),
        "artifact": e.get("artifact"), "sha256": e.get("sha256"),
        "goal_criteria": e.get("goal_criteria") or [],
    } for e in items[-40:]]


def _artifacts(state: dict, run_dir=None) -> list[dict]:
    tasks = state.get("tasks") or {}
    items = []
    for task in tasks.values():
        if not task.get("artifact"):
            continue
        art = task["artifact"]
        excerpt = ""
        if run_dir is not None:
            try:
                excerpt = (Path(run_dir) / art["path"]).read_text(encoding="utf-8")[:600]
            except OSError:
                excerpt = ""
        items.append({
            "ref": _artifact_ref(task), "task": task["id"],
            "version": task.get("result_version"), "sha256": art.get("sha256"),
            "chars": art.get("chars"), "path": art.get("path"),
            "patch": art.get("patch_path"),
            "verified": task.get("status") == "completed",
            "status": task.get("status"),
            "acceptance": {
                "policy": (task.get("acceptance_result") or {}).get("policy"),
                "passed": (task.get("acceptance_result") or {}).get("passed"),
                "review_outcome": (task.get("acceptance_result") or {}).get("review_outcome"),
            },
            "satisfies": task.get("satisfies") or [],
            "summary": util.sentence(task.get("question") or "", 180),
            "excerpt": excerpt,
            "request_hint": f'context_requests: [{{"artifact": "{_artifact_ref(task)}"}}]',
        })
    items.sort(key=lambda x: (not x["verified"], x["task"]))
    return items[:20]


def _unresolved(state: dict) -> list[dict]:
    tasks = state.get("tasks") or {}
    items = []
    for task in tasks.values():
        if task["status"] not in BAD_STATUSES:
            continue
        acceptance = task.get("acceptance_result") or {}
        review = task.get("review") or {}
        items.append({
            "task": task["id"], "status": task["status"],
            "error": util.sentence(task.get("last_error") or "", 500),
            "repeat_count": task.get("repeat_count", 0),
            "repair_inheritance": task.get("repair_inheritance"),
            "outside_scope": ((task.get("changes") or {}).get("outside_scope") or [])[:20],
            "artifact": _artifact_ref(task) if task.get("artifact") else None,
            "checks": [{"name": c["name"], "ok": c["ok"], "exit": c.get("exit"),
                        "output_tail": (c.get("output") or "")[-400:]}
                       for c in (acceptance.get("checks") or [])],
            "failed_criteria": [c for c in (acceptance.get("criteria") or [])
                                if not c.get("met")],
            "review_errors": (review.get("errors") or [])[:6],
            "review_outcome": acceptance.get("review_outcome"),
        })
    for question in state.get("open_questions") or []:
        items.append({"open_question": question})
    return items


def _disagreements(state: dict) -> list[dict]:
    out = []
    for task in (state.get("tasks") or {}).values():
        acc = task.get("acceptance_result") or {}
        if not acc.get("review_available"):
            continue
        checks_ok = acc.get("checks_ok")
        review_passed = acc.get("review_passed")
        if checks_ok is None or review_passed is None:
            continue
        if checks_ok != review_passed:
            out.append({
                "task": task["id"],
                "summary": ("程序检查通过但复核不通过" if checks_ok
                            else "复核通过但程序检查不通过"),
                "checks_ok": checks_ok, "review_passed": review_passed,
                "review_errors": (task.get("review") or {}).get("errors", [])[:4],
            })
    return out


def _budget(state: dict) -> dict:
    return copy.deepcopy(state.get("budget") or {})


def build_packet(state: dict, max_chars: int = 30000, run_dir=None) -> dict:
    tasks = state.get("tasks") or {}
    counts = _task_counts(tasks)
    rounds = state.get("round", 0)
    goal = state["goal"]
    packet = {
        "schema": 1,
        "run_id": state["run_id"],
        "round": rounds,
        "status": state.get("status"),
        "mode": state.get("mode", "external"),
        "goal": {
            "text": goal["text"],
            "constraints": goal.get("constraints") or [],
            "completion_criteria": _goal_criteria(state),
        },
        "state_summary": {
            "tasks": counts,
            "total_tasks": len(tasks),
            "completed": counts.get("completed", 0),
            "workflow_rounds": rounds,
            "next_hint": state.get("next_hint") or "",
        },
        "new_evidence": _new_evidence(state),
        "key_artifacts": _artifacts(state, run_dir),
        "unresolved": _unresolved(state),
        "disagreements": _disagreements(state),
        "critic_findings": (state.get("critic_findings") or [])[-5:],
        "hypotheses": [h for h in (state.get("hypotheses") or {}).values()
                       if h.get("status") == "pending"],
        "refuted": state.get("refuted") or [],
        "open_questions": state.get("open_questions") or [],
        "recommendations": state.get("recommendations") or [],
        "failures": [t for t in _unresolved(state) if t.get("repeat_count", 0) >= 2],
        "budget": _budget(state),
        "workspace": state.get("workspace") or {},
        "roles": state.get("roles_summary") or {},
        "decision_needed": {
            "triggers": state.get("triggers") or [],
            "allowed_actions": sorted(
                ["dispatch", "repair", "verify", "replan", "complete",
                 "request_input", "pause", "request_context"]
                + (["review"] if state.get("research_mode") else [])),
            "note": ("目标完成必须逐项引用当前已验证证据并由控制器校验。"
                     "简单问题可以只派一个任务；局部错误用 repair，方向错误用 replan。"
                     + (" 研究模式：review 只更新研究判断，不创建任务。"
                        if state.get("research_mode") else "")),
        },
        "available_context": [
            {"ref": a["ref"], "chars": a["chars"], "sha256": a["sha256"],
             "request_hint": a["request_hint"]} for a in _artifacts(state, run_dir)
        ],
        "context_notes": state.get("context_notes") or [],
        "recent_decisions": [{
            "round": d.get("round"), "action": d.get("action"),
            "reason": util.sentence(d.get("reason", ""), 300),
            "target": d.get("target_task"),
        } for d in (state.get("decisions") or [])[-5:]],
        "external_planner_unmetered": (state.get("budget") or {}).get("external_unmetered", 0),
    }
    if state.get("research_mode"):
        packet["research"] = researchmod.packet_section(
            state, int(min(8000, max(1200, max_chars // 4))))
    # Deterministic trimming: optional fields go first; unresolved evidence is kept.
    text = util.canonical(packet)
    if len(text) > max_chars:
        packet["recent_decisions"] = packet["recent_decisions"][-3:]
        packet["key_artifacts"] = packet["key_artifacts"][:12]
        packet["available_context"] = packet["available_context"][:12]
        if packet.get("context_notes"):
            # Explicitly requested evidence outranks cosmetic trimming. Keeping it
            # may put the packet over the soft display budget; the planner asked
            # for it and can page it with offset/limit.
            over = len(util.canonical(packet)) > max_chars
            packet["packet_meta"] = {
                "truncated": True,
                "context_over_budget": over,
                "note": ("已裁剪旧细节；明确请求的 context 保留。"
                         if over else "已裁剪旧细节。"),
            }
        else:
            packet["packet_meta"] = {"truncated": True,
                                     "note": "已按结构裁剪旧细节；未解决问题、反证和证据 id 未删除。"}
    else:
        packet["packet_meta"] = {"truncated": False}
    packet["packet_meta"].update({
        "generated_at": util.iso(),
        "est_tokens": util.estimate_tokens(util.canonical(packet)),
        "token_estimate_method": "heuristic: CJK 1 token/char, other ~4 chars/token; not provider usage",
        "chars": len(util.canonical(packet)),
        "watermark": state.get("evidence_seq", 0),
    })
    return packet
