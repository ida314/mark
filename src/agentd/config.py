"""Configuration loading: packaged defaults <- user TOML <- AGENT_* environment."""

from __future__ import annotations

import os
import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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
    # When push may not wake you. None inherits [daemon] quiet_hours, which is what one
    # setting meant before this existed; [0, 0] is an empty window, so push never waits.
    # Separate from the daemon's because they are different questions wearing one name:
    # "may this wake me" is about a phone at 3am, "should the agent think now" is about
    # spending an LLM turn on a situation nobody will read until morning.
    quiet_hours: tuple[int, int] | None = None


class TelegramConfig(BaseModel):
    """A chat front end, in the same category as `agent chat` — not a connector, not a tool.

    A connector is inbound-only and holds a credential the agent is fenced out of. A tool is
    something the model calls. This is neither: it is a way for *you* to reach the loop, and
    for the loop to answer you, which is what the CLI already is and what ntfy is half of.

    Two properties worth stating before switching it on:

    - `allowed_chat_ids` is the entire security boundary. Anyone who learns the bot's
      username can message it, and an empty allowlist therefore means nobody, not everybody.
    - Bot messages are **not** end-to-end encrypted. They cross Telegram's servers in a form
      Telegram can read. Everything else in this system was built so nothing leaves hardware
      you control — ntfy is self-hosted on the tailnet for exactly that reason — and this is
      the one place that stops being true. It is a real trade, not an oversight.
    """

    enabled: bool = False
    # Numeric chat ids, not usernames: a username can be changed by its owner, an id cannot.
    # `agent telegram whoami` prints the id of whoever messages the bot next.
    allowed_chat_ids: list[int] = Field(default_factory=list)

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _ids_not_usernames(cls, value):
        """The two mistakes worth naming, because the raw parse error names neither.

        A username instead of an id, and — easier to make — the *bot's* identity instead of
        your own. The allowlist answers "who may talk to the agent", and the agent is not on
        that list.
        """
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and not entry.lstrip("-").isdigit():
                    raise ValueError(
                        f"{entry!r} is a username, not a chat id. This list holds numeric ids "
                        "of the people who may message the agent — your own Telegram account, "
                        "not the bot's. Message the bot, then run `agent telegram whoami`."
                    )
        return value
    # Long polling, so this box needs no inbound port, no public address and no certificate.
    # A webhook would need all three and it is a headless machine on a tailnet.
    poll_timeout_s: int = 50
    api_base: str = "https://api.telegram.org"
    autonomy: Autonomy = "assist"
    # Telegram refuses anything longer; replies are split rather than truncated.
    max_message_chars: int = 4000
    # A turn that runs longer than this gets a "still working" nudge, because a silent bot
    # is indistinguishable from a broken one.
    slow_turn_s: float = 20.0
    # Show which tools ran, as they run. On by default: in a terminal you watch the tool
    # lines scroll past, and losing that on a phone turns the agent into something that
    # produces answers with no visible account of where they came from. `/tools` toggles it
    # per chat.
    show_tools: bool = True


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


CONNECTOR_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,30}")


def _check_labels(accounts: dict[str, Any]) -> dict[str, Any]:
    """A label is not cosmetic: it becomes the connector's name, which is the
    `connector_state` primary key and the `raw_events` kind prefix. Rejecting a bad one here
    is how a typo in config.toml fails at load rather than as a mystery row in the archive."""
    for label in accounts:
        if not CONNECTOR_LABEL.fullmatch(label):
            raise ValueError(
                f"account label {label!r} must be lowercase letters, digits, - or _ "
                "(it becomes the connector name)"
            )
    return accounts


class MailRules(BaseModel):
    """What counts as mail that is waiting on you.

    This is your noise budget and the only part of the mail connectors that is yours to
    tune. Titles are not here, and will not be: an open-loop title reaches an LLM prompt
    through the heartbeat, so composing one is an injection boundary rather than formatting.
    """

    # You in To or Cc. Off means anything unread counts, including mail you were bcc'd on
    # and everything a mailing list sends - which is a different product.
    direct_only: bool = True
    # List-Unsubscribe, List-Id, Precedence: bulk/list/junk, Auto-Submitted: anything but no.
    skip_bulk: bool = True
    # Exact addresses, or "@domain" to mean the whole domain.
    skip_senders: list[str] = Field(default_factory=list)
    # Other addresses that are also you. NYU hands out a netid address and a name-based
    # alias for the same mailbox, and `direct_only` would drop half your mail without this.
    aliases: list[str] = Field(default_factory=list)
    # 0 invents no deadlines, exactly as GitHub's review_due_in_h does - and with the same
    # consequence: overdue_loops() only sees loops with a due_at, so at 0 the heartbeat stays
    # quiet about mail and delivery is entirely via push.
    due_in_h: int = 0
    notify: bool = True


class GoogleAccountConfig(BaseModel):
    """One Google account. Mail and calendar share a credential, so they share an entry -
    but they are two connectors, because one being rate-limited should not stall the other."""

    address: str = ""  # the refresh token lives in the vault at google/<address>
    mail: bool = True
    calendar: bool = False
    calendar_id: str = "primary"
    # Server-side narrowing, so the rules below run over a small set. Kept broad on purpose:
    # `category:primary` silently matches nothing on a Workspace account without tabs.
    query: str = "is:unread in:inbox"
    max_results: int = 50
    horizon_days: int = 14  # calendar: how far ahead one poll looks
    poll_interval_s: float = 120.0
    sweep_interval_s: float = 3600.0
    rules: MailRules = Field(default_factory=MailRules)


class GoogleConnectorConfig(BaseModel):
    enabled: bool = False
    gmail_api_base: str = "https://gmail.googleapis.com"
    calendar_api_base: str = "https://www.googleapis.com"
    token_uri: str = "https://oauth2.googleapis.com/token"
    auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    accounts: dict[str, GoogleAccountConfig] = Field(default_factory=dict)

    _labels = field_validator("accounts")(_check_labels)


class ImapAccountConfig(BaseModel):
    """A mailbox reachable only by IMAP, which is most of them.

    Polled less often than Gmail because every poll is a fresh TLS connection and a LOGIN
    rather than a conditional GET; there is no cheap 304 to hide behind.
    """

    host: str = ""
    port: int = 993
    username: str = ""  # the password lives in the vault at imap/<label>
    address: str = ""  # what "addressed to you" means here; defaults to username
    mailbox: str = "INBOX"
    poll_interval_s: float = 300.0
    sweep_interval_s: float = 3600.0
    rules: MailRules = Field(default_factory=MailRules)

    def me(self) -> str:
        return self.address or self.username


class ImapConnectorConfig(BaseModel):
    enabled: bool = False
    accounts: dict[str, ImapAccountConfig] = Field(default_factory=dict)

    _labels = field_validator("accounts")(_check_labels)


class BrightspaceConnectorConfig(BaseModel):
    """The per-user iCal feed, not the Valence API.

    Valence would give announcements and grades, and needs an application key that a D2L
    administrator registers - which a student cannot do. The calendar feed needs nobody: it
    is a URL with a token in it, it is read-only, and it carries the thing that actually has
    a deadline. The URL is therefore a credential and lives in the vault, not in config.toml.
    """

    enabled: bool = False
    label: str = "nyu"  # the feed URL lives in the vault at brightspace/<label>
    horizon_days: int = 21
    poll_interval_s: float = 1800.0
    # An all-day entry in a course calendar is reading week or a holiday, not something you
    # owe anybody. Archived either way; this decides whether it also becomes a loop.
    all_day_loops: bool = False
    # Case-insensitive substrings a summary must contain to become a loop. Empty means every
    # timed event does. Set it to ["due"] once you have seen what your own feed actually
    # contains - which `agent connectors poll brightspace` will show you.
    require: list[str] = Field(default_factory=list)
    # Unlike mail and GitHub, a due date is a real deadline somebody else set, so this
    # connector does give its loops a due_at and the heartbeat does speak up about them.
    notify: bool = True


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
    google: GoogleConnectorConfig = Field(default_factory=GoogleConnectorConfig)
    imap: ImapConnectorConfig = Field(default_factory=ImapConnectorConfig)
    brightspace: BrightspaceConnectorConfig = Field(
        default_factory=BrightspaceConnectorConfig
    )


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


class ReviewConfig(BaseModel):
    # How long a candidate may sit unadjudicated before `agent doctor` calls it a problem.
    # A user-origin candidate gets its own, much shorter budget: they typed it expecting it
    # to stick, and the queue has no other consumer than the daemon.
    pending_warn_after_s: int = 7200
    user_pending_warn_after_s: int = 300


class Config(BaseModel):
    db: DbConfig = Field(default_factory=DbConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embed: EmbedConfig = Field(default_factory=EmbedConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    obs: ObsConfig = Field(default_factory=ObsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
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
