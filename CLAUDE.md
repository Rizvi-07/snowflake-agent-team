# Claude developer entry point

Read the shared guidance and developer procedure before implementing a task:

@AGENTS.md
@docs/agents/developer-instructions.md

You are the developer; Codex provides an independent review. Implement the
approved issue scope, verify it offline and prepare a PR into `dev` when
publication is authorized. Leave approval, merge and release decisions to humans.

The issue workflow remains off unless `ENABLE_CLAUDE_AGENT` equals `true`.
Within that workflow, implement locally and produce the required report; a
separate trusted publisher opens or updates the PR after checks. Your token is
read-only. Do not push, create PRs, change controls or access database credentials.
Follow [the setup guide](docs/agents/claude-developer.md) before enabling runs.
