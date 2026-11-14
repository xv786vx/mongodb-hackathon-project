# mongodb-hackathon-project

Death at Halloway Manor is a Discord mystery demonstrating governed AI memory in MongoDB Atlas.
Each detective team has a separate world; suspect tools retrieve only active, unexpired memories
authorized for that team and character. Correcting a clue revokes the action it supported.

The hosted suspect agent, shared reply validation, local fallback and bot request adapter are implemented.
Start with the [agent setup and bot handoff](agents/suspect/README.md), then [CONTRACTS.md](CONTRACTS.md)
for the shared interfaces. [ARCHITECTURE.md](ARCHITECTURE.md) describes the complete game, including
Discord command handlers, contradiction orchestration and voice delivery owned by the other integrations.

Registered workspace: `ws-6af8c0b85e1f510db9b7a4f2` in project `6af88435eee09d5d80696a95`.
Use `voyage-4-lite` at 1024 dimensions for the seeder, bot and hosted agent.
