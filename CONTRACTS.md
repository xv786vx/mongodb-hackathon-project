# Contracts

Shared interfaces between the four roles. Changing anything here: update this file and tell the team before merging.

## Memory layer (Person 1)

Code: `agents/suspect/src/mystery_core/memory/`. Install once with `pip install -e agents/suspect`.

### Decisions (defaults, change only by agreement)

| Decision | Choice |
|---|---|
| Embeddings | Voyage `voyage-4-lite`, 1024 dims (`EMBEDDING_DIMS` must equal `memory_index.numDimensions`). `EMBEDDING_MODEL=fake` uses a deterministic offline embedder. The hosted and local suspect runtime requires the agreed Voyage model. |
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

Entrypoint: `agents/suspect/agent.yaml` -> `suspect_agent.graph:app`. Built-in memory is off.
Both tools are local async tools and expose no scope arguments:

```python
async def search_memories(query: str) -> str: ...
async def submit_reply(decision: str, reply: str, proposed_action: str | None = None,
                       supporting_memory_id: str | None = None) -> str: ...
```

Tools read `case-id`, `team-id`, `suspect-id`, `turn-id`, `command` from
`get_current_custom_headers()`. The bot sends these using `X-Mdb-Agent-Engine-Custom-`.
Missing/invalid headers, unknown suspects and a mismatched trace actor/command fail closed.
The bot creates the pending trace **before** invocation. `begin_turn()` returns a Trace, not a prepared message.

`TurnService(store, traces, model=...).prepare(ctx, fresh_memory_ids=())` returns a `PreparedTurn`.
It records only live, scoped fresh IDs; the agent rebuilds the prompt from the immutable trace input,
the current persona and freshly fetched memories. It never serializes the entire case or prior chat history.
`search_memories` returns authorized IDs, text, kind, source, confidence and unlocks, and appends IDs to the trace.

`submit_reply` uses `ReplyProposal`, removes bracketed tags, rechecks the confession gate and action support,
and commits two deterministic memories (suspect interaction + team finding), the optional offer, and
the terminal trace in one `store.atomic_turn(traces, callback)` transaction. The callback receives
session-bound memory/trace stores. A completed retry returns the existing trace before writing anything.
Replays write only a replay trace; they create no gameplay memories or offers.

`bot.local_runner.run(prepared)` returns the same validated Trace using the direct model fallback.
`suspect_agent.client.invoke(prepared)` sends `{"message": prepared.message}`, the five scope headers,
the pinned preview Accept header, and a 60-second server wait budget. It omits `X-Session-ID`.
The client exchanges bot-only `AGENT_ENGINE_CLIENT_ID`/`AGENT_ENGINE_CLIENT_SECRET` for a bearer token.
Invocation responses are metadata, never the bot's dialogue source. A timeout may leave the server running;
inspect the same scoped trace and preserve the turn ID.

`mystery_core.engine.actions.use_offer(store, traces, clicker_scope, offer_id)` is the click-time helper.
It verifies the current clicker's scope and re-fetches the supporting memory; consuming an offer and
writing its finding commit together. The Discord developer must supply identity from the clicker's role,
keep the private-channel checks, and deduplicate Discord posts separately (the agent never posts to Discord).

`engine.evidence.present_evidence` authorizes the team's live notebook and returns a live scoped evidence memory.
It does not implement the game's LLM contradiction step. The game orchestrator still owns that step before invocation.
`engine.events.apply_event(..., is_host=True)` is the host-only route to cross-world supersession.

Canonical seed verification: `python -m scripts.seed --case case_manor --teams A B --verify-model`.
For a confirmed model mismatch, use `--reembed --reembed-worlds --verify-model`; this changes vectors,
not memory text, access rules, status or lineage. Verification re-embeds with the actual configured model
and checks the case clues and active canonical team copies against it.

Workspace registered for this implementation: `ws-6af8c0b85e1f510db9b7a4f2` in project
`6af88435eee09d5d80696a95`, Agent Engine organization `6aa1ed66371c6468b4a49ac1`.
The Atlas organization ID is different; do not use it for CLI workspace context.

## Case JSON (Person 3)

_TODO. Memory layer needs: `_id` starting `case_`, `seed_memories[]` with `id`, `text`, `kind`, `known_by`, optional `unlocks`, `confidence`. See `tests/fixtures/mini_case.json`._

## Discord (Person 4)

_TODO: slash command list, button `custom_id` format._
