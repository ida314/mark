# syntax=docker/dockerfile:1
#
# Throwaway execution environment for shell_exec. No secrets, no docker socket, no host
# mounts other than the project (or workspace) at /workspace.
#
# The sandbox runs `--network none`. That is the point of it: it executes command lines the
# model wrote, so it must not be able to reach the agent's own Postgres on the host, nor the
# internet. But a delegated `coder` worker still has to be able to run this project's suite,
# and every test in it needs a database. So the image is self-contained: it carries its own
# Postgres (same image and pgvector version as compose.yaml) reachable over *container*
# loopback, which works fine with no network, and its own prebuilt Python environment.

FROM ghcr.io/astral-sh/uv:latest AS uvbin

FROM pgvector/pgvector:0.8.1-pg17

# What the previous image had and shell_exec users still rely on.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uvbin /uv /uvx /usr/local/bin/

# UV_PROJECT_ENVIRONMENT is load-bearing, not tidiness. /workspace is the *real* repository
# mounted read-write, so a `uv sync` that fell back to its default of /workspace/.venv would
# overwrite the host virtualenv the user works in. Pointing it at /opt/venv means every uv
# invocation inside the container, including ones the model writes itself, resolves to the
# prebuilt environment and cannot touch .venv.
#
# UV_NO_SYNC + UV_OFFLINE: at run time there is no network, so `uv run pytest` must exec the
# prebuilt environment rather than try to reconcile it against an index. UV_CACHE_DIR points
# into the tmpfs because the root filesystem is read-only.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    UV_OFFLINE=1 \
    UV_PYTHON_DOWNLOADS=never

# The project declares >=3.12, but the host virtualenv is 3.14 and that is what the user
# actually runs the suite on. Match it, so a failure here is a failure there.
RUN UV_OFFLINE=0 UV_PYTHON_DOWNLOADS=manual uv python install 3.14

RUN useradd -m -u 1000 sandbox

# Built at /workspace on purpose: `uv sync` installs the project itself as an editable
# install, and the path it records has to be the one the mount will supply at run time.
# The stub package exists only to give hatchling something to point at; the bind mount
# replaces all of it, and the build removes it again below.
WORKDIR /workspace
COPY pyproject.toml uv.lock /workspace/
RUN mkdir -p /workspace/src/agentd && touch /workspace/src/agentd/__init__.py \
    && UV_OFFLINE=0 UV_NO_SYNC=0 uv sync --frozen --python 3.14 \
    && rm -rf /workspace/src /workspace/pyproject.toml /workspace/uv.lock /tmp/uv-cache

# Postgres cannot initialise a cluster as root, and this image's own `postgres` user is uid
# 999 while the sandbox runs as uid 1000. So the template cluster is created here as uid
# 1000 and copied into the tmpfs at run time. That is both correct on ownership and cheaper
# per container than initdb, which every shell_exec would otherwise pay: 57ms to copy the
# template against 265ms to build a cluster from scratch.
ENV PATH=/usr/lib/postgresql/17/bin:$PATH
RUN install -d -o 1000 -g 1000 -m 0700 /opt/pgdata-template
USER sandbox
RUN initdb --pgdata=/opt/pgdata-template --username=agent --auth=trust --no-sync \
      --encoding=UTF8 --locale=C \
    && pg_ctl -D /opt/pgdata-template -w -t 60 -l /tmp/initdb.log \
         -o "-c listen_addresses= -c unix_socket_directories=/tmp -c fsync=off" start \
    && createdb -h /tmp -U agent agent \
    && psql -h /tmp -U agent -d agent -v ON_ERROR_STOP=1 \
         -c "ALTER ROLE agent WITH PASSWORD 'agent'" \
    && pg_ctl -D /opt/pgdata-template -w -t 60 -m fast stop \
    && rm -f /tmp/initdb.log /opt/pgdata-template/postmaster.pid

USER root
COPY docker/sandbox-profile.sh /etc/profile.d/10-agent-sandbox.sh
RUN chmod 0644 /etc/profile.d/10-agent-sandbox.sh

USER sandbox
WORKDIR /workspace
CMD ["bash"]
