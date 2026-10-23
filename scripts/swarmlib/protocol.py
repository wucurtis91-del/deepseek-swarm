"""Structured decision protocol and task-graph validation."""
from __future__ import annotations

import copy
import fnmatch
import re

from . import util
from .config import TOOL_PRESETS
from .state import TASK_STATUSES

ACTIONS = {
    "dispatch", "repair", "verify", "replan", "complete", "request_input", "pause",
    "request_context",
}
POLICIES = {"deterministic", "semantic", "combined", "none"}
KINDS = {"text", "analysis", "material", "code", "verify"}
WRITE_TOOLS = {"edit", "bash"}
CHECK_NAME = re.compile(r"[A-Za-z0-9_.-]{1,48}")


class DecisionError(ValueError):
    pass


def _require(cond, message: str) -> None:
    if not cond:
        raise DecisionError(message)


def _string_list(value, field: str, allow_empty=True) -> list[str]:
    if value is None:
        value = []
    _require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value),
             f"{field} 必须是非空字符串列表")
    if not allow_empty:
        _require(bool(value), f"{field} 不能为空")
    return [x.strip() for x in value]


def normalize_acceptance(raw, policy: str, *, assign_ids: bool = True) -> list[dict]:
    if raw is None:
        raw = []
    _require(isinstance(raw, list), "acceptance 必须是列表")
    items, seen = [], set()
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            _require(assign_ids, "修改验收标准时必须显式写出稳定 id")
            cid, text, check = f"c{i}", item, None
        elif isinstance(item, dict):
            cid = item.get("id") or (f"c{i}" if assign_ids else None)
            text = item.get("text")
            check = item.get("check")
        else:
            raise DecisionError("acceptance 的每一项必须是字符串或 {id,text,check}")
        _require(isinstance(cid, str) and util.ACCEPT_ID.fullmatch(cid),
                 f"验收标准 id 不合法：{cid!r}")
        _require(isinstance(text, str) and text.strip(), f"验收标准 {cid} 缺少 text")
        _require(cid not in seen, f"验收标准 id 重复：{cid}")
        seen.add(cid)
        entry = {"id": cid, "text": text.strip()}
        if check is not None:
            _require(isinstance(check, str) and CHECK_NAME.fullmatch(check),
                     f"验收标准 {cid} 的 check 名字不合法")
            entry["check"] = check
        items.append(entry)
    if policy in ("semantic", "combined"):
        _require(bool(items), f"验收策略 {policy} 需要至少一条 acceptance")
    return items


def normalize_checks(raw_task: dict) -> list[dict]:
    checks = []
    raw_list = raw_task.get("checks")
    if raw_list is not None:
        _require(isinstance(raw_list, list), "checks 必须是列表")
        for item in raw_list:
            _require(isinstance(item, dict), "checks 的每一项必须是对象")
            checks.append(_normalize_check(item, item.get("name")))
    if raw_task.get("check_argv") is not None or raw_task.get("check_cmd") is not None:
        _require(raw_task.get("check_argv") is None or raw_task.get("check_cmd") is None,
                 "check_argv 和 check_cmd 只能选一个")
        checks.append(_normalize_check(raw_task, "check"))
    names = [c["name"] for c in checks]
    _require(len(names) == len(set(names)), "checks 的 name 必须唯一")
    return checks


def _normalize_check(item: dict, name) -> dict:
    name = name or "check"
    _require(isinstance(name, str) and CHECK_NAME.fullmatch(name), f"检查名称不合法：{name!r}")
    argv = item.get("argv", item.get("check_argv"))
    cmd = item.get("cmd", item.get("check_cmd"))
    _require((argv is None) != (cmd is None), f"检查 {name} 必须且只能有 argv 或 cmd 之一")
    if argv is not None:
        _require(isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv),
                 f"检查 {name} 的 argv 必须是非空字符串列表")
        out = {"name": name, "argv": list(argv)}
    else:
        _require(isinstance(cmd, str) and cmd.strip(), f"检查 {name} 的 cmd 必须是非空字符串")
        out = {"name": name, "cmd": cmd}
    expect = item.get("expect_exit")
    if expect is not None:
        _require(type(expect) is int, f"检查 {name} 的 expect_exit 必须是整数")
        out["expect_exit"] = expect
    timeout = item.get("timeout")
    if timeout is not None:
        _require(type(timeout) is int and timeout > 0, f"检查 {name} 的 timeout 必须是正整数")
        out["timeout"] = timeout
    return out


def normalize_task(raw: dict, goal_criteria: list[dict], round_no: int,
                   *, allow_string_ids: bool = True) -> dict:
    _require(isinstance(raw, dict), "任务必须是对象")
    tid = raw.get("id")
    _require(isinstance(tid, str) and util.TASK_ID.fullmatch(tid),
             f"任务 id 只能用字母、数字、_ 和 -，最长 64 字符：{tid!r}")
    prompt = raw.get("prompt")
    _require(isinstance(prompt, str) and prompt.strip(), f"任务 {tid} 缺少 prompt")
    for field in ("question", "produces", "decision_impact"):
        value = raw.get(field)
        _require(isinstance(value, str) and value.strip(),
                 f"任务 {tid} 必须说明 {field}（要解决的未知、产物、对后续决策的影响）")
    kind = raw.get("kind") or "text"
    _require(kind in KINDS, f"任务 {tid} 的 kind 只能是 {sorted(KINDS)}")
    tools = raw.get("tools") or "read"
    _require(tools in TOOL_PRESETS, f"任务 {tid} 的 tools 只能是 {list(TOOL_PRESETS)}")
    if kind == "code" or tools in WRITE_TOOLS:
        kind = "code"
    policy = raw.get("acceptance_policy")
    checks = normalize_checks(raw)
    acceptance_raw = raw.get("acceptance", [])
    if policy is None:
        has_acc = bool(acceptance_raw)
        if checks and has_acc:
            policy = "combined"
        elif checks:
            policy = "deterministic"
        elif has_acc:
            policy = "semantic"
        else:
            policy = "none"
    _require(policy in POLICIES, f"任务 {tid} 的 acceptance_policy 只能是 {sorted(POLICIES)}")
    acceptance = normalize_acceptance(acceptance_raw, policy, assign_ids=allow_string_ids)
    if policy in ("deterministic", "combined"):
        _require(bool(checks), f"任务 {tid} 的策略 {policy} 需要至少一个检查")
    if policy == "deterministic":
        names = {c["name"] for c in checks}
        for criterion in acceptance:
            _require(criterion.get("check") in names,
                     f"deterministic 任务的验收标准 {criterion['id']} 必须用 check 指定一个检查")
    for criterion in acceptance:
        if criterion.get("check"):
            _require(criterion["check"] in {c["name"] for c in checks},
                     f"验收标准 {criterion['id']} 引用了不存在的检查 {criterion['check']}")
    if not acceptance and policy in ("semantic", "combined"):
        raise DecisionError(f"任务 {tid} 缺少 acceptance")
    allowed = raw.get("allowed_paths") or []
    _require(isinstance(allowed, list) and all(isinstance(x, str) and x.strip() for x in allowed),
             f"任务 {tid} 的 allowed_paths 必须是字符串列表")
    for path in allowed:
        _require(not path.startswith("/") and ".." not in path.split("/"),
                 f"任务 {tid} 的 allowed_paths 必须是仓库内相对路径：{path}")
    if kind == "code" and policy != "none":
        _require(bool(allowed), f"代码任务 {tid} 必须声明允许修改的 allowed_paths")
    deps = raw.get("depends_on") or []
    _require(isinstance(deps, list) and all(isinstance(x, str) and x.strip() for x in deps),
             f"任务 {tid} 的 depends_on 必须是字符串列表")
    satisfies = raw.get("satisfies") or []
    _require(isinstance(satisfies, list) and all(isinstance(x, str) for x in satisfies),
             f"任务 {tid} 的 satisfies 必须是字符串列表")
    goal_ids = {c["id"] for c in goal_criteria}
    for cid in satisfies:
        _require(cid in goal_ids, f"任务 {tid} 的 satisfies 引用了不存在的完成条件 {cid}")
    max_chars = raw.get("max_chars")
    if max_chars is not None:
        _require(type(max_chars) is int and max_chars > 0, f"任务 {tid} 的 max_chars 必须是正整数")
    max_out = raw.get("max_output_tokens")
    if max_out is not None:
        _require(type(max_out) is int and max_out > 0,
                 f"任务 {tid} 的 max_output_tokens 必须是正整数")
    timeout = raw.get("timeout")
    if timeout is not None:
        _require(type(timeout) is int and timeout > 0, f"任务 {tid} 的 timeout 必须是正整数")
    input_files = raw.get("input_files") or []
    _require(isinstance(input_files, list) and all(isinstance(x, str) and x for x in input_files),
             f"任务 {tid} 的 input_files 必须是字符串列表")
    side_effects = raw.get("side_effects")
    if side_effects is None:
        side_effects = kind == "code"
    _require(isinstance(side_effects, bool), f"任务 {tid} 的 side_effects 必须是布尔值")
    verify_of = raw.get("verify_of")
    if verify_of is not None:
        _require(isinstance(verify_of, str) and util.TASK_ID.fullmatch(verify_of),
                 f"任务 {tid} 的 verify_of 不合法")
    model_override = raw.get("model")
    if model_override is not None:
        _require(isinstance(model_override, str) and model_override.strip(),
                 f"任务 {tid} 的 model 必须是非空字符串")
    cwd_value = raw.get("cwd")
    if cwd_value is not None:
        _require(isinstance(cwd_value, str) and cwd_value.strip(),
                 f"任务 {tid} 的 cwd 必须是非空字符串")
    negative = raw.get("negative_check")
    if negative is not None:
        _require(isinstance(negative, dict), f"任务 {tid} 的 negative_check 必须是对象")
        for field in ("path", "find", "replace"):
            _require(isinstance(negative.get(field), str), f"negative_check 缺少 {field}")
        _require(not str(negative["path"]).startswith("/") and ".." not in str(negative["path"]).split("/"),
                 "negative_check.path 必须是仓库内相对路径")
        _require(bool(checks), f"任务 {tid} 的 negative_check 需要至少一个检查")
        if negative.get("check") is not None:
            _require(negative["check"] in {c["name"] for c in checks},
                     "negative_check.check 引用了不存在的检查")
        negative = {"path": negative["path"], "find": negative["find"],
                    "replace": negative["replace"],
                    "check": negative.get("check") or checks[0]["name"],
                    "note": str(negative.get("note") or "")}
    task = {
        "id": tid, "version": 1, "kind": kind, "prompt": prompt.strip(),
        "question": raw["question"].strip(), "produces": raw["produces"].strip(),
        "decision_impact": raw["decision_impact"].strip(),
        "tools": tools, "acceptance": acceptance, "acceptance_policy": policy,
        "checks": checks, "negative_check": negative, "allowed_paths": allowed,
        "depends_on": deps, "satisfies": satisfies, "max_chars": max_chars,
        "max_output_tokens": max_out, "timeout": timeout, "side_effects": side_effects,
        "input_files": input_files, "verify_of": verify_of,
        "cwd": cwd_value, "model": model_override,
        "created_round": round_no, "status": "pending", "attempts": 0, "repairs": 0,
        "result_version": 0, "artifact": None, "dep_versions": {},
        "last_error": None, "error_signature": None, "repeat_count": 0,
        "definition_hash": "",
    }
    task["definition_hash"] = definition_hash(task)
    return task


def definition_hash(task: dict) -> str:
    keys = ("id", "kind", "prompt", "question", "produces", "decision_impact", "tools",
            "acceptance", "acceptance_policy", "checks", "negative_check", "allowed_paths",
            "depends_on", "satisfies", "max_chars", "max_output_tokens", "timeout",
            "side_effects", "input_files", "verify_of", "cwd", "model")
    return util.hash_obj({k: task.get(k) for k in keys})


def resolve_dep_patterns(tasks: dict[str, dict], patterns: list[str], self_id: str) -> list[str]:
    ids = list(tasks)
    deps = []
    for pattern in patterns:
        hits = [i for i in ids if i != self_id and fnmatch.fnmatchcase(i, pattern)]
        _require(bool(hits), f"任务 {self_id} 的 depends_on「{pattern}」没有匹配到别的任务")
        deps.extend(h for h in hits if h not in deps)
    return deps


def validate_graph(tasks: dict[str, dict], goal_criteria: list[dict]) -> None:
    goal_ids = {c["id"] for c in goal_criteria}
    for tid, task in tasks.items():
        _require(isinstance(task, dict) and task.get("id") == tid, f"任务记录无效：{tid}")
        _require(isinstance(task.get("version"), int) and task["version"] >= 1,
                 f"任务 {tid} 的版本无效")
        _require(task.get("status") in TASK_STATUSES,
                 f"任务 {tid} 的状态无效：{task.get('status')}")
        for dep in task.get("depends_on", []):
            _require(dep in tasks, f"任务 {tid} 依赖了不存在的任务 {dep}")
        for cid in task.get("satisfies", []):
            _require(cid in goal_ids, f"任务 {tid} 引用了不存在的完成条件 {cid}")
        seen = set()
        for criterion in task.get("acceptance", []):
            _require(criterion["id"] not in seen, f"任务 {tid} 的验收标准 id 重复")
            seen.add(criterion["id"])
        if task.get("acceptance_policy") == "deterministic":
            names = {c["name"] for c in task.get("checks", [])}
            for criterion in task.get("acceptance", []):
                _require(criterion.get("check") in names,
                         f"任务 {tid} 的验收标准 {criterion['id']} 没有对应的程序检查")
    # cycles
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 1:
            raise DecisionError("任务依赖成环：" + " → ".join(path + [node]))
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in tasks[node].get("depends_on", []):
            visit(dep, path + [node])
        state[node] = 2

    for node in tasks:
        visit(node, [])


def normalize_decision(raw: dict, goal_criteria: list[dict], round_no: int,
                       known_task_ids: set[str]) -> dict:
    _require(isinstance(raw, dict), "决策必须是 JSON 对象")
    action = raw.get("action")
    _require(action in ACTIONS, f"action 必须是 {sorted(ACTIONS)} 之一")
    reason = raw.get("reason")
    _require(isinstance(reason, str) and reason.strip(), "决策必须给出非空的 reason")
    _require(len(reason) <= 2000, "reason 过长（最多 2000 字）")
    evidence = raw.get("evidence") or []
    _require(isinstance(evidence, list) and all(isinstance(x, str) for x in evidence),
             "evidence 必须是证据 ID 字符串列表")
    hypotheses = raw.get("hypotheses") or []
    _require(isinstance(hypotheses, list), "hypotheses 必须是列表")
    for item in hypotheses:
        _require(isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip(),
                 "hypotheses 的每一项必须是 {text}")
    refuted = raw.get("refuted") or []
    _require(isinstance(refuted, list), "refuted 必须是列表")
    for item in refuted:
        _require(isinstance(item, dict) and isinstance(item.get("reason"), str) and item["reason"].strip(),
                 "refuted 的每一项必须给出 reason")
        _require(isinstance(item.get("text"), str) or isinstance(item.get("id"), str),
                 "refuted 的每一项必须有 text 或 id")
    open_questions = _string_list(raw.get("open_questions"), "open_questions")
    requests = raw.get("context_requests") or []
    _require(isinstance(requests, list) and len(requests) <= 4, "context_requests 最多 4 项")
    for item in requests:
        _require(isinstance(item, dict) and (isinstance(item.get("artifact"), str)
                                             or isinstance(item.get("evidence"), str)),
                 "context_requests 的每一项必须是 {artifact} 或 {evidence}")
        if "offset" in item:
            _require(type(item["offset"]) is int and item["offset"] >= 0,
                     "context_requests.offset 必须是非负整数")
        if "limit" in item:
            _require(type(item["limit"]) is int and item["limit"] > 0,
                     "context_requests.limit 必须是正整数")
    next_acceptance = _string_list(raw.get("next_acceptance"), "next_acceptance")
    recommendations = _string_list(raw.get("recommendations"), "recommendations")
    decision = {
        "action": action, "reason": reason.strip(), "evidence": evidence,
        "hypotheses": hypotheses, "refuted": refuted, "open_questions": open_questions,
        "recommendations": recommendations,
        "context_requests": requests, "next_acceptance": next_acceptance,
        "round": round_no, "raw_action": action,
    }
    tasks = raw.get("tasks") or []
    _require(isinstance(tasks, list), "tasks 必须是列表")
    if action == "dispatch":
        _require(bool(tasks), "dispatch 至少要派发一个任务")
        decision["tasks"] = [normalize_task(t, goal_criteria, round_no) for t in tasks]
    elif action == "request_context":
        _require(bool(requests), "request_context 至少要请求一个 artifact 或 evidence")
    elif action == "verify":
        target = raw.get("target_task")
        _require(isinstance(target, str) and target, "verify 必须给出 target_task")
        _require(target in known_task_ids, f"verify 的目标任务不存在：{target}")
        _require(bool(tasks), "verify 至少要给出一个验证任务")
        verify_tasks = []
        for item in tasks:
            item = copy.deepcopy(item)
            item.setdefault("kind", "verify")
            item.setdefault("tools", "read")
            item.setdefault("verify_of", target)
            deps = item.setdefault("depends_on", [])
            if target not in deps:
                deps.append(target)
            verify_tasks.append(normalize_task(item, goal_criteria, round_no))
        decision["target_task"] = target
        decision["tasks"] = verify_tasks
    elif action == "replan":
        decision["cancel"] = _string_list(raw.get("cancel"), "cancel")
        decision["tasks"] = [normalize_task(t, goal_criteria, round_no) for t in tasks]
        _require(bool(decision["tasks"] or decision["cancel"]),
                 "replan 至少要新增/修改任务或取消任务")
    elif action == "repair":
        target = raw.get("target_task")
        _require(isinstance(target, str) and target, "repair 必须给出 target_task")
        _require(target in known_task_ids, f"repair 的目标任务不存在：{target}")
        instructions = raw.get("instructions")
        _require(isinstance(instructions, str) and instructions.strip(),
                 "repair 必须给出 instructions")
        decision["target_task"] = target
        decision["instructions"] = instructions.strip()
        if raw.get("acceptance") is not None:
            policy = raw.get("acceptance_policy") or "semantic"
            _require(policy in POLICIES, "repair 的 acceptance_policy 无效")
            decision["acceptance"] = normalize_acceptance(
                raw["acceptance"], policy, assign_ids=False)
            decision["acceptance_policy"] = policy
        if raw.get("checks") is not None or raw.get("check_argv") is not None \
                or raw.get("check_cmd") is not None:
            decision["checks"] = normalize_checks(raw)
    elif action == "complete":
        completion = raw.get("completion")
        _require(isinstance(completion, list) and completion, "complete 必须给出 completion 列表")
        goal_ids = {c["id"] for c in goal_criteria}
        seen = set()
        normalized = []
        for item in completion:
            _require(isinstance(item, dict), "completion 的每一项必须是对象")
            cid = item.get("criterion")
            _require(cid in goal_ids, f"completion 引用了不存在的完成条件：{cid!r}")
            _require(cid not in seen, f"completion 的完成条件重复：{cid}")
            seen.add(cid)
            met = item.get("met")
            _require(isinstance(met, bool), f"完成条件 {cid} 的 met 必须是布尔值")
            ev = item.get("evidence") or []
            _require(isinstance(ev, list) and all(isinstance(x, str) for x in ev),
                     f"完成条件 {cid} 的 evidence 必须是字符串列表")
            if met:
                _require(bool(ev), f"完成条件 {cid} 标为满足时必须给出证据")
            normalized.append({"criterion": cid, "met": met, "evidence": ev,
                               "note": str(item.get("note") or "")})
        _require(seen == goal_ids, "complete 必须逐项覆盖全部完成条件")
        decision["completion"] = normalized
        decision["summary"] = str(raw.get("summary") or "")
    elif action == "request_input":
        question = raw.get("question")
        _require(isinstance(question, str) and question.strip(),
                 "request_input 必须给出具体的 question")
        decision["question"] = question.strip()
    elif action == "pause":
        decision["pause_kind"] = str(raw.get("pause_kind") or "planner")
    return decision


def decision_fingerprint(decision: dict) -> str:
    relevant = {
        "action": decision.get("action"),
        "target": decision.get("target_task"),
        "instructions": decision.get("instructions"),
        "tasks": [{k: t.get(k) for k in ("id", "prompt", "acceptance", "checks",
                                         "depends_on", "version", "verify_of")}
                  for t in decision.get("tasks", [])],
        "cancel": decision.get("cancel"),
        "completion": decision.get("completion"),
        "question": decision.get("question"),
        "reason": decision.get("reason"),
    }
    return util.hash_obj(relevant)
