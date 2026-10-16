# Agent integration smoke test

This page exists to exercise the issue-driven agent workflow end to end with a
documentation-only change. It also summarizes the human merge boundary that
every change in this repository follows.

## Development starts from `dev`

Work begins from the current remote `dev` branch on a dedicated feature
branch. Agent tasks use `agent/issue-<number>`; humans may use any feature
branch name. Nobody pushes directly to `dev` or `main`.

## CI validates, a human reviews and merges

Each change reaches `dev` through a pull request. Existing CI validates the
PR's changes, including the isolated **PR Dev validation required** check.
Codex may add an advisory review, but a human reviewer decides whether the PR
is ready and performs the merge. Agents never approve or merge their own work
and never enable auto-merge.

## Production releases use `dev` → `main`

Production releases are separate pull requests from `dev` to `main`. They also
require human review and merge before the production deployment runs.

See [AGENTS.md](../AGENTS.md) and the [README](../README.md) for the full
repository rules.
