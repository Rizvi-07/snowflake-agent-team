"""Offline workflow structure, gate execution and PR lifecycle race checks."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from ingestion import ingest_products as bronze
from scripts import ci_pr_lifecycle as lifecycle


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "agent_platform:example/repo:pr:42:run:"


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # BaseLoader preserves `on` as a key (YAML 1.2 Actions, not YAML 1.1 bool).
        cls.workflows = {name: yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader) for name in ("pr-dev-check.yml", "dev-check.yml", "prod-deploy.yml")}

    def test_pr_events_and_serialized_queue_include_close_and_reopen(self):
        pr = self.workflows["pr-dev-check.yml"]
        self.assertEqual(pr["on"]["pull_request"]["branches"], ["dev", "main"])
        self.assertEqual(set(pr["on"]["pull_request"]["types"]), {"opened", "synchronize", "reopened", "closed"})
        self.assertEqual(pr["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(pr["concurrency"]["queue"], "max")
        self.assertEqual(pr["concurrency"]["group"], "snowflake-pr-dev-${{ github.event.pull_request.number }}")
        self.assertEqual(pr["permissions"], {"contents": "read", "actions": "read", "pull-requests": "read"})

    def test_required_gate_name_dependency_and_always_condition(self):
        gate = self.workflows["pr-dev-check.yml"]["jobs"]["require-pr-validation"]
        self.assertEqual(gate["name"], "PR Dev validation required")
        self.assertEqual(gate["needs"], "pr-dev")
        self.assertIn("always()", gate["if"])
        self.assertIn("github.event.action != 'closed'", gate["if"])
        self.assertEqual(gate["steps"][0]["env"]["VALIDATION_RESULT"], "${{ needs.pr-dev.result }}")

    def test_required_gate_executes_fail_closed_for_every_unsuccessful_result(self):
        bash = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else Path(shutil.which("bash") or "/missing-bash")
        self.assertTrue(bash.is_file(), "Bash is needed to execute the actual branch-protection gate")
        gate = self.workflows["pr-dev-check.yml"]["jobs"]["require-pr-validation"]["steps"][0]["run"]
        base_env = {**os.environ, "GITHUB_REPOSITORY": "example/repo", "PR_HEAD_REPOSITORY": "example/repo", "PR_BASE_BRANCH": "dev", "PR_HEAD_BRANCH": "feat/products"}
        for result in ("success", "failure", "cancelled", "skipped", "timed_out", ""):
            with self.subTest(result=result):
                completed = subprocess.run([str(bash), "-c", gate], env={**base_env, "VALIDATION_RESULT": result}, capture_output=True, text=True)
                self.assertEqual(completed.returncode == 0, result == "success")
        for base, head, repository, expected in (("main", "dev", "example/repo", 0), ("main", "feat/products", "example/repo", 1), ("dev", "feat/products", "fork/repo", 1)):
            with self.subTest(base=base, head=head, repository=repository):
                completed = subprocess.run([str(bash), "-c", gate], env={**base_env, "VALIDATION_RESULT": "success", "PR_BASE_BRANCH": base, "PR_HEAD_BRANCH": head, "PR_HEAD_REPOSITORY": repository}, capture_output=True, text=True)
                self.assertEqual(completed.returncode, expected)

    def test_shared_and_production_targets_and_environment_boundaries(self):
        for filename, branch, target, job_name, environment in (
            ("dev-check.yml", "dev", "dev", "check-dev", "development"),
            ("prod-deploy.yml", "main", "prod", "deploy-prod", "production"),
        ):
            with self.subTest(workflow=filename):
                workflow = self.workflows[filename]
                self.assertEqual(workflow["on"]["push"]["branches"], [branch])
                job = workflow["jobs"][job_name]
                self.assertEqual(job["environment"], environment)
                runs = "\n".join(s.get("run", "") for s in job["steps"])
                self.assertIn(f"python scripts/run_data_flow.py --target {target} --profiles-dir config/ci --apply", runs)
                self.assertIn(f"--target {target} --select connection_check", runs)
                self.assertIn(f"--target {target}", runs)
                self.assertNotIn(f"--target {'dev' if target == 'prod' else 'prod'}", runs)
                self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
                if target == "prod":
                    self.assertEqual(job["if"], "github.ref == 'refs/heads/main'")
                else:
                    self.assertEqual(job["env"]["DBT_TARGET_SCHEMA"], "SILVER")

    def test_pr_checkout_and_helper_have_no_fork_or_target_event_escalation(self):
        workflow = self.workflows["pr-dev-check.yml"]
        self.assertNotIn("pull_request_target", workflow["on"])
        job = workflow["jobs"]["pr-dev"]
        self.assertEqual(job["environment"], "development")
        self.assertEqual(job["if"], "github.event.pull_request.head.repo.full_name == github.repository")
        checkout = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")]
        self.assertEqual(len(checkout), 2)
        self.assertEqual(checkout[1]["with"]["ref"], "${{ github.event.pull_request.head.sha }}")
        helper = next(s for s in job["steps"] if s.get("run") == "python scripts/ci_pr_lifecycle.py")
        self.assertEqual(helper["env"]["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertEqual(job["env"]["DBT_TARGET_SCHEMA"], "PR_${{ github.event.pull_request.number }}_SILVER")

    def test_all_workflows_install_pins_and_always_remove_their_key(self):
        for filename, workflow in self.workflows.items():
            for name, job in workflow["jobs"].items():
                if name == "require-pr-validation":
                    self.assertNotIn("environment", job)
                    self.assertNotIn("SNOWFLAKE_ACCOUNT", job.get("env", {}))
                    continue
                with self.subTest(workflow=filename, job=name):
                    steps = job["steps"]
                    self.assertTrue(any(s.get("run") == "python -m pip install -r requirements.txt" for s in steps))
                    cleanup = steps[-1]
                    self.assertEqual(cleanup["if"], "always()")
                    key = "snowflake-prod-key.p8" if filename == "prod-deploy.yml" else "snowflake-dev-key.p8"
                    self.assertEqual(cleanup["run"].strip(), f'rm -f "$RUNNER_TEMP/{key}"')
                    for step in steps:
                        self.assertNotIn("continue-on-error", step)
                        self.assertNotIn("|| true", step.get("run", ""))
                    self.assertEqual(len([s for s in steps if "SNOWFLAKE_PRIVATE_KEY" in s.get("env", {})]), 1)

    def test_actual_key_preparation_validates_target_before_writing(self):
        for filename, workflow in self.workflows.items():
            target = "prod" if filename == "prod-deploy.yml" else "dev"
            job = next(j for n, j in workflow["jobs"].items() if n != "require-pr-validation")
            step = next(s for s in job["steps"] if "SNOWFLAKE_PRIVATE_KEY" in s.get("env", {}))
            source = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            compile(source, filename, "exec")
            with tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, **{f"SNOWFLAKE_{k.upper()}": v for k, v in bronze.EXPECTED_BY_TARGET[target].items()}, "RUNNER_TEMP": directory, "DBT_TARGET_SCHEMA": "PR_42_SILVER", "SNOWFLAKE_ACCOUNT": "offline.invalid", "SNOWFLAKE_PRIVATE_KEY": "offline-only-key"}
                path = Path(directory) / f"snowflake-{target}-key.p8"
                result = subprocess.run([sys.executable, "-c", source], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(path.read_text(), "offline-only-key")
                path.unlink()
                result = subprocess.run([sys.executable, "-c", source], env={**env, "SNOWFLAKE_DATABASE": "WRONG"}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(path.exists())

    def test_ci_dbt_credentials_keep_the_exact_pr_query_tag_on_all_targets(self):
        from dbt.adapters.snowflake.connections import SnowflakeCredentials

        tag = PREFIX + "123:2"
        for target in ("dev", "prod"):
            environment = {
                **{f"SNOWFLAKE_{k.upper()}": v for k, v in bronze.EXPECTED_BY_TARGET[target].items()},
                "SNOWFLAKE_ACCOUNT": "offline.invalid", "RUNNER_TEMP": "offline-temp",
                "DBT_ENV_SECRET_SNOWFLAKE_KEY_PASSPHRASE": "offline-secret",
                "SNOWFLAKE_QUERY_TAG": tag,
            }
            with self.subTest(target=target), patch.dict(os.environ, environment, clear=True):
                raw = bronze.selected_profile(ROOT / "config/ci", "agent_platform", target)
                rendered = bronze.profile_renderer().render_data(raw)
                creds = SnowflakeCredentials.from_dict({k: v for k, v in rendered.items() if k not in ("type", "threads")})
                self.assertEqual(creds.query_tag, tag)


class FakeConnection:
    def __init__(self, histories=None):
        self.histories = list(histories or [[]])
        self.statements = []
        self.identity = tuple(bronze.EXPECTED.values())
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self.last_sql = sql

    def fetchone(self):
        if self.last_sql == "SELECT CURRENT_SESSION()":
            return (999,)
        return self.identity

    def fetchall(self):
        if len(self.histories) > 1:
            return self.histories.pop(0)
        return self.histories[0]

    def close(self):
        self.closed = True


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pr = {
            "state": "open",
            "head": {"sha": "a" * 40, "ref": "feat/products", "repo": {"full_name": "example/repo"}},
            "base": {"sha": "b" * 40, "ref": "dev", "repo": {"full_name": "example/repo"}},
        }
        self.event = {"number": 42, "action": "opened", "pull_request": deepcopy(self.pr)}
        self.event_path = Path(self.temp.name) / "event.json"
        self.environment = {
            "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REPOSITORY": "example/repo", "GITHUB_EVENT_PATH": str(self.event_path),
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_TOKEN": "offline-token-secret",
            "DBT_TARGET_SCHEMA": "PR_42_SILVER",
        }
        self.run = {"status": "in_progress", "conclusion": None, "run_attempt": 2}
        self.clock = 0

    def sleep(self, seconds):
        self.clock += seconds

    def fake_get(self, resource):
        return self.run if resource.startswith("actions/") else self.pr

    def github(self):
        self.event_path.write_text(json.dumps(self.event))
        with patch.dict(os.environ, self.environment, clear=True):
            return lifecycle.GitHubPR()

    def execute_main(self, *, command_failure=None, connection=None):
        self.event_path.write_text(json.dumps(self.event))
        stdout, stderr = io.StringIO(), io.StringIO()
        connection = connection or FakeConnection()
        with patch.dict(os.environ, self.environment, clear=True), patch.object(lifecycle.GitHubPR, "get", side_effect=self.fake_get), patch.object(bronze, "selected_profile", return_value={}), patch.object(bronze, "render_settings", return_value={}), patch.object(bronze, "connect", return_value=connection) as connect, patch.object(lifecycle, "drain") as drain, patch.object(lifecycle, "command", side_effect=command_failure) as command, redirect_stdout(stdout), redirect_stderr(stderr):
            status = lifecycle.main()
        self.assertNotIn("offline-token-secret", stdout.getvalue() + stderr.getvalue())
        return status, connection, connect, drain, command

    def test_validation_and_release_pr_use_dev_isolation_and_both_commands(self):
        for base, head in (("dev", "feat/products"), ("main", "dev")):
            with self.subTest(base=base):
                self.pr["base"]["ref"], self.pr["head"]["ref"] = base, head
                self.event["pull_request"] = deepcopy(self.pr)
                status, conn, _, drain, command = self.execute_main()
                self.assertEqual(status, 0)
                drain.assert_called_once_with(conn, PREFIX)
                first, second = command.call_args_list
                self.assertIn("--pr-number", first.args[0])
                self.assertIn("42", first.args[0])
                self.assertIn("--apply", first.args[0])
                self.assertIn("dev", first.args[0])
                self.assertIn("connection_check", second.args[0])
                self.assertEqual(first.args[1]["SNOWFLAKE_QUERY_TAG"], PREFIX + "123:2")
                self.assertTrue(conn.closed)

    def test_stale_close_after_reopen_does_not_connect_or_drop(self):
        self.event["action"] = "closed"
        status, conn, connect, _, command = self.execute_main()
        self.assertEqual(status, 0)
        connect.assert_not_called()
        command.assert_not_called()
        self.assertEqual(conn.statements, [])

    def test_stale_validation_closed_or_changed_head_or_base_cannot_write(self):
        for change in ("closed", "head", "base", "attempt", "cancelled", "fork", "wrong_release"):
            with self.subTest(change=change):
                original, run = deepcopy(self.pr), deepcopy(self.run)
                if change == "closed": self.pr["state"] = "closed"
                elif change == "head": self.pr["head"]["sha"] = "c" * 40
                elif change == "base": self.pr["base"]["sha"] = "c" * 40
                elif change == "attempt": self.run["run_attempt"] = 3
                elif change == "cancelled": self.run.update(status="completed", conclusion="cancelled")
                elif change == "fork": self.pr["head"]["repo"]["full_name"] = "fork/repo"
                else:
                    self.pr["base"]["ref"] = "main"
                    self.event["pull_request"] = deepcopy(self.pr)
                status, _, connect, _, command = self.execute_main()
                self.assertEqual(status, 1)
                connect.assert_not_called()
                command.assert_not_called()
                self.pr, self.run = original, run
                self.event["pull_request"] = deepcopy(self.pr)

    def test_reopened_event_validates_and_never_drops(self):
        self.event["action"] = "reopened"
        status, conn, _, _, command = self.execute_main()
        self.assertEqual(status, 0)
        self.assertEqual(command.call_count, 2)
        self.assertFalse(any(sql.startswith("DROP") for sql, _ in conn.statements))

    def test_products_failure_stops_before_connection_check(self):
        status, _, _, _, command = self.execute_main(command_failure=lifecycle.LifecycleError("Products failed"))
        self.assertEqual(status, 1)
        self.assertEqual(command.call_count, 1)

    def test_connection_check_failure_propagates(self):
        status, _, _, _, command = self.execute_main(command_failure=[None, lifecycle.LifecycleError("dbt failed")])
        self.assertEqual(status, 1)
        self.assertEqual(command.call_count, 2)

    def test_bad_dev_session_refuses_drain_and_drops(self):
        conn = FakeConnection()
        conn.identity = ("AGENTWAREHOUSEPROD", *conn.identity[1:])
        status, _, _, drain, command = self.execute_main(connection=conn)
        self.assertEqual(status, 1)
        drain.assert_not_called()
        command.assert_not_called()
        self.assertEqual(len(conn.statements), 1)

    def test_command_exit_codes_and_raw_output_are_not_ignored_or_leaked(self):
        for code in (0, 1, 2, -9):
            with self.subTest(code=code), patch.object(lifecycle.subprocess, "run", return_value=subprocess.CompletedProcess([], code, "secret-output", "secret-error")) as run:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    if code:
                        with self.assertRaises(lifecycle.LifecycleError): lifecycle.command(["a script.py"], {}, "Step")
                    else: lifecycle.command(["a script.py"], {}, "Step")
                self.assertEqual(run.call_args.args[0], [sys.executable, "a script.py"])
                self.assertTrue(run.call_args.kwargs["capture_output"])
                self.assertNotIn("secret", stdout.getvalue())

    def run_drain(self, conn, **kwargs):
        with patch.object(lifecycle.time, "monotonic", side_effect=lambda: self.clock), patch.object(lifecycle.time, "sleep", side_effect=self.sleep):
            lifecycle.drain(conn, PREFIX, **kwargs)

    def test_drain_cancels_only_exact_pr_writers_then_waits_for_terminal(self):
        active = [("q1", 101, PREFIX + "12:1", "RUNNING"), ("q2", 102, PREFIX + "13:1", "QUEUED")]
        unrelated = [("q3", 103, PREFIX.replace(":42:", ":421:") + "12:1", "RUNNING"), ("q4", 104, "agent_platform:example/repo:dev:run:12:1", "RUNNING"), ("q5", 999, PREFIX.replace(":run:", ":control:") + "123:2", "RUNNING")]
        terminal = [(qid, sid, tag, "FAILED_WITH_ERROR") for qid, sid, tag, _ in active]
        conn = FakeConnection([active + unrelated, active + unrelated, terminal + unrelated])
        self.run_drain(conn, quiet_seconds=4)
        cancelled = [params for sql, params in conn.statements if "SYSTEM$CANCEL_QUERY" in sql]
        aborted = [params for sql, params in conn.statements if "SYSTEM$ABORT_SESSION" in sql]
        self.assertEqual(cancelled, [("q1",), ("q2",)])
        self.assertEqual(aborted, [(101,), (102,)])
        self.assertGreaterEqual(self.clock, 8)
        for sql, _ in conn.statements:
            if "QUERY_HISTORY" in sql:
                self.assertNotIn("END_TIME_RANGE_END", sql)
                self.assertNotIn("ACCOUNT_USAGE", sql)

    def test_drain_aborts_detached_cleanup_before_reopened_validation(self):
        old_drop = ("old-drop", 201, PREFIX.replace(":run:", ":control:") + "120:1", "RUNNING")
        own_query = ("own-read", 999, PREFIX.replace(":run:", ":control:") + "123:2", "RUNNING")
        conn = FakeConnection([[old_drop, own_query], [(old_drop[0], 201, old_drop[2], "FAILED_WITH_ERROR"), own_query]])
        self.run_drain(conn, quiet_seconds=4)
        cancelled = [params for sql, params in conn.statements if "SYSTEM$CANCEL_QUERY" in sql]
        aborted = [params for sql, params in conn.statements if "SYSTEM$ABORT_SESSION" in sql]
        self.assertEqual(cancelled, [("old-drop",)])
        self.assertEqual(aborted, [(201,)])
        self.assertGreaterEqual(self.clock, 6)

    def test_drain_catches_query_appearing_during_quiet_period(self):
        conn = FakeConnection([[], [("late", 101, PREFIX + "12:1", "BLOCKED")], []])
        self.run_drain(conn, quiet_seconds=4)
        self.assertTrue(any(params == ("late",) for _, params in conn.statements))
        self.assertGreaterEqual(self.clock, 8)

    def test_uncertain_history_or_nonterminal_queries_prevent_drop(self):
        for history in ([('q', 1, PREFIX + '12:1', 'RUNNING')], [('q', 1, PREFIX + '12:1', 'SUCCESS')] * lifecycle.HISTORY_LIMIT):
            with self.subTest(size=len(history)):
                self.clock = 0
                conn = FakeConnection([history])
                github = SimpleNamespace(prefix=PREFIX, number="42", closed=lambda: True)
                with patch.object(lifecycle.time, "monotonic", side_effect=lambda: self.clock), patch.object(lifecycle.time, "sleep", side_effect=self.sleep), self.assertRaises(lifecycle.LifecycleError):
                    lifecycle.cleanup(conn, github)
                self.assertFalse(any(sql.startswith("DROP") for sql, _ in conn.statements))

    def test_cleanup_exact_three_schemas_is_idempotent_for_both_close_outcomes(self):
        for merged in (False, True):
            with self.subTest(merged=merged):
                self.event["action"], self.pr["state"] = "closed", "closed"
                self.pr["merged"] = merged
                status, conn, _, _, command = self.execute_main()
                self.assertEqual(status, 0)
                self.assertEqual([sql for sql, _ in conn.statements if sql.startswith("DROP")], [f'DROP SCHEMA IF EXISTS "AGENTWAREHOUSEDEV"."PR_42_{layer}" CASCADE' for layer in ("GOLD", "SILVER", "BRONZE")])
                command.assert_not_called()

    def test_reopen_during_cleanup_stops_remaining_drops(self):
        conn = FakeConnection()
        github = SimpleNamespace(prefix=PREFIX, number="42", closed=unittest.mock.Mock(side_effect=[True, True, False]))
        with patch.object(lifecycle, "drain"):
            lifecycle.cleanup(conn, github)
        drops = [sql for sql, _ in conn.statements if sql.startswith("DROP")]
        self.assertEqual(drops, ['DROP SCHEMA IF EXISTS "AGENTWAREHOUSEDEV"."PR_42_GOLD" CASCADE'])

    def test_invalid_namespace_and_schema_never_cancel_other_prs_or_drop_shared(self):
        conn = FakeConnection()
        for prefix in ("", "agent_platform:", PREFIX.replace("42", "0")):
            with self.subTest(prefix=prefix), self.assertRaises(lifecycle.LifecycleError):
                lifecycle.drain(conn, prefix)
        self.assertEqual(conn.statements, [])
        with patch.object(lifecycle, "drain"), self.assertRaises(lifecycle.LifecycleError):
            lifecycle.cleanup(conn, SimpleNamespace(prefix=PREFIX, number='42";DROP SCHEMA SILVER', closed=lambda: True))
        self.assertFalse(any(sql.startswith("DROP") for sql, _ in conn.statements))

    def test_cancellation_privilege_or_api_failure_prevents_cleanup(self):
        conn = FakeConnection([[("q1", 101, PREFIX + "12:1", "RUNNING")]])
        real_execute = conn.execute

        def execute(sql, params=None):
            if "SYSTEM$ABORT_SESSION" in sql: raise RuntimeError("denied")
            real_execute(sql, params)

        with patch.object(conn, "execute", side_effect=execute), self.assertRaises(RuntimeError):
            lifecycle.cleanup(conn, SimpleNamespace(prefix=PREFIX, number="42", closed=lambda: True))
        self.assertFalse(any(sql.startswith("DROP") for sql, _ in conn.statements))
        with self.assertRaises(RuntimeError):
            lifecycle.cleanup(conn, SimpleNamespace(prefix=PREFIX, number="42", closed=unittest.mock.Mock(side_effect=RuntimeError("API unavailable"))))


if __name__ == "__main__":
    unittest.main()
