"""Google OAuth for an installed app: the one place a refresh token is spent.

Gmail and Calendar are two connectors sharing one credential, so the credential does not
live in either of them. The vault layout is:

    google/client        client_id, client_secret   - one Cloud project, shared by accounts
    google/<address>     refresh_token, scopes      - one per mailbox you authorised

Three things here are deliberate and easy to undo by accident:

- **The refresh token is never exchanged eagerly.** An access token is fetched on the first
  request that needs one and cached in this process until a minute before it expires. A
  daemon restart therefore costs one extra token request, not a re-authorisation.
- **The exchange takes the caller's `httpx.AsyncClient`.** Same rule as every connector: no
  module here constructs one, which is what lets a test drive the whole refresh path
  through `MockTransport` without a network.
- **`invalid_grant` is an auth error, not a transient one.** A revoked, expired or
  password-reset-invalidated refresh token will never start working again, so it must reach
  `_handle_auth_error` and stop the connector rather than back off forever.

The scopes are read-only and stay that way. Nothing in this daemon sends mail, marks mail
read, or writes a calendar event, and a read-only scope is the cheapest possible proof of
that - it is enforced by Google rather than by our own restraint.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets as pysecrets
import threading
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from .. import secrets as vault
from ..config import Config
from .base import ConnectorAuthError, ConnectorRateLimited, ConnectorTransient

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CLIENT_REF = "google/client"


def account_ref(address: str) -> str:
    return f"google/{address}"


def not_configured(address: str) -> str | None:
    """None when this account can poll; otherwise the command that fixes it."""
    if not address:
        return "set address for this account in config.toml"
    if vault.get(CLIENT_REF, "client_id") is None:
        return f"no OAuth client: run `agent secrets set {CLIENT_REF} client_id`"
    if vault.get(CLIENT_REF, "client_secret") is None:
        return f"no OAuth client: run `agent secrets set {CLIENT_REF} client_secret`"
    if vault.get(account_ref(address), "refresh_token") is None:
        return f"not authorised: run `agent connectors auth <label>` for {address}"
    return None


def granted_scopes(address: str) -> list[str]:
    """What this account was actually authorised for.

    `vault.get` returns scalars only, and scopes are a list, so this reads the vault
    directly. A vault with the wrong permissions must not crash the caller - `configured()`
    is called from the supervised loop, where an exception is a restart rather than an
    explanation - so the failure reads as "no scopes", which reports itself.
    """
    try:
        node = vault.load()
        for part in ("google", address):
            node = node.get(part) if isinstance(node, dict) else None
        scopes = node.get("scopes") if isinstance(node, dict) else None
    except Exception:
        return []
    return [str(s) for s in scopes] if isinstance(scopes, list) else []


# --- access tokens -----------------------------------------------------------


@dataclass
class _Cached:
    value: str
    expires_at: float  # monotonic


_tokens: dict[str, _Cached] = {}


def forget(address: str) -> None:
    """Drop the cached access token. Called on a 401 so the next attempt refreshes rather
    than replaying a token the server has already stopped accepting."""
    _tokens.pop(address, None)


async def access_token(address: str, cfg: Config, client: httpx.AsyncClient) -> str:
    cached = _tokens.get(address)
    if cached and cached.expires_at > time.monotonic():
        return cached.value

    client_id = vault.get(CLIENT_REF, "client_id")
    client_secret = vault.get(CLIENT_REF, "client_secret")
    refresh = vault.get(account_ref(address), "refresh_token")
    if client_id is None or client_secret is None or refresh is None:
        raise ConnectorAuthError(not_configured(address) or "missing Google credentials")

    try:
        response = await client.post(
            cfg.connectors.google.token_uri,
            data={
                "client_id": client_id.reveal(),
                "client_secret": client_secret.reveal(),
                "refresh_token": refresh.reveal(),
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise ConnectorTransient(f"token endpoint: {exc}") from exc

    if response.status_code == 429:
        raise ConnectorRateLimited(_retry_after(response, 60.0))
    if response.status_code >= 500:
        raise ConnectorTransient(f"token endpoint returned {response.status_code}")
    if response.status_code >= 400:
        # Google names the failure in `error`; `invalid_grant` is the terminal one and the
        # only message worth repeating to a human, since it tells them to re-authorise.
        error = _error_code(response)
        if error in ("invalid_grant", "invalid_client", "unauthorized_client"):
            raise ConnectorAuthError(
                f"{address}: refresh token rejected ({error}) - re-run "
                f"`agent connectors auth` for this account"
            )
        raise ConnectorTransient(f"token endpoint returned {response.status_code} ({error})")

    payload = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ConnectorTransient("token endpoint returned no access_token")
    try:
        lifetime = float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600.0
    # A minute of margin, so a token cannot expire between the check and the request.
    _tokens[address] = _Cached(token, time.monotonic() + max(30.0, lifetime - 60.0))
    return token


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unparseable"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):  # the API style, as opposed to the OAuth style
            return str(error.get("status") or error.get("message") or "")[:120]
        return str(error or "")[:120]
    return ""


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return default


# --- authenticated requests --------------------------------------------------


def _error_reason(response: httpx.Response) -> str:
    """Google's error envelope, defensively. A 403 whose body cannot be parsed is treated as
    a permission problem, which is the direction that asks a human rather than retries."""
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason") or "")[:60]
    return str(error.get("status") or "")[:60]


def check(response: httpx.Response, *, what: str, address: str) -> None:
    if response.status_code == 401:
        raise ConnectorAuthError(f"{address}: access token rejected (401)")
    if response.status_code == 429:
        raise ConnectorRateLimited(_retry_after(response, 60.0))
    if response.status_code == 403:
        # 403 is two different answers wearing one status code. "You are going too fast" is
        # the API working as designed; "this token may not read that" needs a human, and
        # telling them apart is the difference between backing off and stopping.
        reason = _error_reason(response)
        if reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
            raise ConnectorRateLimited(_retry_after(response, 60.0))
        raise ConnectorAuthError(
            f"{address}: {what} returned 403 ({reason or 'no reason given'}). Either the "
            f"API is not enabled on the Cloud project, or the account's administrator does "
            f"not allow this OAuth client."
        )
    if response.status_code == 404:
        raise ConnectorAuthError(f"{address}: {what} has nothing at that path (404)")
    if response.status_code >= 500:
        raise ConnectorTransient(f"{what} returned {response.status_code}")


async def get_json(
    cfg: Config,
    client: httpx.AsyncClient,
    *,
    address: str,
    url: str,
    params: list[tuple[str, str]],
    what: str,
) -> dict:
    """One authenticated GET, with exactly one retry on 401.

    The retry is there because an access token can expire between the cache check and the
    request reaching Google. Retrying once on a *fresh* token distinguishes that from a
    refresh token that has actually been revoked, which must stop the connector instead.
    """
    for attempt in (1, 2):
        token = await access_token(address, cfg, client)
        try:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise ConnectorTransient(str(exc)) from exc

        if response.status_code == 401 and attempt == 1:
            forget(address)
            continue
        check(response, what=what, address=address)
        try:
            body = response.json()
        except ValueError as exc:
            raise ConnectorTransient(f"{what} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ConnectorTransient(f"{what} returned an unexpected shape")
        return body
    raise ConnectorAuthError(f"{address}: {what} rejected the access token")


# --- the one-time authorisation ----------------------------------------------


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    """Catches the redirect. Nothing here trusts the browser beyond reading one query
    parameter, and the server is bound to loopback and serves exactly one request."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        type(self).code = (query.get("code") or [None])[0]
        type(self).error = (query.get("error") or [None])[0]
        body = (
            b"Authorised. You can close this tab and go back to the terminal."
            if type(self).code
            else b"No code came back. Check the terminal."
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        """Silence. The default logs the full request line to stderr, and the request line
        of the redirect contains the authorisation code."""


def build_auth_url(cfg: Config, client_id: str, redirect_uri: str, scopes: list[str],
                   challenge: str, address: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        # offline + consent is what actually returns a refresh token. Without `consent`,
        # a second authorisation of the same account returns none, and the connector then
        # works until the first restart and never again.
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": address,
    }
    return f"{cfg.connectors.google.auth_uri}?{urllib.parse.urlencode(params)}"


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def serve_once(port: int, timeout_s: float) -> tuple[http.server.HTTPServer, threading.Thread]:
    server = http.server.HTTPServer(("127.0.0.1", port), _CodeHandler)
    server.timeout = timeout_s
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server, thread


async def exchange_code(
    cfg: Config, client: httpx.AsyncClient, *, code: str, verifier: str, redirect_uri: str
) -> dict:
    client_id = vault.get(CLIENT_REF, "client_id")
    client_secret = vault.get(CLIENT_REF, "client_secret")
    if client_id is None or client_secret is None:
        raise ConnectorAuthError(f"no OAuth client in the vault at {CLIENT_REF}")
    response = await client.post(
        cfg.connectors.google.token_uri,
        data={
            "client_id": client_id.reveal(),
            "client_secret": client_secret.reveal(),
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise ConnectorAuthError(f"code exchange failed: {_error_code(response)}")
    payload = response.json()
    if not payload.get("refresh_token"):
        raise ConnectorAuthError(
            "Google returned no refresh_token. That happens when the account was already "
            "authorised for this client; revoke it at myaccount.google.com/permissions "
            "and try again."
        )
    return payload


def store(address: str, payload: dict, scopes: list[str]) -> None:
    """The only writer of a Google credential. Scopes are stored alongside so that adding
    calendar access later is a visible mismatch rather than a silent 403."""
    vault.put(
        account_ref(address),
        refresh_token=payload["refresh_token"],
        scopes=scopes,
        obtained_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
