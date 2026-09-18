"""Configuration loading: packaged defaults <- user TOML <- AGENT_* environment."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.toml"
DEFAULT_POLICY = REPO_ROOT / "config" / "policy.default.yaml"
CONFIG_DIR = Path(os.environ.get("AGENT_CONFIG_DIR", "~/.config/agent")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"
POLICY_FILE = CONFIG_DIR / "policy.yaml"

Autonomy = Literal["observe", "assist", "act", "trusted"]


def expand(p: str | Path) -> Path:
    return Path(str(p)).expanduser().resolve()


class DbConfig(BaseModel):
    dsn: str = "postgresql://agent:agent@127.0.0.1:55432/agent"
    min_size: int = 1
    max_size: int = 8
    # pg_dump/pg_restore run inside the container: the host has no client, and a mismatched
    # major version would refuse the dump anyway.
    container: str = "agentd-postgres-1"
    backup_keep: int = 14


class RoleConfig(BaseModel):
    temperature: float = 0.7
    top_p: float = 0.8
    thinking: bool = False
    max_tokens: int = 4096
    model: str | None = None


class LLMConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8001/v1"
    api_key: str = "not-needed"
    model: str = "Qwen/Qwen3.8-27B-FP8"
    timeout_s: float = 300.0
    max_context_tokens: int = 262144
    roles: dict[str, RoleConfig] = Field(default_factory=dict)

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name) or self.roles.get("main") or RoleConfig()


class EmbedConfig(BaseModel):
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int = 384
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    enabled: bool = True


class AgentConfig(BaseModel):
    max_steps: int = 12
    history_tokens: int = 24000
    context_budget_tokens: int = 2000
    tool_result_max_chars: int = 8000
    autonomy: Autonomy = "assist"


class RetrievalConfig(BaseModel):
    fast_budget_tokens: int = 2000
    deep_budget_tokens: int = 4000
    channel_limit: int = 30
    rerank_top_k: int = 30
    ef_search: int = 80
    query_expansion: bool = True  # deep mode only: rewrite vague questions before searching
    expansion_variants: int = 2
    expansion_timeout_s: float = 15.0


class PathsConfig(BaseModel):
    data_dir: Path = Path("~/.local/share/agent")
    allowed_roots: list[Path] = Field(default_factory=list)

    @property
    def memory_repo(self) -> Path:
        return expand(self.data_dir) / "memory"

    @property
    def workspace(self) -> Path:
        return expand(self.data_dir) / "workspace"

    @property
    def backups(self) -> Path:
        return expand(self.data_dir) / "backups"

    @property
    def logs(self) -> Path:
        return expand(self.data_dir) / "logs"

    def roots(self) -> list[Path]:
        return [expand(r) for r in self.allowed_roots]


class SandboxConfig(BaseModel):
    image: str = "agent-sandbox:local"
    memory: str = "2g"
    cpus: str = "2"
    default_timeout_s: int = 120
    max_timeout_s: int = 600


class DaemonConfig(BaseModel):
    heartbeat_interval_s: int = 1800
    quiet_hours: tuple[int, int] = (23, 8)
    idle_consolidate_after_s: int = 900
    nightly_at: str = "03:30"
    scheduler_poll_s: int = 10


class ObsConfig(BaseModel):
    otlp_endpoint: str = "localhost:4317"
    enabled: bool = True
    capture_content: bool = False
    jaeger_ui: str = "http://127.0.0.1:16686"


class NtfyConfig(BaseModel):
    """Push delivery. Self-hosted and reached over Tailscale, so notification bodies never
    leave hardware you control."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8088"
    topic: str = "agent"
    timeout_s: float = 10.0
    min_level: Literal["info", "warn", "error"] = "info"
    sweep_s: int = 60


class GithubConnectorConfig(BaseModel):
    enabled: bool = False
    user: str = ""  # your login. The token lives in the vault at github/<user>, never here.
    api_base: str = "https://api.github.com"
    poll_interval_s: float = 60.0  # a floor; GitHub's X-Poll-Interval may raise it
    sweep_interval_s: float = 3600.0  # the unconditional listing that closes finished loops
    # 0 means "invent no deadlines". The consequence is worth knowing: overdue_loops() only
    # returns loops with a due_at, so at 0 the heartbeat never mentions GitHub and delivery
    # is entirely via push. Set 24 to turn the heartbeat path on.
    review_due_in_h: int = 0
    # Your noise budget, and the only part of this that is yours to tune. Titles stay in
    # code because they are an injection boundary, not formatting.
    include_reasons: list[str] = Field(
        default_factory=lambda: ["review_requested", "assign", "mention"]
    )
    notify_reasons: list[str] = Field(
        default_factory=lambda: ["review_requested", "mention"]
    )


class ConnectorsConfig(BaseModel):
    """Daemon-side feeds. Not tools: the agent cannot call these and never sees their
    credentials."""

    enabled: bool = False  # the one-line kill switch for all of them
    timeout_s: float = 20.0
    max_items_per_poll: int = 50
    max_backoff_s: float = 1800.0
    notify_after_failures: int = 5
    disabled_recheck_s: float = 300.0
    github: GithubConnectorConfig = Field(default_factory=GithubConnectorConfig)


class McpServerConfig(BaseModel):
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    risk_default: str = "external"
    trust_output: bool = False
    trust_annotations: bool = False
    allow_tools: list[str] | None = None
    timeout_s: float = 30.0


class McpConfig(BaseModel):
    http_port: int = 8770
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class Config(BaseModel):
    db: DbConfig = Field(default_factory=DbConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embed: EmbedConfig = Field(default_factory=EmbedConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    obs: ObsConfig = Field(default_factory=ObsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)

    policy_file: Path = POLICY_FILE

    def ensure_dirs(self) -> None:
        for p in (
            expand(self.paths.data_dir),
            self.paths.memory_repo,
            self.paths.workspace,
            self.paths.backups,
            self.paths.logs,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict[str, Any]:
    """AGENT_LLM__BASE_URL=... -> {"llm": {"base_url": ...}}."""
    out: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith("AGENT_") or key == "AGENT_CONFIG_DIR":
            continue
        parts = key[len("AGENT_") :].lower().split("__")
        if not parts:
            continue
        try:
            value: Any = tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            value = raw
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


def load_config(path: Path | None = None) -> Config:
    data: dict[str, Any] = {}
    if DEFAULT_CONFIG.exists():
        data = tomllib.loads(DEFAULT_CONFIG.read_text())
    user = path or CONFIG_FILE
    if user.exists():
        data = _deep_merge(data, tomllib.loads(user.read_text()))
    data = _deep_merge(data, _env_overrides())
    cfg = Config.model_validate(data)
    if POLICY_FILE.exists():
        cfg.policy_file = POLICY_FILE
    else:
        cfg.policy_file = DEFAULT_POLICY
    return cfg


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def reset_config_cache() -> None:
    get_config.cache_clear()
