"""Bot-side hosted invocation with a service-account access token and a fresh platform session."""

import os
import httpx

API_BASE = "https://agentengine.mongodb.com"
API_VERSION = "application/vnd.agent-engine-2026-09-20-preview+json"


class InvokePending(RuntimeError):
    """The original execution may still be running. Poll its trace; don't mint another turn ID."""


async def invoke(prepared, *, client=None):
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=75)
    try:
        token = os.environ.get("AGENT_ENGINE_ACCESS_TOKEN")
        if not token:
            client_id = os.environ.get("AGENT_ENGINE_CLIENT_ID")
            secret = os.environ.get("AGENT_ENGINE_CLIENT_SECRET")
            if not client_id or not secret:
                raise ValueError("Agent Engine service-account credentials are required in the bot environment")
            auth = await client.post(API_BASE + "/api/v1/oauth/token", auth=(client_id, secret),
                                     data={"grant_type": "client_credentials"})
            auth.raise_for_status()
            token = auth.json()["access_token"]
        project, workspace = os.environ["AGENT_ENGINE_PROJECT_ID"], os.environ["AGENT_ENGINE_WORKSPACE_ID"]
        headers = {**prepared.context.custom_headers(), "Authorization": "Bearer " + token,
                   "Accept": API_VERSION, "X-Agent-Engine-Invoke-Timeout-Seconds": "60"}
        # Intentionally omit X-Session-ID. No conversation/session state is shared across turns or teams.
        response = await client.post(f"{API_BASE}/api/v1/projects/{project}/workspaces/{workspace}/invoke",
                                     headers=headers, json={"message": prepared.message})
        response.raise_for_status()
        result = response.json()
        if result.get("status") not in ("completed", "complete"):
            raise InvokePending("Check the existing scoped trace before retrying this turn")
        # The response is operational metadata only. The bot must read get_trace(scope, turn_id).
        return {"execution_id": result.get("execution_id"), "status": result.get("status")}
    except httpx.TimeoutException:
        raise InvokePending("The server may still be working; inspect the same turn trace") from None
    finally:
        if own_client:
            await client.aclose()
