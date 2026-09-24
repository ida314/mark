"""The project a worker works on, named in config and carried to where work happens.

Why this matters. A live `coder` delegation failed all fourteen of its tool calls, and not
because of permissions: `~/Projects` was already in `paths.allowed_roots`. It failed because
nothing in this runtime ever tells a worker which directory it is working in. `fs_*`
resolves a relative path under the agent's workspace, which is empty; the sandbox mounted
that same empty workspace, so no command could see the code either. The path must be
configured explicitly and then *stated* - to the model in its system prompt, and to the
container as a bind mount. Both halves are asserted here, plus the two ways this could
quietly go wrong: a project that reaches outside the allowed roots, and a mount source that
does not exist and which `docker run -v` would otherwise create as an empty root-owned
directory the worker would then report success against.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentd.agent.delegation import TaskSpec
from agentd.agent.loop import Session
from agentd.agent.results import WorkerReport
from agentd.agent.subagents import SubagentSpec, project_block, run_subagent
from agentd.config import PathsConfig
from agentd.llm.fake import FakeProvider
from agentd.policy.approvals import AutoApprover
from agentd.tools.base import ToolContext
from agentd.tools.registry import build_registry

# The one sentence the block leads with. Asserted by identity rather than by keyword: the
# assembled system prompt already contains the words "project" and "workspace" on its own.
SENTENCE = "The project you are working on is at"


def _set_project(cfg, root: Path) -> Path:
    """Point the config at `root` the way a user's TOML would, validator included."""
    root.mkdir(parents=True, exist_ok=True)
    cfg.paths = PathsConfig(
        data_dir=cfg.paths.data_dir,
        allowed_roots=list(cfg.paths.allowed_roots),
        project=root,
    )
    return cfg.paths.project_root


def _spec(**kwargs) -> SubagentSpec:
    defaults = dict(
        name="coder", prompt="You are a coding sub-agent.",
        tool_names=["fs_read", "shell_exec"], max_steps=3, autonomy_cap="assist",
    )
    defaults.update(kwargs)
    return SubagentSpec(**defaults)


async def _worker_system_prompt(cfg, spec: SubagentSpec) -> str:
    provider = FakeProvider(
        turns=["done"], json_results=[WorkerReport(status="completed", answer="ok")]
    )
    session = await Session.create("test")
    await run_subagent(
        spec, TaskSpec(spec.name, "change the thing"),
        parent_session_id=session.id, parent_turn_id=session.id, parent_autonomy="assist",
        approver=AutoApprover(True), registry=build_registry(), cfg=cfg, provider=provider,
    )
    assert provider.calls, "the worker never reached the provider"
    first = provider.calls[0]["messages"][0]
    assert first["role"] == "system"
    return first["content"]


# --- half A: the worker is told ----------------------------------------------


async def test_a_worker_is_told_the_absolute_path_of_the_project_it_works_on(cfg):
    root = _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    prompt = await _worker_system_prompt(cfg, _spec())
    assert str(root) in prompt
    # Its role is still there: the path is added to the role prompt, not instead of it.
    assert "You are a coding sub-agent." in prompt


async def test_with_no_project_configured_a_worker_is_told_nothing_about_one(cfg):
    """Silence, not a guess. A named directory would be treated as the project."""
    assert cfg.paths.project_root is None
    prompt = await _worker_system_prompt(cfg, _spec())
    assert SENTENCE not in prompt


def test_a_worker_is_told_that_a_relative_path_is_not_in_the_project(cfg):
    """The accepted cost of leaving `_p()` alone is that the worker has to know."""
    _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    block = project_block(cfg, has_shell=True)
    assert str(cfg.paths.workspace) in block
    assert "absolute" in block


def test_a_worker_with_a_shell_is_told_the_project_is_the_container_cwd(cfg):
    _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    block = project_block(cfg, has_shell=True)
    assert "mounted read-write at /workspace" in block


def test_a_worker_without_a_shell_is_not_told_about_a_mount_it_cannot_use(cfg):
    _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    assert "mounted" not in project_block(cfg, has_shell=False)


# --- half B: the container is pointed at it ----------------------------------


def test_the_sandbox_mounts_the_configured_project_not_the_workspace(cfg):
    from agentd.tools.builtin_shell import docker_command

    root = _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    joined = " ".join(docker_command("pytest", network=False, timeout_s=10, name="n"))
    assert f"{root}:/workspace:rw" in joined
    assert f"{cfg.paths.workspace}:/workspace" not in joined


def test_the_sandbox_mounts_the_workspace_only_when_there_is_no_project(cfg):
    from agentd.tools.builtin_shell import docker_command

    assert cfg.paths.project_root is None
    joined = " ".join(docker_command("echo hi", network=False, timeout_s=10, name="n"))
    assert f"{cfg.paths.workspace}:/workspace:rw" in joined


def test_the_preview_names_the_directory_that_is_really_mounted(cfg):
    """Approval is worth nothing if what is shown is not what is mounted."""
    from agentd.tools.builtin_shell import shell_exec

    root = _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    preview = shell_exec.preview({"command": "rm -rf build"})
    assert str(root) in preview
    assert "read-write" in preview


async def test_shell_exec_refuses_a_mount_source_that_does_not_exist(cfg, monkeypatch):
    """`docker run -v` would create it, as root, and every command would then lie."""
    from agentd.tools import builtin_shell

    root = _set_project(cfg, cfg.paths.data_dir / "projects" / "repo")
    root.rmdir()
    monkeypatch.setattr(builtin_shell.shutil, "which", lambda _: "/usr/bin/docker")

    async def _no(*args, **kwargs):
        raise AssertionError("a container was started against a directory that is not there")

    monkeypatch.setattr(builtin_shell.asyncio, "create_subprocess_exec", _no)
    result = await builtin_shell.shell_exec.handler(
        {"command": "pytest"}, ToolContext(actor="user", origin="interactive")
    )
    assert result.ok is False
    assert str(root) in result.content


# --- the guard that holds the two halves together ----------------------------


def test_a_project_outside_the_allowed_roots_is_refused_at_load(tmp_path):
    with pytest.raises(ValidationError) as exc:
        PathsConfig(
            data_dir=tmp_path,
            allowed_roots=[tmp_path / "projects"],
            project=tmp_path / "elsewhere" / "repo",
        )
    assert "allowed_roots" in str(exc.value)


def test_a_project_inside_an_allowed_root_loads(tmp_path):
    paths = PathsConfig(
        data_dir=tmp_path,
        allowed_roots=[tmp_path / "projects"],
        project=tmp_path / "projects" / "repo",
    )
    assert paths.project_root == (tmp_path / "projects" / "repo").resolve()
