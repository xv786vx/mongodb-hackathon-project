import pytest

from bot.config import Settings
from bot.fake_engine import EVENT_NOTICE, FakeEngine
from bot.identity import NotHost, NotOnATeam, WrongChannel, require_host, resolve_team
from bot.render import found_text

CFG = Settings(team_roles={"A": 1, "B": 2}, team_channels={"A": 10, "B": 20}, host_role_id=9)


def test_team_comes_from_role():
    assert resolve_team(CFG, {1}, 10).team_id == "A"
    assert resolve_team(CFG, {2, 77}, 20).team_id == "B"


def test_refuses_no_role_two_roles_and_wrong_channel():
    with pytest.raises(NotOnATeam):
        resolve_team(CFG, {77}, 10)
    with pytest.raises(NotOnATeam):
        resolve_team(CFG, {1, 2}, 10)
    with pytest.raises(WrongChannel):
        resolve_team(CFG, {1}, 20)          # team A in team B's channel


def test_host_check():
    require_host(CFG, {9})
    with pytest.raises(NotHost):
        require_host(CFG, {1})


def ask(e, team, turn, q="where were you after midnight?", suspect="butler"):
    prep = e.prepare_interrogate(team, suspect, q, [])
    p = e.begin_turn(team, suspect, turn, "interrogate", q, prep)
    e.generate(p)
    return e.get_trace(team, turn)


def stage_team_a(e):
    e.discover("A", "ev_note")
    assert e.prepare_present("A", "butler", "ev_note", "", []).ok


def test_core_demo():
    e = FakeEngine()
    stage_team_a(e)
    offer = ask(e, "A", "t_1").offer_id
    assert offer is not None
    assert ask(e, "B", "t_2").offer_id is None                  # Team B has no live lead
    assert e.use_offer("B", offer).status == "forbidden"
    result = e.trigger_event("gardener_recants")
    assert result.notify_teams == ("A",) and result.notice == EVENT_NOTICE   # Team B is not told
    assert e.use_offer("A", offer).status == "rejected"
    assert ask(e, "A", "t_3").offer_id is None                  # a fresh question offers nothing
    assert not e.prepare_present("A", "butler", "ev_note", "", []).ok       # withdrawn evidence cannot return


def test_present_needs_found_evidence():
    e = FakeEngine()
    prep = e.prepare_present("A", "butler", "ev_note", "", [])
    assert not prep.ok and prep.error
    assert "ev_note" not in {x.id for x in e.team_findings("B")}  # autocomplete lists only the team's own


def test_double_click_acts_once():
    e = FakeEngine()
    stage_team_a(e)
    offer = ask(e, "A", "t_1").offer_id
    assert e.use_offer("A", offer).status == "used"
    assert e.use_offer("A", offer).status == "already_used"


def test_retry_same_turn_same_trace():
    e = FakeEngine()
    first = ask(e, "A", "t_1")
    again = ask(e, "A", "t_1")
    assert first == again and len(e.traces) == 1


def test_search_closed_and_open_rooms():
    e = FakeEngine()
    assert not e.search_room("A", "wine_cellar").ok
    r = e.search_room("A", "greenhouse")
    assert r.ok and "gardener's note" in found_text(r.found)
    assert found_text(e.search_room("A", "greenhouse").found) == "Nothing new here."   # already held


def test_accusation_flow():
    e = FakeEngine()
    assert e.accuse("A", "cook", "fire_poker", "map_room").status == "eliminated"
    assert e.is_eliminated("A")
    assert e.accuse("A", "butler", "fire_poker", "map_room").status == "already_accused"
    assert e.accuse("B", "nobody", "fire_poker", "map_room").status == "invalid"
    assert e.accuse("B", "butler", "fire_poker", "map_room").status == "solved"


# ---- hosted invoke -------------------------------------------------------------------------------
import asyncio

import httpx

from bot import invoke as ae
from bot.engine_api import Prepared

HOSTED = Settings(engine_mode="hosted", ae_project_id="proj", ae_workspace_id="ws", ae_api_key="static-token")
CREDS = Settings(engine_mode="hosted", ae_project_id="proj", ae_workspace_id="ws",
                 ae_client_id="cid", ae_client_secret="csecret")
PREP = Prepared("t_9", "case_manor", "A", "butler", "interrogate", "hello")


def run_invoke(handler):
    return asyncio.run(ae.invoke(HOSTED, PREP, transport=httpx.MockTransport(handler)))


def test_invoke_sends_the_five_headers_and_message():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(200, json={})

    run_invoke(handler)
    req = seen["req"]
    assert str(req.url) == "https://agentengine.mongodb.com/api/v1/projects/proj/workspaces/ws/invoke"
    prefix = "x-mdb-agent-engine-custom-"
    got = {k.lower(): v for k, v in req.headers.items()}
    assert {k[len(prefix):]: v for k, v in got.items() if k.startswith(prefix)} == {
        "case-id": "case_manor", "team-id": "A", "suspect-id": "butler", "turn-id": "t_9", "command": "interrogate"}
    assert got["authorization"] == "Bearer static-token"
    assert got["x-agent-engine-invoke-timeout-seconds"] == "60"
    assert "2026-09-20-preview" in got["accept"]
    assert req.read() == b'{"message":"hello"}'


@pytest.mark.parametrize("status,retryable", [(503, True), (502, True), (401, False), (500, False)])
def test_invoke_error_classification(status, retryable):
    with pytest.raises(ae.InvokeError) as e:
        run_invoke(lambda req: httpx.Response(status))
    assert e.value.retryable is retryable


def test_timeout_is_never_retried_blindly():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ae.InvokeError) as e:
        run_invoke(handler)
    assert e.value.retryable is False          # the first execution may still be running


def test_hosted_requires_agent_engine_settings():
    gaps = Settings(engine_mode="hosted", token="t", guild_id=1, team_roles={"A": 1}, team_channels={"A": 1},
                    host_role_id=1).missing()
    assert {"AGENT_ENGINE_PROJECT_ID", "AGENT_ENGINE_WORKSPACE_ID"} <= set(gaps)
    assert any("CLIENT_ID" in g for g in gaps)


CREDS_OK = Settings(engine_mode="hosted", token="t", guild_id=1, team_roles={"A": 1}, team_channels={"A": 1},
                    host_role_id=1, ae_project_id="p", ae_workspace_id="w", ae_client_id="c", ae_client_secret="s")


def test_credentials_satisfy_hosted_settings():
    assert not any("CLIENT_ID" in g for g in CREDS_OK.missing())


def test_token_exchange_then_invoke_uses_bearer_token():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path == "/api/v1/oauth/token":
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return httpx.Response(200, json={})

    asyncio.run(ae.invoke(CREDS, PREP, transport=httpx.MockTransport(handler)))
    token_req, invoke_req = calls
    assert token_req.headers["authorization"].startswith("Basic ")            # client id:secret, Basic auth
    assert b"grant_type=client_credentials" in token_req.read()
    assert invoke_req.headers["authorization"] == "Bearer tok-1"


def test_401_refreshes_token_once_then_succeeds():
    issued, invokes = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/oauth/token":
            issued.append(1)
            return httpx.Response(200, json={"access_token": f"tok-{len(issued)}", "expires_in": 3600})
        invokes.append(req.headers["authorization"])
        return httpx.Response(401 if len(invokes) == 1 else 200)

    asyncio.run(ae.invoke(CREDS, PREP, transport=httpx.MockTransport(handler)))
    assert invokes == ["Bearer tok-1", "Bearer tok-2"]


def test_token_rejection_is_an_error_without_leaking_the_secret():
    with pytest.raises(ae.InvokeError) as e:
        asyncio.run(ae.invoke(CREDS, PREP, transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    assert "csecret" not in str(e.value)
