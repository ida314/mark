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
    config_mod.get_config = original
    set_provider(None)


TABLES = [
    "fact_evidence", "fact_entities", "facts", "entities", "episodes",
    "candidate_memories", "procedures", "raw_event_embeddings", "raw_events", "sessions",
    "goals", "open_loops", "watchers", "notifications", "approvals", "actions", "tools",
    "consolidation_runs", "daemon_status",
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
