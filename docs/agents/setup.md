# Owner setup and verification

This is the owner runbook for `Rizvi-07/snowflake-agent-team`. Implementation is
local on `feat/github-ai-agents`; both agents are **disabled by default**.
No App installation, settings/secret change, issue, PR, API/model invocation or
Snowflake SQL is part of the offline verification described below. Public
documentation and release files were read to verify guidance and obtain the
syntax checker. Follow these setup steps manually after reviewing the code.

The implementation details and recovery limits are in
[Claude developer](claude-developer.md) and [Codex reviewer](codex-reviewer.md).
Repository files, including [AGENTS.md](../../AGENTS.md), are the versioned
instruction source. No Wiki checkout is required.

## Bootstrap: feature to dev to main, agents off

1. In repository **Settings → Secrets and variables → Actions → Variables**,
   leave both enablement variables unset or set explicit repository values
   `ENABLE_CLAUDE_AGENT=false` and `ENABLE_CODEX_REVIEW=false`. Explicit false
   overrides any inherited organization value. Do not enable either yet.
2. Review all local changes, including untracked files, on
   `feat/github-ai-agents`. Commit and push that feature branch yourself; open a
   PR with **base `dev`**, compare `feat/github-ai-agents`. Do not push directly
   to `dev` or `main`. Run the offline commands below before publication.
3. Observe existing PR Dev validation and the exact required check
   **PR Dev validation required**. A human reviews and merges into `dev`.
   The existing push workflow then validates the shared Dev pipeline.
4. Open the normal release PR with **base `main`**, compare **`dev`**. Observe
   isolated Dev validation, review and merge as a human. Main-bound PRs from a
   feature branch remain rejected. The existing main push workflow runs in
   `environment: production`; this normal bootstrap release can execute the
   production pipeline even though agent automation is off. Review/approve it
   under the existing release process. No deployment behavior is changed here.
5. Confirm the workflow/controller/instruction/configuration files reached both
   branches. In **Settings → General**, verify the repository's default branch
   is `main`; this guide assumes that existing setting. Do not switch the default
   branch as an agent setup shortcut. Only then configure the App and API keys.

**Issue-triggered workflows must exist on default branch `main`.** Having
`claude-developer.yml` only on the feature branch or `dev` cannot enable issue
label runs. Its trusted controller comes from the default-branch workflow
revision; Claude's task workspace still starts at current remote `dev`.
Codex uses trusted target-branch workflow/instructions for PRs into dev/main.
GitHub documents the default-branch requirement for
[`issues` events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#issues).

## Install the chosen GitHub App

This implementation selects an **owner-controlled custom GitHub App**, used
with the official Anthropic action. The owner registers and installs it; no
installation was performed by the coding agents. The reviewer does not need
another App. Use an App dedicated to this repository, with no branch-protection
bypass and no organization/account permissions.

1. Sign into the account that will own the App (normally `Rizvi-07`). Open
   **Settings → Developer settings → GitHub Apps → New GitHub App**. For an
   organization owner, use that organization's Settings instead.
2. Choose a unique display name, for example `snowflake-agent-team-developer`.
   Set Homepage URL to `https://github.com/Rizvi-07/snowflake-agent-team`.
   Leave Callback URL, Setup URL, OAuth authorization and device flow unused;
   this integration uses installation authentication, not user OAuth.
3. Under Webhook, clear **Active**. Do not add an endpoint or event subscriptions:
   GitHub Actions itself receives issue/PR events. Under installation visibility,
   choose **Only on this account**, then create the App. See GitHub's
   [registration instructions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).
4. In **Permissions & events → Repository permissions**, select:

   - **Contents: Read and write** — Git blobs, trees, commits and issue branch refs.
   - **Pull requests: Read and write** — create/reuse PRs into dev.
   - **Issues: Read and write** — the configured publication token also requests
     this permission for issue/PR report comments.
   - **Metadata: Read-only** — automatically included by GitHub.

   Leave other permissions at no access, including Actions, Workflows,
   Administration, Deployments, Environments and Checks. These are the selected
   workflow permissions; PR comments can also be authorized by PR write access.
   If the App installation lacks a permission requested by the token action,
   minting fails rather than falling back to a broader token.
5. In the App's **General** page, record its **Client ID**. Click **Generate a
   private key**, keep the downloaded key outside the repository, and transfer
   its complete PEM text privately to the Actions secret named below. This is
   the App signing key, a separate credential from the Snowflake key.
6. Open **Install App**, select the repository owner, choose **Only select
   repositories**, select **snowflake-agent-team**, and install. Verify this is
   the only selected repository. When changing permissions on an existing App,
   approve the corresponding installation update. GitHub describes these steps
   in [Installing your own App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app).
7. Record the exact slug from `https://github.com/apps/<slug>`. The bot login is
   `<slug>[bot]`. To obtain its public numeric bot user ID, the owner can open
   `https://api.github.com/users/<slug>%5Bbot%5D` in a browser and record `id` after
   verifying `login` and `type: Bot`. This is an owner configuration lookup,
   **not** a command in the offline tests. The bot user ID differs from App ID,
   installation ID and Client ID. No key is needed for that public lookup.

## Configure Actions secrets, variables and the label

Open repository **Settings → Secrets and variables → Actions → Secrets → New
repository secret** for each of:

- `CLAUDE_AGENT_APP_PRIVATE_KEY`: the complete App signing key from the previous
  step, including its real newlines. Do not encode it, print it or put it in Git.
- `ANTHROPIC_API_KEY`: an owner-controlled Anthropic API key from the correct
  Claude Console workspace with API billing and access to Claude Code's default
  model. Use a dedicated key so usage can be attributed to this integration.
- `OPENAI_API_KEY`: an owner-controlled OpenAI API project key with access to
  the Codex CLI's default model and Responses API. Use a dedicated project/key
  with suitable usage controls; it need not be an organization admin key.

Use each provider's signed-in API console to create/copy the key, then paste
only into the private secret form. These workflows use API credentials, not
chat subscription OAuth. Do not add provider admin/usage-report credentials.
GitHub's [secret setup](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
describes repository secret creation. Existing Snowflake secrets stay in their
existing CI environments; agent jobs reference neither deployment environment.

In the **Variables** tab, use **New repository variable** to add:

- `CLAUDE_AGENT_APP_CLIENT_ID`: exact Client ID from the App General page; used
  by pinned `actions/create-github-app-token` v3. No App ID or installation-ID
  variable is read by this implementation.
- `CLAUDE_AGENT_APP_SLUG`: exact slug, without `[bot]`, URL or wildcard.
- `CLAUDE_AGENT_APP_BOT_ID`: numeric GitHub bot user ID, as decimal text.
- `ENABLE_CLAUDE_AGENT`: keep `false` through configuration.
- `ENABLE_CODEX_REVIEW`: keep `false` through configuration.

All three identity values must identify the **same** installed App. The reviewer
checks bot login, type and numeric ID, not just a branch name or bot suffix. For
human-only review both slug and bot ID may be absent; when Claude is configured,
set both before enabling Codex. No model variable is currently read. Neither
workflow explicitly selects a model: the action/CLI defaults apply and may
change. There is no `CLAUDE_MODEL` or `CODEX_MODEL` override in this code.
A fixed model would require a separate human-maintained workflow change.
Claude uses `--max-turns 40`, an estimated USD 5 run budget and a 30-minute job
timeout. The turn allowance was raised from 20 as a bounded workaround for
issue #19 and upstream #1795; it loosens execution limits, does not fix the
counting discrepancy, and does not guarantee success. See the
[diagnosis and release/retry procedure](claude-developer.md#issue-19-bounded-turn-limit-workaround).
The pinned action v1.0.251 selects CLI 2.1.301. Codex pins CLI/proxy `0.163.0`
with a 20-minute review job, text/schema
bounds, and no configured hard dollar cap. Check provider billing controls
before enabling; timeouts and output bounds are not a spending guarantee.

Under **Issues → Labels → New label**, create the exact lowercase name **`ai`**,
description `Maintainer-approved task for the Claude developer`, with any color.
Creating the label does not start work. The issue form does not apply it.
A human with current write/maintain/admin access must apply it to an **open**,
triaged issue with all four task fields. A trusted author or preexisting label
does not authorize another actor. See [GitHub label management](https://docs.github.com/en/issues/using-labels-and-milestones-to-track-work/managing-labels).

## GitHub authentication and downstream checks

The default workflow token is read-only for admission/checkout. Claude receives
a repository-scoped **read-only App installation token**. A fresh publisher
job independently mints an **App installation write token**, validates the text
artifact and uses it for Git Data API branch updates and opening the dev PR.
It has no model key and runs no proposed code. Both tokens are installation
tokens, not personal tokens. The token action revokes them in its post-job step;
GitHub installation tokens otherwise expire after one hour. No token is passed
between jobs. See the [official token action](https://github.com/actions/create-github-app-token).

Claude's shell/test runner is not a hardened security sandbox. The App signing
key is used earlier in that same job to mint its read token, so a separate
publisher does not prove isolation from hostile code or process-memory access.
Only use maintainer-triaged tasks on the disposable runner; the initial design
does not promise adversarial code isolation. Codex has separate privilege and
command-tool restrictions. Secret scanning covers recognizable formats, not
every possible credential or transformed value.
Transient `.agent-task/`, `.agent-control/` and `.claude/` runtime directories
are Git-ignored as well as excluded from the Claude publication artifact.

GitHub's current documentation says App installation-token events can start
downstream PR workflows automatically. It also now documents an exception for
`GITHUB_TOKEN`: PR `opened`, `synchronize` and `reopened` can create runs awaiting
approval by a user with write access. Other PR activity types, including
`closed`, remain suppressed under that default token. This implementation
deliberately uses the App token for branch/PR publication, retaining automatic
CI eligibility and PR close cleanup events. It does not dispatch workflows or
substitute a PAT/default token. This documents expected platform behavior, not
an observed repository run.
[GitHub: triggering a workflow from a workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).

The Codex generator has only contents/PR read permissions, read-only sandboxing
and privilege reduction. Its fresh publisher gets `GITHUB_TOKEN` with only PR
write access to post/update a comment. Comment publication does not push code
or trigger this integration again: the workflows do not subscribe to comment
events. There is no automatic Claude/Codex retry loop. Adding a bot to reviewers
does not grant branch bypass or authorize merges.

Repository/organization Actions policies, allowed-action rules and environment
approvals can still block or delay otherwise eligible CI. Keep default token
permissions read-only, preserve existing branch rules and
**PR Dev validation required**. Do not add either advisory agent workflow as a
required check or enable automatic approval/merge. Do not grant workflow-write
permission or protected-branch bypass to make an App run succeed.

## Enable after bootstrap and run the smoke test

After both branches contain the reviewed files, the three secrets and identity
variables are configured, and Actions/provider access is ready:

1. Set `ENABLE_CODEX_REVIEW` to literal **`true`** first, then
   `ENABLE_CLAUDE_AGENT` to literal **`true`**. These changes are manual owner
   actions; no setup step above enables automation. Keep other native Codex or
   Claude automations off to avoid duplicate work.
2. Create one issue using **New issue → Development task**, copying the title
   and fields below. Review it before applying `ai`. Opening the issue alone
   does not invoke Claude; the maintainer's label event does.
3. Apply `ai` as the maintainer. Observe the results listed below. Do not merge
   the smoke PR into dev/main. Close it after inspection to exercise PR cleanup;
   remove `ai` and close the issue manually when finished. This live smoke test
   may consume provider API usage and Actions minutes; it was not run here.

### Documentation-only smoke issue

Title: **[Task]: Document the human merge boundary (agent smoke test)**

Copy these four field values into the form (the form supplies their headings):

```markdown
### Goal
Add a tiny document explaining the repository's human merge boundary.

### Scope
Create only docs/agent-smoke-test.md. Do not edit existing files, workflow or
agent controls, models, sample data, credentials or dependency configuration.
Do not execute database commands. Open a PR into dev; do not merge it.

### Acceptance criteria
- The new file has the heading "Agent integration smoke test".
- It states that work starts from dev on a feature branch, existing CI validates
  PR changes, and a human reviews and merges. Production remains dev to main.
- The PR links this issue and describes actual offline checks and gaps.
- Only docs/agent-smoke-test.md changes.

### Verification instructions
Run git diff --check and the relevant repository offline unittest checks.
Inspect the new document and report exact results; do not claim live CI passed.
The owner will observe PR Dev validation and the Codex advisory comment. Keep
the PR unmerged; existing CI may use isolated Dev schemas, never Prod for this PR.
```

Expected **live** observations, each to be checked rather than assumed:

- **Claude issue developer** runs from main, admits the maintainer and open
  issue, starts from current dev, validates offline and publishes
  `agent/issue-<issue number>` as the configured App, with one PR into dev.
  PR-body `Refs #<issue>` links the issue; it does not promise automatic closure.
- **Snowflake PR Dev lifecycle** runs for that App-created PR and validates the
  products graph plus `connection_check` in Dev. Its three schemas are
  `AGENTWAREHOUSEDEV.PR_<PR number>_{BRONZE,SILVER,GOLD}`. The issue number may
  differ from the PR number. **PR Dev validation required** reports success only
  when database validation succeeds. Agent jobs themselves do not execute SQL.
- **Codex advisory PR review** admits this exact App identity, reviews the exact
  current head and publishes one marked advisory comment containing that SHA.
  A successful review may report no findings. **Review failed** is a failed
  smoke result, not a clean review; inspect the corresponding run.
- After the first run completes, a deliberate rerun while the issue is still
  open/unchanged and labeled must reuse the same owned open PR. It must not
  overwrite unrelated work or create a second issue PR. New commits should
  produce a new head-specific review in the same identifiable comment.
- Closing the PR as a human queues cleanup of only its three temporary Dev
  schemas. Observe cleanup completion; runner cancellation alone does not prove
  detached Snowflake work stopped. No main push, production deployment or
  production-data change should result from this unmerged smoke PR. A docs-only
  PR still runs existing database CI, so temporary Dev writes are expected.

If state changes invalidate a queued run, inspect its safe error before retrying.
Closed issues, revoked permissions, changed issue text, foreign/orphan branches,
duplicate PRs or conflicts require re-triage/human recovery. Reopening an issue
or leaving `ai` in place is not a new label event: review the task, remove/reapply
`ai` as a maintainer, or use a valid completed run's rerun controls. Do not rerun
a stale-head Codex event expecting it to review a newer SHA; use a fresh PR event.

## Disable either agent quickly

Set the respective repository variable explicitly to **`false`**:
`ENABLE_CLAUDE_AGENT` or `ENABLE_CODEX_REVIEW`. Disable both to stop both paths.
This blocks new admissions and preserves any work already published. For urgent
shutdown, also open **Actions → the specific agent workflow → … → Disable
workflow**, and cancel its queued/running runs. Do not disable the existing PR
database lifecycle workflow, which is responsible for cleanup.

Configuration already resolved by an in-flight run may remain true. Removing
`ai` or closing the issue also blocks Claude's later live rechecks, but neither
flag changes nor cancellation atomically stops a write/API call already sent.
Inspect partial branches/PRs; do not reset, force-push or automatically delete
work. If credentials must be contained, suspend/remove this App's repository
access and revoke the affected provider API key in its console. Deleting an App
signing key stops future token minting but is not a reliable revocation of an
already minted token. Paid requests/comments already completed cannot be undone.
Only humans decide whether to merge, retry, resolve conflicts or release.

## Logs and provider API usage

In **Actions**, filter by **Claude issue developer** or **Codex advisory PR
review**, then open the run/attempt and failed job/step. For Claude, inspect
`authorize`, `develop`, `publish`, then the `report` job summary. For Codex,
inspect `authorize`, `review`, `publish`; comments include the head SHA and
Actions run link. A skipped flag gate is not a model invocation. A missing-key
failure is not a completed review. Raw model output/report artifacts may contain
task source: keep access restricted, retain the one-day artifact setting and do
not paste logs wholesale into issues or chat. Never log key values or env dumps.

For the smoke PR, inspect **Snowflake PR Dev lifecycle** separately. Observe the
exact required check, isolated Dev validation and close cleanup. Production
status belongs to **Snowflake production deployment**, main, and its production
environment; it is not an agent success indicator.

- Anthropic: sign into the correct [Claude Console](https://platform.claude.com),
  open [Usage](https://platform.claude.com/usage) and
  [Cost](https://platform.claude.com/cost), and filter the workspace/API key and
  run time window. Review billing/limits in that console before the first paid
  test. These dashboard locations are described in Anthropic's
  [usage documentation](https://platform.claude.com/docs/en/manage-claude/usage-cost-api).
- OpenAI: sign into the correct organization/project in the
  [API platform](https://platform.openai.com), open
  [Usage](https://platform.openai.com/usage), and filter the project/key, model
  and time window. Review project rate/spend limits and notifications under
  Settings/Limits. Use separate keys/projects for clear attribution, and check
  the scope/enforcement of billing controls. See OpenAI's
  [production guidance](https://developers.openai.com/api/docs/guides/production-best-practices#managing-billing-limits).

No usage/admin API credential or reporting job is needed. We did not inspect
either provider account. Dashboard reporting can lag; job timeout/budget
estimates are not proof of final charges. GitHub Actions minutes are separate.

## Offline verification and outstanding live checks

Run from the repository root, with the existing pinned environment. Node is
needed for the actual publisher JavaScript tests; set its path explicitly when
it is not on PATH. Without Node those tests report a skip, not verification.

```powershell
# This installed local Node path was used here; substitute yours if different.
$env:CODEX_REVIEW_NODE = 'C:\Program Files\dotnet\packs\Microsoft.NET.Runtime.Emscripten.3.1.56.Node.win-x64\9.0.20\tools\bin\node.exe'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent_dispatch.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_codex_review.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
git diff --exit-code origin/dev -- .github/workflows/pr-dev-check.yml .github/workflows/dev-check.yml .github/workflows/prod-deploy.yml config/ci/profiles.yml requirements.txt tests/requirements.txt
```

Observed on **2026-10-16**, using Python **3.12.10** from the existing `.venv`
and the installed Node **18.17.1** path above:

- Claude command: **36 passed**, including final regressions for OpenAI-style
  keys and fine-grained GitHub tokens in publication files/reports.
- Codex command: **31 passed**, including six added regression tests; the actual
  publisher JavaScript tests executed with no skips.
- Ingestion command: **25 passed**. Full `tests` command: **130 passed**,
  including both agent suites and six repository-safety tests;
  **155 unique tests total**. All exited 0.
- `pip check`: **No broken requirements found**. `git diff --check`: passed.
  A separate whitespace check included new/untracked agent files.
- The `git diff --exit-code origin/dev -- ...` command above exited 0: all three
  database workflows, CI profile and dependency pins are unchanged.
- Both actionlint commands below exited 0 with the stated tool limitation.
  The release archive SHA256 matched its official checksum:
  `6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`.
- Local documentation-link checks passed (57 links); the smoke-issue body has
  all four nonempty required form fields, and the form adds no automatic label.

The final review fixed a reproducible publication gap: OpenAI-style keys and
fine-grained GitHub tokens are now withheld, and credential-scan controls are
protected from agent changes. The token scan examines both the Git index and
working-tree candidates, without echoing matched values; it catches a staged
token hidden by a clean working copy. No recognizable token/private-key material
was found in the inspected Git files. This is format-based detection, not an
exhaustive guarantee or a scan of historical commits. Agent task/controller and
Claude runtime directories are now ignored to prevent accidental staging.

The dbt suite emits an upstream logger `ResourceWarning` but exits successfully.
Its offline compile output includes Prod relation names; this is not production
SQL execution. GitHub/model calls
are fakes, unexpected controller HTTP calls fail immediately, Snowflake clients
are mocked, and dbt parsing/compilation uses dummy offline profiles. The Node
harness executes the **actual inline publisher program**, with fake GitHub
endpoints. Temporary Git fixtures exercise branch retry/snapshot behavior without
touching repository history. No tests install dependencies or execute Snowflake
SQL; data-model SQL is checked in memory with DuckDB.

For syntax validation, a checksum-verified official actionlint 1.7.12 release
was downloaded into ignored `logs/agent-review-tools/` (no API endpoint used).
These are the exact commands, while that temporary executable is available:

```powershell
& .\logs\agent-review-tools\actionlint.exe -shellcheck='' -pyflakes='' .github/workflows/codex-review.yml
& .\logs\agent-review-tools\actionlint.exe -shellcheck='' -pyflakes='' -ignore '^unexpected key .*queue.* for .*concurrency.* section' .github/workflows/claude-developer.yml .github/workflows/codex-review.yml .github/workflows/pr-dev-check.yml .github/workflows/dev-check.yml .github/workflows/prod-deploy.yml
```

The exclusion is only actionlint's unsupported `concurrency.queue` diagnostic;
GitHub currently documents `queue: max` with `cancel-in-progress: false` as valid.
See [GitHub concurrency reference](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Shellcheck/pyflakes are not installed or claimed as run. A fresh actionlint on
PATH can replace the temporary executable, which was removed after verification.
No GitHub workflow result can be
inferred from these static checks.

The final lint rerun used the same verified release and commands with the
temporary directory changed to `logs/agent-final-tools/`; both commands exited
0. That temporary executable was also removed after verification. Final local
suite output is retained in ignored `logs/agents-final-tests.log` and
`logs/agents-final-ingestion-tests.log`.

Remaining live checks: App installation/permission access and bot attribution;
default-branch event delivery; provider key/default-model access; pinned actions'
CLI/artifact behavior; automatic downstream CI/reviewer delivery; permission
revocation, stale/reopened/cancelled runs and partial publication recovery;
actual Dev validation/cleanup; required-check/production environment settings.
The source tests confirm preserved controls, but repository settings and live
runs were not queried in this review. The owner reported prior production
success; this work does not independently re-verify that run.

### Turn-limit maintenance verification

For the issue #19 workaround on **2026-10-16**, the same unittest commands and
Node path above produced **38 Claude tests**, **132 full tests** and **25
ingestion tests**, all passed with no skips (**157 unique tests**). `pip check`
and `git diff --check` passed. The comparison against `origin/dev` also included
the Codex workflow and `scripts/agents/`; these and all existing database
workflows, CI profile and dependency pins were unchanged. Current output is in
ignored `logs/claude-turn-limit-tests.log` and
`logs/claude-turn-limit-ingestion-tests.log`. Actionlint was not rerun for this
maintenance change; the earlier lint results above describe the initial
integration review. YAML parsing and workflow structure/failure checks passed.
No paid model call, live workflow retry, repository settings change or Snowflake
SQL was performed. After human release to main, use the
[fresh-event retry procedure](claude-developer.md#issue-19-bounded-turn-limit-workaround)
to verify actual behavior; the increased limit is not a guaranteed fix.
