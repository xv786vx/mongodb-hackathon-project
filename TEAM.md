# Team plan

How four people split the work. Read `ARCHITECTURE.md` and `AGENTS.md` first.

Each person owns one area of the repo and works against agreed interfaces, so nobody waits on anyone.

## Roles

### 1. Memory and data
**Owns:** `mystery_core/memory/`, `setup_db.py`, `scripts/`

- Collections, schema validator, TTL index, vector index, pre-images
- `retrieve`, `fetch`, `write` (with dedup), `supersede`, `supersede_by_ref`, `forget`, `seed_world`
- Seed and reset scripts, the `audit` collection
- `tests/test_isolation.py`
- The expiry-notice change stream worker
- Owns the claim that nothing leaks, and explains it to judges

### 2. Agent Engine
**Owns:** `agents/suspect/` (except `mystery_core/`), `bot/local_runner.py`

- `agentengine create`, `init`, `atlas setup`, secrets, egress, `scaling.replicas`
- The `search_memories` and `submit_reply` tools, reading scope from custom headers
- Suspect prompts and the `ENGINE_MODE=local` fallback
- `/replay`
- The only person who deploys the agent
- Owns the first milestone: a Playground turn that retrieves a seeded clue from Cluster0

### 3. Game rules and case
**Owns:** `mystery_core/engine/`, `cases/`

- The case file: suspects, personas, evidence with `unlocks`, actions, closed rooms, the recant event
- The confession gate, contradiction check, leads and offers, world events, accusation, gossip
- Builds the turn message the agent receives
- Owns whether the game is actually playable and fun

### 4. Discord, voice and demo
**Owns:** `bot/` (except `local_runner.py`), `mystery_core/voice.py`

- Discord app, server, roles, private team channels, slash commands, persistent buttons
- `bot/invoke.py` (Agent Engine client), posting from traces, the 3-second defer
- ElevenLabs: one designed voice per suspect, `voice.py` settings, attaching clips
- Owns the demo script, the one-minute video and the submission

If one person is the strongest backend engineer, give them role 1. A bug in the memory layer breaks every other role.

## First 45 minutes: agree on contracts together

Write these into `mystery_core/memory/models.py` and `CONTRACTS.md` before anyone builds features.

| Contract | Drafted by |
|---|---|
| `Memory`, `Trace` and `Offer` document shapes | Person 1 |
| `retrieve(scope, viewer, query, ...)` and the other store signatures | Person 1 |
| Custom header names: `case-id`, `team-id`, `suspect-id`, `turn-id`, `command` | Person 2 |
| `submit_reply` arguments and what it writes | Person 2 |
| Case JSON shape | Person 3 |
| Slash command list and button `custom_id` format | Person 4 |

Then everyone codes against stubs:

- Person 4 uses a fake engine that returns a canned trace.
- Person 2 uses a fake `retrieve` until person 1's version is ready.
- Person 3 tests rules against an in-memory store.

## Phases

| Phase | Goal | Done when |
|---|---|---|
| 0. Setup | Contracts agreed; repo, Atlas databases, Discord server and keys shared | Everyone can run `pytest` and connect to Cluster0 |
| 1. One turn | `/interrogate` works end to end in local mode, then hosted | A paraphrased question in Discord returns a seeded clue in character |
| 2. Core demo | Leads, offer button, `/event` recant, rejected click, voice clip | The three-step demo works in both engine modes |
| 3. Extras | `/present` correction, gossip and expiry, replay, `/leaktest`, accusation | Done in that order; drop whatever doesn't fit |
| 4. Freeze | No new features. Rehearse, record the video, write the submission | Video recorded, with a backup take |

Phase 2 is the only phase that must finish. Set the feature freeze at least two hours before submission.

### Who does what in each phase

| Phase | Person 1 | Person 2 | Person 3 | Person 4 |
|---|---|---|---|---|
| 0 | Atlas DB, `setup_db.py`, models | Agent scaffold, `atlas setup` on Cluster0 | Case file draft | Discord server, roles, channels, bot skeleton |
| 1 | `retrieve`, `write`, seed script | `search_memories`, `submit_reply`, Playground turn, local runner | Turn message builder, personas | `/interrogate`, invoke client, posting from traces |
| 2 | `supersede_by_ref`, isolation test | Offer proposals in `submit_reply`, deploy | Leads, offers, `/event`, action checks | Buttons, click handling, voice clips |
| 3 | Expiry worker, `forget`, `/leaktest` | `/replay` | `/present` contradiction, gossip, accusation | Replay clips, notices, polish |
| 4 | Explain governance to judges | Keep both engine modes working | Play-test, fix rules | Run the demo, record the video, submit |

## Working agreements

- **One owner per area.** Changes to someone else's area go through them.
- **Separate dev databases.** Each person develops against their own `DB_NAME` on their own M10. The `mystery` database on Cluster0 is for integration and the demo, and only person 1 resets it.
- **Integrate every two hours.** Merge to main, run the core demo, fix what broke.
- **One deployer.** Only person 2 runs `agentengine deploy`, to avoid conflicting deployments and the daily build limit.
- **Player's-eye checks.** Person 4 runs every integration check as a player, because they'll present it.
- **Contract changes are announced.** Changing a shared model, header or signature means updating `CONTRACTS.md` and telling the team before merging.

## Core demo (the finish line)

1. Both teams ask the same suspect the same question. Only Team A gets the "Search the cellar" button.
2. The host runs `/event gardener_recants`. Show the memory become `superseded` in Atlas.
3. Team A clicks the old button and is refused. A fresh question no longer offers it.