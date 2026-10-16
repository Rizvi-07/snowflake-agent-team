# Claude issue developer workflow

Implemented locally, disabled by default. No App, repository variable, secret,
issue or PR was created during implementation; no paid API call was made.
The owner must complete setup and approve live testing before enabling it.
Use the [combined owner runbook](setup.md) for exact setup, current GitHub token
behavior, bootstrap order and the smoke-test issue.

## Files and execution

- [Workflow](../../.github/workflows/claude-developer.yml): issue admission,
  model job, isolated publisher and final status reporting.
- [Controller](../../scripts/agents/issue_dispatch.py): live authorization,
  current-dev snapshots, branch ownership, scope validation and GitHub writes.
- [Offline tests](../../tests/test_agent_dispatch.py): no model or GitHub calls.
- [Developer procedure](developer-instructions.md): versioned task instructions;
  [AGENTS.md](../../AGENTS.md) and [CLAUDE.md](../../CLAUDE.md) are the entry points.

Only `issues` events of type `labeled` are registered. The exact label is `ai`
and the issue must be open. The repository variable `ENABLE_CLAUDE_AGENT` must
equal the literal string `true`; unset, `false`, `True` and `1` stay disabled.
PR/comment/dispatch events are not registered and the controller rejects them.

The admission job uses a read-only default workflow token, with no agent
secrets. It checks the event sender matches the human label actor, queries the
actor's current repository permission and accepts only write/maintain/admin.
On reruns it also checks the current rerun actor's permission. Issue authorship,
author association, bot allowlists and the presence of a label never substitute
for that check. It verifies the live issue, label and approved title/body.
The issue form's four required sections must be present and nonempty.

Admission records the current remote `dev` commit and any verified owned open
PR in trusted job outputs. The model job starts at that dev commit, with local
branch `agent/issue-<number>`. On retry it applies the existing owned PR's diff
onto current dev; conflicts require human resolution. The controller comes
from the workflow's trusted default-branch revision in a separate checkout.
Instructions come from the dev base, not an issue attachment or Wiki.

Permission, issue revision, dev commit and branch ownership are rechecked
before App credentials, immediately before `ANTHROPIC_API_KEY`, and before
publication. Issue text is serialized to `.agent-task/issue.json`. The official
action receives a static prompt to read that file as untrusted task input;
event bodies/titles are never embedded in a shell program.

Claude uses the official `anthropics/claude-code-action`, pinned to reviewed
`v1` commit `374f254f5efaf968abfe15d399fd90972e8138b9`. The action's own runtime
installation remains upstream behavior; pinning the wrapper is not a complete
lock of every downloaded dependency. The model gets a repository-only **read**
App token and local file/shell tools. It implements and runs relevant offline
checks, and writes a structured summary, acceptance evidence and untested work.
The collector also runs both repository unittest suites, `pip check` and
`git diff --check`; a failure or non-success action conclusion stops publication.

Only a bounded JSON artifact of changed text files/report/check results is
transferred. The fresh publisher checks out trusted controller code, validates
the artifact again and executes none of its code. It uses a separate App write
token and GitHub's Git Data API to update only the canonical issue branch.
New PRs target `dev`, include `Refs #<issue>` and the issue ownership marker,
changes, verification, untested work and the resulting commit.

For an owned open PR, it preserves history with a new non-force commit and
adds a report comment to the same PR, preserving its existing description.
It never merges or writes a `dev`/`main` ref. Existing CI validates the PR;
Codex and a human review. **PR Dev validation required** and the existing three
database workflows are unchanged. Merges into dev/main remain human decisions.

## Selected GitHub App and owner setup

Use an **owner-controlled custom GitHub App** with the official Claude action.
No official Claude App installation, PAT or subscription OAuth token is selected
for this workflow. The supplied App installation tokens are mandatory: there
is no fallback to a PAT or `GITHUB_TOKEN` for Claude or publication. The default
workflow token is used only for read-only admission/rechecks and Actions' own
checkout/artifact plumbing.

Owner actions, not performed here:

1. Create and install a custom App for only `Rizvi-07/snowflake-agent-team`.
   Grant repository **Contents: read/write**, **Pull requests: read/write**,
   **Issues: read/write**, and automatic **Metadata: read**. The selected token
   configuration requests Issues write for report comments (GitHub also accepts
   PR write for PR comments). Grant no Workflows,
   Actions, Administration, Deployments, Environments or Checks write access.
   No webhook subscription is needed: the repository Actions workflow receives
   issue events. The App needs no bypass of protected branches or review rules.
2. Privately configure Actions variables `CLAUDE_AGENT_APP_CLIENT_ID` (the App's
   Client ID) and `CLAUDE_AGENT_APP_SLUG` (its slug without `[bot]`). Generate the
   App's private key and store it only as secret `CLAUDE_AGENT_APP_PRIVATE_KEY`.
   The pinned `actions/create-github-app-token` creates separate read/write
   installation tokens restricted to this repository, and revokes them on job
   completion. The controller checks the minted App slug and repository scope.
3. Store the Anthropic API credential only as Actions secret
   `ANTHROPIC_API_KEY`. Do not configure an OAuth alternative or paste values
   into repository files, issues or chat. Review the API account budget and
   GitHub Actions minutes before a live test.
4. Keep `ENABLE_CLAUDE_AGENT` unset or `false`. Review and merge the workflow,
   controller, instructions and tests into `dev`, then release through the
   human-reviewed `dev` to `main` path. Issue-event workflows run from default
   branch `main`; the development checkout remains current `dev`.
5. Verify human-reviewed branch rules, the required check and environment secret
   placement. Agent jobs reference neither `development` nor `production` and
   contain no database secret mappings. Avoid inherited credentials on runners;
   this workflow uses fresh GitHub-hosted runners, not a persistent local runner.
6. With owner approval, set `ENABLE_CLAUDE_AGENT=true`, create a small scoped
   task using the issue form, and have a human with write/maintain/admin apply
   `ai`. Verify App-authored PR creation, automatic downstream CI, same-PR
   retry behavior and failures. This live task will incur API/Actions usage.
   Set the variable back to `false` if setup or validation is incomplete.

The selected custom App follows the
[official action's custom-App setup](https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md#using-a-custom-github-app)
and [Claude GitHub Actions documentation](https://code.claude.com/docs/en/github-actions).
Explicitly passing the installation token avoids relying on the default
workflow token's restricted downstream event behavior; see
[GitHub workflow trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
Do not use `/install-github-app` to silently create a different workflow or
credential setup. Codex review settings are a separate owner-controlled setup.

## Enforced limits and instruction limits

Technically enforced by workflow/controller and token separation:

- Default-off exact variable gate; issue-only open `ai` events; live label and
  rerun actor permissions before the API secret is referenced.
- Per-issue concurrency, no automatic cancellation, queued events, admission
  timeout 5 minutes, model job 30 minutes, publisher 10 minutes, reporting 2
  minutes. Claude has at most 20 turns and a CLI budget of USD 5 per invocation.
  These bounds do not guarantee an exact invoice or stop already completed writes.
- The model is supplied only a read installation token; the fresh publisher
  mints a separate write token and executes no model/generated code. Tokens
  are App-scoped to one repository; no Snowflake credentials
  or deployment environments are passed to either job.
- Publication paths: Python under `app/`, `src/`, `ingestion/`, `scripts/`,
  `tests/`; SQL/YAML under `dbt/models/` and `dbt/tests/`; CSV/JSON under
  `sample_data/`; Markdown/RST/text under `docs/`; root `README.md`.
- Exceptions denied even inside those directories: all `docs/agents/`,
  `scripts/agents/`, `AGENTS.*`/`CLAUDE.*`, hidden paths, known workflow/agent
  tests and current files containing authentication/lifecycle/deployment guards
  (`ingest_products.py`, `run_data_flow.py`, `ci_pr_lifecycle.py`, connection
  checks). Auth/permission/deploy path components are also denied. Root config,
  dependency files, `.github/`, dbt macros and keys are outside the allowlist.
- No traversal, duplicate paths, symlinks or binary contents; publication creates
  regular UTF-8 text blobs only. Limits: 50 files, 1 MB/file, 5 MB total, plus
  basic credential-pattern rejection. This is not a comprehensive secret scanner.
- Canonical App-owned open PRs only, all visibility failures rejected, old head
  and dev snapshots checked, non-force branch updates, and no merge endpoints.

Instruction-based or requiring human/CI assessment:

- Understanding acceptance criteria, selecting meaningful checks, truthful
  model-authored reports, prompt-injection resistance and detecting a semantic
  authentication/deployment change hidden in an otherwise allowed code file.
- The local/shell tools are not a network or operating-system sandbox. A read
  token rejects GitHub writes made with that token; it does not prevent arbitrary local commands or
  prove no attempted SQL/network access. Database credentials are absent, and
  instructions forbid SQL; CI alone owns actual database checks/deployment.
- The App signing key is used to mint the read token in the same job that later
  runs Claude and proposed tests. The fresh publisher is not a hardened boundary
  against hostile code on that privileged model runner: do not assume process
  memory or credentials used earlier in its job are isolated from shell access.
  Treat maintainer-admitted tasks and that runner as trusted; this initial phase
  is not suitable for executing untrusted code with adversarial isolation needs.
- The model runner can edit its local files and test code. The independent
  publisher limits what is published, but model-job check reports are not an
  attestation. Downstream CI and human review remain necessary. Existing PR CI
  executes the proposed application/dbt code with its own isolated Dev identity.
  Review credential-sensitive behavior before authorizing that CI as needed.

Control changes rejected by these limits require a separate human-maintained
task. Broadening a prompt or applying `ai` cannot override the path policy.

## Retry, failure and recovery

Missing secrets, permission/API errors, malformed input, model failure/turn
limit, protected changes, failed offline checks and publication failures yield
a nonzero result and a final workflow status summary. Raw child/API diagnostics
are suppressed; model output is not uploaded as logs. No automatic repair loop,
auto-merge or database deployment is started by this workflow.

Retries reuse only the same App's marked canonical open PR into dev. Other
human/foreign PRs referring to the issue, duplicate PRs, closed attempts and a
branch left without an owned open PR block work for human inspection. A missing
PR after a successful branch write is a partial publication, not permission to
overwrite it. Inspect and resolve that state manually before relabeling/rerun.
Existing PR conflicts with current dev also require human resolution.

Turning the variable off stops new admissions. Removing `ai`, editing/closing
the issue or changing actor permissions fails later live rechecks. In-flight
GitHub calls are not atomic with those checks; cancellation cannot undo a ref
update, paid work or a PR already created. A failed publisher may leave Git
objects, a branch update or a PR; no reset, force push or automatic deletion is
used for recovery. Revoking the App/API credential is an owner emergency action.

## Offline checks and remaining live verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent_dispatch.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Observed on 2026-10-16: 28 agent-specific tests passed; the complete suites
passed with 25 ingestion and 88 pipeline/agent tests (113 total). Dependency
and whitespace checks passed. Checks also passed for local documentation links,
issue-form fields and unchanged database workflows/config/runtime pins. Tests
include a real temporary Git retry fixture and execution of the workflow's
failure-report program; GitHub/model interactions remain mocked.

SHA256-verified actionlint 1.7.12 passed with shellcheck/pyflakes disabled and
only its known unsupported `concurrency.queue` diagnostic excluded. This is a
static check, not a GitHub run. See the
[upstream compatibility issue](https://github.com/rhysd/actionlint/issues/657).

No setup, model invocation, App installation, secret/variable update, push or
PR creation is part of local verification. A real owner-approved GitHub run must
still confirm custom-App endpoint access and bot attribution, CLI/action output,
artifact transfer, automatic PR CI, retries and partial failures. The existing
required check must continue to gate human merges. Static/unit checks do not
claim those live outcomes.
