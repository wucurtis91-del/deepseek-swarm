"""Role isolation, provider validation, prices and loop defaults."""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import util

KNOWN_PROVIDERS = {
    "anthropic": {
        "model_prefix": "claude",
        "endpoints": ("anthropic.com",),
    },
    "deepseek": {
        "model_prefix": "deepseek",
        "endpoints": ("deepseek.com",),
    },
}

# DeepSeek peak hours, from api-docs.deepseek.com/quick_start/pricing (read 2026-10-23):
# Monday to Friday, 01:00-04:00 and 06:00-10:00 UTC. Every other hour, weekends included,
# is off-peak at half price. Chinese public holidays are off-peak too but are not
# modelled, so on those days the estimate is the higher peak rate.
DEEPSEEK_PEAK = {"weekdays": [0, 1, 2, 3, 4], "windows_utc": [["01:00", "04:00"], ["06:00", "10:00"]],
                 "off_peak_factor": 0.5}

# USD per 1M tokens. Sources and update dates travel with each entry so an
# estimate can always be traced. Verified 2026-10-23.
PRICES = {
    "deepseek-flash": {
        "input": 0.30, "cached_input": 0.006, "cache_write": 0.30, "output": 1.20,
        "source": "https://api-docs.deepseek.com/quick_start/pricing (peak rates; off-peak 50%)",
        "updated": "2026-10-23",
        "peak": DEEPSEEK_PEAK,
    },
    "deepseek-v4-pro": {
        "input": 1.32, "cached_input": 0.044, "cache_write": 1.32, "output": 3.96,
        "source": "https://api-docs.deepseek.com/quick_start/pricing (peak rates; off-peak 50%)",
        "updated": "2026-10-23",
        "peak": DEEPSEEK_PEAK,
    },
    # Anthropic prices: platform.claude.com/docs/en/about-claude/pricing, read 2026-10-23.
    # Cache-write is the 5-minute rate; 1-hour writes cost more and are not modelled.
    "claude-opus-5-5": {
        "input": 4.0, "cached_input": 0.20, "cache_write": 5.0, "output": 20.0,
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        "updated": "2026-10-23",
    },
    "claude-sonnet-5-5": {
        "input": 2.0, "cached_input": 0.10, "cache_write": 2.5, "output": 10.0,
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        "updated": "2026-10-23",
    },
    "claude-haiku-5-5": {
        "input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50,
        "source": "https://platform.claude.com/docs/en/about-claude/pricing (prompts <=100k; long-context rates are higher)",
        "updated": "2026-10-23",
    },
    "claude-fable-5-1": {
        "input": 10.0, "cached_input": 0.25, "cache_write": 12.5, "output": 50.0,
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        "updated": "2026-10-23",
    },
}

DEFAULT_ROLES = {
    "planner": {
        "backend": "claude_cli", "provider": "anthropic", "model": "claude-opus-5-5",
        "endpoint": None, "auth": "local_login", "api_key_env": None,
        "tools": "read", "max_output_tokens": 16000, "max_turns": 20, "timeout": 900,
        "reasoning_effort": None,
    },
    "worker": {
        "backend": "claude_cli", "provider": "deepseek", "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/anthropic", "auth": "api_key",
        "api_key_env": "DEEPSEEK_API_KEY",
        "tools": "read", "max_output_tokens": 64000, "max_turns": 30, "timeout": 900,
        "reasoning_effort": None,
    },
    "reviewer": {
        "backend": "claude_cli", "provider": "deepseek", "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/anthropic", "auth": "api_key",
        "api_key_env": "DEEPSEEK_API_KEY",
        "tools": "read", "max_output_tokens": 48000, "max_turns": 20, "timeout": 900,
        "reasoning_effort": None,
    },
    "critic": {
        "backend": "claude_cli", "provider": "deepseek", "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/anthropic", "auth": "api_key",
        "api_key_env": "DEEPSEEK_API_KEY",
        "tools": "read", "max_output_tokens": 16000, "max_turns": 15, "timeout": 600,
        "reasoning_effort": None, "enabled": False,
    },
    "compressor": {
        "backend": "claude_cli", "provider": "deepseek", "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/anthropic", "auth": "api_key",
        "api_key_env": "DEEPSEEK_API_KEY",
        "tools": "none", "max_output_tokens": 16000, "max_turns": 5, "timeout": 600,
        "reasoning_effort": None, "enabled": False,
    },
}

DEFAULT_LOOP = {
    "concurrency": 4,
    "max_repairs_per_task": 3,
    "exec_retries": 1,
    "review_retries": 2,
    "planner_retries": 1,
    "no_progress_limit": 3,
    "repeated_failure_limit": 3,
    "packet_max_chars": 30000,
    "max_tasks": 100,
    "max_steps": 50,
    "research_mode": False,
    "critic": {"enabled": False, "on_repeated_failures": 2},
    "workspace": {"repo": None, "isolate": "worktree", "require_git_for_code": True},
    "check_timeout": 300,
    "check_output_chars": 3000,
    "check_env_allow": [],
    "packet_compress": {"enabled": False, "min_chars": 60000},
    "review_max_artifact_chars": 24000,
    "upstream_chars": 6000,
}

DEFAULT_BUDGET = {
    "max_calls": 200,
    "max_workflow_rounds": 30,
    "max_seconds": 7200,
    "max_tokens": 5000000,
    "max_usd": None,            # soft: evaluated with the price table, may be a lower bound
    "max_unknown_calls": 20,    # hard: calls whose cost/usage is unknown
    "max_repairs_per_task": 3,
    "max_calls_per_role": {"planner": 60},
}

TOOL_PRESETS = {
    "none": ([], None),
    "read": (["Read", "Grep", "Glob"], None),
    "edit": (["Read", "Grep", "Glob", "Edit", "Write"], "acceptEdits"),
    "bash": (["Read", "Grep", "Glob", "Edit", "Write", "Bash"], "bypassPermissions"),
}


class ConfigError(ValueError):
    pass


@dataclass
class Role:
    name: str
    backend: str
    provider: str
    model: str
    endpoint: str | None
    auth: str
    api_key_env: str | None
    tools: str
    max_output_tokens: int
    max_turns: int
    timeout: int
    reasoning_effort: str | None = None
    enabled: bool = True

    def fingerprint(self) -> dict:
        return {
            "backend": self.backend, "provider": self.provider, "model": self.model,
            "endpoint": self.endpoint, "auth": self.auth,
            "credential_source": self.api_key_env if self.auth == "api_key" else "local_login",
            "tools": self.tools, "max_output_tokens": self.max_output_tokens,
            "max_turns": self.max_turns, "timeout": self.timeout,
            "reasoning_effort": self.reasoning_effort,
        }

    def sanitized(self) -> dict:
        return asdict(self)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_workspace(values: dict | None) -> dict:
    """Normalize and validate the workspace settings used before a run starts."""
    values = dict(values or {})
    isolate = values.get("isolate", "worktree")
    if isolate != "worktree":
        raise ConfigError(
            "代码任务只支持 workspace.isolate=worktree；isolate=none 没有实现，"
            "不会创建 worktree，也不会静默改走其他目录。")
    values["isolate"] = "worktree"
    if values.get("repo") is not None and not isinstance(values.get("repo"), str):
        raise ConfigError("workspace.repo 必须是路径字符串或 null")
    return values


def load_config(path: str | None = None) -> dict:
    cfg = {"roles": copy.deepcopy(DEFAULT_ROLES), "prices": copy.deepcopy(PRICES),
           "loop": copy.deepcopy(DEFAULT_LOOP), "budget": copy.deepcopy(DEFAULT_BUDGET)}
    if path:
        raw = util.read_json(Path(path))
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件必须是 JSON 对象：{path}")
        for key in ("roles", "prices", "loop", "budget"):
            if key in raw:
                if not isinstance(raw[key], dict):
                    raise ConfigError(f"配置项 {key} 必须是对象")
                cfg[key] = _deep_merge(cfg[key], raw[key])
    cfg["loop"] = normalize_loop(cfg["loop"])
    return cfg


def _endpoint_host(endpoint: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/:]+)", endpoint or "")
    return (m.group(1) if m else (endpoint or "")).lower()


def validate_roles(roles: dict, allow_fake: bool = False) -> dict[str, Role]:
    parsed: dict[str, Role] = {}
    names = ["planner", "worker", "reviewer", "critic"]
    if isinstance(roles, dict) and "compressor" in roles:
        names.append("compressor")
    for name in names:
        raw = roles.get(name)
        if not isinstance(raw, dict):
            raise ConfigError(f"缺少角色配置：{name}")
        backend = str(raw.get("backend") or "claude_cli")
        provider = str(raw.get("provider") or "").lower()
        model = str(raw.get("model") or "").strip()
        endpoint = raw.get("endpoint")
        auth = str(raw.get("auth") or ("local_login" if provider == "anthropic" else "api_key"))
        api_key_env = raw.get("api_key_env")
        tools = str(raw.get("tools") or ("read" if name != "worker" else "read"))
        if tools not in TOOL_PRESETS:
            raise ConfigError(f"角色 {name} 的 tools 只能是 {list(TOOL_PRESETS)}")
        if not model:
            raise ConfigError(f"角色 {name} 缺少 model")
        if backend not in ("claude_cli", "fake"):
            raise ConfigError(f"角色 {name} 的 backend 只能是 claude_cli 或 fake")
        if backend == "fake" and not allow_fake:
            raise ConfigError(
                f"角色 {name} 使用 fake backend。它只用于离线测试；"
                "命令行需要显式 --allow-fake-backend 才允许。")
        if backend == "claude_cli":
            if provider not in KNOWN_PROVIDERS:
                raise ConfigError(f"角色 {name} 的 provider 必须是 {sorted(KNOWN_PROVIDERS)} 之一")
            info = KNOWN_PROVIDERS[provider]
            if not model.lower().startswith(info["model_prefix"]):
                raise ConfigError(
                    f"角色 {name} 配置为 provider={provider}，但 model={model!r} 不是该 provider 的模型。"
                    f"这通常意味着 planner/worker 的 provider 与 endpoint 串用了。")
            host = _endpoint_host(endpoint) if endpoint else ""
            if endpoint and not any(host == d or host.endswith("." + d) for d in info["endpoints"]):
                raise ConfigError(
                    f"角色 {name} 的 endpoint={endpoint!r} 不属于 provider={provider}。"
                    "拒绝把模型名和实际 endpoint 混用。")
            if provider == "deepseek" and not endpoint:
                raise ConfigError(f"角色 {name} 是 DeepSeek，必须显式配置 endpoint")
            if provider == "deepseek" and "/anthropic" not in str(endpoint):
                raise ConfigError(
                    f"角色 {name} 的 DeepSeek endpoint 应指向 Anthropic 兼容接口，"
                    "例如 https://api.deepseek.com/anthropic")
            if auth == "local_login":
                if provider != "anthropic":
                    raise ConfigError(f"角色 {name} 只有 Anthropic 可以使用本地 Claude 登录")
                if endpoint:
                    raise ConfigError(f"角色 {name} 使用 local_login 时不能配置 endpoint")
                if api_key_env:
                    raise ConfigError(f"角色 {name} 使用 local_login 时不能同时指定 api_key_env")
            elif auth == "api_key":
                if not api_key_env:
                    raise ConfigError(f"角色 {name} 使用 api_key 时必须指定 api_key_env")
            else:
                raise ConfigError(f"角色 {name} 的 auth 只能是 api_key 或 local_login")
        else:
            if not provider:
                provider = "fake"
        effort = raw.get("reasoning_effort")
        if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"角色 {name} 的 reasoning_effort 只能是 low/medium/high/xhigh/max")
        if effort is not None and provider == "deepseek":
            raise ConfigError(
                f"角色 {name} 使用 DeepSeek endpoint；没有验证过该接口支持 --effort，"
                "请不要把未验证的推理参数当成已生效。")
        try:
            role = Role(
                name=name, backend=backend, provider=provider, model=model,
                endpoint=endpoint, auth=auth, api_key_env=api_key_env, tools=tools,
                max_output_tokens=int(raw.get("max_output_tokens", 16000)),
                max_turns=int(raw.get("max_turns", 20)),
                timeout=int(raw.get("timeout", 900)),
                reasoning_effort=effort,
                enabled=bool(raw.get("enabled", True)),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"角色 {name} 的数值配置无效：{exc}") from exc
        for field_name in ("max_output_tokens", "max_turns", "timeout"):
            if getattr(role, field_name) <= 0:
                raise ConfigError(f"角色 {name} 的 {field_name} 必须是正整数")
        if name in ("planner", "worker", "reviewer") and not role.enabled:
            raise ConfigError(f"角色 {name} 不能被禁用")
        if name == "planner" and role.tools not in ("read", "none"):
            raise ConfigError("planner 只能用 read 或 none 工具，避免主持者直接改产物")
        parsed[name] = role
    if parsed["planner"].provider == "deepseek" and parsed["planner"].model.startswith("deepseek"):
        # Allowed in principle (a cheaper planner), but it must be explicit and
        # the model/endpoint check above already prevents the dangerous mixed case.
        pass
    return parsed


def normalize_budget(values: dict | None) -> dict:
    budget = _deep_merge(DEFAULT_BUDGET, values or {})
    for key in ("max_calls", "max_workflow_rounds", "max_seconds", "max_tokens",
                "max_unknown_calls", "max_repairs_per_task"):
        value = budget.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ConfigError(f"budget.{key} 必须是非负整数")
    if budget.get("max_usd") is not None:
        try:
            budget["max_usd"] = float(budget["max_usd"])
        except (TypeError, ValueError) as exc:
            raise ConfigError("budget.max_usd 必须是数字或 null") from exc
        if budget["max_usd"] < 0:
            raise ConfigError("budget.max_usd 不能为负")
    if not isinstance(budget.get("max_calls_per_role"), dict):
        raise ConfigError("budget.max_calls_per_role 必须是对象")
    for role, value in budget["max_calls_per_role"].items():
        if type(value) is not int or value < 0:
            raise ConfigError(f"budget.max_calls_per_role.{role} 必须是非负整数")
    return budget


def normalize_loop(values: dict | None) -> dict:
    loop = _deep_merge(DEFAULT_LOOP, values or {})
    for key in ("concurrency", "exec_retries", "review_retries", "planner_retries",
                "no_progress_limit", "repeated_failure_limit", "packet_max_chars",
                "max_tasks", "max_steps", "check_timeout", "check_output_chars",
                "review_max_artifact_chars", "upstream_chars"):
        value = loop.get(key)
        if type(value) is not int or value < 0:
            raise ConfigError(f"loop.{key} 必须是非负整数")
    if loop["concurrency"] < 1 or loop["max_steps"] < 1 or loop["packet_max_chars"] < 1:
        raise ConfigError("loop.concurrency / max_steps / packet_max_chars 必须大于 0")
    if not isinstance(loop.get("check_env_allow"), list) or \
            not all(isinstance(x, str) for x in loop["check_env_allow"]):
        raise ConfigError("loop.check_env_allow 必须是字符串列表")
    if not isinstance(loop.get("research_mode"), bool):
        raise ConfigError("loop.research_mode 必须是布尔值")
    critic = loop.get("critic")
    if not isinstance(critic, dict) or not isinstance(critic.get("enabled"), bool) or \
            type(critic.get("on_repeated_failures")) is not int:
        raise ConfigError("loop.critic 必须是 {enabled: bool, on_repeated_failures: int}")
    compress = loop.get("packet_compress")
    if not isinstance(compress, dict) or not isinstance(compress.get("enabled"), bool) or \
            type(compress.get("min_chars")) is not int:
        raise ConfigError("loop.packet_compress 必须是 {enabled: bool, min_chars: int}")
    workspace = loop.get("workspace")
    if not isinstance(workspace, dict) or \
            not isinstance(workspace.get("require_git_for_code"), bool):
        raise ConfigError(
            "loop.workspace 必须包含 isolate=worktree（当前唯一支持值）和 require_git_for_code")
    validate_workspace(workspace)
    return loop


def normalize_goal(obj: dict) -> dict:
    if not isinstance(obj, dict):
        raise ConfigError("目标文件必须是 JSON 对象")
    text = obj.get("goal") or obj.get("objective")
    if not isinstance(text, str) or not text.strip():
        raise ConfigError("目标文件缺少非空的 goal 字段")
    constraints = obj.get("constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
        raise ConfigError("constraints 必须是字符串列表")
    raw = obj.get("completion_criteria")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("completion_criteria 必须是非空列表")
    criteria, seen = [], set()
    for i, item in enumerate(raw, 1):
        if isinstance(item, str):
            cid, ctext = f"g{i}", item
        elif isinstance(item, dict):
            cid = str(item.get("id") or f"g{i}")
            ctext = item.get("text")
        else:
            raise ConfigError("completion_criteria 的每一项必须是字符串或 {id,text}")
        if not isinstance(ctext, str) or not ctext.strip():
            raise ConfigError(f"完成条件 {cid} 缺少 text")
        if not util.ACCEPT_ID.fullmatch(cid):
            raise ConfigError(f"完成条件 id 不合法：{cid!r}")
        if cid in seen:
            raise ConfigError(f"完成条件 id 重复：{cid}")
        seen.add(cid)
        criteria.append({"id": cid, "text": ctext.strip()})
    research = obj.get("research", False)
    if not isinstance(research, bool):
        raise ConfigError("goal.research 必须是布尔值")
    goal = {
        "text": text.strip(),
        "constraints": [c.strip() for c in constraints if c.strip()],
        "criteria": criteria,
        "workspace": obj.get("workspace"),
        "research": research,
    }
    if goal["workspace"] is not None and not isinstance(goal["workspace"], dict):
        raise ConfigError("goal.workspace 必须是对象")
    if obj.get("budget") is not None:
        if not isinstance(obj["budget"], dict):
            raise ConfigError("goal.budget 必须是对象")
        goal["budget"] = obj["budget"]
    return goal


def price_for(model: str, prices: dict | None = None) -> dict | None:
    table = prices if prices is not None else PRICES
    base = str(model or "").split("[")[0].strip()
    entry = table.get(base)
    return entry if isinstance(entry, dict) else None


def _minutes(hhmm: str) -> int:
    hh, mm = map(int, str(hhmm).split(":"))
    return hh * 60 + mm


def peak_factor(price: dict, when: dt.datetime) -> float:
    """Price multiplier at `when`: 1.0 inside a peak window, off_peak_factor outside.

    `windows_utc` lists the peak windows on `weekdays` (Monday is 0). The older
    off_peak_start/off_peak_end form, a single daily off-peak window in a fixed
    UTC offset, is still read for custom price tables."""
    peak = price.get("peak")
    if not isinstance(peak, dict):
        return 1.0
    try:
        if "windows_utc" in peak:
            utc = when.astimezone(dt.timezone.utc)
            minutes = utc.hour * 60 + utc.minute
            in_peak = utc.weekday() in peak.get("weekdays", range(7)) and any(
                _minutes(a) <= minutes < _minutes(b) for a, b in peak["windows_utc"])
            return 1.0 if in_peak else float(peak.get("off_peak_factor", 0.5))
        offset = int(peak.get("utc_offset_hours", 0))
        local = when.astimezone(dt.timezone(dt.timedelta(hours=offset)))
        hh, mm = map(int, str(peak.get("off_peak_start", "00:30")).split(":"))
        sh, sm = map(int, str(peak.get("off_peak_end", "08:30")).split(":"))
        minutes = local.hour * 60 + local.minute
        start, end = hh * 60 + mm, sh * 60 + sm
        in_off = start <= minutes < end if start <= end else (minutes >= start or minutes < end)
        return float(peak.get("off_peak_factor", 0.5)) if in_off else 1.0
    except (TypeError, ValueError, KeyError):
        return 1.0


def normalize_usage(usage: dict | None) -> dict:
    """Preserve provider usage; map alternate field names without double counting."""
    raw = usage if isinstance(usage, dict) else {}
    out = {
        "input_tokens": raw.get("input_tokens"),
        "cache_read_input_tokens": raw.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": raw.get("cache_creation_input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "reasoning_tokens": None,
        "raw": raw,
    }
    # OpenAI-format DeepSeek usage, if a provider returns it.
    if out["input_tokens"] is None and raw.get("prompt_cache_miss_tokens") is not None:
        out["input_tokens"] = raw.get("prompt_cache_miss_tokens")
    if out["cache_read_input_tokens"] is None and raw.get("prompt_cache_hit_tokens") is not None:
        out["cache_read_input_tokens"] = raw.get("prompt_cache_hit_tokens")
    if out["output_tokens"] is None and raw.get("completion_tokens") is not None:
        out["output_tokens"] = raw.get("completion_tokens")
    details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        out["reasoning_tokens"] = details.get("reasoning_tokens")
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "reasoning_tokens"):
        value = out.get(key)
        out[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    # Input and output are required to price a call. Missing cache counters are
    # treated as zero but the basis says so, so an undercount is visible.
    out["complete"] = out["input_tokens"] is not None and out["output_tokens"] is not None
    out["assumed_zero"] = [k for k in ("cache_read_input_tokens", "cache_creation_input_tokens")
                           if out.get(k) is None]
    out["cache_read_input_tokens"] = out.get("cache_read_input_tokens") or 0
    out["cache_creation_input_tokens"] = out.get("cache_creation_input_tokens") or 0
    return out


def estimate_cost(usage: dict | None, model: str, when: dt.datetime | None = None,
                  prices: dict | None = None) -> dict:
    """Cost estimate with an explicit coverage status. Never invents a cheap price."""
    norm = normalize_usage(usage)
    price = price_for(model, prices)
    when = when or util.now()
    if price is None:
        return {"usd": None, "known": False, "reason": f"unknown_price:{model}",
                "price": None, "usage": norm}
    if not norm["complete"]:
        return {"usd": None, "known": False, "reason": "usage_incomplete",
                "price": price, "usage": norm}
    if usage == {} or usage is None:
        return {"usd": None, "known": False, "reason": "usage_missing",
                "price": price, "usage": norm}
    factor = peak_factor(price, when)
    try:
        usd = (
            (norm["input_tokens"] or 0) * float(price.get("input", 0))
            + (norm["cache_read_input_tokens"] or 0) * float(price.get("cached_input", price.get("input", 0)))
            + (norm["cache_creation_input_tokens"] or 0) * float(price.get("cache_write", price.get("input", 0)))
            + (norm["output_tokens"] or 0) * float(price.get("output", 0))
        ) / 1e6 * factor
    except (TypeError, ValueError):
        return {"usd": None, "known": False, "reason": "bad_price_entry",
                "price": price, "usage": norm}
    return {"usd": round(usd, 8), "known": True, "reason": None,
            "price": price, "price_factor": factor, "usage": norm}
