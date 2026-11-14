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

## Case JSON (Person 3)

_TODO. Memory layer needs: `_id` starting `case_`, `seed_memories[]` with `id`, `text`, `kind`, `known_by`, optional `unlocks`, `confidence`. See `tests/fixtures/mini_case.json`._

## Discord (Person 4)

Code: `bot/` (Person 4). Identity, posting and voice live here; no game rules.

### Identity

`team_id` comes from the member's Discord **role** (`TEAM_ROLES`). Exactly one team role is required; zero or two is refused.
Replies must land in that team's private channel (`TEAM_CHANNELS`), otherwise the command is refused. `case_id` is `CASE_ID`.
Suspect, room, weapon, evidence and event IDs are validated against the case. Team is never a player argument.
The host is whoever holds `HOST_ROLE_ID`. Refusals are ephemeral.

### Slash commands

| Command | Who | Arguments | Output goes to |
|---|---|---|---|
| `/interrogate` | team | `suspect`, `question` | team channel |
| `/present` | team | `evidence`, `suspect`, `question` (optional) | team channel |
| `/search` | team | `room` (open rooms only) | team channel |
| `/notebook` | team | `query` (optional) | ephemeral |
| `/accuse` | team | `suspect`, `weapon`, `room` | `#case-room` (result only), ephemeral ack |
| `/event` | host | `event_id` | each affected team's channel only |
| `/discover` | host | `team`, `evidence` | ephemeral |
| `/round` | host | none | `#case-room`: "Round N begins." No rumour text |
| `/replay` | host | `trace_id`, `memory_id` | `#host` (not built yet) |
| `/leaktest` | host | none | `#host` (not built yet) |

`/present` evidence autocomplete lists only the team's own findings (`team_findings`), never the case's evidence list.

### Button `custom_id`

`offer:<offer_id>`, where `<offer_id>` is `ids.offer_id(team, action, memory_id)`, at most 94 characters. One persistent
`DynamicItem` matches every such button, including those on messages from before a restart. The clicker's role decides
the team, not whoever ran the command. Every click goes through `use_offer` and the result is rendered by the bot.

### What the bot calls

| Step | Call | Owner |
|---|---|---|
| Turn start | `traces.begin_turn(scope, "t_<interaction.id>", "suspect:<id>", command, input)` | 1 |
| Turn message | `turn.prepare_interrogate(...)` / `turn.prepare_present(..., llm=None)`; if `ok` is false, post `error`, do not invoke | 3 |
| Generate | hosted: `invoke` (below). local: `local_runner.run(prepared)`. Both write the same trace | 2 |
| Read back | `traces.get_trace(scope, turn_id)`; post only `status == "complete"` | 1 |
| Button click | `actions.use_offer(store, case, offer_id, clicker_team_id)` | 3 |
| World event | `events.trigger_event(...)`: notify `notify_teams` with `event.notice` | 3 |
| Voice | `store.fetch` + `store.count_superseded` for inputs; `traces.set_voice(scope, turn_id, voice)` after the clip | 1 |
| Expiry | `store.watch_expiries(on_expired)`; the bot writes the wording, no memory text | 1 |

**Decided (Person 3 and Person 4): async.** The engine functions are async, taking the async `MemoryStore` and
`TraceStore`. The bot `await`s each call directly, with no threads. Only `case_info()` is synchronous: the adapter loads
the case once at startup (`load_case`). Until the real engine lands, the bot runs against `bot/fake_engine.py`
(`ENGINE_MODE=fake`), which has the same async interface.

### Invoke (hosted mode)

`POST {base}/api/v1/projects/{project}/workspaces/{workspace}/invoke`, body `{"message": prepared.message}`.
Headers on every request: `Accept: application/vnd.agent-engine-2026-09-20-preview+json`,
`X-Agent-Engine-Invoke-Timeout-Seconds: 60`, and the five `X-Mdb-Agent-Engine-Custom-` headers `Case-Id`, `Team-Id`,
`Suspect-Id`, `Turn-Id`, `Command`. Auth: `POST {base}/api/v1/oauth/token` with the service account's client id and secret
(HTTP Basic, `grant_type=client_credentials`) gives the bearer token. The token and secret live only in the bot's `.env`.
A timeout is never retried blindly: the first execution may still be running, so the trace decides.

### Voice

`mystery_core/voice.py` maps decision, mean confidence and cover-story breaks to ElevenLabs settings (no API calls).
`truth` and `lie` are identical. `crack` uses `eleven_multilingual_v2`; the rest use `VOICE_MODEL`
(`eleven_flash_v2_5`). Voice IDs come from `case.suspects[].voice_id`; `VOICE_IDS` in `.env` is a local fallback.
One clip per suspect reply, attached in memory to the team's message, text posted first.
