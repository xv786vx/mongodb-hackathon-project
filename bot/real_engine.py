"""The real engine behind the bot (ENGINE_MODE=local or hosted).

Two halves:

* Live now, on person 1's memory layer: the case from Atlas, traces (begin/get/voice), voice inputs, recent turns
  and the expiry watcher.
* Waiting on person 3's mystery_core.engine and person 2's local runner: turn preparation, offers, events, search,
  discovery, notebook, accusation and gossip. Those methods are written against the agreed signatures
  (CONTRACTS.md) and raise EngineNotReady until the modules exist, so the bot says so instead of crashing.

The bot never reads private case fields: personas, confessions, fallback lies, seed memories and truth stay out of
CaseInfo (get_case already drops truth).
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
from typing import Any

from bot.config import Settings as BotSettings
from bot.engine_api import (AccusationResult, CaseInfo, Evidence, EventResult, Found, Named, OfferResult,
                            Prepared, SearchResult, Suspect, Trace, TurnPrep)
from mystery_core.memory import Scope, suspect_viewer

log = logging.getLogger("bot.real_engine")
RECENT_TURNS = 3


class EngineNotReady(RuntimeError):
    """A game step whose engine code has not landed yet."""


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def case_info_from_doc(doc: dict[str, Any]) -> CaseInfo:
    """Public, display-only view of the case. Takes ids, names, labels and voice ids; nothing else."""
    return CaseInfo(
        title=doc.get("title", ""),
        intro=doc.get("intro", ""),
        suspects=tuple(Suspect(s["id"], s.get("name", s["id"]), s.get("role", ""), s.get("voice_id"))
                       for s in doc.get("suspects", [])),
        open_rooms=tuple(Named(r["id"], r.get("name", r["id"])) for r in doc.get("rooms", []) if r.get("open")),
        weapons=tuple(Named(w["id"], w.get("name", w["id"])) if isinstance(w, dict) else Named(w, w)
                      for w in doc.get("weapons", [])),
        events=tuple(Named(e["id"], e.get("label", e["id"])) for e in doc.get("events", [])),
        evidence=tuple(Named(e["id"], e.get("name", e["id"])) for e in doc.get("evidence", [])),
    )


def _evidence(e: Any) -> Evidence:
    return Evidence(getattr(e, "id", ""), getattr(e, "name", ""), getattr(e, "text", "") or "")


def _found(d: Any) -> Found:
    """Person 3's discovery items: .evidence, .new, .withdrawn."""
    return Found(_evidence(getattr(d, "evidence", d)), bool(getattr(d, "new", True)),
                 bool(getattr(d, "withdrawn", False)))


def _trace(t: Any) -> Trace:
    return Trace(turn_id=t.id, status=t.status, suspect_id=t.suspect_id, reply=t.reply or "",
                 offer_id=t.offer_id, offer_label=t.offer_label, decision=t.decision or "")


class RealEngine:
    def __init__(self, cfg: BotSettings, store, traces, case_doc: dict[str, Any], *, db=None,
                 engine_case=None, modules: dict[str, Any] | None = None, runner=None, client=None) -> None:
        self.cfg = cfg
        self.store, self.traces, self.db, self.client = store, traces, db, client
        self._info = case_info_from_doc(case_doc)
        # Evidence text, only ever shown to the team that just revealed that item.
        self._evidence = {e["id"]: Evidence(e["id"], e.get("name", e["id"]), e.get("text", ""))
                          for e in case_doc.get("evidence", [])}
        self.case = engine_case                       # person 3's loaded case object, once available
        self.m = modules or {}                        # engine modules, once available
        self.runner = runner                          # person 2's local runner module, once available
        self._runtime = None

    # ---- construction ----------------------------------------------------------------------------
    @classmethod
    async def open(cls, cfg: BotSettings) -> "RealEngine":
        from pymongo import AsyncMongoClient

        from mystery_core.memory import MemoryStore, TraceStore
        from mystery_core.memory.config import Settings

        settings = Settings.from_env()
        client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True, appname="mystery-bot")
        store = await MemoryStore.open(settings, client)
        db = client[settings.db_name]
        doc = await store.get_case(cfg.case_id)
        if not doc:
            raise SystemExit(f"Case {cfg.case_id!r} not found in database {settings.db_name!r}.")

        modules = {n: _try_import(f"mystery_core.engine.{n}") for n in ("case", "turn", "context", "actions",
                                                                          "events", "game", "gossip")}
        modules = {n: m for n, m in modules.items() if m is not None}
        engine_case = None
        if "case" in modules:
            engine_case = await _maybe_await(modules["case"].load_case(store, cfg.case_id))
        runner = _try_import("bot.local_runner")
        log.info("real engine: case %s, engine modules %s, local runner %s",
                 cfg.case_id, sorted(modules) or "none", "yes" if runner else "no")
        return cls(cfg, store, TraceStore(db), doc, db=db, engine_case=engine_case, modules=modules, runner=runner,
                   client=client)

    def _scope(self, team_id: str) -> Scope:
        return Scope(case_id=self.cfg.case_id, team_id=team_id)     # team always from the Discord role

    def _need(self, name: str):
        """A game-rules module that also needs person 3's loaded case."""
        mod = self.m.get(name)
        if mod is None or self.case is None:
            raise EngineNotReady(f"mystery_core.engine.{name}")
        return mod

    def _need_mod(self, name: str):
        mod = self.m.get(name)
        if mod is None:
            raise EngineNotReady(f"mystery_core.engine.{name}")
        return mod

    # ---- live now (person 1) ---------------------------------------------------------------------
    def case_info(self) -> CaseInfo:
        return self._info

    def _turn_service(self):
        turn = self.m.get("turn")
        if turn is None or not hasattr(turn, "TurnService"):
            raise EngineNotReady("mystery_core.engine.turn.TurnService")
        return turn.TurnService(self.store, self.traces, model=os.environ.get("LLM_MODEL", ""))

    async def begin_turn(self, team_id, suspect_id, turn_id, command, question, prep: TurnPrep) -> Prepared:
        """Create the pending trace, then let person 2's TurnService build the agent's prompt from it.

        The prompt is rebuilt from the immutable trace input and freshly fetched, scoped memories; `prep.fresh`
        (the /present evidence ids) crosses search-index lag this way.
        """
        mode = self.cfg.engine_mode if self.cfg.engine_mode in ("hosted", "local") else None
        scope = self._scope(team_id)
        await self.traces.begin_turn(scope, turn_id, suspect_viewer(suspect_id), command, question, engine_mode=mode)
        ctx = self._need_mod("context").TurnContext(scope, suspect_id, turn_id, command)
        native = await self._turn_service().prepare(ctx, fresh_memory_ids=list(prep.fresh))
        return Prepared(turn_id, self.cfg.case_id, team_id, suspect_id, command, native.message,
                        prep.prompt_version, prep.fresh, native=native)

    async def get_trace(self, team_id, turn_id) -> Trace | None:
        t = await self.traces.get_trace(self._scope(team_id), turn_id)
        return _trace(t) if t is not None else None

    async def recent_turns(self, team_id, suspect_id) -> list[dict]:
        if self.db is None:
            return []
        cur = self.db["traces"].find(
            {"case_id": self.cfg.case_id, "team_id": team_id, "actor": suspect_viewer(suspect_id),
             "status": "complete", "replay_of": None},
            {"input": 1, "reply": 1}).sort("ts", -1).limit(RECENT_TURNS)
        rows = [{"input": d.get("input", ""), "reply": d.get("reply", "")} async for d in cur]
        return list(reversed(rows))

    async def voice_inputs(self, team_id, trace: Trace) -> tuple[float, int]:
        scope, viewer = self._scope(team_id), suspect_viewer(trace.suspect_id)
        full = await self.traces.get_trace(scope, trace.turn_id)
        mems = await self.store.fetch(scope, viewer, full.retrieved if full else [])
        confidence = sum(m.confidence for m in mems) / len(mems) if mems else 1.0
        breaks = await self.store.count_superseded(scope, viewer, kind="cover_story")
        return confidence, breaks

    async def record_voice(self, team_id, turn_id, voice: dict) -> None:
        await self.traces.set_voice(self._scope(team_id), turn_id, voice)

    async def watch_expiries(self, on_expired) -> None:
        await self.store.watch_expiries(on_expired)

    # ---- generation (person 2) ---------------------------------------------------------------------
    async def generate(self, prepared: Prepared) -> None:
        """Local mode only; hosted mode is invoke.py, called from turns.generate. Both write the same trace."""
        if self.runner is None or prepared.native is None:
            raise EngineNotReady("bot.local_runner")
        await self.runner.run(prepared.native, runtime=self._local_runtime())

    def _local_runtime(self):
        """Reuse the bot's own store, traces and connection instead of opening a second one."""
        if self._runtime is None and self.client is not None:
            from suspect_agent.runtime import Runtime
            self._runtime = Runtime(self.store, self.traces, self._turn_service(), self.client)
        return self._runtime

    # ---- waiting on person 3 -------------------------------------------------------------------------
    async def prepare_interrogate(self, team_id, suspect_id, question, recent) -> TurnPrep:
        turn = self._need("turn")
        p = await _maybe_await(turn.prepare_interrogate(self.store, self.case, self._scope(team_id), suspect_id,
                                                        question, recent))
        return self._prep(p)

    async def prepare_present(self, team_id, suspect_id, evidence_id, question, recent) -> TurnPrep:
        turn = self._need("turn")
        p = await _maybe_await(turn.prepare_present(self.store, self.case, self._scope(team_id), suspect_id,
                                                    evidence_id, question=question, recent_turns=tuple(recent),
                                                    llm=None))            # all model calls go through the agent
        return self._prep(p)

    @staticmethod
    def _prep(p: Any) -> TurnPrep:
        # The engine's `fresh` is a list of Memory objects; the trace's `written` wants their ids.
        fresh = tuple(m if isinstance(m, str) else m.id for m in (getattr(p, "fresh", ()) or ()))
        return TurnPrep(ok=bool(p.ok), error=getattr(p, "error", "") or "", message=getattr(p, "message", "") or "",
                        prompt_version=getattr(p, "prompt_version", "") or "", fresh=fresh)

    async def team_findings(self, team_id) -> list[Evidence]:
        actions = self._need("actions")
        found = await _maybe_await(actions.team_findings(self.store, self.case, self._scope(team_id)))
        return [_evidence(e) for e in found]

    # person 2's use_offer statuses -> the bot's
    _CLICK = {"performed": "used", "used": "already_used", "rejected": "rejected"}

    async def use_offer(self, team_id, offer_id) -> OfferResult:
        """Click: scope from the clicker's role. Consuming the offer and writing the finding commit together."""
        actions = self._need_mod("actions")
        try:
            r = await actions.use_offer(self.store, self.traces, self._scope(team_id), offer_id)
        except self._need_mod("context").TurnError:
            # unknown offer, or another team's: indistinguishable on purpose
            return OfferResult("not_found", "That lead no longer exists.")
        found = tuple(Found(self._evidence[e]) for e in (getattr(r, "revealed", ()) or ()) if e in self._evidence)
        return OfferResult(self._CLICK.get(r.status, r.status), getattr(r, "message", "") or "", found)

    async def trigger_event(self, event_id) -> EventResult:
        events = self._need("events")
        if event_id not in {e.id for e in self._info.events}:
            raise KeyError(event_id)
        r = await _maybe_await(events.trigger_event(self.store, self.case, event_id))
        return EventResult(tuple(r.notify_teams), r.event.notice)

    async def search_room(self, team_id, room_id) -> SearchResult:
        actions = self._need("actions")
        r = await _maybe_await(actions.search_room(self.store, self.case, self._scope(team_id), room_id))
        return SearchResult(bool(r.ok), getattr(r, "room", room_id) or room_id,
                            tuple(_found(d) for d in (getattr(r, "found", None) or ())), getattr(r, "error", "") or "")

    async def discover(self, team_id, evidence_id) -> Found:
        actions = self._need("actions")
        if evidence_id not in {e.id for e in self._info.evidence}:
            raise KeyError(evidence_id)
        return _found(await _maybe_await(actions.discover(self.store, self.case, self._scope(team_id), evidence_id)))

    async def notebook(self, team_id, query) -> list[str]:
        actions = self._need("actions")
        mems = await _maybe_await(actions.notebook(self.store, self._scope(team_id), query))
        return [m.text for m in mems]

    async def accuse(self, team_id, who, weapon, room) -> AccusationResult:
        game = self._need("game")
        r = await _maybe_await(game.accuse(self.store, self.case, self._scope(team_id), who, weapon, room))
        return AccusationResult(r.status, getattr(r, "message", "") or "")

    async def is_eliminated(self, team_id) -> bool:
        game = self.m.get("game")
        if game is None:
            return False                              # nobody can be eliminated before accusations exist
        return bool(await _maybe_await(game.is_eliminated(self.store, self._scope(team_id))))

    async def run_round(self, team_id, round_no) -> int:
        gossip = self._need("gossip")
        written = await _maybe_await(gossip.run_round(self.store, self.case, self._scope(team_id), round_no))
        return len(written or ())
