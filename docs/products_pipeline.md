# Products pipeline: operation and review results

Reviewed locally on 2026-10-15 on `feat/bronze-products`. Changes are prepared
for review, not committed or pushed. No GitHub workflow results were observed.
No production SQL was executed.

The feature branch starts at `ef59092`, matching the locally recorded
`origin/dev`. The recorded `origin/main` is release merge `347f64a`, one merge
commit ahead with an identical file tree. Local `dev` and `main` branch pointers
are older; they were not moved. Remote refs were not refreshed during this
review, so fetch before publishing and review any new upstream changes.

## Data flow and schemas

`sample_data/products.csv` → `ingestion/ingest_products.py` → Bronze `PRODUCTS`
→ dbt source `bronze.products` → Silver `stg_products`
→ Gold `product_category_summary`.

`scripts/run_data_flow.py` is the entry point. It validates the selected dbt
profile, CSV, identity and destinations; an applied run also parses the dbt
manifest before loading. It then replaces Bronze and builds the explicit
`+product_category_summary` graph, with eager selection of its 31 data tests.
The starter examples never enter that graph. The workflows build
`connection_check` and its tests separately.

- Dev: `AGENTWAREHOUSEDEV`, `AGENT_DEV_CI`, `AGENT_DEV_ROLE`, `DEV_COMPUTE_WH`.
- Prod: `AGENTWAREHOUSEPROD`, `AGENT_PROD_CI`, `AGENT_PROD_ROLE`, `PROD_COMPUTE_WH`.
- Shared schemas are `BRONZE`, `SILVER`, `GOLD` in the selected database.
- Dev PR schemas are exactly `PR_<number>_BRONZE`, `PR_<number>_SILVER` and
  `PR_<number>_GOLD`. The same PR number controls sources and outputs.
- A profile schema of `SILVER` or `BRONZE` means shared schemas. A profile schema
  of `PR_<number>_SILVER` or `PR_<number>_BRONZE` supplies PR isolation.
  `--pr-number` can select isolation from a shared Dev profile; conflicting
  profile/CLI PR numbers and all production PR destinations are rejected.

The entry point freezes one selected profile/schema in a private temporary
profile for both stages. Key-pair credentials come from the existing profile;
the passphrase is passed to dbt through a secret environment variable, never
command arguments. dbt uses the active `sys.executable` and subprocess argument
lists. Temporary profiles, artifacts and captured diagnostics are deleted.
`connection_check` keeps its original dbt target-schema behavior.

## Data contract and replacement semantics

The CSV header must be exactly `product_id,product_name,category,price`. The
entire input is validated before connecting. Empty input/fields, malformed
headers/record widths/quoting, NUL bytes and invalid UTF-8 fail without writes.
Quoted commas, escaped quotes, CRLF, UTF-8 BOMs and multiline fields work.
All four Bronze values stay raw strings. Each load adds the source basename,
a fresh batch UUID and one UTC ingestion timestamp.

Bronze creates missing schema/table objects before its row transaction. It
checks the live database/user/role/warehouse and existing table layout before
`BEGIN`; inside the transaction it deletes the previous snapshot, inserts all
records, compares all values and metadata, and commits only after verification.
Loading/verification failures request rollback. Reruns replace rows instead of
appending, including when the next snapshot is smaller. They deliberately get
new batch/timestamp metadata. DDL is never used to replace the Bronze dataset
inside the transaction.

Silver preserves every row and all ingestion metadata. IDs must be integer
strings and become `NUMBER(38,0)`, Snowflake's integer representation. Names
are trimmed, categories title-cased, and prices become `NUMBER(18,2)`.
Prices permit signed decimal notation with at most two fractional digits;
fractional cents and exponent notation become null rather than being rounded
or permissively coerced. Negative cent values remain negative. Invalid IDs,
prices and blank normalized fields remain present for data tests to reject.
See [Snowflake numeric types](https://docs.snowflake.com/en/sql-reference/data-types-numeric)
and [conversion semantics](https://docs.snowflake.com/en/sql-reference/functions/try_to_decimal).

Gold is rebuilt as a table on each successful build. It groups by category,
uses `count(*)` for product counts, and computes fixed-point totals, averages
(six fractional places), minima and maxima. The valid category domain is
currently `Electronics` and `Furniture`.

The fixture yields Electronics: count 3, total 1199.39, average 399.796667,
minimum 49.90, maximum 899.99. Furniture: count 2, total 298.90,
average 149.450000, minimum 99.90, maximum 199.00.

Data tests cover required/unique IDs, required/nonblank names, accepted
categories, nonnegative prices/aggregates, non-null Bronze/Silver metadata,
nonblank metadata strings, Bronze/Silver row counts, and per-category Gold
counts/totals/averages/minima/maxima reconciled with Silver. Fixture and fault
tests additionally detect invalid-row filtering, duplicate normalized IDs,
fractional-cent coercion, lost metadata and corrupted aggregates.

## Local PowerShell commands

From the repository root, with Python 3.12:

```powershell
# All direct runtime dependencies retain their verified pinned versions.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r tests/requirements.txt

# Offline checks; no Snowflake credentials needed.
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe -m pip check
git diff --check

# Shared Dev dry-run: no connection or subprocess.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local

# Example PR dry-run only; do not apply against an arbitrary PR number.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --pr-number 424242
```

For a deliberate shared Dev applied run, supply the existing encrypted-key
passphrase privately. This replaces the shared products dataset:

```powershell
$previousPassphrase = [Environment]::GetEnvironmentVariable('DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE', 'Process')
$productsPassphrase = Read-Host 'Existing Snowflake key passphrase' -AsSecureString
try {
    $env:DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE = [System.Net.NetworkCredential]::new('', $productsPassphrase).Password
    .\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --apply
    if ($LASTEXITCODE -ne 0) { throw 'Products flow failed' }
}
finally {
    [Environment]::SetEnvironmentVariable('DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE', $previousPassphrase, 'Process')
    $productsPassphrase.Dispose()
    Remove-Variable previousPassphrase, productsPassphrase
}
```

For isolated local integration, prefer a unique positive numeric UUID as the
PR-style namespace, not a real PR number. Before applying, verify all three
exact schema names are absent; create them without `IF NOT EXISTS` and record
only successful creations. Run ingestion/build twice and compare five Bronze
and Silver rows and the two expected Gold groups. A cleanup must verify the
run's ownership marker and remove only schemas created by that test. If
ownership or query completion is uncertain, leave objects for inspection.
No such schemas were created in this review because credentials were missing.

Never run applied Prod commands locally. Production execution is guarded for
the released `prod-deploy.yml` on main, inside GitHub Actions, on `push` or
`workflow_dispatch`. That guard supplements the Snowflake role/credential
separation; environment variables alone are not an authorization boundary.

## Workflow behavior and cancellation

- `pr-dev-check.yml`: same-repository feature PRs into `dev` and `dev`-to-`main`
  release PRs run the products flow and `connection_check` in isolated Dev
  schemas. Forks cannot pass the required gate or use this validation path.
- The exact required check name stays **PR Dev validation required**. Only a
  successful database job passes; failures, cancellation and skips fail the
  gate. Main-bound PRs must originate from `dev`.
- `dev-check.yml`: pushes to `dev` run connection checks and shared Dev products
  under `environment: development`.
- `prod-deploy.yml`: pushes to `main` and dispatch on `main` run connection
  checks and shared Prod products under `environment: production`. Prod
  credentials are scoped to that job. Every database job always attempts
  private-key cleanup, and Python/dbt failures propagate.
- PR closure, merged or rejected, drops only the exact PR Gold, Silver and
  Bronze schemas, in that order, under `AGENTWAREHOUSEDEV`.

Validation and cleanup share a per-PR concurrency slot with automatic
cancellation disabled and `queue: max`. Live PR state, head/base commits and
workflow attempt status are checked before work. Old validation events fail;
old close events do nothing if the PR is now open. If the base moved, update
the PR instead of rerunning an old merge snapshot.

Python/dbt validation sessions and lifecycle control sessions use distinct
repository/PR/run/attempt query tags. Before cleanup or a new validation,
the coordinator drains **both** old validation SQL and old cleanup SQL,
excluding only its own session. This includes detached `DROP SCHEMA` queries
from cancelled cleanup runs that could otherwise race a reopened build.
It cancels active queries, aborts their sessions, confirms terminal status in
Information Schema query history, and requires a quiet interval. It never
assumes GitHub cancellation stopped Snowflake SQL.

Reopening during cleanup stops remaining drops; the new validation waits for
the lifecycle slot and drains any older server work before rebuilding. History
truncation at 10,000 rows, a drain timeout, API errors or cancellation/session
abort errors stop further work instead of allowing unsafe cleanup. The
coordinator rechecks PR state before each exact drop and never drops shared
schemas or cancels whole users/warehouses.

## Verified locally and still outstanding

The offline checks use temporary dummy profiles and block Snowflake connections
for parsing/compilation. They cover shared Dev, shared Prod and both PR profile
forms, including unchanged `connection_check` schema behavior. Compiled models
and all 31 products data-test queries execute in memory after SQLGlot
translation to DuckDB. This proves the fixture and regression behavior locally,
not Snowflake runtime syntax, permissions or all engine-specific numeric cases.

Commands actually run in this review (from the repository root):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s ingestion -p 'test_*.py' -v
# Exit 0: 25 tests, OK.
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
# Exit 0: 60 tests, OK. Total: 85 offline tests.
.\.venv\Scripts\python.exe -m pip check
# Exit 0: No broken requirements found.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local
# Exit 0: 5 valid rows; shared BRONZE/SILVER/GOLD; no connection/subprocess.
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --pr-number 424242
# Exit 0: 5 valid rows; PR_424242_BRONZE/SILVER/GOLD; no connection/subprocess.
git diff --check
# Exit 0: no whitespace errors in tracked changes.
```

The 60-test suite includes actual Bash execution of the required-check gate,
actual dbt parsing/compilation with dummy profiles, a real active-Python dbt
parse subprocess, SQL fixture/fault checks, mocked lifecycle/orchestration
checks and the Git credential/artifact audit. All other Snowflake and GitHub
interactions in these tests are mocked or blocked. Repeated in-process dbt
invocations emit an upstream logger `ResourceWarning`; the suite still exits 0.

The local Dev profile exists and is ignored, but its required
`DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE` is unavailable. Dev integration was
therefore skipped before connecting. No Snowflake test objects were created
or dropped. No Prod SQL ran. GitHub workflow execution was not observed.

Still verify in real GitHub/Snowflake runs: environment secrets and approvals,
API token read permissions, schema/model grants, source read access, visibility
of active queries, own-session abort/cancel privileges and timing, the actual
required check under failures/cancellation/skips, close/reject/merge/reopen and
stale rerun behavior, shared Dev deployment and the eventual main release.

## Review fixes and recovery limits

This review fixed unescaped quotes accepted by Python's strict CSV reader,
fractional-cent/permissive numeric coercion, incomplete manifest alias checks,
duplicate or warned/mistyped dbt results treated as successful, and failure to
drain a detached older cleanup before reopening. Regression tests cover each.

The pipeline is not one database transaction. Bronze replacement is atomic
when the transaction outcome is known, but DDL-created objects remain on
failure. A later dbt failure does not undo Bronze or already rebuilt models.
A lost commit/rollback acknowledgement leaves the outcome uncertain: inspect
the batch and query/session state privately before retrying. A normal rerun
replaces Bronze and rebuilds the graph; it does not restore a previous batch.
Test warnings, skipped nodes, missing/duplicate results and nonzero child exit
codes all make the applied flow fail.

Cleanup cannot be guaranteed if Actions itself never runs, a run is manually
cancelled or permissions fail. It leaves uncertain objects in place; rerun the
updated closed-PR workflow after resolving the cause. On first rollout, finish
pre-change workflow attempts and drain their SQL. Do not rerun historical
workflow definitions that lack the new tags and live-state checks. Serialize
local/manual writers too; they do not acquire the GitHub concurrency slot.
Direct SQL against PR schemas must not bypass the tagging/serialization protocol.

Upstream actionlint 1.7.12 does not recognize GitHub's supported `queue` key.
Earlier local workflow-schema validation passed, and actionlint's other checks
passed with only that known diagnostic excluded. These are static checks, not
observations of GitHub workflow success. See [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
and the [actionlint compatibility issue](https://github.com/rhysd/actionlint/issues/657).

Credentials and generated artifacts remain excluded from Git. Safety tests
check both tracked files and untracked Git candidates, ignore rules for local
profiles/key files/logs/targets/caches, and absence of private-key blocks.
They are focused checks, not a general-purpose secret scanner.
