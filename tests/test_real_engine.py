"""RealEngine on person 1's in-memory FakeStore / FakeTraceStore (no Atlas, no network)."""
from types import SimpleNamespace as NS

import pytest

from bot.config import Settings
from bot.engine_api import TurnPrep
from bot.real_engine import EngineNotReady, RealEngine, case_info_from_doc
from mystery_core.engine import context as ctx_mod, turn as turn_mod
from mystery_core.memory import FakeStore, FakeTraceStore, Scope

CASE = {
    "_id": "case_manor", "title": "Death at Halloway Manor", "intro": "Found dead at one.",
    "suspects": [
        {"id": "butler", "name": "Edmund Thorne", "role": "the butler", "voice_id": "v_butler",
         "persona": "PRIVATE persona", "confession": "PRIVATE confession", "fallback_lies": ["PRIVATE"],
         "is_killer": True, "crack_threshold": 2},
        {"id": "cook", "name": "Marta Bell", "role": "the cook", "voice_id": None},
    ],
    "rooms": [{"id": "map_room", "name": "the map room", "open": True},
              {"id": "wine_cellar", "name": "the wine cellar", "open": False}],
    "weapons": [{"id": "fire_poker", "name": "the fire poker"}],
    "events": [{"id": "gardener_recants", "label": "The gardener recants", "notice": "n", "ref": "ev_note"}],
    "evidence": [{"id": "ev_note", "name": "The gardener's note", "text": "PRIVATE clue text"}],
    "seed_memories": [{"id": "s1", "text": "PRIVATE seed memory"}],
}
CFG = Settings(case_id="case_manor", engine_mode="local")


def engine(**kw):
    return RealEngine(CFG, FakeStore(cases={"case_manor": CASE}), FakeTraceStore(), CASE, **kw)


def test_case_info_exposes_only_public_fields():
    info = case_info_from_doc(CASE)
    assert [s.name for s in info.suspects] == ["Edmund Thorne", "Marta Bell"]
    assert info.suspects[0].voice_id == "v_butler"
    assert [r.id for r in info.open_rooms] == ["map_room"]             # locked room not offered
    assert "PRIVATE" not in repr(info)                                  # no persona, confession, clue text, seeds


async def test_turn_lifecycle_on_real_traces():
    e = engine(modules={"turn": turn_mod, "context": ctx_mod})
    prep = TurnPrep(True, message="where were you?", prompt_version="suspect_v1")
    p = await e.begin_turn("A", "butler", "t_1001", "interrogate", "where were you?", prep)
    assert (p.team_id, p.suspect_id) == ("A", "butler")
    assert p.native is not None and p.native.context.turn_id == "t_1001"     # person 2's PreparedTurn
    assert "where were you?" in p.message                                   # rebuilt from the trace input
    assert (await e.get_trace("A", "t_1001")).status == "pending"
    assert await e.get_trace("B", "t_1001") is None                   # another team's trace is invisible

    await e.traces.complete_turn(Scope(case_id="case_manor", team_id="A"), "t_1001", decision="deflect",
                                 reply="Below stairs, sir.", offer_id="a_A_x", offer_label="Search the cellar")
    t = await e.get_trace("A", "t_1001")
    assert (t.status, t.reply, t.decision, t.offer_label) == ("complete", "Below stairs, sir.", "deflect",
                                                              "Search the cellar")
    assert await e.voice_inputs("A", t) == (1.0, 0)                    # nothing retrieved, nothing broken
    await e.record_voice("A", "t_1001", {"voice_id": "v", "model": "m", "stability": 0.8, "style": 0.1})


async def test_retry_reuses_the_same_trace():
    e = engine(modules={"turn": turn_mod, "context": ctx_mod})
    prep = TurnPrep(True, message="q")
    await e.begin_turn("A", "butler", "t_2", "interrogate", "first", prep)
    await e.begin_turn("A", "butler", "t_2", "interrogate", "second", prep)
    assert len(e.traces.traces) == 1


async def test_missing_engine_pieces_say_so():
    e = engine()
    with pytest.raises(EngineNotReady):
        await e.prepare_interrogate("A", "butler", "q", [])
    with pytest.raises(EngineNotReady):
        await e.use_offer("A", "a_A_x")
    with pytest.raises(EngineNotReady):
        await e.generate(None)                                          # no local runner yet
    assert await e.is_eliminated("A") is False


async def test_engine_results_are_mapped_when_modules_exist():
    calls = {}

    async def use_offer(store, traces, scope, offer_id):
        calls["use_offer"] = (offer_id, scope.team_id)
        if offer_id == "a_B_gone":
            raise ctx_mod.TurnError("offer_not_found")
        return NS(status="performed", message="You search the cellar.", revealed=("ev_note",))

    async def trigger_event(store, case, event_id):
        return NS(notify_teams=["A"], event=NS(notice="The gardener recants."), replaced=[])

    def prepare_interrogate(store, case, scope, suspect_id, question, recent):      # sync works too
        calls["scope"] = scope
        return NS(ok=True, error=None, message="TURN MESSAGE", prompt_version="suspect_v1", fresh=[NS(id="m_1")])

    e = engine(engine_case=object(), modules={
        "actions": NS(use_offer=use_offer), "context": ctx_mod,
        "events": NS(trigger_event=trigger_event),
        "turn": NS(prepare_interrogate=prepare_interrogate),
    })
    r = await e.use_offer("A", "a_A_search_cellar_m_1")
    assert r.status == "used" and r.found[0].evidence.name == "The gardener's note"
    assert calls["use_offer"] == ("a_A_search_cellar_m_1", "A")          # clicker's team, from the role
    gone = await e.use_offer("B", "a_B_gone")
    assert gone.status == "not_found" and "a_B_gone" not in gone.message
    ev = await e.trigger_event("gardener_recants")
    assert ev.notify_teams == ("A",) and ev.notice == "The gardener recants."
    with pytest.raises(KeyError):
        await e.trigger_event("not_an_event")
    prep = await e.prepare_interrogate("B", "butler", "q", [])
    assert prep.ok and prep.message == "TURN MESSAGE" and prep.fresh == ("m_1",)
    assert calls["scope"] == Scope(case_id="case_manor", team_id="B")


def test_local_mode_needs_atlas_settings(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    gaps = Settings(engine_mode="local").missing()
    assert "MONGODB_URI" in gaps and "DB_NAME" in gaps
