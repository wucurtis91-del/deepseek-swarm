#!/usr/bin/env python3
"""deepseek-swarm: run several DeepSeek-backed Claude Code workers in parallel.

The planner (you, or a Claude session) writes a tasks file; this script starts one
headless `claude -p` worker per task, all pointed at DeepSeek's Anthropic-compatible
endpoint, and collects every result into an output folder plus a summary.

Workers run in Claude Code's --bare mode: no CLAUDE.md, memory, hooks, plugins or
MCP servers are loaded, so nothing personal leaks to the worker and each call starts
from ~1k tokens of overhead instead of tens of thousands.

Usage:
  swarm.py check                         verify the DeepSeek key with one tiny call
  swarm.py run TASKS.json [options]      run all tasks in parallel
  swarm.py run TASKS.json --only a,b     rerun selected tasks

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"

# USD per 1M tokens, peak rates from api-docs.deepseek.com (checked 2026-10-22).
# Off-peak is half. Only used for a rough estimate in the summary.
PRICES = {
    "deepseek-flash": {"in": 0.30, "cache": 0.006, "out": 1.20},
    "deepseek-v4-pro": {"in": 1.32, "cache": 0.044, "out": 3.96},
}

# Tool presets for workers. "edit" and "bash" let a worker change files, so give
# each task its own output files to avoid two workers fighting over one file.
TOOL_PRESETS = {
    "none": ([], None),
    "read": (["Read", "Grep", "Glob"], None),
    "edit": (["Read", "Grep", "Glob", "Edit", "Write"], "acceptEdits"),
    "bash": (["Read", "Grep", "Glob", "Edit", "Write", "Bash"], "bypassPermissions"),
}

WORKER_RULES = """\
你是并行任务中的一个执行者（worker）。规划者已经把总任务拆开，你只负责下面这一块。
- 只做你这一块，不要扩展范围。
- 拿不准的地方明确写「不确定」，不要编造。
- 直接输出最终结果正文，不要寒暄，不要复述任务。"""

print_lock = threading.Lock()


def log(msg: str) -> None:
    with print_lock:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- key lookup

def key_from_cc_switch() -> str | None:
    """Read the DeepSeek key CC Switch stored for Claude Code, if any."""
    db = Path.home() / ".cc-switch" / "cc-switch.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select settings_config from providers where app_type='claude'"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    for (cfg,) in rows:
        try:
            env = json.loads(cfg).get("env", {})
        except (TypeError, ValueError):
            continue
        if "deepseek.com" in env.get("ANTHROPIC_BASE_URL", ""):
            key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            if key:
                return key
    return None


def get_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY") or key_from_cc_switch()
    if not key:
        sys.exit(
            "找不到 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，"
            "或在 CC Switch 里为 Claude Code 添加 DeepSeek 供应商。"
        )
    return key


# ---------------------------------------------------------------- worker

def find_claude() -> str:
    path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(path).exists():
        sys.exit("找不到 claude 命令行（Claude Code CLI）。")
    return path


def worker_env(key: str) -> dict:
    """A clean environment: nothing inherited from the calling Claude session."""
    home = str(Path.home())
    return {
        "HOME": home,
        "USER": os.environ.get("USER", ""),
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "SHELL": "/bin/zsh",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "TERM": "dumb",
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_API_KEY": key,
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def build_prompt(task: dict, shared: str) -> str:
    parts = [WORKER_RULES]
    if shared:
        parts.append("## 共同背景\n" + shared.strip())
    parts.append(f"## 你的任务（{task['id']}）\n" + task["prompt"].strip())
    return "\n\n".join(parts)


def build_cmd(claude: str, task: dict, args) -> list[str]:
    tools, mode = TOOL_PRESETS[task.get("tools", args.tools)]
    cmd = [
        claude, "-p",
        "--bare",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--model", task.get("model", args.model),
        "--output-format", "json",
        "--max-turns", str(task.get("max_turns", args.max_turns)),
        "--tools", ",".join(tools),
    ]
    if mode:
        cmd += ["--permission-mode", mode]
    for d in task.get("add_dirs", []):
        cmd += ["--add-dir", str(Path(d).expanduser())]
    if not args.keep_sessions:
        cmd.append("--no-session-persistence")
    return cmd


def estimate_cost(usage: dict, model: str, when: dt.datetime) -> float:
    p = PRICES.get(model.split("[")[0], PRICES[DEFAULT_MODEL])
    peak = when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10)
    factor = 1.0 if peak else 0.5
    cost = (
        usage.get("input_tokens", 0) * p["in"]
        + usage.get("cache_read_input_tokens", 0) * p["cache"]
        + usage.get("output_tokens", 0) * p["out"]
    ) / 1e6
    return round(cost * factor, 6)


def run_task(task: dict, shared: str, args, claude: str, env: dict, out: Path) -> dict:
    tid = task["id"]
    cwd = Path(task.get("cwd", args.cwd or os.getcwd())).expanduser()
    prompt = build_prompt(task, shared)
    cmd = build_cmd(claude, task, args)
    rec = {"id": tid, "status": "failed", "attempts": 0}
    # Drop results from an earlier run so a failed rerun never leaves stale output.
    for stale in (out / f"{tid}.md", out / f"{tid}.json"):
        stale.unlink(missing_ok=True)
    if not cwd.is_dir():
        rec["error"] = f"工作目录不存在：{cwd}"
        log(f"[{tid}] 失败：{rec['error']}")
        return rec
    for attempt in range(1, args.retries + 2):
        rec["attempts"] = attempt
        start = time.time()
        log(f"[{tid}] 开始（第 {attempt} 次）")
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                cwd=cwd, env=env, timeout=task.get("timeout", args.timeout),
            )
        except subprocess.TimeoutExpired:
            rec.update(error=f"超时（{args.timeout}s）", seconds=round(time.time() - start, 1))
            log(f"[{tid}] 超时")
            continue
        rec["seconds"] = round(time.time() - start, 1)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            rec["error"] = (proc.stderr or proc.stdout).strip()[-500:] or f"退出码 {proc.returncode}"
            log(f"[{tid}] 输出无法解析：{rec['error'][:200]}")
            continue
        (out / f"{tid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("is_error"):
            rec["error"] = str(data.get("result") or data.get("subtype"))[:500]
            log(f"[{tid}] 失败：{rec['error'][:200]}")
            continue
        result = data.get("result") or ""
        (out / f"{tid}.md").write_text(result)
        usage = data.get("usage", {})
        rec.update(
            status="ok",
            error=None,
            turns=data.get("num_turns"),
            input_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            est_cost_usd=estimate_cost(usage, task.get("model", args.model), dt.datetime.now(dt.timezone.utc)),
            result_file=f"{tid}.md",
            chars=len(result),
        )
        log(f"[{tid}] 完成：{rec['seconds']}s，约 ${rec['est_cost_usd']}")
        break
    return rec


# ---------------------------------------------------------------- commands

def load_tasks(path: Path) -> tuple[list[dict], str]:
    spec = json.loads(path.read_text())
    tasks = spec["tasks"] if isinstance(spec, dict) else spec
    shared = spec.get("shared_context", "") if isinstance(spec, dict) else ""
    ids = set()
    for i, t in enumerate(tasks):
        t.setdefault("id", f"task{i + 1}")
        if t["id"] in ids:
            sys.exit(f"任务 id 重复：{t['id']}")
        if "prompt" not in t:
            sys.exit(f"任务 {t['id']} 缺少 prompt")
        if t.get("tools", "read") not in TOOL_PRESETS:
            sys.exit(f"任务 {t['id']} 的 tools 只能是 {list(TOOL_PRESETS)}")
        ids.add(t["id"])
    return tasks, shared


def cmd_run(args) -> int:
    tasks, shared = load_tasks(Path(args.tasks))
    if args.only:
        want = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in want]
    if not tasks:
        sys.exit("没有要运行的任务。")
    out = Path(args.out or Path(args.tasks).with_suffix("").as_posix() + "-results").expanduser()
    out.mkdir(parents=True, exist_ok=True)
    claude, env = find_claude(), worker_env(get_key())

    log(f"并行运行 {len(tasks)} 个任务，最多同时 {args.concurrency} 个，结果写入 {out}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(run_task, t, shared, args, claude, env, out) for t in tasks]
        records = [f.result() for f in futs]

    summary_path = out / "summary.json"
    old = {}
    if args.only and summary_path.exists():
        old = {r["id"]: r for r in json.loads(summary_path.read_text()).get("tasks", [])}
    old.update({r["id"]: r for r in records})
    merged = list(old.values())
    summary = {
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "wall_seconds": round(time.time() - t0, 1),
        "ok": sum(r["status"] == "ok" for r in merged),
        "failed": [r["id"] for r in merged if r["status"] != "ok"],
        "est_total_cost_usd": round(sum(r.get("est_cost_usd", 0) for r in merged), 4),
        "tasks": merged,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")
    return 0 if not summary["failed"] else 1


def cmd_check(args) -> int:
    claude, env = find_claude(), worker_env(get_key())
    cmd = [claude, "-p", "--bare", "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--model", args.model, "--output-format", "json",
           "--tools", ""]
    proc = subprocess.run(cmd, input="只回复两个字母：OK", capture_output=True,
                          text=True, env=env, timeout=120, cwd=Path.home())
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        print("失败：", (proc.stderr or proc.stdout)[-500:])
        return 1
    ok = not data.get("is_error")
    print(json.dumps({
        "ok": ok,
        "reply": data.get("result"),
        "models": list(data.get("modelUsage", {})),
        "input_tokens": data.get("usage", {}).get("input_tokens"),
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Run DeepSeek-backed Claude Code workers in parallel.")
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="verify the DeepSeek key with one tiny call")
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="run every task in a tasks file in parallel")
    r.add_argument("tasks", help="tasks JSON file")
    r.add_argument("--out", help="output folder (default: <tasks>-results next to the file)")
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--tools", default="read", choices=list(TOOL_PRESETS),
                   help="default tool preset for tasks that do not set one (default: read)")
    r.add_argument("--cwd", help="default working directory for workers")
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--timeout", type=int, default=900, help="seconds per attempt")
    r.add_argument("--retries", type=int, default=1)
    r.add_argument("--only", help="comma-separated task ids to (re)run")
    r.add_argument("--keep-sessions", action="store_true",
                   help="save worker sessions so they show up in /resume")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
