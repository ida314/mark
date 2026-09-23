from __future__ import annotations

import asyncio
import os
from pathlib import Path

import psycopg
import pytest

from agentd.config import DEFAULT_POLICY, Config, PathsConfig, load_config
from agentd.db import migrate as migrate_mod
from agentd.db import pool as pool_mod
from agentd.embed import HashEmbedder, set_embedder
from agentd.journal import runtime as journal_runtime
from agentd.llm.fake import FakeProvider
from agentd.llm.roles import set_provider

TEST_DB = "agent_test"


def _admin_dsn(cfg: Config) -> str:
    return cfg.db.dsn.rsplit("/", 1)[0] + "/postgres"


def _test_dsn(cfg: Config) -> str:
    return cfg.db.dsn.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def base_config() -> Config:
    return load_config()


@pytest.fixture(scope="session")
def pg_dsn(base_config: Config) -> str:
    """A scratch database with the real migrations applied, or skip the whole suite."""

    async def setup() -> str:
        try:
            conn = await psycopg.AsyncConnection.connect(
                _admin_dsn(base_config), autocommit=True, connect_timeout=3
            )
        except Exception as exc:
            pytest.skip(f"postgres unavailable: {exc}")
        try:
            await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            await conn.execute(f"CREATE DATABASE {TEST_DB}")
        finally:
            await conn.close()
        dsn = _test_dsn(base_config)
        await migrate_mod.migrate(base_config, dsn=dsn)
        return dsn

    return asyncio.run(setup())


@pytest.fixture
async def cfg(pg_dsn: str, base_config: Config, tmp_path: Path) -> Config:
    """Config pointed at the scratch database and a throwaway data directory."""
    cfg = base_config.model_copy(deep=True)
    cfg.db.dsn = pg_dsn
    cfg.paths = PathsConfig(
        data_dir=tmp_path,
        allowed_roots=[tmp_path / "projects", tmp_path / "workspace"],
    )
    cfg.policy_file = DEFAULT_POLICY
    cfg.obs.enabled = False
    cfg.ensure_dirs()
    (tmp_path / "projects").mkdir(exist_ok=True)

    import agentd.config as config_mod

    original = config_mod.get_config
    config_mod.get_config = lambda: cfg
    for module in (
        "agentd.db.pool", "agentd.tools.builtin_fs", "agentd.tools.builtin_memory",
        "agentd.tools.builtin_shell", "agentd.memory.retrieval", "agentd.memory.review",
        "agentd.memory.consolidate", "agentd.agent.context", "agentd.agent.loop",
        "agentd.tools.registry", "agentd.embed", "agentd.llm.roles", "agentd.cli.app",
        "agentd.journal.runtime", "agentd.journal.checkpoints", "agentd.agent.budget",
        # Session 6a. `run_subagent` falls back to `get_config()` when its caller passes no
        # config, and the `delegate` tool is exactly that caller - it has a ToolContext and
        # no Config. Without this entry a test that delegates through the tool writes its
        # worker into the real `~/.local/share/agent/journal.db`, which is how this line
        # came to be added.
        "agentd.agent.handoff", "agentd.agent.subagents",
        # Session 7c. `memory/promotion.py` resolves config at call time the same way -
        # `promote_scope` and `complete_pending` both take `cfg: Config | None = None` and
        # fall back to `get_config()`, and the run boundary in `agent/loop.py` is one of the
        # callers that passes one. Without this entry a test that ends a turn with a note in
        # working memory reaches the real provider and the real memory store.
        "agentd.memory.promotion",
    ):
        import importlib

        mod = importlib.import_module(module)
        if hasattr(mod, "get_config"):
            mod.get_config = lambda: cfg

    await pool_mod.close_pool()
    await _truncate_all(pg_dsn)
    set_embedder(HashEmbedder(dim=cfg.embed.dim))
    yield cfg
    await pool_mod.close_pool()
    # The journal writer is cached per process and per file. Closing it here flushes the
    # tail and drops the handle on a `tmp_path` that is about to disappear, so no test can
    # inherit a writer pointed at the previous test's directory.
    journal_runtime.close_writer()
    config_mod.get_config = original
    set_provider(None)


@pytest.fixture
def journaled(cfg: Config):
    """What this test's turns recorded in the journal, in feed order.

    Tests used to read a turn's tool calls off the in-process event stream the CLI rendered.
    Session 2c deleted that stream, so a test that asserts what a turn did now asserts
    against the same rows a frontend renders - which is the point of there being one path.

    The writer is flushed first: the chatty event types are buffered and the age check runs
    on append rather than on a timer, so an assertion made without flushing would be about
    the buffer rather than about the journal.
    """
    from agentd.journal.store import Event

    def read(*types: str, run_id: str | None = None) -> list[Event]:
        writer = journal_runtime.get_writer(cfg)
        writer.flush()
        events = writer.store.read(run_id) if run_id is not None else writer.store.read_all()
        return [e for e in events if not types or e.type in types]

    return read


TABLES = [
    "fact_evidence", "fact_entities", "facts", "entities", "episodes",
    "candidate_memories", "procedures", "raw_event_embeddings", "raw_events", "sessions",
    "goals", "open_loops", "watchers", "notifications", "approvals", "actions", "tools",
    "consolidation_runs", "daemon_status", "connector_state",
]


async def _truncate_all(dsn: str) -> None:
    """Each test starts from an empty database.

    The archive is append-only by trigger, which is the point of it; clearing it for a
    test is the one place that guard is deliberately bypassed.
    """
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        await conn.execute("SET session_replication_role = origin")
    finally:
        await conn.close()


@pytest.fixture
def fake_llm() -> FakeProvider:
    provider = FakeProvider()
    set_provider(provider)
    return provider


@pytest.fixture(autouse=True)
def _no_otel(monkeypatch):
    monkeypatch.setenv("AGENT_OBS__ENABLED", "false")


def pytest_configure(config):
    os.environ.setdefault("AGENT_OBS__ENABLED", "false")
