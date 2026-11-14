# AGENTS.md

Guidance for AI coding agents and humans working in this repo. Read `ARCHITECTURE.md` first.

## Project in one paragraph

A Discord murder-mystery game where AI suspects have governed memory in MongoDB Atlas. The suspect agent is hosted on Atlas Agent Engine, a Discord bot is the front end, and ElevenLabs speaks the suspects' replies. The point of the project is the memory layer: who can read each memory, how long it lives, how it is corrected, and proof that it changed behaviour. Built for the MongoDB Toronto hackathon, Track 2 ("Memory You Can Govern"). Favour working and demoable over complete.

## Layout

```
agents/suspect/                 Atlas Agent Engine workspace (deployed)
  agent.yaml  pyproject.toml  .env
  src/mystery_core/             the governed core; no Discord or Agent Engine imports
    memory/                     store.py, policy.py, models.py
    engine/                     turn.py, contradiction.py, actions.py, events.py, replay.py, game.py, prompts/
    voice.py                    decision -> voice settings (no API calls)
  src/suspect_agent/            graph.py (App, search_memories, submit_reply), llm.py
bot/                            Discord process, runs on our own host
  main.py                       slash commands
  invoke.py                     Agent Engine client
  local_runner.py               ENGINE_MODE=local path
  tts.py                        ElevenLabs client
  workers/                      gossip.py, expiry_notices.py
cases/  scripts/  tests/  setup_db.py
```

`mystery_core` lives inside the agent workspace so the deploy bundle contains it. The bot installs it with `pip install -e agents/suspect`.

## Setup and commands

```bash
# one-time
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e agents/suspect
cp .env.example .env                      # bot variables
python setup_db.py                        # collections, validator, TTL + vector indexes (idempotent)
python -m scripts.seed cases/manor.json   # loads the case and embeds seed memories

# agent (run from agents/suspect)
agentengine dev up                        # local stack + Playground UI
agentengine init                          # register the workspace
agentengine atlas setup                   # choose the existing Cluster0, not a new cluster
agentengine secret set LLM_API_KEY        # repeat for EMBEDDING_API_KEY
agentengine deploy --auto
agentengine invoke "test"                 # smoke test
agentengine logs                          # runtime logs;  agentengine deploy logs  for build failures

# bot (repo root)
python -m bot.main                        # ENGINE_MODE=hosted or local
pytest                                    # unit tests
pytest -m atlas                           # integration tests against DB_NAME=mystery_test
python -m scripts.reset                   # wipe worlds and traces for a fresh demo
```

The vector index takes a minute or two to build after `setup_db.py`. Queries return nothing until its status is READY.

## Build order

1. `setup_db.py`, seed one clue, and get `memory.retrieve` returning it from a script.
2. Deploy the agent with `search_memories` only. In the Playground, ask a paraphrased question and confirm the tool retrieved the clue from Cluster0.
3. Add `submit_reply` and traces. Get `/interrogate` working from Discord in `local` mode, then `hosted`.
4. `/present` with correction. Then the isolation test.
5. Leads and actions: the offer button, the check on click, and `/event` for the recant. This completes the core demo.
6. Voice clips. Then replay, gossip and expiry notices.

Keep `ENGINE_MODE=local` working at every step. It is the fallback for the demo.

## Hard rules

Breaking any of these breaks the project's main claim.

**Memory**
1. **All memory access goes through `mystery_core/memory/store.py`.** No other module touches the `memories` collection.
2. **Every vector search is filtered.** `$vectorSearch` on `memories` always includes `case_id`, `team_id`, `known_by`, `status: "active"` and `expires_at > now`, followed by a `$match` with the same filter. No unfiltered searches, including in scripts and debugging.
3. **Never edit a memory's text.** Corrections go through `supersede()`.
4. **Always set `expires_at`, as a BSON Date.** Permanent memories use the far-future sentinel. No date strings in the database.
5. **`cases.truth` never goes into a prompt.** Only `engine/game.py` reads it, to check an accusation.
6. **No second memory store.** Agent Engine's built-in memory stays off. Do not rely on platform session history; continuity comes from `interaction` memories and recent `traces`.

**Scope and identity**
7. **Identity comes from Discord.** `team_id` comes from the member's role; the suspect comes from a command argument validated against the case. Never from message text, voice, or model output.
8. **Agent tools have no scope parameters.** `search_memories` and `submit_reply` read case, team, suspect and turn ID from `get_current_custom_headers()`. Never add an argument that lets the model name a case, team, suspect or viewer.
9. **Only the bot calls the agent.** The Agent Engine API key lives in the bot's environment and nowhere else.
10. **Player text is untrusted input.** It goes last in the turn message, in a quoted block. It never changes scope, policy or permissions.

**Turn integrity**
11. **Deterministic steps never depend on the model calling a tool.** Evidence writes, the contradiction check, supersession and the confession gate run in `mystery_core` before the agent is invoked.
12. **The bot posts from the trace.** Only a trace marked `complete` by `submit_reply` is shown to players. Never post the raw invoke response.
13. **`crack` is gated.** It is allowed only when the cover story has been superseded `crack_threshold` times in that team's world. `submit_reply` re-checks this.
14. **Every turn writes a trace** with the retrieved memory IDs. Replay is a dry run and writes no memories.
15. **An action needs a live lead, checked twice.** An offer is created only by `submit_reply` after the checks in `ARCHITECTURE.md` section 7, and the supporting memory is fetched again, scoped, on every click. Never cache "this team may do X". `unlocks` is never copied onto rumours or onto a replacement memory.
16. **Every write is safe to repeat.** Turn IDs come from the Discord interaction ID. Discoveries and offers use deterministic `_id`s. Using an action is one atomic status update. Never generate a random ID for something a retry could create twice.
17. **Only world events cross worlds.** `supersede_by_ref` is called from `engine/events.py` and nowhere else, and only by a host command.

**Output**
18. **Suspect replies and clips go only to the asking team's private channel.** The public channel gets accusations and round announcements. World-event notices go only to teams whose world held the memory.
19. **Voice settings and audio tags come from `mystery_core/voice.py`.** Strip bracketed text from model replies before speech. `truth` and `lie` use identical settings.
20. **No audio on disk or at a URL.** Generate the clip in memory and attach it to the Discord message.
21. **Voice never decides access, and nothing is inferred about real players from their voice.**
22. **Original names, characters, voices and art only.** Nothing from Clue/Cluedo or any other published game. Voices are made with Voice Design, never cloned.

## Conventions

- Python 3.11+, type hints. PyMongo for the database (`AsyncMongoClient` in the bot), `discord.py` for Discord.
- Pydantic models in `mystery_core/memory/models.py` for `Memory`, `NewMemory`, `Scope`, `Trace` and every LLM output.
- `mystery_core` must import nothing from `discord`, `agent_engine_sdk_langgraph` or `elevenlabs`.
- Config through environment variables only. Bot: `.env` at the repo root. Agent: `agents/suspect/.env` locally and `agentengine secret set` when deployed. Never commit either file.
- Prompts live in `mystery_core/engine/prompts/` as versioned templates. Record the version in each trace.
- IDs: `m_*` memories, `t_*` traces and turn IDs, `case_*` cases. Viewers are `suspect:<id>` or `team:<id>`.
- Do not log memory text or embeddings at INFO level. Log IDs.

## Things that will bite you

**Atlas**
- **Search index lag.** A memory written a moment ago may not be searchable yet. Pass fresh memories in the turn message; never write and then retrieve to find them.
- **TTL lag.** Expired documents are deleted up to about a minute late. Rule 2's `expires_at` filter is what makes expiry exact.
- **Embedding dimensions.** `EMBEDDING_DIMS` must equal `numDimensions` in the vector index. Changing the model means rebuilding the index and re-seeding.
- **Schema validator.** Inserts that miss `team_id`, `known_by` or `expires_at` fail. Fix the caller, do not loosen the validator.

**Agent Engine**
- **`pool full` errors.** Each session reserves two sandboxes and `scaling.replicas` defaults to 4. Set it to 8 or more, call `app.finish_session()` at the end of every turn, and rebuild after changing scaling values.
- **Custom headers.** Send them as `X-Mdb-Agent-Engine-Custom-<Name>` on every request. Tools read them lowercased without the prefix (`team-id`). They are not stored between requests.
- **Secrets.** The local container reads only `agents/suspect/.env`. The deployed agent reads only values set with `agentengine secret set`.
- **Egress.** The deployed agent can reach only allowed hosts. Add the LLM and embedding hosts, or tool calls fail.
- **Local dev database.** `agentengine dev up` starts its own MongoDB container. If vector search does not work there, point `.env` at a dev database on Atlas.
- **Invoke timeout.** A blocking invoke waits at most 120 seconds. Set `X-Agent-Engine-Invoke-Timeout-Seconds`.
- **Preview API.** Send `Accept: application/vnd.agent-engine-2026-09-20-preview+json` so request and response shapes stay fixed.
- **Small sandbox.** The agent gets 0.5 vCPU and 2 GB. Keep tools light.

**Discord**
- **3-second limit.** Every slash command handler calls `await interaction.response.defer()` first and replies with `interaction.followup.send(...)`.
- **2,000-character limit.** `submit_reply` caps reply length.
- **Audio.** Attach with `discord.File(io.BytesIO(audio), filename="reply.mp3")`. Do not join voice channels.
- **Buttons.** Put the offer ID in the button's `custom_id` and register the view as persistent (`timeout=None`, `bot.add_view(...)` at startup), or buttons on older messages stop responding after a restart. A click is an interaction too, so defer it within 3 seconds. Authorize the person who clicked, not the person who ran the original command.

**ElevenLabs**
- **Latency.** Post the text first, then the clip. `eleven_flash_v2_5` is the default; `eleven_v3` is slower.
- **Tags.** Inline audio tags work only on `eleven_v3`.
- **Cost.** Speech is billed per character. One clip per reply; no clips for notebook or search output.

## Testing

- Unit tests mock the LLM, embedding, Agent Engine and ElevenLabs clients. Deterministic fake embeddings are fine.
- `tests/test_isolation.py` is the most important test. It must cover:
  - Team B cannot retrieve Team A's findings.
  - After Team A presents evidence, the same suspect in Team B's world has no memory of it and still holds the old belief.
  - A suspect cannot retrieve another suspect's memories.
- Also test:
  - `search_memories` and `submit_reply` expose no scope parameters, and fail closed when a header is missing.
  - A superseded memory is excluded immediately; an expired one is excluded before the TTL delete.
  - `crack` is rejected below the threshold; a rejected turn is never posted.
  - Replay writes no memories.
  - Leads: Team A is offered the action and Team B is not; an offer is refused when the supporting memory was not retrieved this turn or lacks `unlocks`; after `/event` the old button is rejected and the evidence cannot be presented again; a rumour copy carries no `unlocks`.
  - Retries: running the same turn twice writes one trace and one set of memories; a double click performs the action once; a repeated discovery creates one finding.
  - `voice.py` returns identical settings for `truth` and `lie`, and bracketed text is stripped.
- Integration tests are marked `atlas`, use `DB_NAME=mystery_test`, poll until documents are searchable, and clean up after themselves.

## When changing things

- **Memory document shape:** update `ARCHITECTURE.md` section 3, the `$jsonSchema` in `setup_db.py`, the Pydantic model, and the vector index if a filter field changed.
- **New memory kind:** add it to the policy document, the schema enum and the kinds table in `ARCHITECTURE.md`.
- **New command:** handler in `bot/main.py`, logic in `mystery_core/engine/`, a trace if it reads memory.
- **New action:** add it to the case's `actions`, put its ID in `unlocks` on the evidence or seed memory that supports it, and close the room it opens.
- **New world event:** add it to the case's `events` with the `ref` it changes and the replacement text.
- **New agent tool:** no scope parameters (rule 8), list it in `agent.yaml`, redeploy.
- **New custom header:** add it in `bot/invoke.py` and in `turn_context()`, and to `ARCHITECTURE.md` section 6.
- **New prompt version:** bump the version name so old traces still identify what produced them.
- **Anything in `agents/suspect/`:** redeploy with `agentengine deploy --auto` and re-run the Playground smoke test.

## Demo path

### Core demo (protect this first)

Setup: Team A has found the gardener's note and shown it to the butler. Team B has not.

1. **Same question, different outcome.** Both teams ask the butler where he was after midnight. Both hear the same voice. Only Team A's reply has a "Search the cellar" button, because only Team A's world holds the memory that supports it.
2. **The witness recants.** The host runs `/event gardener_recants`. Show the memory's `status` change to `superseded` in Atlas Data Explorer.
3. **The old lead fails.** Team A clicks the old button and is refused. On a fresh question the butler no longer offers the cellar. Show the tool call in the Agent Engine trace.

Do not merge anything that breaks these three steps.

### Extras, if time allows

4. Team A presents different evidence to the cook; her belief is corrected, while Team B still hears her original story.
5. `/round` spreads a rumour. It later fades and the team is notified.
6. `/replay` shows the decision flipping when one memory is masked, with both clips attached.
7. `/leaktest` passes.
8. Team A accuses and the case is solved.

Rehearse in both engine modes. Run `python -m scripts.reset` before every rehearsal, then `/discover` and `/present` to stage the setup.
