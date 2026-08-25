"""OAuth for MCP, where scanning the QR *is* the login.

The alternative this replaces is a capability URL — `?k=<token>` pasted into a
connector dialog. That works, but the secret then lives in browser history,
proxy logs and referrer headers, and it is a bad first impression for something
people install on their own phone number.

With this, the client does what it does for any OAuth server: discovers the
metadata, registers itself, and opens a browser. The only unusual part is what
the user does in that browser — instead of typing a password they scan a QR with
WhatsApp. Pairing and authorizing become one step.

    client                     this server                    the user
      │  discover .well-known      │                              │
      │─────────────────────────►  │                              │
      │  POST /register            │                              │
      │─────────────────────────►  │  (dynamic client reg)        │
      │  open /authorize           │                              │
      │─────────────────────────►  │  redirect to /connect?flow=… │
      │                            │─────────────────────────────►│ scans QR
      │                            │  PairStatusEv arrives        │
      │  ◄──── redirect_uri?code=… │◄─────────────────────────────│
      │  POST /token (+PKCE)       │                              │
      │─────────────────────────►  │  access token                │

Everything is stored through the same `kv` the rest of the app uses, so it
inherits whichever backend is configured and survives a restart. Nothing here
is in memory only — a token that vanished on restart would log every client out
whenever the process was updated.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode,
                                      AuthorizationParams, RefreshToken)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions

log = logging.getLogger(__name__)

CODE_TTL = 300           # an authorization code is single-use and short-lived
TOKEN_TTL = 30 * 86400   # a linked device lasts until it is unlinked; match that
FLOW_TTL = 900           # how long someone has to find their phone and scan


def _kv(prefix: str, key: str) -> str:
    return f"oauth.{prefix}.{key}"


class WhatsAppOAuth(OAuthProvider):
    """An OAuth 2.1 server whose authentication step is a WhatsApp pairing."""

    def __init__(self, runtime, base_url: str):
        super().__init__(
            base_url=base_url,
            # Dynamic registration is not optional here: MCP clients have no way
            # to pre-register, and requiring a hand-made client_id would put us
            # back to copying secrets around.
            client_registration_options=ClientRegistrationOptions(enabled=True),
            required_scopes=[],
        )
        self.rt = runtime

    # ------------------------------------------------------------- clients

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.rt.store.put_kv(
            _kv("client", client_info.client_id),
            client_info.model_dump(mode="json"))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self.rt.store.get_kv(_kv("client", client_id))
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    # ----------------------------------------------------------- authorize

    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        """Return where to send the browser.

        The pending request is parked under a random id and the user is sent to
        the pairing page. Nothing about the client — least of all the redirect
        uri — travels in the URL the user sees, so a half-finished flow cannot
        be steered somewhere else by editing the address bar.
        """
        flow = secrets.token_urlsafe(24)
        await self.rt.store.put_kv(_kv("flow", flow), {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "resource": params.resource,
            "expires_at": time.time() + FLOW_TTL,
        })
        return f"/connect?flow={flow}"

    async def complete_flow(self, flow: str) -> str | None:
        """Called by the pairing page once a number is linked.

        Returns the redirect back to the client, or None if the flow is unknown
        or expired — in which case the page just carries on as an ordinary
        pairing screen rather than erroring at someone who has just succeeded.
        """
        pending = await self.rt.store.get_kv(_kv("flow", flow))
        if not pending or pending.get("expires_at", 0) < time.time():
            return None

        code = secrets.token_urlsafe(32)
        await self.rt.store.put_kv(_kv("code", code), {
            **pending,
            "code": code,
            "expires_at": time.time() + CODE_TTL,
            "subject": self.rt.status().get("number") or "whatsapp",
        })
        await self.rt.store.put_kv(_kv("flow", flow), {"expires_at": 0})

        sep = "&" if "?" in pending["redirect_uri"] else "?"
        url = f"{pending['redirect_uri']}{sep}code={code}"
        if pending.get("state"):
            url += f"&state={pending['state']}"
        return url

    # ---------------------------------------------------------------- code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        raw = await self.rt.store.get_kv(_kv("code", authorization_code))
        if not raw or raw.get("client_id") != client.client_id:
            return None
        if raw.get("expires_at", 0) < time.time():
            return None
        return AuthorizationCode(
            code=raw["code"], scopes=raw.get("scopes") or [],
            expires_at=raw["expires_at"], client_id=raw["client_id"],
            code_challenge=raw["code_challenge"],
            redirect_uri=raw["redirect_uri"],
            redirect_uri_provided_explicitly=raw.get(
                "redirect_uri_provided_explicitly", True),
            resource=raw.get("resource"), subject=raw.get("subject"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use. Burned before the token is minted, so a replayed code
        # cannot mint a second one even if two requests arrive together.
        await self.rt.store.put_kv(_kv("code", authorization_code.code),
                                   {"expires_at": 0})

        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        expires = int(time.time()) + TOKEN_TTL
        common = {
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "subject": authorization_code.subject,
            "resource": authorization_code.resource,
            "expires_at": expires,
        }
        await self.rt.store.put_kv(_kv("token", access), {"token": access, **common})
        await self.rt.store.put_kv(_kv("refresh", refresh), {"token": refresh, **common})
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=TOKEN_TTL, refresh_token=refresh,
                          scope=" ".join(authorization_code.scopes))

    # --------------------------------------------------------------- token

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = await self.rt.store.get_kv(_kv("token", token))
        if not raw:
            return None
        expires = raw.get("expires_at")
        # None means never — that is how the configured static token is stored.
        if expires is not None and expires < time.time():
            return None
        return AccessToken(token=raw["token"], client_id=raw["client_id"],
                           scopes=raw.get("scopes") or [],
                           expires_at=raw.get("expires_at"),
                           resource=raw.get("resource"), subject=raw.get("subject"))

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        raw = await self.rt.store.get_kv(_kv("refresh", refresh_token))
        if not raw or raw.get("client_id") != client.client_id:
            return None
        if (raw.get("expires_at") or 0) < time.time():
            return None
        return RefreshToken(token=raw["token"], client_id=raw["client_id"],
                            scopes=raw.get("scopes") or [],
                            expires_at=raw.get("expires_at"),
                            subject=raw.get("subject"))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        # Rotate: the presented refresh token is retired as the new pair is
        # issued, so a stolen one is usable at most once and its use is visible
        # as the legitimate client being logged out.
        await self.rt.store.put_kv(_kv("refresh", refresh_token.token),
                                   {"expires_at": 0})
        access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        expires = int(time.time()) + TOKEN_TTL
        common = {"client_id": client.client_id,
                  "scopes": scopes or refresh_token.scopes,
                  "subject": refresh_token.subject, "expires_at": expires}
        await self.rt.store.put_kv(_kv("token", access), {"token": access, **common})
        await self.rt.store.put_kv(_kv("refresh", new_refresh),
                                   {"token": new_refresh, **common})
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=TOKEN_TTL, refresh_token=new_refresh,
                          scope=" ".join(common["scopes"]))

    async def revoke_token(self, token) -> None:
        for prefix in ("token", "refresh"):
            await self.rt.store.put_kv(_kv(prefix, getattr(token, "token", "")),
                                       {"expires_at": 0})


def verify_pkce(verifier: str, challenge: str) -> bool:
    """S256 only.

    `plain` is still in the spec and is worthless — anyone who can see the
    challenge can replay it. Rejecting it outright is a one-line decision that
    removes a whole class of interception.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return secrets.compare_digest(expected, challenge)
