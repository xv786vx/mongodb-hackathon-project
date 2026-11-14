"""ENGINE_MODE=fake: canned traces so the bot can be built before the real engine exists.

Behaves like the core demo: only Team A holds the "gardener's note" lead, so only Team A is
offered "Search the cellar" when asking the butler about the cellar. /event kills the lead.
"""
from __future__ import annotations

from bot.engine_api import OfferResult, Prepared, Trace

SUSPECTS = [("butler", "Mr. Crane"), ("cook", "Mrs. Pell"), ("gardener", "Old Tobias")]


class FakeEngine:
    def __init__(self) -> None:
        self.traces: dict[str, Trace] = {}
        self.offers: dict[str, str] = {}              # offer_id -> status
        self.live_leads: set[str] = {"A"}             # teams whose world holds the supporting memory

    def suspects(self, case_id):
        return SUSPECTS

    def begin_turn(self, case_id, team_id, suspect_id, turn_id, command, question):
        if turn_id not in self.traces:                # a retry finds the same trace (rule 16)
            self.traces[turn_id] = Trace(turn_id, "pending", suspect_id)
        return Prepared(turn_id, case_id, team_id, suspect_id, command, question)

    def generate(self, p: Prepared) -> None:
        if self.traces[p.turn_id].status == "complete":
            return
        wants_cellar = "cellar" in p.message.lower() or "midnight" in p.message.lower()
        offer_id = None
        label = None
        if p.suspect_id == "butler" and wants_cellar and p.team_id in self.live_leads:
            offer_id = f"a_{p.team_id}_search_cellar_m_093"
            self.offers.setdefault(offer_id, "offered")
            label = "Search the cellar"
            reply = "I was... below stairs, sir. Though the cellar door was unlatched, I will grant you."
        else:
            reply = "I was below stairs after midnight, sir. Ask anyone."
        self.traces[p.turn_id] = Trace(p.turn_id, "complete", p.suspect_id, reply, offer_id, label,
                                       decision="deflect")

    def get_trace(self, turn_id):
        return self.traces.get(turn_id)

    def use_offer(self, case_id, team_id, offer_id):
        if offer_id.split("_")[1] != team_id:
            return OfferResult("wrong_team")
        status = self.offers.get(offer_id)
        if status is None or team_id not in self.live_leads:
            self.offers[offer_id] = "rejected"
            return OfferResult("lead_gone", "That lead no longer holds.")
        if status == "used":
            return OfferResult("already_used", "Your team has already done this.")
        self.offers[offer_id] = "used"
        return OfferResult("used", "You search the cellar and find a bloody glove.")

    def trigger_event(self, case_id, event_id):
        if event_id != "gardener_recants":
            return []
        teams = sorted(self.live_leads)
        self.live_leads.clear()
        return teams
