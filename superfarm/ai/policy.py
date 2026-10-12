"""The operator's policy for AI assistants, and what it says about one tool.

    read-only   read tools only
    ask         read tools run; changing and destructive tools wait for the operator's
                Yes or No (ai.ask_for can exempt the merely changing ones, ai.auto_approve
                single tools)
    allow       everything that is not denied runs

`ai.deny` wins over everything. The decision is made in the daemon for every changing call
(see api/routes/approvals.py); a caller may also apply a stricter policy of its own first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

READ, CHANGE, DESTRUCTIVE = "read", "change", "destructive"
CLASSES = (READ, CHANGE, DESTRUCTIVE)
ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass(frozen=True)
class Policy:
    mode: str = "ask"
    ask_for: frozenset[str] = frozenset({CHANGE, DESTRUCTIVE})
    auto_approve: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset({"run_shell"})
    approval_timeout_seconds: int = 300
    max_screenshot_px: int = 768
    forbidden_words: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Any) -> Policy:
        ai = config.ai
        return cls(mode=ai.policy, ask_for=frozenset(ai.ask_for), auto_approve=frozenset(ai.auto_approve),
                   deny=frozenset(ai.deny), approval_timeout_seconds=ai.approval_timeout_seconds,
                   max_screenshot_px=ai.max_screenshot_px,
                   forbidden_words=tuple(config.automation.forbidden_field_words))

    @classmethod
    def from_info(cls, info: dict) -> Policy:
        """From the body of GET /api/mcp/info. Anything missing falls back to the cautious default."""
        default = cls()
        return cls(mode=str(info.get("policy") or default.mode),
                   ask_for=frozenset(info.get("ask_for") or default.ask_for),
                   auto_approve=frozenset(info.get("auto_approve") or ()),
                   deny=frozenset(info["deny"]) if isinstance(info.get("deny"), list) else default.deny,
                   approval_timeout_seconds=int(info.get("approval_timeout_seconds") or default.approval_timeout_seconds),
                   max_screenshot_px=int(info.get("max_screenshot_px") or default.max_screenshot_px),
                   forbidden_words=tuple(info.get("forbidden_words") or ()))

    def to_dict(self) -> dict:
        return {"policy": self.mode, "ask_for": sorted(self.ask_for), "auto_approve": sorted(self.auto_approve),
                "deny": sorted(self.deny), "approval_timeout_seconds": self.approval_timeout_seconds,
                "max_screenshot_px": self.max_screenshot_px, "forbidden_words": list(self.forbidden_words)}


@dataclass(frozen=True)
class Decision:
    action: str          # allow | ask | deny
    reason: str


def decide(name: str, klass: str, policy: Policy) -> Decision:
    if name in policy.deny:
        return Decision(DENY, "this tool is switched off for AI assistants on this farm (ai.deny)")
    if klass == READ:
        return Decision(ALLOW, "reading needs no approval")
    if policy.mode == "read-only":
        return Decision(DENY, "the farm's AI policy is read-only (ai.policy): assistants may look, not change")
    if policy.mode == "allow":
        return Decision(ALLOW, "ai.policy is allow")
    if policy.mode != "ask":
        return Decision(DENY, f"unknown AI policy {policy.mode!r}")
    if name in policy.auto_approve:
        return Decision(ALLOW, "pre-approved by the operator (ai.auto_approve)")
    if klass == DESTRUCTIVE or klass in policy.ask_for:
        return Decision(ASK, "the operator decides")
    return Decision(ALLOW, f"ai.ask_for does not include {klass} actions")
