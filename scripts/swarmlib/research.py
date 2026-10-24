"""Minimal research-mode bookkeeping.

This module deliberately does not decide whether a hypothesis is true. It keeps
observations, planner interpretations and direction decisions separate, and only
propagates validity when the underlying evidence changes.
"""
from __future__ import annotations

import copy

from . import util

SCHEMA = 1
RELATIONS = {"supports", "against", "inconclusive", "mixed"}
DECISIONS = {"continue", "experiment", "narrow", "switch", "pause", "stop"}
REVIEW_DIRECTIONS = {"continue", "narrow", "switch", "pause", "stop"}
HYPOTHESIS_STATUSES = {
    "open", "supported", "contested", "contradicted", "inconclusive",
    "superseded", "legacy", "needs_review",
}
TASK_STATUS_NEEDS_REVIEW = "needs_review"


def _new_research() -> dict:
    return {
        "schema": SCHEMA,
        "observations": {},
        "interpretations": {},
        "pending_reviews": [],
        "reviews": [],
        "seen_events": {},
        "review_fingerprints": {},
        "review_seq": 0,
        "migrated_at": None,
    }


def ensure(state: dict, enabled: bool | None = None) -> dict | None:
    """Create or migrate research state. Non-research runs remain untouched."""
    if enabled is None:
        enabled = bool(state.get("research_mode"))
    if not enabled:
        state.setdefault("research_mode", False)
        return None
    state["research_mode"] = True
    research = state.setdefault("research", _new_research())
    research.setdefault("schema", SCHEMA)
    for key, value in _new_research().items():
        research.setdefault(key, value)
    if research.get("migrated_at") is None:
        migrate(state)
        research["migrated_at"] = util.iso()
    return research


def migrate(state: dict) -> None:
    """Move old hypotheses/refutations to an explicitly unassessed history."""
    for hid, item in (state.get("hypotheses") or {}).items():
        if not isinstance(item, dict):
            continue
        item.setdefault("version", 1)
        item.setdefault("scope", "")
        item.setdefault("core", False)
        item.setdefault("alternatives", [])
        item.setdefault("evidence_for", [])
        item.setdefault("evidence_against", [])
        item.setdefault("history", [])
        if item.get("legacy") is None:
            item["legacy"] = True
            item["original_status"] = item.get("status")
            item["status"] = "legacy"
            item["assessment_status"] = "unassessed"
            item["migrated_at"] = util.iso()
    for ref in state.get("refuted") or []:
        if not isinstance(ref, dict):
            continue
        ref.setdefault("legacy", True)
        ref.setdefault("active", False)
        ref.setdefault("assessment_status", "unassessed")
        ref.setdefault("history", [])


def hypothesis(state: dict, hid: str) -> dict | None:
    return (state.get("hypotheses") or {}).get(hid)


def ensure_hypothesis(state: dict, item: dict, round_no: int) -> dict:
    hid = str(item["id"])
    existing = hypothesis(state, hid)
    if existing is None:
        record = {
            "id": hid,
            "text": str(item["text"]),
            "version": int(item.get("version") or 1),
            "status": item.get("status") or "open",
            "legacy": False,
            "assessment_status": "unassessed",
            "scope": str(item.get("scope") or ""),
            "core": bool(item.get("core", False)),
            "alternatives": list(item.get("alternatives") or []),
            "evidence_for": [],
            "evidence_against": [],
            "created_round": round_no,
            "updated_round": round_no,
            "history": [],
            "last_review": None,
        }
        state.setdefault("hypotheses", {})[hid] = record
        return record
    old = copy.deepcopy(existing)
    old_status = existing.get("status")
    definition_changed = False
    metadata_changed = False
    status_changed = False
    for field in ("text", "scope"):
        if field in item and item[field] != existing.get(field):
            existing[field] = str(item[field])
            definition_changed = True
    for field in ("core", "alternatives"):
        if field in item and item[field] != existing.get(field):
            existing[field] = item[field]
            metadata_changed = True
    if item.get("status") and item["status"] != old_status:
        existing["status"] = item["status"]
        status_changed = True
        metadata_changed = True
    if existing.get("legacy") and existing.get("assessment_status") == "unassessed":
        # An explicit new assessment can upgrade a legacy record, but only with
        # a new research decision; migration alone never does so.
        existing["legacy"] = False
        if not item.get("status"):
            existing["status"] = "open"
        existing["history"].append({"at": util.iso(), "from": old,
                                    "reason": "explicit research assessment"})
    if definition_changed:
        old_version = int(existing.get("version", 1))
        existing["version"] = old_version + 1
        existing["history"].append({"at": util.iso(), "from": old,
                                    "reason": "definition changed"})
        for interp in (state.get("research") or {}).get("interpretations", {}).values():
            if interp.get("hypothesis") != hid or interp.get("hypothesis_version") != old_version:
                continue
            if interp.get("active"):
                interp["active"] = False
                interp["status"] = "needs_review"
                interp["history"].append({"at": util.iso(), "from": copy.deepcopy(interp),
                                          "reason": "hypothesis definition revised"})
        _add_pending(
            state, kind="hypothesis_revised",
            event_key=f"hyprev:{hid}:{existing['version']}",
            task={}, evidence={}, observation=None, hypothesis_ids=[hid],
            reason=f"假设 {hid} 定义已修订到 v{existing['version']}，旧解释需要复审",
            round_no=round_no, trigger="hypothesis_revised")
    elif metadata_changed:
        existing["history"].append({"at": util.iso(), "from": old,
                                    "reason": "research metadata changed"})
    if status_changed and existing.get("legacy") is not True:
        for task in (state.get("tasks") or {}).values():
            if hid not in (task.get("depends_on_hypotheses") or []):
                continue
            if task.get("status") not in (
                    "completed", "needs_review", "unreviewed",
                    "review_unavailable", "review_invalid"):
                continue
            recorded = (task.get("hypothesis_versions") or {}).get(hid)
            task["status"] = TASK_STATUS_NEEDS_REVIEW
            task["hypothesis_review_reason"] = (
                f"{hid} 状态显式变为 {existing.get('status')}")
            _add_pending(
                state, kind="hypothesis_dependency_stale",
                event_key=(f"hypdep:{task.get('id')}:{hid}:{recorded}->"
                           f"{existing.get('version')}:{existing.get('status')}"),
                task=task, evidence={}, observation=None,
                hypothesis_ids=[hid],
                reason=f"任务 {task.get('id')} 依赖的 {hid} 状态显式变为 "
                       f"{existing.get('status')}，需要复审",
                round_no=round_no,
                trigger="hypothesis_dependency_stale")
    existing["updated_round"] = round_no
    existing["assessment_status"] = existing.get("assessment_status") or "unassessed"
    return existing


def observation(state: dict, task: dict, evidence: dict) -> dict | None:
    """Record the observed result as a separate, versioned object."""
    if not state.get("research_mode") or evidence.get("kind") != "artifact":
        return None
    ev_id = evidence.get("id")
    if not ev_id:
        return None
    research = ensure(state)
    for obs in research["observations"].values():
        if obs.get("evidence") == ev_id:
            return obs
    obs_id = f"obs-{len(research['observations']) + 1:04d}"
    artifact = task.get("artifact") or {}
    source = {
        "task": task.get("id"),
        "artifact": artifact.get("path"),
        "artifact_sha256": artifact.get("sha256"),
        "input_hashes": copy.deepcopy(task.get("input_hashes") or {}),
        "dep_versions": copy.deepcopy(task.get("dep_versions") or {}),
        "experiment_group": task.get("experiment_group"),
    }
    shared = {
        "input_hashes": source["input_hashes"],
        "dep_versions": source["dep_versions"],
        "experiment_group": source["experiment_group"],
    }
    record = {
        "id": obs_id,
        "evidence": ev_id,
        "task": task.get("id"),
        "version": task.get("result_version"),
        "definition_version": task.get("version"),
        "artifact": artifact.get("path"),
        "artifact_sha256": artifact.get("sha256"),
        "source": source,
        "source_group": util.hash_obj(source),
        "shared_group": util.hash_obj(shared),
        "status": "current" if task.get("status") == "completed" else "unverified",
        "created_round": task.get("created_round"),
        "observed_at": util.iso(),
        "invalidated_by": None,
        "history": [],
    }
    research["observations"][obs_id] = record
    evidence["observation"] = obs_id
    evidence["source_group"] = record["source_group"]
    evidence["shared_group"] = record["shared_group"]
    return record


def _interpretation_id(observation_id: str, hid: str, version: int,
                       relation: str, conditions: str, rationale: str,
                       supersedes: list[str] | None = None) -> str:
    return "int-" + util.hash_obj({
        "observation": observation_id, "hypothesis": hid, "version": version,
        "relation": relation, "conditions": conditions, "rationale": rationale,
        "supersedes": sorted(supersedes or []),
    })[:16]


def apply_interpretation(state: dict, raw: dict, round_no: int,
                         *, source: str = "planner") -> dict:
    observation_id = raw["observation"]
    obs = (state.get("research") or {}).get("observations", {}).get(observation_id)
    if not obs:
        obs = next((o for o in (state.get("research") or {}).get("observations", {}).values()
                    if o.get("evidence") == observation_id), None)
    if not obs:
        raise ValueError(f"observation 不存在：{observation_id}")
    observation_id = obs["id"]
    hid = raw["hypothesis"]
    h = hypothesis(state, hid)
    if not h:
        raise ValueError(f"hypothesis 不存在：{hid}")
    version = int(raw.get("hypothesis_version") or 0)
    if version <= 0:
        raise ValueError("hypothesis_version 必须是正整数")
    relation = raw["relation"]
    if relation not in RELATIONS:
        raise ValueError(f"relation 必须是 {sorted(RELATIONS)} 之一")
    if obs.get("status") != "current" and relation != "inconclusive":
        raise ValueError(
            f"observation {observation_id} 已失效，不能用 {relation} 解释")
    if version != int(h.get("version") or 0) and relation != "inconclusive":
        raise ValueError(
            f"假设 {hid} 当前版本是 {h.get('version')}，解释引用的是 {version}")
    conditions = str(raw.get("conditions") or "")
    rationale = str(raw.get("rationale") or "")
    supersedes = list(raw.get("supersedes") or [])
    interpretations = state["research"]["interpretations"]
    for sid in supersedes:
        old = interpretations.get(sid)
        if not old:
            raise ValueError(f"supersedes 解释不存在：{sid}")
        if (old.get("observation") != observation_id
                or old.get("hypothesis") != hid
                or old.get("hypothesis_version") != version
                or old.get("conditions") != conditions):
            raise ValueError(
                "supersedes 只能修正同一 observation、假设版本和条件下的旧解释")
    iid = _interpretation_id(observation_id, hid, version, relation,
                             conditions, rationale, supersedes)
    if iid in interpretations:
        existing = interpretations[iid]
        if not existing.get("active"):
            raise ValueError(
                f"解释 {iid} 当前状态是 {existing.get('status')}，"
                "已失效的解释不能重新用于支持或反驳假设")
        return existing
    record = {
        "id": iid,
        "observation": observation_id,
        "observation_version": obs.get("version"),
        "evidence": obs.get("evidence"),
        "hypothesis": hid,
        "hypothesis_version": version,
        "relation": relation,
        "conditions": conditions,
        "rationale": rationale,
        "source": source,
        "source_group": obs.get("source_group"),
        "shared_group": obs.get("shared_group"),
        # A model claim is data, not proof. It is never used to compute
        # independence; source_group comes from the observation.
        "source_claim": raw.get("source"),
        "supersedes": supersedes,
        "active": (obs.get("status") == "current" and version == h.get("version")),
        "status": "current" if obs.get("status") == "current" else "needs_review",
        "created_round": round_no,
        "created_at": util.iso(),
        "invalidated_by": None,
        "history": [],
    }
    interpretations[iid] = record
    for sid in supersedes:
        old = interpretations[sid]
        old_item = copy.deepcopy(old)
        old["active"] = False
        old["status"] = "superseded"
        old["superseded_by"] = iid
        old["history"].append({"at": util.iso(), "from": old_item,
                               "reason": "superseded by a corrected interpretation"})
    _recompute_hypothesis(state, h)
    return record


def _valid_interpretations(state: dict, hid: str) -> list[dict]:
    research = state.get("research") or {}
    out = []
    for item in research.get("interpretations", {}).values():
        if item.get("hypothesis") != hid or not item.get("active"):
            continue
        obs = research.get("observations", {}).get(item.get("observation"))
        h = hypothesis(state, hid)
        if not obs or obs.get("status") != "current":
            continue
        if not h or item.get("hypothesis_version") != h.get("version"):
            continue
        out.append(item)
    return out


def _recompute_hypothesis(state: dict, h: dict) -> None:
    if h.get("legacy"):
        return
    if h.get("status") == "superseded":
        return
    valid = _valid_interpretations(state, h["id"])
    supports = [i for i in valid if i.get("relation") == "supports"]
    against = [i for i in valid if i.get("relation") == "against"]
    h["evidence_for"] = list(dict.fromkeys(i["evidence"] for i in supports if i.get("evidence")))
    h["evidence_against"] = list(dict.fromkeys(i["evidence"] for i in against if i.get("evidence")))
    if h.get("status") == "contradicted":
        # A reviewer explicitly accepted contradiction; a later invalidation
        # must request review rather than silently turning it back into support.
        if against:
            return
        h["status"] = "needs_review"
        return
    if h.get("status") in ("needs_review", "inconclusive") and not valid:
        return
    if supports and against:
        h["status"] = "contested"
    elif against:
        # Against evidence is a reason to review, not an automatic refutation.
        h["status"] = "needs_review"
    elif supports:
        h["status"] = "supported"
    else:
        h["status"] = "open"


def _add_pending(state: dict, *, kind: str, event_key: str, task: dict,
                 evidence: dict, observation: dict, hypothesis_ids: list[str],
                 reason: str, round_no: int, trigger: str) -> dict:
    research = ensure(state)
    existing = research["seen_events"].get(event_key)
    if existing:
        return next(p for p in research["pending_reviews"] if p["id"] == existing)
    research["review_seq"] = int(research.get("review_seq", 0)) + 1
    rid = f"rr-{research['review_seq']:04d}"
    item = {
        "id": rid,
        "event_key": event_key,
        "kind": kind,
        "trigger": trigger,
        "task": task.get("id"),
        "evidence": [evidence.get("id")] if evidence else [],
        "observation": observation.get("id") if observation else None,
        "hypothesis_ids": list(dict.fromkeys(hypothesis_ids)),
        "status": "pending",
        "reason": reason[:800],
        "created_round": round_no,
        "created_at": util.iso(),
        "source_group": (observation or {}).get("source_group"),
        "resolved_round": None,
        "direction": None,
    }
    research["pending_reviews"].append(item)
    research["seen_events"][event_key] = rid
    return item


def key_result(state: dict, task: dict, evidence: dict, round_no: int) -> dict | None:
    if not state.get("research_mode"):
        return None
    if not task.get("key_experiment"):
        return None
    if task.get("status") != "completed":
        return None
    obs = observation(state, task, evidence)
    if not obs:
        return None
    event_key = f"key:{task.get('id')}:v{task.get('result_version')}:{obs.get('artifact_sha256')}"
    return _add_pending(
        state, kind="key_result", event_key=event_key, task=task,
        evidence=evidence, observation=obs,
        hypothesis_ids=list(task.get("tests_hypotheses") or []),
        reason="关键实验结果已完成，等待研究解释", round_no=round_no,
        trigger="key_experiment_completed")


def prediction_mismatch(state: dict, task: dict, evidence: dict,
                        check: dict, round_no: int) -> dict | None:
    if not state.get("research_mode"):
        return None
    prediction = task.get("research_prediction") or {}
    if not prediction:
        return None
    obs = observation(state, task, evidence)
    if not obs:
        return None
    event_key = (f"prediction:{task.get('id')}:v{task.get('result_version')}:"
                 f"{check.get('name')}:{check.get('exit')}")
    return _add_pending(
        state, kind="prediction_mismatch", event_key=event_key, task=task,
        evidence=evidence, observation=obs,
        hypothesis_ids=[prediction.get("hypothesis")],
        reason=f"预测检查 {check.get('name')} 不匹配（退出码 {check.get('exit')}）",
        round_no=round_no, trigger="prediction_mismatch")


def invalidate_evidence(state: dict, evidence_id: str, reason: str,
                        round_no: int | None = None) -> list[dict]:
    """Invalidate observations/interpretations depending on one evidence item."""
    research = state.get("research") or {}
    touched = []
    for obs in research.get("observations", {}).values():
        if obs.get("evidence") != evidence_id or obs.get("status") == "invalidated":
            continue
        old = copy.deepcopy(obs)
        obs["status"] = "invalidated"
        obs["invalidated_by"] = reason
        obs["invalidated_at"] = util.iso()
        obs["history"].append({"at": util.iso(), "from": old, "reason": reason})
        touched.append(obs)
        for item in research.get("interpretations", {}).values():
            if item.get("observation") != obs["id"] or not item.get("active"):
                continue
            old_item = copy.deepcopy(item)
            item["active"] = False
            item["status"] = "needs_review"
            item["invalidated_by"] = reason
            item["history"].append({"at": util.iso(), "from": old_item,
                                    "reason": reason})
            h = hypothesis(state, item.get("hypothesis"))
            if h and not h.get("legacy") and h.get("status") != "superseded":
                h["status"] = "needs_review"
        for pending in research.get("pending_reviews", []):
            if (pending.get("status") == "pending"
                    and pending.get("observation") == obs["id"]
                    and pending.get("kind") in ("key_result", "prediction_mismatch")):
                old_status = pending.get("status")
                pending["status"] = "superseded"
                pending["resolved_round"] = round_no
                pending["invalidated_by"] = reason
                pending.setdefault("history", []).append({
                    "at": util.iso(), "from": old_status,
                    "reason": "evidence invalidated before review"})
        _add_pending(
            state, kind="evidence_invalidated",
            event_key=f"invalid:{evidence_id}:{obs.get('id')}",
            task=state.get("tasks", {}).get(obs.get("task"), {}),
            evidence={"id": evidence_id}, observation=obs,
            hypothesis_ids=[i.get("hypothesis") for i in
                            research.get("interpretations", {}).values()
                            if i.get("observation") == obs["id"]],
            reason=f"证据失效：{reason}", round_no=round_no or 0,
            trigger="evidence_invalidated")
    for h in (state.get("hypotheses") or {}).values():
        _recompute_hypothesis(state, h)
    return touched


def refresh(state: dict, round_no: int | None = None, validator=None) -> None:
    """Reconcile research objects with the current task/evidence state.

    `validator(task_id)` may return the same path/message defects used by the
    completion validator; the controller passes one so artifact, patch, input
    and full dependency-chain checks are not duplicated here.
    """
    if not state.get("research_mode"):
        return
    ensure(state)
    for obs in list((state.get("research") or {}).get("observations", {}).values()):
        ev = (state.get("evidence") or {}).get(obs.get("evidence"))
        task = (state.get("tasks") or {}).get(obs.get("task"))
        defects = []
        if validator is not None and obs.get("task"):
            try:
                defects = validator(obs.get("task")) or []
            except Exception:
                defects = [([obs.get("task")], "有效性检查失败")]
        reasons = [str(message) for _, message in defects]
        if (not ev or ev.get("stale") or ev.get("superseded")
                or not task or task.get("status") == "stale"):
            reasons.append("证据或任务已失效")
        if reasons and obs.get("status") != "invalidated":
            invalidate_evidence(state, obs.get("evidence"),
                                "；".join(dict.fromkeys(reasons))[:800], round_no)
    for h in (state.get("hypotheses") or {}).values():
        _recompute_hypothesis(state, h)
    for ref in state.get("refuted") or []:
        evidence_ids = list(ref.get("evidence") or [])
        if not evidence_ids:
            continue
        invalid = []
        for eid in evidence_ids:
            ev = (state.get("evidence") or {}).get(eid)
            if not ev or ev.get("stale") or ev.get("superseded"):
                invalid.append(eid)
        if not invalid:
            continue
        key = f"legacyref:{ref.get('id') or ref.get('text')}:{','.join(invalid)}"
        if ref.get("assessment_status") == "needs_review" and \
                ref.get("last_invalidated_key") == key:
            continue
        old = copy.deepcopy(ref)
        ref["active"] = False
        ref["assessment_status"] = "needs_review"
        ref["last_invalidated_key"] = key
        ref.setdefault("history", []).append({
            "at": util.iso(), "from": old, "reason": "反证依赖的证据失效",
        })
        _add_pending(
            state, kind="legacy_refutation_invalidated", event_key=key,
            task={}, evidence={"id": invalid[0]}, observation=None,
            hypothesis_ids=[ref.get("id")] if ref.get("id") else [],
            reason="旧反证依赖的证据已失效，需要复审", round_no=round_no or 0,
            trigger="evidence_invalidated")
    for task in (state.get("tasks") or {}).values():
        if task.get("status") not in ("completed", "needs_review", "unreviewed",
                                      "review_unavailable", "review_invalid"):
            continue
        versions = task.get("hypothesis_versions") or {}
        stale = []
        for hid in task.get("depends_on_hypotheses") or []:
            h = (state.get("hypotheses") or {}).get(hid)
            recorded = versions.get(hid)
            if not h:
                stale.append((hid, recorded, None, "missing"))
            elif recorded is not None and h.get("version") != recorded:
                stale.append((hid, recorded, h.get("version"), "version"))
            elif h.get("status") in ("superseded", "contradicted", "needs_review"):
                stale.append((hid, recorded, h.get("version"), h.get("status")))
        if stale:
            task["status"] = "needs_review"
            task["hypothesis_review_reason"] = "；".join(
                f"{hid} 记录 v{old} 当前 v{new} ({why})" for hid, old, new, why in stale)
            for hid, old, new, why in stale:
                _add_pending(
                    state, kind="hypothesis_dependency_stale",
                    event_key=f"hypdep:{task.get('id')}:{hid}:{old}->{new}:{why}",
                    task=task, evidence={}, observation=None,
                    hypothesis_ids=[hid],
                    reason=f"任务 {task.get('id')} 依赖的 {hid} 已变化：{why}",
                    round_no=round_no or 0,
                    trigger="hypothesis_dependency_stale")


def apply_review(state: dict, payload: dict, round_no: int,
                 *, allow_no_pending: bool = False) -> list[dict]:
    """Apply interpretations/updates/direction from one planner decision."""
    research = ensure(state)
    fingerprint = util.hash_obj(payload)
    if fingerprint in research.setdefault("review_fingerprints", {}):
        return []
    review_ids = list(payload.get("review_ids") or [])
    pending = {p["id"]: p for p in research["pending_reviews"]}
    for rid in review_ids:
        if rid not in pending:
            raise ValueError(f"pending review 不存在：{rid}")
    research["review_fingerprints"][fingerprint] = util.iso()
    # Apply interpretations before resolving the review. They are planner
    # judgments, not automatic truth; the controller only validates references.
    applied = []
    for raw in payload.get("interpretations") or []:
        applied.append(apply_interpretation(state, raw, round_no))
    for update in payload.get("hypothesis_updates") or []:
        ensure_hypothesis(state, update, round_no)
    direction = payload.get("direction")
    if direction is not None:
        choice = direction.get("choice")
        if choice not in DECISIONS:
            raise ValueError(f"direction.choice 必须是 {sorted(DECISIONS)} 之一")
        h = hypothesis(state, direction.get("hypothesis") or "")
        if direction.get("hypothesis") and not h:
            raise ValueError(f"direction.hypothesis 不存在：{direction.get('hypothesis')}")
        # A direction is an investment choice, not a truth or validity update.
        # Only explicit hypothesis_updates / definition changes may alter the
        # hypothesis and re-open dependent tasks.
        if review_ids:
            for rid in review_ids:
                item = pending[rid]
                if item.get("status") == "pending":
                    item["status"] = "resolved"
                    item["resolved_round"] = round_no
                    item["direction"] = copy.deepcopy(direction)
                    item["review_id"] = f"rv-{len(research['reviews']) + 1:04d}"
                    research["reviews"].append({
                        "id": item["review_id"], "round": round_no,
                        "review_id": rid, "hypothesis": direction.get("hypothesis"),
                        "choice": choice, "reason": direction.get("reason"),
                        "evidence": list(direction.get("evidence") or []),
                        "direction": copy.deepcopy(direction),
                    })
        else:
            rid = f"rv-{len(research['reviews']) + 1:04d}"
            research["reviews"].append({
                "id": rid, "round": round_no, "review_id": None,
                "hypothesis": direction.get("hypothesis"), "choice": choice,
                "reason": direction.get("reason"),
                "evidence": list(direction.get("evidence") or []),
                "direction": copy.deepcopy(direction),
            })
    elif review_ids:
        for rid in review_ids:
            item = pending[rid]
            if item.get("status") == "pending":
                item["status"] = "resolved"
                item["resolved_round"] = round_no
    # Recompute after updates; no automatic contradiction status.
    for h in (state.get("hypotheses") or {}).values():
        _recompute_hypothesis(state, h)
    return applied


def pending(state: dict, *, include_resolved: bool = False) -> list[dict]:
    items = list((state.get("research") or {}).get("pending_reviews") or [])
    if include_resolved:
        return items
    return [p for p in items if p.get("status") == "pending"]


def progress_fp(state: dict) -> dict:
    """Decision-relevant research state, independent of ids/rationale churn."""
    hyps = []
    for h in (state.get("hypotheses") or {}).values():
        if h.get("legacy"):
            continue
        hyps.append([str(h.get("id")), int(h.get("version") or 0),
                     str(h.get("status") or ""), bool(h.get("core"))])
    hyps.sort()
    links = set()
    for item in (state.get("research") or {}).get("interpretations", {}).values():
        if not item.get("active"):
            continue
        links.add((str(item.get("observation") or ""),
                   str(item.get("hypothesis") or ""),
                   int(item.get("hypothesis_version") or 0),
                   str(item.get("relation") or "")))
    invalidated = set()
    observations = []
    for obs in (state.get("research") or {}).get("observations", {}).values():
        observations.append((
            str(obs.get("id") or ""), str(obs.get("task") or ""),
            str(obs.get("evidence") or ""), str(obs.get("artifact_sha256") or ""),
            str(obs.get("status") or "")))
        if obs.get("status") == "invalidated":
            invalidated.add((str(obs.get("task") or ""),
                             str(obs.get("evidence") or "")))
    observations.sort()
    return {"hypotheses": hyps,
            "links": sorted([list(x) for x in links]),
            "observations": [list(x) for x in observations],
            "invalidated": sorted([list(x) for x in invalidated])}

def _compact_pending(item: dict, research: dict) -> dict:
    obs = (research.get("observations") or {}).get(item.get("observation"))
    return {
        "id": item.get("id"), "kind": item.get("kind"),
        "status": item.get("status"), "trigger": item.get("trigger"),
        "task": item.get("task"), "evidence": list(item.get("evidence") or []),
        "observation": item.get("observation"),
        "hypothesis_ids": list(item.get("hypothesis_ids") or []),
        "reason": util.sentence(item.get("reason") or "", 180),
        "source_group": item.get("source_group"),
        "artifact_sha256": (obs or {}).get("artifact_sha256"),
        "shared_group": (obs or {}).get("shared_group"),
        "direction": item.get("direction"),
    }


def packet_section(state: dict, max_chars: int = 6000, offset: int = 0) -> dict:
    """A hard-bounded research view with deterministic pagination.

    `pending_results` is one page. `next_offset` is the read cursor; every page
    contains at least one pending item, even when the minimum structure exceeds
    `max_chars`, in which case `limits.over_budget` and `limits.min_chars` say
    how much space is actually required.
    """
    max_chars = max(1, int(max_chars))
    offset = max(0, int(offset or 0))
    research = state.get("research") or {}
    all_pending = [p for p in research.get("pending_reviews", [])
                   if p.get("status") == "pending"]
    total = len(all_pending)
    offset = min(offset, total)
    hypotheses = []
    for h in sorted((state.get("hypotheses") or {}).values(),
                    key=lambda x: str(x.get("id"))):
        if h.get("legacy") and h.get("status") == "superseded":
            continue
        hypotheses.append({
            "id": h.get("id"), "version": h.get("version"),
            "status": h.get("status"), "legacy": bool(h.get("legacy")),
            "text": util.sentence(h.get("text") or "", 120),
            "scope": util.sentence(h.get("scope") or "", 100),
            "core": bool(h.get("core")),
            "alternatives": list(h.get("alternatives") or [])[:6],
            "evidence_for": list(h.get("evidence_for") or [])[:8],
            "evidence_against": list(h.get("evidence_against") or [])[:8],
        })
    hypotheses_total = len(hypotheses)
    hypotheses = hypotheses[:12]
    invalidated_all = []
    for obs in (research.get("observations") or {}).values():
        if obs.get("status") == "invalidated":
            invalidated_all.append({"id": obs.get("id"), "evidence": obs.get("evidence"),
                                    "task": obs.get("task"),
                                    "reason": util.sentence(
                                        obs.get("invalidated_by") or "", 160)})
    invalidated_total = len(invalidated_all)
    invalidated = invalidated_all[-4:]
    reviewed_all = [_compact_pending(p, research)
                    for p in research.get("pending_reviews", [])
                    if p.get("status") != "pending"]
    reviewed = reviewed_all[-2:]
    reviews = (research.get("reviews") or [])[-2:]

    def make(page, include_optional=True):
        next_offset = offset + len(page) if offset + len(page) < total else None
        section = {
            "enabled": bool(state.get("research_mode")),
            "schema": SCHEMA,
            "active_hypotheses": hypotheses,
            "hypotheses_total": hypotheses_total,
            "pending_total": total,
            "pending_offset": offset,
            "pending_returned": len(page),
            "pending_results": page,
            "next_offset": next_offset,
            "invalidated_observations": invalidated if include_optional else [],
            "invalidated_total": invalidated_total,
            "reviewed_results": reviewed if include_optional else [],
            "reviewed_total": len(reviewed_all),
            "recent_reviews": reviews if include_optional else [],
            "truncated_sections": {
                "hypotheses": hypotheses_total > len(hypotheses),
                "invalidated": invalidated_total > len(invalidated),
                "reviewed": len(reviewed_all) > len(reviewed),
            },
            "limits": {"max_chars": max_chars, "truncated": False},
            "read_hint": (
                f'context_requests: [{{"research_page": {next_offset}}}]'
                if next_offset is not None else
                "all pending research results are in this page"),
        }
        section["limits"]["truncated"] = (
            next_offset is not None
            or any(section["truncated_sections"].values()))
        return section

    def mark_over_budget(section):
        section["limits"].update({"over_budget": True, "truncated": True,
                                  "min_chars": 0})
        for _ in range(3):
            size = len(util.canonical(section))
            if section["limits"].get("min_chars") == size:
                break
            section["limits"]["min_chars"] = size
        return section

    def fitted(page):
        section = make(page)
        if len(util.canonical(section)) > max_chars:
            section = make(page, include_optional=False)
        return section

    if offset >= total:
        section = fitted([])
        if len(util.canonical(section)) > max_chars:
            return mark_over_budget(section)
        return section
    page = [all_pending[offset]]
    section = fitted(page)
    if len(util.canonical(section)) > max_chars:
        return mark_over_budget(section)
    # Add as many pending items as fit without exceeding the page budget.
    page_items = [all_pending[offset]]
    pos = offset + 1
    while pos < total:
        candidate = fitted(page_items + [all_pending[pos]])
        if len(util.canonical(candidate)) > max_chars:
            break
        page_items.append(all_pending[pos])
        pos += 1
    section = fitted(page_items)
    if len(util.canonical(section)) > max_chars:
        return mark_over_budget(section)
    return section
def packet_signature(section: dict) -> dict:
    """Fields a compressor/packet trim is not allowed to lose or change."""
    try:
        return {
            "enabled": bool(section.get("enabled")),
            "hypotheses_total": int(section.get("hypotheses_total") or 0),
            "pending_total": int(section.get("pending_total") or 0),
            "pending_offset": int(section.get("pending_offset") or 0),
            "next_offset": section.get("next_offset"),
            "invalidated_total": int(section.get("invalidated_total") or 0),
            "reviewed_total": int(section.get("reviewed_total") or 0),
            "hypotheses": sorted((str(h.get("id")), h.get("version"),
                                  h.get("status"))
                                 for h in section.get("active_hypotheses") or []),
            "pending": [(str(p.get("id")), p.get("status"),
                         tuple(p.get("evidence") or []),
                         tuple(p.get("hypothesis_ids") or []))
                        for p in section.get("pending_results") or []],
            "invalidated": sorted((str(o.get("id")), str(o.get("evidence")))
                                  for o in section.get("invalidated_observations") or []),
        }
    except (TypeError, ValueError, AttributeError):
        return {"invalid": True}
