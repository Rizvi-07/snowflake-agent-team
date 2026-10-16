# GitHub Issues to Claude development and Codex review

Start with the [owner setup runbook](setup.md): disabled bootstrap through
feature → dev → main, custom App/secret configuration, enablement, shutdown,
logs/API usage and the documentation-only live smoke-test issue.

## Current phase: Claude development and advisory Codex review, default off

The [Claude issue workflow](claude-developer.md) is implemented with the official
Anthropic action, live actor admission and a separate App-authenticated publisher.
It runs only when `ENABLE_CLAUDE_AGENT` equals the literal string `true`.
Keep that variable unset/false until owner setup and live verification are complete.
The [Codex reviewer](codex-reviewer.md) is also implemented and gated separately
by `ENABLE_CODEX_REVIEW=true`. It loads trusted-base instructions, reviews exact
head text without executing PR code and publishes through a separate validator.

No App was installed and no variable, secret, issue, PR or remote setting was
created or changed. No paid API call was made. External integration settings
have not been inspected; existing database workflows continue unchanged.

Instruction sources:

- [AGENTS.md](../../AGENTS.md): shared boundaries and repository review rules.
- [CLAUDE.md](../../CLAUDE.md): Claude entry point importing the shared and
  [developer instructions](developer-instructions.md).
- [Reviewer instructions](reviewer-instructions.md): Codex's independent role.
- [Development task form](../../.github/ISSUE_TEMPLATE/development-task.yml):
  goal, scope, acceptance criteria and verification instructions, all required.

These files travel with the code. Load the trusted base revision explicitly in
future agent prompts; do not assume a role document is loaded just because it
is linked. Wiki pages may link to the files on `dev` or a specific commit; no
Wiki checkout, duplicated instructions or Wiki write is needed.

## Intended enabled flow

1. A maintainer applies `ai` to an open issue after reviewing its task fields.
2. A guarded dispatcher verifies live maintainer permission and issue state,
   snapshots the issue/instructions and records current remote `dev` as base.
3. Claude implements on `agent/issue-<number>` with a read-only token.
   After offline checks and scope validation, a separate trusted publisher opens
   or updates the same-repository PR into `dev`. Repeated events reuse verified
   issue-owned work, rather than overwriting branches or duplicating PRs.
4. Existing PR CI validates isolated `AGENTWAREHOUSEDEV.PR_<number>_BRONZE`,
   `PR_<number>_SILVER` and `PR_<number>_GOLD`. Here the number is the **PR**
   number, which may differ from the issue number. Codex reviews the PR.
5. A human reviews the changes, CI and findings and chooses whether to merge.
   Merging into `dev` runs the existing shared Dev workflow.
6. A human-reviewed release PR from `dev` into `main` remains the production
   path. Its merge runs the existing `environment: production` workflow.

Agent jobs run offline and receive no Snowflake credentials or deployment
environment access. The existing database CI jobs remain separate; they retain
their own credentials, isolation, cleanup and failure behavior. Review any
sensitive workflow/credential changes before authorizing credentialed CI.
Keep **PR Dev validation required** exactly as named and do not let an agent
review replace it or a human merge decision.

## Observed repository baseline

Inspected on 2026-10-16 after `git fetch origin`. The working tree was clean on
`feat/bronze-products` at `e5e0139`; no existing root/nested `AGENTS.md` or
`CLAUDE.md` was found. The new `feat/github-ai-agents` branch starts at fetched
`origin/dev`, `c7bb808` (products pipeline merge). The previous branch is intact.
Fetched `origin/main` is `bf38516` (products production release), two merge
commits ahead of `dev` with an identical file tree. No protected branch was
moved, and release commits were not copied into this feature branch.

The three existing workflows are `pr-dev-check.yml`, `dev-check.yml` and
`prod-deploy.yml`; they are unchanged in this phase. PR validation and cleanup
share a serialized PR lifecycle, the required gate rejects unsuccessful
validation, and release PRs into `main` must originate from `dev`.
The owner reports the production workflow succeeded. That result was not
independently retrieved during this local inspection. The older
[products review guide](../products_pipeline.md) records its own historical
local checks, not the current release status.

## Implemented files and remaining setup

- [`.github/workflows/claude-developer.yml`](../../.github/workflows/claude-developer.yml):
  default-off issue workflow, per-issue serialization, bounded model job,
  isolated publication and failure summary.
- [`scripts/agents/issue_dispatch.py`](../../scripts/agents/issue_dispatch.py):
  live admission, issue snapshots, current-dev preparation, branch/PR ownership,
  file allowlist and non-force App publication.
- [`tests/test_agent_dispatch.py`](../../tests/test_agent_dispatch.py): offline
  controller and workflow regression tests.
- [Owner setup and enforcement boundaries](claude-developer.md): selected custom
  App permissions, API secret, exact variables, technical/instruction limits,
  retries and recovery. No personal-token/default-token publication fallback.

Owner configuration is still required: `CLAUDE_AGENT_APP_CLIENT_ID`,
`CLAUDE_AGENT_APP_SLUG`, `CLAUDE_AGENT_APP_PRIVATE_KEY`, `ANTHROPIC_API_KEY` and,
only after setup, `ENABLE_CLAUDE_AGENT=true`. The App is installed only on this
repository, without protected-branch bypass. The issue form does not apply `ai`.
Workflow/form/instructions must reach default branch `main` through normal human
review and the dev-to-main release path. Agent tasks still start from `dev`.

Codex review uses [`.github/workflows/codex-review.yml`](../../.github/workflows/codex-review.yml),
[`scripts/agents/review_pr.py`](../../scripts/agents/review_pr.py), the trusted
configuration/schema in `.github/codex/` and
[`tests/test_codex_review.py`](../../tests/test_codex_review.py).
Owner setup requires `OPENAI_API_KEY` and, for Claude-authored PRs, the exact
`CLAUDE_AGENT_APP_SLUG` plus numeric `CLAUDE_AGENT_APP_BOT_ID`. Keep
`ENABLE_CODEX_REVIEW` unset/false until setup completes. See the
[reviewer guide](codex-reviewer.md) for enforcement, failure/recovery boundaries
and verification. No native automatic Codex review or bot retry loop is added.

## Recovery boundaries

Turning the enablement gate off stops new admissions; removing `ai` or
closing the issue must stop future writes after live rechecks. These actions
cannot undo an existing commit/PR or guarantee an in-flight push stops. Inspect
partial branches/PRs before retrying. Only humans resolve protected-branch
history, merge, release or delete task work. Database recovery and detached SQL
remain the existing CI lifecycle's responsibility, never the agent job's.

## Local verification of this phase

On 2026-10-16, both suites passed: 25 ingestion tests and 88 pipeline/agent
tests (113 total, including 28 new agent tests). Commands:
`python -m unittest discover -s ingestion -p 'test_*.py' -v`
and `python -m unittest discover -s tests -p 'test_*.py' -v`, using the existing
`.venv` Python. `python -m pip check` and `git diff --check` also passed.
The suite includes offline dbt parsing/compilation, in-memory SQL checks, actual
required-gate shell execution, lifecycle mocks and Git safety checks. dbt emits
an upstream logger `ResourceWarning`; the suite still exits successfully.

Additional local checks passed for issue-form YAML, all four required fields,
unique field IDs, absence of automatic labels, local links and Claude imports.
The existing three database workflows match `origin/dev` without changes.
The Claude workflow now exists but remains default off; its new offline results
are recorded in the workflow guide. No Snowflake SQL or live agent task was executed.
GitHub form rendering, integration access, PR CI/review delivery and admission
controls still need verification in later setup; no new GitHub run is claimed.

The subsequent advisory reviewer phase passed **25 new reviewer tests**, and
the full suites now pass **25 ingestion + 113 pipeline/agent = 138 tests**,
including the actual publisher JavaScript with mocked GitHub endpoints and
temporary Git snapshots. Dependency/whitespace and workflow lint checks passed;
see the [reviewer guide](codex-reviewer.md#offline-verification-and-live-setup-checks)
for exact commands, tooling caveats, observed results and remaining live checks.

The subsequent integration review adds twelve regression tests and the
[combined owner setup guide](setup.md). Its commands passed with **34 Claude
tests, 31 Codex tests, and 150 unique tests across the full suites**. The guide
records current GitHub App/default-token behavior and separates offline evidence
from the live smoke test, which has not been executed.

Final review fixed the credential-publication gap, ignored transient agent
runtime state and added index-aware secret checks. Latest local results are
**36 Claude, 31 Codex and 155 unique tests across the full suites**, with no
recognized token/private-key values found in Git candidates. See the setup guide
for the documented Claude runner trust limitation and remaining live checks.
