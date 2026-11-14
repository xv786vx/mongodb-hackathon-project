# Architecture

A multiplayer murder-mystery game played in Discord. Detective teams interrogate AI suspects whose memories live in MongoDB Atlas under explicit rules for **who can read each memory, how long it lives, and how it is corrected or forgotten** (MongoDB Hackathon, Track 2: Memory You Can Govern).

The game is the demo. The product is the **memory governance engine**: policy stored as data and enforced in the database query, never by the prompt.

---

## 1. System overview

```
Discord (players)
   │  slash commands, one private channel per team
   ▼
bot/  (our own host; a laptop is fine)
   │   identity, commands, deterministic game steps, voice
   │
   ├──► Atlas Agent Engine ── suspect agent ──┐
   │        hosted LangGraph agent with       │
   │        search_memories + submit_reply    ├──► MongoDB Atlas (Cluster0, M10)
   │                                          │      cases, policies, memories
   ├──► mystery_core (same package, in-process)┘      (+ vector index), traces, audit
   │
   └──► ElevenLabs  text-to-speech for the suspect's reply
```

Three pieces of code:

| Piece | Runs on | Job |
|---|---|---|
| `mystery_core` | Imported by both the agent and the bot | The governed core: memory store, policy, turn rules, contradiction check, replay, voice settings. No Discord or Agent Engine imports. |
| Suspect agent | Atlas Agent Engine | Generates one suspect reply per invocation, using tools that call `mystery_core`. |
| Discord bot | Our host | Front end. Resolves identity, runs the deterministic steps, invokes the agent, posts the reply and its audio clip. |

**Fallback.** Agent Engine is in Public Preview. With `ENGINE_MODE=local` the bot generates the suspect turn in-process through `mystery_core` and a direct LLM call. Everything else is identical, so the game never depends on the hosted platform being up.

---

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Case** | One mystery: ground truth (killer, weapon, room), suspects, rooms, evidence, seed memories. Shared by all teams. |
| **World** | One team's private copy of the suspects' memories for a case, keyed by `team_id`. Teams never share a world. |
| **Viewer** | Who a retrieval is performed for: `suspect:<id>` or `team:<id>`. Every read has exactly one viewer inside exactly one world. |
| **Memory** | One natural-language belief held by one or more viewers, with a kind, source, confidence and lifespan. |
| **Belief vs truth** | Memories are beliefs and can be wrong. Ground truth lives only in `cases.truth` and is never retrieved. |
| **Policy** | A document that sets lifespan, propagation and correction behaviour per memory kind. |
| **Trace** | The record of one turn: input, memory IDs retrieved, decision, reply. Basis for replay and audit, and the only source the bot posts replies from. |
| **Lead** | A memory that carries `unlocks`: it supports a specific action for as long as it is active. |
| **Action** | A move the case defines beyond the open commands, such as "Search the cellar". A team can take it only while a live lead in its world supports it. |
| **World event** | Something that happens to the case itself, such as a witness recanting. Applied to every world by the host. |

### Why each team gets its own world

Scoping by suspect alone leaks. If Team A shows the butler a glove and that memory is simply "known by the butler", the butler can mention it to Team B. The same applies to corrections: if Team A's evidence changes the butler's story globally, Team B sees the effect of Team A's work.

So isolation has two dimensions, both enforced in the query:

- `team_id`: the tenant. Which world this memory belongs to.
- `known_by`: the viewer. Who inside that world holds it.

This is the hackathon's "scoped per client and per advisor" example. When a team joins a case, the case's seed memories are copied into that team's world (embeddings are computed once and copied).

---

## 3. Data model (Atlas)

Database `mystery` on Cluster0. All timestamps are **BSON Dates**, never strings. TTL indexes ignore string dates.

### `cases`
```json
{
  "_id": "case_manor",
  "title": "Death at Halloway Manor",
  "truth": { "killer": "butler", "weapon": "candlestick", "room": "library" },
  "suspects": [
    { "id": "butler", "persona": "...", "is_killer": true, "crack_threshold": 2, "voice_id": "..." }
  ],
  "rooms": [
    { "id": "library", "open": true },
    { "id": "conservatory", "open": true },
    { "id": "cellar", "open": false }
  ],
  "weapons": ["candlestick", "rope", "knife", "poison"],
  "evidence": [
    { "id": "ev_note", "room": "conservatory", "unlocks": ["search_cellar"],
      "text": "The gardener's note: he saw the butler go into the cellar after midnight." },
    { "id": "ev_glove", "room": "cellar", "text": "A bloody glove" }
  ],
  "actions": [
    { "id": "search_cellar", "label": "Search the cellar", "reveals": "ev_glove" }
  ],
  "events": [
    { "id": "gardener_recants", "ref": "ev_note",
      "replacement": "The gardener now says he saw no one that night." }
  ],
  "seed_memories": [
    { "text": "...", "embedding": [], "known_by": ["suspect:cook"], "kind": "witnessed" }
  ],
  "status": "active"
}
```
Only `mystery_core/engine/game.py` reads `truth`, and only to resolve an accusation. Each `voice_id` is an original ElevenLabs voice made with Voice Design.

- `rooms`: `/search` works only on open rooms. A closed room is reachable only through an action.
- `evidence[].unlocks`: the actions this evidence supports once a suspect has been shown it.
- `actions`: what each action reveals when taken.
- `events`: world events the host can trigger. `ref` names the evidence or seed memory that changes.

### `policies`
```json
{
  "_id": "mystery_v1",
  "kinds": {
    "witnessed":   { "ttl_seconds": null, "propagates": true,  "on_contradiction": "correct" },
    "rumour":      { "ttl_seconds": 600,  "propagates": true,  "on_contradiction": "correct", "confidence_decay": 0.3 },
    "cover_story": { "ttl_seconds": null, "propagates": true,  "on_contradiction": "new_lie" },
    "evidence":    { "ttl_seconds": null, "propagates": false },
    "interaction": { "ttl_seconds": null, "propagates": false },
    "finding":     { "ttl_seconds": null, "propagates": false }
  }
}
```

| Kind | Held by | What it is |
|---|---|---|
| `witnessed` | suspect | Something the suspect saw or assumed. Can be wrong (seed at least one false belief for the demo). |
| `rumour` | suspect | Heard from another suspect through gossip. Expires. |
| `cover_story` | killer | A deliberate lie. Contradiction produces a new lie, not a correction. |
| `evidence` | suspect | Something a detective showed this suspect. |
| `interaction` | suspect | "The detectives asked me about X." |
| `finding` | team | The team's notebook. |

Lifespans are wall-clock seconds, because TTL indexes work on time. `RUMOUR_TTL_SECONDS` overrides the rumour lifespan for the demo.

### `memories`
```json
{
  "_id": "m_042",
  "case_id": "case_manor",
  "team_id": "A",
  "text": "At 8pm I saw the butler polishing silver in the pantry.",
  "embedding": [],
  "known_by": ["suspect:cook"],
  "kind": "witnessed",
  "source": { "type": "seed", "ref": null },
  "confidence": 1.0,
  "unlocks": [],
  "status": "active",
  "supersedes": null,
  "superseded_by": null,
  "created_at": ISODate("2026-11-14T16:00:00Z"),
  "expires_at": ISODate("9999-12-31T00:00:00Z")
}
```

- `source.type`: `seed | told_by | evidence | interaction | fabricated | event`. `source.ref` is the seed ID, evidence ID or suspect it came from, and is how a world event finds the same memory in every world.
- `unlocks`: action IDs this memory supports. Copied from the evidence item when evidence is presented. Never copied onto rumours or onto the replacement in a supersession, so a lead dies with the memory that carried it.
- `status`: `active | superseded`. Forgetting is a hard delete.
- `expires_at` is always set. Permanent memories use the far-future sentinel, so one `expires_at > now` filter covers every memory.
- Corrections never edit `text`. They insert a new memory and mark the old one `superseded`, linked both ways.

### `traces`
```json
{
  "_id": "t_9f2c",
  "status": "complete",
  "case_id": "case_manor",
  "team_id": "A",
  "actor": "suspect:butler",
  "command": "interrogate",
  "input": "Where were you at 8pm?",
  "retrieved": ["m_042", "m_051"],
  "decision": "deflect",
  "reply": "...",
  "written": ["m_088"],
  "offer_id": null,
  "engine_mode": "hosted",
  "execution_id": "...",
  "model": "...",
  "prompt_version": "suspect_v3",
  "voice": { "voice_id": "...", "model": "eleven_flash_v2_5", "stability": 0.45, "style": 0.5 },
  "replay_of": null,
  "masked": [],
  "ts": ISODate("...")
}
```
`_id` is the turn ID, derived from the Discord interaction ID (`t_<interaction_id>`), so a retried command maps to the same trace. `status` is `pending | complete | rejected`.

### `offers`
```json
{
  "_id": "a_A_search_cellar_m_093",
  "case_id": "case_manor",
  "team_id": "A",
  "action": "search_cellar",
  "supported_by": "m_093",
  "offered_in": "t_9f2c",
  "status": "offered",
  "ts": ISODate("...")
}
```
One document per action a team has been offered. `_id` is built from the team, action and supporting memory, so the same offer is never stored twice. `status` is `offered | used | rejected`.

### `audit`
Governance events with IDs only, never memory text: `supersede`, `forget`, `expire`, `leak_test`.

### Indexes and validation (created by `setup_db.py`)

**Vector Search index** `memory_index` on `memories`:
```json
{
  "fields": [
    { "type": "vector", "path": "embedding", "numDimensions": 1024, "similarity": "cosine" },
    { "type": "filter", "path": "case_id" },
    { "type": "filter", "path": "team_id" },
    { "type": "filter", "path": "known_by" },
    { "type": "filter", "path": "status" },
    { "type": "filter", "path": "kind" },
    { "type": "filter", "path": "expires_at" }
  ]
}
```
`numDimensions` must match the embedding model (`EMBEDDING_DIMS`).

**TTL index:** `{ expires_at: 1 }`, `expireAfterSeconds: 0`.

**Regular indexes:** `{ case_id: 1, team_id: 1, known_by: 1, status: 1 }` and `{ case_id: 1, "source.ref": 1, status: 1 }` on `memories`; `{ case_id: 1, team_id: 1, ts: -1 }` on `traces`; `{ case_id: 1, team_id: 1, status: 1 }` on `offers`.

**Schema validation** on `memories` (`$jsonSchema`, `validationAction: "error"`): requires `case_id`, `team_id`, `text`, `embedding`, `known_by` (array, at least one item), `kind` (enum), `status` (enum), `created_at` and `expires_at` (`bsonType: "date"`). `unlocks` is optional and must be an array of strings.

**Pre-images:** `memories` is created with `changeStreamPreAndPostImages: { enabled: true }` so the expiry worker can see what a TTL delete removed.

---

## 4. Memory service (`mystery_core/memory/`)

The only module allowed to touch `memories`. Both the agent's tools and the bot call it.

```python
retrieve(scope: Scope, viewer, query, k=5, kinds=None, mask=None) -> list[Memory]
fetch(scope: Scope, viewer, ids) -> list[Memory]          # by _id, same scope filter
write(scope: Scope, memories: list[NewMemory]) -> list[Memory]
supersede(old_id, new: NewMemory, reason: str) -> Memory
supersede_by_ref(case_id, ref, replacement_text, reason) -> list[Memory]   # world events only
forget(scope: Scope, viewer=None) -> int
seed_world(case_id, team_id) -> int
```
`Scope` is `(case_id, team_id)`. It is always built from trusted input (section 6), never from model output or player text.

### `retrieve`
```python
flt = {
    "case_id": scope.case_id,
    "team_id": scope.team_id,
    "known_by": viewer,
    "status": "active",
    "expires_at": {"$gt": now},
}
pipeline = [
    {"$vectorSearch": {
        "index": "memory_index", "path": "embedding",
        "queryVector": embed(query),
        "numCandidates": 100, "limit": k,
        "filter": flt,
    }},
    {"$match": flt},
    {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
]
```

- **The Vector Search index is eventually consistent.** A write or status change can take a moment to reach the index. The `$match` stage repeats the filter against the live documents, so a memory superseded a second ago is still excluded.
- **The TTL monitor runs about once a minute.** Expired memories are excluded by the `expires_at` filter immediately; the delete follows later.
- `mask` excludes memory IDs. Only replay uses it.

### `write`
1. Apply policy defaults: `expires_at` from `ttl_seconds`, confidence.
2. Embed the text.
3. Dedup within the same scope, `known_by` and `kind`. If a near-duplicate exists, raise its confidence and skip the insert.
4. Insert. Schema validation is the last line of defence.
5. Return the full documents. A memory needed in the next prompt is passed directly; nothing relies on retrieval finding something written a moment ago.

### `supersede`
Inserts the new memory with `supersedes: old_id`, sets the old one to `status: "superseded"` and `superseded_by`, and writes an `audit` event, in one transaction. The replacement never inherits `unlocks`.

### `supersede_by_ref`
Used only for world events. Supersedes every active memory in the case whose `source.ref` matches, in every world that holds one, and returns the replacements so the bot knows which teams to notify. Each replacement has `source: { type: "event", ref: <event_id> }`, so a team's notebook no longer holds the original evidence and it cannot be presented again to bring the lead back. It is the one call that crosses worlds, and it is host-only.

### `forget`
Hard-deletes memories for a team (or one viewer) in a case and writes one `audit` event with the count. Closing a case also deletes that case's `traces`.

---

## 5. The suspect agent (Atlas Agent Engine)

One hosted LangGraph agent plays every suspect. Which suspect, which case and which team arrive with each request.

```python
from agent_engine_sdk_langgraph import App
from agent_engine_runner_shared import get_current_custom_headers

app = App(app_name="Suspect")

@app.tool(is_local=True)
def search_memories(query: str) -> str:
    """Recall what this character knows that is relevant to the query."""
    turn = turn_context(get_current_custom_headers())   # case, team, suspect, turn id
    ...

@app.tool(is_local=True)
def submit_reply(decision: str, reply: str,
                 proposed_action: str | None = None,
                 supporting_memory_id: str | None = None) -> str:
    """Submit the character's final answer. Must be called exactly once."""
    ...
```

### Tools

| Tool | Model supplies | Tool does |
|---|---|---|
| `search_memories` | `query` only | Reads scope from the request's custom headers, calls `memory.retrieve`, appends the returned IDs to the turn's trace, returns the memory texts. In replay mode it returns the original turn's memories minus the masked one. |
| `submit_reply` | `decision`, `reply`, and optionally `proposed_action` with `supporting_memory_id` | Validates the decision against the allowed set (recomputed from the database), caps length, strips bracketed tags, writes the `interaction` memory and the team's `finding`, validates any proposed action (section 7), and marks the trace `complete`. Returns an error the model can act on if the decision is not allowed. If the trace is already `complete`, it returns the stored result and writes nothing. |

Neither tool has a parameter for case, team, suspect or viewer. The model cannot widen its own scope because there is nothing to pass.

### Platform settings

- **Atlas link:** `agentengine atlas setup`, selecting the existing Cluster0 (M10). The game uses its own database, `mystery`.
- **Built-in memory: off** (`features.memory: false`). The `memories` collection is the only memory store.
- **Sessions hold nothing we rely on.** Each turn uses a fresh session and the agent calls `app.finish_session()` when the turn ends. Continuity comes from `interaction` memories and the last few turns in `traces`, both of which obey `forget()`. Platform conversation history would be a second memory store outside our rules: a superseded belief would linger in it and replay would be contaminated.
- **Capacity:** each session reserves one agent sandbox and one tool sandbox, and `scaling.replicas` (default 4) caps concurrent sessions. Set it to at least 8. Scaling values are captured at build time.
- **Secrets:** `MONGODB_URI` (set by `atlas setup`), `LLM_API_KEY`, `EMBEDDING_API_KEY`, via `agentengine secret set`.
- **Egress:** allow the LLM and embedding hosts for the sandbox that runs the tools.
- **Debugging:** the Playground shows each tool call in the turn's trace. Our own `traces` collection is still the record replay uses.

---

## 6. Trust boundary: how scope reaches the agent

The bot is the only caller of the agent, and the only holder of the Agent Engine API key.

```
POST https://agentengine.mongodb.com/api/v1/projects/{project}/workspaces/{workspace}/invoke
Authorization: Bearer <AGENT_ENGINE_API_KEY>
Accept: application/vnd.agent-engine-2026-09-20-preview+json
X-Mdb-Agent-Engine-Custom-Case-Id:    case_manor
X-Mdb-Agent-Engine-Custom-Team-Id:    A
X-Mdb-Agent-Engine-Custom-Suspect-Id: butler
X-Mdb-Agent-Engine-Custom-Turn-Id:    t_9f2c
X-Mdb-Agent-Engine-Custom-Command:    interrogate
X-Agent-Engine-Invoke-Timeout-Seconds: 60

{ "message": "<turn message>" }
```

- **Trusted:** the custom headers. The bot sets them from the player's Discord role and a suspect validated against the case. The platform strips the prefix and lowercases the names, so tools read `case-id`, `team-id`, `suspect-id`, `turn-id`, `command`. Headers are not persisted and are sent on every request.
- **Trusted, built by `mystery_core`:** the first part of the message: character sheet, allowed decisions, any fresh memories (for `/present`), and the last few turns from `traces`.
- **Untrusted:** the player's question, placed last in a clearly quoted block.

Two rules keep the model from being a control point:

1. **Deterministic steps never depend on the model choosing to call a tool.** Evidence writes, the contradiction check, supersession and the confession gate run in `mystery_core` before the agent is invoked.
2. **The bot posts what the trace says, not what the model says.** After the invocation returns, the bot reads `traces[turn_id]`. Only a `complete` trace, written by `submit_reply` after validation, is shown to players.

---

## 7. Turn flows

Slash commands must be acknowledged within 3 seconds, so every handler defers first. Turns are serialized per `(team_id, suspect)`.

### `/interrogate <suspect> <question>`
1. Bot resolves `team_id` from the member's Discord role and `case_id` from the server's active case, validates the suspect, and inserts a `pending` trace.
2. `mystery_core` builds the turn message, including the allowed decisions. `crack` is allowed only when the suspect's cover story has been superseded `crack_threshold` times in this world.
3. Bot invokes the agent (or the local runner).
4. Agent calls `search_memories`, then `submit_reply`.
5. Bot reads the trace. If `complete`, it posts the reply text in the team's private channel, then attaches the audio clip (section 8). If the trace carries an `offer_id`, the reply includes a button for that action.

### `/present <evidence> <suspect>`
The correction happens before the reply is generated.

1. Check that the team's notebook holds an active finding whose `source.ref` is this evidence.
2. Write an `evidence` memory for the suspect, copying the evidence item's `unlocks`.
3. Contradiction check: retrieve the suspect's active beliefs near the evidence text (`witnessed`, `rumour`, `cover_story`), then ask the LLM for `{ "contradicts": true, "memory_id": "..." }`.
4. Apply the policy's `on_contradiction`: `correct` supersedes the belief with a corrected one; `new_lie` supersedes the cover story with a new one (`source.type: "fabricated"`).
5. Continue as `/interrogate` from step 2, with the evidence and the new memory in the trusted part of the message.

### Leads and actions

An action is available to a team only while a live memory in its world supports it. This is the most direct proof that memory changes what the agent can do next.

**Offering.** In `submit_reply` the model may propose one action and name the memory that supports it. The offer is created only if all of these hold:

1. The action exists in the case's `actions`.
2. The supporting memory ID is in this turn's `trace.retrieved`.
3. A scoped `memory.fetch` returns it, meaning it is active, unexpired and in this team's world.
4. Its `unlocks` contains the action.
5. The team has not already used the action.

If any check fails, the reply is still accepted and the offer is dropped, with the reason recorded on the trace. The model proposes; `mystery_core` decides.

**Using.** The button's ID is the offer ID. On click:

1. The clicker's Discord role must match the offer's `team_id`.
2. The supporting memory is fetched again with the same scoped call. If it is no longer returned, the offer becomes `rejected`, an `audit` event is written, and the team is told the lead no longer holds.
3. Otherwise the offer flips from `offered` to `used` in one atomic update, the action's evidence is revealed, and a `finding` is written.

A lead that has been superseded or has expired fails step 2 without any extra bookkeeping, because the scoped fetch simply stops returning it.

**World events.** `/event <event_id>` (host only) calls `supersede_by_ref`. Every world holding that memory gets the replacement, which carries no `unlocks`. Only the teams whose world held it are notified, in their own channels, so the event does not reveal the memory's existence to anyone else.

### Other commands (no agent involved)
- `/search <room>`: open rooms only. Reveals evidence there and writes a `finding`.
- `/notebook [query]`: vector search over the team's own findings.
- `/accuse <who> <weapon> <room>`: checked against `cases.truth`. Result announced in `#case-room`.
- `/round` (host only): runs gossip for every world.
- `/event <event_id>` (host only): triggers a world event.
- `/discover <team> <evidence>` (host only): records a discovery for one team, to stage the demo.
- `/replay <trace_id> <memory_id>` (host only): section 9.
- `/leaktest` (host only): runs the isolation checks live and posts pass or fail.

### Retry safety

A timeout, a double click or a model calling a tool twice must not create a second copy of anything.

| Operation | How a repeat is made harmless |
|---|---|
| A turn | The turn ID comes from the Discord interaction ID. A retry finds the same trace: if `complete` the bot posts it, if `pending` the bot invokes again. |
| `submit_reply` | Returns the stored result when the trace is already `complete`. |
| A discovery | The finding's `_id` is built from case, team and evidence ID, so a second insert fails on the duplicate key and is treated as "already found". |
| An offer | `_id` is built from team, action and supporting memory. |
| Using an action | One atomic update from `offered` to `used`. Only the update that matches performs the action. |
| A world event | Skips worlds where the memory is already superseded. |

### Workers (inside the bot process)

| Worker | Trigger | Action |
|---|---|---|
| `gossip.py` | `/round` | Per world, for selected suspect pairs: copy the speaker's propagating memories on a topic to the listener as `rumour`, with decayed confidence and the rumour TTL. |
| `expiry_notices.py` | Change stream on `memories`, delete events, `fullDocumentBeforeChange: "whenAvailable"` | Writes an `audit` event and posts "a rumour has faded" in that team's channel. Stores its resume token. |

---

## 8. Voice (ElevenLabs)

Voice is output only. It never decides identity, scope or visibility.

1. After a turn's trace is `complete`, the bot asks `mystery_core/voice.py` for settings. Inputs are facts the engine already holds: the decision, the mean confidence of the retrieved memories, and how many times the cover story has been broken.
2. The bot sends only the reply text and the suspect's `voice_id` to the ElevenLabs text-to-speech API.
3. The clip is attached to the reply in the team's private channel. Discord's channel permissions are the access check. Nothing is written to disk or served from a URL.

| Decision | Stability | Style | Sounds like |
|---|---|---|---|
| `truth` | 0.80 | 0.10 | Calm and steady |
| `lie` | 0.80 | 0.10 | Identical to `truth`, so the voice is never a tell |
| `deflect` | 0.45 | 0.50 | Guarded, uneven |
| `crack` | 0.20 | 0.85 | Breaking down |

Stability is then scaled down by low memory confidence, so a suspect repeating a fading rumour sounds unsure. These numbers are starting points to tune by ear.

- **Model:** `eleven_flash_v2_5` by default for speed. `eleven_v3` is optional for the confession scene; it supports inline audio tags such as `[sighs]`.
- **Tags come from a whitelist in `voice.py`.** Bracketed text in the model's reply is stripped, so the model cannot direct its own delivery.
- **Text first, audio second.** The text reply is posted as soon as the trace is complete; the clip follows.
- **Audible replay:** `/replay` attaches both versions of the turn, so the change in behaviour can be heard.

Not built: whisper-based visibility, voice roll call for identity, stress scoring of players, and barge-in. Each one lets an audio classifier or a spoofable signal decide access, or infers things about real people.

---

## 9. Proving a memory changed behaviour

There are two proofs. The first is leads and actions (section 7): a team with the supporting memory is offered an action, a team without it is not, and once the memory is superseded the old action is rejected. That proof is deterministic and visible in play. The second is replay, which shows the same effect on the suspect's dialogue.

`/replay <trace_id> <memory_id>`:
1. Loads the trace. `search_memories` returns the original turn's retrieved memories, minus the masked one, fetched by ID.
2. Runs the turn N times with the memory and N times without, through the same engine mode as the original, each in a fresh session.
3. Reports the decision counts side by side, for example "with m_088: crack 3/3, without: deflect 3/3", and attaches one clip of each.
4. Is a dry run: `submit_reply` writes a replay trace with `replay_of` and `masked` set, and no memories.

Host-only, because it exposes what a suspect knows.

---

## 10. Track 2 questions

| Question | Mechanism | Shown by |
|---|---|---|
| Keep one team's memory from leaking into another's | `team_id` and `known_by` pre-filters inside `$vectorSearch`. Scope travels in bot-set headers; tools have no scope parameters. | `/leaktest`, `tests/test_isolation.py`, Team B getting the old story after Team A's evidence |
| Fix or drop a wrong or expired memory | Supersession with lineage; world events; TTL plus the `expires_at` filter; `forget()` with an audit event; no platform session memory | A witness recanting; `/present` changing a suspect's story; a rumour fading |
| Prove a memory changed behaviour | Actions backed by a live lead and re-checked on use; structured decisions, traces, masked replay over N samples | An action offered to one team only, then rejected after the recant; `/replay`, heard as well as read |

---

## 11. Where memory text travels

| Destination | What it receives | Why |
|---|---|---|
| Atlas (Cluster0) | Everything | System of record |
| Atlas Agent Engine | The turn message and tool results, for the length of one turn | Hosts the suspect agent |
| LLM provider | The turn message and retrieved memories | Generates the reply |
| Embedding provider | Memory text and queries | Vector search |
| ElevenLabs | The reply text and a voice ID | Speech |
| Discord | Replies and clips, in the asking team's channel | Front end |

`forget()` removes memories and traces from Atlas. It cannot recall text already posted to Discord or sent to a provider.

---

## 12. Threats and limits

- **Prompt injection by players.** Retrieval is filtered before the model sees anything, tools take no scope arguments, and replies are released only through `submit_reply`.
- **Stale buttons.** A button posted before a correction stays visible in Discord. It is harmless, because the lead is checked again on every click.
- **Stolen Agent Engine key.** Whoever holds it can set any headers. The key lives only in the bot's environment.
- **Broad database user.** `agentengine atlas setup` grants `readWriteAnyDatabase` on the cluster. Acceptable for the hackathon; a production version would use a user limited to `mystery`.
- **Leaks through public channels.** Only accusations and round announcements go to `#case-room`.
- **Preview platform.** Agent Engine has no SLA and its API can change. Pin the API version and keep `ENGINE_MODE=local` working.
- **Not covered:** players sharing information outside the game, and replies that hint at the truth from the persona alone. Keep personas free of case facts.

---

## 13. Deployment and configuration

- **Atlas:** one shared Cluster0 (M10) for the game. Each teammate's own M10 is a dev sandbox with its own `DB_NAME`.
- **Agent:** `agents/suspect/` is an Agent Engine workspace, deployed with `agentengine deploy --auto`.
- **Bot:** one process on a laptop or any host. It also runs the workers.

| Variable | Bot | Agent |
|---|---|---|
| `DISCORD_TOKEN`, `DISCORD_GUILD_ID` | yes | |
| `ENGINE_MODE` (`hosted` or `local`) | yes | |
| `AGENT_ENGINE_API_KEY`, `AGENT_ENGINE_PROJECT_ID`, `AGENT_ENGINE_WORKSPACE_ID` | yes | |
| `MONGODB_URI`, `DB_NAME` | yes | yes |
| `LLM_API_KEY`, `LLM_MODEL` | yes | yes |
| `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMS` | yes | yes |
| `ELEVENLABS_API_KEY`, `VOICE_MODEL` | yes | |
| `RUMOUR_TTL_SECONDS` | yes | yes |

### To verify in the first hour
1. A hosted Playground turn calls `search_memories` and returns a seeded clue from Cluster0.
2. A fresh session per turn is fast enough. If cold starts are slow, reuse one session per case, team and suspect, and accept that the platform keeps that pair's history.
3. `agentengine dev up` starts a local MongoDB container. If it cannot run `$vectorSearch`, point the agent's `.env` at a dev database on Atlas.
4. The deploy bundle includes `mystery_core` (it lives inside the agent workspace for this reason).

## 14. Out of scope for the hackathon

Disabling a stale button the moment its lead is superseded (a change stream could do it; the click check already makes it safe), voice input (Discord voice messages transcribed with ElevenLabs Scribe, with identity still taken from the Discord role), playback in a Discord voice channel, overheard-gossip audio, procedural case generation, a web UI, and several simultaneous cases per server.
