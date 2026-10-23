"""Model backends. The real backend drives the Claude Code CLI; fake is test-only."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import config, procs, security, util


class BackendError(RuntimeError):
    pass


def find_claude() -> str:
    path = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(path).exists():
        raise BackendError("找不到 claude 命令行（Claude Code CLI）。")
    return path


def key_from_cc_switch() -> str | None:
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
    for (raw,) in rows:
        if raw is None:
            continue
        try:
            cfg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env")
        if not isinstance(env, dict):
            continue
        base = env.get("ANTHROPIC_BASE_URL") or ""
        if "deepseek.com" in str(base):
            key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
            if isinstance(key, str) and key:
                return key
    return None


def resolve_api_key(role: config.Role) -> str:
    if role.auth != "api_key":
        return ""
    source = role.api_key_env or ""
    key = os.environ.get(source) if source else None
    if not key and role.provider == "deepseek":
        key = key_from_cc_switch()
    if not key:
        raise BackendError(
            f"角色 {role.name} 需要 {source or 'API key'}，但环境变量未设置"
            + ("，CC Switch 里也没有可用的 DeepSeek 供应商。" if role.provider == "deepseek" else "。"))
    return str(key)


def classify_error(text: str) -> str:
    low = (text or "").lower()
    if any(s in low for s in ("429", "rate limit", "rate_limit", "too many requests",
                              "overloaded", "529", "500", "502", "503", "504",
                              "timeout", "timed out", "econnreset", "connection reset",
                              "network error", "暂时")):
        return "transient"
    if any(s in low for s in ("401", "403", "unauthorized", "authentication", "invalid api key",
                              "api key", "invalid x-api-key", "permission denied")):
        return "auth"
    if any(s in low for s in ("400", "invalid model", "model not found", "unknown model",
                              "not supported", "invalid request", "context length",
                              "maximum context", "bad request")):
        return "config"
    return "unknown"


def _observed_models(data: dict) -> list[str]:
    found = []
    for key in ("model", "modelName", "model_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    for key in ("modelUsage", "model_usage"):
        value = data.get(key)
        if isinstance(value, dict):
            found.extend(str(k) for k in value.keys() if str(k).strip())
    return sorted(set(found))


def _identity(requested: str, observed: list[str], provider: str = "") -> str:
    if not observed:
        return "unknown"
    req = requested.split("[")[0].lower()
    for item in observed:
        obs = item.split("[")[0].lower()
        if req == obs or obs.startswith(req) or req.startswith(obs):
            return "verified"
    # DeepSeek publishes aliases (deepseek-flash is served by a versioned model).
    # A different concrete name is recorded as unknown identity, never claimed as
    # a verified match; a different family is a routing mismatch.
    if provider == "deepseek" and all(o.startswith("deepseek") for o in observed):
        return "unknown" if req.startswith("deepseek") else "mismatch"
    # A different model in the same provider family is still a real substitution;
    # only the requested/observed prefix relation above counts as verified.
    return "mismatch"


class ClaudeBackend:
    """One `claude -p` call for one configured role."""

    def __init__(self, role: config.Role, claude: str | None = None,
                 keep_sessions: bool = False):
        self.role = role
        self.claude = claude or find_claude()
        self.keep_sessions = keep_sessions
        self._key = resolve_api_key(role) if role.backend == "claude_cli" else ""

    def _env(self, max_output_tokens: int | None = None) -> dict:
        role = self.role
        extra = {
            "TERM": "dumb",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens or role.max_output_tokens),
        }
        env = security.minimal_env(extra=extra)
        # Never inherit a provider credential from the calling session.
        for name in list(env):
            if name.startswith("ANTHROPIC_") and name not in (
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
                env.pop(name, None)
        if role.endpoint:
            env["ANTHROPIC_BASE_URL"] = role.endpoint
        if role.auth == "api_key":
            env["ANTHROPIC_API_KEY"] = self._key
        return env

    def command(self, *, tools: str, max_turns: int, add_dirs=(), model: str | None = None) -> list[str]:
        role = self.role
        effective_model = model or role.model
        tool_list, mode = config.TOOL_PRESETS[tools]
        cmd = [
            self.claude, "-p",
            "--output-format", "json",
            "--model", effective_model,
            "--max-turns", str(max_turns),
            "--tools", ",".join(tool_list),
        ]
        if role.auth == "local_login":
            # Keeps a user's Claude subscription/CLI login working while disabling
            # CLAUDE.md, plugins, hooks and MCP, which --bare would also do but it
            # refuses OAuth/keychain credentials.
            cmd += ["--safe-mode", "--setting-sources", "", "--strict-mcp-config"]
        else:
            cmd += ["--bare", "--setting-sources", "", "--strict-mcp-config"]
        if mode:
            cmd += ["--permission-mode", mode]
        if role.reasoning_effort:
            cmd += ["--effort", role.reasoning_effort]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        if not self.keep_sessions:
            cmd.append("--no-session-persistence")
        return cmd

    def call(self, prompt: str, *, cwd, tools: str | None = None,
             max_turns: int | None = None, timeout: int | None = None,
             add_dirs=(), cancel_event=None, purpose: str = "",
             model: str | None = None,
             max_output_tokens: int | None = None) -> dict:
        role = self.role
        effective_model = model or role.model
        tools = tools or role.tools
        max_turns = max_turns or role.max_turns
        timeout = timeout or role.timeout
        cmd = self.command(tools=tools, max_turns=max_turns, add_dirs=add_dirs,
                           model=effective_model)
        start = time.time()
        base = {
            "ok": False, "text": "", "usage": {}, "raw_usage": {},
            "model_requested": effective_model, "model_observed": [],
            "identity": "unknown", "provider": role.provider,
            "endpoint": role.endpoint, "role": role.name,
            "total_cost_usd": None, "seconds": 0.0, "turns": None,
            "error": None, "error_kind": None, "cancelled": False,
            "usage_missing": True,
        }
        proc = procs.run(cmd, shell=False, cwd=cwd, env=self._env(max_output_tokens),
                         input_text=prompt, timeout=timeout, cancel_event=cancel_event)
        base["seconds"] = proc["seconds"]
        base["exit_code"] = proc.get("code")
        base["cancelled"] = bool(proc.get("cancelled"))
        if proc.get("timed_out"):
            base.update(ok=False, error=f"调用超时（{timeout}s）", error_kind="timeout")
            return base
        if proc.get("cancelled"):
            base.update(ok=False, error="已取消", error_kind="cancelled")
            return base
        if proc.get("error") == "spawn_failed":
            base.update(ok=False, error=security.redact(proc.get("output", ""))[:500],
                        error_kind="config")
            return base
        stdout = proc.get("stdout", "")
        try:
            data = json.loads(stdout)
        except ValueError:
            err = security.redact((proc.get("output") or stdout or "").strip())[-800:]
            base.update(ok=False, error=err or f"退出码 {proc.get('code')}",
                        error_kind=classify_error(err))
            return base
        if not isinstance(data, dict):
            base.update(ok=False, error="CLI 返回的 JSON 不是对象", error_kind="config")
            return base
        observed = _observed_models(data)
        base["model_observed"] = observed
        base["identity"] = _identity(effective_model, observed, role.provider)
        usage = data.get("usage")
        if isinstance(usage, dict) and usage:
            base["usage"] = usage
            base["raw_usage"] = usage
            base["usage_missing"] = False
        if isinstance(data.get("total_cost_usd"), (int, float)) and not isinstance(data.get("total_cost_usd"), bool):
            base["total_cost_usd"] = float(data.get("total_cost_usd"))
        if data.get("num_turns") is not None:
            base["turns"] = data.get("num_turns")
        is_error = data.get("is_error")
        if is_error is not None and not isinstance(is_error, bool):
            base.update(ok=False, error="CLI 返回的 is_error 不是布尔值", error_kind="config")
            return base
        if is_error is True:
            msg = data.get("result") or data.get("subtype") or "调用返回错误"
            msg = security.redact(str(msg))[:800]
            base.update(ok=False, error=msg, error_kind=classify_error(msg))
            return base
        if proc.get("code") != 0:
            msg = security.redact(str(proc.get("stderr") or
                                      f"CLI 退出码 {proc.get('code')}"))[-800:]
            base.update(ok=False, error=msg, error_kind=classify_error(msg))
            return base
        text = data.get("result")
        if not isinstance(text, str):
            text = ""
        base.update(ok=True, text=text, error=None, error_kind=None)
        return base


class FakeBackend:
    """Scripted backend for offline tests and benchmarks.

    `responses[role]` is a list of strings/dicts/callables, or one callable that
    receives the call arguments. This is deliberately not wired into any default
    production configuration.
    """

    def __init__(self, role: config.Role, responses=None, usage: dict | None = None,
                 model_observed: list[str] | None = None):
        self.role = role
        self.responses = responses
        self.usage = usage if usage is not None else {
            "input_tokens": 100, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "output_tokens": 50, "total_tokens": 150,
        }
        self.model_observed = model_observed or [role.model]
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def call(self, prompt: str, *, cwd, tools: str | None = None,
             max_turns: int | None = None, timeout: int | None = None,
             add_dirs=(), cancel_event=None, purpose: str = "",
             model: str | None = None,
             max_output_tokens: int | None = None) -> dict:
        effective_model = model or self.role.model
        with self._lock:
            self.calls.append({"prompt": prompt, "cwd": str(cwd), "tools": tools or self.role.tools,
                               "model": effective_model, "role": self.role.name,
                               "purpose": purpose, "max_output_tokens": max_output_tokens})
            response = None
            if callable(self.responses):
                response = self.responses(prompt=prompt, cwd=cwd, tools=tools,
                                          purpose=purpose, role=self.role.name,
                                          max_output_tokens=max_output_tokens)
            elif isinstance(self.responses, dict):
                key = purpose or self.role.name
                queue = self.responses.get(key) or []
                response = queue.pop(0) if isinstance(queue, list) and queue else None
            elif isinstance(self.responses, list) and self.responses:
                response = self.responses.pop(0)
        base = {"ok": True, "text": "", "usage": dict(self.usage), "raw_usage": dict(self.usage),
                "model_requested": effective_model, "model_observed": list(self.model_observed),
                "identity": _identity(effective_model, list(self.model_observed), self.role.provider),
                "provider": self.role.provider, "endpoint": self.role.endpoint,
                "role": self.role.name, "total_cost_usd": None, "seconds": 0.01,
                "turns": 1, "error": None, "error_kind": None, "cancelled": False,
                "usage_missing": not bool(self.usage)}
        if response is None:
            base.update(ok=False, error="fake backend 没有更多脚本响应", error_kind="config")
            return base
        if isinstance(response, Exception):
            base.update(ok=False, error=str(response), error_kind=classify_error(str(response)))
            return base
        if isinstance(response, str):
            base["text"] = response
        elif isinstance(response, dict):
            base.update({k: v for k, v in response.items()
                         if k in ("ok", "text", "usage", "raw_usage", "error", "error_kind",
                                  "model_observed", "identity", "cancelled", "turns",
                                  "total_cost_usd", "usage_missing")})
            if "usage" in response:
                base["raw_usage"] = response["usage"]
        else:
            base.update(ok=False, error="fake 响应类型不支持", error_kind="config")
        if base.get("cancelled"):
            base["ok"] = False
            base["error_kind"] = "cancelled"
        return base


def build_backends(roles: dict[str, config.Role], *, allow_fake: bool = False,
                   keep_sessions: bool = False, fake_responses: dict | None = None,
                   fake_usage: dict | None = None) -> dict:
    backends = {}
    for name, role in roles.items():
        if not role.enabled:
            continue
        if role.backend == "fake":
            if not allow_fake:
                raise BackendError(f"角色 {name} 是 fake backend，需要显式允许。")
            responses = (fake_responses or {}).get(name)
            backends[name] = FakeBackend(role, responses, usage=fake_usage)
        elif role.backend == "claude_cli":
            backends[name] = ClaudeBackend(role, keep_sessions=keep_sessions)
        else:
            raise BackendError(f"不支持的角色 backend：{role.backend}")
    return backends
