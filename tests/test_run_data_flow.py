"""Offline orchestration tests, with dbt processes and Snowflake mocked."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from ingestion import ingest_products as bronze
from ingestion.test_ingest_products import MemoryConnection
from scripts import run_data_flow as flow


PROD_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REF": "refs/heads/main",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_WORKFLOW_REF": "example/repo/.github/workflows/prod-deploy.yml@refs/heads/main",
}


class DataFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profiles_dir = Path(self.temp.name) / "profiles with spaces"
        self.profiles_dir.mkdir()
        self.environment = {"DBT_ENV_SECRET_OFFLINE_FLOW_PASSPHRASE": "offline-secret-sentinel"}
        self.write_profiles()
        self.connection = MemoryConnection()
        self.processes = []
        self.temp_profile_paths = []
        self.process_fail = None
        self.bad_manifest = None
        self.bad_results = None
        self.schema_literal = None

    def write_profiles(self, *, schema="SILVER", overrides=None):
        outputs = {}
        for target, expected in bronze.EXPECTED_BY_TARGET.items():
            outputs[target] = {
                "type": "snowflake", "authenticator": "SNOWFLAKE_JWT",
                "account": "offline.invalid", **expected, "schema": schema,
                "private_key_path": "offline-key.p8",
                "private_key_passphrase": "{{ env_var('DBT_ENV_SECRET_OFFLINE_FLOW_PASSPHRASE') }}",
                "threads": 2,
            }
        for target, override in (overrides or {}).items():
            outputs[target].update(override)
        (self.profiles_dir / "profiles.yml").write_text(yaml.safe_dump({"agent_platform": {"target": "dev", "outputs": outputs}}), encoding="utf-8")

    def manifest(self, database, schema):
        prefix = schema[:-len("SILVER")]
        source = "source.agent_platform.bronze.products"
        silver = "model.agent_platform.stg_products"
        gold = "model.agent_platform.product_category_summary"
        nodes = {
            silver: {"resource_type": "model", "alias": "stg_products", "database": database, "schema": schema, "depends_on": {"nodes": [source]}},
            gold: {"resource_type": "model", "alias": "product_category_summary", "database": database, "schema": prefix + "GOLD", "depends_on": {"nodes": [silver]}},
        }
        for name, parents in (("required_id", [silver]), ("source_metadata", [source]), ("reconciliation", [silver, gold])):
            key = "test.agent_platform." + name
            nodes[key] = {"unique_id": key, "resource_type": "test", "depends_on": {"nodes": parents}}
        manifest = {"nodes": nodes, "sources": {source: {"database": database, "schema": prefix + "BRONZE", "identifier": "PRODUCTS"}}}
        if self.bad_manifest == "schema":
            nodes[gold]["schema"] = "WRONG_GOLD"
        elif self.bad_manifest == "source":
            manifest["sources"][source]["schema"] = "WRONG_BRONZE"
        elif self.bad_manifest == "graph":
            nodes[silver]["depends_on"]["nodes"].append("model.agent_platform.my_first_dbt_model")
        elif self.bad_manifest == "alias":
            nodes[gold]["alias"] = "unreported_destination"
        return manifest

    def dbt_process(self, args, **kwargs):
        self.assertIsInstance(args, list)
        self.assertEqual(args[:3], [sys.executable, "-m", "dbt.cli.main"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["cwd"], flow.ROOT)
        self.assertNotIn("offline-secret-sentinel", " ".join(args))
        command = args[3]
        self.processes.append(command)
        profiles = Path(args[args.index("--profiles-dir") + 1])
        self.temp_profile_paths.append(profiles)
        text = (profiles / "profiles.yml").read_text(encoding="utf-8")
        self.assertNotIn("offline-secret-sentinel", text)
        output = yaml.safe_load(text)["agent_platform"]["outputs"][args[args.index("--target") + 1]]
        self.schema_literal = output["schema"]
        self.assertEqual(kwargs["env"][flow.PASSPHRASE_ENV], "offline-secret-sentinel")
        target_path = Path(args[args.index("--target-path") + 1])
        target_path.mkdir(exist_ok=True)
        manifest = self.manifest(output["database"], output["schema"])
        (target_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if command == "build":
            self.assertEqual(args[args.index("--select") + 1], "+product_category_summary")
            self.assertEqual(len(self.connection.rows), 5)
            records = [{"unique_id": key, "status": "success" if key.startswith("model.") else "pass"} for key in manifest["nodes"]]
            if self.bad_results == "missing":
                records.pop()
            elif self.bad_results == "duplicate":
                records.append(records[-1].copy())
            elif self.bad_results == "warning":
                records[-1]["status"] = "warn"
            elif self.bad_results == "skipped":
                records[-1]["status"] = "skipped"
            elif self.bad_results == "wrong_type_status":
                records[-1]["status"] = "success"
            elif self.bad_results == "failed":
                records[-1]["status"] = "fail"
            elif self.bad_results == "secret_message":
                records[-1].update(status="error", message="offline-secret-sentinel")
            (target_path / "run_results.json").write_text(json.dumps({"results": records}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 2 if self.process_fail == command else 0, "raw-sensitive-output", "raw-sensitive-error")

    def run_cli(self, *args, environment=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {**self.environment, **(environment or {})}
        with patch.dict(os.environ, env, clear=True), patch.object(flow.subprocess, "run", side_effect=self.dbt_process) as process, patch.object(bronze, "connect", return_value=self.connection) as connect, redirect_stdout(stdout), redirect_stderr(stderr):
            status = flow.main(["--profiles-dir", str(self.profiles_dir), *args])
        combined = stdout.getvalue() + stderr.getvalue()
        for secret in ("offline-secret-sentinel", "raw-sensitive-output", "raw-sensitive-error"):
            self.assertNotIn(secret, combined)
        return status, stdout.getvalue(), stderr.getvalue(), process, connect

    def test_default_dry_run_validates_five_rows_without_side_effects(self):
        status, stdout, stderr, process, connect = self.run_cli()
        self.assertEqual(status, 0, stderr)
        self.assertIn("Validated 5 product rows", stdout)
        self.assertIn("DRY RUN", stdout)
        self.assertIn("AGENTWAREHOUSEDEV.SILVER.stg_products", stdout)
        process.assert_not_called()
        connect.assert_not_called()

    def test_actual_dbt_module_preflight_with_dummy_profile_is_offline(self):
        # Exercise the real active-Python subprocess and flags, without real
        # credentials. `dbt parse` does not connect or read the dummy key file.
        env = {**os.environ, **self.environment}
        target_path = Path(self.temp.name) / "actual-parse"
        result = flow.run_dbt("parse", self.profiles_dir, "dev", target_path, env)
        self.assertEqual(result.returncode, 0, "Dummy-profile dbt subprocess parsing failed")
        selected = flow.validate_manifest(
            target_path / "manifest.json", "agent_platform", "AGENTWAREHOUSEDEV",
            {layer: layer for layer in ("BRONZE", "SILVER", "GOLD")},
        )
        self.assertEqual(len(selected), 43)

    def test_pr_dry_run_routes_all_layers(self):
        status, stdout, stderr, process, connect = self.run_cli("--pr-number", "42")
        self.assertEqual(status, 0, stderr)
        for layer in ("BRONZE", "SILVER", "GOLD"):
            self.assertIn("PR_42_" + layer, stdout)
        process.assert_not_called()
        connect.assert_not_called()

    def test_apply_order_and_argument_list_and_secret_cleanup(self):
        status, stdout, stderr, _, connect = self.run_cli("--apply")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.processes, ["parse", "build"])
        connect.assert_called_once()
        self.assertEqual(self.connection.commits, 1)
        self.assertTrue(self.connection.closed)
        self.assertIn("APPLIED", stdout)
        self.assertIn("pass=3", stdout)
        self.assertTrue(all(not directory.exists() for directory in self.temp_profile_paths))
        self.assertNotIn(flow.PASSPHRASE_ENV, os.environ)

    def test_apply_pr_overrides_literal_schema_consistently(self):
        status, stdout, stderr, _, _ = self.run_cli("--apply", "--pr-number", "42")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.schema_literal, "PR_42_SILVER")
        self.assertIn('"PR_42_BRONZE"', stdout)

    def test_profile_pr_number_is_used_without_flag(self):
        self.write_profiles(schema="PR_7_SILVER")
        status, stdout, stderr, _, _ = self.run_cli("--apply")
        self.assertEqual(status, 0, stderr)
        self.assertEqual(self.schema_literal, "PR_7_SILVER")
        self.assertIn("PR_7_GOLD", stdout)

    def test_rejects_invalid_and_conflicting_pr_numbers_before_execution(self):
        for number in ("0", "01", "-2", "42; dangerous"):
            with self.subTest(number=number):
                status, _, _, process, connect = self.run_cli("--apply", "--pr-number", number)
                self.assertEqual(status, 1)
                process.assert_not_called()
                connect.assert_not_called()
        self.write_profiles(schema="PR_7_SILVER")
        status, _, _, process, connect = self.run_cli("--apply", "--pr-number", "8")
        self.assertEqual(status, 1)
        process.assert_not_called()
        connect.assert_not_called()

    def test_wrong_identity_never_starts_a_process_or_connects(self):
        for name in bronze.EXPECTED:
            with self.subTest(field=name):
                self.write_profiles(overrides={"dev": {name: "WRONG"}})
                status, _, _, process, connect = self.run_cli("--apply")
                self.assertEqual(status, 1)
                process.assert_not_called()
                connect.assert_not_called()

    def test_missing_passphrase_stops_before_process_or_connection(self):
        self.environment = {}
        status, _, _, process, connect = self.run_cli("--apply")
        self.assertEqual(status, 1)
        process.assert_not_called()
        connect.assert_not_called()

    def test_bad_csv_stops_before_execution(self):
        with patch.object(bronze, "read_products", side_effect=bronze.IngestionError("Malformed CSV")):
            status, _, _, process, connect = self.run_cli("--apply")
        self.assertEqual(status, 1)
        process.assert_not_called()
        connect.assert_not_called()

    def test_preflight_process_failure_never_loads_bronze(self):
        self.process_fail = "parse"
        status, _, _, _, connect = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertEqual(self.processes, ["parse"])
        connect.assert_not_called()

    def test_preflight_wrong_source_schema_or_graph_never_loads_bronze(self):
        for failure in ("schema", "source", "graph", "alias"):
            with self.subTest(failure=failure):
                self.bad_manifest = failure
                status, _, _, _, connect = self.run_cli("--apply")
                self.assertEqual(status, 1)
                connect.assert_not_called()

    def test_ingestion_failure_rolls_back_and_never_builds(self):
        self.connection.failure = "insert"
        status, _, stderr, _, _ = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertEqual(self.processes, ["parse"])
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertEqual(self.connection.rows, [("previous dataset",)])
        self.assertTrue(self.connection.closed)
        self.assertNotIn("Bronze was committed", stderr)

    def test_build_failure_preserves_bronze_and_reports_stage(self):
        self.process_fail = "build"
        status, _, stderr, _, _ = self.run_cli("--apply")
        self.assertEqual(status, 1)
        self.assertEqual(self.connection.commits, 1)
        self.assertIn("Bronze was committed", stderr)
        self.assertIn("dbt products build/tests", stderr)

    def test_interrupt_during_build_reports_committed_bronze(self):
        def interrupt_build(command, *args, **kwargs):
            if command == "build":
                raise KeyboardInterrupt
            return real_run_dbt(command, *args, **kwargs)

        real_run_dbt = flow.run_dbt
        with patch.object(flow, "run_dbt", side_effect=interrupt_build):
            status, _, stderr, _, _ = self.run_cli("--apply")
        self.assertEqual(status, 130)
        self.assertIn("Bronze was committed", stderr)

    def test_failed_or_incomplete_results_fail_even_with_zero_process_exit(self):
        for failure in ("missing", "failed", "secret_message", "duplicate", "warning", "skipped", "wrong_type_status"):
            with self.subTest(failure=failure):
                self.bad_results = failure
                status, _, _, _, _ = self.run_cli("--apply")
                self.assertEqual(status, 1)

    def test_local_production_apply_is_blocked_before_any_steps(self):
        status, _, stderr, process, connect = self.run_cli("--target", "prod", "--apply")
        self.assertEqual(status, 1)
        self.assertIn("main-branch prod-deploy", stderr)
        process.assert_not_called()
        connect.assert_not_called()

    def test_production_dry_run_is_allowed_without_execution(self):
        status, stdout, stderr, process, connect = self.run_cli("--target", "prod")
        self.assertEqual(status, 0, stderr)
        self.assertIn("AGENTWAREHOUSEPROD", stdout)
        process.assert_not_called()
        connect.assert_not_called()

    def test_production_rejects_pr_flag_or_pr_profile(self):
        for schema, args in (("SILVER", ["--pr-number", "42"]), ("PR_42_SILVER", [])):
            with self.subTest(schema=schema):
                self.write_profiles(schema=schema)
                status, _, _, process, connect = self.run_cli("--target", "prod", *args)
                self.assertEqual(status, 1)
                process.assert_not_called()
                connect.assert_not_called()

    def test_production_guard_rejects_wrong_ref_event_or_workflow(self):
        for name, value in (("GITHUB_REF", "refs/heads/dev"), ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_WORKFLOW_REF", "other-workflow"), ("GITHUB_ACTIONS", "false")):
            with self.subTest(field=name):
                env = {**PROD_ENV, name: value}
                status, _, _, process, connect = self.run_cli("--target", "prod", "--apply", environment=env)
                self.assertEqual(status, 1)
                process.assert_not_called()
                connect.assert_not_called()

    def test_production_ci_path_uses_prod_identity_and_shared_schemas(self):
        self.connection.identity = tuple(bronze.EXPECTED_BY_TARGET["prod"].values())
        status, stdout, stderr, _, connect = self.run_cli("--target", "prod", "--apply", environment=PROD_ENV)
        self.assertEqual(status, 0, stderr)
        self.assertIn('"AGENTWAREHOUSEPROD"."BRONZE"."PRODUCTS"', stdout)
        self.assertIn("AGENTWAREHOUSEPROD.GOLD.product_category_summary", stdout)
        self.assertEqual(connect.call_args.kwargs["target"], "prod")
        self.assertEqual(self.connection.commits, 1)

    def test_prod_write_boundary_cannot_bypass_workflow_guard(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(bronze.IngestionError):
            bronze.replace_products(self.connection, '"AGENTWAREHOUSEPROD"."BRONZE"."PRODUCTS"', [("1", "A", "B", "1")], "products.csv", target="prod")
        self.assertEqual(self.connection.statements, [])


if __name__ == "__main__":
    unittest.main()
