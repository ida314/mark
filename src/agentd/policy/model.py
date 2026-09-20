"""Schema for policy.yaml. A malformed policy fails to load, so it fails closed."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Outcome = Literal["allow", "deny", "require_approval"]
Risk = Literal["read", "draft", "write", "external", "destructive"]
Autonomy = Literal["observe", "assist", "act", "trusted"]

ALLOWED_ARG_MATCHERS = {"equals", "in", "glob", "regex", "under", "not_under", "max", "contains"}


class ArgMatcher(BaseModel):
    model_config = {"extra": "forbid"}

    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    glob: list[str] | str | None = None
    regex: str | None = None
    under: list[str] | str | None = None
    not_under: list[str] | str | None = None
    max: float | None = None
    contains: str | None = None


class Match(BaseModel):
    model_config = {"extra": "forbid"}

    tool: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    risk: list[Risk] | None = None
    autonomy: list[Autonomy] | None = None
    origin: list[str] | None = None
    tainted: bool | None = None
    # True matches only once a tool marked private_output has run in this session - the
    # user's own data is in context. Stronger than `tainted`, which any web page raises.
    private: bool | None = None
    args: dict[str, ArgMatcher] = Field(default_factory=dict)


class Rule(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    match: Match
    outcome: Outcome | None = None
    reason: str | None = None


class Defaults(BaseModel):
    model_config = {"extra": "forbid"}

    autonomy: Autonomy = "assist"
    unknown_risk: Risk = "external"


class Policy(BaseModel):
    model_config = {"extra": "forbid"}

    version: int = 1
    defaults: Defaults = Field(default_factory=Defaults)
    risk_matrix: dict[Risk, dict[Autonomy, Outcome]]
    hard_deny: list[Rule] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("risk_matrix")
    @classmethod
    def _complete_matrix(cls, v: dict) -> dict:
        missing = {"read", "draft", "write", "external", "destructive"} - set(v)
        if missing:
            raise ValueError(f"risk_matrix is missing risk levels: {sorted(missing)}")
        return v


class PolicyError(RuntimeError):
    pass


def load_policy(path: Path, substitutions: dict[str, list[str]] | None = None) -> Policy:
    """Load and validate policy.yaml, expanding ${placeholders} from config."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    if substitutions:
        raw = _substitute(raw, substitutions)
    try:
        return Policy.model_validate(raw)
    except Exception as exc:
        raise PolicyError(f"invalid policy {path}: {exc}") from exc


def _substitute(node: Any, subs: dict[str, list[str]]) -> Any:
    """Replace "${name}" with its list value, in place, anywhere in the tree."""
    if isinstance(node, dict):
        return {k: _substitute(v, subs) for k, v in node.items()}
    if isinstance(node, list):
        out: list[Any] = []
        for item in node:
            replaced = _substitute(item, subs)
            if isinstance(item, str) and item.startswith("${") and isinstance(replaced, list):
                out.extend(replaced)
            else:
                out.append(replaced)
        return out
    if isinstance(node, str) and node.startswith("${") and node.endswith("}"):
        key = node[2:-1]
        if key not in subs:
            raise PolicyError(f"policy references unknown placeholder ${{{key}}}")
        return subs[key]
    return node
