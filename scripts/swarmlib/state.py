"""Append-only event journal plus recoverable snapshots and a single-writer lock."""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import signal
from pathlib import Path

from . import SCHEMA_VERSION, util

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class StateError(RuntimeError):
    pass


class WriterLocked(StateError):
    pass


TASK_STATUSES = {
    "pending", "running", "completed", "unverified", "exec_failed",
    "interrupted", "unknown", "blocked", "stale", "cancelled", "review_unavailable",
    "review_invalid", "unreviewed", "budget_blocked", "patch_conflict",
    "needs_review",
}


def _top_level_diff(old: dict, new: dict) -> dict:
    """Snapshot patches at top-level key granularity: robust and easy to replay."""
    set_values, deleted = {}, []
    for key, value in new.items():
        if key in ("seq", "updated_at"):
            continue
        if key not in old or old[key] != value:
            set_values[key] = copy.deepcopy(value)
    for key in old:
        if key not in new:
            deleted.append(key)
    return {"set": set_values, "del": deleted}


def _apply_patch(state: dict, patch: dict) -> dict:
    for key, value in (patch.get("set") or {}).items():
        state[key] = copy.deepcopy(value)
    for key in patch.get("del") or []:
        state.pop(key, None)
    return state


class RunStore:
    def __init__(self, run_dir):
        self.dir = Path(run_dir).expanduser().resolve()
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock_path = self.dir / "run.lock"
        self.cancel_path = self.dir / "cancel.request"
        self._fd = None
        self.load_notes: list[str] = []
        self._last_sha = ""
        self._tail_truncate_at: int | None = None
        self._needs_final_newline = False

    # ------------------------------------------------------------ creation
    def exists(self) -> bool:
        return self.state_path.exists() or self.events_path.exists()

    def create(self, state: dict) -> dict:
        if self.exists():
            raise StateError(f"目录里已经有运行状态：{self.dir}（用 status 查看，或换一个目录）")
        self.dir.mkdir(parents=True, exist_ok=True)
        state = copy.deepcopy(state)
        state["schema"] = SCHEMA_VERSION
        state.setdefault("seq", 0)
        state.setdefault("created_at", util.iso())
        state["updated_at"] = state["created_at"]
        state.setdefault("run_id", util.new_run_id())
        self._validate(state)
        util.atomic_write_json(self.state_path, state)
        event = {
            "seq": 0, "ts": util.iso(), "type": "run_created", "summary": "run created",
            "set": {"state": state}, "del": [], "prev": "", "data": {}, "state": state,
        }
        event["sha"] = util.sha256_text(util.canonical(event))
        self._append_event(event)
        self._last_sha = event["sha"]
        return state

    # ------------------------------------------------------------ loading
    def _read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        raw = self.events_path.read_bytes()
        lines = raw.splitlines(keepends=True)
        events = []
        prev = ""
        offset = 0
        last_good_offset = 0
        for i, raw_line in enumerate(lines, 1):
            complete = raw_line.endswith(b"\n")
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                if not complete or i == len(lines):
                    self.load_notes.append(f"忽略截断的事件日志最后一行（第 {i} 行）")
                    self._tail_truncate_at = last_good_offset
                    break
                raise StateError(f"事件日志第 {i} 行损坏；可用 state.json 快照恢复，但请检查磁盘")
            if not isinstance(event, dict) or "seq" not in event:
                if not complete or i == len(lines):
                    self.load_notes.append(f"忽略不完整的最后一行（第 {i} 行）")
                    self._tail_truncate_at = last_good_offset
                    break
                raise StateError(f"事件日志第 {i} 行结构无效")
            sha = event.pop("sha", None)
            expected = util.sha256_text(util.canonical(event))
            if sha != expected:
                if not complete or i == len(lines):
                    self.load_notes.append(f"忽略校验失败的日志尾部（第 {i} 行）")
                    self._tail_truncate_at = last_good_offset
                    break
                raise StateError(f"事件日志第 {i} 行校验失败（内容被改动或写入中断）")
            if event.get("prev") not in ("", prev):
                if not complete or i == len(lines):
                    self.load_notes.append(f"忽略链断裂的日志尾部（第 {i} 行）")
                    self._tail_truncate_at = last_good_offset
                    break
                raise StateError(f"事件日志第 {i} 行的前序哈希不匹配")
            prev = sha
            offset += len(raw_line)
            last_good_offset = offset
            events.append(event)
        if events and not raw.endswith(b"\n"):
            self._needs_final_newline = True
        return events

    def _repair_tail(self) -> None:
        if self._tail_truncate_at is not None:
            if self.events_path.exists():
                size = self.events_path.stat().st_size
                if size > self._tail_truncate_at:
                    with open(self.events_path, "r+b") as f:
                        f.truncate(self._tail_truncate_at)
                        f.flush()
                        os.fsync(f.fileno())
                    self.load_notes.append(
                        f"已截断事件日志尾部 {size - self._tail_truncate_at} 字节，保留最后一条完整记录")
                    self._needs_final_newline = False
            # The damage is now gone. Keeping the old offset would make the next
            # commit truncate the event that was just appended.
            self._tail_truncate_at = None
        if self._needs_final_newline and self.events_path.exists():
            with open(self.events_path, "ab") as f:
                f.write(b"\n")
                f.flush()
                os.fsync(f.fileno())
            self._needs_final_newline = False

    def _append_event(self, event: dict) -> None:
        self._repair_tail()
        util.atomic_append_line(self.events_path, json.dumps(event, ensure_ascii=False))

    def _validate(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise StateError("state 不是对象")
        schema = state.get("schema")
        if schema != SCHEMA_VERSION:
            raise StateError(
                f"运行状态版本是 {schema}，本程序支持 {SCHEMA_VERSION}。"
                "不兼容的旧状态不会被静默改写；请用对应版本的程序或导出后重开。")
        for key in ("run_id", "goal", "tasks", "evidence", "status", "seq"):
            if key not in state:
                raise StateError(f"state 缺少字段 {key}")
        if not isinstance(state["tasks"], dict) or not isinstance(state["evidence"], dict):
            raise StateError("state.tasks / state.evidence 必须是对象")
        for tid, task in state["tasks"].items():
            if not isinstance(task, dict) or task.get("id") != tid:
                raise StateError(f"任务记录无效：{tid}")
            if task.get("status") not in TASK_STATUSES:
                raise StateError(f"任务 {tid} 的状态无效：{task.get('status')}")

    def load(self) -> dict:
        events = self._read_events()
        # Repairing a truncated tail is a write; only do it while this process
        # holds the writer lock. Read-only status commands merely ignore it.
        if self._fd is not None:
            self._repair_tail()
        state = None
        try:
            if self.state_path.exists():
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_notes.append(f"state.json 无法读取（{exc}），尝试用事件日志恢复")
            state = None
        if state is not None:
            schema = state.get("schema") if isinstance(state, dict) else None
            if isinstance(schema, int) and schema > SCHEMA_VERSION:
                raise StateError(
                    f"运行状态版本是 {schema}，本程序支持 {SCHEMA_VERSION}；"
                    "拒绝静默降级，请用对应版本的程序打开或导出后重开。")
            try:
                self._validate(state)
            except StateError as exc:
                self.load_notes.append(f"state.json 校验失败（{exc}），尝试用事件日志恢复")
                state = None
        if state is None:
            state = self._rebuild(events)
            if state is None:
                raise StateError("没有可用的 state.json，事件日志里也没有快照")
            util.atomic_write_json(self.state_path, state)
        # Replay events newer than the snapshot.
        if events:
            last_seq = events[-1].get("seq", 0)
            if state.get("seq", 0) < last_seq:
                for event in events:
                    if event.get("seq", 0) <= state.get("seq", 0):
                        continue
                    if event.get("seq", 0) != state.get("seq", 0) + 1:
                        raise StateError(
                            f"事件日志缺号：state 在 {state.get('seq')}，下一条是 {event.get('seq')}")
                    _apply_patch(state, event)
                    state["seq"] = event["seq"]
                    state["updated_at"] = event.get("ts", state.get("updated_at"))
            elif state.get("seq", 0) > last_seq:
                self.load_notes.append(
                    f"state.json 比事件日志新（{state.get('seq')} > {last_seq}），以快照为准")
        self._validate(state)
        self._last_sha = events[-1].get("sha", "") if events else ""
        # Recompute the last event sha (we popped it while verifying).
        if events:
            last = dict(events[-1])
            self._last_sha = util.sha256_text(util.canonical(last))
        return state

    def _rebuild(self, events: list[dict]) -> dict | None:
        snapshot = None
        index = -1
        for i, event in enumerate(events):
            if isinstance(event.get("state"), dict):
                snapshot, index = event["state"], i
        if snapshot is None:
            # The run_created event stores the whole initial state under set.state.
            for event in events:
                candidate = (event.get("set") or {}).get("state")
                if isinstance(candidate, dict):
                    snapshot, index = candidate, events.index(event)
        if snapshot is None:
            return None
        state = copy.deepcopy(snapshot)
        for event in events[index + 1:]:
            _apply_patch(state, event)
            state["seq"] = event.get("seq", state.get("seq", 0))
            state["updated_at"] = event.get("ts", state.get("updated_at"))
        return state

    # ------------------------------------------------------------ writing
    def commit(self, old_state: dict, new_state: dict, event_type: str,
               data: dict | None = None, summary: str = "") -> dict:
        if self._fd is None:
            raise StateError("写入前必须先取得写锁（store.lock()）")
        patch = _top_level_diff(old_state, new_state)
        seq = int(old_state.get("seq", 0)) + 1
        new_state = copy.deepcopy(new_state)
        new_state["seq"] = seq
        new_state["updated_at"] = util.iso()
        event = {
            "seq": seq, "ts": new_state["updated_at"], "type": event_type,
            "summary": summary[:500], "set": patch["set"], "del": patch["del"],
            "prev": self._last_sha, "data": data or {},
        }
        if seq % 20 == 0:
            event["state"] = copy.deepcopy(new_state)
        sha = util.sha256_text(util.canonical(event))
        event["sha"] = sha
        self._repair_tail()
        util.atomic_append_line(self.events_path, json.dumps(event, ensure_ascii=False, default=str))
        util.atomic_write_json(self.state_path, new_state)
        self._last_sha = sha
        return new_state

    # ------------------------------------------------------------ lock
    @contextlib.contextmanager
    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    os.close(fd)
                    fd = -1
                    raise WriterLocked(
                        f"另一个进程正在写这个运行目录：{self.dir}。"
                        "同一个输出目录只允许一个活动写入者。") from exc
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
            self._fd = fd
            yield self
        finally:
            self._fd = None
            if fd >= 0:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def writer_pid(self) -> int | None:
        try:
            raw = self.lock_path.read_text(encoding="ascii").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    def pid_alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    # ------------------------------------------------------------ cancel
    def request_cancel(self, reason: str = "") -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        util.atomic_write_text(self.cancel_path, json.dumps(
            {"requested_at": util.iso(), "reason": reason}, ensure_ascii=False) + "\n")

    def cancel_requested(self) -> bool:
        return self.cancel_path.exists()

    def clear_cancel(self) -> None:
        try:
            self.cancel_path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ paths
    def packet_path(self, seq: int) -> Path:
        return self.dir / "packets" / f"{seq:04d}.json"

    def decision_path(self, seq: int) -> Path:
        return self.dir / "decisions" / f"{seq:04d}.json"

    def artifact_dir(self, task_id: str) -> Path:
        return self.dir / "artifacts" / task_id

    def worktree_dir(self, task_id: str, version: int, attempt: int) -> Path:
        return self.dir / "worktrees" / f"{task_id}-v{version}-a{attempt}"

    def read_state_readonly(self) -> dict:
        """Read without taking the writer lock; os.replace makes this safe."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(f"无法读取状态：{exc}") from exc
        self._validate(state)
        return state
