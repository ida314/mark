"""The deterministic gate: the model proposes, this decides. Pure, no IO."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import ArgMatcher, Match, Outcome, Policy

AUTONOMY_ORDER = ["observe", "assist", "act", "trusted"]


@dataclass(frozen=True)
class ToolCallInfo:
    name: str
    risk: str
    tags: tuple[str, ...] = ()
    source: str = "builtin"
    args: dict[str, Any] = field(default_factory=dict)
    path_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyContext:
    autonomy: str = "assist"
    origin: str = "interactive"  # interactive | daemon | mcp | subagent:<name>
    tainted: bool = False
    private: bool = False  # the user's own private data is in context
    approved: bool = False  # set when replaying an approved queued call


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    rule_id: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


def normalize_args(call: ToolCallInfo) -> dict[str, Any]:
    """Resolve declared path arguments so `..` and symlinks cannot dodge a rule."""
    args = dict(call.args)
    for key in call.path_args:
        value = args.get(key)
        if isinstance(value, str) and value:
            args[key] = str(Path(os.path.realpath(Path(value).expanduser())))
    return args


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _expand(pattern: str) -> str:
    return str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern


def _is_under(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(Path(os.path.realpath(Path(_expand(root)).expanduser())))
        return True
    except ValueError:
        return False


def _match_arg(value: Any, matcher: ArgMatcher) -> bool:
    """Every populated field of the matcher must hold."""
    checks: list[bool] = []
    if matcher.equals is not None:
        checks.append(value == matcher.equals)
    if matcher.in_ is not None:
        checks.append(value in matcher.in_)
    if matcher.glob is not None:
        patterns = [_expand(p) for p in _as_list(matcher.glob)]
        checks.append(
            isinstance(value, str)
            and any(fnmatch.fnmatch(value, p) for p in patterns)
        )
    if matcher.regex is not None:
        checks.append(isinstance(value, str) and re.search(matcher.regex, value) is not None)
    if matcher.under is not None:
        roots = _as_list(matcher.under)
        checks.append(isinstance(value, str) and any(_is_under(value, r) for r in roots))
    if matcher.not_under is not None:
        roots = _as_list(matcher.not_under)
        checks.append(isinstance(value, str) and not any(_is_under(value, r) for r in roots))
    if matcher.max is not None:
        checks.append(isinstance(value, int | float) and value <= matcher.max)
    if matcher.contains is not None:
        checks.append(isinstance(value, str) and matcher.contains in value)
    return all(checks) if checks else True


def _matches(m: Match, call: ToolCallInfo, ctx: PolicyContext, args: dict[str, Any]) -> bool:
    if m.tool is not None and not fnmatch.fnmatch(call.name, m.tool):
        return False
    if m.source is not None and not fnmatch.fnmatch(call.source, m.source):
        return False
    if m.tags is not None and not (set(m.tags) & set(call.tags)):
        return False
    if m.risk is not None and call.risk not in m.risk:
        return False
    if m.autonomy is not None and ctx.autonomy not in m.autonomy:
        return False
    if m.origin is not None and not any(
        fnmatch.fnmatch(ctx.origin, o) for o in m.origin
    ):
        return False
    if m.private is not None and ctx.private != m.private:
        return False
    if m.tainted is not None and ctx.tainted != m.tainted:
        return False
    for key, matcher in m.args.items():
        if key not in args:
            # A rule that constrains an absent argument does not match. A hard_deny
            # on a missing path argument would otherwise never fire.
            return False
        if not _match_arg(args[key], matcher):
            return False
    return True


def _tighten(outcome: Outcome, floor: Outcome) -> Outcome:
    order = {"allow": 0, "require_approval": 1, "deny": 2}
    return outcome if order[outcome] >= order[floor] else floor


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def evaluate(self, call: ToolCallInfo, ctx: PolicyContext) -> Decision:
        args = normalize_args(call)
        risk = call.risk if call.risk in self.policy.risk_matrix else self.policy.defaults.unknown_risk
        probe = ToolCallInfo(
            name=call.name, risk=risk, tags=call.tags, source=call.source,
            args=args, path_args=call.path_args,
        )

        # 1. hard denies win outright and cannot be overridden later.
        for rule in self.policy.hard_deny:
            if _matches(rule.match, probe, ctx, args):
                return Decision("deny", rule.id, rule.reason or "denied by hard_deny")

        # 2. first matching rule.
        decision: Decision | None = None
        for rule in self.policy.rules:
            if _matches(rule.match, probe, ctx, args):
                decision = Decision(rule.outcome or "require_approval", rule.id, rule.reason or "")
                break

        # 3. otherwise the risk/autonomy matrix.
        if decision is None:
            row = self.policy.risk_matrix[risk]
            outcome = row.get(ctx.autonomy, "deny")
            decision = Decision(outcome, f"risk_matrix:{risk}/{ctx.autonomy}", "")

        return self._post_rules(decision, probe, ctx)

    def _post_rules(self, d: Decision, call: ToolCallInfo, ctx: PolicyContext) -> Decision:
        """Post-rules may only tighten. Nothing here can turn a deny into an allow."""
        outcome = d.outcome
        reason = d.reason
        rule_id = d.rule_id

        # Untrusted content in context must not silently drive side effects.
        if ctx.tainted and call.risk in ("write", "external", "destructive"):
            tightened = _tighten(outcome, "require_approval")
            if tightened != outcome:
                outcome = tightened
                rule_id = f"{rule_id}+taint"
                reason = "context contains untrusted content, so writes need confirmation"

        # A previously approved call still cannot escape hard_deny (checked above).
        if ctx.approved and outcome == "require_approval":
            outcome = "allow"
            rule_id = f"{rule_id}+approved"
            reason = "approved by the user"

        return Decision(outcome, rule_id, reason)


def cap_autonomy(parent: str, cap: str) -> str:
    """A sub-agent never runs above its caller, whatever its spec asks for."""
    return AUTONOMY_ORDER[min(AUTONOMY_ORDER.index(parent), AUTONOMY_ORDER.index(cap))]


def load_engine(policy_path: Path, substitutions: dict[str, list[str]]) -> PolicyEngine:
    from .model import load_policy

    return PolicyEngine(load_policy(policy_path, substitutions))


def engine_from_config(cfg) -> PolicyEngine:
    # `under`/`not_under` compare against directories, so these are directories.
    subs = {
        "allowed_roots": [str(r) for r in cfg.paths.roots()],
        "workspace": [str(cfg.paths.workspace)],
        "memory_repo": [str(cfg.paths.memory_repo)],
    }
    return load_engine(cfg.policy_file, subs)
