# Contracts

Shared interfaces between the four roles. Changing anything here: update this file and tell the team before merging.

## Memory layer (Person 1)

Code: `agents/suspect/src/mystery_core/memory/`. Install once with `pip install -e agents/suspect`.

### Decisions (defaults, change only by agreement)

| Decision | Choice |
|---|---|
| Embeddings | Voyage `voyage-3.5`, 1024 dims (`EMBEDDING_DIMS` must equal `memory_index.numDimensions`). `EMBEDDING_MODEL=fake` uses a deterministic offline embedder. |
| Store API | **async** (PyMongo `AsyncMongoClient`). Person 2: if Agent Engine tools can't be `async def`, say so and a sync wrapper will be added. |
| Case JSON | Every `seed_memories[]` item has a unique `id` (becomes `source.ref`, used by world events). Seed memories may carry `unlocks`. |

### Models (`mystery_core.memory.models`)

`Scope(case_id, team_id)` (frozen) · `Source(type, ref)` · `NewMemory` · `Memory` · `Trace` · `Offer` · `AuditEvent`.
Shapes follow ARCHITECTURE.md section 3. Documents store `id` as `_id`; use `Model.model_validate(doc)` and `model.to_doc()`.

Viewers are strings: `suspect:<id>` or `team:<id>` (`suspect_viewer()`, `team_viewer()`, `validate_viewer()`).

### Store API (`MemoryStoreAPI`; implemented by `MemoryStore` and `FakeStore`)

```python
store = await MemoryStore.open(Settings.from_env())        # Atlas; loads the policy from `policies`
store = FakeStore(cases={"case_x": case_dict})             # in-memory, for unit tests and stubs

await store.retrieve(scope, viewer, query, k=5, kinds=None, mask=None) -> list[Memory]
await store.fetch(scope, viewer, ids) -> list[Memory]       # same filter; missing = superseded/expired/out of scope
await store.write(scope, memories: list[NewMemory], *, dedup=True) -> list[Memory]
await store.supersede(scope, old_id, new: NewMemory, reason) -> Memory
await store.supersede_by_ref(case_id, ref, replacement_text, event_id, reason) -> list[Memory]   # events.py only
await store.forget(scope, viewer=None) -> int
await store.seed_world(case_id, team_id) -> int
await store.count_superseded(scope, viewer, kind="cover_story") -> int   # crack gate (rule 13) + voice
await store.watch_expiries(on_expired, worker_id="expiry_notices")       # runs forever; bot starts it as a task
await store.get_case(case_id) -> dict | None     # case WITHOUT truth and seed embeddings; cacheable per case_id
await store.get_truth(case_id) -> dict | None    # {killer, weapon, room}; engine/game.py ONLY (rule 5)
store.policy -> Policy                           # loaded from the database (policy as data)

# GameStore functions for the engine (Person 3)
await store.find_by_ref(scope, viewer, ref, kinds=None) -> list[Memory]   # active, scoped, source.ref == ref
await store.list_by_viewer(scope, viewer, kinds=None) -> list[Memory]     # active, scoped, oldest first
await store.get_offer(offer_id) -> Offer | None
await store.insert_offer(offer) -> Offer           # returns the existing offer on a duplicate _id
await store.transition_offer(offer_id, from_status, to_status) -> bool   # one atomic update; True only for the winner
await store.action_used(scope, action) -> bool
await store.record_accusation(scope, result: dict) -> bool   # one per team; False if already recorded
await store.get_accusation(scope) -> dict | None
await store.log_audit(event: AuditEvent)            # IDs only; idempotent on event.id
```

`Offer.held_by` is the suspect viewer holding the supporting memory; a click re-checks with
`store.fetch(scope, offer.held_by, [offer.supported_by])`.

Differences from ARCHITECTURE.md section 4, on purpose:
- `supersede` takes `scope`: the old memory must be in that world, so a caller can't supersede across worlds by id.
- `supersede_by_ref` takes `event_id`: replacements get `source = {type: "event", ref: event_id}`.
- `write` honours `NewMemory.id`: writing the same deterministic id twice returns the existing memory (retry-safe).

Guarantees:
- Every read is filtered by `case_id`, `team_id`, `known_by`, `status: active`, `expires_at > now`, in `$vectorSearch` and again in `$match`.
- `retrieve`/`fetch` never return embeddings. `score` is set by `retrieve` only.
- Replacements never inherit `unlocks`. A superseded memory disappears from `retrieve`/`fetch` immediately.
- Search index lag: don't write then retrieve. Use the memories `write` returns.
- `supersede` is one transaction (old -> superseded, replacement inserted, audit). Repeat returns the same replacement;
  `KeyError` if the memory isn't in this world, `ValueError` if it was superseded by something else.
- `supersede_by_ref` skips worlds already superseded, so a repeat returns `[]`.
- Seed memories must have `id`, `text`, `kind` and `known_by`; `seed_world` raises a `ValueError` listing every problem.

Status: everything above is live on Atlas and in `FakeStore`. `pytest -m atlas tests/test_isolation.py` checks it on Atlas.

### Traces (`TraceStoreAPI`; implemented by `TraceStore(db)` and `FakeTraceStore()`)

```python
traces = TraceStore(client[db_name])

# bot, at the start of a command (after defer). Find-or-create: a retry returns the same trace, input unchanged.
await traces.begin_turn(scope, turn_id, actor, command, input, *, engine_mode=None, replay_of=None, masked=()) -> Trace
# bot, after invoke. None for an unknown id OR another team's trace.
await traces.get_trace(scope, turn_id) -> Trace | None
# agent tools (Person 2)
await traces.add_retrieved(scope, turn_id, memory_ids)       # only while pending; idempotent
await traces.complete_turn(scope, turn_id, *, decision, reply, written=(), offer_id=None, offer_label=None,
                           offer_rejected_reason=None, model=None, prompt_version=None, execution_id=None) -> Trace
await traces.reject_turn(scope, turn_id, reason) -> Trace
# bot, after the clip
await traces.set_voice(scope, turn_id, voice: dict)
```

- `pending -> complete` or `pending -> rejected`, nothing else. `complete_turn` on a complete trace returns it unchanged
  (retry-safe `submit_reply`); on a rejected or missing trace it raises `TraceConflict`.
- Post only when `trace.status == "complete"`. Use `trace.reply`, `trace.decision`, `trace.offer_id`, `trace.offer_label`.
- `trace.actor` is `"suspect:<id>"`; `trace.suspect_id` gives `<id>`.
- `offer_label` is display text copied from `case.actions[].label` by `submit_reply`. The click handler still re-checks the offer and the memory.

### Voice inputs (Person 4)

```python
mems = await store.fetch(scope, trace.actor, trace.retrieved)
mean_confidence = sum(m.confidence for m in mems) / len(mems) if mems else 1.0
breaks = await store.count_superseded(scope, trace.actor, kind="cover_story")
```

### Expiry notices (Person 4)

```python
async def on_expired(n: ExpiryNotice) -> None:   # n.case_id, n.team_id, n.memory_id, n.kind
    ...post a generic message (e.g. "A rumour has faded.") in team n.team_id's channel...

asyncio.create_task(store.watch_expiries(on_expired))
```

The notice carries IDs and kind only, never memory text: a rumour belongs to a suspect, so its text must not reach the team.
Deletes by `forget()` are not reported (only memories whose `expires_at` has passed). The watcher writes an `expire`
audit event and stores its resume token in `worker_state`, so a restart continues where it stopped.

### Deterministic IDs (`mystery_core.memory.ids`)

| Thing | Helper | Example |
|---|---|---|
| Turn / trace | `turn_id(interaction_id)` | `t_1234567890` |
| Seed copy | `seed_memory_id(case, team, seed_id)` | `m_manor_A_s_butler_alibi` |
| Finding (discovery) | `finding_id(case, team, evidence_id)` | `m_find_manor_A_ev_note` |
| Evidence shown to a suspect | `evidence_memory_id(case, team, suspect, evidence_id)` | `m_ev_manor_A_butler_ev_note` |
| Memory written in a turn | `turn_memory_id(turn_id, label)` | `m_3f2a…` |
| Replacement | `replacement_memory_id(old_id)` | `m_r_9c1d…` |
| Offer | `offer_id(team, action, memory_id)` | `a_A_search_cellar_m_ev_…` |

### Database

`python setup_db.py` (idempotent) creates `memories` (validator, pre-images, TTL), `cases`, `policies`, `traces`, `offers`, `audit`, `worker_state`, the indexes, `memory_index`, and the `mystery_v1` policy.
`python -m scripts.seed --case case_manor --teams A B` embeds the seed memories of a case already in Atlas (sets only
`seed_memories.$.embedding`) and seeds worlds. `--file <case.json>` loads from a file and refuses to overwrite an existing
case without `--replace`.
`python -m scripts.try_retrieve <case_id> <team> <viewer> "<query>"` checks retrieval.

## Agent Engine (Person 2)

_TODO: custom header names, `submit_reply` arguments and writes._

## Case JSON and game engine (Person 3)

### Case document

**Atlas is the only copy of a case.** Cases live in `mystery.cases` (the demo case is `case_manor`). There is no case file in the repo; `tests/engine/fixtures/case_test.json` is a synthetic test case.

Load with `case = await load_case(store, case_id)` (`mystery_core.engine.case`). It calls `store.get_case` and validates the document, so a broken case fails loudly at load time. `truth` is never part of a `Case`; only `engine/game.py` calls `store.get_truth`.

| Field | Shape | Notes |
|---|---|---|
| `_id` | `"case_*"` | |
| `title`, `victim`, `intro` | string | `intro` is safe to post publicly |
| `truth` | `{killer, weapon, room}` (ids) | Read only through `get_truth`, by `engine/game.py` |
| `suspects[]` | `{id, name, role, persona, is_killer, crack_threshold, voice_id, fallback_lies[], confession}` | Exactly one killer. Personas hold no case facts. `fallback_lies` are the killer's next lies when no LLM is used. `confession` (killer only) is what really happened; it is not a seed memory, so search never returns it, and the engine adds it to the turn message only once `crack` is allowed. `is_killer` stays visible in `get_case`: the engine needs it, and it only reaches the killer's own turn message |
| `rooms[]` | `{id, name, open}` | Every closed room must be opened by an action |
| `weapons[]` | `{id, name}` | Objects, so the bot can show names |
| `evidence[]` | `{id, name, room, text, unlocks[], contradicts}` | `contradicts`: `{seed_id: replacement text or null}`. `{}` means it contradicts nothing. Omitted means the optional LLM checker decides |
| `actions[]` | `{id, label, reveals[], opens}` | `label` is the button text |
| `events[]` | `{id, label, ref, replacement, notice, also[]}` | `also: [{ref, replacement}]` lets one event change several memories. Only worlds that held `ref` get the `notice` |
| `seed_memories[]` | `{id, text, known_by[], kind, confidence, unlocks[]}` | `kind` is required: `witnessed` or `cover_story`. `id` becomes `source.ref` in every world |
| `gossip` | `{topics[], pairs_per_round}` | |

Rooms avoid the famous board game's names (AGENTS.md rule 22): map room, pantry, scullery, greenhouse, cloakroom, wine cellar.

### Engine entry points (`mystery_core.engine`)

**All are `async def`**, because the store is async. They take the `store`, a loaded `case` and a `Scope`.

| Caller | Call | Returns |
|---|---|---|
| Bot `/interrogate` | `turn.prepare_interrogate(store, case, scope, suspect_id, question, recent_turns)` | `TurnPrep` |
| Bot `/present` | `turn.prepare_present(store, case, scope, suspect_id, evidence_id, question="", recent_turns=(), llm=None)` | `TurnPrep` |
| Bot `/present` autocomplete | `actions.team_findings(store, case, scope)` | evidence the team holds |
| Bot button click | `actions.use_offer(store, case, offer_id, clicker_team_id)` | `ClickResult`: `used`, `rejected`, `already_used`, `forbidden` or `not_found` |
| Bot `/event` | `events.trigger_event(store, case, event_id)` | `EventResult(event, replaced, notify_teams)` |
| Bot `/search`, `/discover`, `/notebook` | `actions.search_room`, `actions.discover`, `actions.notebook` | |
| Bot `/accuse` | `game.accuse(store, case, scope, who, weapon, room)`, `game.is_eliminated(store, scope)` | `AccusationResult(status, message)` |
| Bot `/round` | `gossip.run_round(store, case, scope, round_no)` per team | rumours written |
| `search_memories` | `turn.format_memories(memories)` | memory ids and `[lead: …]` markers for the model |
| `submit_reply` | `rules.check_decision(store, case, scope, suspect_id, decision)`, then `actions.validate_offer(store, case, scope, suspect_id, turn_id, trace.retrieved, proposed_action, supporting_memory_id)` | `None` or an error for the model; then `OfferDecision(offer, reason)`. Pass `reason` as `traces.complete_turn(..., offer_rejected_reason=...)` |

- `TurnPrep` has `ok`, `message` (the invoke message), `allowed`, `prompt_version`, `fresh` (ids go in the trace's `written`) and `error` (if `ok` is `False`, post it and don't invoke).
- `recent_turns` is the last few completed `Trace`s for this team and suspect.
- `llm` is optional (`async (prompt) -> str`). The demo passes `None`, and the case's declared contradictions and `fallback_lies` are used.
- Policy values come from `store.policy`, which the store loads from the `policies` collection.
- The engine uses only store functions listed in "Store API" above. `GameStore` in `mystery_core/engine/ports.py` is the subset it calls; keep the two in step.

## Discord (Person 4)

_TODO: slash command list, button `custom_id` format._
