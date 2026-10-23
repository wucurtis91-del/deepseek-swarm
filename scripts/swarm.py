#!/usr/bin/env python3
"""deepseek-swarm: run several DeepSeek-backed Claude Code workers in parallel.

The planner (you, or a Claude session) writes a tasks file; this script starts one
headless `claude -p` worker per task, all pointed at DeepSeek's Anthropic-compatible
endpoint, and collects every result into an output folder plus a summary.

Quality loop for each task:
  1. a worker produces the result and a short self-check;
  2. if the task sets `max_chars`, the script counts the result; an over-long one is
     sent back for compression, and one that stays too long fails without paying a
     reviewer;
  3. if the task sets `check_cmd` (tests, a linter...), the script runs it; a failing
     run goes back to the worker with the command's output;
  4. if the task lists `acceptance` criteria, an independent reviewer worker checks
     the result against them and the source files, re-doing calculations;
  5. a failed result goes back to a worker with the notes, up to --max-rework times.
     A review that itself fails (API error, unreadable verdict) is retried and never
     costs a rework round.

Tasks file extras: `inputs` turns one task into one task per matching file, and
`depends_on` makes a task wait for others and see their results.

Workers run in Claude Code's --bare mode: no CLAUDE.md, memory, hooks, plugins or
MCP servers are loaded, so nothing personal leaks to the worker and each call starts
from ~1k tokens of overhead instead of tens of thousands.

Usage:
  swarm.py check                         verify the DeepSeek key with one tiny call
  swarm.py plan TASKS.json [--show ID]   show expanded tasks and their order, no API calls
  swarm.py run TASKS.json [options]      run all tasks in parallel
  swarm.py run TASKS.json --only a,b     rerun selected tasks

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fnmatch
import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from swarmlib import procs as _procs            # noqa: E402
from swarmlib import security as _security      # noqa: E402
from swarmlib import util as _util              # noqa: E402
from swarmlib.backends import classify_error as _error_kind  # noqa: E402

BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"

# Claude Code stops a call at 32000 output tokens by default, and DeepSeek's reasoning
# counts towards it: in a 2026-10-23 run, 3 of 14 reviews died on that limit.
DEFAULT_MAX_OUTPUT_TOKENS = 64000

# Task ids become file names in the results folder (<id>.md, <id>.v1.md ...). No dots or
# slashes, so the stale-file glob "<id>.*" can never match another task's files.
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# How many times an over-long result is sent back for compression before review.
SHRINK_TRIES = 2

# check_cmd: the planner's command (tests, a linter...) run after each worker round.
DEFAULT_CHECK_TIMEOUT = 300
CHECK_OUTPUT_CHARS = 3000          # tail of the command's output sent back to the worker

# depends_on: how much of each upstream result goes straight into the prompt.
DEFAULT_UPSTREAM_CHARS = 6000

# inputs: an over-broad glob should stop the run, not start hundreds of workers.
DEFAULT_MAX_TASKS = 50
PLACEHOLDERS = ("{path}", "{name}", "{stem}", "{i}", "{n}")

# Upstream statuses a dependent task may build on. unverified, failed and blocked may not.
USABLE = ("ok", "unreviewed")

# USD per 1M tokens, peak rates from api-docs.deepseek.com (checked 2026-10-22).
# Off-peak is half. Only used for a rough estimate in the summary. Unknown models
# are reported as unknown, never priced with a cheaper default.
PRICES = {
    "deepseek-flash": {"in": 0.30, "cache": 0.006, "out": 1.20,
                       "source": "https://api-docs.deepseek.com/quick_start/pricing",
                       "updated": "2026-10-22"},
    "deepseek-v4-pro": {"in": 1.32, "cache": 0.044, "out": 3.96,
                        "source": "https://api-docs.deepseek.com/quick_start/pricing",
                        "updated": "2026-10-22"},
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
- 涉及计算的，给出结果前自己验算一遍；正文写关键的中间步骤，验算过程不用写出来。
- 多想少说：推理、草稿和验算放在思考过程里，正文只写结论和必要的关键步骤。
  不写过渡句、不复述任务、不加开场白和总结段。
- 有篇幅上限时必须遵守，宁可删解释也不要超。"""

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
- 多想少说：核对过程放在思考里，输出只写结论。每条 note 不超过 30 字，满足的写「符合」即可；
  errors 最多 8 条，每条不超过 80 字。

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

SHRINK_RULES = """\
下面这份结果超过了篇幅上限。把它压缩到上限以内，只输出压缩后的完整正文（不要自查块，不要说明）。
- 保留：每一条验收标准对应的内容、所有结论和数值、必要的关键步骤。
- 删掉：重复、过渡句、复述题目、泛泛的解释、开场白和总结段。
- 不要新增内容，不要改结论。"""

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
        if cfg is None:
            continue
        try:
            data = json.loads(cfg)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        env = data.get("env")
        if not isinstance(env, dict):
            continue
        base = env.get("ANTHROPIC_BASE_URL") or ""
        if "deepseek.com" in str(base):
            key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            if isinstance(key, str) and key:
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
    env = _security.minimal_env(extra={
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_API_KEY": key,
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    })
    env.update({
        "USER": os.environ.get("USER", ""),
        "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "SHELL": "/bin/zsh",
        "LANG": "en_US.UTF-8",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "TERM": "dumb",
    })
    return env


class Runner:
    """Runs single `claude -p` calls with shared settings."""

    def __init__(self, args):
        self.args = args
        self.claude = find_claude()
        self.env = worker_env(get_key())
        self.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(
            getattr(args, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))

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
        cwd = Path(task.get("cwd", a.cwd or os.getcwd())).expanduser().resolve()
        if not cwd.is_dir():
            return {"ok": False, "error": f"工作目录不存在：{cwd}", "seconds": 0.0,
                    "usage": {}, "usage_missing": True}
        proc = _procs.run(cmd, shell=False, cwd=cwd, env=self.env, input_text=prompt,
                          timeout=task.get("timeout", a.timeout))
        seconds = proc["seconds"]
        if proc.get("timed_out"):
            return {"ok": False, "error": f"超时（{task.get('timeout', a.timeout)}s）",
                    "seconds": seconds, "usage": {}, "usage_missing": True,
                    "error_kind": "timeout"}
        stdout = proc.get("stdout", "")
        try:
            data = json.loads(stdout)
        except ValueError:
            err = _security.redact((proc.get("output") or stdout).strip())[-500:] or (
                f"退出码 {proc.get('code')}")
            return {"ok": False, "error": err, "seconds": seconds,
                    "error_kind": _error_kind(err), "usage": {}, "usage_missing": True}
        if not isinstance(data, dict):
            return {"ok": False, "error": "CLI 返回的 JSON 不是对象", "seconds": seconds,
                    "error_kind": "config", "usage": {}, "usage_missing": True}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        observed = []
        if isinstance(data.get("model"), str):
            observed.append(data["model"])
        if isinstance(data.get("modelUsage"), dict):
            observed.extend(str(k) for k in data["modelUsage"])
        is_error = data.get("is_error")
        if is_error is not None and not isinstance(is_error, bool):
            return {"ok": False, "error": "CLI 返回的 is_error 不是布尔值", "seconds": seconds,
                    "error_kind": "config", "usage": usage,
                    "usage_missing": not bool(usage), "model_observed": sorted(set(observed))}
        if is_error is True:
            msg = _security.redact(str(data.get("result") or data.get("subtype") or "调用返回错误"))[:500]
            return {"ok": False, "error": msg, "seconds": seconds, "usage": usage,
                    "usage_missing": not bool(usage), "error_kind": _error_kind(msg),
                    "model_observed": sorted(set(observed))}
        text = data.get("result")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "CLI 返回了空结果", "seconds": seconds,
                    "error_kind": "empty_output", "usage": usage,
                    "usage_missing": not bool(usage),
                    "model_observed": sorted(set(observed))}
        return {"ok": True, "text": text, "usage": usage,
                "usage_missing": not bool(usage), "turns": data.get("num_turns"),
                "seconds": seconds, "model_observed": sorted(set(observed)),
                "total_cost_usd": data.get("total_cost_usd")}


# ---------------------------------------------------------------- prompts

def criteria_block(task: dict) -> str:
    items = task.get("acceptance") or []
    if not items:
        return ""
    return "## 验收标准（全部满足才算完成）\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1))


def count_chars(text: str) -> int:
    """Length as the workers are told it is counted: every non-whitespace character."""
    return len(re.sub(r"\s", "", text))


def length_block(limit: int | None) -> str:
    if not limit:
        return ""
    return (f"## 篇幅上限\n正文不超过 {limit} 字（程序按非空白字符计数，自查块不计），"
            f"目标写到 {int(limit * 0.8)} 字左右。超出会被退回压缩。")


def has_check(task: dict) -> bool:
    return bool(task.get("check_cmd") or task.get("check_argv"))


def check_label(task: dict) -> str:
    return task.get("check_cmd") or " ".join(task.get("check_argv") or [])


def check_block(task: dict) -> str:
    cmd = task.get("check_cmd") or " ".join(task.get("check_argv") or [])
    if not cmd:
        return ""
    form = "参数数组（不经过 shell）" if task.get("check_argv") else "shell 命令"
    return ("## 检查命令\n你交回后，程序会在工作目录运行下面的检查（" + form + "），退出码为 0 才算通过；"
            "没通过会把输出打回给你修改。有 Bash 工具就先自己运行一遍，没有就在交回前仔细核对。\n"
            f"```\n{cmd}\n```")


def upstream_block(task: dict, deps: dict, out: Path) -> str:
    """The results of the tasks this one depends on, each cut to upstream_chars."""
    cap = task.get("upstream_chars", DEFAULT_UPSTREAM_CHARS)
    parts = ["## 上游结果（你依赖的任务已经完成，下面是它们交回的正文）"]
    for dep, status in deps.items():
        path = out / f"{dep}.md"
        text = path.read_text() if path.exists() else "（没有结果文件）"
        if len(text) > cap:
            text = text[:cap] + f"\n……（已截断，全文在 {path}，需要时用 Read 读取）"
        note = "" if status == "ok" else "（未经复核，只能当草稿）"
        parts.append(f"### {dep}{note}\n{text}")
    return "\n\n".join(parts)


def worker_prompt(task: dict, shared: str, prev: str | None = None, review: dict | None = None,
                  limit: int | None = None) -> str:
    parts = [WORKER_RULES]
    if shared:
        parts.append("## 共同背景\n" + shared.strip())
    parts.append(f"## 你的任务（{task['id']}）\n" + task["prompt"].strip())
    if task.get("_upstream"):
        parts.append(task["_upstream"])
    if task.get("acceptance"):
        parts.append(criteria_block(task))
    if limit:
        parts.append(length_block(limit))
    if has_check(task):
        parts.append(check_block(task))
    if prev is not None:
        parts.append(REWORK_RULES)
        parts.append("## 你上一版的结果\n" + prev)
        parts.append("## 复核意见\n" + json.dumps(review, ensure_ascii=False, indent=2))
    parts.append(SELFCHECK_RULES)
    return "\n\n".join(parts)


def shrink_prompt(task: dict, body: str, limit: int) -> str:
    n = count_chars(body)
    target = int(limit * 0.85)
    parts = [SHRINK_RULES,
             f"## 篇幅\n现在 {n} 字（非空白字符），上限 {limit} 字。压到 {target} 字左右，"
             f"也就是删掉大约 {round(100 * (n - target) / n)}%。"]
    if task.get("acceptance"):
        parts.append(criteria_block(task))
    parts.append("## 要压缩的结果\n" + body)
    return "\n\n".join(parts)


def review_prompt(task: dict, shared: str, result: str, limit: int | None = None) -> str:
    parts = [REVIEW_RULES]
    if shared:
        parts.append("## 共同背景（执行者收到的）\n" + shared.strip())
    parts.append("## 执行者收到的任务\n" + task["prompt"].strip())
    if task.get("_upstream"):
        parts.append(task["_upstream"])
    parts.append(criteria_block(task))
    if limit:
        parts.append(f"## 篇幅\n程序已检查：正文 {count_chars(result)} 字，上限 {limit} 字，符合。"
                     "涉及字数的验收标准直接判满足，不要自己数字数。")
    if has_check(task):
        parts.append(f"## 检查命令\n程序已运行 `{check_label(task)}`，通过（退出码 0）。不用重复这项检查。")
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


def truthy(value) -> bool:
    """Only an explicit yes counts. bool("false") is True, so never use bool() here."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "pass", "passed", "通过", "是")
    if isinstance(value, (int, float)):
        return value == 1
    return False


def parse_review(text: str, criteria: list | None = None) -> dict:
    """Parse a reviewer verdict. With `criteria`, a pass must cover every criterion.

    A missing/extra criterion makes a *pass* unusable (the caller asks again), but a
    fail is still a usable verdict: being conservative cannot accept a bad result.
    """
    blocks = [m.group(2) for m in FENCE.finditer(text) if m.group(1) in (None, "json")]
    for raw in reversed(blocks or [text]):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not (isinstance(data, dict) and "pass" in data):
            continue
        data["pass"] = truthy(data["pass"])
        if not isinstance(data.get("errors"), list):
            return {"pass": False, "valid": False, "errors": ["errors 不是列表"],
                    "fix_instructions": "", "unparsed": text[-1500:]}
        data["errors"] = [str(e) for e in data.get("errors") or [] if str(e).strip()]
        items = data.get("criteria")
        if items is not None and not isinstance(items, list):
            return {"pass": False, "valid": False, "errors": ["criteria 不是列表"],
                    "fix_instructions": "", "unparsed": text[-1500:]}
        unmet = [c.get("criterion", "?") for c in (items or [])
                 if isinstance(c, dict) and "met" in c and not truthy(c["met"])]
        if data["pass"] and unmet:
            data["pass"] = False
            data["errors"].append("复核员判了通过，但这些标准写的是未满足：" + "；".join(map(str, unmet)))
        if data["pass"] and data["errors"]:
            data["pass"] = False
            data["errors"].append("errors 非空时不能判为通过")
        if criteria is not None and data["pass"]:
            expected = [re.sub(r"\s+", " ", str(c)).strip() for c in criteria]
            seen = []
            shape_ok = isinstance(items, list)
            if shape_ok:
                for item in items:
                    if not isinstance(item, dict):
                        shape_ok = False
                        break
                    label = item.get("criterion", item.get("id"))
                    if label is None:
                        shape_ok = False
                        break
                    seen.append(re.sub(r"\s+", " ", str(label)).strip())
            if not shape_ok:
                return {"pass": False, "valid": False,
                        "errors": ["复核响应缺少可用的 criteria 列表"],
                        "fix_instructions": "", "unparsed": text[-1500:]}
            missing = [c for c in expected if c not in seen]
            extra = [s for s in seen if s not in expected]
            if missing or extra:
                return {"pass": False, "valid": False,
                        "errors": ["复核没有逐条覆盖验收标准；缺少：" + "、".join(missing)
                                   + ("；多出：" + "、".join(extra) if extra else "")],
                        "fix_instructions": "", "unparsed": text[-1500:]}
        data["valid"] = True
        return data
    return {"pass": False, "valid": False, "errors": ["复核员没有给出可解析的结论"],
            "fix_instructions": "", "unparsed": text[-1500:]}


# ---------------------------------------------------------------- one task

def estimate_cost(usage: dict, model: str, when: dt.datetime) -> float | None:
    p = PRICES.get(model.split("[")[0])
    if p is None:
        return None
    if not isinstance(usage, dict) or usage.get("input_tokens") is None \
            or usage.get("output_tokens") is None:
        return None
    local = when.astimezone(dt.timezone(dt.timedelta(hours=8)))  # DeepSeek off-peak is 00:30-08:30 Beijing
    minutes = local.hour * 60 + local.minute
    off_peak = 30 <= minutes < 8 * 60 + 30
    factor = 0.5 if off_peak else 1.0
    try:
        return (
            usage.get("input_tokens", 0) * p["in"]
            + (usage.get("cache_read_input_tokens") or 0) * p["cache"]
            + usage.get("output_tokens", 0) * p["out"]
        ) / 1e6 * factor
    except (TypeError, ValueError):
        return None


def run_check(task: dict, cwd: Path, result_path: Path) -> dict:
    """Run the planner's check in the task's folder. {result} is this round's result file.

    `check_argv` is preferred and never goes through a shell. The legacy `check_cmd`
    still works: placeholders are replaced through environment variables when the
    value contains shell metacharacters, so a filename such as `$(touch X).txt`
    cannot execute a command substitution.
    """
    limit = task.get("check_timeout", DEFAULT_CHECK_TIMEOUT)
    values = dict(task.get("_placeholders") or {})
    values["{result}"] = str(Path(result_path).resolve())
    env = _security.minimal_env(extra={str(k): str(v)
                                       for k, v in (task.get("check_env") or {}).items()})
    secrets = tuple(str(v) for v in (task.get("check_env") or {}).values())
    start = time.time()
    argv = task.get("check_argv")
    if argv:
        if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)):
            return {"pass": False, "exit": None, "seconds": 0.0,
                    "output": "check_argv 必须是非空字符串列表"}
        filled = []
        for arg in argv:
            for name, value in values.items():
                arg = arg.replace(name, str(value))
            filled.append(arg)
        res = _procs.run(filled, shell=False, cwd=Path(cwd).resolve(), env=env,
                         timeout=limit, merge_output=True)
        shown = " ".join(filled)
    else:
        cmd = task.get("check_cmd") or ""
        try:
            cmd, extra_env = _procs.substitute_placeholders(cmd, values)
        except _procs.PlaceholderError as exc:
            return {"pass": False, "exit": None, "seconds": 0.0, "output": str(exc)}
        env = {**env, **extra_env}
        res = _procs.run(cmd, shell=True, cwd=Path(cwd).resolve(), env=env,
                         timeout=limit, merge_output=True)
        shown = cmd
    output = _security.redact(res.get("output", ""), secrets)[-CHECK_OUTPUT_CHARS:]
    code = res.get("code")
    if res.get("timed_out"):
        code = None
        output = f"超时（{limit}s），已停止。\n{output}"
    return {"pass": code == 0 and not res.get("cancelled"), "exit": code,
            "seconds": round(time.time() - start, 2), "output": output,
            "command": shown}


def run_task(task: dict, shared: str, runner: Runner, out: Path) -> dict:
    a = runner.args
    tid = task["id"]
    out = Path(out).expanduser().resolve()
    definition_hash = _util.hash_obj(
        {k: v for k, v in task.items() if not str(k).startswith("_")})
    deps = task.get("_deps") or {}
    if deps:
        # Upstream results go into both prompts, and the results folder becomes readable
        # so a worker can open an upstream result that was cut short.
        task = {**task, "_upstream": upstream_block(task, deps, out),
                "add_dirs": [*task.get("add_dirs", []), str(out)]}
    model = task.get("model", a.model)
    review_model = task.get("review_model", a.review_model or model)
    tools = task.get("tools", a.tools)
    limit = task.get("max_chars", a.max_chars)
    do_review = a.review and task.get("review", True) and bool(task.get("acceptance"))
    rec = {"id": tid, "status": "failed", "rounds": 0, "shrinks": 0, "cost_usd": 0.0,
           "cost_known": True, "cost_unknown_calls": 0, "usage_missing_calls": 0,
           "seconds": 0.0, "models": [], "calls": 0,
           "tokens": {"input": 0, "cache_read": 0, "output": 0}}
    rec["definition_hash"] = definition_hash
    rec["dep_versions"] = {
        dep: {"status": rec_dep.get("status"),
              "result_sha256": rec_dep.get("result_sha256"),
              "definition_hash": rec_dep.get("definition_hash"),
              "version": rec_dep.get("version")}
        for dep, rec_dep in (task.get("_dep_records") or {}).items()}
    # Drop results from an earlier run so a failed rerun never leaves stale output.
    for stale in out.glob(f"{glob_escape(tid)}.*"):
        stale.unlink()
    cwd = Path(task.get("cwd", a.cwd or os.getcwd())).expanduser().resolve()
    if not cwd.is_dir():
        rec["error"] = f"工作目录不存在：{cwd}"
        log(f"[{tid}] 失败：{rec['error']}")
        return rec

    def spend(res: dict, m: str) -> None:
        rec["calls"] += 1
        usage = res.get("usage", {}) if isinstance(res.get("usage"), dict) else {}
        if res.get("usage_missing") or not usage:
            rec["usage_missing_calls"] += 1
            rec["cost_unknown_calls"] += 1
            rec["cost_known"] = False
        else:
            cost = estimate_cost(usage, m, dt.datetime.now(dt.timezone.utc))
            if cost is None:
                rec["cost_unknown_calls"] += 1
                rec["cost_known"] = False
            else:
                rec["cost_usd"] += cost
        rec["seconds"] += res.get("seconds", 0)
        rec["tokens"]["input"] += usage.get("input_tokens", 0) or 0
        rec["tokens"]["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
        rec["tokens"]["output"] += usage.get("output_tokens", 0) or 0
        if m not in rec["models"]:
            rec["models"].append(m)

    def attempt(prompt: str, preset: str, m: str, what: str) -> dict:
        """One call, retried up to --retries times for transient errors only.

        Workers and reviewers both go through here. Auth/config errors are not
        retried, and a timed-out call that may have edited files is not replayed.
        """
        res = {"ok": False, "error": "未执行", "seconds": 0.0, "usage": {},
               "usage_missing": True}
        for i in range(max(a.retries, 0) + 1):
            if i:
                time.sleep(min(30, 5 * i))   # give a rate limit (429) a moment to clear
            try:
                res = runner.call(prompt, task, preset, m)
            except Exception as exc:
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "seconds": 0.0, "usage": {}, "usage_missing": True,
                       "error_kind": "unknown"}
            spend(res, m)
            if res["ok"]:
                return res
            log(f"[{tid}] {what}调用失败（第 {i + 1} 次）：{res['error'][:200]}")
            kind = res.get("error_kind")
            if kind in ("auth", "config", "identity"):
                break
            if kind == "timeout" and preset in ("edit", "bash"):
                break                      # it may already have changed files
        return res

    def fit(body: str) -> str:
        """Send an over-long result back for compression, so no reviewer pays to read padding."""
        for i in range(SHRINK_TRIES):
            if count_chars(body) <= limit:
                break
            log(f"[{tid}] 正文 {count_chars(body)} 字，超过上限 {limit}：压缩（第 {i + 1} 次）")
            res = attempt(shrink_prompt(task, body, limit), "none", model, "压缩")
            if not res["ok"]:
                break
            shorter, _ = split_selfcheck(res["text"])
            if shorter and count_chars(shorter) < count_chars(body):
                body = shorter
                rec["shrinks"] += 1
        return body

    def get_review(body: str) -> tuple[dict | None, str, str]:
        """The reviewer's verdict, or (None, reason, outcome) when it never became usable."""
        why = ""
        for i in range(max(a.retries, 0) + 1):
            rres = attempt(review_prompt(task, shared, body, limit), "read", review_model, "复核")
            if not rres["ok"]:
                return None, f"复核调用失败：{rres['error'][:300]}", "review_unavailable"
            review = parse_review(rres["text"], task.get("acceptance"))
            if review.get("valid"):
                return review, "", "reviewed"
            why = "复核结果无效：" + "；".join(review.get("errors") or [])[:300]
            log(f"[{tid}] {why}，重新复核（第 {i + 1} 次）")
        return None, why, "review_invalid"

    prev, review = None, None
    for rnd in range(1, a.max_rework + 2):
        rec["rounds"] = rnd
        log(f"[{tid}] 第 {rnd} 轮：执行")
        res = attempt(worker_prompt(task, shared, prev, review, limit), tools, model, "执行")
        if not res["ok"]:
            rec["error"] = res["error"]
            break
        body, check = split_selfcheck(res["text"])
        if limit:
            body = fit(body)
        result_path = out / f"{tid}.v{rnd}.md"
        result_path.write_text(body)
        rec.update(selfcheck=check, chars=count_chars(body), error=None)

        # Cheap checks first: a result that fails them goes straight back, no reviewer paid.
        review = None
        if limit and count_chars(body) > limit:
            review = {"pass": False, "checked_by": "script",
                      "errors": [f"正文 {count_chars(body)} 字，超过上限 {limit} 字"],
                      "fix_instructions": f"把正文压到 {limit} 字以内：删掉重复和解释，保留结论和关键步骤。"}
        elif has_check(task):
            log(f"[{tid}] 第 {rnd} 轮：运行检查命令")
            ck = run_check(task, cwd, result_path)
            rec["check"] = {k: ck[k] for k in ("pass", "exit", "seconds")}
            if not ck["pass"]:
                how = "超时" if ck["exit"] is None else f"退出码 {ck['exit']}"
                review = {"pass": False, "checked_by": "check_cmd",
                          "errors": [f"检查命令没通过（{how}）：{check_label(task)}"],
                          "check_output": ck["output"],
                          "fix_instructions": "看 check_output 里的报错，只改你这个任务负责的文件，让检查命令通过。"}

        if review is None and not do_review:
            if has_check(task):
                rec.update(status="ok", verified_by="check_cmd")
                log(f"[{tid}] 第 {rnd} 轮检查命令通过")
            else:
                rec["status"] = "unreviewed"
                rec["unreviewed_reason"] = ("没有 acceptance" if not task.get("acceptance")
                                            else "任务设了 review: false" if task.get("review") is False
                                            else "--no-review")
            break
        if review is None:
            log(f"[{tid}] 第 {rnd} 轮：复核")
            review, why, outcome = get_review(body)
            if review is None:
                # A failed/invalid review says nothing about the result: keep it as
                # unreviewed instead of spending a rework round on it. It is never
                # counted as a normal passed result.
                rec.pop("review", None)
                rec.update(status="unreviewed",
                           unreviewed_reason=("复核失败" if outcome == "review_unavailable"
                                              else "复核结果无效"),
                           review_outcome=outcome, review_error=why)
                log(f"[{tid}] 第 {rnd} 轮没能复核：{why[:200]}")
                break
        (out / f"{tid}.v{rnd}.review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2))
        rec["review"] = {"pass": review["pass"], "by": review.get("checked_by", "reviewer"),
                         "errors": review.get("errors", [])}
        if review["pass"]:
            rec.update(status="ok", verified_by="review+check_cmd" if has_check(task) else "review")
            log(f"[{tid}] 第 {rnd} 轮复核通过")
            break
        log(f"[{tid}] 第 {rnd} 轮没通过（{rec['review']['by']}）：{len(review.get('errors', []))} 处问题")
        prev = body
        rec["status"] = "unverified"   # has a result, but it never passed its checks

    # The newest version that was actually produced (a later round may have errored).
    final = next((out / f"{tid}.v{r}.md" for r in range(rec["rounds"], 0, -1)
                  if (out / f"{tid}.v{r}.md").exists()), None)
    if final:
        shutil.copyfile(final, out / f"{tid}.md")
        rec["result_file"] = f"{tid}.md"
        rec["result_sha256"] = _util.sha256_text(final.read_text(encoding="utf-8"))
        rec["version"] = int(final.stem.rsplit(".v", 1)[-1])
    rec["cost_usd"] = round(rec["cost_usd"], 6)
    rec["seconds"] = round(rec["seconds"], 1)
    if rec["cost_unknown_calls"]:
        rec["cost_known"] = False
    log(f"[{tid}] 结束：{rec['status']}，{rec['rounds']} 轮，"
        f"约 ${rec['cost_usd']:.4f}" + ("（部分费用未知）" if not rec["cost_known"] else ""))
    return rec


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", s)


# ---------------------------------------------------------------- commands

def fill(value, values: dict, skip: tuple = ()):
    """Put file values into every string of a task template. Not str.format: prompts often
    contain other braces, such as JSON examples."""
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace(k, v)
        return value
    if isinstance(value, list):
        return [fill(v, values, skip) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in skip else fill(v, values, skip)) for k, v in value.items()}
    return value


def expand_inputs(task: dict, default_cwd: str | None) -> list[dict]:
    """One task per file matched by the task's `inputs` globs, with {path} {name} {stem}
    {i} {n} filled in. Globs are relative to the task's cwd."""
    if "inputs" not in task:
        return [task]
    pats = [task["inputs"]] if isinstance(task["inputs"], str) else task["inputs"]
    if not (isinstance(pats, list) and pats and all(isinstance(p, str) for p in pats)):
        sys.exit(f"任务 {task['id']} 的 inputs 必须是 glob 字符串或字符串列表")
    base = Path(task.get("cwd") or default_cwd or os.getcwd()).expanduser()
    files = []
    for p in pats:
        p = os.path.expanduser(p)
        for f in sorted(glob.glob(p if os.path.isabs(p) else str(base / p), recursive=True)):
            if os.path.isfile(f) and f not in files:
                files.append(f)
    if not files:
        sys.exit(f"任务 {task['id']} 的 inputs 没有匹配到任何文件：{pats}（相对于 {base}）")
    template = {k: v for k, v in task.items() if k != "inputs"}
    if not any(ph in template["id"] for ph in PLACEHOLDERS):
        template["id"] += "-{stem}"
    tasks, seen = [], set()
    for i, f in enumerate(files, 1):
        fp = Path(f)
        try:
            rel = str(fp.relative_to(base))
        except ValueError:
            rel = str(fp)
        values = {"{path}": rel, "{name}": fp.name, "{stem}": fp.stem, "{i}": str(i), "{n}": str(len(files))}
        # Prompt/acceptance get literal filenames. Check commands keep their
        # placeholders and receive values through the safe substitution path at
        # run time, so a hostile filename cannot become shell syntax.
        t = fill(template, values, skip=("check_cmd", "check_argv"))
        t["_placeholders"] = dict(values)
        # Ids become file names: keep them safe, and fall back to the number for names
        # with no ASCII letters (Chinese file names, say).
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", fp.stem).strip("_") or str(i)
        tid = fill(template["id"], {**values, "{stem}": safe, "{name}": safe, "{path}": safe})
        tid = base_id = re.sub(r"[^A-Za-z0-9_-]", "_", tid)[:64]
        k = 2
        while tid in seen:
            tid = f"{base_id[:60]}-{k}"
            k += 1
        t["id"] = tid
        seen.add(tid)
        tasks.append(t)
    return tasks


def resolve_deps(tasks: list[dict]) -> None:
    """Turn each depends_on pattern list ("lec-*" matches every expanded lecture task) into
    task ids, and refuse unknown names and cycles."""
    ids = [t["id"] for t in tasks]
    for t in tasks:
        pats = t.get("depends_on", [])
        pats = [pats] if isinstance(pats, str) else pats
        if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
            sys.exit(f"任务 {t['id']} 的 depends_on 必须是任务 id（可用 * 通配）的列表")
        deps = []
        for p in pats:
            hits = [i for i in ids if i != t["id"] and fnmatch.fnmatchcase(i, p)]
            if not hits:
                sys.exit(f"任务 {t['id']} 的 depends_on「{p}」没有匹配到别的任务")
            deps += [h for h in hits if h not in deps]
        t["depends_on"] = deps
    graph = {t["id"]: t["depends_on"] for t in tasks}
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 1:
            sys.exit("任务依赖成环：" + " → ".join(path + [node]))
        if state.get(node) == 2:
            return
        state[node] = 1
        for d in graph[node]:
            visit(d, path + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])


def layers(tasks: list[dict]) -> dict[str, int]:
    """Dependency depth of each task: 0 runs first, 1 after its upstream, and so on."""
    by_id = {t["id"]: t for t in tasks}
    depth: dict[str, int] = {}

    def d(tid: str) -> int:
        if tid not in depth:
            ups = [u for u in by_id[tid].get("depends_on", []) if u in by_id]
            depth[tid] = 1 + max(map(d, ups)) if ups else 0
        return depth[tid]

    return {t["id"]: d(t["id"]) for t in tasks}


def load_tasks(path: Path, default_cwd: str | None = None,
               max_tasks: int = DEFAULT_MAX_TASKS) -> tuple[list[dict], str]:
    try:
        spec = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        sys.exit(f"读取任务文件失败：{exc}")
    if isinstance(spec, dict):
        tasks = spec.get("tasks")
        shared = spec.get("shared_context", "")
    elif isinstance(spec, list):
        tasks, shared = spec, ""
    else:
        sys.exit("任务文件必须是 JSON 对象或任务数组")
    if not isinstance(tasks, list) or not tasks:
        sys.exit("任务文件里必须有非空的 tasks 数组")
    if not isinstance(shared, str):
        sys.exit("shared_context 必须是字符串")
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            sys.exit(f"第 {i + 1} 个任务不是对象")
        t.setdefault("id", f"task{i + 1}")
    tasks = [x for t in tasks for x in expand_inputs(t, default_cwd)]
    if len(tasks) > max_tasks:
        sys.exit(f"展开后有 {len(tasks)} 个任务，超过上限 {max_tasks}。检查 inputs 的 glob，"
                 "确实需要就加 --max-tasks。")
    ids = set()
    for t in tasks:
        if not isinstance(t["id"], str) or not TASK_ID.fullmatch(t["id"]):
            sys.exit(f"任务 id 只能用字母、数字、_ 和 -，最长 64 个字符：{t['id']!r}")
        if t["id"] in ids:
            sys.exit(f"任务 id 重复：{t['id']}")
        if "max_chars" in t and not (type(t["max_chars"]) is int and t["max_chars"] > 0):
            sys.exit(f"任务 {t['id']} 的 max_chars 必须是正整数")
        if "prompt" not in t or not isinstance(t.get("prompt"), str) or not t["prompt"].strip():
            sys.exit(f"任务 {t['id']} 缺少 prompt")
        if t.get("tools", "read") not in TOOL_PRESETS:
            sys.exit(f"任务 {t['id']} 的 tools 只能是 {list(TOOL_PRESETS)}")
        if not isinstance(t.get("acceptance", []), list) or \
                not all(isinstance(c, str) for c in t.get("acceptance", [])):
            sys.exit(f"任务 {t['id']} 的 acceptance 必须是字符串列表")
        if not isinstance(t.get("review", True), bool):
            sys.exit(f"任务 {t['id']} 的 review 只能是 true 或 false")
        if "check_cmd" in t and not (isinstance(t["check_cmd"], str) and t["check_cmd"].strip()):
            sys.exit(f"任务 {t['id']} 的 check_cmd 必须是非空的命令字符串")
        if "check_argv" in t:
            argv = t["check_argv"]
            if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv)):
                sys.exit(f"任务 {t['id']} 的 check_argv 必须是非空字符串列表")
            if "check_cmd" in t:
                sys.exit(f"任务 {t['id']} 的 check_argv 和 check_cmd 只能选一个")
        if "checks" in t:
            sys.exit(f"任务 {t['id']} 的 checks 只在新工作流中支持；旧 run 请用 check_argv/check_cmd")
        for key in ("check_timeout", "upstream_chars"):
            if key in t and not (type(t[key]) is int and t[key] > 0):
                sys.exit(f"任务 {t['id']} 的 {key} 必须是正整数")
        ids.add(t["id"])
    resolve_deps(tasks)
    return tasks, shared


def blocked_record(tid: str, why: str, task: dict | None = None) -> dict:
    rec = {"id": tid, "status": "blocked", "rounds": 0, "shrinks": 0, "cost_usd": 0.0,
           "cost_known": True, "cost_unknown_calls": 0, "usage_missing_calls": 0,
           "seconds": 0.0, "models": [], "calls": 0,
           "tokens": {"input": 0, "cache_read": 0, "output": 0},
           "error": why}
    if task:
        rec["definition_hash"] = _util.hash_obj(
            {k: v for k, v in task.items() if not str(k).startswith("_")})
        rec["dep_versions"] = {}
    return rec


def _safe_run_task(task: dict, shared: str, runner: Runner, out: Path) -> dict:
    """A single task must never take down the batch or prevent summary.json."""
    try:
        return run_task(task, shared, runner, out)
    except Exception as exc:
        rec = blocked_record(task.get("id", "?"), "内部错误", task)
        rec["status"] = "failed"
        rec["error"] = _security.redact(f"{type(exc).__name__}: {exc}")[:800]
        rec["traceback_tail"] = _security.redact(traceback.format_exc())[-1500:]
        log(f"[{task.get('id', '?')}] 内部错误：{rec['error']}")
        return rec


def run_all(tasks: list[dict], shared: str, runner: Runner, out: Path, concurrency: int,
            done: dict[str, dict]) -> dict[str, dict]:
    """Run tasks as soon as everything they depend on has finished, up to `concurrency` at
    once. `done` starts with usable records from earlier runs (for --only) and is filled in.
    A task whose upstream failed is marked blocked and never started."""
    pending = list(tasks)
    running: dict[cf.Future, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while pending or running:
            moved = True
            while moved:
                moved = False
                for t in list(pending):
                    deps = t.get("depends_on", [])
                    if not all(d in done for d in deps):
                        if any(d not in done and d not in running.values() and
                               d not in [p["id"] for p in pending] for d in deps):
                            missing = [d for d in deps if d not in done]
                            why = "上游不在这次运行里，也没有可用的旧结果：" + "、".join(missing)
                        else:
                            continue
                    else:
                        bad = [f"{d}（{done[d]['status']}）" for d in deps if done[d]["status"] not in USABLE]
                        if not bad:
                            job = {**t, "_deps": {d: done[d]["status"] for d in deps},
                                   "_dep_records": {d: done[d] for d in deps}}
                            running[pool.submit(_safe_run_task, job, shared, runner, out)] = t["id"]
                            pending.remove(t)
                            moved = True
                            continue
                        why = "上游没完成：" + "、".join(bad)
                    for stale in out.glob(f"{glob_escape(t['id'])}.*"):
                        stale.unlink()
                    done[t["id"]] = blocked_record(t["id"], why, t)
                    log(f"[{t['id']}] 跳过：{why}")
                    pending.remove(t)
                    moved = True
            if not running:
                break
            finished, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
            for f in finished:
                tid = running.pop(f)
                try:
                    done[tid] = f.result()
                except BaseException as exc:
                    rec = blocked_record(tid, "内部错误")
                    rec["status"] = "failed"
                    rec["error"] = _security.redact(f"{type(exc).__name__}: {exc}")[:800]
                    done[tid] = rec
    for t in pending:   # only reachable if dependencies could never be met
        done[t["id"]] = blocked_record(t["id"], "上游一直没有完成", t)
    return done


def cmd_run(args) -> int:
    tasks, shared = load_tasks(Path(args.tasks), args.cwd, args.max_tasks)
    by_id = {t["id"]: t for t in tasks}
    all_ids = [t["id"] for t in tasks]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        if not want:
            sys.exit("--only 不能为空；请给出逗号分隔的任务 id")
        unknown = sorted(want - set(all_ids))
        if unknown:
            sys.exit("--only 里这些 id 不存在：" + "、".join(unknown) + "（可以先运行 plan 看展开后的 id）")
        tasks = [t for t in tasks if t["id"] in want]
    if not tasks:
        sys.exit("没有要运行的任务。")
    out = Path(args.out or Path(args.tasks).with_suffix("").as_posix() + "-results").expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / "summary.json"
    old = {}
    if args.only and summary_path.exists():
        try:
            old = {r["id"]: r for r in json.loads(summary_path.read_text()).get("tasks", [])
                   if isinstance(r, dict) and isinstance(r.get("id"), str)}
        except (OSError, ValueError, AttributeError) as exc:
            log(f"旧 summary.json 无法读取，按首次运行处理：{exc}")
    run_ids = {t["id"] for t in tasks}
    # Upstream tasks left out of an --only rerun count as finished if their old result is
    # still there.
    done = {i: r for i, r in old.items()
            if i not in run_ids and (r["status"] not in USABLE or (out / f"{i}.md").exists())}
    runner = Runner(args)

    reviewed = sum(1 for t in tasks if args.review and t.get("review", True) and t.get("acceptance"))
    checked = sum(1 for t in tasks if t.get("check_cmd") or t.get("check_argv"))
    waiting = sum(1 for t in tasks if t.get("depends_on"))
    log(f"运行 {len(tasks)} 个任务（{reviewed} 个带复核，{checked} 个带检查命令，{waiting} 个要等上游；"
        f"最多返工 {args.max_rework} 次），最多同时 {args.concurrency} 个，结果写入 {out}")
    run_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    fatal = None
    try:
        done = run_all(tasks, shared, runner, out, max(1, args.concurrency), done)
    except BaseException as exc:      # run_all itself should not raise; keep state explicit
        fatal = f"{type(exc).__name__}: {exc}"
        log(f"批次异常终止：{fatal}")
        for t in tasks:
            if t["id"] not in done:
                done[t["id"]] = blocked_record(t["id"], "批次异常终止", t)
                done[t["id"]]["status"] = "failed"
                done[t["id"]]["error"] = fatal

    old.update({i: done[i] for i in run_ids})
    merged = sorted(old.values(), key=lambda r: all_ids.index(r["id"]) if r["id"] in all_ids else len(all_ids))
    merged_by_id = {r["id"]: r for r in merged}

    def current_definition(tid: str) -> str | None:
        task = by_id.get(tid)
        if not task:
            return None
        return _util.hash_obj({k: v for k, v in task.items() if not str(k).startswith("_")})

    for rec in merged:
        reasons = []
        for dep, info in (rec.get("dep_versions") or {}).items():
            cur = merged_by_id.get(dep)
            if not cur:
                reasons.append(f"上游 {dep} 的当前记录不存在")
                continue
            if cur.get("status") not in USABLE:
                reasons.append(f"上游 {dep} 当前状态是 {cur.get('status')}")
                continue
            if cur.get("stale"):
                reasons.append(f"上游 {dep} 本身已 stale")
            if info.get("result_sha256") and cur.get("result_sha256") != info["result_sha256"]:
                reasons.append(f"上游 {dep} 的结果内容已变化")
            if info.get("definition_hash") and cur.get("definition_hash") != info["definition_hash"]:
                reasons.append(f"上游 {dep} 的任务定义已变化")
        own = current_definition(rec["id"])
        if own and rec.get("definition_hash") and rec["definition_hash"] != own:
            reasons.append("当前任务文件的定义与该记录不同")
        if reasons:
            rec["stale"] = True
            rec["stale_reasons"] = reasons
        else:
            rec["stale"] = False
            rec.pop("stale_reasons", None)

    summary = {
        "run_id": run_id,
        "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
        "wall_seconds": round(time.time() - t0, 1),
        "passed": [r["id"] for r in merged if r["status"] == "ok" and not r.get("stale")],
        "unverified": [r["id"] for r in merged if r["status"] == "unverified"],
        "unreviewed": [r["id"] for r in merged if r["status"] == "unreviewed"],
        "review_failed": [r["id"] for r in merged if r.get("review_error")],
        "blocked": [r["id"] for r in merged if r["status"] == "blocked"],
        "failed": [r["id"] for r in merged if r["status"] == "failed"],
        "stale": [r["id"] for r in merged if r.get("stale")],
        "est_total_cost_usd": round(sum(r.get("cost_usd", 0) for r in merged), 4),
        "total_calls": sum(int(r.get("calls", 0)) for r in merged),
        "cost_unknown_calls": sum(int(r.get("cost_unknown_calls", 0)) for r in merged),
        "cost_coverage": ("full" if all(r.get("cost_known", True) for r in merged)
                          else ("partial" if any(r.get("cost_usd", 0) for r in merged) else "none")),
        "total_tokens": {k: sum(r.get("tokens", {}).get(k, 0) for r in merged)
                         for k in ("input", "cache_read", "output")},
        "tasks": merged,
    }
    if fatal:
        summary["fatal_error"] = fatal
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "tasks"}, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")
    bad = (summary["failed"] or summary["unverified"] or summary["review_failed"]
           or summary["blocked"] or summary["stale"] or fatal)
    return 1 if bad else 0


def cmd_plan(args) -> int:
    """Show what `run` would start, without calling DeepSeek: expanded ids, dependency
    order, checks and prompt sizes."""
    tasks, shared = load_tasks(Path(args.tasks), args.cwd, args.max_tasks)
    if not tasks:
        sys.exit("任务文件里没有任务。")
    depth = layers(tasks)
    print(f"共 {len(tasks)} 个任务，{max(depth.values()) + 1} 层；共同背景 {len(shared)} 字。")
    for t in sorted(tasks, key=lambda t: depth[t["id"]]):
        review = "复核" if t.get("acceptance") and t.get("review", True) else "不复核"
        bits = [f"第 {depth[t['id']] + 1} 层", t["id"], t.get("tools", args.tools), review,
                f"上限 {t.get('max_chars', args.max_chars) or '无'} 字", f"prompt {len(t['prompt'])} 字"]
        if has_check(t):
            bits.append(f"检查：{check_label(t)}")
        if t.get("depends_on"):
            bits.append("等：" + "、".join(t["depends_on"]))
        print(" | ".join(bits))
    if args.show:
        t = next((t for t in tasks if t["id"] == args.show), None)
        if t is None:
            sys.exit(f"没有 id 为 {args.show} 的任务")
        print(f"\n--- {t['id']} 的执行者 prompt（上游结果要到运行时才有）---")
        print(worker_prompt(t, shared, limit=t.get("max_chars", args.max_chars)))
    return 0


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
    ap = argparse.ArgumentParser(
        description="One-shot DeepSeek-backed worker runner for a fixed task list. "
                    "Use scripts/swarm_loop.py when the next task depends on results.")
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="verify the DeepSeek key with one tiny call")
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.set_defaults(func=cmd_check)

    def task_options(p: argparse.ArgumentParser) -> None:
        """Options that change how the tasks file is read, shared by run and plan."""
        p.add_argument("tasks", help="tasks JSON file")
        p.add_argument("--tools", default="read", choices=list(TOOL_PRESETS),
                       help="default tool preset for tasks that do not set one (default: read)")
        p.add_argument("--cwd", help="default working directory for workers (and for inputs globs)")
        p.add_argument("--max-chars", type=int,
                       help="default length limit (non-whitespace characters) for tasks without max_chars")
        p.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS,
                       help=f"refuse to run more tasks than this after inputs expansion (default: {DEFAULT_MAX_TASKS})")

    p = sub.add_parser("plan", help="show expanded tasks and dependency order without calling DeepSeek")
    task_options(p)
    p.add_argument("--show", metavar="ID", help="also print the full worker prompt of one task")
    p.set_defaults(func=cmd_plan)

    r = sub.add_parser("run", help="run every task in a tasks file in parallel")
    task_options(r)
    r.add_argument("--out", help="output folder (default: <tasks>-results next to the file)")
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--review-model", help="model for reviewers (default: same as --model)")
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--timeout", type=int, default=900, help="seconds per call")
    r.add_argument("--retries", type=int, default=1,
                   help="retries per call on API/CLI errors, for workers and reviewers alike")
    r.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                   help=f"output token cap per call, reasoning included (default: {DEFAULT_MAX_OUTPUT_TOKENS})")
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
