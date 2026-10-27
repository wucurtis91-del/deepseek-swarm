"""Live progress for running workers: what each task is doing, what it has been
thinking about, and what it has cost so far.

Workers run `claude -p --output-format stream-json --include-partial-messages`, so
every thinking/answer delta, tool call and per-message usage arrives as one JSON
line while the call is still running. A LiveBoard folds those lines into a small
snapshot and, about once a second, writes it to `<dir>/live/status.json` and an
auto-refreshing `<dir>/live/index.html`. `watch` renders the same snapshot in a
terminal, or once for an agent to relay.

Costs of finished messages come from provider usage. While a message is still
streaming, its output tokens are estimated from the characters seen so far, so
the running total is marked as an estimate until every call has finished.
Standard library only.
"""
from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import security

# A folder, not files: run_task deletes "<id>.*" in the results folder, and no
# task id can match a name without a dot.
LIVE_DIR = "live"
STATUS_FILE = "status.json"
PAGE_FILE = "index.html"

STREAM_FLAGS = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]

TAIL_CHARS = 240
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
STALE_AFTER = 30          # seconds without a snapshot update before watch warns


def final_result(stdout: str) -> dict | None:
    """The `result` event of a stream-json run; it has the same fields as
    `--output-format json`. Plain json output (one object) is accepted too."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("type") in (None, "result"):
            return data
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def _is_cjk(ch: str) -> bool:
    return "　" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


def _describe_tool(name: str, raw: str) -> str:
    try:
        args = json.loads(raw) if raw else {}
    except ValueError:
        args = {}
    arg = ""
    if isinstance(args, dict):
        for key in ("file_path", "path", "pattern", "command", "url"):
            if args.get(key):
                arg = str(args[key])
                break
    if "/" in arg and name in ("Read", "Edit", "Write"):
        arg = arg.rsplit("/", 1)[-1]
    text = f"{name} {arg}".strip()
    return text if len(text) <= 60 else text[:57] + "…"


def _fmt_seconds(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分{s % 60:02d}秒"
    return f"{s // 3600}小时{s % 3600 // 60:02d}分"


def _fmt_chars(n: int) -> str:
    return f"{n / 1000:.1f}k字" if n >= 1000 else f"{n}字"


def _fmt_usd(x: float | None) -> str:
    return "未知" if x is None else f"${x:.4f}"


class CallTracker:
    """One model call. `on_line` takes the CLI's stream-json lines."""

    def __init__(self, board: "LiveBoard", task: str, phase: str, model: str, round_no):
        self.board = board
        self.task = task
        self.phase = phase
        self.model = model
        self.round = round_no
        self.started = self.last_event = time.time()
        self.activity = "等待模型"
        self.thinking_chars = 0
        self.text_chars = 0
        self.tool_calls = 0
        self.last_tool = ""
        self.tail = ""
        self.tail_kind = ""
        self.done_usage = dict.fromkeys(USAGE_KEYS, 0)
        self.msg_usage: dict | None = None     # the message still streaming
        self.msg_cjk = self.msg_other = 0
        self.tool_name = ""
        self.tool_json = ""
        self.final_usage: dict | None = None
        self.closed = False

    # -- stream parsing
    def on_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        with self.board.lock:
            self._apply(event)
            self.last_event = time.time()

    def _grow(self, s: str, kind: str) -> None:
        for ch in s:
            if _is_cjk(ch):
                self.msg_cjk += 1
            else:
                self.msg_other += len(ch.encode("utf-8"))
        if kind != self.tail_kind:
            self.tail, self.tail_kind = "", kind
        self.tail = (self.tail + s)[-TAIL_CHARS:]

    def _commit_message(self) -> None:
        if self.msg_usage:
            for k in USAGE_KEYS:
                self.done_usage[k] += int(self.msg_usage.get(k) or 0)
        self.msg_usage = None
        self.msg_cjk = self.msg_other = 0

    def _apply(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "stream_event":
            ev = e.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                self._commit_message()          # a message that never sent message_delta
                usage = (ev.get("message") or {}).get("usage") or {}
                self.msg_usage = {k: int(usage.get(k) or 0) for k in USAGE_KEYS}
                self.msg_usage["output_tokens"] = 0
                self.activity = "等待模型"
            elif et == "content_block_start":
                block = ev.get("content_block") or {}
                bt = block.get("type")
                if bt == "thinking":
                    self.activity = "思考中"
                elif bt == "text":
                    self.activity = "写答案"
                elif bt == "tool_use":
                    self.tool_name, self.tool_json = str(block.get("name") or "工具"), ""
                    self.activity = f"调用 {self.tool_name}"
            elif et == "content_block_delta":
                d = ev.get("delta") or {}
                dt = d.get("type")
                if dt == "thinking_delta":
                    s = str(d.get("thinking") or "")
                    self.thinking_chars += len(s)
                    self._grow(s, "thinking")
                elif dt == "text_delta":
                    s = str(d.get("text") or "")
                    self.text_chars += len(s)
                    self._grow(s, "text")
                elif dt == "input_json_delta" and len(self.tool_json) < 4000:
                    self.tool_json += str(d.get("partial_json") or "")
            elif et == "content_block_stop" and self.tool_name:
                self.tool_calls += 1
                self.last_tool = _describe_tool(self.tool_name, self.tool_json)
                self.activity = self.last_tool
                self.tool_name = self.tool_json = ""
            elif et == "message_delta":
                usage = ev.get("usage") or {}
                if self.msg_usage is None:
                    self.msg_usage = dict.fromkeys(USAGE_KEYS, 0)
                for k in USAGE_KEYS:
                    if usage.get(k) is not None:
                        self.msg_usage[k] = int(usage[k])
                self._commit_message()
        elif kind == "user":
            self.activity = "等待模型"          # a tool result went back to the model
        elif kind == "result":
            usage = e.get("usage")
            if isinstance(usage, dict) and usage:
                self.final_usage = {k: int(usage.get(k) or 0) for k in USAGE_KEYS}
            self.activity = "结束"

    # -- figures
    def usage(self) -> tuple[dict, bool]:
        """Tokens so far and whether they include an estimate."""
        if self.final_usage is not None:
            return dict(self.final_usage), False
        usage = dict(self.done_usage)
        estimated = False
        if self.msg_usage is not None:
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[k] += int(self.msg_usage.get(k) or 0)
            usage["output_tokens"] += self.msg_cjk + -(-self.msg_other // 4)
            estimated = True
        return usage, estimated

    def cost(self) -> tuple[float | None, bool]:
        usage, estimated = self.usage()
        return self.board.price_of(usage, self.model), estimated

    def close(self, res: dict | None = None) -> None:
        with self.board.lock:
            if self.closed:
                return
            self.closed = True
            if self.final_usage is None and isinstance((res or {}).get("usage"), dict) and res["usage"]:
                self.final_usage = {k: int(res["usage"].get(k) or 0) for k in USAGE_KEYS}
            if self.final_usage is None:
                self._commit_message()
        self.board._end(self)


class _NullTracker:
    def on_line(self, line: str) -> None:
        pass

    def close(self, res: dict | None = None) -> None:
        pass


class LiveBoard:
    """Shared, thread-safe progress for one run. Writes nothing until a call begins."""

    def __init__(self, base_dir, *, price=None, title: str = "", echo=None,
                 echo_every: float = 15.0, flush_every: float = 1.0):
        self.dir = Path(base_dir) / LIVE_DIR
        self.price = price
        self.title = title
        self.echo = echo
        self.echo_every = echo_every
        self.flush_every = flush_every
        self.lock = threading.RLock()
        self.started = time.time()
        self.tasks: dict[str, dict] = {}
        self.active: list[CallTracker] = []
        self.finished = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_echo = time.time()

    # -- bookkeeping
    def price_of(self, usage: dict, model: str) -> float | None:
        if self.price is None:
            return None
        try:
            return self.price(usage, model)
        except Exception:
            return None

    def _task(self, tid: str) -> dict:
        return self.tasks.setdefault(tid, {
            "id": tid, "state": "waiting", "status": None, "phase": "", "round": None,
            "started": None, "ended": None, "cost_usd": 0.0, "cost_unknown_calls": 0,
            "calls": 0, "tokens": dict.fromkeys(USAGE_KEYS, 0)})

    def register(self, ids) -> None:
        with self.lock:
            for tid in ids:
                self._task(tid)

    def begin(self, task: str, phase: str, model: str, round_no=None) -> CallTracker:
        tracker = CallTracker(self, task, phase, model, round_no)
        with self.lock:
            t = self._task(task)
            t["state"] = "running"
            t["phase"] = phase
            t["round"] = round_no
            t["started"] = t["started"] or tracker.started
            self.active.append(tracker)
        self._start()
        return tracker

    def _end(self, tracker: CallTracker) -> None:
        cost, _ = tracker.cost()
        usage, _ = tracker.usage()
        with self.lock:
            if tracker in self.active:
                self.active.remove(tracker)
            t = self._task(tracker.task)
            t["calls"] += 1
            for k in USAGE_KEYS:
                t["tokens"][k] += usage.get(k, 0)
            if cost is None:
                t["cost_unknown_calls"] += 1
            else:
                t["cost_usd"] += cost
            t["last_tool"] = tracker.last_tool or t.get("last_tool", "")
            if t["state"] == "running" and not any(c.task == tracker.task for c in self.active):
                t["state"] = "idle"            # between steps: next call, check or review
                t["phase"] = f"{tracker.phase}结束"
        self.flush()

    def note(self, task: str, phase: str) -> None:
        """A step that is not a model call, such as running a check command."""
        with self.lock:
            t = self._task(task)
            t["state"] = "running"
            t["phase"] = phase
            t["started"] = t["started"] or time.time()
        self._start()
        self.flush()

    def task_done(self, task: str, status: str) -> None:
        with self.lock:
            t = self._task(task)
            t["state"] = "done"
            t["status"] = status
            t["phase"] = ""
            t["ended"] = time.time()
        self.flush()

    def finish(self) -> None:
        """Mark the run finished, write the final snapshot and stop the ticker."""
        if self._thread is None and not self.tasks:
            return
        with self.lock:
            self.finished = True
        self._stop.set()
        self.flush()
        if self.echo:
            self.echo(self.progress_line())

    # -- snapshot
    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            rows = []
            total_cost, total_unknown, estimated = 0.0, 0, False
            for tid, t in self.tasks.items():
                cost, unknown = t["cost_usd"], t["cost_unknown_calls"]
                live_call = next((c for c in reversed(self.active) if c.task == tid), None)
                row = {"id": tid, "state": t["state"], "status": t["status"], "phase": t["phase"],
                       "round": t["round"], "calls": t["calls"],
                       "elapsed_s": round(((t["ended"] or now) - t["started"]) if t["started"] else 0, 1),
                       "last_tool": t.get("last_tool", "")}
                if live_call is not None:
                    c_cost, c_est = live_call.cost()
                    if c_cost is None:
                        unknown += 1
                    else:
                        cost += c_cost
                    estimated = estimated or c_est
                    usage, _ = live_call.usage()
                    row.update({
                        "activity": live_call.activity,
                        "thinking_chars": live_call.thinking_chars,
                        "text_chars": live_call.text_chars,
                        "tool_calls": live_call.tool_calls,
                        "last_tool": live_call.last_tool or row["last_tool"],
                        "tail": security.redact(live_call.tail),
                        "tail_kind": live_call.tail_kind,
                        "quiet_s": round(now - live_call.last_event, 1),
                        "call_elapsed_s": round(now - live_call.started, 1),
                        "call_output_tokens": usage.get("output_tokens", 0),
                    })
                row["cost_usd"] = round(cost, 6)
                row["cost_unknown_calls"] = unknown
                total_cost += cost
                total_unknown += unknown
                rows.append(row)
            counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("waiting", "running", "done")}
            counts["running"] += sum(1 for r in rows if r["state"] == "idle")
            return {
                "schema": 1, "title": self.title, "finished": self.finished,
                "started_at": self.started, "updated_at": now,
                "elapsed_s": round(now - self.started, 1),
                "totals": {"tasks": len(rows), **counts, "active_calls": len(self.active),
                           "cost_usd": round(total_cost, 6), "cost_estimated": estimated,
                           "cost_unknown_calls": total_unknown},
                "tasks": rows,
            }

    def progress_line(self) -> str:
        return progress_line(self.snapshot())

    # -- writing
    def flush(self) -> None:
        snap = self.snapshot()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.dir / STATUS_FILE, json.dumps(snap, ensure_ascii=False, indent=1))
            _atomic_write(self.dir / PAGE_FILE, render_html(snap))
        except OSError:
            pass                                   # progress must never break a run

    def _start(self) -> None:
        with self.lock:
            if self._thread is not None or self.finished:
                return
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def _tick(self) -> None:
        while not self._stop.wait(self.flush_every):
            self.flush()
            if self.echo and self.echo_every and time.time() - self._last_echo >= self.echo_every:
                self._last_echo = time.time()
                try:
                    self.echo(self.progress_line())
                except Exception:
                    pass


class _NullBoard:
    """Stands in when live progress is off (tests, `check`)."""

    def begin(self, task, phase, model, round_no=None):
        return _NullTracker()

    def register(self, ids):
        pass

    def note(self, task, phase):
        pass

    def task_done(self, task, status):
        pass

    def finish(self):
        pass


NULL_BOARD = _NullBoard()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- rendering

STATUS_LABEL = {"ok": "通过", "unverified": "没通过", "unreviewed": "未复核", "blocked": "被阻塞",
                "failed": "失败", "stale": "过期", "completed": "完成"}


def _row_line(r: dict) -> str:
    if r["state"] == "waiting":
        return f"{r['id']}：等待开始"
    if r["state"] == "idle":
        return (f"{r['id']}：{r['phase']}，等下一步 · 用时 {_fmt_seconds(r['elapsed_s'])}"
                f" · 约 {_fmt_usd(r['cost_usd'])}")
    if r["state"] == "done":
        label = STATUS_LABEL.get(r.get("status"), r.get("status") or "完成")
        return (f"{r['id']}：已结束（{label}），{_fmt_seconds(r['elapsed_s'])}，"
                f"{_fmt_usd(r['cost_usd'])}" + ("，部分费用未知" if r.get("cost_unknown_calls") else ""))
    bits = [r["phase"] + (f" 第{r['round']}轮" if r.get("round") else "")]
    if r.get("activity"):
        bits.append(r["activity"])
    if r.get("thinking_chars"):
        bits.append(f"已思考 {_fmt_chars(r['thinking_chars'])}")
    if r.get("text_chars"):
        bits.append(f"已写 {_fmt_chars(r['text_chars'])}")
    bits.append(f"用时 {_fmt_seconds(r['elapsed_s'])}")
    bits.append(f"约 {_fmt_usd(r['cost_usd'])}")
    if r.get("quiet_s", 0) >= 60:
        bits.append(f"已经 {_fmt_seconds(r['quiet_s'])}没有新输出")
    return f"{r['id']}：" + " · ".join(bits)


def _summary(snap: dict) -> str:
    t = snap["totals"]
    cost = f"约 {_fmt_usd(t['cost_usd'])}" + ("（含进行中的估算）" if t["cost_estimated"] else "")
    if t.get("cost_unknown_calls"):
        cost += f"，另有 {t['cost_unknown_calls']} 次调用费用未知"
    return (f"完成 {t['done']}/{t['tasks']} · 运行中 {t['running']} · 等待 {t['waiting']} · 已花{cost}")


def progress_line(snap: dict) -> str:
    """One line for a log or an agent to relay."""
    head = f"[进度 {_fmt_seconds(snap['elapsed_s'])}] " + _summary(snap)
    running = [r for r in snap["tasks"] if r["state"] == "running"]
    parts = []
    for r in running[:3]:
        what = r.get("activity") or r["phase"]
        extra = f" {_fmt_chars(r['thinking_chars'])}" if r.get("activity") == "思考中" and r.get("thinking_chars") else ""
        parts.append(f"{r['id']} {r['phase']}·{what}{extra}")
    if len(running) > 3:
        parts.append(f"另 {len(running) - 3} 个")
    return head + (" | " + " | ".join(parts) if parts else "")


def render_text(snap: dict, thinking: bool = False) -> str:
    t = snap["totals"]
    age = time.time() - snap["updated_at"]
    lines = ["deepseek-swarm 实时进度" + (f" · {snap['title']}" if snap.get("title") else ""),
             _summary(snap) + f" · 已运行 {_fmt_seconds(snap['elapsed_s'])}"]
    if snap["finished"]:
        lines.append("运行已结束。")
    elif age > STALE_AFTER:
        lines.append(f"注意：快照已经 {_fmt_seconds(age)}没有更新，脚本可能已经停止。")
    elif t["active_calls"] == 0 and t["running"] == 0:
        lines.append("现在没有正在运行的调用。")
    lines.append("")
    order = {"running": 0, "idle": 1, "waiting": 2, "done": 3}
    for r in sorted(snap["tasks"], key=lambda r: order.get(r["state"], 3)):
        lines.append(_row_line(r))
        if thinking and r["state"] == "running" and r.get("tail"):
            label = "最近在想" if r.get("tail_kind") == "thinking" else "正在写"
            tail = " ".join(r["tail"].split())
            lines.append(f"    {label}：…{tail[-160:]}")
    return "\n".join(lines)


def render_html(snap: dict) -> str:
    e = html.escape
    t = snap["totals"]
    # Keep refreshing after the end too (more slowly): the next run in the same folder
    # should show up in a page that is already open.
    refresh = f'<meta http-equiv="refresh" content="{5 if snap["finished"] else 2}">'
    rows = []
    order = {"running": 0, "idle": 1, "waiting": 2, "done": 3}
    for r in sorted(snap["tasks"], key=lambda r: order.get(r["state"], 3)):
        if r["state"] == "running":
            state = e(r["phase"] + (f" 第{r['round']}轮" if r.get("round") else ""))
            detail = e(r.get("activity") or "")
        elif r["state"] == "idle":
            state, detail = e(r["phase"]), "等下一步"
        elif r["state"] == "done":
            state = e("已结束：" + STATUS_LABEL.get(r.get("status"), r.get("status") or "完成"))
            detail = ""
        else:
            state, detail = "等待开始", ""
        tail = ""
        if r["state"] == "running" and r.get("tail"):
            label = "最近在想" if r.get("tail_kind") == "thinking" else "正在写"
            tail = f'<div class="tail"><b>{label}</b> …{e(r["tail"])}</div>'
        rows.append(
            f'<tr class="{e(r["state"])}"><td class="n">{e(r["id"])}</td><td class="n">{state}</td><td>{detail}{tail}</td>'
            f'<td class="n">{_fmt_chars(r.get("thinking_chars", 0)) if r.get("thinking_chars") else ""}</td>'
            f'<td class="n">{_fmt_seconds(r["elapsed_s"]) if r["state"] != "waiting" else ""}</td>'
            f'<td class="n">{e(_fmt_usd(r["cost_usd"])) if r["state"] != "waiting" else ""}</td></tr>')
    status = "运行已结束" if snap["finished"] else "运行中，每 2 秒自动刷新"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">{refresh}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSeek 实时进度</title>
<style>
:root {{ --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4; --run:#2563eb; --tail:#f5f5f4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1917; --fg:#f5f5f4; --muted:#a8a29e; --line:#44403c; --run:#60a5fa; --tail:#292524; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:14px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
.sum {{ color:var(--muted); margin-bottom:12px; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; min-width:560px; }}
th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; }}
tr.running td:first-child {{ color:var(--run); font-weight:600; }}
tr.waiting, tr.done {{ color:var(--muted); }}
td.n {{ white-space:nowrap; }}
tr.running td:first-child::before {{ content:"●"; margin-right:6px; animation:pulse 1.2s ease-in-out infinite; }}
@keyframes pulse {{ 50% {{ opacity:.25; }} }}
.bar {{ height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin:0 0 14px; }}
.bar > div {{ height:100%; background:var(--run); }}
.tail {{ margin-top:4px; padding:6px 8px; background:var(--tail); border-radius:6px;
        font-size:12px; color:var(--muted); white-space:pre-wrap; word-break:break-all; }}
</style></head><body>
<h1>DeepSeek 实时进度{(" · " + e(snap["title"])) if snap.get("title") else ""}</h1>
<div class="sum">{status} · 已运行 {_fmt_seconds(snap["elapsed_s"])} · {e(_summary(snap))}</div>
<div class="bar"><div style="width:{round(100 * t["done"] / max(1, t["tasks"]))}%"></div></div>
<div class="wrap"><table><thead><tr><th>任务</th><th>状态</th><th>正在做</th><th>思考</th><th>用时</th><th>花费</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
</body></html>
"""


# ---------------------------------------------------------------- watch

def find_status(path) -> Path:
    """Accept a results folder, a loop run folder, its live/ folder, a status.json,
    or a one-shot tasks file (whose results folder sits next to it)."""
    p = Path(path).expanduser()
    candidates = []
    if p.is_file() and p.name == STATUS_FILE:
        return p
    if p.is_file() and p.suffix == ".json":
        candidates.append(Path(p.with_suffix("").as_posix() + "-results") / LIVE_DIR / STATUS_FILE)
    candidates += [p / LIVE_DIR / STATUS_FILE, p / STATUS_FILE]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"在 {p} 找不到实时进度文件（{LIVE_DIR}/{STATUS_FILE}）。运行可能还没开始第一次调用，"
        "或者结果目录写错了。")


def serve(path, *, port: int = 8765, open_window: bool = False, out=None) -> int:
    """Serve the live page on 127.0.0.1 so a browser pane or window can show it.

    Some viewers (Claude's built-in browser, for one) cannot open local files, so the
    page is served over http instead. Only the live/ folder is exposed, and only to
    this machine. Runs until interrupted; start it before or during a run."""
    import functools
    import http.server
    import webbrowser
    out = out or sys.stdout
    p = Path(path).expanduser()
    try:
        folder = find_status(p).parent
    except FileNotFoundError:
        folder = p / LIVE_DIR if p.is_dir() and p.name != LIVE_DIR else p
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / PAGE_FILE
        if not page.exists():
            page.write_text('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="2">'
                            "<p>等待第一次调用开始……</p>", encoding="utf-8")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(folder))
    for candidate in range(port, port + 20):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    else:
        print(f"端口 {port}–{port + 19} 都被占用了。", file=out)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/{PAGE_FILE}"
    print(f"实时进度页面：{url}（按 Ctrl-C 停止）", file=out, flush=True)
    if open_window:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def watch(path, *, once: bool = False, interval: float = 2.0, thinking: bool = False,
          out=None) -> int:
    out = out or sys.stdout
    try:
        return _watch(path, once, interval, thinking, out)
    except KeyboardInterrupt:
        return 0


def _watch(path, once, interval, thinking, out) -> int:
    status = None
    while True:
        try:
            status = status or find_status(path)
            snap = json.loads(status.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            if once:
                print(str(exc), file=out)
                return 1
            snap = None
            text = str(exc)
        except ValueError:
            snap, text = None, "进度文件正在写入，稍后刷新……"
        if snap is not None:
            text = render_text(snap, thinking=thinking)
        if once:
            print(text, file=out)
            return 0
        out.write("\033[2J\033[H" + text + "\n\n按 Ctrl-C 退出。\n")
        out.flush()
        if snap is not None and snap.get("finished"):
            return 0
        time.sleep(interval)
