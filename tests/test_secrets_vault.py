"""The vault: what it stores, and what it refuses to say out loud."""

from __future__ import annotations

import pytest

from agentd import secrets


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "secrets.toml"


def test_a_secret_will_not_print_itself(vault):
    value = secrets.Secret("github_pat_supersecret")
    assert "supersecret" not in repr(value)
    assert "supersecret" not in str(value)
    assert "supersecret" not in f"{value}"
    assert "supersecret" not in f"{value!r}"
    assert "supersecret" not in "{}".format(value)  # noqa: UP032 - the point is the format path
    # and the only way out is to say so explicitly
    assert value.reveal() == "github_pat_supersecret"


def test_round_trip_through_the_file(vault):
    secrets.put("google/dylan@nyu.edu", vault, refresh_token="1//abc", scopes=["a", "b"])
    secrets.put("github/dyd2008", vault, token="ghp_x")

    assert secrets.get("google/dylan@nyu.edu", "refresh_token", vault).reveal() == "1//abc"
    assert secrets.get("github/dyd2008", "token", vault).reveal() == "ghp_x"
    assert secrets.load(vault)["google"]["dylan@nyu.edu"]["scopes"] == ["a", "b"]


def test_writing_one_entry_leaves_the_others_alone(vault):
    secrets.put("github/dyd2008", vault, token="first")
    secrets.put("google/client", vault, client_id="cid", client_secret="csec")
    secrets.put("github/dyd2008", vault, token="second")

    assert secrets.get("github/dyd2008", "token", vault).reveal() == "second"
    assert secrets.get("google/client", "client_id", vault).reveal() == "cid"


def test_the_file_is_written_owner_only(vault):
    secrets.put("github/x", vault, token="t")
    assert vault.stat().st_mode & 0o777 == 0o600
    assert secrets.check_permissions(vault) is None


def test_a_world_readable_vault_is_refused_rather_than_read(vault):
    secrets.put("github/x", vault, token="t")
    vault.chmod(0o644)
    with pytest.raises(secrets.VaultPermissionError):
        secrets.load(vault)


def test_a_missing_credential_is_none_not_an_explosion(vault):
    """A connector that is not configured should say so, not take the daemon down."""
    secrets.put("github/x", vault, token="t")
    assert secrets.get("github/nobody", "token", vault) is None
    assert secrets.get("github/x", "nonexistent", vault) is None
    assert secrets.get("nothing/at/all", "token", vault) is None


def test_listing_shows_names_and_never_values(vault):
    secrets.put("google/dylan@nyu.edu", vault, refresh_token="1//abc")
    secrets.put("github/dyd2008", vault, token="ghp_x")

    listing = secrets.describe(vault)
    rendered = repr(listing)
    assert ("github/dyd2008", ["token"]) in listing
    assert ("google/dylan@nyu.edu", ["refresh_token"]) in listing
    assert "1//abc" not in rendered and "ghp_x" not in rendered


def test_fingerprints_tell_two_credentials_apart_without_revealing_either():
    a, b = secrets.Secret("token-one"), secrets.Secret("token-two")
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint() == secrets.Secret("token-one").fingerprint()
    assert "token" not in a.fingerprint()


def test_removal(vault):
    secrets.put("github/x", vault, token="t")
    assert secrets.remove("github/x", vault) is True
    assert secrets.get("github/x", "token", vault) is None
    assert secrets.remove("github/x", vault) is False


def test_awkward_values_survive_the_round_trip(vault):
    """Tokens contain slashes and quotes; a hand-rolled TOML writer has to handle them."""
    nasty = 'a"b\\c/d=e[f]'
    secrets.put("weird/entry", vault, token=nasty, count=3, on=True)
    assert secrets.get("weird/entry", "token", vault).reveal() == nasty
    assert secrets.load(vault)["weird"]["entry"]["count"] == 3
    assert secrets.load(vault)["weird"]["entry"]["on"] is True


def test_the_vault_is_not_reachable_through_config():
    """Config is dumped in `agent doctor` and folded in from AGENT_* env vars. If credentials
    lived there, one model_dump would leak them."""
    from agentd.config import Config

    dumped = repr(Config().model_dump())
    assert "secret" not in dumped.lower() or "secrets.toml" not in dumped
    assert not hasattr(Config(), "secrets")
