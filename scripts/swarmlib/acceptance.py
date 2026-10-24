"""Acceptance: deterministic checks, strict semantic review, independent test quality."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import procs, security, util

REVIEW_RULES = """\
你是独立复核员（reviewer）。另一个执行者完成了下面的任务，你要严格检查它的结果。
- 逐条对照验收标准判断；只要有一条不满足，就不能通过。
- 自己从源文件和实际改动中核对，不要相信结果里的说法；计算要独立重算。
- 代码任务要核对 diff 是否真的实现了要求的行为，而不只是看执行者的报告。
- 不要自己重写整份结果。指出具体错误：哪一处、错在哪、正确应是什么。
- 复核过程放在思考里，输出只写结论。每条 note 不超过 40 字，errors 最多 8 条。

最后只输出一个 JSON 代码块，严格按这个结构：
```json
{"criteria": [{"id": "验收标准的 id", "met": true或false, "note": "依据或问题"}],
 "errors": ["具体错误，含位置和正确内容"],
 "fix_instructions": "给执行者的修改指示；通过时写空字符串"}
```
必须覆盖给定的每一个验收标准 id，不能省略、不能多写、不能用字符串代替布尔值。
errors 非空时不能判为通过（即使所有 criteria 都写了 true）。"""


def _placeholders(task: dict, result_path: Path) -> dict:
    values = dict(task.get("_placeholders") or {})
    values["{result}"] = str(result_path)
    return values


def _fill_argv(argv: list[str], values: dict) -> list[str]:
    out = []
    for arg in argv:
        for name, value in values.items():
            arg = arg.replace(name, str(value))
        out.append(arg)
    return out


def check_env(task: dict, cfg: dict) -> dict:
    allow = tuple(cfg.get("check_env_allow") or [])
    extra = task.get("check_env") or {}
    if not isinstance(extra, dict):
        extra = {}
    return security.minimal_env(extra={k: str(v) for k, v in extra.items()}, allow=allow)


def run_checks(task: dict, cwd: Path, result_path: Path, cfg: dict,
               cancel_event, secrets=()) -> list[dict]:
    env = check_env(task, cfg)
    values = _placeholders(task, result_path)
    secrets = tuple(secrets) + tuple(str(v) for v in (task.get("check_env") or {}).values())
    records = []
    for spec in task.get("checks") or []:
        timeout = spec.get("timeout") or cfg.get("check_timeout", 300)
        expect = spec.get("expect_exit", 0)
        if spec.get("argv"):
            argv = _fill_argv(spec["argv"], values)
            res = procs.run(argv, shell=False, cwd=cwd, env=env, timeout=timeout,
                            cancel_event=cancel_event, merge_output=True)
            shown = argv
        else:
            try:
                cmd, extra_env = procs.substitute_placeholders(spec.get("cmd", ""), values)
            except procs.PlaceholderError as exc:
                records.append({"name": spec["name"], "ok": False, "exit": None,
                                "expected_exit": expect, "seconds": 0.0,
                                "output": str(exc), "error": "placeholder", "argv": None})
                continue
            res = procs.run(cmd, shell=True, cwd=cwd, env={**env, **extra_env},
                            timeout=timeout, cancel_event=cancel_event, merge_output=True)
            shown = cmd
        output = security.redact(res.get("output", ""), secrets)
        output = output[-int(cfg.get("check_output_chars", 3000)):]
        ok = (not res.get("timed_out") and not res.get("cancelled")
              and res.get("code") == expect)
        records.append({
            "name": spec["name"], "ok": ok, "exit": res.get("code"),
            "expected_exit": expect, "seconds": res.get("seconds", 0.0),
            "output": output, "timed_out": bool(res.get("timed_out")),
            "cancelled": bool(res.get("cancelled")),
            "argv": shown if isinstance(shown, str) else " ".join(shown),
            "error": res.get("error"),
        })
    return records


def run_negative_check(task: dict, cwd: Path, result_path: Path, cfg: dict,
                       workspaces, cancel_event, secrets=()) -> dict | None:
    """Inject a known fault into a copy and require the check to notice it.

    This is independent evidence about test quality: a worker's own tests passing
    is not enough, because a test that asserts nothing also passes.
    """
    neg = task.get("negative_check")
    if not neg:
        return None
    secrets = tuple(secrets) + tuple(str(v) for v in (task.get("check_env") or {}).values())
    name = neg.get("check")
    spec = next((c for c in task.get("checks") or [] if c["name"] == name), None)
    if spec is None:
        return {"name": f"negative:{name}", "ok": False, "exit": None,
                "output": f"negative_check 引用了不存在的检查 {name}", "error": "config"}
    try:
        tree = workspaces.copy_for_negative_check(Path(cwd))
    except OSError as exc:
        return {"name": f"negative:{name}", "ok": False, "exit": None,
                "output": security.redact(f"无法复制工作区做负向检查：{exc}", secrets),
                "error": "copy_failed"}
    try:
        target = tree / neg["path"]
        if not target.is_file():
            return {"name": f"negative:{name}", "ok": False, "exit": None,
                    "output": f"负向检查的目标文件不存在：{neg['path']}", "error": "config"}
        text = target.read_text(encoding="utf-8", errors="replace")
        if neg["find"] not in text:
            return {"name": f"negative:{name}", "ok": False, "exit": None,
                    "output": f"负向检查找不到要替换的文本：{neg['find'][:120]}",
                    "error": "fault_not_applied"}
        target.write_text(text.replace(neg["find"], neg["replace"], 1), encoding="utf-8")
        values = _placeholders(task, result_path)
        timeout = spec.get("timeout") or cfg.get("check_timeout", 300)
        expect = spec.get("expect_exit", 0)
        if spec.get("argv"):
            res = procs.run(_fill_argv(spec["argv"], values), shell=False, cwd=tree,
                            env=check_env(task, cfg), timeout=timeout,
                            cancel_event=cancel_event, merge_output=True)
        else:
            cmd, extra = procs.substitute_placeholders(spec.get("cmd", ""), values)
            res = procs.run(cmd, shell=True, cwd=tree,
                            env={**check_env(task, cfg), **extra}, timeout=timeout,
                            cancel_event=cancel_event, merge_output=True)
        detected = (not res.get("timed_out") and not res.get("cancelled")
                    and res.get("code") != expect)
        output = security.redact(res.get("output", ""), secrets)[-1500:]
        return {
            "name": f"negative:{name}", "ok": detected, "exit": res.get("code"),
            "expected_exit": f"!={expect}", "seconds": res.get("seconds", 0.0),
            "output": output,
            "detail": ("检查发现了注入的错误" if detected
                       else "注入已知错误后检查仍然通过：测试质量不合格"),
            "note": neg.get("note", ""),
        }
    finally:
        shutil.rmtree(tree.parent, ignore_errors=True)


def parse_review_strict(text: str, criteria: list[dict]) -> dict:
    """A review must cover every criterion ID with a real boolean."""
    required = {c["id"]: c for c in criteria}
    candidate = None
    for obj in util.json_objects(text):
        if isinstance(obj, dict) and ("criteria" in obj or "pass" in obj):
            candidate = obj
            break
    if candidate is None:
        return {"pass": False, "outcome": "invalid", "criteria": {},
                "errors": ["复核员没有给出可解析的 JSON 结论"], "fix_instructions": "",
                "reason": "unparsed"}
    if "errors" not in candidate:
        return {"pass": False, "outcome": "invalid", "criteria": {},
                "errors": ["复核响应缺少 errors 字段"], "fix_instructions": "",
                "reason": "errors_missing"}
    errors_raw = candidate.get("errors", [])
    if not isinstance(errors_raw, list) or not all(isinstance(e, str) for e in errors_raw):
        return {"pass": False, "outcome": "invalid", "criteria": {},
                "errors": ["errors 必须是字符串列表"], "fix_instructions": "",
                "reason": "errors_not_list"}
    items = candidate.get("criteria")
    if not isinstance(items, list):
        return {"pass": False, "outcome": "invalid", "criteria": {},
                "errors": ["复核响应缺少 criteria 列表"], "fix_instructions": "",
                "reason": "criteria_missing"}
    got = {}
    shape_errors = []
    for item in items:
        if not isinstance(item, dict):
            shape_errors.append("criteria 的每一项必须是对象")
            continue
        cid = item.get("id")
        if cid not in required:
            shape_errors.append(f"未知的验收标准 id：{cid!r}")
            continue
        if cid in got:
            shape_errors.append(f"验收标准重复：{cid}")
            continue
        met = item.get("met")
        if not isinstance(met, bool):
            shape_errors.append(f"验收标准 {cid} 的 met 必须是布尔值，不是 {type(met).__name__}")
            continue
        note = item.get("note", "")
        if not isinstance(note, str):
            note = str(note)
        got[cid] = {"met": met, "note": note[:500]}
    missing = [cid for cid in required if cid not in got]
    if missing:
        shape_errors.append("缺少验收标准：" + "、".join(missing))
    extra_errors = [e for e in errors_raw if e.strip()]
    declared_pass = candidate.get("pass", None)
    if declared_pass is not None and not isinstance(declared_pass, bool):
        shape_errors.append("pass 必须是布尔值")
    fix = candidate.get("fix_instructions", "")
    if not isinstance(fix, str):
        fix = str(fix)
    if shape_errors:
        return {"pass": False, "outcome": "invalid", "criteria": got,
                "errors": shape_errors + extra_errors, "fix_instructions": fix,
                "reason": "shape"}
    all_met = all(v["met"] for v in got.values())
    if declared_pass is True and not all_met:
        return {"pass": False, "outcome": "failed", "criteria": got,
                "errors": extra_errors + ["复核员声称通过，但有验收标准标为未满足"],
                "fix_instructions": fix, "reason": "contradiction"}
    if declared_pass is True and extra_errors:
        return {"pass": False, "outcome": "failed", "criteria": got,
                "errors": extra_errors + ["errors 非空却声称通过"],
                "fix_instructions": fix, "reason": "contradiction"}
    passed = all_met and not extra_errors and (declared_pass is not False)
    return {"pass": passed, "outcome": "passed" if passed else "failed",
            "criteria": got, "errors": extra_errors, "fix_instructions": fix,
            "reason": None}


def review_prompt(task: dict, artifact_text: str, checks: list[dict],
                  changes: dict | None, cfg: dict) -> str:
    parts = [REVIEW_RULES,
             "## 任务\n" + task["prompt"],
             "## 这个任务要解决的未知\n" + task.get("question", ""),
             "## 验收标准（必须逐条覆盖 id）\n" + "\n".join(
                 f"- {c['id']}: {c['text']}"
                 + (f"（对应程序检查 {c['check']}）" if c.get("check") else "")
                 for c in task.get("acceptance") or [])]
    if checks:
        parts.append("## 程序检查结果\n" + "\n".join(
            f"- {c['name']}: {'通过' if c['ok'] else '未通过'}"
            + (f"，退出码 {c.get('exit')}" if c.get("exit") is not None else "")
            + (f"\n  输出尾部：{c.get('output', '')[-800:]}" if c.get("output") else "")
            for c in checks))
    if changes and changes.get("kind") == "git":
        diff = changes.get("diff") or ""
        cap = int(cfg.get("review_max_artifact_chars", 24000))
        parts.append("## 实际代码改动（git diff）\n```diff\n"
                     + util.truncate(diff, cap) + "\n```")
        files = ", ".join(f"{c['status']} {c['path']}" for c in changes.get("changed_files", []))
        parts.append("## 改动文件\n" + (files or "（无）"))
    cap = int(cfg.get("review_max_artifact_chars", 24000))
    parts.append("## 执行者交回的结果\n" + util.truncate(artifact_text, cap))
    return "\n\n".join(parts)


def semantic_review(task: dict, artifact_text: str, checks: list[dict],
                    changes: dict | None, cfg: dict, call_reviewer,
                    cancel_event) -> dict:
    """Call the reviewer, retrying transport errors and malformed verdicts separately."""
    prompt = review_prompt(task, artifact_text, checks, changes, cfg)
    results = []
    last_error = ""
    last_kind = None
    for attempt in range(int(cfg.get("review_retries", 2)) + 1):
        if cancel_event is not None and cancel_event.is_set():
            return {"outcome": "unavailable", "verdict": None, "calls": results,
                    "prompt": prompt, "error": "已取消"}
        if attempt:
            prompt = prompt + (
                "\n\n## 上一次复核输出无效\n"
                f"{last_error}\n请严格按上面规定的 JSON 结构重新输出，覆盖全部验收标准 id。")
        res = call_reviewer(prompt, purpose="review" if attempt == 0 else "review_retry")
        results.append(res)
        if not res.get("ok"):
            last_error = str(res.get("error") or "")[:500]
            last_kind = "call"
            kind = res.get("error_kind")
            if kind in ("auth", "config", "identity"):
                return {"outcome": "unavailable", "verdict": None, "calls": results,
                        "prompt": prompt, "error": last_error, "error_kind": kind}
            continue
        verdict = parse_review_strict(res.get("text", ""), task.get("acceptance") or [])
        if verdict["outcome"] == "invalid":
            last_error = "；".join(verdict["errors"])[:500]
            last_kind = "invalid"
            continue
        verdict["calls"] = results
        verdict["prompt"] = prompt
        return verdict
    outcome = "invalid" if last_kind == "invalid" else "unavailable"
    return {"outcome": outcome, "verdict": None, "calls": results, "prompt": prompt,
            "error": last_error or "复核没有给出有效结论"}


def evaluate(task: dict, checks: list[dict], review: dict | None,
             negative: dict | None = None) -> dict:
    policy = task.get("acceptance_policy", "none")
    criteria = {c["id"]: {"id": c["id"], "text": c["text"], "met": False,
                          "by": [], "note": ""}
                for c in task.get("acceptance") or []}
    checks_by_name = {c["name"]: c for c in checks}
    all_checks_ok = all(c["ok"] for c in checks) if checks else True
    if negative is not None:
        all_checks_ok = all_checks_ok and bool(negative.get("ok"))
    review_passed = bool(review and review.get("outcome") == "passed" and review.get("pass"))
    review_available = bool(review and review.get("outcome") in ("passed", "failed"))
    for cid, item in criteria.items():
        spec = next((c for c in task.get("acceptance") or [] if c["id"] == cid), None) or {}
        check_name = spec.get("check")
        if check_name and check_name in checks_by_name:
            check = checks_by_name[check_name]
            item["met"] = bool(check["ok"])
            item["by"].append(f"check:{check_name}")
            item["note"] = check.get("output", "")[-300:] if not check["ok"] else "程序检查通过"
        if policy in ("semantic", "combined") and review and review.get("criteria"):
            rv = review["criteria"].get(cid)
            if rv:
                item["by"].append("review")
                item["note"] = rv.get("note", "")[:300]
                if check_name:
                    item["met"] = item["met"] and bool(rv["met"])
                else:
                    item["met"] = bool(rv["met"])
    if policy == "deterministic":
        passed = all_checks_ok and all(v["met"] for v in criteria.values())
    elif policy == "semantic":
        passed = review_passed and all(v["met"] for v in criteria.values())
    elif policy == "combined":
        passed = all_checks_ok and review_passed and all(v["met"] for v in criteria.values())
    else:
        passed = False
    if policy == "none":
        outcome = "not_required"
    elif review is None:
        outcome = "not_requested"
    else:
        outcome = review.get("outcome", "unavailable")
    return {
        "policy": policy, "passed": passed, "verified": passed and policy != "none",
        "checks_ok": all_checks_ok if checks else None,
        "review_outcome": outcome,
        "review_passed": review_passed, "review_available": review_available,
        "criteria": list(criteria.values()), "checks": checks, "negative": negative,
    }


def criteria_signature(criteria: list[dict]) -> str:
    return util.hash_obj([{"id": c["id"], "text": c["text"], "check": c.get("check")}
                          for c in criteria])


def _file_sha(path: Path) -> str | None:
    try:
        return util.sha256_file(path)
    except OSError:
        return None


def _task_record_defects(state: dict, task_id: str, run_dir: Path | None,
                         cache: dict, expected: dict | None) -> list[tuple[list[str], str]]:
    """Validate one task and its dependency subtree.

    File/subtree defects are cached per task. Expectations belong to an incoming
    dependency edge and must be checked separately on every visit.
    """
    state_tasks = state.get("tasks") or {}
    task = state_tasks.get(task_id)
    if not task:
        return [([task_id], f"任务 {task_id} 不存在")]
    expected = expected or {}
    defects: list[tuple[list[str], str]] = []
    path = [task_id]

    def add(suffix: list[str], message: str) -> None:
        defects.append((path + suffix, message))

    expected_status = expected.get("status")
    status = task.get("status")
    if expected_status is None:
        expected_status = "completed"
    if status != expected_status:
        detail = task.get("stale_reason") or task.get("last_error") or ""
        add([], f"当前状态是 {status}，记录要求 {expected_status}"
            + (f"（{detail}）" if detail else ""))
    if expected.get("result_version") is not None and \
            task.get("result_version") != expected.get("result_version"):
        add([], f"结果版本从 v{expected.get('result_version')} 变成 "
                f"v{task.get('result_version')}")
    if expected.get("definition_version") is not None and \
            task.get("version") != expected.get("definition_version"):
        add([], f"任务定义从 v{expected.get('definition_version')} 变成 v{task.get('version')}")
    artifact = task.get("artifact") or {}
    if expected.get("sha256") is not None and artifact.get("sha256") != expected.get("sha256"):
        add([], "产物哈希与依赖它的任务记录不一致")
    edge_defects = defects
    if task_id in cache:
        return edge_defects + cache[task_id]
    defects = []
    if task.get("stale"):
        add([], f"任务已失效：{task.get('stale_reason') or '上游或输入变化'}")
    if status == "completed":
        acc = task.get("acceptance_result") or {}
        if not acc.get("passed") or not acc.get("verified"):
            add([], "当前验收结果不是通过状态")
        if any(not item.get("met") for item in (acc.get("criteria") or [])):
            add([], "仍有未满足的验收标准")
    art_path = artifact.get("path")
    if not art_path:
        add([], "没有产物文件")
    elif run_dir is not None:
        full = Path(run_dir) / art_path
        if not full.is_file():
            add([], f"产物文件不存在：{art_path}")
        elif artifact.get("sha256") and _file_sha(full) != artifact.get("sha256"):
            add([], f"产物 {art_path} 哈希已变化")
    if task.get("kind") == "code":
        changes = task.get("changes") or {}
        patch_path = changes.get("patch_path")
        if not patch_path:
            add([], "代码任务没有补丁文件")
        elif run_dir is not None:
            full_patch = Path(run_dir) / patch_path
            if not full_patch.is_file():
                add([], f"补丁文件不存在：{patch_path}")
            elif changes.get("diff_sha256") and \
                    _file_sha(full_patch) != changes.get("diff_sha256"):
                add([], f"补丁 {patch_path} 哈希已变化")
        if not changes.get("scope_ok") or changes.get("outside_scope"):
            add([], "代码改动超出允许路径")
    if run_dir is not None:
        for rel, recorded in (task.get("input_hashes") or {}).items():
            base = Path(recorded.get("base") or run_dir)
            source = base / rel
            actual = _file_sha(source) if source.is_file() else None
            if actual != recorded.get("sha256"):
                add([], f"输入文件 {rel} 已变化或不存在")

    dep_versions = task.get("dep_versions") or {}
    deps = list(dict.fromkeys(list(task.get("depends_on") or []) + list(dep_versions)))
    for dep in deps:
        recorded = dep_versions.get(dep)
        if not recorded:
            add([dep], f"依赖 {dep} 没有记录版本")
            continue
        dep_task = state_tasks.get(dep)
        if not dep_task:
            add([dep], f"上游 {dep} 不存在")
            continue
        for child_path, message in _task_record_defects(
                state, dep, run_dir, cache, recorded):
            add(child_path, message)
    cache[task_id] = defects
    return edge_defects + defects


def _evidence_refresh_errors(state: dict, ev: dict, cid: str,
                            run_dir: Path | None, cache: dict | None = None) -> list[str]:
    """Re-check the evidence against the current run, not only its old snapshot.

    A model can say complete after an artifact or an input changed. The decision
    is valid only while the whole dependency chain, its files and its acceptance
    signatures still match the results that were actually verified.
    """
    errors: list[str] = []
    ref = ev.get("id") or "?"
    tid = ev.get("task")
    task = (state.get("tasks") or {}).get(tid)
    if not task:
        return [f"完成条件 {cid} 的证据 {ref} 指向不存在的任务 {tid}"]
    if ev.get("stale") or ev.get("superseded"):
        errors.append(f"完成条件 {cid} 的证据 {ref} 已失效（{ev.get('superseded_reason') or '过期'}）")
    if not ev.get("verified"):
        errors.append(f"完成条件 {cid} 的证据 {ref} 还没有通过验收")
    if ev.get("task_status") != "completed" or task.get("status") != "completed":
        detail = task.get("stale_reason") or task.get("last_error") or ""
        errors.append(
            f"完成条件 {cid} 的证据 {ref} 对应任务 {tid} 当前状态是 "
            f"{task.get('status')}，不是 completed" + (f"（{detail}）" if detail else ""))
    if ev.get("version") != task.get("result_version"):
        errors.append(
            f"完成条件 {cid} 的证据 {ref} 对应结果 v{ev.get('version')}，"
            f"当前是 v{task.get('result_version')}")
    if ev.get("definition_version") not in (None, task.get("version")):
        errors.append(
            f"完成条件 {cid} 的证据 {ref} 对应任务定义 v{ev.get('definition_version')}，"
            f"当前是 v{task.get('version')}")
    current_sig = criteria_signature(task.get("acceptance") or [])
    if ev.get("acceptance_signature") not in (None, current_sig):
        errors.append(f"完成条件 {cid} 的证据 {ref} 对应的验收标准已被修改")
    if run_dir is None:
        errors.append(f"完成条件 {cid} 的完成校验缺少运行目录，无法核对文件")
        return errors
    run_dir = Path(run_dir)
    cache = cache if cache is not None else {}
    for path, message in _task_record_defects(
            state, tid, run_dir, cache, {"status": "completed"}):
        errors.append(f"完成条件 {cid}：{' -> '.join(path)}：{message}")
    artifact = task.get("artifact") or {}
    art_path = artifact.get("path")
    if not ev.get("artifact"):
        errors.append(f"完成条件 {cid} 的证据 {ref} 没有产物文件")
    elif ev.get("artifact") != art_path:
        errors.append(f"完成条件 {cid} 的证据 {ref} 指向的产物路径与当前产物不一致")
    if art_path and ev.get("sha256") and ev.get("sha256") != artifact.get("sha256"):
        errors.append(f"完成条件 {cid} 的证据 {ref} 与当前产物哈希不一致")
    if task.get("kind") == "code":
        changes = task.get("changes") or {}
        if ev.get("patch_path") and ev.get("patch_path") != changes.get("patch_path"):
            errors.append(f"完成条件 {cid} 的证据 {ref} 指向的补丁与当前补丁不一致")
        if ev.get("diff_sha256") and \
                ev.get("diff_sha256") != changes.get("diff_sha256"):
            errors.append(f"完成条件 {cid} 的证据 {ref} 的补丁哈希与当前补丁不一致")
    return errors


def validate_completion(state: dict, decision: dict,
                        run_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Validate completion from current, verified evidence for every goal criterion."""
    goal = state.get("goal") or {}
    goal_ids = [c["id"] for c in goal.get("criteria", [])]
    entries = {e["criterion"]: e for e in decision.get("completion", [])}
    errors = []
    if set(entries) != set(goal_ids):
        errors.append("complete 没有逐项覆盖全部完成条件")
        return False, errors
    evidence = state.get("evidence") or {}
    chain_cache: dict[str, list[tuple[list[str], str]]] = {}
    for cid in goal_ids:
        entry = entries[cid]
        if not entry.get("met"):
            errors.append(f"完成条件 {cid} 没有被标为满足")
            continue
        valid_refs = []
        for ref in entry.get("evidence") or []:
            ev = evidence.get(ref)
            if not ev:
                errors.append(f"完成条件 {cid} 引用了不存在的证据 {ref}")
                continue
            if ev.get("kind") != "artifact" or ev.get("task_status") != "completed":
                errors.append(f"完成条件 {cid} 的证据 {ref} 不是来自已完成任务的验收产物")
                continue
            if ev.get("task_kind") == "code" and not (
                    ev.get("patch_path") and ev.get("diff_sha256") and ev.get("scope_ok")):
                errors.append(f"完成条件 {cid} 的代码产物 {ref} 没有可核对的补丁或超出允许路径")
                continue
            linked = cid in (ev.get("goal_criteria") or []) or cid in (ev.get("satisfies") or [])
            if not linked:
                errors.append(f"完成条件 {cid} 的证据 {ref} 没有关联到该完成条件")
                continue
            refresh = _evidence_refresh_errors(state, ev, cid, run_dir, chain_cache)
            errors.extend(refresh)
            if not refresh:
                valid_refs.append(ref)
        if not valid_refs:
            errors.append(f"完成条件 {cid} 没有有效的验收证据")
    return not errors, errors
