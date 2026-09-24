# Sourced by /etc/profile, because shell_exec runs `bash -lc <command>` - a login shell.
#
# Brings up the sandbox's own Postgres on container loopback and points the agent's config
# at it, so `uv run pytest` inside the sandbox reaches a real database with `--network none`
# and without any route to the host's. Idempotent: a second source is a no-op.
#
# /etc/profile resets PATH for non-root logins (dropping anything the image's ENV put
# there), so PATH is restored here rather than in the Dockerfile.

# /opt/venv/bin is on PATH because the base image is Postgres, not python:3.12-slim, and
# so has no system interpreter at all. Without this a model-written `python3 -c ...` - which
# the old image answered - fails with "command not found". The project environment is the
# only Python here, and it is the right one: it is what `uv run` execs too.
PATH="/usr/lib/postgresql/17/bin:/opt/venv/bin:$PATH"
export PATH

export PGDATA=/tmp/pgdata
export PGHOST=127.0.0.1
export PGPORT=5432
export PGUSER=agent
export PGDATABASE=agent
# The config layer reads AGENT_<SECTION>__<KEY>. tests/conftest.py splits this DSN on the
# last "/" to build its admin and scratch DSNs, so it must stay a plain host:port URL with
# no query string.
export AGENT_DB__DSN="postgresql://agent:agent@127.0.0.1:5432/agent"

_agent_sandbox_pg_up() {
    pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null && return 0

    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        rm -rf "$PGDATA" || return 1
        cp -a /opt/pgdata-template "$PGDATA" || return 1
        chmod 0700 "$PGDATA" || return 1
    fi

    pg_ctl -D "$PGDATA" -l /tmp/postgres.log -w -t 60 -s \
        -o "-c listen_addresses=127.0.0.1 -c port=5432 \
            -c unix_socket_directories=/tmp \
            -c fsync=off -c full_page_writes=off -c synchronous_commit=off \
            -c max_wal_size=64MB -c min_wal_size=32MB -c wal_level=minimal \
            -c max_wal_senders=0 -c autovacuum=off" \
        start >/dev/null 2>&1
}

# Said, not swallowed. If Postgres does not come up, the session-scoped `pg_dsn` fixture
# calls pytest.skip and the whole suite reports "skipped" - which reads like a pass and is
# exactly the failure mode this project keeps getting bitten by. Make it loud instead.
if ! _agent_sandbox_pg_up; then
    echo "sandbox: postgres did not start; the test suite will skip. Log follows:" >&2
    tail -n 25 /tmp/postgres.log >&2 2>/dev/null
fi
