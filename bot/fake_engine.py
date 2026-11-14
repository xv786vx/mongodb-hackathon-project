"""ENGINE_MODE=fake: an in-memory stand-in with the real case's names, so the bot can be built and rehearsed
before the real engine is wired in. It reproduces the core demo:

    /discover A ev_note   /present ev_note butler   (Team A now holds a live lead)
    both teams ask the butler about the cellar      -> only Team A is offered "Search the cellar"
    /event gardener_recants                          -> the lead dies; the old button is refused
"""
from __future__ import annotations

from bot.engine_api import (AccusationResult, CaseInfo, Evidence, EventResult, Found, Named, OfferResult,
                            Prepared, SearchResult, Suspect, Trace, TurnPrep)

EVIDENCE = {
    "ev_note": Evidence("ev_note", "The gardener's note",
                        "He saw the butler go down to the wine cellar after midnight."),
    "ev_glove": Evidence("ev_glove", "A bloody glove", "Hidden behind the wine racks."),
}
ROOM_EVIDENCE = {"greenhouse": "ev_note"}                   # /search greenhouse finds the note
TRUTH = ("butler", "fire_poker", "map_room")                # fake only; the real truth stays in game.py
EVENT_NOTICE = "The gardener recants: he now says he saw no one that night."

CASE = CaseInfo(
    title="Death at Halloway Manor",
    intro="Lord Ambrose Halloway is found dead in his map room around 1am.",
    suspects=(Suspect("butler", "Edmund Thorne", "the butler"), Suspect("cook", "Marta Bell", "the cook"),
              Suspect("gardener", "Silas Reed", "the gardener"), Suspect("niece", "Clara Vance", "the niece")),
    open_rooms=tuple(Named(i, n) for i, n in [("map_room", "the map room"), ("pantry", "the pantry"),
                                              ("scullery", "the scullery"), ("greenhouse", "the greenhouse"),
                                              ("cloakroom", "the cloakroom")]),
    weapons=tuple(Named(i, n) for i, n in [("fire_poker", "the fire poker"), ("garden_shears", "the garden shears"),
                                           ("letter_opener", "the letter opener"),
                                           ("sleeping_draught", "the sleeping draught")]),
    events=(Named("gardener_recants", "The gardener recants"),),
    evidence=tuple(Named(e.id, e.name) for e in EVIDENCE.values()),
)


class FakeEngine:
    def __init__(self) -> None:
        self.traces: dict[tuple[str, str], Trace] = {}
        self.history: dict[tuple[str, str], list[dict]] = {}
        self.findings: dict[str, dict[str, bool]] = {}       # team -> evidence id -> withdrawn?
        self.live_leads: set[str] = set()                     # teams whose world holds the supporting memory
        self.offers: dict[str, str] = {}
        self.accused: set[str] = set()
        self.eliminated: set[str] = set()

    # -- case and notebook ----------------------------------------------------------------------
    def case_info(self):
        return CASE

    async def team_findings(self, team_id):
        return [EVIDENCE[e] for e, withdrawn in self.findings.get(team_id, {}).items() if not withdrawn]

    async def discover(self, team_id, evidence_id):
        if evidence_id not in EVIDENCE:
            raise KeyError(evidence_id)
        mine = self.findings.setdefault(team_id, {})
        new = evidence_id not in mine
        mine.setdefault(evidence_id, False)
        return Found(EVIDENCE[evidence_id], new=new, withdrawn=mine[evidence_id])

    async def search_room(self, team_id, room_id):
        if room_id == "wine_cellar":
            return SearchResult(False, room_id, error="That room is locked.")
        if room_id not in {r.id for r in CASE.open_rooms}:
            return SearchResult(False, room_id, error="No such room.")
        ev = ROOM_EVIDENCE.get(room_id)
        found = (await self.discover(team_id, ev),) if ev else ()
        return SearchResult(True, room_id, found)

    async def notebook(self, team_id, query):
        rows = [f"{e.name}: {e.text}" for e in await self.team_findings(team_id)]
        return [r for r in rows if query.lower() in r.lower()] if query else rows

    # -- turns ----------------------------------------------------------------------------------
    async def recent_turns(self, team_id, suspect_id):
        return self.history.get((team_id, suspect_id), [])[-3:]

    async def prepare_interrogate(self, team_id, suspect_id, question, recent):
        return TurnPrep(True, message=question, prompt_version="fake_v0")

    async def prepare_present(self, team_id, suspect_id, evidence_id, question, recent):
        if evidence_id not in {e.id for e in await self.team_findings(team_id)}:
            return TurnPrep(False, error="Your team hasn't found that evidence.")
        if suspect_id == "butler" and evidence_id == "ev_note":
            self.live_leads.add(team_id)                      # the evidence memory carries the lead
        return TurnPrep(True, message=question or f"The detectives show you {EVIDENCE[evidence_id].name}.",
                        prompt_version="fake_v0")

    async def begin_turn(self, team_id, suspect_id, turn_id, command, question, prep):
        key = (team_id, turn_id)
        if key not in self.traces:                            # a retry finds the same trace (rule 16)
            self.traces[key] = Trace(turn_id, "pending", suspect_id)
        return Prepared(turn_id, "case_manor", team_id, suspect_id, command, prep.message,
                        prep.prompt_version, prep.fresh)

    async def generate(self, p: Prepared) -> None:
        if self.traces[(p.team_id, p.turn_id)].status == "complete":
            return
        text = p.message.lower()
        offer_id = label = None
        if p.suspect_id == "butler" and ("cellar" in text or "midnight" in text) and p.team_id in self.live_leads:
            offer_id = f"a_{p.team_id}_search_cellar_m_093"
            self.offers.setdefault(offer_id, "offered")
            label = "Search the cellar"
            reply = "I was... below stairs, sir. Though the cellar door was unlatched, I will grant you."
        else:
            reply = "I was below stairs after midnight, sir. Ask anyone."
        self.traces[(p.team_id, p.turn_id)] = Trace(p.turn_id, "complete", p.suspect_id, reply, offer_id, label,
                                                    decision="deflect")
        self.history.setdefault((p.team_id, p.suspect_id), []).append({"input": p.message, "reply": reply})

    async def get_trace(self, team_id, turn_id):
        return self.traces.get((team_id, turn_id))

    # -- leads, events, game --------------------------------------------------------------------
    async def use_offer(self, team_id, offer_id):
        if offer_id.split("_")[1] != team_id:
            return OfferResult("forbidden", "That lead belongs to another team.")
        status = self.offers.get(offer_id)
        if status is None:
            return OfferResult("not_found", "That lead no longer exists.")
        if team_id not in self.live_leads:
            self.offers[offer_id] = "rejected"
            return OfferResult("rejected", "That lead no longer holds.")
        if status == "used":
            return OfferResult("already_used", "Your team has already followed that lead.")
        self.offers[offer_id] = "used"
        return OfferResult("used", "You search the cellar.", (await self.discover(team_id, "ev_glove"),))

    async def trigger_event(self, event_id):
        if event_id != "gardener_recants":
            raise KeyError(event_id)
        teams = tuple(sorted(self.live_leads))
        for t in teams:
            self.findings.get(t, {}).update({"ev_note": True})   # withdrawn: cannot be presented again
        self.live_leads.clear()
        return EventResult(teams, EVENT_NOTICE)

    async def accuse(self, team_id, who, weapon, room):
        if team_id in self.accused:
            return AccusationResult("already_accused", "Your team has already made an accusation.")
        if who not in {s.id for s in CASE.suspects} or weapon not in {w.id for w in CASE.weapons} \
                or room not in {r.id for r in CASE.open_rooms} | {"wine_cellar"}:
            return AccusationResult("invalid", "That is not a valid accusation.")
        self.accused.add(team_id)
        if (who, weapon, room) == TRUTH:
            return AccusationResult("solved", f"Team {team_id} has solved the case!")
        self.eliminated.add(team_id)
        return AccusationResult("eliminated", f"Team {team_id} made an accusation. It was wrong.")

    async def run_round(self, team_id, round_no):
        return 0

    async def is_eliminated(self, team_id):
        return team_id in self.eliminated

    async def voice_inputs(self, team_id, trace):
        return 1.0, 0

    async def record_voice(self, team_id, turn_id, voice):
        self.voices = getattr(self, "voices", {})
        self.voices[(team_id, turn_id)] = voice
