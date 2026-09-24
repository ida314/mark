"""The sandbox image has two properties that no other test would notice breaking.

The first is that `uv` inside the container must never build an environment at
`/workspace/.venv`. `/workspace` is the user's real repository, mounted read-write, so a
`uv sync` that fell back to uv's default would overwrite the virtualenv the user works in -
destroying host state from inside a container that exists to contain damage.

The second is that the DSN the image exports for the suite has to survive
`tests/conftest.py`'s `cfg.db.dsn.rsplit("/", 1)`, which is how it derives the admin and
scratch database names. A `?host=...` query string - the obvious way to reach a unix socket -
parses fine everywhere else and silently produces a nonsense database name here, and the
symptom is the whole suite skipping with "postgres unavailable", which reads like a pass.

Both are checked statically against the files, because the image itself is not built in CI.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "docker" / "sandbox.Dockerfile"
PROFILE = REPO / "docker" / "sandbox-profile.sh"


def _env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().removeprefix("export ").removeprefix("ENV ")
        if stripped.startswith(f"{key}="):
            return stripped[len(key) + 1 :].strip().strip('"').rstrip("\\").strip()
    raise AssertionError(f"{key} is not set in {text[:40]!r}...")


def test_the_sandbox_never_builds_a_virtualenv_in_the_mounted_repository() -> None:
    value = _env_value(DOCKERFILE.read_text(), "UV_PROJECT_ENVIRONMENT")
    assert value and not value.startswith("/workspace"), (
        f"UV_PROJECT_ENVIRONMENT={value!r} would let `uv sync` write inside the host mount"
    )


def test_the_sandbox_does_not_try_to_reach_an_index_it_cannot_see() -> None:
    """`--network none` is the default posture, so a run-time sync is a hang, not a fix."""
    text = DOCKERFILE.read_text()
    assert _env_value(text, "UV_NO_SYNC") == "1"
    assert _env_value(text, "UV_OFFLINE") == "1"


def test_the_sandbox_dsn_survives_the_split_conftest_does_to_it() -> None:
    dsn = _env_value(PROFILE.read_text(), "AGENT_DB__DSN")
    assert "?" not in dsn, f"a query string breaks conftest's rsplit: {dsn!r}"
    base, database = dsn.rsplit("/", 1)
    assert database == "agent"
    assert base.endswith(":5432")


def test_the_tmpfs_holds_a_postgres_cluster_as_well_as_the_test_run(cfg) -> None:
    """PGDATA lives in the tmpfs because the container's root filesystem is read-only.

    One full suite run was measured at a 188MB peak there (a 106MB cluster plus pytest's
    tmp_path roots). Below that the suite fails with ENOSPC dressed up as assertion errors.
    """
    from agentd.tools.builtin_shell import docker_command

    joined = " ".join(docker_command("pytest", network=False, timeout_s=10, name="n"))
    size = next(part for part in joined.split() if part.startswith("/tmp:rw,size="))
    assert int(size.removeprefix("/tmp:rw,size=").removesuffix("m")) >= 384
