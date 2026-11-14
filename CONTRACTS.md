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
store = MemoryStore.from_settings(Settings.from_env())    # Atlas
store = FakeStore(cases={"case_x": case_dict})             # in-memory, for unit tests and stubs

await store.retrieve(scope, viewer, query, k=5, kinds=None, mask=None) -> list[Memory]
await store.fetch(scope, viewer, ids) -> list[Memory]       # same filter; missing = superseded/expired/out of scope
await store.write(scope, memories: list[NewMemory], *, dedup=True) -> list[Memory]
await store.supersede(scope, old_id, new: NewMemory, reason) -> Memory
await store.supersede_by_ref(case_id, ref, replacement_text, event_id, reason) -> list[Memory]   # events.py only
await store.forget(scope, viewer=None) -> int
await store.seed_world(case_id, team_id) -> int
```

Differences from ARCHITECTURE.md section 4, on purpose:
- `supersede` takes `scope`: the old memory must be in that world, so a caller can't supersede across worlds by id.
- `supersede_by_ref` takes `event_id`: replacements get `source = {type: "event", ref: event_id}`.
- `write` honours `NewMemory.id`: writing the same deterministic id twice returns the existing memory (retry-safe).

Guarantees:
- Every read is filtered by `case_id`, `team_id`, `known_by`, `status: active`, `expires_at > now`, in `$vectorSearch` and again in `$match`.
- `retrieve`/`fetch` never return embeddings. `score` is set by `retrieve` only.
- Replacements never inherit `unlocks`. A superseded memory disappears from `retrieve`/`fetch` immediately.
- Search index lag: don't write then retrieve. Use the memories `write` returns.

Status: `retrieve`, `fetch`, `write`, `seed_world` are live on Atlas. `supersede`, `supersede_by_ref`, `forget` work in `FakeStore`; Atlas versions land in Phase 2/3.

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
`python -m scripts.seed <case.json> --teams A B` embeds seed memories once and seeds worlds.
`python -m scripts.try_retrieve <case_id> <team> <viewer> "<query>"` checks retrieval.

## Agent Engine (Person 2)

_TODO: custom header names, `submit_reply` arguments and writes._

## Case JSON (Person 3)

_TODO. Memory layer needs: `_id` starting `case_`, `seed_memories[]` with `id`, `text`, `kind`, `known_by`, optional `unlocks`, `confidence`. See `tests/fixtures/mini_case.json`._

## Discord (Person 4)

_TODO: slash command list, button `custom_id` format._
