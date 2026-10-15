"""Offline safety tests; no Snowflake connection or real credentials required."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ingestion import ingest_products as loader


TABLE = '"AGENTWAREHOUSEDEV"."BRONZE"."PRODUCTS"'
HEADER = ",".join(loader.SOURCE_COLUMNS) + "\n"


class MemoryConnection:
    """Model transaction state and failures, preserving the previous snapshot."""

    def __init__(self, *, failure=None):
        self.rows = [("previous dataset",)]
        self.before = None
        self.failure = failure
        self.statements = []
        self.identity = tuple(loader.EXPECTED.values())
        self.layout = [(c.upper(), "TIMESTAMP_TZ(9)" if c == "ingested_at" else "VARCHAR(16777216)", "COLUMN") for c in loader.COLUMNS]
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        self.statements.append(sql)
        if sql.startswith(("CREATE", "DESCRIBE")) and self.before is not None:
            raise AssertionError("DDL inside replacement transaction")
        if sql == "BEGIN TRANSACTION":
            self.before = deepcopy(self.rows)
        elif sql.startswith("DELETE"):
            self.rows.clear()
        self.last = sql
        return self

    def executemany(self, sql, records):
        self.statements.append(sql)
        self.rows.extend(records[:1])
        if self.failure == "insert":
            raise RuntimeError("simulated partial insert failure")
        if self.failure == "interrupt":
            raise KeyboardInterrupt
        self.rows.extend(records[1:])

    def fetchone(self):
        return self.identity

    def fetchall(self):
        if self.last.startswith("DESCRIBE"):
            return self.layout
        if self.failure == "verification_query":
            raise RuntimeError("simulated verification query failure")
        rows = deepcopy(self.rows)
        if self.failure == "count":
            rows.pop()
        elif self.failure == "value":
            rows[0] = ("corrupted", *rows[0][1:])
        elif self.failure == "metadata":
            rows[0] = (*rows[0][:5], "wrong-batch", rows[0][6])
        return rows

    def commit(self):
        if self.failure == "commit":
            raise RuntimeError("simulated commit failure")
        self.commits += 1
        self.before = None

    def rollback(self):
        self.rollbacks += 1
        self.rows = self.before
        self.before = None

    def close(self):
        self.closed = True


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.csv_path = self.directory / "products.csv"
        self.raw = {
            "type": "snowflake", "authenticator": "SNOWFLAKE_JWT",
            "account": "offline-test", **loader.EXPECTED, "schema": "SILVER",
            "private_key_path": "{{ env_var('OFFLINE_KEY_PATH') }}",
            "private_key_passphrase": "{{ env_var('DBT_ENV_SECRET_OFFLINE_PASSPHRASE') }}",
        }
        self.write_profile()

    def write_profile(self, output=None):
        profiles = {"agent_platform": {"target": "dev", "outputs": {
            "dev": output or self.raw,
            "prod": {"account": "{{ env_var('UNUSED_PROD_SECRET') }}"},
        }}}
        (self.directory / "profiles.yml").write_text(yaml.safe_dump(profiles), encoding="utf-8")

    def write_csv(self, contents):
        self.csv_path.write_text(contents, encoding="utf-8", newline="")
        return self.csv_path

    def test_exact_sample_and_raw_strings(self):
        rows = loader.read_products(loader.ROOT / "sample_data" / "products.csv")
        self.assertEqual(rows, [
            ("1", "Laptop", "Electronics", "899.99"),
            ("2", "Monitor", "Electronics", "249.50"),
            ("3", "Desk", "Furniture", "199.00"),
            ("4", "Chair", "Furniture", "99.90"),
            ("5", "Keyboard", "Electronics", "49.90"),
        ])

    def test_preserves_whitespace_leading_zeros_and_quotes(self):
        path = self.write_csv(HEADER + '001,"Desk, large"," Office ",0199.00\n')
        self.assertEqual(loader.read_products(path), [("001", "Desk, large", " Office ", "0199.00")])

    def test_bom_unicode_and_quoted_newlines(self):
        path = self.write_csv('\ufeff' + HEADER + '1,"Café\nchair",Furniture,99.90\n')
        self.assertEqual(loader.read_products(path)[0][1], "Café\nchair")

    def test_rejects_malformed_or_empty_csv(self):
        cases = [
            "", HEADER, "id,product_name,category,price\n1,A,B,1\n",
            "product_name,product_id,category,price\nA,1,B,1\n",
            HEADER.rstrip() + ",extra\n1,A,B,1,x\n",
            HEADER + "1,A,B\n", HEADER + "1,A,B,1,extra\n",
            HEADER + "1,,B,1\n", HEADER + "1,A,B,  \n",
            HEADER + "\n", HEADER + '1,"unterminated,B,1\n',
            HEADER + '1,"A"junk,B,1\n', HEADER + "1,A\x00,B,1\n",
            HEADER + '1,Lap"top,B,1.00\n',
            HEADER + '1, "Laptop",B,1.00\n',
            HEADER + "1,A,B,1\n2,invalid\n",
        ]
        for contents in cases:
            with self.subTest(contents=repr(contents)), self.assertRaises(loader.IngestionError):
                loader.read_products(self.write_csv(contents))

    def test_escaped_quotes_and_crlf_are_preserved(self):
        path = self.write_csv(HEADER.replace('\n', '\r\n') + '1,"Desk ""large""",Furniture,199.00\r\n')
        self.assertEqual(loader.read_products(path), [("1", 'Desk "large"', "Furniture", "199.00")])

    def test_invalid_utf8_and_missing_file(self):
        self.csv_path.write_bytes(b"\xff")
        with self.assertRaises(loader.IngestionError):
            loader.read_products(self.csv_path)
        with self.assertRaises(loader.IngestionError):
            loader.read_products(self.directory / "missing.csv")

    def test_selected_target_and_dry_settings_need_no_key_secrets(self):
        with patch.dict(os.environ, {}, clear=True):
            raw = loader.selected_profile(self.directory, "agent_platform", None)
            settings = loader.render_settings(raw)
        self.assertEqual(loader.destination(settings, None), TABLE)
        self.assertNotIn("private_key_passphrase", settings)

    def test_dbt_renders_secret_environment_variable(self):
        secret = "offline-only-passphrase"
        with patch.dict(os.environ, {"OFFLINE_KEY_PATH": "offline-key.p8", "DBT_ENV_SECRET_OFFLINE_PASSPHRASE": secret}):
            settings = loader.render_settings(self.raw, credentials=True)
        self.assertEqual(settings["private_key_passphrase"], secret)

    def test_rejects_each_wrong_identity_before_connecting(self):
        for name in loader.EXPECTED:
            with self.subTest(field=name), self.assertRaises(loader.IngestionError):
                loader.render_settings({**self.raw, name: "PRODUCTION_OR_UNEXPECTED"})

    def test_rejects_password_authentication_or_missing_key(self):
        for override in ({"authenticator": "snowflake"}, {"private_key_path": ""}, {"type": "postgres"}):
            with self.subTest(override=override), self.assertRaises(loader.IngestionError):
                loader.render_settings({**self.raw, **override})

    def test_pr_destinations_and_shared_dev(self):
        for schema, number, expected in [
            ("SILVER", None, "BRONZE"), ("BRONZE", None, "BRONZE"),
            ("SILVER", "42", "PR_42_BRONZE"), ("PR_42_SILVER", None, "PR_42_BRONZE"),
            ("PR_42_BRONZE", "42", "PR_42_BRONZE"),
        ]:
            with self.subTest(schema=schema, number=number):
                self.assertEqual(loader.destination({"schema": schema}, number), f'"AGENTWAREHOUSEDEV"."{expected}"."PRODUCTS"')

    def test_rejects_unsafe_or_conflicting_pr_destinations(self):
        for schema, number in [
            ("PUBLIC", None), ("SILVER", "0"), ("SILVER", "01"),
            ("SILVER", "1;DROP SCHEMA BRONZE"), ("SILVER", "-1"),
            ("PR_42_SILVER", "43"), ("PR_0_SILVER", None), ("SILVER", "1" * 256),
        ]:
            with self.subTest(schema=schema, number=number), self.assertRaises(loader.IngestionError):
                loader.destination({"schema": schema}, number)

    def test_successful_load_and_rerun_replace_rows_and_metadata(self):
        connection = MemoryConnection()
        rows = loader.read_products(loader.ROOT / "sample_data" / "products.csv")
        first_batch = loader.replace_products(connection, TABLE, rows, "products.csv")
        self.assertEqual([r[:4] for r in connection.rows], rows)
        self.assertTrue(all(r[4] == "products.csv" and r[5] == first_batch and r[6].utcoffset().total_seconds() == 0 for r in connection.rows))
        self.assertEqual(len({r[6] for r in connection.rows}), 1)
        second_batch = loader.replace_products(connection, TABLE, rows, "products.csv")
        self.assertNotEqual(first_batch, second_batch)
        self.assertEqual(len(connection.rows), 5)
        self.assertTrue(all(r[5] == second_batch for r in connection.rows))
        self.assertEqual(connection.commits, 2)
        self.assertEqual(connection.rollbacks, 0)

    def test_partial_insert_verification_and_commit_failures_restore_previous_rows(self):
        rows = loader.read_products(loader.ROOT / "sample_data" / "products.csv")
        for failure in ("insert", "count", "value", "metadata", "verification_query", "commit", "interrupt"):
            with self.subTest(failure=failure):
                connection = MemoryConnection(failure=failure)
                with self.assertRaises((loader.IngestionError, RuntimeError, KeyboardInterrupt)):
                    loader.replace_products(connection, TABLE, rows, "products.csv")
                self.assertEqual(connection.rows, [("previous dataset",)])
                self.assertEqual(connection.rollbacks, 1)
                self.assertEqual(connection.commits, 0)

    def test_smaller_snapshot_removes_previous_products(self):
        connection = MemoryConnection()
        rows = loader.read_products(loader.ROOT / "sample_data" / "products.csv")
        loader.replace_products(connection, TABLE, rows, "products.csv")
        loader.replace_products(connection, TABLE, rows[:2], "products.csv")
        self.assertEqual([r[:4] for r in connection.rows], rows[:2])

    def test_ci_profile_uses_rendered_pr_schema_and_ignores_prod_target(self):
        environment = {
            "SNOWFLAKE_ACCOUNT": "offline-test",
            **{f"SNOWFLAKE_{k.upper()}": v for k, v in loader.EXPECTED.items()},
            "DBT_TARGET_SCHEMA": "PR_123_SILVER",
        }
        with patch.dict(os.environ, environment, clear=True):
            raw = loader.selected_profile(loader.ROOT / "config" / "ci", "agent_platform", "dev")
            settings = loader.render_settings(raw)
        self.assertEqual(loader.destination(settings, None), '"AGENTWAREHOUSEDEV"."PR_123_BRONZE"."PRODUCTS"')

    def test_connected_identity_mismatch_performs_no_ddl(self):
        for index in range(4):
            connection = MemoryConnection()
            identity = list(connection.identity)
            identity[index] = "UNEXPECTED"
            connection.identity = tuple(identity)
            with self.subTest(index=index), self.assertRaises(loader.IngestionError):
                loader.replace_products(connection, TABLE, [("1", "A", "B", "1")], "products.csv")
            self.assertEqual(len(connection.statements), 1)
            self.assertEqual(connection.rows, [("previous dataset",)])

    def test_incompatible_table_is_not_deleted(self):
        connection = MemoryConnection()
        connection.layout[0] = ("PRODUCT_ID", "NUMBER(38,0)", "COLUMN")
        with self.assertRaises(loader.IngestionError):
            loader.replace_products(connection, TABLE, [("001", "A", "B", "1")], "products.csv")
        self.assertFalse(any(sql.startswith(("BEGIN", "DELETE")) for sql in connection.statements))

    def test_write_boundary_rejects_wrong_destination_or_empty_rows(self):
        for table, rows in [(TABLE.replace("DEV", "PROD"), [("1", "A", "B", "1")]), (TABLE, [])]:
            connection = MemoryConnection()
            with self.subTest(table=table, rows=rows), self.assertRaises(loader.IngestionError):
                loader.replace_products(connection, table, rows, "products.csv")
            self.assertEqual(connection.statements, [])

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = loader.main(["--profiles-dir", str(self.directory), "--csv", str(self.csv_path), *args])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_never_connects(self):
        self.write_csv(HEADER + "1,A,B,1.00\n")
        with patch.object(loader, "connect") as connect:
            status, stdout, stderr = self.run_cli()
        connect.assert_not_called()
        self.assertEqual(status, 0, stderr)
        self.assertIn("DRY RUN: validated 1 rows", stdout)

    def test_bad_final_record_with_apply_never_connects(self):
        self.write_csv(HEADER + "1,A,B,1\n2,incomplete\n")
        with patch.object(loader, "connect") as connect:
            status, _, _ = self.run_cli("--apply")
        self.assertEqual(status, 1)
        connect.assert_not_called()

    def test_missing_secret_with_apply_never_connects_or_leaks_values(self):
        self.write_csv(HEADER + "1,A,B,1\n")
        with patch.dict(os.environ, {}, clear=True), patch.object(loader, "connect") as connect:
            status, _, stderr = self.run_cli("--apply")
        self.assertEqual(status, 1)
        connect.assert_not_called()
        self.assertIn("Cannot render selected dbt target", stderr)

    def test_connector_errors_are_redacted(self):
        self.write_csv(HEADER + "1,A,B,1\n")
        with patch.dict(os.environ, {"OFFLINE_KEY_PATH": "offline", "DBT_ENV_SECRET_OFFLINE_PASSPHRASE": "offline-secret"}), patch.object(loader, "connect", side_effect=RuntimeError("sensitive-value")):
            status, _, stderr = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertNotIn("sensitive-value", stderr)
        self.assertNotIn("offline-secret", stderr)

    def test_apply_uses_selected_profile_and_closes_connection(self):
        self.write_csv(HEADER + "1,A,B,1.00\n")
        connection = MemoryConnection()
        with patch.dict(os.environ, {"OFFLINE_KEY_PATH": "offline", "DBT_ENV_SECRET_OFFLINE_PASSPHRASE": "offline-secret"}), patch.object(loader, "connect", return_value=connection):
            status, stdout, stderr = self.run_cli("--apply", "--pr-number", "42")
        self.assertEqual(status, 0, stderr)
        self.assertIn('"PR_42_BRONZE"', stdout)
        self.assertTrue(connection.closed)

    def test_encrypted_key_is_reused_without_exposing_passphrase(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        passphrase = "offline-key-passphrase"
        key_path = self.directory / "test-key.p8"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(passphrase.encode())))
        settings = {**loader.render_settings(self.raw), "private_key_path": str(key_path), "private_key_passphrase": passphrase}
        with patch.dict(os.environ, {"SNOWFLAKE_QUERY_TAG": "agent_platform:example/repo:pr:42:run:123:2"}), patch("snowflake.connector.connect") as connect:
            loader.connect(settings)
        self.assertEqual(connect.call_args.kwargs["private_key_file"], str(key_path))
        self.assertEqual(connect.call_args.kwargs["private_key_file_pwd"], passphrase)
        self.assertEqual(connect.call_args.kwargs["paramstyle"], "pyformat")
        self.assertEqual(connect.call_args.kwargs["session_parameters"]["QUERY_TAG"], "agent_platform:example/repo:pr:42:run:123:2")
        self.assertTrue(connect.call_args.kwargs["session_parameters"]["ABORT_DETACHED_QUERY"])
        with patch.dict(os.environ, {"SNOWFLAKE_QUERY_TAG": "writer"}), patch("snowflake.connector.connect") as connect:
            loader.connect(settings, query_tag="coordinator")
        self.assertEqual(connect.call_args.kwargs["session_parameters"]["QUERY_TAG"], "coordinator")
        for password in ("wrong", ""):
            with self.subTest(password=password), patch("snowflake.connector.connect") as connect, self.assertRaises(loader.IngestionError):
                loader.connect({**settings, "private_key_passphrase": password})
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
