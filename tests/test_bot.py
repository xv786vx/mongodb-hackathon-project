import pytest

from bot.config import Settings
from bot.fake_engine import FakeEngine
from bot.identity import NotHost, NotOnATeam, WrongChannel, require_host, resolve_team

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


def ask(e, team, turn):
    p = e.begin_turn("c", team, "butler", turn, "interrogate", "where were you after midnight?")
    e.generate(p)
    return e.get_trace(turn)


def test_core_demo_in_fake_engine():
    e = FakeEngine()
    assert ask(e, "A", "t_1").offer_id is not None
    assert ask(e, "B", "t_2").offer_id is None            # Team B has no live lead
    offer = e.get_trace("t_1").offer_id
    assert e.use_offer("c", "B", offer).status == "wrong_team"
    e.trigger_event("c", "gardener_recants")
    assert e.use_offer("c", "A", offer).status == "lead_gone"
    assert ask(e, "A", "t_3").offer_id is None             # a fresh question offers nothing


def test_double_click_acts_once():
    e = FakeEngine()
    offer = ask(e, "A", "t_1").offer_id
    assert e.use_offer("c", "A", offer).status == "used"
    assert e.use_offer("c", "A", offer).status == "already_used"


def test_retry_same_turn_same_trace():
    e = FakeEngine()
    first = ask(e, "A", "t_1")
    again = ask(e, "A", "t_1")
    assert first == again and len(e.traces) == 1
