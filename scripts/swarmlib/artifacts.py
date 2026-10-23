"""Workspace isolation, code-change capture, scope checks and patch conflicts."""
from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import threading
from pathlib import Path

from . import procs, security, util

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
)


class WorkspaceError(RuntimeError):
    pass


class PatchConflict(WorkspaceError):
    pass


def _git_env() -> dict:
    return security.minimal_env(extra={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                                       "LC_ALL": "C"})


def git(args: list[str], cwd: Path, timeout: int = 120, check: bool = True) -> dict:
    res = procs.run(["git", *args], cwd=cwd, env=_git_env(), timeout=timeout)
    if check and not res["ok"]:
        raise WorkspaceError(f"git {' '.join(args)} 失败：{security.redact(res['output'])[:500]}")
    return res


def path_matches(path: str, patterns: list[str]) -> bool:
    path = path.replace("\\", "/").lstrip("/")
    for pattern in patterns or []:
        pattern = pattern.replace("\\", "/").lstrip("/")
        if pattern in ("**", "**/*", "*"):
            return True
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if path == prefix or path.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _parse_name_status(raw: str) -> list[dict]:
    parts = [p for p in raw.split("\x00") if p]
    out, i = [], 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        letter = status[0]
        if letter in ("R", "C"):
            if i + 1 >= len(parts):
                break
            old, new = parts[i], parts[i + 1]
            i += 2
            out.append({"path": new.replace("\\", "/"), "old_path": old.replace("\\", "/"),
                        "status": "renamed" if letter == "R" else "copied"})
        else:
            if i >= len(parts):
                break
            path = parts[i]
            i += 1
            out.append({"path": path.replace("\\", "/"), "status": {
                "M": "modified", "A": "added", "D": "deleted", "T": "typechange",
                "U": "unmerged",
            }.get(letter, letter)})
    return out


def _porcelain_paths(raw: str) -> list[str]:
    """Porcelain -z uses 'XY path', unlike diff --name-status -z."""
    entries = iter(raw.split("\x00"))
    paths = set()
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            source = next(entries, "")
            if source:
                paths.add(source)
    return sorted(paths)


class Workspace:
    def __init__(self, repo: str | Path | None, run_dir: Path, *,
                 isolate: str = "worktree", require_git_for_code: bool = True):
        if isolate != "worktree":
            raise WorkspaceError(
                "代码任务当前只支持 isolate=worktree。")
        self.repo = Path(repo).expanduser().resolve() if repo else None
        self.run_dir = Path(run_dir)
        self.isolate = isolate
        self.require_git_for_code = require_git_for_code
        self.is_git = bool(self.repo and (self.repo / ".git").exists())
        self.dirty: list[str] = []
        self.base_commit: str | None = None
        # git's worktree metadata is a single shared lock; concurrent add/remove
        # from worker threads can corrupt it.
        self._git_lock = threading.RLock()
        if self.is_git:
            res = git(["rev-parse", "HEAD"], self.repo, check=False)
            if res["ok"]:
                self.base_commit = res["stdout"].strip().splitlines()[0] if res["stdout"].strip() else None
            status = git(["status", "--porcelain", "-z", "--untracked-files=all"], self.repo, check=False)
            if status["ok"]:
                self.dirty = _porcelain_paths(status["stdout"])

    def describe(self) -> dict:
        return {
            "repo": str(self.repo) if self.repo else None,
            "is_git": self.is_git,
            "base_commit": self.base_commit,
            "isolation": "worktree" if (self.is_git and self.isolate == "worktree") else "none",
            "dirty_at_start": self.dirty,
            "dirty_note": ("工作区有未提交改动；worktree 从 HEAD 建立，不包含这些改动，"
                           "程序不会重置或覆盖用户文件。") if self.dirty else "",
            "require_git_for_code": self.require_git_for_code,
        }

    def prepare(self, task: dict, attempt: int, dep_patches: list[dict],
                previous_patches: list[dict] | None = None) -> dict:
        """Return {cwd, isolated, base_commit, applied_patches, dirty_snapshot}."""
        if task.get("kind") != "code":
            raw_cwd = task.get("cwd")
            base = self.repo or self.run_dir
            cwd = (Path(raw_cwd).expanduser() if raw_cwd else base)
            if not cwd.is_absolute():
                cwd = base / cwd
            cwd = cwd.resolve()
            if not cwd.is_dir():
                raise WorkspaceError(f"工作目录不存在：{cwd}")
            return {"cwd": str(cwd), "isolated": False, "base_commit": self.base_commit,
                    "applied_patches": [], "workspace": str(self.repo) if self.repo else str(cwd)}
        if not self.repo:
            raise WorkspaceError(f"代码任务 {task['id']} 没有配置 workspace.repo")
        if not self.is_git:
            if self.require_git_for_code:
                raise WorkspaceError(
                    f"代码任务 {task['id']} 要求 git 工作区以隔离改动；"
                    "可把 workspace.repo 指向 git 仓库，或显式关闭 require_git_for_code。")
            snapshot = self._snapshot(self.repo)
            return {"cwd": str(self.repo), "isolated": False, "base_commit": None,
                    "applied_patches": [], "snapshot": snapshot, "workspace": str(self.repo)}
        if not self.base_commit:
            raise WorkspaceError(f"无法读取 {self.repo} 的 HEAD")
        previous_patches = list(previous_patches or [])
        wt = self.run_dir / "worktrees" / f"{task['id']}-v{task.get('version', 1)}-a{attempt}"
        with self._git_lock:
            if wt.exists():
                shutil.rmtree(wt)
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(["worktree", "add", "--detach", str(wt), self.base_commit], self.repo)
        applied = []
        inherited = []
        try:
            for patch in dep_patches:
                patch_path = Path(patch["path"])
                if not patch_path.exists():
                    raise PatchConflict(f"上游补丁不存在：{patch_path}")
                res = git(["apply", "--whitespace=nowarn", str(patch_path)], wt, check=False)
                if not res["ok"]:
                    raise PatchConflict(
                        f"任务 {task['id']} 无法应用上游 {patch.get('task')} 的补丁："
                        + security.redact(res["output"])[:500])
                applied.append({"task": patch.get("task"), "sha256": patch.get("sha256"),
                                "path": str(patch_path)})
            input_commit = self.base_commit
            if applied:
                # The worker must see a clean input baseline that already contains
                # upstream changes. Otherwise `diff HEAD` after the worker also
                # contains every upstream patch and duplicates it downstream.
                add = git(["add", "-A"], wt, check=False)
                if not add["ok"]:
                    raise WorkspaceError("无法暂存上游补丁以建立输入基线："
                                         + security.redact(add["output"])[:500])
                commit = git(["-c", "user.email=swarm@local", "-c", "user.name=swarm",
                              "commit", "-qm", "swarm input baseline"], wt, check=False)
                if not commit["ok"]:
                    raise WorkspaceError("无法提交上游补丁以建立输入基线："
                                         + security.redact(commit["output"])[:500])
                head = git(["rev-parse", "HEAD"], wt, check=False)
                if not head["ok"] or not head["stdout"].strip():
                    raise WorkspaceError("无法读取输入基线 commit")
                input_commit = head["stdout"].strip().splitlines()[0]
            for patch in previous_patches:
                patch_path = Path(patch["path"])
                if not patch_path.exists():
                    raise PatchConflict(f"上一版代码补丁不存在：{patch_path}")
                if patch.get("sha256") and util.sha256_file(patch_path) != patch.get("sha256"):
                    raise PatchConflict(
                        f"上一版代码补丁的哈希已变化：{patch_path}")
                res = git(["apply", "--whitespace=nowarn", str(patch_path)], wt, check=False)
                if not res["ok"]:
                    raise PatchConflict(
                        f"任务 {task['id']} 无法继承上一版代码补丁（v{patch.get('version')}）："
                        + security.redact(res["output"])[:500])
                inherited.append({"task": task["id"], "version": patch.get("version"),
                                  "sha256": patch.get("sha256"), "path": str(patch_path)})
        except Exception:
            with self._git_lock:
                git(["worktree", "remove", "--force", str(wt)], self.repo, check=False)
            raise
        return {"cwd": str(wt), "isolated": True, "base_commit": self.base_commit,
                "input_commit": input_commit, "applied_patches": applied,
                "previous_patches": inherited,
                "worktree": str(wt), "workspace": str(self.repo)}

    def _snapshot(self, root: Path) -> dict:
        files = {}
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                try:
                    files[str(path.relative_to(root))] = {
                        "size": path.stat().st_size, "sha256": util.sha256_file(path)}
                except OSError:
                    continue
        return files

    def capture(self, ctx: dict, task: dict) -> dict:
        """Record the diff, changed files, scope check and patch-conflict check."""
        cwd = Path(ctx["cwd"])
        if task.get("kind") != "code":
            return {"kind": "none"}
        if not ctx.get("isolated"):
            before = ctx.get("snapshot") or {}
            after = self._snapshot(cwd)
            changed = []
            for path in sorted(set(before) | set(after)):
                if before.get(path) != after.get(path):
                    status = "added" if path not in before else ("deleted" if path not in after else "modified")
                    changed.append({"path": path.replace("\\", "/"), "status": status})
            outside = [c["path"] for c in changed if not path_matches(c["path"], task.get("allowed_paths"))]
            return {
                "kind": "snapshot", "changed_files": changed, "diff": None,
                "scope_ok": not outside, "outside_scope": outside,
                "base_commit": None, "patch_conflict": None,
                "note": "工作区不是 git，只能比较文件快照；没有补丁文本。",
            }
        # Intent-to-add includes untracked files without
        # committing them; this happens only inside the isolated worktree.
        # A worker may commit its changes. Compare against the recorded input,
        # not the mutable HEAD, so those changes remain in the final patch.
        baseline = ctx.get("input_commit") or ctx["base_commit"]
        git(["add", "-A", "-N"], cwd)
        ns = git(["diff", "--name-status", "--no-ext-diff", "-z", baseline], cwd)
        changed = _parse_name_status(ns["stdout"])
        diff_res = git(["diff", "--binary", "--no-color", "--no-ext-diff", baseline], cwd)
        diff_text = diff_res["stdout"]
        touched = {c["path"] for c in changed}
        touched.update(c["old_path"] for c in changed if c.get("status") == "renamed")
        outside = sorted(p for p in touched if not path_matches(p, task.get("allowed_paths")))
        return {
            "kind": "git", "changed_files": changed,
            "diff": diff_text, "diff_sha256": util.sha256_text(diff_text) if diff_text else None,
            "diff_chars": len(diff_text),
            "scope_ok": not outside, "outside_scope": outside,
            "base_commit": ctx.get("base_commit"),
            "input_commit": ctx.get("input_commit"),
            "worktree": ctx.get("worktree"),
            "applied_patches": ctx.get("applied_patches", []),
            "previous_patches": ctx.get("previous_patches", []),
            "previous_base_changed": bool(ctx.get("previous_base_changed")),
        }

    def check_patch_conflict(self, base_commit: str | None, dep_patches: list[dict],
                             diff_text: str | None) -> dict | None:
        if not self.is_git or not base_commit or not diff_text:
            return None
        tmp_parent = self.run_dir / "tmp"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        with self._git_lock:
            tmp = Path(tempfile.mkdtemp(prefix="conflict-", dir=str(tmp_parent)))
            try:
                git(["worktree", "add", "--detach", str(tmp), base_commit], self.repo)
                for patch in dep_patches:
                    res = git(["apply", "--whitespace=nowarn", str(patch["path"])], tmp, check=False)
                    if not res["ok"]:
                        return {"ok": False, "reason": f"上游补丁无法叠加：{patch.get('task')}",
                                "output": security.redact(res["output"])[:800]}
                patch_file = tmp / ".swarm-patch.diff"
                patch_file.write_text(diff_text, encoding="utf-8")
                res = git(["apply", "--check", str(patch_file)], tmp, check=False)
                return {"ok": res["ok"], "output": security.redact(res["output"])[:800],
                        "reason": None if res["ok"] else "补丁在基线上无法应用"}
            except WorkspaceError as exc:
                return {"ok": False, "reason": str(exc), "output": ""}
            finally:
                git(["worktree", "remove", "--force", str(tmp)], self.repo, check=False)
                shutil.rmtree(tmp, ignore_errors=True)

    def copy_for_negative_check(self, workdir: Path) -> Path:
        parent = self.run_dir / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="negative-", dir=str(parent)))
        dst = tmp / "tree"
        shutil.copytree(workdir, dst, ignore=COPY_IGNORE, symlinks=True)
        return dst
