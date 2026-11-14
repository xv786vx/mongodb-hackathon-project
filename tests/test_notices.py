import asyncio

from bot.config import Settings
from bot.notices import make_on_expired, notice_text
from mystery_core.memory import ExpiryNotice

CFG = Settings(team_channels={"A": 10, "B": 20})


def notice(team="A", kind="rumour", case="case_manor"):
    return ExpiryNotice(case_id=case, team_id=team, memory_id="m_secret_id", kind=kind)


def run(n, case_id=None):
    sent = []

    async def post(channel, text):
        sent.append((channel, text))

    asyncio.run(make_on_expired(CFG, post, case_id)(n))
    return sent


def test_notice_goes_only_to_the_owning_teams_channel():
    assert run(notice("A")) == [(10, "A rumour has faded.")]
    assert run(notice("B")) == [(20, "A rumour has faded.")]


def test_text_is_generic_and_never_names_the_memory():
    assert "m_secret_id" not in notice_text(notice())
    assert notice_text(notice(kind="witnessed")) == "Something you heard has faded from memory."


def test_other_cases_and_unknown_teams_are_ignored():
    assert run(notice(case="case_other"), case_id="case_manor") == []
    assert run(notice(team="Z")) == []


def test_a_failing_post_does_not_raise():
    async def boom(channel, text):
        raise RuntimeError("discord down")

    asyncio.run(make_on_expired(CFG, boom)(notice()))
