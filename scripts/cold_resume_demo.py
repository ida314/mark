"""Set up one dead run so `agent journal continue` can be driven against it by hand.

Session 5c's exit criterion, made checkable by a person rather than only by a test: *a cold
resume from a stale run with no stored handoff produces a usable handoff object and the new
orchestrator picks up correctly.*

Both halves of "stale, with no stored handoff" are real rather than simulated.
`[checkpoints] enabled` is false - the shipped default - so nothing stores a handoff at all,
and the run is made stale by setting `warm_window_s` to zero on the continuation, which is
the same comparison the code makes against a real clock.

The run itself is laid down with the scripted provider, so it costs nothing and is the same
every time; the *continuation* is against the real model, which is the part being judged.

    uv run python scripts/cold_resume_demo.py
    AGENT_DB__DSN=... AGENT_PATHS__DATA_DIR=... AGENT_HANDOFF__WARM_WINDOW_S=0 \
      agent journal continue <run_id> "<message>" --apply
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.confab_eval import _fresh_database, _point_config_at  # noqa: E402

from agentd.agent.loop import AgentLoop, Session  # noqa: E402
from agentd.agent.stream import Answer  # noqa: E402
from agentd.config import DEFAULT_POLICY, PathsConfig, load_config  # noqa: E402
from agentd.db import pool as pool_mod  # noqa: E402
from agentd.embed import HashEmbedder, set_embedder  # noqa: E402
from agentd.journal import runtime as journal_runtime  # noqa: E402
from agentd.llm.fake import FakeProvider  # noqa: E402
from agentd.policy.approvals import AutoApprover  # noqa: E402
from agentd.policy.engine import engine_from_config  # noqa: E402
from agentd.tools.base import Tool, ToolResult, obj  # noqa: E402
from agentd.tools.registry import Registry  # noqa: E402

HOME = Path("/tmp/cold-resume-demo")

BODY = """ALWAYS_EXPOSE_LIMIT = 20
SIMILARITY_FLOOR = 0.30
TOP_K = 8

    async def select(self, query, *, session_used=None, cfg=None):
        enabled = self.enabled()
        if len(enabled) <= ALWAYS_EXPOSE_LIMIT:
            return enabled
"""


def _reader() -> Tool:
    async def handler(args, ctx):
        return ToolResult(content=BODY + "\n# padding\n" * 300)

    return Tool(
        name="reads_a_file", description="reads one file", parameters=obj(path={}),
        handler=handler, effect_class="read", risk="read",
    )


async def main() -> None:
    base = load_config()
    dsn = await _fresh_database(base)
    shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True)

    cfg = base.model_copy(deep=True)
    cfg.db.dsn = dsn
    cfg.paths = PathsConfig(data_dir=HOME, allowed_roots=[HOME])
    cfg.policy_file = DEFAULT_POLICY
    cfg.obs.enabled = cfg.telemetry.enabled = False
    # The shipped default, deliberately: with checkpoints off nothing stores a handoff, so
    # a resume has no object to read back and must build one. That is the case under test.
    cfg.checkpoints.enabled = False
    cfg.ensure_dirs()
    _point_config_at(cfg)
    await pool_mod.close_pool()
    set_embedder(HashEmbedder(dim=cfg.embed.dim))

    registry = Registry()
    registry.add(_reader())
    provider = FakeProvider(
        turns=[
            [("reads_a_file", {"path": "src/agentd/tools/registry.py"})],
            "The per-turn tool cap is ALWAYS_EXPOSE_LIMIT = 20 in "
            "src/agentd/tools/registry.py, with TOP_K = 8 bounding how many similar tools "
            "are added on top.",
        ]
    )
    loop = AgentLoop(
        cfg=cfg, registry=registry, engine=engine_from_config(cfg),
        approver=AutoApprover(True), provider=provider,
    )
    session = await Session.create("test")
    run_id = None
    async for event in loop.run_turn(
        session, "Where is it decided which tools a single turn may see, and what caps it?"
    ):
        if isinstance(event, Answer):
            run_id = event.turn_id

    journal_runtime.get_writer(cfg).flush()
    await pool_mod.close_pool()
    journal_runtime.close_writer()
    print(json.dumps({"run_id": run_id, "dsn": dsn, "data_dir": str(HOME)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
