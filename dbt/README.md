# Products transformations

The `agent_platform` dbt project reads the Python loader's `bronze.products`
source and builds two models tagged `products`:

- `stg_products` is a Silver view. It retains every Bronze row, converts IDs to
  integers and prices to `NUMBER(18,2)`, trims names, title-cases categories,
  and passes through `source_filename`, `batch_id` and `ingested_at` unchanged.
  Invalid/overflowing numeric values become null. Fractional IDs become null
  instead of being rounded. Fractional-cent prices and exponent notation also
  become null to avoid silent monetary coercion. Blank names/categories become
  null. No rows are
  filtered or deduplicated; tests report invalid data.
- `product_category_summary` is a Gold table with one row per category. It
  computes `product_count` using `count(*)`, plus `total_price`, `average_price`
  (six decimal places), `minimum_price` and `maximum_price`.

The current category domain is `Electronics` and `Furniture`. Change the
accepted-values tests when the product data contract adds categories.

## Schema routing

The database always follows `target.database`: `AGENTWAREHOUSEDEV` in Dev and
`AGENTWAREHOUSEPROD` in Prod. The selected profile's `target.schema` determines
the products namespace:

- `SILVER` or `BRONZE` routes to shared `BRONZE`, `SILVER`, and `GOLD`.
- `PR_42_SILVER` or `PR_42_BRONZE` routes to `PR_42_BRONZE`, `PR_42_SILVER`,
  and `PR_42_GOLD`. Any positive PR number without leading zeros is supported.
- Unexpected target schemas fail parsing instead of falling back to shared data.

`generate_schema_name` applies this routing only to this project's products
models. `connection_check` retains dbt's default schema behavior and still uses
the unmodified target schema. Source-property YAML cannot call project macros,
so its Bronze schema expression mirrors `products_schema`; the offline manifest
checks verify both for shared Dev, shared Prod and PR configurations.

## Data tests

The 31 products data tests check required fields, unique converted product IDs,
allowed categories, nonnegative prices/aggregates, non-null metadata in Bronze
and Silver, nonblank names/metadata strings, Bronze-to-Silver row counts, and
Gold aggregates reconciled with Silver. Gold reconciliation uses a full outer
join and null-safe comparisons to detect missing/extra categories and mismatched
counts, totals, averages, minima or maxima. Fixture-specific totals belong in
offline tests so real datasets can grow without breaking a hardcoded row count.

The five-product CSV produces Electronics with 3 products totaling 1199.39,
and Furniture with 2 products totaling 298.90.

## Offline validation

From the repository root, using the existing Python 3.12 environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r ingestion/requirements.txt -r tests/requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

These checks parse and compile with dbt-core/dbt-snowflake for five destination
configurations using temporary dummy profiles. Compilation disables database
introspection/cache population and a connection mock rejects any attempt to
connect to Snowflake. Artifacts go into ignored `dbt/target/offline/`.

The tests translate the actual compiled models and all 31 products test queries
from Snowflake SQL to DuckDB using SQLGlot, and execute them in memory. They
validate the CSV totals, normalization, metadata, preservation/detection of
invalid and duplicate rows, and failing reconciliation after deliberately
corrupting Silver/Gold. This is local SQL validation, not proof of Snowflake
syntax, permissions, numeric behavior or runtime success.

## Actual Dev validation

The preferred entry point for ingestion plus dbt is:

```powershell
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local
.\.venv\Scripts\python.exe scripts/run_data_flow.py --target dev --profiles-dir config/local --apply
```

It freezes one consistent profile/schema configuration for ingestion and dbt,
checks the parsed manifest before loading Bronze, and explicitly builds
`+product_category_summary` and its data tests. It propagates failures as nonzero
exit codes and reserves applied Prod runs for the main-branch GitHub Actions
deployment workflow. See the root README for the complete entry point contract.

Use the existing local profile and encrypted key. Set the profile's passphrase
privately as described in the root README; do not put secret values in commands
or files tracked by Git. From the repository root:

```powershell
# Load Bronze first. The loader verifies the expected Dev identity.
.\.venv\Scripts\python.exe ingestion/ingest_products.py --profiles-dir config/local --target dev --apply
if ($LASTEXITCODE -ne 0) { throw 'Dev Bronze ingestion failed' }

# Offline compilation using the real local profile still needs its env_var values.
.\.venv\Scripts\dbt.exe compile --project-dir dbt --profiles-dir config/local --target dev --no-introspect --no-populate-cache --select tag:products connection_check
if ($LASTEXITCODE -ne 0) { throw 'Dev compilation failed' }

# This command executes models and their data tests in Snowflake Dev.
.\.venv\Scripts\dbt.exe build --project-dir dbt --profiles-dir config/local --target dev --select tag:products connection_check
if ($LASTEXITCODE -ne 0) { throw 'Dev products build/tests failed' }
```

For PR work, set `DBT_TARGET_SCHEMA=PR_<number>_SILVER` in the process environment
before both ingestion and dbt; both honor the same profile schema. Restore the
environment afterward. The Dev role needs source read access, warehouse usage,
and schema/model creation privileges.

Workflows run products through the entry point and build `connection_check`
separately in the same target/schema. The PR lifecycle serializes validation
and cleanup, checks live GitHub state, drains tagged SQL before dropping exact
PR schemas, and handles stale close/reopen/rerun events. See the root README
for the lifecycle contract and real GitHub validation still required.
Production execution belongs to the main-branch workflow. The starter example
models remain unchanged and outside the products selector; unrestricted builds
still include their starter test with a deliberately null ID.
