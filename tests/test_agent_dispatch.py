"""Offline admission, scope, publication and workflow checks; no API/model calls."""

import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.agents import issue_dispatch as agent


ROOT = Path(__file__).resolve().parents[1]
GIT_PATH = os.environ.get('PATH', '')
BASE, HEAD, TREE, COMMIT = (character * 40 for character in "abcd")
BODY = "### Goal\nImplement $(unsafe) `task`\n### Scope\nApplication code\n### Acceptance criteria\nObservable result\n### Verification instructions\nOffline checks\n"


class FakeGitHub:
    def __init__(self, issue):
        self.issue = deepcopy(issue)
        self.permission = {"maintainer": "write", "rerunner": "maintain"}
        self.base, self.head = BASE, None
        self.open_prs, self.closed = [], []
        self.calls, self.writes = [], []
        self.fail = None

    def request(self, path, method="GET", data=None, **kwargs):
        if path == "installation/repositories?per_page=100":
            return {"total_count": 1, "repositories": [{"full_name": agent.REPOSITORY}]}
        raise AssertionError(path)

    def existing(self):
        self.head = HEAD
        self.open_prs = [{"number": 77, "state": "open", "user": {"login": "task-app[bot]"},
            "body": "<!-- claude-agent:issue:42 -->\nRefs #42", "head": {"ref": "agent/issue-42", "sha": HEAD,
            "repo": {"full_name": agent.REPOSITORY}}, "base": {"ref": "dev", "repo": {"full_name": agent.REPOSITORY}}}]

    def repo(self, path, method="GET", data=None, **kwargs):
        self.calls.append((path, method, deepcopy(data)))
        if method != "GET":
            self.writes.append((path, method, deepcopy(data)))
            if self.fail == path:
                raise agent.AgentError("Simulated GitHub publication failure.")
        if path.startswith("collaborators/"):
            name = path.split('/')[1]
            return {"permission": self.permission.get(name, "read"), "user": {"type": "User"}}
        if path == "issues/42":
            return deepcopy(self.issue)
        if path == "git/ref/heads/dev":
            return {"object": {"sha": self.base}}
        if path.startswith("pulls?state=open"):
            return deepcopy(self.open_prs)
        if path.startswith("pulls?state=closed"):
            return deepcopy(self.closed)
        if path == "git/ref/heads/agent/issue-42":
            return {"object": {"sha": self.head}} if self.head else None
        if path == f"git/commits/{BASE}":
            return {"tree": {"sha": TREE}}
        if path == "git/trees":
            return {"sha": TREE}
        if path == "git/commits":
            return {"sha": COMMIT}
        if path in {"git/refs", "git/refs/heads/agent/issue-42"}:
            self.head = data["sha"]
            for pr in self.open_prs:
                pr["head"]["sha"] = self.head
            return {"object": {"sha": self.head}}
        if path == "pulls" and method == "POST":
            self.existing()
            self.head = COMMIT
            self.open_prs[0]["head"]["sha"] = COMMIT
            self.open_prs[0]["body"] = data["body"]
            return {"number": 77}
        if path == "pulls/77":
            return deepcopy(self.open_prs[0])
        if path == "issues/77/comments":
            return {"id": 1}
        raise AssertionError((path, method))


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.issue = {"number": 42, "id": 100, "title": "Example", "body": BODY, "state": "open", "labels": [{"name": "ai"}]}
        self.event = {"action": "labeled", "label": {"name": "ai"}, "issue": deepcopy(self.issue),
            "sender": {"login": "maintainer", "type": "User"}, "repository": {"full_name": agent.REPOSITORY}}
        self.event_path = self.directory / "event.json"
        self.save_event()
        self.env = {"ENABLE_CLAUDE_AGENT": "true", "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": agent.REPOSITORY,
            "GITHUB_EVENT_NAME": "issues", "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_ACTOR": "maintainer",
            "GITHUB_TRIGGERING_ACTOR": "rerunner", "CLAUDE_AGENT_APP_SLUG": "task-app", "TOKEN_APP_SLUG": "task-app"}
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.api = FakeGitHub(self.issue)
        # A missed mock must fail rather than reach GitHub during offline tests.
        self.network_guard = patch.object(agent, 'urlopen', side_effect=AssertionError('Network forbidden in offline agent tests'))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def save_event(self):
        self.event_path.write_text(json.dumps(self.event), encoding="utf-8")

    def payload(self, path="app/products.py", text="value = 1\n"):
        return {"files": [{"path": path, "content": base64.b64encode(text.encode()).decode()}],
            "report": {"summary": "Implemented", "acceptance_criteria": "Verified example", "untested": "Snowflake and GitHub CI not run"},
            "checks": ["ingestion: passed", "tests: passed", "pip check: passed", "git diff --check: passed"]}

    def test_disabled_or_missing_gate_stops_before_any_api(self):
        for value in ("", "false", "True", "1"):
            with self.subTest(value=value), patch.dict(os.environ, {"ENABLE_CLAUDE_AGENT": value}):
                with self.assertRaises(agent.AgentError):
                    agent.make_plan(self.api)
        self.assertEqual(self.api.calls, [])

    def test_wrong_events_closed_prs_bots_and_actor_mismatch_are_rejected(self):
        for overrides in ({"action": "opened"}, {"pull_request": {}}, {"label": {"name": "other"}},
                          {"issue": {**self.issue, "state": "closed"}}, {"issue": {**self.issue, "pull_request": {}}},
                          {"sender": {"login": "maintainer", "type": "Bot"}}, {"sender": {"login": "author", "type": "User"}}):
            with self.subTest(overrides=overrides):
                event = {**self.event, **overrides}
                self.event_path.write_text(json.dumps(event), encoding="utf-8")
                with self.assertRaises(agent.AgentError):
                    agent.make_plan(self.api)
        self.save_event()
        for name in ("pull_request", "pull_request_target", "issue_comment", "workflow_dispatch"):
            with self.subTest(event=name), patch.dict(os.environ, {"GITHUB_EVENT_NAME": name}):
                with self.assertRaises(agent.AgentError):
                    agent.make_plan(self.api)
        self.assertEqual(self.api.calls, [])

    def test_live_actor_permission_required_even_with_trusted_author_and_label(self):
        self.event["issue"]["author_association"] = "OWNER"
        self.save_event()
        for name in ("maintainer", "rerunner"):
            for permission in ("read", "triage", "none"):
                with self.subTest(actor=name, permission=permission):
                    self.api.permission[name] = permission
                    with self.assertRaises(agent.AgentError):
                        agent.make_plan(self.api)
                    self.api.permission[name] = "write"
        self.assertFalse(self.api.writes)

    def test_all_three_current_permissions_are_accepted(self):
        for permission in ("write", "maintain", "admin"):
            self.api.permission["maintainer"] = permission
            plan = agent.make_plan(self.api)
            self.assertEqual(plan["base_sha"], BASE)
            self.assertEqual(plan["branch"], "agent/issue-42")

    def test_live_closed_unlabeled_edited_or_incomplete_issue_fails(self):
        for change in ({"state": "closed"}, {"labels": []}, {"title": "edited"}, {"body": "edited"}, {"pull_request": {}}):
            with self.subTest(change=change):
                self.api.issue = {**self.issue, **change}
                with self.assertRaises(agent.AgentError):
                    agent.make_plan(self.api)
        self.api.issue = deepcopy(self.issue)
        self.api.issue["body"] = self.event["issue"]["body"] = "### Goal\nOnly one field"
        self.save_event()
        with self.assertRaises(agent.AgentError):
            agent.make_plan(self.api)

    def test_reuses_exact_owned_open_pr(self):
        self.api.existing()
        unrelated = deepcopy(self.api.open_prs[0])
        unrelated['head']['ref'] = 'unrelated-issue'
        unrelated['body'] = f'https://github.com/{agent.REPOSITORY}/issues/420'
        self.api.open_prs.append(unrelated)
        plan = agent.make_plan(self.api)
        self.assertEqual((plan["pr_number"], plan["head_sha"]), (77, HEAD))

    def test_unrelated_human_wrong_base_fork_or_duplicate_pr_blocks(self):
        for mutation in ("human", "base", "fork", "marker", "duplicate", "other_branch"):
            self.api.existing()
            pr = self.api.open_prs[0]
            if mutation == "human": pr["user"]["login"] = "human"
            if mutation == "base": pr["base"]["ref"] = "main"
            if mutation == "fork": pr["head"]["repo"]["full_name"] = "other/repo"
            if mutation == "marker": pr["body"] = "Refs #42"
            if mutation == "duplicate": self.api.open_prs.append(deepcopy(pr))
            if mutation == "other_branch": pr["head"]["ref"] = "human/issue-42"
            with self.subTest(mutation=mutation), self.assertRaises(agent.AgentError):
                agent.make_plan(self.api)

    def test_orphan_branch_and_closed_attempt_require_human_recovery(self):
        for head, closed in ((HEAD, []), (None, [{"number": 77}])):
            self.api.head, self.api.closed = head, closed
            with self.assertRaises(agent.AgentError):
                agent.make_plan(self.api)

    def test_dev_head_issue_revision_or_permission_change_stops_publication(self):
        plan = agent.make_plan(self.api)
        for mutation in ("base", "issue", "permission", "head"):
            self.api = FakeGitHub(self.issue)
            if mutation == "base": self.api.base = HEAD
            if mutation == "issue": self.api.issue["body"] += "changed"
            if mutation == "permission": self.api.permission["maintainer"] = "read"
            if mutation == "head": self.api.head = HEAD
            with self.subTest(mutation=mutation), self.assertRaises(agent.AgentError):
                agent.publish(self.api, plan, self.payload())
            self.assertEqual(self.api.writes, [])

    def test_allowed_scope_and_known_controls(self):
        for name in ("app/products.py", "src/products.py", "ingestion/test_ingest_products.py", "scripts/check_products.py",
                     "dbt/models/products/new.sql", "dbt/tests/products/new.sql", "tests/test_new.py",
                     "sample_data/new.csv", "docs/products.md", "README.md"):
            self.assertTrue(agent.allowed_path(name), name)
        for name in (".github/workflows/new.yml", "AGENTS.md", "CLAUDE.md", "docs/CLAUDE.local.md", "docs/agents/new.md",
                     "config/ci/profiles.yml", "requirements.txt", "scripts/agents/new.py", "scripts/ci_pr_lifecycle.py",
                     "scripts/run_data_flow.py", "ingestion/ingest_products.py", "src/auth.py", "src/permissions/check.py",
                     "scripts/deploy.py", "dbt/macros/products_schema.sql", "tests/test_agent_dispatch.py",
                     "dbt/models/connection_check.sql", "../app/a.py", "/app/a.py", "app//a.py", "app/./a.py", "app/a\nb.py"):
            self.assertFalse(agent.allowed_path(name), name)

    def test_payload_rejects_protected_duplicate_binary_secret_and_missing_verification(self):
        invalid = [self.payload("AGENTS.md"), self.payload(text="private\x00data"), self.payload(text="sk-ant-example")]
        duplicate = self.payload(); duplicate["files"] *= 2; invalid.append(duplicate)
        oversized = self.payload(text="x" * (agent.MAX_FILE_BYTES + 1)); invalid.append(oversized)
        missing = self.payload(); missing["checks"] = []; invalid.append(missing)
        missing = self.payload(); missing["report"]["untested"] = ""; invalid.append(missing)
        secret = self.payload(); secret["report"]["summary"] = "sk-ant-example"; invalid.append(secret)
        for payload in invalid:
            with self.subTest(payload_type=payload["files"][0]["path"]), self.assertRaises(agent.AgentError):
                agent.validate_payload(payload)

    def test_openai_key_cannot_be_published_in_source_or_report(self):
        candidate = 'sk-' + 'proj-' + 'x' * 64
        report = self.payload()
        report['report']['summary'] = candidate
        for payload in (self.payload(text=candidate), report):
            with self.assertRaises(agent.AgentError):
                agent.validate_payload(payload)

    def test_fine_grained_token_and_secret_scanner_controls_are_protected(self):
        with self.assertRaises(agent.AgentError):
            agent.validate_payload(self.payload(text='github_' + 'pat_' + 'x' * 64))
        self.assertFalse(agent.allowed_path('tests/test_repository_safety.py'))

    def test_new_publication_is_app_branch_pr_dev_never_merge_or_protected_ref(self):
        plan = agent.make_plan(self.api)
        agent.publish(self.api, plan, self.payload())
        writes = self.api.writes
        self.assertEqual(writes[2][2]["ref"], "refs/heads/agent/issue-42")
        pr = writes[-1][2]
        self.assertEqual(pr["base"], "dev")
        self.assertIn("Refs #42", pr["body"])
        self.assertIn("Untested", pr["body"])
        self.assertFalse(any("merge" in path or path.endswith(("heads/dev", "heads/main")) for path, _, _ in writes))

    def test_rerun_keeps_history_and_comments_on_same_pr_without_duplicate(self):
        self.api.existing()
        plan = agent.make_plan(self.api)
        agent.publish(self.api, plan, self.payload())
        commits = [data for path, _, data in self.api.writes if path == "git/commits"]
        self.assertEqual(commits[0]["parents"], [HEAD, BASE])
        update = next(data for path, _, data in self.api.writes if path.startswith("git/refs/heads/"))
        self.assertIs(update["force"], False)
        self.assertEqual(self.api.writes[-1][0], "issues/77/comments")
        self.assertFalse(any(path == "pulls" for path, _, _ in self.api.writes))

    def test_pr_creation_failure_leaves_branch_and_retry_refuses_overwrite(self):
        plan = agent.make_plan(self.api)
        self.api.fail = "pulls"
        with self.assertRaises(agent.AgentError):
            agent.publish(self.api, plan, self.payload())
        self.assertEqual(self.api.head, COMMIT)
        self.api.fail = None
        with self.assertRaises(agent.AgentError):
            agent.make_plan(self.api)

    def test_duplicate_admissions_cannot_publish_twice_and_fresh_run_reuses_pr(self):
        first = agent.make_plan(self.api)
        duplicate = agent.make_plan(self.api)
        agent.publish(self.api, first, self.payload())
        self.assertEqual(len(self.api.open_prs), 1)
        self.api.writes.clear()
        with self.assertRaises(agent.AgentError):
            agent.publish(self.api, duplicate, self.payload())
        self.assertEqual(self.api.writes, [])
        fresh = agent.make_plan(self.api)
        agent.publish(self.api, fresh, self.payload(text='value = 2\n'))
        self.assertEqual(fresh['pr_number'], 77)
        self.assertEqual(len(self.api.open_prs), 1)
        self.assertFalse(any(path == 'pulls' for path, _, _ in self.api.writes))

    def test_issue_closes_during_publication_before_ref_update(self):
        plan = agent.make_plan(self.api)
        request = self.api.repo
        def close_after_commit(path, method='GET', data=None, **kwargs):
            result = request(path, method, data, **kwargs)
            if path == 'git/commits' and method == 'POST':
                self.api.issue['state'] = 'closed'
            return result
        with patch.object(self.api, 'repo', side_effect=close_after_commit), self.assertRaises(agent.AgentError):
            agent.publish(self.api, plan, self.payload())
        self.assertTrue(any(path == 'git/commits' for path, _, _ in self.api.writes))
        self.assertFalse(any(path.startswith('git/refs') or path == 'pulls' for path, _, _ in self.api.writes))

    def test_unrelated_bot_cannot_own_retry_even_with_canonical_branch_and_marker(self):
        self.api.existing()
        self.api.open_prs[0]['user']['login'] = 'unrelated-app[bot]'
        with self.assertRaises(agent.AgentError):
            agent.make_plan(self.api)
        self.assertEqual(self.api.writes, [])

    def test_hostile_issue_text_round_trips_as_data_and_never_becomes_git_arguments(self):
        hostile = '\"double\" \'single\'\n$(touch AGENT_SENTINEL)\n`; New-Item AGENT_SENTINEL; #\n@maintainer'
        self.issue.update(title=hostile, body=BODY + '\n' + hostile)
        self.event['issue'] = deepcopy(self.issue)
        self.api.issue = deepcopy(self.issue)
        self.save_event()
        plan = agent.make_plan(self.api)
        with self.cwd(), patch.object(agent, 'git', side_effect=[(BASE + '\n').encode(), b'']) as git, \
                patch.object(agent.subprocess, 'run', side_effect=AssertionError('Event text must not execute')):
            agent.prepare(self.api, plan)
            saved = json.loads(Path('.agent-task/issue.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['title'], hostile)
            self.assertEqual(saved['body'], self.issue['body'])
            self.assertNotIn(hostile, str(git.call_args_list))
            self.assertFalse(Path('AGENT_SENTINEL').exists())
        payload = self.payload()
        payload['report']['summary'] = hostile
        with patch.object(agent.subprocess, 'run', side_effect=AssertionError('Reports are data, not commands')):
            agent.publish(self.api, plan, payload)
        self.assertIn(hostile, self.api.writes[-1][2]['body'])

    def test_real_http_request_serializes_quotes_and_newlines_without_network(self):
        hostile = '\"quoted\"\n\'single\' $(never-execute) `shell-like`\nsecond line'
        data = {'title': hostile, 'body': hostile, 'base': 'dev', 'head': 'agent/issue-42'}
        with patch.object(agent, 'urlopen', return_value=io.BytesIO(b'{"number": 77}')) as transport:
            result = agent.GitHub('offline-app-token').repo('pulls', 'POST', data)
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + agent.REPOSITORY + '/pulls')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer offline-app-token')
        self.assertEqual(result, {'number': 77})

    def test_wrong_app_identity_or_repository_scope_cannot_publish(self):
        plan = agent.make_plan(self.api)
        with patch.dict(os.environ, {"TOKEN_APP_SLUG": "github-actions"}), self.assertRaises(agent.AgentError):
            agent.publish(self.api, plan, self.payload())
        with patch.object(self.api, "request", return_value={"total_count": 2}), self.assertRaises(agent.AgentError):
            agent.publish(self.api, plan, self.payload())
        self.assertEqual(self.api.writes, [])

    @contextmanager
    def cwd(self):
        previous = Path.cwd()
        os.chdir(self.directory)
        try:
            yield
        finally:
            os.chdir(previous)

    def test_prepare_uses_data_file_and_fixed_git_arguments(self):
        plan = agent.make_plan(self.api)
        with self.cwd(), patch.object(agent, "git", side_effect=[(BASE + '\n').encode(), b'']) as git:
            agent.prepare(self.api, plan)
            task = json.loads(Path('.agent-task/issue.json').read_text())
            self.assertEqual(task['body'], BODY)
            self.assertEqual(git.call_args_list[1].args, ('switch', '-c', 'agent/issue-42'))
            self.assertNotIn(BODY, str(git.call_args_list))

    def test_collect_requires_model_success_and_propagates_offline_failure(self):
        plan = agent.make_plan(self.api)
        with self.assertRaises(agent.AgentError):
            agent.collect(plan, self.directory / 'payload.json')
        with self.cwd(), patch.dict(os.environ, {'CLAUDE_CONCLUSION': 'success'}):
            Path('app').mkdir(); Path('app/products.py').write_bytes(b'value = 1\n')
            Path('.agent-task').mkdir(); Path('.agent-task/report.json').write_text(json.dumps(self.payload()['report']))
            with patch.object(agent, 'git', side_effect=[b'', b'app/products.py\0']), patch.object(agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
                with self.assertRaises(agent.AgentError):
                    agent.collect(plan, Path('payload.json'))
                self.assertEqual(run.call_count, 1)
                self.assertFalse(Path('payload.json').exists())

    def test_failed_action_cannot_publish_even_when_cli_result_reports_success(self):
        plan = agent.make_plan(self.api)
        with self.cwd(), patch.dict(os.environ, {'CLAUDE_CONCLUSION': 'failure'}):
            Path('.agent-task').mkdir()
            Path('.agent-task/report.json').write_text(json.dumps(self.payload()['report']))
            # Reproduce the reported conflicting signals without invoking the CLI.
            # A raw successful result never overrides the action's failure.
            Path('execution-output.json').write_text(json.dumps([
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 21},
            ]))
            with patch.object(agent, 'git') as git, patch.object(agent.subprocess, 'run') as checks:
                with self.assertRaisesRegex(agent.AgentError, 'no publication artifact'):
                    agent.collect(plan, Path('output/payload.json'))
                git.assert_not_called()
                checks.assert_not_called()
            self.assertFalse(Path('output/payload.json').exists())
            self.assertEqual(self.api.writes, [])

    def test_collect_success_protected_paths_and_symlinks(self):
        plan = agent.make_plan(self.api)
        with self.cwd(), patch.dict(os.environ, {'CLAUDE_CONCLUSION': 'success'}):
            Path('app').mkdir(); Path('app/products.py').write_bytes(b'value = 1\n')
            Path('.agent-task').mkdir(); Path('.agent-task/report.json').write_text(json.dumps(self.payload()['report']))
            with patch.object(agent, 'git', side_effect=[b'', b'app/products.py\0', b'']), patch.object(agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                agent.collect(plan, Path('output/payload.json'))
                self.assertEqual(run.call_count, len(agent.CHECKS))
                self.assertEqual(json.loads(Path('output/payload.json').read_text())['files'], self.payload()['files'])
            with patch.object(agent, 'git', side_effect=[b'.github/workflows/evil.yml\0', b'']), self.assertRaises(agent.AgentError):
                agent.collect(plan, Path('output/must-not-exist.json'))
            with patch.object(agent, 'git', side_effect=[b'app/products.py\0', b'']), patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(agent.AgentError):
                agent.collect(plan, Path('output/must-not-exist.json'))

    def test_prepare_retries_apply_owned_changes_to_dev_and_stop_on_conflicts(self):
        self.api.existing()
        plan = agent.make_plan(self.api)
        git_results = [(BASE + '\n').encode(), b'', (TREE + '\n').encode(), b'app/products.py\0', b'fixed-patch']
        with self.cwd(), patch.object(agent, 'git', side_effect=git_results), patch.object(agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as apply:
            agent.prepare(self.api, plan)
            self.assertEqual(apply.call_args.args[0], ['git', 'apply', '--index', '--whitespace=error'])
            self.assertEqual(apply.call_args.kwargs['input'], b'fixed-patch')
        with self.cwd(), patch.object(agent, 'git', side_effect=git_results), patch.object(agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), self.assertRaises(agent.AgentError):
            agent.prepare(self.api, plan)

    def test_current_plan_authorization_output_contains_no_issue_body_or_secret(self):
        output_path = self.directory / 'step-output'
        with patch.object(agent.sys, 'argv', ['issue_dispatch.py', 'authorize']), patch.dict(os.environ, {'GH_TOKEN': 'not-output', 'GITHUB_OUTPUT': str(output_path)}), patch.object(agent, 'GitHub', return_value=self.api):
            self.assertEqual(agent.main(), 0)
        output = output_path.read_text()
        self.assertTrue(output.startswith('approved=true\nplan='))
        self.assertNotIn(BODY, output)
        self.assertNotIn('not-output', output)
        self.assertEqual(json.loads(output.split('plan=', 1)[1])['branch'], 'agent/issue-42')

    def test_real_git_retry_preserves_current_dev_and_existing_issue_changes(self):
        with self.cwd(), patch.dict(os.environ, {'PATH': GIT_PATH, 'GIT_CONFIG_NOSYSTEM': '1'}):
            self.assertIsNotNone(shutil.which('git'))
            def run(*args):
                return subprocess.run(['git', *args], capture_output=True, check=True).stdout.decode().strip()
            run('init', '-b', 'dev')
            run('config', 'user.name', 'Offline test')
            run('config', 'user.email', 'offline@example.invalid')
            run('config', 'commit.gpgsign', 'false')
            Path('app').mkdir(); Path('app/products.py').write_bytes(b'value = 1\n')
            run('add', 'app/products.py'); run('commit', '-m', 'Offline base fixture')
            run('switch', '-c', 'prior-issue')
            Path('app/products.py').write_bytes(b'value = 2\n')
            run('add', 'app/products.py'); run('commit', '-m', 'Offline issue fixture')
            issue_head = run('rev-parse', 'HEAD')
            run('switch', 'dev')
            Path('docs').mkdir(); Path('docs/new.md').write_bytes(b'Current dev work\n')
            run('add', 'docs/new.md'); run('commit', '-m', 'Offline updated dev fixture')
            current_dev = run('rev-parse', 'HEAD')
            self.api.existing()
            self.api.base, self.api.head = current_dev, issue_head
            self.api.open_prs[0]['head']['sha'] = issue_head
            plan = agent.make_plan(self.api)
            agent.prepare(self.api, plan)
            self.assertEqual(run('branch', '--show-current'), 'agent/issue-42')
            self.assertEqual(run('rev-parse', 'HEAD'), current_dev)
            self.assertEqual(Path('app/products.py').read_bytes(), b'value = 2\n')
            self.assertEqual(Path('docs/new.md').read_bytes(), b'Current dev work\n')
            self.assertEqual(self.api.writes, [])

    def test_last_head_recheck_blocks_non_fast_forward(self):
        self.api.existing()
        plan = agent.make_plan(self.api)
        original = self.api.repo
        def change_head(path, method='GET', data=None, **kwargs):
            result = original(path, method, data, **kwargs)
            if path == 'git/commits' and method == 'POST':
                self.api.head = TREE
            return result
        with patch.object(self.api, 'repo', side_effect=change_head), self.assertRaises(agent.AgentError):
            agent.publish(self.api, plan, self.payload())
        self.assertFalse(any(path.startswith('git/refs') for path, _, _ in self.api.writes))

    def test_main_never_falls_back_to_default_token_and_redacts_unknown_errors(self):
        with patch.object(agent.sys, 'argv', ['issue_dispatch.py', 'publish']), patch.dict(os.environ, {'GITHUB_TOKEN': 'default-must-not-use'}), redirect_stderr(self.output):
            self.assertEqual(agent.main(), 1)
        self.assertNotIn('default-must-not-use', self.output.getvalue())
        with patch.object(agent.sys, 'argv', ['issue_dispatch.py', 'publish']), patch.dict(os.environ, {'GH_TOKEN': 'app-token'}), patch.object(agent, 'publish', side_effect=RuntimeError('secret-diagnostic')), patch.dict(os.environ, {'AGENT_PLAN': '{}', 'AGENT_PAYLOAD': str(self.event_path)}), redirect_stderr(self.output):
            self.assertEqual(agent.main(), 1)
        self.assertNotIn('secret-diagnostic', self.output.getvalue())


class AgentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / '.github/workflows/claude-developer.yml').read_text(encoding='utf-8')
        self.workflow = yaml.load(self.text, Loader=yaml.BaseLoader)
        self.jobs = self.workflow['jobs']

    def test_trigger_default_off_gate_and_secret_free_authorization(self):
        self.assertEqual(self.workflow['on'], {'issues': {'types': ['labeled']}})
        gate = self.jobs['authorize']
        for required in ("vars.ENABLE_CLAUDE_AGENT == 'true'", "github.event.label.name == 'ai'", "github.event.issue.state == 'open'", '!github.event.issue.pull_request'):
            self.assertIn(required, gate['if'])
        self.assertNotIn('secrets.', json.dumps(gate))
        self.assertTrue(self.jobs['develop']['if'].endswith("== 'true'"))
        self.assertEqual(self.jobs['develop']['needs'], 'authorize')

    def test_live_recheck_precedes_any_api_key_or_app_key_and_model_uses_read_app(self):
        steps = self.jobs['develop']['steps']
        recheck = next(i for i, s in enumerate(steps) if s.get('run', '').endswith(' recheck'))
        app = next(i for i, s in enumerate(steps) if s.get('id') == 'read-token')
        model = next(i for i, s in enumerate(steps) if s.get('id') == 'claude')
        self.assertLess(recheck, app); self.assertLess(app, model)
        self.assertEqual(steps[app]['with']['permission-contents'], 'read')
        self.assertEqual(steps[model]['with']['github_token'], '${{ steps.read-token.outputs.token }}')
        self.assertIn('anthropics/claude-code-action@', steps[model]['uses'])
        self.assertNotIn('${{ github.event.', steps[model]['with']['prompt'])

    def test_turn_workaround_preserves_other_bounds_and_failure_gates(self):
        develop = self.jobs['develop']
        model = next(step for step in develop['steps'] if step.get('id') == 'claude')
        self.assertEqual(model['uses'], 'anthropics/claude-code-action@374f254f5efaf968abfe15d399fd90972e8138b9')
        self.assertEqual(shlex.split(model['with']['claude_args']), [
            '--max-turns', '40', '--max-budget-usd', '5',
            '--tools', 'Read,Glob,Grep,Edit,Write,Bash',
            '--allowedTools', 'Read,Glob,Grep,Edit,Write,Bash',
        ])
        self.assertEqual(develop['timeout-minutes'], '30')
        for setting in ('show_full_output', 'display_report', 'classify_inline_comments'):
            self.assertEqual(model['with'][setting], 'false')
        # Collection and transfer retain the implicit success() gate. A failed
        # wrapper must not be rescued by a raw CLI result, always() or a retry.
        for step in develop['steps']:
            self.assertNotIn('if', step)
        for job in self.jobs.values():
            self.assertNotIn('continue-on-error', job)
        collector = next(step for step in develop['steps'] if step.get('run', '').endswith(' collect'))
        self.assertEqual(collector['env']['CLAUDE_CONCLUSION'], '${{ steps.claude.outputs.conclusion }}')
        self.assertEqual(self.jobs['publish']['if'], "needs.authorize.outputs.approved == 'true' && needs.develop.result == 'success'")

    def test_publisher_is_fresh_trusted_job_with_no_model_or_artifact_execution(self):
        job = self.jobs['publish']
        self.assertEqual(job['needs'], ['authorize', 'develop'])
        self.assertEqual(job['steps'][0]['with']['ref'], '${{ github.workflow_sha }}')
        self.assertFalse(any('claude-code-action' in s.get('uses', '') for s in job['steps']))
        self.assertFalse(any('pip install' in s.get('run', '') or 'unittest' in s.get('run', '') for s in job['steps']))
        mint = next(s for s in job['steps'] if s.get('id') == 'write-token')
        self.assertEqual(mint['with']['repositories'], 'snowflake-agent-team')
        self.assertEqual(mint['with']['permission-contents'], 'write')
        self.assertNotIn('permission-workflows', mint['with'])

    def test_bounds_no_database_secrets_or_environments_and_safe_shell(self):
        self.assertEqual(self.workflow['concurrency']['cancel-in-progress'], 'false')
        self.assertIn('github.event.issue.number', self.workflow['concurrency']['group'])
        self.assertEqual(self.workflow['concurrency']['queue'], 'max')
        self.assertNotIn('SNOWFLAKE', self.text)
        self.assertNotIn('environment:', self.text)
        for job in self.jobs.values():
            self.assertLessEqual(int(job['timeout-minutes']), 30)
            for step in job['steps']:
                self.assertNotIn('${{ github.event.', step.get('run', ''))
                self.assertNotIn('continue-on-error', step)
                if step.get('uses', '').startswith(('anthropics/', 'actions/create-github-app-token', 'actions/upload-artifact', 'actions/download-artifact')):
                    self.assertRegex(step['uses'], r'@[0-9a-f]{40}$')

    def test_downstream_pr_events_use_app_publication_auth_not_default_token(self):
        publisher = self.jobs['publish']['steps']
        mint = next(step for step in publisher if step.get('id') == 'write-token')
        self.assertEqual(mint['with']['client-id'], '${{ vars.CLAUDE_AGENT_APP_CLIENT_ID }}')
        self.assertEqual(mint['with']['private-key'], '${{ secrets.CLAUDE_AGENT_APP_PRIVATE_KEY }}')
        self.assertEqual(mint['with']['owner'], 'Rizvi-07')
        self.assertEqual(mint['with']['repositories'], 'snowflake-agent-team')
        self.assertNotIn('skip-token-revoke', mint['with'])
        publish = next(step for step in publisher if step.get('run', '').endswith(' publish'))
        self.assertEqual(publish['env']['GH_TOKEN'], '${{ steps.write-token.outputs.token }}')
        self.assertEqual(publish['env']['TOKEN_APP_SLUG'], '${{ steps.write-token.outputs.app-slug }}')
        self.assertNotIn('secrets.GITHUB_TOKEN', json.dumps(publish))
        self.assertNotIn('permission-workflows', mint['with'])

    def test_actual_report_program_returns_nonzero_for_failed_or_cancelled_work(self):
        script = self.jobs['report']['steps'][0]['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        self.assertEqual(self.jobs['report']['if'], 'always()')
        cases = [
            ('skipped', 'skipped', 'skipped', 0),
            ('success', 'success', 'success', 0),
            ('failure', 'skipped', 'skipped', 1),
            ('success', 'failure', 'skipped', 1),
            ('success', 'success', 'failure', 1),
            ('success', 'cancelled', 'skipped', 1),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for auth, develop, publish, expected in cases:
                with self.subTest(auth=auth, develop=develop, publish=publish):
                    env = {**os.environ, 'JOB_RESULTS': json.dumps({key: {'result': value} for key, value in zip(('authorize', 'develop', 'publish'), (auth, develop, publish))}), 'GITHUB_STEP_SUMMARY': str(Path(directory) / 'summary.md')}
                    completed = subprocess.run([agent.sys.executable, '-c', script], env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(completed.returncode, expected, completed.stderr)
                    self.assertIn('Claude issue developer:', Path(env['GITHUB_STEP_SUMMARY']).read_text())


if __name__ == '__main__':
    unittest.main()
