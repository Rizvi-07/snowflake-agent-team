"""Serialized PR validation/cleanup with live GitHub checks and Snowflake drain.

Only called by pr-dev-check.yml. Never use pull_request_target to run PR code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from ingestion import ingest_products as bronze


TERMINAL = {"SUCCESS", "FAILED_WITH_ERROR", "FAILED_WITH_INCIDENT"}
HISTORY_LIMIT = 10000


class LifecycleError(Exception):
    """A safe diagnostic, with no tokens, SQL or key material."""


class GitHubPR:
    def __init__(self):
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
            raise LifecycleError("PR lifecycle must run in GitHub Actions on pull_request.")
        self.repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise LifecycleError("Invalid repository context.")
        self.event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        self.number = str(self.event["number"])
        if not re.fullmatch(r"[1-9][0-9]*", self.number):
            raise LifecycleError("Invalid PR number.")
        self.action = self.event["action"]
        if self.action not in ("opened", "synchronize", "reopened", "closed"):
            raise LifecycleError("Unsupported PR event.")
        self.run_id, self.attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
        if not all(re.fullmatch(r"[1-9][0-9]*", v) for v in (self.run_id, self.attempt)):
            raise LifecycleError("Invalid workflow run context.")
        self.prefix = f"agent_platform:{self.repository}:pr:{self.number}:run:"
        self.query_tag = f"{self.prefix}{self.run_id}:{self.attempt}"

    def get(self, resource: str):
        # GitHub.com only; never send the token to a URL from PR payload content.
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{resource}",
            headers={
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Cache-Control": "no-cache",
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def current(self):
        run = self.get(f"actions/runs/{self.run_id}")
        if str(run["run_attempt"]) != self.attempt or run["status"] != "in_progress" or run.get("conclusion") is not None:
            raise LifecycleError("This workflow attempt is no longer active.")
        pr = self.get(f"pulls/{self.number}")
        if pr["head"]["repo"]["full_name"] != self.repository or pr["base"]["repo"]["full_name"] != self.repository:
            raise LifecycleError("Fork PRs cannot access Dev credentials or schemas.")
        if pr["base"]["ref"] not in ("dev", "main"):
            raise LifecycleError("Unexpected PR base branch.")
        return pr

    def validation_current(self):
        pr = self.current()
        original = self.event["pull_request"]
        if pr["state"] != "open" or pr["head"]["sha"] != original["head"]["sha"]:
            raise LifecycleError("Validation event is stale: PR closed or head changed.")
        if (pr["base"]["ref"], pr["head"]["ref"]) != (original["base"]["ref"], original["head"]["ref"]):
            raise LifecycleError("Validation event is stale: PR branches changed.")
        if pr["base"]["sha"] != original["base"]["sha"]:
            raise LifecycleError("Validation event is stale: base changed; update the PR before validating again.")
        if pr["base"]["ref"] == "main" and pr["head"]["ref"] != "dev":
            raise LifecycleError("Pull requests into main must come from dev.")
        return pr

    def closed(self):
        return self.current()["state"] == "closed"


def verify_session(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_DATABASE(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
        if cursor.fetchone() != tuple(bronze.EXPECTED.values()):
            raise LifecycleError("Refusing PR operations outside the expected Dev session.")


def drain(connection, prefix: str, *, timeout: float = 180, quiet_seconds: float = 10):
    """Abort this PR's older validation AND cleanup SQL before new work.

    The workflow holds the same per-PR concurrency slot for validation/cleanup,
    with cancel-in-progress:false. Thus older runners have finished before this
    starts; remaining server queries can still run and must be drained here.
    In particular a detached old DROP must finish before reopening can build.
    Never use ACCOUNT_USAGE (latency), END_TIME_RANGE_END (omits running SQL),
    warehouse-wide cancellation, or a LIKE pattern with underscore wildcards.
    """
    if not re.fullmatch(r"agent_platform:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:pr:[1-9][0-9]*:run:", prefix):
        raise LifecycleError("Refusing an unexpected PR query namespace.")
    control_prefix = prefix.removesuffix("run:") + "control:"
    with connection.cursor() as cursor:
        cursor.execute("SELECT CURRENT_SESSION()")
        current_session = int(cursor.fetchone()[0])
    deadline = time.monotonic() + timeout
    quiet_since = None
    cancelled, aborted = set(), set()
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT QUERY_ID, SESSION_ID, QUERY_TAG, EXECUTION_STATUS FROM TABLE("
                '"AGENTWAREHOUSEDEV".INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER('
                "USER_NAME => 'AGENT_DEV_CI', RESULT_LIMIT => 10000, "
                "INCLUDE_CLIENT_GENERATED_STATEMENT => TRUE))"
            )
            history = cursor.fetchall()
            # The function applies LIMIT before filtering. Incomplete visibility
            # must block cleanup rather than incorrectly declaring it safe.
            if len(history) >= HISTORY_LIMIT:
                raise LifecycleError("PR query history reached its visibility limit; no schemas dropped.")
            active = [
                (qid, sid) for qid, sid, tag, status in history
                if int(sid) != current_session
                and isinstance(tag, str) and tag.startswith((prefix, control_prefix))
                and str(status).upper() not in TERMINAL
            ]
            for query_id, session_id in active:
                if query_id not in cancelled:
                    cursor.execute("SELECT SYSTEM$CANCEL_QUERY(%s)", (query_id,))
                    cancelled.add(query_id)
                if session_id not in aborted:
                    cursor.execute("SELECT SYSTEM$ABORT_SESSION(%s)", (int(session_id),))
                    aborted.add(session_id)
        now = time.monotonic()
        if active:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= quiet_seconds:
            print(f"PR Snowflake work drained; cancelled_queries={len(cancelled)}.")
            return
        time.sleep(2)
    raise LifecycleError("PR SQL did not become terminal; no new work or schema drops permitted.")


def cleanup(connection, github: GitHubPR):
    if not github.closed():
        print("PR is open again; stale close cleanup skipped.")
        return
    drain(connection, github.prefix)
    # Dependency order; construct exact names from the validated numeric PR ID.
    for layer in ("GOLD", "SILVER", "BRONZE"):
        if not github.closed():
            print("PR reopened during cleanup; remaining drops skipped.")
            return
        schema = f"PR_{github.number}_{layer}"
        if not re.fullmatch(r"PR_[1-9][0-9]*_(BRONZE|SILVER|GOLD)", schema):
            raise LifecycleError("Refusing an unexpected cleanup schema.")
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "AGENTWAREHOUSEDEV"."{schema}" CASCADE')
        print(f"Removed temporary Dev schema: {schema}")


def command(args: list[str], env: dict, label: str):
    result = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise LifecycleError(f"{label} failed; validation is unsuccessful.")
    print(f"{label} passed.")


def main() -> int:
    try:
        github = GitHubPR()
        if github.action == "closed":
            if not github.closed():
                print("PR is open again; stale close cleanup skipped.")
                return 0
        else:
            github.validation_current()
        schema = f"PR_{github.number}_SILVER"
        if os.environ.get("DBT_TARGET_SCHEMA") != schema:
            raise LifecycleError("Workflow schema does not match this PR.")
        raw = bronze.selected_profile(ROOT / "config" / "ci", "agent_platform", "dev")
        settings = bronze.render_settings(raw, credentials=True)
        control_tag = f"agent_platform:{github.repository}:pr:{github.number}:control:{github.run_id}:{github.attempt}"
        connection = bronze.connect(settings, query_tag=control_tag)
        try:
            verify_session(connection)
            if github.action == "closed":
                cleanup(connection, github)
                return 0
            # Also drain detached SQL before a reopened PR or rerun writes again.
            drain(connection, github.prefix)
        finally:
            connection.close()
        env = os.environ.copy()
        env["SNOWFLAKE_QUERY_TAG"] = github.query_tag
        github.validation_current()
        command(["scripts/run_data_flow.py", "--target", "dev", "--profiles-dir", "config/ci", "--pr-number", github.number, "--apply"], env, "PR products ingestion/build/tests")
        github.validation_current()
        command(["-m", "dbt.cli.main", "build", "--project-dir", "dbt", "--profiles-dir", "config/ci", "--target", "dev", "--select", "connection_check", "--no-send-anonymous-usage-stats"], env, "PR connection_check build/tests")
        github.validation_current()
        print(f"Validated PR {github.number} in PR_{github.number}_BRONZE/SILVER/GOLD.")
        return 0
    except (LifecycleError, bronze.IngestionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("ERROR: PR lifecycle failed; private diagnostics suppressed. No further operations attempted.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
