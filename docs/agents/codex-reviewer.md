# Advisory Codex PR reviewer

Implemented locally, **disabled by default**. No model API call, PR comment, remote
configuration change or Snowflake SQL was performed during implementation.
Keep `ENABLE_CODEX_REVIEW` unset/false until owner setup and human review finish.
Use the [combined owner runbook](setup.md) for bootstrap, exact App identity and
secret setup, shutdown, logs/API usage and the documentation-only smoke test.

## Owner setup

1. Review the [workflow](../../.github/workflows/codex-review.yml),
   [controller](../../scripts/agents/review_pr.py),
   [review instructions](reviewer-instructions.md),
   [configuration](../../.github/codex/review-config.toml) and
   [output schema](../../.github/codex/review-schema.json). Merge normally into
   `dev`, then release `dev` to `main` with human review. Both target branches
   need trusted instructions/configuration before reviews can run on them.
2. Configure repository Actions secret **`OPENAI_API_KEY`** for an owner-controlled
   OpenAI API project with suitable usage controls. This uses API billing, not
   ChatGPT subscription entitlement. No key value belongs in files, logs or chat.
   No Snowflake secret, deployment environment, personal token or new App is
   required for this reviewer. The separate publisher uses the job-scoped
   `GITHUB_TOKEN` with only `pull-requests: write`.
3. To admit the Claude developer App, set **`CLAUDE_AGENT_APP_SLUG`** to its exact
   existing slug and **`CLAUDE_AGENT_APP_BOT_ID`** to its numeric GitHub **bot user
   ID**, not its App ID/client ID/installation ID. Verify the public user record
   `GET https://api.github.com/users/<slug>%5Bbot%5D`: login must be
   `<slug>[bot]`, type `Bot`, id the configured number. The controller checks this
   live. Both variables must be set together; leaving both unset permits human
   maintainer PRs only. A slug left over from Claude setup still requires the bot
   ID before reviewer enablement. Wildcards and other bots are rejected.
4. Keep native/other Codex automatic review disabled to avoid duplicate reviews.
   Allow the pinned official action under repository/organization Actions policy.
   Confirm Actions may publish PR comments and the API project can use the
   action's default model. No new required check should be selected.
5. Only after setup, set **`ENABLE_CODEX_REVIEW=true`** (literal lowercase). An
   owner-approved same-repository test PR should verify the cases below. Enabling
   does not retroactively trigger existing PRs; use a new PR update/reopen event.

## Events, trust and exact revisions

The workflow uses `pull_request_target` on `opened`, `reopened`, `synchronize`
into `dev`/`main`. This deliberately loads the **trusted target workflow**, not
workflow edits supplied by the PR. It must never be extended to execute PR code.
The job gate and controller reject forks, other bases/events, closed PRs and
other repositories **before accessing the API secret**.

The controller checks the original event sender's identity, the rerun actor and
the live PR author. Each must either be a current repository collaborator with
`write`, `maintain` or `admin` permission, or exactly the configured Claude App's
bot login/type/numeric ID. Author association, an `ai` label, a trusted issue
author, `agent/` branch names and generic bot status grant no permission. A
maintainer's rerun cannot make an untrusted original sender eligible.

Admission records PR number, full current head SHA, base SHA/ref, actor IDs and
run/attempt. The event must still match the live open PR. Preparation rechecks
admission before and after snapshotting; publishing rechecks identities/current
permissions and the open PR/head/base before processing and immediately before
writing. Stale queued events/reruns are rejected rather than reviewing an older
head. If the base advanced, use a fresh PR update/reopen event; an old event
cannot silently change its admitted base. The SHA appears in every result.

## Review generation and publication

The first two jobs have `contents: read`, `pull-requests: read`; neither can
comment, edit branches or merge. The review job checks out only the trusted
workflow revision with credentials persistence disabled. The stdlib controller
fetches validated commit IDs, reads Git trees/blobs without filters or external
diff tools and compares the merge-base to the exact head. It never checks out
the PR, follows a PR symlink, initializes submodules, executes a PR command or
installs PR dependencies. PR descriptions and files travel as JSON text in a
prompt file; they are never interpolated into shell code.

Root/applicable nested `AGENTS.md`, reviewer instructions, runtime configuration
and the JSON output schema come from the admitted **trusted base SHA**. Proposed
versions remain untrusted review material. Codex's working directory is empty
outside the checkout, with a fresh `CODEX_HOME`; PR `.codex` configuration,
skills and instructions are not loaded as runtime configuration.

The official `openai/codex-action` is pinned to
`bdf19a4a223ec2549a3e2274a0cf61556bc07675` (v1); its CLI and API proxy are pinned
together to `0.163.0`. It installs these official tools, not project packages.
The action runs **last** in its job on a disposable GitHub-hosted Ubuntu runner.
It uses `permission-profile: ":read-only"` (the current read-only sandbox
selection) **and** `safety-strategy: drop-sudo`, which applies the action's
documented Linux privilege restrictions. Do not substitute `unsafe` or use
`safety-strategy: read-only` alone. No host step processes output after the model
on that runner. Trusted configuration disables shell tools, web search, apps,
goals and automatic skill dependency installation; approval policy is `never`.
No PR scripts, test runners, dependency installs or database commands are
available to the review. Judging correctness and resisting misleading PR text
still depend on model behavior and human review; privileges do not prove the
model's conclusions.

Only the final JSON message crosses to a fresh publisher job. The publisher has
only `pull-requests: write`, no checkout, no API key, no repository scripts and
no subprocess/evaluation path. Its fixed inline JavaScript validates exact
fields, SHA, status, finding severity/location/types/limits and changed-file
membership. It treats the result as data, neutralizes Markdown/HTML/mentions
and withholds recognizable credential-like output. It creates or updates one
`github-actions[bot]` comment marked `<!-- codex-advisory-review:v1 -->`.
Foreign marker comments are never edited; duplicate owned markers or exhausted
comment visibility require human recovery. A newer recorded run/attempt cannot
be overwritten by an older run.

Findings have P0–P3 severity, file, side/line, explanation and concrete
recommendation. Locations refer to the supplied full-file text (base text is
the diff's merge-base); they are not executable links/commands. A completed
empty result explicitly says **No actionable findings**. Model failure,
cancellation, missing/invalid/oversized output or `status=failed` produces a
distinct **Review failed** comment and failing publisher result when publishing
is still authorized/current. Admission failures have Actions errors and no
comment. Stale/closed PRs are deliberately not modified. Findings themselves
remain advisory and do not fail the publisher.

The review covers correctness/test coverage, Dev/Prod routing, PR sources and
outputs, SQL identifiers/cleanup, transaction/rerun behavior, failure propagation,
credentials/logging and changes weakening workflow/deployment controls.
It reports static observations; it does not claim to have executed tests or
observed CI. Linked issue/CI details absent from the snapshot remain context
limitations; a human should supply/inspect those separately.

## Bounds and recovery limitations

- Per-PR concurrency is serialized, with no cancellation of an active review.
  GitHub may replace an older pending run with a newer one. Stale-head checks
  prevent old queued/rerun results from replacing the current review.
- Job limits are 5 minutes admission, 20 minutes generation, 5 minutes
  publication. There is no retry loop and no auto-approval/merge. The action has
  no configured hard API spending cap; time/input/output limits do not replace
  owner project billing controls.
- At most 50 changed text files, 64,000 bytes per file, 500,000 serialized context
  bytes, and a 32,000-byte PR description are accepted. Binary files, submodules,
  sensitive paths, recognizable secrets, empty/oversized diffs and excess context
  fail closed for manual review; required material is never silently truncated.
- Model JSON is limited by schema (10 findings, 8 limitations, bounded strings)
  and a 32,000-byte publisher input limit; rendered comments are bounded at
  60,000 bytes. GitHub also imposes its own job-output limits. Missing or masked
  outputs are failures, never a clean review. Up to 1,000 comments are inspected.
- Turning the variable off prevents new admissions. Values already resolved by
  an in-flight run are not a revocation mechanism; manually cancel/disable the
  workflow or revoke the API key when needed. Cancellation cannot undo paid
  requests or a posted comment. The API has no atomic head-check-and-comment
  operation: a narrow race after the final check remains. Comments always record
  the SHA so a human can see stale results; a fresh event corrects the comment.
- This workflow does not cancel/clean up Snowflake work; it never starts any.
  Existing CI remains responsible for database validation/cleanup/deployment.
  **PR Dev validation required** and human-reviewed `dev`/`main` merges remain
  unchanged. Do not make this advisory reviewer a required check initially.

## Offline verification and live setup checks

With existing Python 3.12 dependencies and Node on PATH (or an explicit local
Node executable), run:

```powershell
# Optional when Node is installed outside PATH; substitute your actual path.
# $env:CODEX_REVIEW_NODE = 'C:\path\to\node.exe'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_codex_review.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Without Node, publisher JavaScript tests explicitly skip; do not count those as
verified. The offline tests use real temporary Git objects and run the actual
inline publisher JavaScript with mocked GitHub endpoints. No model, database,
remote writes or paid requests are involved.

Observed on 2026-10-16 with the existing `.venv` Python 3.12.10:

- Reviewer command above: **25 passed**, with no skips. The actual publisher
  JavaScript ran under the already installed Node 18.17.1, selected for these
  commands with
  `$env:CODEX_REVIEW_NODE = 'C:\Program Files\dotnet\packs\Microsoft.NET.Runtime.Emscripten.3.1.56.Node.win-x64\9.0.20\tools\bin\node.exe'`.
  This is offline program verification; the Actions runtime uses the action's
  own Node version and still needs live verification.
- Full suites above: **25 ingestion + 113 pipeline/agent = 138 passed**. These
  include offline dbt parsing/compilation and in-memory SQL checks, with dummy
  profiles. No Snowflake SQL was executed, including for Prod. An upstream dbt
  logger `ResourceWarning` did not fail the suite.
- `python -m pip check`: no broken requirements. `git diff --check`: passed.
  Database workflows, CI profile and dependency pins match `origin/dev`.
- A temporary Git fixture using all 22 current pipeline context files produced
  a complete 129,445-byte snapshot within the configured bounds.
- SHA256-verified actionlint **1.7.12** passed for the new workflow using
  `actionlint -shellcheck= -pyflakes= .github/workflows/codex-review.yml`, without
  diagnostic exclusions. All five workflows passed with only the existing
  unsupported `concurrency.queue` diagnostic excluded via
  `-ignore '^unexpected key .*queue.* for .*concurrency.* section'`.
  The downloaded tool was used from ignored `logs/codex-tools/` and removed
  afterward; shellcheck/pyflakes were not installed or claimed as run.

Live verification remains owner work: same-repository maintainer and configured
Claude App events into both bases; fork/untrusted actor rejection; original
actor/rerunner permission revocation; missing-key failure; current-SHA comment
creation/update; stale, closed, reopened and cancelled runs; pinned action/CLI
compatibility and repository policies. Confirm existing CI still gates merges.
No GitHub run is claimed by these offline tests.

Official sources inspected 2026-10-16:
[Codex GitHub Action](https://developers.openai.com/codex/github-action/),
[action README](https://github.com/openai/codex-action/blob/main/README.md),
[security guidance](https://github.com/openai/codex-action/blob/main/docs/security.md),
[configuration reference](https://developers.openai.com/codex/config-reference/).
