# snowflake-agent-team
An AI-assisted data engineering project using Claude as a developer and Codex as a reviewer to build and validate Snowflake pipelines with Python, dbt, GitHub Issues and CI/CD across separate development and production environments.

See [the reviewed products pipeline guide](docs/products_pipeline.md) for the
data contract, operating commands, verified results and recovery limits.

## Run the products pipeline

Use `scripts/run_data_flow.py` as the entry point for CSV → Python Bronze → dbt
Silver → dbt Gold. Python 3.12 and the existing encrypted-key dbt profile are
required. Direct runtime dependencies are pinned to the verified versions in
`requirements.txt`; `ingestion/requirements.txt` includes that same file.

From the repository root in PowerShell:

```powershell
# Install only when setting up the active environment.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Default: validate CSV/configuration and report destinations without connecting.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local

# Execute after supplying the profile's passphrase privately (see below).
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --apply
if ($LASTEXITCODE -ne 0) { throw 'Products flow failed' }

# Optional PR isolation: add --apply to execute in all three PR schemas.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --pr-number 42
```

`--target` defaults to `dev`; `--profiles-dir` defaults to `DBT_PROFILES_DIR` or
`config/local`. The profile name comes from `dbt/dbt_project.yml`. The supplied
PR number overrides shared schemas for this run; if the selected profile already
uses a PR schema, its number is honored and conflicting numbers are rejected.
Production never accepts PR schemas or `--pr-number`.

An applied run validates the CSV and target identity, parses dbt configuration
and verifies its source/model destinations, loads and verifies Bronze, then
runs `dbt build --select +product_category_summary --indirect-selection eager`.
This explicitly selects the products source/model graph and its 31 data tests,
excluding the starter examples and `connection_check`. The workflows run
`connection_check` and its tests separately in the same target/schema.

dbt runs via the same `sys.executable` as the entry point using a subprocess
argument list. A private temporary profile freezes the selected target's schema
for both stages; its passphrase is supplied through a secret environment variable.
Temporary profiles, dbt artifacts and captured diagnostics are removed after the
run. Only destinations, row/batch counts and structured execution statuses are
reported; raw dbt/connector error output is suppressed.

Every failed step returns a nonzero status and prevents subsequent steps.
Bronze replacement has its own transaction: failure in a later dbt build does
not undo the committed Bronze dataset or successful dbt models. A rerun replaces
Bronze and rebuilds the products graph.

`--target prod` can validate a configured Prod profile in dry-run mode. Applied
Prod runs require GitHub Actions on `refs/heads/main`, a `push` or
`workflow_dispatch` event, and the released `.github/workflows/prod-deploy.yml`.
Prod identity/session checks also run at the Bronze write boundary. Production
execution remains reserved for the main-branch workflow after release.

Offline tests, with no Snowflake credentials:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r tests/requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

## GitHub Actions lifecycle

- Feature PRs into `dev` and release PRs from `dev` into `main` run products
  ingestion/build/tests plus `connection_check` in that PR's Dev schemas.
- Pushes to `dev` run the shared Dev flow under `environment: development`.
- Pushes to `main` (or manual dispatch on `main`) run the shared production
  flow under `environment: production`, using only that environment's secrets.
- Closing a PR, merged or rejected, drops exactly its
  `AGENTWAREHOUSEDEV.PR_<number>_GOLD`, `PR_<number>_SILVER`, and
  `PR_<number>_BRONZE` schemas. Shared schemas are never cleanup targets.

`PR Dev validation required` keeps its existing name and requires the database
job to succeed. Failure, cancellation, skipping, fork PRs and main-bound PRs
from branches other than `dev` cannot pass that gate. Dependency installation,
Python/dbt exit codes and private-key cleanup remain part of each job.

The PR workflow holds the same per-PR concurrency slot for the full lifecycle,
with `cancel-in-progress: false` and `queue: max`. This keeps close/reopen events
queued rather than replacing the single pending event; ordering is not assumed.
See [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
The helper reads the live PR and current run attempt from GitHub before acting:
stale validation attempts fail before writing, and stale close attempts skip
cleanup when the PR is open. Validation also checks the head and base commits;
if the base moved, update the PR rather than rerunning an old merge snapshot.

All Python and dbt PR SQL carries a repository/PR/run/attempt query tag. Before
cleanup, reopening or rerun writes, `scripts/ci_pr_lifecycle.py` inspects the
current user's Information Schema query history, cancels active statements for
only that PR, including old cleanup queries, aborts their sessions, and waits
for terminal statuses plus a quiet interval. The current coordinator session
is excluded. It does not assume runner cancellation stopped Snowflake SQL, use
delayed Account Usage history, or cancel an entire user/warehouse. Query-history
visibility is checked before filtering; hitting its 10,000-row limit, API errors,
abort/cancel errors or a drain timeout blocks cleanup without dropping schemas.
See [Snowflake query history](https://docs.snowflake.com/en/sql-reference/functions/query_history)
and [query cancellation](https://docs.snowflake.com/en/sql-reference/functions/system_cancel_query).

The helper rechecks live state before every drop. If the PR reopens while a
close run is draining/dropping, the remaining drops stop; the queued reopened
validation rebuilds all layers after the cleanup run releases its slot.
Cleanup reruns are idempotent. Manual cancellation may require rerunning the
closed-PR job once GitHub is available again; any uncertainty leaves schemas
in place rather than dropping them while a writer could still be running.

First rollout must let pre-change workflow attempts finish and stop their
detached SQL before enabling the new lifecycle. Do not rerun historical attempts
that predate this protocol: they run their original workflow code without tags
or live-state checks. Updated attempts share the lifecycle lock and drain logic.

Offline checks execute the required gate in Bash, validate workflow structure,
test stale/reopened/rerun scenarios and query drain failures with mocks, and
parse/compile the models with dummy profiles. The current workflow JSON schema
also accepts all three workflow files. Upstream actionlint 1.7.12 passes its
other checks with only its known unsupported `concurrency.queue` diagnostic
excluded ([upstream issue](https://github.com/rhysd/actionlint/issues/657)).

Real GitHub runs must verify environment access/approvals and token read
permissions (`contents`, `pull-requests`, `actions`), Dev grants for all three
schemas and the current CI user's ability to inspect/cancel its queries and
abort its sessions. Exercise close during validation, reject, merge, reopen,
stale rerun and manually cancelled work before relying on the lifecycle in
production. No production database checks run locally.

## Products Bronze ingestion

`ingestion/ingest_products.py` reads `sample_data/products.csv` and uses the
profile named by `dbt/dbt_project.yml` (`agent_platform`). It selects the existing
profile's target unless `--target` is supplied. Profiles default to
`config/local`, or `DBT_PROFILES_DIR` when set; `--profiles-dir` overrides both.
Only the selected output is rendered, using dbt's profile renderer.

The default is a **dry-run with no database connection**. Every record is
validated before connecting: the header must be exactly
`product_id,product_name,category,price`, each record must contain four nonempty
fields, and empty, malformed or invalid UTF-8 input is rejected. UTF-8 BOMs and
properly quoted commas/newlines are supported. All four source values stay raw
strings, including whitespace, leading zeros and price formatting; Bronze does
not perform numeric conversion or product-key deduplication.

With `--apply`, the loader reuses `private_key_path` and
`private_key_passphrase` from the selected profile and validates the encrypted
key before connecting. Both the profile settings and the connected session must
match `AGENTWAREHOUSEDEV`, `AGENT_DEV_CI`, `AGENT_DEV_ROLE` and `DEV_COMPUTE_WH`.
This lower-level command supports Dev only. Production ingestion runs through
`scripts/run_data_flow.py` in the main-branch production workflow.

The shared destination is `AGENTWAREHOUSEDEV.BRONZE.PRODUCTS`. Use
`--pr-number 42` for `AGENTWAREHOUSEDEV.PR_42_BRONZE.PRODUCTS`. A rendered dbt
schema of `PR_42_SILVER` or `PR_42_BRONZE` also selects that PR destination
automatically; conflicting PR numbers are rejected. Shared dbt schemas must be
`SILVER` or `BRONZE`.

The table has four `VARCHAR` source columns plus `SOURCE_FILENAME` (basename),
`BATCH_ID` (new UUID per load) and `INGESTED_AT` (one UTC `TIMESTAMP_TZ` per
load). Required schemas/tables are created if missing. An incompatible existing
table is rejected before replacing rows. DDL finishes before an explicit
transaction deletes the old rows, inserts the complete snapshot, compares all
values and metadata, and commits. Loading or verification failures request a
rollback, preserving the prior snapshot. Newly created schema/table objects can
remain after failure; DDL cannot be rolled back with the row replacement
([Snowflake transaction semantics](https://docs.snowflake.com/en/sql-reference/transactions)).
If the connection is lost during commit/rollback, verify transaction status
before assuming its outcome. Successful reruns replace the dataset rather than
appending rows. Serialize writers to a shared destination.

Run these commands from the repository root in PowerShell:

```powershell
# Install only if the verified dependencies are not already in .venv.
.\.venv\Scripts\python.exe -m pip install -r ingestion/requirements.txt

# Offline tests and shared Dev dry-run; neither needs a key passphrase.
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe ingestion/ingest_products.py --profiles-dir config/local --target dev

# Optional PR dry-run. Leave off --apply until ready to write that PR schema.
.\.venv\Scripts\python.exe ingestion/ingest_products.py --profiles-dir config/local --target dev --pr-number 42
```

The current local profile reads its passphrase from
`DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE`. If it is not already provided by your
private environment, this prompts locally without echoing it or storing it in
shell history. It restores any previous environment value afterward. Never put
the passphrase or key into Git, command arguments, or chat.

```powershell
$previousPassphrase = [Environment]::GetEnvironmentVariable('DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE', 'Process')
$productsPassphrase = Read-Host 'Existing Snowflake key passphrase' -AsSecureString
try {
    $env:DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE = [System.Net.NetworkCredential]::new('', $productsPassphrase).Password

    # First load: verifies all five rows and their metadata before committing.
    .\.venv\Scripts\python.exe ingestion/ingest_products.py --profiles-dir config/local --target dev --apply
    if ($LASTEXITCODE -ne 0) { throw 'Products load failed' }

    # Rerun: verifies the full table still contains exactly the same five products.
    .\.venv\Scripts\python.exe ingestion/ingest_products.py --profiles-dir config/local --target dev --apply
    if ($LASTEXITCODE -ne 0) { throw 'Products rerun failed' }
}
finally {
    [Environment]::SetEnvironmentVariable('DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE', $previousPassphrase, 'Process')
    $productsPassphrase.Dispose()
    Remove-Variable previousPassphrase, productsPassphrase
}
```

Required Dev privileges include schema creation for missing destinations, table
creation for missing `PRODUCTS` tables, warehouse usage, and `SELECT`, `INSERT`
and `DELETE` access on existing destination tables. PR cleanup additionally
requires ownership/access to drop that PR's exact three temporary schemas and
permissions to drain the CI user's tagged SQL as described above.

## Products Silver and Gold

The dbt source, `stg_products` Silver view and `product_category_summary` Gold
table are implemented with isolated shared/PR schema routing and data-quality
and reconciliation tests. See [the dbt guide](dbt/README.md) for model behavior,
offline fixture checks and actual Dev build commands.
