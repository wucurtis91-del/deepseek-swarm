#!/usr/bin/env python3
"""deepseek-swarm: run several DeepSeek-backed Claude Code workers in parallel.

The planner (you, or a Claude session) writes a tasks file; this script starts one
headless `claude -p` worker per task, all pointed at DeepSeek's Anthropic-compatible
endpoint, and collects every result into an output folder plus a summary.

Quality loop (on by default when a task lists `acceptance` criteria):
  1. a worker produces the result and a short self-check;
  2. an independent reviewer worker checks it against the acceptance criteria and
     the source files, re-doing calculations, and returns a pass/fail verdict;
  3. a failed result goes back to a worker with the reviewer's notes, up to
     --max-rework times.

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
import re
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
- 只做你这一块，不要扩展范围，但这一块要做完整：验收标准里的每一条都要满足。
- 需要的信息先从给定文件里读，不要凭印象。拿不准的地方明确写「不确定」，不要编造。
- 涉及计算的，写出中间步骤，并在给出结果前自己验算一遍。
- 直接输出最终结果正文，不要寒暄，不要复述任务。"""

SELFCHECK_RULES = """\
正文结束后，另起一行，输出一个自查代码块（规划者会把它从正文里拿掉），格式严格如下：
```selfcheck
{"met": ["已满足的验收标准，简写"], "not_met": ["没做到的，说明原因"], "uncertain": ["没把握的具体地方"]}
```"""

REVIEW_RULES = """\
你是独立复核员（reviewer）。另一个执行者完成了下面的任务，你要严格检查它的结果。
- 逐条对照验收标准判断是否满足；只要有一条没满足，就不能通过。
- 自己从源文件读取信息核对，不要相信结果里的说法；所有计算都独立重算一遍。
- 指出具体错误：哪一处、错在哪、正确应是什么。不要泛泛而谈。
- 不要自己重写整份结果。

最后只输出一个 JSON 代码块，格式严格如下：
```json
{"pass": true或false,
 "criteria": [{"criterion": "标准原文", "met": true或false, "note": "依据或问题"}],
 "errors": ["具体错误，含位置和正确内容"],
 "fix_instructions": "给执行者的修改指示；通过时写空字符串"}
```"""

REWORK_RULES = """\
你上一版的结果没有通过独立复核。根据下面的复核意见修改，输出**完整的新版本**
（不是修改说明，也不是只给改动部分）。复核意见也可能有错：如果你核对源文件后
确认自己原来是对的，就保留原内容，并在自查块的 uncertain 里写明分歧。"""

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


# ---------------------------------------------------------------- one call

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


class Runner:
    """Runs single `claude -p` calls with shared settings."""

    def __init__(self, args):
        self.args = args
        self.claude = find_claude()
        self.env = worker_env(get_key())

    def call(self, prompt: str, task: dict, tools: str, model: str) -> dict:
        """One headless call. Returns {ok, text, usage, turns, seconds, error}."""
        a = self.args
        tool_list, mode = TOOL_PRESETS[tools]
        cmd = [
            self.claude, "-p", "--bare",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--model", model,
            "--output-format", "json",
            "--max-turns", str(task.get("max_turns", a.max_turns)),
            "--tools", ",".join(tool_list),
        ]
        if mode:
            cmd += ["--permission-mode", mode]
        for d in task.get("add_dirs", []):
            cmd += ["--add-dir", str(Path(d).expanduser())]
        if not a.keep_sessions:
            cmd.append("--no-session-persistence")
        cwd = Path(task.get("cwd", a.cwd or os.getcwd())).expanduser()
        start = time.time()
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                cwd=cwd, env=self.env, timeout=task.get("timeout", a.timeout),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"超时（{task.get('timeout', a.timeout)}s）",
                    "seconds": round(time.time() - start, 1)}
        seconds = round(time.time() - start, 1)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            err = (proc.stderr or proc.stdout).strip()[-500:] or f"退出码 {proc.returncode}"
            return {"ok": False, "error": err, "seconds": seconds}
        if data.get("is_error"):
            return {"ok": False, "error": str(data.get("result") or data.get("subtype"))[:500],
                    "seconds": seconds, "usage": data.get("usage", {})}
        return {"ok": True, "text": data.get("result") or "", "usage": data.get("usage", {}),
                "turns": data.get("num_turns"), "seconds": seconds}


# ---------------------------------------------------------------- prompts

def criteria_block(task: dict) -> str:
    items = task.get("acceptance") or []
    if not items:
        return ""
    return "## 验收标准（全部满足才算完成）\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))


def worker_prompt(task: dict, shared: str, prev: str | None = None, review: dict | None = None) -> str:
    parts = [WORKER_RULES]
    if shared:
        parts.append("## 共同背景\n" + shared.strip())
    parts.append(f"## 你的任务（{task['id']}）\n" + task["prompt"].strip())
    if task.get("acceptance"):
        parts.append(criteria_block(task))
    if prev is not None:
        parts.append(REWORK_RULES)
        parts.append("## 你上一版的结果\n" + prev)
        parts.append("## 复核意见\n" + json.dumps(review, ensure_ascii=False, indent=2))
    parts.append(SELFCHECK_RULES)
    return "\n\n".join(parts)


def review_prompt(task: dict, shared: str, result: str) -> str:
    parts = [REVIEW_RULES]
    if shared:
        parts.append("## 共同背景（执行者收到的）\n" + shared.strip())
    parts.append("## 执行者收到的任务\n" + task["prompt"].strip())
    parts.append(criteria_block(task))
    parts.append("## 执行者交回的结果\n" + result)
    return "\n\n".join(parts)


FENCE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.S)


def split_selfcheck(text: str) -> tuple[str, dict | None]:
    """Remove the trailing selfcheck block from a worker result."""
    for m in reversed(list(FENCE.finditer(text))):
        if m.group(1) == "selfcheck":
            try:
                check = json.loads(m.group(2))
            except ValueError:
                check = {"raw": m.group(2).strip()}
            return (text[:m.start()] + text[m.end():]).strip(), check
    return text.strip(), None


def parse_review(text: str) -> dict:
    blocks = [m.group(2) for m in FENCE.finditer(text) if m.group(1) in (None, "json")]
    for raw in reversed(blocks or [text]):
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "pass" in data:
                data["pass"] = bool(data["pass"])
                return data
        except ValueError:
            continue
    return {"pass": False, "errors": ["复核员没有给出可解析的结论"], "fix_instructions": "",
            "unparsed": text[-1500:]}


# ---------------------------------------------------------------- one task

def estimate_cost(usage: dict, model: str, when: dt.datetime) -> float:
    p = PRICES.get(model.split("[")[0], PRICES[DEFAULT_MODEL])
    peak = when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10)
    factor = 1.0 if peak else 0.5
    cost = (
        usage.get("input_tokens", 0) * p["in"]
        + usage.get("cache_read_input_tokens", 0) * p["cache"]
        + usage.get("output_tokens", 0) * p["out"]
    ) / 1e6
    return cost * factor


def run_task(task: dict, shared: str, runner: Runner, out: Path) -> dict:
    a = runner.args
    tid = task["id"]
    model = task.get("model", a.model)
    review_model = task.get("review_model", a.review_model or model)
    do_review = a.review and bool(task.get("acceptance"))
    rec = {"id": tid, "status": "failed", "rounds": 0, "cost_usd": 0.0, "seconds": 0.0}
    # Drop results from an earlier run so a failed rerun never leaves stale output.
    for stale in out.glob(f"{glob_escape(tid)}.*"):
        stale.unlink()
    cwd = Path(task.get("cwd", a.cwd or os.getcwd())).expanduser()
    if not cwd.is_dir():
        rec["error"] = f"工作目录不存在：{cwd}"
        log(f"[{tid}] 失败：{rec['error']}")
        return rec

    def spend(res: dict, m: str) -> None:
        rec["cost_usd"] += estimate_cost(res.get("usage", {}), m, dt.datetime.now(dt.timezone.utc))
        rec["seconds"] += res.get("seconds", 0)

    def produce(prompt: str) -> dict:
        for attempt in range(a.retries + 1):
            res = runner.call(prompt, task, task.get("tools", a.tools), model)
            spend(res, model)
            if res["ok"]:
                return res
            log(f"[{tid}] 调用失败（第 {attempt + 1} 次）：{res['error'][:200]}")
        return res

    prev, review = None, None
    for rnd in range(1, a.max_rework + 2):
        rec["rounds"] = rnd
        log(f"[{tid}] 第 {rnd} 轮：执行")
        res = produce(worker_prompt(task, shared, prev, review))
        if not res["ok"]:
            rec["error"] = res["error"]
            break
        body, check = split_selfcheck(res["text"])
        (out / f"{tid}.v{rnd}.md").write_text(body)
        rec.update(selfcheck=check, chars=len(body), error=None)
        if not do_review:
            rec["status"] = "ok"
            break
        log(f"[{tid}] 第 {rnd} 轮：复核")
        rres = runner.call(review_prompt(task, shared, body), task, "read", review_model)
        spend(rres, review_model)
        review = parse_review(rres["text"]) if rres["ok"] else {
            "pass": False, "errors": [f"复核调用失败：{rres['error'][:300]}"], "fix_instructions": ""}
        (out / f"{tid}.v{rnd}.review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2))
        rec["review"] = {"pass": review["pass"], "errors": review.get("errors", [])}
        if review["pass"]:
            rec["status"] = "ok"
            log(f"[{tid}] 第 {rnd} 轮复核通过")
            break
        log(f"[{tid}] 第 {rnd} 轮复核未通过：{len(review.get('errors', []))} 处问题")
        prev = body
        rec["status"] = "unverified"   # has a result, but it never passed review

    # The newest version that was actually produced (a later round may have errored).
    final = next((out / f"{tid}.v{r}.md" for r in range(rec["rounds"], 0, -1)
                  if (out / f"{tid}.v{r}.md").exists()), None)
    if final:
        shutil.copyfile(final, out / f"{tid}.md")
        rec["result_file"] = f"{tid}.md"
    rec["cost_usd"] = round(rec["cost_usd"], 6)
    rec["seconds"] = round(rec["seconds"], 1)
    log(f"[{tid}] 结束：{rec['status']}，{rec['rounds']} 轮，约 ${rec['cost_usd']:.4f}")
    return rec


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", s)


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
        if not isinstance(t.get("acceptance", []), list):
            sys.exit(f"任务 {t['id']} 的 acceptance 必须是字符串列表")
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
    runner = Runner(args)

    reviewed = sum(1 for t in tasks if args.review and t.get("acceptance"))
    log(f"并行运行 {len(tasks)} 个任务（{reviewed} 个带复核，最多返工 {args.max_rework} 次），"
        f"最多同时 {args.concurrency} 个，结果写入 {out}")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = [pool.submit(run_task, t, shared, runner, out) for t in tasks]
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
        "passed": [r["id"] for r in merged if r["status"] == "ok"],
        "unverified": [r["id"] for r in merged if r["status"] == "unverified"],
        "failed": [r["id"] for r in merged if r["status"] == "failed"],
        "est_total_cost_usd": round(sum(r.get("cost_usd", 0) for r in merged), 4),
        "tasks": merged,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")
    return 0 if not (summary["failed"] or summary["unverified"]) else 1


def cmd_check(args) -> int:
    args.max_turns, args.timeout, args.keep_sessions, args.cwd = 2, 120, False, str(Path.home())
    res = Runner(args).call("只回复两个字母：OK", {}, "none", args.model)
    print(json.dumps({
        "ok": res["ok"],
        "reply": res.get("text") if res["ok"] else res.get("error"),
        "input_tokens": res.get("usage", {}).get("input_tokens"),
    }, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


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
    r.add_argument("--review-model", help="model for reviewers (default: same as --model)")
    r.add_argument("--tools", default="read", choices=list(TOOL_PRESETS),
                   help="default tool preset for tasks that do not set one (default: read)")
    r.add_argument("--cwd", help="default working directory for workers")
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--timeout", type=int, default=900, help="seconds per call")
    r.add_argument("--retries", type=int, default=1, help="retries per call on API/CLI errors")
    r.add_argument("--no-review", dest="review", action="store_false",
                   help="skip review even for tasks with acceptance criteria")
    r.add_argument("--max-rework", type=int, default=2,
                   help="how many times a result that fails review is sent back (default: 2)")
    r.add_argument("--only", help="comma-separated task ids to (re)run")
    r.add_argument("--keep-sessions", action="store_true",
                   help="save worker sessions so they show up in /resume")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
