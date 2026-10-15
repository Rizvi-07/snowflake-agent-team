"""Validate a products snapshot; replace Dev Bronze data only with --apply."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import yaml
from dbt.config.renderer import ProfileRenderer
from dbt_common.context import set_invocation_context


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COLUMNS = ("product_id", "product_name", "category", "price")
COLUMNS = (*SOURCE_COLUMNS, "source_filename", "batch_id", "ingested_at")
EXPECTED = {
    "database": "AGENTWAREHOUSEDEV",
    "user": "AGENT_DEV_CI",
    "role": "AGENT_DEV_ROLE",
    "warehouse": "DEV_COMPUTE_WH",
}
EXPECTED_BY_TARGET = {
    "dev": EXPECTED,
    "prod": {
        "database": "AGENTWAREHOUSEPROD",
        "user": "AGENT_PROD_CI",
        "role": "AGENT_PROD_ROLE",
        "warehouse": "PROD_COMPUTE_WH",
    },
}


class IngestionError(Exception):
    """A safe, actionable error that does not contain credentials or row values."""


def expected_identity(target: str) -> dict[str, str]:
    if target not in EXPECTED_BY_TARGET:
        raise IngestionError("Target must be dev or prod.")
    return EXPECTED_BY_TARGET[target]


def validate_execution(target: str) -> None:
    """Production writes are reserved for the released main-branch workflow."""
    expected_identity(target)
    if target == "prod" and not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_EVENT_NAME") in ("push", "workflow_dispatch")
        and os.environ.get("GITHUB_WORKFLOW_REF", "").endswith(
            "/.github/workflows/prod-deploy.yml@refs/heads/main"
        )
    ):
        raise IngestionError("Production execution requires the main-branch prod-deploy GitHub Actions workflow.")


def profile_renderer() -> ProfileRenderer:
    # dbt normally initializes this in its CLI. Standalone callers must do so
    # before env_var() can resolve ordinary and DBT_ENV_SECRET_* variables.
    set_invocation_context(os.environ)
    return ProfileRenderer()


def strict_csv_lines(handle):
    """Reject unescaped quotes too; csv.reader(strict=True) accepts them.

    Validate only CSV quoting here. csv.reader still handles records, delimiters,
    escaped quotes and multiline fields, without changing any source values.
    """
    state = "start"
    for line in handle:
        for character in line:
            if state == "quoted":
                if character == '"':
                    state = "after_quote"
            elif state == "after_quote":
                if character == '"':
                    state = "quoted"
                elif character == ',' or character in '\r\n':
                    state = "start"
                else:
                    raise csv.Error("Unexpected text after a quoted field")
            elif character == '"':
                if state != "start":
                    raise csv.Error("Unescaped quote in an unquoted field")
                state = "quoted"
            elif character == ',' or character in '\r\n':
                state = "start"
            else:
                state = "unquoted"
        yield line
    if state == "quoted":
        raise csv.Error("Unterminated quoted field")


def read_products(path: Path) -> list[tuple[str, ...]]:
    """Read the entire input before connecting; preserve all four strings."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(strict_csv_lines(handle), strict=True)
            if tuple(next(reader, ())) != SOURCE_COLUMNS:
                raise IngestionError(
                    "CSV header must be exactly: " + ",".join(SOURCE_COLUMNS)
                )
            rows = []
            for row in reader:
                if len(row) != len(SOURCE_COLUMNS):
                    raise IngestionError(
                        f"CSV record ending on line {reader.line_num} must have four fields."
                    )
                if any(not value.strip() or "\x00" in value for value in row):
                    raise IngestionError(
                        f"CSV record ending on line {reader.line_num} has an empty or invalid field."
                    )
                rows.append(tuple(row))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise IngestionError("Cannot read a well-formed UTF-8 products CSV.") from exc
    if not rows:
        raise IngestionError("CSV must contain at least one product record.")
    return rows


def selected_profile(profiles_dir: Path, profile_name: str, target: str | None) -> dict:
    """Select first, so unused dbt targets never require credentials."""
    try:
        profiles = yaml.safe_load((profiles_dir / "profiles.yml").read_text(encoding="utf-8"))
        profile = profiles[profile_name]
        renderer = profile_renderer()
        target = target or renderer.render_value(profile.get("target", "dev"))
        output = profile["outputs"][target]
        if not isinstance(output, dict):
            raise TypeError
        return output
    except Exception as exc:
        raise IngestionError("Cannot select the dbt profile/target; check --profiles-dir, --profile and --target.") from exc


def render_settings(raw: dict, *, credentials: bool = False, target: str = "dev") -> dict:
    # A dry-run needs no passphrase or private-key file. Still render and check
    # all destination/identity settings with dbt's own env_var/secret handling.
    expected = expected_identity(target)
    if credentials:
        validate_execution(target)
    fields = ("type", "account", "authenticator", *expected, "schema")
    if credentials:
        fields += ("private_key_path", "private_key_passphrase")
    try:
        settings = profile_renderer().render_data({k: raw[k] for k in fields if k in raw})
    except Exception as exc:
        raise IngestionError(
            "Cannot render selected dbt target. Check its environment variables, "
            "including DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE for --apply."
        ) from exc
    if settings.get("type") != "snowflake" or settings.get("authenticator") != "SNOWFLAKE_JWT":
        raise IngestionError("Selected dbt target must use Snowflake key-pair authentication (SNOWFLAKE_JWT).")
    for name, value in expected.items():
        if str(settings.get(name, "")).upper() != value:
            raise IngestionError(f"Refusing ingestion: selected target must use {name}={value}.")
        settings[name] = value
    if not settings.get("account") or not raw.get("private_key_path"):
        raise IngestionError("Selected dbt target must configure account and private_key_path.")
    return settings


def destination(settings: dict, pr_number: str | None, *, target: str = "dev") -> str:
    expected = expected_identity(target)
    schema = str(settings.get("schema", "")).upper()
    match = re.fullmatch(r"PR_([1-9][0-9]*)_(?:SILVER|BRONZE)", schema)
    if pr_number is not None and not re.fullmatch(r"[1-9][0-9]*", pr_number):
        raise IngestionError("--pr-number must be a positive integer without leading zeros.")
    if match:
        if pr_number is not None and pr_number != match[1]:
            raise IngestionError("--pr-number conflicts with the selected dbt target schema.")
        pr_number = match[1]
    elif schema not in ("SILVER", "BRONZE"):
        raise IngestionError("Selected schema must be SILVER, BRONZE, PR_<number>_SILVER or PR_<number>_BRONZE.")
    if target == "prod" and pr_number is not None:
        raise IngestionError("PR isolation is supported only for Dev.")
    bronze = f"PR_{pr_number}_BRONZE" if pr_number else "BRONZE"
    if len(bronze) > 255:
        raise IngestionError("PR schema name exceeds Snowflake's identifier length limit.")
    return f'"{expected["database"]}"."{bronze}"."PRODUCTS"'


def connect(settings: dict, *, target: str = "dev", query_tag: str | None = None):
    validate_execution(target)
    expected = expected_identity(target)
    if any(settings.get(k) != v for k, v in expected.items()):
        raise IngestionError("Connection settings do not match the expected target identity.")
    import snowflake.connector
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key_path = Path(settings.get("private_key_path", "")).expanduser()
    passphrase = settings.get("private_key_passphrase")
    if not isinstance(passphrase, str) or not passphrase:
        raise IngestionError("The selected profile's encrypted-key passphrase is unavailable; configure its environment variable privately.")
    try:
        # Confirm that the existing key is encrypted and usable before connecting.
        load_pem_private_key(key_path.read_bytes(), password=passphrase.encode("utf-8"))
    except Exception as exc:
        raise IngestionError("Cannot decrypt the selected profile's existing private key; check its file and passphrase privately.") from exc
    return snowflake.connector.connect(
        **{k: settings[k] for k in ("account", *expected, "authenticator")},
        private_key_file=str(key_path),
        private_key_file_pwd=passphrase,
        autocommit=True,
        # Client-side binding avoids server-bind staging DDL inside the transaction.
        paramstyle="pyformat",
        login_timeout=30,
        network_timeout=60,
        session_parameters={
            "QUERY_TAG": query_tag or os.environ.get("SNOWFLAKE_QUERY_TAG", "products_bronze_ingestion"),
            "STATEMENT_TIMEOUT_IN_SECONDS": 120,
            "ABORT_DETACHED_QUERY": True,
        },
    )


def replace_products(connection, table: str, rows: list[tuple[str, ...]], source_filename: str, *, target: str = "dev") -> str:
    validate_execution(target)
    expected = expected_identity(target)
    # Validate identifiers again at the write boundary, even for direct callers.
    schema_pattern = r"(?:BRONZE|PR_[1-9][0-9]*_BRONZE)" if target == "dev" else "BRONZE"
    if not re.fullmatch(fr'"{expected["database"]}"\."{schema_pattern}"\."PRODUCTS"', table):
        raise IngestionError("Refusing an unexpected destination table.")
    if not rows or any(len(row) != 4 or any(not isinstance(v, str) or not v.strip() or "\x00" in v for v in row) for row in rows):
        raise IngestionError("A nonempty validated products dataset is required.")
    batch_id = str(uuid4())
    ingested_at = datetime.now(timezone.utc)
    records = [(*row, source_filename, batch_id, ingested_at) for row in rows]
    column_sql = ", ".join(f'"{name.upper()}"' for name in COLUMNS)
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_DATABASE(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
        if cursor.fetchone() != tuple(expected.values()):
            raise IngestionError("Connected Snowflake session does not match the expected target identity; no DDL or writes performed.")

        # Snowflake DDL implicitly commits; finish it before BEGIN. Never use
        # CREATE OR REPLACE or TRUNCATE to replace a previously loaded dataset.
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {table.rsplit('.', 1)[0]}")
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            '"PRODUCT_ID" VARCHAR, "PRODUCT_NAME" VARCHAR, "CATEGORY" VARCHAR, '
            '"PRICE" VARCHAR, "SOURCE_FILENAME" VARCHAR, "BATCH_ID" VARCHAR, '
            '"INGESTED_AT" TIMESTAMP_TZ)'
        )
        cursor.execute(f"DESCRIBE TABLE {table}")
        actual = [(r[0], r[1], r[2]) for r in cursor.fetchall()]
        if len(actual) != len(COLUMNS) or any(
            name != expected.upper() or kind != "COLUMN" or
            not data_type.startswith("TIMESTAMP_TZ(" if expected == "ingested_at" else "VARCHAR(")
            for (name, data_type, kind), expected in zip(actual, COLUMNS)
        ):
            raise IngestionError("Existing PRODUCTS table has an incompatible layout; its data was not modified.")

        cursor.execute("BEGIN TRANSACTION")
        try:
            cursor.execute(f"DELETE FROM {table}")
            cursor.executemany(
                f"INSERT INTO {table} ({column_sql}) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                records,
            )
            cursor.execute(f"SELECT {column_sql} FROM {table}")
            if Counter(cursor.fetchall()) != Counter(records):
                raise IngestionError("Loaded values or metadata did not match the input; replacement rolled back.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return batch_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "sample_data" / "products.csv")
    parser.add_argument("--profiles-dir", type=Path, default=Path(os.environ.get("DBT_PROFILES_DIR", ROOT / "config" / "local")))
    parser.add_argument("--profile", help="Defaults to the profile named in dbt/dbt_project.yml")
    parser.add_argument("--target", help="Defaults to the selected profile's target")
    parser.add_argument("--pr-number", help="Use PR_<number>_BRONZE instead of shared BRONZE")
    parser.add_argument("--apply", action="store_true", help="Connect to Dev and replace the complete dataset")
    args = parser.parse_args(argv)
    try:
        rows = read_products(args.csv)
        profile_name = args.profile
        if not profile_name:
            profile_name = yaml.safe_load((ROOT / "dbt" / "dbt_project.yml").read_text(encoding="utf-8"))["profile"]
        raw = selected_profile(args.profiles_dir, profile_name, args.target)
        settings = render_settings(raw)
        table = destination(settings, args.pr_number)
        if not args.apply:
            print(f"DRY RUN: validated {len(rows)} rows from {args.csv.name}; destination {table}.")
            print("No connection or writes. Credentials not checked. Use --apply to replace the dataset.")
            return 0
        settings = render_settings(raw, credentials=True)
        connection = connect(settings)
        try:
            batch_id = replace_products(connection, table, rows, args.csv.name)
        finally:
            connection.close()
        print(f"APPLIED: committed and verified {len(rows)} rows in {table}; batch_id={batch_id}.")
        return 0
    except IngestionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # Connector/dbt exceptions may include credentials or CSV contents.
        print("ERROR: ingestion failed. Rollback is attempted on replacement failures; if connectivity was lost, verify transaction status privately. Check connectivity, grants and table compatibility.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
