# Halloway Manor suspect workspace

One hosted LangGraph plays the butler, cook, gardener or niece selected by the bot. It gets identity
only from the five bot-set custom headers, validates the existing pending trace, retrieves governed
memories and submits its approved reply to that trace. It never posts to Discord or calls ElevenLabs.

`agent.yaml` loads `suspect_agent.graph:app`. Built-in memory is disabled. The graph clears all prior
messages at bootstrap, mandates an initial `search_memories` call and ends the platform session after
completion. Every subsequent turn reconstructs its context from current scoped records.

## Bot developer handoff

- Agent Engine project: `6af88435eee09d5d80696a95`
- Workspace: `ws-6af8c0b85e1f510db9b7a4f2`
- Agent Engine organization: `6aa1ed66371c6468b4a49ac1` (different from the Atlas organization)
- Atlas: existing `Cluster0`, game database `mystery`
- Embeddings: `voyage-4-lite`, 1024 dimensions everywhere
- Existing bot service-account client ID: `ae_sa_id_6af8b80300b256560b913302`

Store the service-account client secret only in the bot environment as `AGENT_ENGINE_CLIENT_SECRET`.
It is exchanged for a one-hour bearer token by `suspect_agent.client.invoke`. The workspace must never
receive the invocation credentials. `AGENT_ENGINE_ACCESS_TOKEN` is also supported for a short-lived
token supplied by the bot's token manager.

From the repository root, install `requirements.txt` and `pip install -e agents/suspect`.
Build each turn from the current Discord interaction and verified member role:

```python
from mystery_core.engine.context import TurnContext
from mystery_core.engine.turn import TurnService
from mystery_core.memory import Scope
from suspect_agent.client import invoke

ctx = TurnContext(Scope(case_id=case_id, team_id=verified_team), suspect_id,
                  f"t_{interaction.id}", "interrogate")
await traces.begin_turn(ctx.scope, ctx.turn_id, ctx.viewer, ctx.command, question,
                        engine_mode="hosted")
turns = TurnService(store, traces, model=llm_model)
prepared = await turns.prepare(ctx)
await invoke(prepared)
trace = await traces.get_trace(ctx.scope, ctx.turn_id)
if trace is not None and trace.status == "complete":
    # Post trace.reply only in the verified team's private channel.
    # Then generate the voice clip from that approved reply.
    ...
```

The invoke body is exactly `{"message": prepared.message}`. The helper sends Case-Id, Team-Id,
Suspect-Id, Turn-Id and Command under `X-Mdb-Agent-Engine-Custom-`, a bearer token, the pinned preview
Accept header and a 60-second server wait budget. It omits `X-Session-ID`. On a timeout inspect the
same trace: the original execution may still run. Preserve the turn ID on retry. Discord post
deduplication and channel authorization remain bot responsibilities.

For local mode, `await bot.local_runner.run(prepared)` returns the validated trace using the same
`TurnService`. To hand fresh evidence across vector-index lag, use
`await turns.prepare(ctx, fresh_memory_ids=[evidence.id])`; every ID is fetched under the suspect's
scope before its text enters a prompt. The hosted graph ignores stale body context and re-fetches
those recorded IDs. The game orchestrator still owns contradiction checks before invocation.

For buttons, call `mystery_core.engine.actions.use_offer(store, traces, clicker_scope, offer_id)`.
`clicker_scope` must come from the person clicking now. The helper rechecks the supporting memory and
atomically consumes the offer with the finding write. `performed` means this click won; `used` means
it already happened; `rejected` means the supporting lead no longer holds. It never posts a message.

## Seed and verify

Use the root `.env`, with the agreed embedding settings:

```powershell
python -m scripts.seed --case case_manor --teams A B --verify-model
```

The canonical IDs come from `mystery_core.memory.ids`. The verifier checks the actual vectors against
fresh Voyage document embeddings, not only their dimensions. For a confirmed old-model mismatch:

```powershell
python -m scripts.seed --case case_manor --teams A B --reembed --reembed-worlds --verify-model
```

This changes embeddings, preserving text, team permissions, status and correction lineage. The case's
current seed count is used; no code assumes 15 clues. Superseded seed copies intentionally fail the
pristine-world verification after play; do not re-seed to revive them.

## Deploy

Use the official Agent Engine CLI, sign in, and run commands from this directory. The current CLI
calls the old `--auto` build-and-deploy flag `--build`:

```powershell
agentengine auth login
agentengine init --org-id 6aa1ed66371c6468b4a49ac1 --project-id 6af88435eee09d5d80696a95 --workspace-id ws-6af8c0b85e1f510db9b7a4f2
agentengine atlas status
agentengine agent validate
agentengine deploy --build
```

Keep the existing Cluster0 link; do not accept defaults that create a cluster. The runtime needs
`MONGODB_URI`, `DB_NAME=mystery`, `EMBEDDING_MODEL=voyage-4-lite`, `EMBEDDING_DIMS=1024`,
`EMBEDDING_API_KEY`, `LLM_PROVIDER=openai`, `LLM_MODEL` and a real `LLM_API_KEY`.
Use `agentengine secret set NAME --workspace-scope` or a private stdin import restricted to those names.
The manifest permits OpenAI and Voyage egress and lets `invoke_llm` run in the tool sandbox.
The Python SDK/model-adapter pins are tested and predate the platform's seven-day package cutoff.

## Verification

```powershell
# From repository root
python -m pytest
python setup_db.py --db mystery_test
python -m pytest -m atlas tests/test_isolation.py tests/test_traces.py
python -m suspect_agent.smoke --mode local --team A
python -m suspect_agent.smoke --mode hosted --team A
```

The smoke commands print only trace/memory/offer IDs, and require both a complete trace and a retrieved
clue. They never print provider keys or private clue text. Use an explicit `--turn-id t_...` to inspect
or retry the same test turn. Unit tests cover missing headers, prompt isolation, confession gating,
live/retrieved action support, recantation, retries, atomic rollback, and replay without gameplay writes.
Atlas tests use `mystery_test` with fake embeddings and exercise real search filters and transactions.

References: [agent contract](https://www.mongodb.com/docs/agentengine/reference/agent-contract/),
[custom headers and sessions](https://www.mongodb.com/docs/agentengine/deploy/invoke-agent/),
[service-account authentication](https://www.mongodb.com/docs/agentengine/api-keys-service-accounts/).
