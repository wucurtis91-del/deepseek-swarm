#!/usr/bin/env python3
"""Persistent decision loop for deepseek-swarm.

External-host mode (uses the Claude session you are already in):
    swarm_loop.py start RUN_DIR --goal goal.json [--config roles.json]
    swarm_loop.py next RUN_DIR
    swarm_loop.py decide RUN_DIR decision.json
    swarm_loop.py next RUN_DIR              # repeat

Automatic-host mode (the controller calls a configured Claude backend):
    swarm_loop.py auto RUN_DIR [--max-steps N]

Other commands: status, resume, cancel, artifacts, validate.

Standard library only. See README.md for the decision schema and budget model.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from swarmlib import backends as backends_mod       # noqa: E402
from swarmlib import config as config_mod           # noqa: E402
from swarmlib import engine as engine_mod           # noqa: E402
from swarmlib import protocol                       # noqa: E402
from swarmlib import state as state_mod             # noqa: E402
from swarmlib import util                           # noqa: E402


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _read_json(path: str, what: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读取{what}失败：{exc}")


def _roles(cfg: dict, allow_fake: bool):
    return config_mod.validate_roles(cfg["roles"], allow_fake=allow_fake)


def _load_run_config(args):
    """Use an explicit override, otherwise the run's saved startup config."""
    if args.config:
        return config_mod.load_config(args.config)
    store = state_mod.RunStore(args.run_dir)
    if store.exists():
        saved = store.load().get("execution_config")
        if saved is not None:
            saved["loop"] = config_mod.normalize_loop(saved.get("loop"))
            saved["budget"] = config_mod.normalize_budget(saved.get("budget"))
            return saved
    # Runs created before configuration snapshots used defaults or --config.
    return config_mod.load_config(None)


def _backends(roles, args, fake_script=None):
    return backends_mod.build_backends(
        roles, allow_fake=bool(getattr(args, "allow_fake_backend", False)),
        keep_sessions=bool(getattr(args, "keep_sessions", False)),
        fake_responses=fake_script)


def _fake_script(args):
    path = getattr(args, "fake_script", None)
    if not path:
        return None
    data = _read_json(path, "fake 脚本")
    if not isinstance(data, dict):
        raise SystemExit("fake 脚本必须是 {role: [response...]} 对象")
    return data


def _install_signals(cancel_event: threading.Event):
    old = {}

    def handler(signum, frame):
        cancel_event.set()
        log("收到中断信号，正在停止子进程并保存状态……")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    return old


def _restore_signals(old):
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _print_status(summary: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"run_id: {summary.get('run_id')}")
    print(f"状态: {summary.get('status')}  模式: {summary.get('mode')}  轮次: {summary.get('round')}")
    print(f"目标: {summary.get('goal')}")
    print("任务: " + ", ".join(f"{k}={v}" for k, v in (summary.get("task_counts") or {}).items()))
    budget = summary.get("budget") or {}
    print(f"费用: ${budget.get('cost_usd_known', 0)}（覆盖 {budget.get('cost_coverage')}，"
          f"未知调用 {budget.get('cost_unknown_calls', 0)}，外部未计量 {budget.get('external_unmetered', 0)}）；"
          f"调用 {budget.get('calls', 0)}；tokens {budget.get('tokens', 0)}")
    if summary.get("packet"):
        print(f"决策材料: {summary['packet']}")
    research = summary.get("research") or {}
    if research.get("enabled"):
        print(f"研究模式: 待审查 {research.get('pending_reviews', 0)}；"
              f"假设 " + json.dumps(research.get("hypotheses") or {}, ensure_ascii=False))
    if summary.get("triggers"):
        print("触发: " + "；".join(
            f"{t.get('kind')}: {t.get('detail', '')}" for t in summary["triggers"]))
    if summary.get("stop_reason"):
        print(f"停止原因: {summary['stop_reason']}")
    if summary.get("question"):
        print(f"需要用户输入: {summary['question']}")
    if summary.get("completion"):
        print("完成验证: " + json.dumps(summary["completion"], ensure_ascii=False))


def cmd_start(args) -> int:
    goal_raw = _read_json(args.goal, "目标文件") if args.goal else {"goal": args.goal_text,
                                                                  "completion_criteria": args.criteria}
    if args.goal is None and not args.criteria:
        raise SystemExit("start 需要 --goal 文件，或 --goal-text 加至少一个 --criteria")
    goal = config_mod.normalize_goal(goal_raw)
    cfg = config_mod.load_config(args.config)
    roles = _roles(cfg, args.allow_fake_backend)
    script = _fake_script(args)
    workspace = dict(goal.get("workspace") or cfg["loop"].get("workspace") or {})
    if args.workspace:
        workspace["repo"] = args.workspace
    if args.isolate:
        workspace["isolate"] = args.isolate
    if args.allow_unversioned_code:
        workspace["require_git_for_code"] = False
    if getattr(args, "research", False):
        goal["research"] = True
    try:
        workspace = config_mod.validate_workspace(workspace)
    except config_mod.ConfigError as exc:
        raise SystemExit(str(exc))
    backends = _backends(roles, args, script)   # fail early on missing keys/config
    store = state_mod.RunStore(args.run_dir)
    engine = engine_mod.Engine(store, cfg, roles, backends, mode=args.mode)
    state = engine.start(goal, workspace)
    print(json.dumps({
        "run_id": state["run_id"], "run_dir": str(store.dir),
        "status": state["status"], "packet": state.get("pending_packet"),
        "workspace": state.get("workspace"),
    }, ensure_ascii=False, indent=2))
    return 0


def _open_for_read(store, cfg, roles, *, need_backends=False, args=None, script=None):
    engine = engine_mod.Engine(
        store, cfg, roles,
        _backends(roles, args, script) if need_backends else {},
        mode="external")
    engine.open()
    return engine


def cmd_next(args) -> int:
    cfg = _load_run_config(args)
    roles = _roles(cfg, args.allow_fake_backend)
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    with store.lock():
        engine = _open_for_read(store, cfg, roles)
        recovering = (engine.state.get("status") == "executing"
                      or any(t.get("status") == "running"
                             for t in (engine.state.get("tasks") or {}).values()))
        engine.recover()
        if engine.state.get("status") in ("complete", "paused", "needs_input", "cancelled"):
            _print_status(engine.status_summary(), args.json)
            return 0 if engine.state.get("status") == "complete" else 1
        packet = engine.current_packet()
        if recovering or packet is None:
            packet = engine._build_and_store_packet()
        if not packet:
            _print_status(engine.status_summary(), args.json)
            return 1
        if args.json:
            print(json.dumps(packet, ensure_ascii=False, indent=2))
        else:
            print(f"决策材料：{store.dir / engine.state['pending_packet']}")
            print(f"轮次 {packet.get('round')}；触发："
                  + "；".join(f"{t.get('kind')}: {t.get('detail', '')}"
                             for t in (packet.get("decision_needed") or {}).get("triggers", [])))
            print(json.dumps(packet, ensure_ascii=False, indent=2))
        return 0


def cmd_decide(args) -> int:
    raw = _read_json(args.decision, "决策文件")
    cfg = _load_run_config(args)
    roles = _roles(cfg, args.allow_fake_backend)
    script = _fake_script(args)
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    cancel_event = threading.Event()
    old = _install_signals(cancel_event)
    try:
        with store.lock():
            engine = engine_mod.Engine(store, cfg, roles, _backends(roles, args, script),
                                       mode="external", cancel_event=cancel_event)
            engine.open()
            if engine.state.get("status") in ("complete", "cancelled"):
                _print_status(engine.status_summary(), args.json)
                return 1
            if engine.state.get("mode") != "external":
                engine.state["mode"] = "external"
                engine.commit("mode_changed", {"mode": "external"},
                              summary="switching to external host mode")
            try:
                decision = protocol.normalize_decision(
                    raw, engine.state["goal"]["criteria"],
                    int(engine.state.get("round", 0)) + 1,
                    set(engine.state.get("tasks") or {}))
                engine.validate_decision(decision)
            except protocol.DecisionError as exc:
                print(json.dumps({"accepted": False, "error": str(exc),
                                  "status": engine.state.get("status")},
                                 ensure_ascii=False, indent=2))
                return 2
            try:
                out = engine.apply_decision(decision, external=True)
            except protocol.DecisionError as exc:
                print(json.dumps({"accepted": False, "error": str(exc)},
                                 ensure_ascii=False, indent=2))
                return 2
            summary = engine.status_summary()
            summary["accepted"] = True
            summary["next"] = out
            _print_status(summary, args.json)
            return 0
    finally:
        _restore_signals(old)


def cmd_auto(args) -> int:
    cfg = _load_run_config(args)
    roles = _roles(cfg, args.allow_fake_backend)
    script = _fake_script(args)
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    cancel_event = threading.Event()
    old = _install_signals(cancel_event)
    try:
        with store.lock():
            engine = engine_mod.Engine(store, cfg, roles, _backends(roles, args, script),
                                       mode="auto", cancel_event=cancel_event)
            engine.open()
            if engine.state.get("mode") != "auto":
                engine.state["mode"] = "auto"
                engine.commit("mode_changed", {"mode": "auto"}, summary="switching to auto mode")
            summary = engine.run_auto(max_steps=args.max_steps)
            _print_status(summary, args.json)
            return 0 if summary.get("status") == "complete" else 1
    finally:
        _restore_signals(old)


def cmd_resume(args) -> int:
    cfg = _load_run_config(args)
    roles = _roles(cfg, args.allow_fake_backend)
    script = _fake_script(args)
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    cancel_event = threading.Event()
    old = _install_signals(cancel_event)
    try:
        with store.lock():
            engine = engine_mod.Engine(store, cfg, roles, _backends(roles, args, script),
                                       mode="external", cancel_event=cancel_event)
            engine.open()
            engine.recover()
            if engine.state.get("status") == "complete" or engine.state.get("status") == "cancelled":
                summary = engine.status_summary()
            elif engine.state.get("mode") == "auto" and engine.state.get("status") not in (
                    "paused", "needs_input"):
                summary = engine.run_auto(max_steps=args.max_steps)
            else:
                engine.run_until_decision()
                summary = engine.status_summary()
            _print_status(summary, args.json)
            return 0 if summary.get("status") == "complete" else 1
    finally:
        _restore_signals(old)


def cmd_status(args) -> int:
    try:
        cfg = _load_run_config(args)
        roles = _roles(cfg, args.allow_fake_backend)
    except (config_mod.ConfigError, SystemExit, ValueError):
        cfg = config_mod.load_config(None)
        roles = {}
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    engine = _open_for_read(store, cfg, roles)
    summary = engine.status_summary()
    _print_status(summary, args.json)
    return 0


def cmd_cancel(args) -> int:
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    try:
        cfg = _load_run_config(args)
        roles = _roles(cfg, args.allow_fake_backend)
    except (config_mod.ConfigError, SystemExit, ValueError):
        # Cancelling must not depend on a valid model configuration.
        cfg = config_mod.load_config(None)
        roles = {}
    try:
        with store.lock():
            engine = _open_for_read(store, cfg, roles)
            if engine.state.get("status") in ("complete", "cancelled"):
                _print_status(engine.status_summary(), args.json)
                return 0
            engine._cancel_state(args.reason or "用户取消")
            engine.commit("cancelled", {}, summary=args.reason or "用户取消")
            _print_status(engine.status_summary(), args.json)
        return 0
    except state_mod.WriterLocked:
        pid = store.writer_pid()
        store.request_cancel(args.reason)
        if store.pid_alive(pid) and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(json.dumps({"cancel_requested": True, "writer_pid": pid,
                          "note": "已写入取消标记并通知活动进程"}, ensure_ascii=False))
        return 0


def cmd_artifacts(args) -> int:
    try:
        cfg = _load_run_config(args)
        roles = _roles(cfg, args.allow_fake_backend)
    except (config_mod.ConfigError, SystemExit, ValueError):
        cfg = config_mod.load_config(None)
        roles = {}
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    engine = _open_for_read(store, cfg, roles)
    items = engine.artifacts_list()
    if args.task:
        items = [i for i in items if i["task"] == args.task]
    if args.show:
        target = next((i for i in items if i["task"] == args.show), None)
        if target is None:
            raise SystemExit(f"没有找到任务 {args.show} 的产物")
        print((store.dir / target["path"]).read_text(encoding="utf-8"))
        return 0
    print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args) -> int:
    raw = _read_json(args.decision, "决策文件")
    cfg = _load_run_config(args)
    roles = _roles(cfg, args.allow_fake_backend)
    store = state_mod.RunStore(args.run_dir)
    if not store.exists():
        raise SystemExit(f"运行目录不存在或没有状态：{store.dir}")
    engine = _open_for_read(store, cfg, roles)
    try:
        decision = protocol.normalize_decision(
            raw, engine.state["goal"]["criteria"],
            int(engine.state.get("round", 0)) + 1,
            set(engine.state.get("tasks") or {}))
        engine.validate_decision(decision)
        if decision.get("_completion_errors"):
            print(json.dumps({"valid": False, "completion_errors": decision["_completion_errors"],
                              "decision": decision}, ensure_ascii=False, indent=2, default=str))
            return 2
    except protocol.DecisionError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"valid": True, "decision": decision}, ensure_ascii=False, indent=2,
                     default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Persistent loop: Claude planner, DeepSeek workers, controller-owned "
                    "acceptance, state, budgets and recovery.")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, *, exec_required=False):
        p.add_argument("--config", help="roles/budget/loop config JSON")
        p.add_argument("--allow-fake-backend", action="store_true",
                       help="offline tests only: allow fake backends; not a model evaluation")
        p.add_argument("--fake-script", help="offline tests only: JSON {role: [response...]}")
        p.add_argument("--json", action="store_true", help="print JSON")

    s = sub.add_parser("start", help="create a run; write initial state and decision packet")
    s.add_argument("run_dir")
    s.add_argument("--goal", help="goal JSON file")
    s.add_argument("--goal-text")
    s.add_argument("--criteria", action="append", default=[],
                   help="completion criterion (repeatable; only with --goal-text)")
    s.add_argument("--workspace", help="git repository for code tasks")
    s.add_argument("--isolate", choices=("worktree",),
                   help="code isolation; currently only worktree is supported")
    s.add_argument("--allow-unversioned-code", action="store_true",
                   help="allow non-git code workspaces (no patch evidence)")
    s.add_argument("--mode", choices=("external", "auto"), default="external",
                   help="external host (default) or controller-driven planner")
    s.add_argument("--research", action="store_true",
                   help="enable the minimal research ledger and direction review")
    s.add_argument("--keep-sessions", action="store_true")
    common(s)
    s.set_defaults(func=cmd_start)

    n = sub.add_parser("next", help="print the current packet; read-only except recovery")
    n.add_argument("run_dir")
    n.add_argument("--config")
    n.add_argument("--allow-fake-backend", action="store_true")
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_next)

    d = sub.add_parser("decide", help="validate/apply a decision, execute, write the next packet")
    d.add_argument("run_dir")
    d.add_argument("decision")
    d.add_argument("--config")
    d.add_argument("--allow-fake-backend", action="store_true")
    d.add_argument("--fake-script")
    d.add_argument("--keep-sessions", action="store_true")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_decide)

    a = sub.add_parser("auto", help="run the loop with the configured planner backend")
    a.add_argument("run_dir")
    a.add_argument("--config")
    a.add_argument("--allow-fake-backend", action="store_true")
    a.add_argument("--fake-script")
    a.add_argument("--keep-sessions", action="store_true")
    a.add_argument("--max-steps", type=int, help="max planner/execution steps; default from config")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_auto)

    r = sub.add_parser("resume", help="recover an interrupted run and continue")
    r.add_argument("run_dir")
    r.add_argument("--config")
    r.add_argument("--allow-fake-backend", action="store_true")
    r.add_argument("--fake-script")
    r.add_argument("--keep-sessions", action="store_true")
    r.add_argument("--max-steps", type=int)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_resume)

    st = sub.add_parser("status", help="show state, budget, triggers and the next packet")
    st.add_argument("run_dir")
    st.add_argument("--config")
    st.add_argument("--allow-fake-backend", action="store_true")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("cancel", help="cancel a run (safe marker + signal for an active writer)")
    c.add_argument("run_dir")
    c.add_argument("--reason", default="用户取消")
    c.add_argument("--config")
    c.add_argument("--allow-fake-backend", action="store_true")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_cancel)

    ar = sub.add_parser("artifacts", help="list or print task artifacts and patches")
    ar.add_argument("run_dir")
    ar.add_argument("--task", help="only this task")
    ar.add_argument("--show", help="print this task's latest artifact")
    ar.add_argument("--config")
    ar.add_argument("--allow-fake-backend", action="store_true")
    ar.set_defaults(func=cmd_artifacts)

    v = sub.add_parser("validate", help="validate a decision and refreshed completion evidence without applying it")
    v.add_argument("run_dir")
    v.add_argument("decision")
    v.add_argument("--config")
    v.add_argument("--allow-fake-backend", action="store_true")
    v.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
