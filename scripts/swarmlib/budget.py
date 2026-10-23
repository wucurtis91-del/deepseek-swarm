"""Run-level budget with per-role metering and concurrent reservations."""
from __future__ import annotations

import copy
import datetime as dt
import threading

from . import config as cfgmod
from . import util

ROLES = ("planner", "worker", "reviewer", "critic", "compressor")


def _blank_role() -> dict:
    return {
        "calls": 0, "retry_calls": 0, "external_decisions": 0,
        "input_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0,
        "reasoning_tokens": 0, "seconds": 0.0,
        "cost_usd": 0.0, "cost_known": True, "cost_unknown_calls": 0,
        "usage_unknown_calls": 0, "provider_reported_usd": 0.0,
        "models": {},
    }


class Budget:
    def __init__(self, limits: dict, prices: dict, snapshot: dict | None = None,
                 started_at: str | None = None):
        self.limits = copy.deepcopy(limits)
        self.prices = prices if prices is not None else cfgmod.PRICES
        self.started_at = started_at or util.iso()
        self.lock = threading.Lock()
        self.roles = {r: _blank_role() for r in ROLES}
        self.cost_unknown_calls = 0
        self.usage_unknown_calls = 0
        self.external_unmetered = 0
        self.reserved = {"calls": 0, "tokens": 0, "usd": 0.0}
        self.reserved_roles = {r: 0 for r in ROLES}
        self.reserved_unknown = 0
        self._reservations: dict[str, dict] = {}
        self._res_seq = 0
        self.stop_reason: str | None = None
        if snapshot:
            self._load(snapshot)

    def _load(self, snap: dict) -> None:
        for role in ROLES:
            if isinstance((snap.get("roles") or {}).get(role), dict):
                self.roles[role].update(snap["roles"][role])
        self.cost_unknown_calls = int(snap.get("cost_unknown_calls", 0))
        self.usage_unknown_calls = int(snap.get("usage_unknown_calls", 0))
        self.external_unmetered = int(snap.get("external_unmetered", 0))
        self.started_at = snap.get("started_at", self.started_at)
        self.limits.update(snap.get("limits") or {})

    # ------------------------------------------------------------ reservation
    def _totals(self) -> dict:
        calls = sum(r["calls"] for r in self.roles.values())
        tokens = sum(r["input_tokens"] + r["cache_read_input_tokens"]
                     + r["cache_creation_input_tokens"] + r["output_tokens"]
                     for r in self.roles.values())
        usd = sum(r["cost_usd"] for r in self.roles.values())
        return {"calls": calls, "tokens": tokens, "usd": usd}

    def elapsed_seconds(self) -> float:
        try:
            start = dt.datetime.fromisoformat(self.started_at)
            return max(0.0, (util.now() - start).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    def allow_output(self, est_input_tokens: int, requested_output: int) -> int | None:
        """The largest output cap this call may use after reserving its input.

        This is the value both reserved and passed to the backend. A CLI call can
        still make several API turns when it uses tools; the token limit is therefore
        an estimate, not a hard guarantee (recorded in every snapshot).
        """
        with self.lock:
            if self.limits.get("max_tokens") is None:
                return max(1, int(requested_output))
            totals = self._totals()
            remaining = int(self.limits["max_tokens"]) - totals["tokens"] - self.reserved["tokens"]
            if remaining <= int(est_input_tokens):
                return None
            return max(1, min(int(requested_output), remaining - int(est_input_tokens)))

    def reserve(self, role: str, est_input_tokens: int, est_output_tokens: int,
                est_usd: float | None, *, unknown_cost: bool = False
                ) -> tuple[dict | None, str | None]:
        """Check every enforceable limit and hold a slot before a concurrent call."""
        with self.lock:
            totals = self._totals()
            limits = self.limits
            if limits.get("max_seconds") and self.elapsed_seconds() >= float(limits["max_seconds"]):
                return None, "max_seconds"
            if limits.get("max_calls") is not None:
                if totals["calls"] + self.reserved["calls"] + 1 > int(limits["max_calls"]):
                    return None, "max_calls"
            per_role = (limits.get("max_calls_per_role") or {}).get(role)
            if per_role is not None and (
                    self.roles.get(role, {}).get("calls", 0)
                    + self.reserved_roles.get(role, 0) + 1 > int(per_role)):
                return None, f"max_calls:{role}"
            reserve_tokens = max(0, int(est_input_tokens)) + max(0, int(est_output_tokens))
            if limits.get("max_tokens") is not None:
                if totals["tokens"] + self.reserved["tokens"] + reserve_tokens > int(limits["max_tokens"]):
                    return None, "max_tokens"
            if limits.get("max_unknown_calls") is not None and unknown_cost:
                if self.cost_unknown_calls + self.reserved_unknown + 1 > int(limits["max_unknown_calls"]):
                    return None, "max_unknown_calls"
            if limits.get("max_usd") is not None:
                if est_usd is None:
                    # Unknown price cannot be enforced as USD. It is bounded by
                    # max_unknown_calls instead; the reported cost is a lower bound.
                    pass
                elif totals["usd"] + self.reserved["usd"] + est_usd > float(limits["max_usd"]) + 1e-12:
                    return None, "max_usd_soft"
            self._res_seq += 1
            rid = f"res-{self._res_seq}"
            self._reservations[rid] = {"role": role, "tokens": reserve_tokens,
                                       "usd": est_usd or 0.0, "unknown": bool(unknown_cost)}
            self.reserved["calls"] += 1
            self.reserved["tokens"] += reserve_tokens
            self.reserved["usd"] += est_usd or 0.0
            self.reserved_roles[role] = self.reserved_roles.get(role, 0) + 1
            if unknown_cost:
                self.reserved_unknown += 1
            return {"id": rid, "role": role, "tokens": reserve_tokens,
                    "usd": est_usd, "unknown": bool(unknown_cost)}, None

    def release(self, reservation: dict | None) -> None:
        if not reservation:
            return
        with self.lock:
            item = self._reservations.pop(reservation["id"], None)
            if item:
                self.reserved["calls"] = max(0, self.reserved["calls"] - 1)
                self.reserved["tokens"] = max(0, self.reserved["tokens"] - item["tokens"])
                self.reserved["usd"] = max(0.0, self.reserved["usd"] - item["usd"])
                role = item["role"]
                self.reserved_roles[role] = max(0, self.reserved_roles.get(role, 0) - 1)
                if item.get("unknown"):
                    self.reserved_unknown = max(0, self.reserved_unknown - 1)

    # ------------------------------------------------------------ metering
    def record(self, role: str, result: dict, model: str, *, purpose: str = "call",
               external: bool = False) -> dict:
        """Meter one model call. Returns the per-call record stored with the event.

        A timeout with no usage is *unknown*, never a zero-cost call. An external
        planner decision is also unknown, because this process cannot see that
        Claude session's usage.
        """
        usage = result.get("usage") if isinstance(result, dict) else {}
        usage_missing = bool(result.get("usage_missing", not usage)) if isinstance(result, dict) else True
        when = util.now()
        cost = cfgmod.estimate_cost(usage if not usage_missing else None, model, when, self.prices)
        record = {
            "at": util.iso(when), "role": role, "model": model,
            "provider": (result or {}).get("provider"),
            "endpoint": (result or {}).get("endpoint"),
            "identity": (result or {}).get("identity", "unknown"),
            "model_observed": (result or {}).get("model_observed") or [],
            "purpose": purpose, "seconds": (result or {}).get("seconds"),
            "usage": cost.get("usage"), "usage_missing": usage_missing,
            "cost_usd": cost.get("usd"), "cost_known": cost.get("known"),
            "cost_reason": cost.get("reason"), "price_source": (
                (cost.get("price") or {}).get("source") if cost.get("price") else None),
            "price_updated": (
                (cost.get("price") or {}).get("updated") if cost.get("price") else None),
            "provider_reported_usd": (result or {}).get("total_cost_usd"),
            "external_unmetered": external,
        }
        with self.lock:
            bucket = self.roles.setdefault(role, _blank_role())
            if external:
                bucket["external_decisions"] += 1
                self.external_unmetered += 1
                record["cost_known"] = False
                record["cost_reason"] = "external_unmetered"
                return record
            bucket["calls"] += 1
            if purpose == "retry" or str(purpose).endswith("_retry"):
                bucket["retry_calls"] += 1
            bucket["seconds"] += float(record.get("seconds") or 0)
            models = bucket.setdefault("models", {})
            models[model] = models.get(model, 0) + 1
            if usage_missing:
                self.usage_unknown_calls += 1
                self.cost_unknown_calls += 1
                bucket["usage_unknown_calls"] += 1
                bucket["cost_unknown_calls"] += 1
                bucket["cost_known"] = False
                record["cost_reason"] = record.get("cost_reason") or "usage_missing"
                return record
            norm = cost.get("usage") or {}
            for key in ("input_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens", "output_tokens"):
                bucket[key] += int(norm.get(key) or 0)
            bucket["reasoning_tokens"] += int(norm.get("reasoning_tokens") or 0)
            if cost.get("known") and cost.get("usd") is not None:
                bucket["cost_usd"] += float(cost["usd"])
            else:
                self.cost_unknown_calls += 1
                bucket["cost_unknown_calls"] += 1
                bucket["cost_known"] = False
            reported = (result or {}).get("total_cost_usd")
            if isinstance(reported, (int, float)) and not isinstance(reported, bool):
                bucket["provider_reported_usd"] += float(reported)
            return record

    # ------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        with self.lock:
            roles = copy.deepcopy(self.roles)
            totals = self._totals()
            known_calls = sum(r["calls"] for r in roles.values())
            coverage = "full"
            if self.cost_unknown_calls or self.external_unmetered:
                coverage = "partial" if (known_calls or self.external_unmetered) else "none"
            return {
                "limits": copy.deepcopy(self.limits),
                "roles": roles,
                "calls": totals["calls"],
                "tokens": totals["tokens"],
                "cost_usd_known": round(totals["usd"], 6),
                "cost_coverage": coverage,
                "cost_unknown_calls": self.cost_unknown_calls,
                "usage_unknown_calls": self.usage_unknown_calls,
                "external_unmetered": self.external_unmetered,
                "reserved": copy.deepcopy(self.reserved),
                "reserved_roles": copy.deepcopy(self.reserved_roles),
                "reserved_unknown": self.reserved_unknown,
                "token_limit_semantics": "estimated",
                "token_limit_note": ("max_tokens is an estimated budget: one CLI call "
                                     "may make several tool-use turns; calls/rounds are hard limits"),
                "elapsed_seconds": round(self.elapsed_seconds(), 1),
                "started_at": self.started_at,
                "stop_reason": self.stop_reason,
                "price_basis": "configured prices; unknown models are not priced",
            }

    def can_start_work(self) -> tuple[bool, str | None]:
        with self.lock:
            limits = self.limits
            totals = self._totals()
            if limits.get("max_seconds") and self.elapsed_seconds() >= float(limits["max_seconds"]):
                return False, "max_seconds"
            if limits.get("max_calls") is not None and totals["calls"] >= int(limits["max_calls"]):
                return False, "max_calls"
            if limits.get("max_tokens") is not None and totals["tokens"] >= int(limits["max_tokens"]):
                return False, "max_tokens"
            if limits.get("max_usd") is not None and totals["usd"] >= float(limits["max_usd"]):
                return False, "max_usd_soft"
            if limits.get("max_unknown_calls") is not None and \
                    self.cost_unknown_calls >= int(limits["max_unknown_calls"]):
                return False, "max_unknown_calls"
        return True, None
