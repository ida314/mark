"""Role -> provider + parameters. Swapping a model is a config edit, not a code change."""

from __future__ import annotations

from ..config import Config, get_config
from .base import CallParams, LLMProvider
from .openai_compat import OpenAICompatProvider

_provider: LLMProvider | None = None


def get_provider(cfg: Config | None = None) -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = OpenAICompatProvider(cfg or get_config())
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Tests and the daemon inject their own provider here."""
    global _provider
    _provider = provider


def params_for(role: str, cfg: Config | None = None, **overrides) -> CallParams:
    cfg = cfg or get_config()
    rc = cfg.llm.role(role)
    params = CallParams(
        temperature=rc.temperature,
        top_p=rc.top_p,
        max_tokens=rc.max_tokens,
        thinking=rc.thinking,
        model=rc.model or cfg.llm.model,
    )
    for key, value in overrides.items():
        setattr(params, key, value)
    return params
