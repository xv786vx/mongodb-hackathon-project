"""Agent Engine client (ENGINE_MODE=hosted). Only the bot calls the agent, and only the bot holds the key (rule 9).

The response body is ignored except for logging. The output is the trace the agent's submit_reply writes (rule 12).
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from bot.config import Settings
from bot.engine_api import Prepared

log = logging.getLogger("bot.invoke")

HEADER_PREFIX = "X-Mdb-Agent-Engine-Custom-"
ACCEPT = "application/vnd.agent-engine-2026-09-20-preview+json"   # pins request/response shapes
SERVER_TIMEOUT_S = 60                                              # X-Agent-Engine-Invoke-Timeout-Seconds
CLIENT_TIMEOUT_S = SERVER_TIMEOUT_S + 15                           # a little longer than the server's


TOKEN_PATH = "/api/v1/oauth/token"
DEFAULT_TOKEN_TTL_S = 300             # used only if the response carries no expires_in
REFRESH_MARGIN_S = 60                 # refresh this long before the token expires


class InvokeError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class TokenProvider:
    """Exchanges the service account's client id and secret for a bearer token, and caches it.

    The secret goes only in the Basic auth header of the token request. It is never logged.
    """

    def __init__(self, cfg: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg, self._transport = cfg, transport
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token, self._expires_at = "", 0.0

    async def get(self) -> str:
        if not (self.cfg.ae_client_id and self.cfg.ae_client_secret):
            return self.cfg.ae_api_key                          # static token fallback
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - REFRESH_MARGIN_S:
                return self._token
            try:
                async with httpx.AsyncClient(timeout=15, transport=self._transport) as c:
                    r = await c.post(self.cfg.ae_base_url + TOKEN_PATH,
                                     auth=(self.cfg.ae_client_id, self.cfg.ae_client_secret),
                                     data={"grant_type": "client_credentials"})
            except httpx.HTTPError as e:
                raise InvokeError(f"token request failed: {e.__class__.__name__}", retryable=True) from e
            if r.status_code >= 400:
                raise InvokeError(f"token request rejected ({r.status_code})", retryable=False)
            body = r.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", DEFAULT_TOKEN_TTL_S))
            return self._token


def build_headers(token: str, p: Prepared) -> dict[str, str]:
    """The five agreed custom headers, sent on every invoke. The platform strips the prefix and lowercases."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
        "X-Agent-Engine-Invoke-Timeout-Seconds": str(SERVER_TIMEOUT_S),
        HEADER_PREFIX + "Case-Id": p.case_id,
        HEADER_PREFIX + "Team-Id": p.team_id,
        HEADER_PREFIX + "Suspect-Id": p.suspect_id,
        HEADER_PREFIX + "Turn-Id": p.turn_id,
        HEADER_PREFIX + "Command": p.command,
    }


def invoke_url(cfg: Settings) -> str:
    return (f"{cfg.ae_base_url}/api/v1/projects/{cfg.ae_project_id}"
            f"/workspaces/{cfg.ae_workspace_id}/invoke")


_tokens: dict[int, TokenProvider] = {}


def _provider(cfg: Settings, transport) -> TokenProvider:
    if transport is not None:                                   # tests get their own provider
        return TokenProvider(cfg, transport)
    return _tokens.setdefault(id(cfg), TokenProvider(cfg))


async def invoke(cfg: Settings, p: Prepared, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """One invoke. Raises InvokeError; `retryable` is True only for a confirmed failure that did not run the turn.

    A client timeout is NOT retryable here: the first execution may still be running, so the caller must look at
    the trace (pending / complete / rejected) before deciding anything. A 401 refreshes the token once.
    """
    tokens = _provider(cfg, transport)
    for attempt in (1, 2):
        token = await tokens.get()
        try:
            async with httpx.AsyncClient(timeout=CLIENT_TIMEOUT_S, transport=transport) as client:
                r = await client.post(invoke_url(cfg), headers=build_headers(token, p), json={"message": p.message})
        except httpx.ConnectError as e:
            raise InvokeError(f"could not connect: {e.__class__.__name__}", retryable=True) from e
        except httpx.TimeoutException as e:
            raise InvokeError("invoke timed out; the execution may still be running", retryable=False) from e
        except httpx.HTTPError as e:
            raise InvokeError(f"transport error: {e.__class__.__name__}", retryable=False) from e
        if r.status_code == 401 and attempt == 1:
            tokens.invalidate()                                 # expired or revoked: fetch a fresh one
            continue
        break

    if r.status_code in (502, 503, 504):
        raise InvokeError(f"agent engine unavailable ({r.status_code})", retryable=True)
    if r.status_code >= 400:
        # Log the status only. Never log headers or bodies: they can carry the token or memory text.
        raise InvokeError(f"invoke failed ({r.status_code})", retryable=False)
    log.info("invoked turn %s (%s)", p.turn_id, r.status_code)
