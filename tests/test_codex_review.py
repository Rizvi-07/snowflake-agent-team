"""Offline trusted-review admission, Git snapshots and actual publisher program.

Set CODEX_REVIEW_NODE to a local Node executable if Node is not on PATH.
No API/model/database calls. Temporary repositories contain no production code.
"""
from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import yaml

from scripts.agents import review_pr as review
from scripts.agents import issue_dispatch as developer

ROOT = Path(__file__).resolve().parents[1]
BASE, HEAD = 'a' * 40, 'b' * 40
HUMAN = {'login': 'maintainer', 'id': 7, 'type': 'User'}
BOT = {'login': 'task-app[bot]', 'id': 17, 'type': 'Bot'}


class FakeGitHub:
    def __init__(self):
        self.users = {user['login']: deepcopy(user) for user in (HUMAN, BOT)}
        self.permissions = {'maintainer': 'write'}
        self.pr = {'number': 42, 'state': 'open', 'user': deepcopy(HUMAN), 'title': 'Task $(never-run)',
                   'body': '`never execute`', 'head': {'sha': HEAD, 'ref': 'arbitrary-branch', 'repo': {'full_name': review.REPOSITORY}},
                   'base': {'sha': BASE, 'ref': 'dev', 'repo': {'full_name': review.REPOSITORY}}}
        self.calls = []

    def request(self, path, method='GET', **kwargs):
        self.calls.append((path, method))
        assert method == 'GET'
        from urllib.parse import unquote
        return deepcopy(self.users[unquote(path.removeprefix('users/'))])

    def repo(self, path, method='GET', **kwargs):
        self.calls.append((path, method))
        assert method == 'GET'
        if path == 'pulls/42':
            return deepcopy(self.pr)
        if path.startswith('collaborators/'):
            return {'permission': self.permissions.get(path.split('/')[1], 'read')}
        raise AssertionError(path)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.api = FakeGitHub()
        self.network_guard = patch.object(developer, 'urlopen', side_effect=AssertionError('Network forbidden in offline reviewer tests'))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.event = {'action': 'opened', 'repository': {'full_name': review.REPOSITORY},
                      'pull_request': deepcopy(self.api.pr), 'sender': deepcopy(HUMAN)}
        self.env = {'ENABLE_CODEX_REVIEW': 'true', 'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': review.REPOSITORY,
                    'GITHUB_EVENT_NAME': 'pull_request_target', 'GITHUB_EVENT_PATH': str(self.folder / 'event.json'),
                    'GITHUB_ACTOR': HUMAN['login'], 'GITHUB_TRIGGERING_ACTOR': HUMAN['login'],
                    'GITHUB_RUN_ID': '100', 'GITHUB_RUN_ATTEMPT': '1', 'GH_TOKEN': 'dummy-not-secret',
                    'RUNNER_TEMP': str(self.folder), 'GITHUB_OUTPUT': str(self.folder / 'outputs')}
        self.write_event()

    def write_event(self):
        Path(self.env['GITHUB_EVENT_PATH']).write_text(json.dumps(self.event), encoding='utf-8')

    def plan(self):
        self.write_event()
        return review.make_plan(self.api, self.env)

    def test_exact_head_read_only_and_both_protected_bases(self):
        for base in ('dev', 'main'):
            self.api.pr['base']['ref'] = self.event['pull_request']['base']['ref'] = base
            plan = self.plan()
            self.assertEqual(plan['head_sha'], HEAD)
            self.assertEqual(plan['allowed_bot'], '')
        self.assertTrue(all(method == 'GET' for _, method in self.api.calls))

    def test_reject_disabled_unexpected_repository_event_action_or_closed(self):
        for key, bad in [('ENABLE_CODEX_REVIEW', ''), ('ENABLE_CODEX_REVIEW', 'True'), ('GITHUB_ACTIONS', 'false'),
                         ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REPOSITORY', 'other/repo')]:
            with self.subTest(key=key, bad=bad), patch.dict(self.env, {key: bad}), self.assertRaises(review.AgentError):
                self.plan()
        for field, bad in [('action', 'closed'), ('action', 'edited')]:
            self.event[field] = bad
            with self.assertRaises(review.AgentError): self.plan()
        self.event['action'] = 'opened'
        self.event['pull_request']['state'] = 'closed'
        with self.assertRaises(review.AgentError): self.plan()

    def test_forks_rejected_before_identity_lookup(self):
        self.event['pull_request']['head']['repo']['full_name'] = 'fork/repo'
        with self.assertRaises(review.AgentError): self.plan()
        self.assertEqual(self.api.calls, [])

    def test_disabled_flag_variants_never_query_api(self):
        for value in ('', 'false', 'True', '1', ' true', 'true '):
            with self.subTest(value=value), patch.dict(self.env, {'ENABLE_CODEX_REVIEW': value}), self.assertRaises(review.AgentError):
                self.plan()
        self.assertEqual(self.api.calls, [])

    def test_current_human_permission_matrix(self):
        for permission in ('write', 'maintain', 'admin', 'read', 'triage', 'none'):
            self.api.permissions['maintainer'] = permission
            with self.subTest(permission=permission):
                if permission in ('write', 'maintain', 'admin'):
                    self.assertEqual(self.plan()['actors']['maintainer'], HUMAN)
                else:
                    with self.assertRaises(review.AgentError): self.plan()

    def test_unrelated_bot_is_not_authorized_by_branch_or_write_permission(self):
        stranger = {'login': 'unrelated-app[bot]', 'type': 'Bot', 'id': 18}
        self.api.users[stranger['login']] = stranger
        self.api.permissions[stranger['login']] = 'admin'
        self.env.update(CLAUDE_AGENT_APP_SLUG='task-app', CLAUDE_AGENT_APP_BOT_ID='17')
        self.api.pr['head']['ref'] = 'agent/issue-42'
        self.api.pr['user'] = deepcopy(stranger)
        with self.assertRaises(review.AgentError): self.plan()
        self.api.pr['user'] = deepcopy(HUMAN)
        self.event['sender'] = deepcopy(stranger)
        self.env.update(GITHUB_ACTOR=stranger['login'], GITHUB_TRIGGERING_ACTOR=stranger['login'])
        with self.assertRaises(review.AgentError): self.plan()

    def test_shell_like_sha_is_rejected_before_any_lookup_or_git_fetch(self):
        self.event['pull_request']['head']['sha'] = '--upload-pack="touch REVIEW_SENTINEL"'
        with patch.object(review, 'git', side_effect=AssertionError('Invalid SHA must not reach Git')), self.assertRaises(review.AgentError):
            self.plan()
        self.assertEqual(self.api.calls, [])

    def test_live_fork_closed_head_base_or_base_branch_changes_rejected(self):
        original = deepcopy(self.api.pr)
        for field in ('closed', 'head', 'base', 'branch', 'fork'):
            self.api.pr = deepcopy(original)
            if field == 'closed': self.api.pr['state'] = 'closed'
            elif field in ('head', 'base'): self.api.pr[field]['sha'] = 'c' * 40
            elif field == 'branch': self.api.pr['base']['ref'] = 'main'
            else: self.api.pr['head']['repo']['full_name'] = 'fork/repo'
            with self.subTest(field=field), self.assertRaises(review.AgentError): self.plan()

    def test_current_permission_required_even_with_trusted_author_or_branch(self):
        self.api.pr['head']['ref'] = 'agent/issue-42'
        self.api.permissions['maintainer'] = 'read'
        with self.assertRaises(review.AgentError): self.plan()
        self.api.permissions['maintainer'] = 'admin'
        self.api.users['rerunner'] = {'login': 'rerunner', 'id': 8, 'type': 'User'}
        self.env['GITHUB_TRIGGERING_ACTOR'] = 'rerunner'
        with self.assertRaises(review.AgentError): self.plan()
        self.api.permissions['rerunner'] = 'maintain'
        self.assertEqual(self.plan()['actors']['rerunner']['id'], 8)

    def test_author_also_requires_trust_and_sender_id_must_match(self):
        self.api.pr['user'] = deepcopy(BOT)
        with self.assertRaises(review.AgentError): self.plan()
        self.api.pr['user'] = deepcopy(HUMAN)
        self.event['sender']['id'] = 99
        with self.assertRaises(review.AgentError): self.plan()

    def test_only_explicit_app_login_and_numeric_id_are_allowed(self):
        self.env.update(CLAUDE_AGENT_APP_SLUG='task-app', CLAUDE_AGENT_APP_BOT_ID='17',
                        GITHUB_ACTOR=BOT['login'], GITHUB_TRIGGERING_ACTOR=BOT['login'])
        self.event['sender'] = deepcopy(BOT)
        self.api.pr['user'] = deepcopy(BOT)
        self.assertEqual(self.plan()['allowed_bot'], BOT['login'])
        for change in ({'CLAUDE_AGENT_APP_BOT_ID': '18'}, {'CLAUDE_AGENT_APP_BOT_ID': ''}, {'CLAUDE_AGENT_APP_SLUG': '*'}):
            with patch.dict(self.env, change), self.assertRaises(review.AgentError): self.plan()
        self.api.users[BOT['login']]['type'] = 'User'
        with self.assertRaises(review.AgentError): self.plan()

    def test_recheck_rejects_revoked_permission_and_redacts_unexpected_errors(self):
        plan = self.plan()
        self.api.permissions['maintainer'] = 'read'
        with self.assertRaises(review.AgentError): review.recheck(self.api, self.env, plan)
        capture = io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(review.sys, 'argv', ['review_pr.py', 'authorize']), \
                patch.object(review, 'make_plan', side_effect=RuntimeError('credential-material')), redirect_stderr(capture):
            self.assertEqual(review.main(), 1)
        self.assertNotIn('credential-material', capture.getvalue())


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git_run('init', '--quiet')
        self.git_run('config', 'user.name', 'Offline test')
        self.git_run('config', 'user.email', 'offline@example.invalid')
        self.write('AGENTS.md', 'TRUSTED ROOT\n')
        self.write('app/AGENTS.md', 'TRUSTED NESTED\n')
        self.write('docs/agents/reviewer-instructions.md', 'TRUSTED REVIEW\n')
        self.write('.github/codex/review-config.toml', (ROOT / '.github/codex/review-config.toml').read_text())
        self.write('.github/codex/review-schema.json', (ROOT / '.github/codex/review-schema.json').read_text())
        self.write('app/value.py', 'value = 1\n')
        self.commit()
        self.base = self.git_run('rev-parse', 'HEAD').strip()
        self.write('AGENTS.md', 'UNTRUSTED ROOT override\n')
        self.write('app/AGENTS.md', 'UNTRUSTED NESTED override\n')
        self.write('app/value.py', "raise RuntimeError('MUST NOT EXECUTE')\n")
        self.write('.codex/config.toml', 'UNTRUSTED CONFIG\n')
        self.commit()
        self.head = self.git_run('rev-parse', 'HEAD').strip()
        self.plan = {'head_sha': self.head, 'base_sha': self.base}

    def git_run(self, *args):
        result = subprocess.run(['git', *args], cwd=self.root, capture_output=True, text=True, check=True)
        return result.stdout

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')

    def commit(self):
        self.git_run('add', '.')
        self.git_run('commit', '--quiet', '-m', 'Offline fixture')

    def test_actual_git_snapshots_use_only_base_guidance_and_never_checkout_head(self):
        self.git_run('checkout', '--quiet', self.base)
        material, _ = review.snapshots(self.root, self.plan)
        self.assertEqual(self.git_run('rev-parse', 'HEAD').strip(), self.base)
        self.assertEqual(material['head_sha'], self.head)
        self.assertEqual(material['trusted_base_instructions']['app/AGENTS.md']['text'], 'TRUSTED NESTED\n')
        self.assertNotIn('UNTRUSTED', json.dumps(material['trusted_base_instructions']))
        self.assertIn('UNTRUSTED CONFIG', json.dumps(material['untrusted_changed_files']))
        self.assertIn('MUST NOT EXECUTE', json.dumps(material))

    def test_binary_large_input_and_credentials_fail_closed(self):
        for value in ('a\0b', 'x' * 64001, 'sk-' + 'a' * 25):
            self.write('app/value.py', value)
            self.commit()
            plan = {**self.plan, 'head_sha': self.git_run('rev-parse', 'HEAD').strip()}
            with self.subTest(value=value[:10]), self.assertRaises(review.AgentError): review.snapshots(self.root, plan)
        with patch.object(review, 'MAX_FILES', 1), self.assertRaises(review.AgentError): review.snapshots(self.root, self.plan)
        with patch.object(review, 'MAX_INPUT_BYTES', 1), self.assertRaises(review.AgentError): review.snapshots(self.root, self.plan)

    def test_tracked_sensitive_paths_are_withheld(self):
        self.write('config/local/profiles.yml', 'not-a-secret: fixture\n')
        self.commit()
        plan = {**self.plan, 'head_sha': self.git_run('rev-parse', 'HEAD').strip()}
        with self.assertRaises(review.AgentError): review.snapshots(self.root, plan)

    def test_prepare_rechecks_and_writes_fresh_config_outside_checkout(self):
        api = FakeGitHub()
        api.pr['number'] = 42
        hostile = '\"double\" \'single\'\n$(touch REVIEW_SENTINEL)\n`; New-Item REVIEW_SENTINEL; #'
        api.pr.update(title=hostile, body=hostile)
        env = {'RUNNER_TEMP': str(self.root.parent / (self.root.name + '-runner')), 'GH_TOKEN': 'mock'}
        folder = Path(env['RUNNER_TEMP'])
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        original = review.git
        def read_only_git(root, *args, **kwargs):
            if args[0] == 'fetch': return b''
            return original(root, *args, **kwargs)
        with patch.object(review, 'recheck') as check, patch.object(review, 'git', side_effect=read_only_git) as git:
            review.prepare(api, env, self.root, {**self.plan, 'number': 42})
        self.assertEqual(check.call_count, 2)
        config = tomllib.loads((folder / 'codex-advisory/home/config.toml').read_text())
        self.assertFalse(config['features']['shell_tool'])
        self.assertEqual(list((folder / 'codex-advisory/work').iterdir()), [])
        prompt = (folder / 'codex-advisory/prompt.txt').read_text(encoding='utf-8')
        self.assertIn(self.head, prompt)
        material = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(material['untrusted_pr_description'], {'title': hostile, 'body': hostile})
        self.assertNotIn(hostile, str(git.call_args_list))
        self.assertFalse((self.root / 'REVIEW_SENTINEL').exists())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / '.github/workflows/codex-review.yml').read_text(encoding='utf-8')
        self.flow = yaml.load(self.raw, Loader=yaml.BaseLoader)
        self.jobs = self.flow['jobs']

    def test_trusted_event_same_repo_gate_and_exact_read_permissions(self):
        self.assertEqual(self.flow['on'], {'pull_request_target': {'branches': ['dev', 'main'], 'types': ['opened', 'reopened', 'synchronize']}})
        self.assertEqual(self.flow['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        self.assertIn("vars.ENABLE_CODEX_REVIEW == 'true'", self.jobs['authorize']['if'])
        self.assertIn('head.repo.full_name == github.repository', self.jobs['authorize']['if'])
        self.assertNotIn('secrets.', json.dumps(self.jobs['authorize']))
        for job in (self.jobs['authorize'], self.jobs['review']):
            for step in job['steps']:
                if step.get('uses', '').startswith('actions/checkout@'):
                    self.assertEqual(step['with']['ref'], '${{ github.workflow_sha }}')
                    self.assertEqual(step['with']['persist-credentials'], 'false')

    def test_model_is_last_readonly_with_privilege_drop_and_no_command_tools(self):
        steps = self.jobs['review']['steps']
        model = steps[-1]
        self.assertRegex(model['uses'], r'^openai/codex-action@[0-9a-f]{40}$')
        self.assertEqual(model['with']['permission-profile'], ':read-only')
        self.assertEqual(model['with']['safety-strategy'], 'drop-sudo')
        self.assertEqual(model['with']['allow-bots'], 'false')
        self.assertIn('.allowed_bot', model['with']['allow-bot-users'])
        self.assertNotIn('allow-users', model['with'])
        prepare = next(i for i, step in enumerate(steps) if step.get('run', '').endswith(' prepare'))
        first_secret = next(i for i, step in enumerate(steps) if 'secrets.' in json.dumps(step))
        self.assertLess(prepare, first_secret)
        config = tomllib.loads((ROOT / '.github/codex/review-config.toml').read_text())
        for feature in ('shell_tool', 'apps', 'goals', 'skill_mcp_dependency_install'):
            self.assertFalse(config['features'][feature])
        self.assertEqual(config['web_search'], 'disabled')
        self.assertEqual(config['approval_policy'], 'never')

    def test_publisher_does_not_checkout_or_execute_repository_or_model_code(self):
        publisher = self.jobs['publish']
        self.assertEqual(publisher['permissions'], {'pull-requests': 'write'})
        self.assertEqual(publisher['if'], "always() && needs.authorize.outputs.approved == 'true'")
        self.assertEqual(len(publisher['steps']), 1)
        step = publisher['steps'][0]
        self.assertTrue(step['uses'].startswith('actions/github-script@'))
        for forbidden in ('eval(', 'new Function', 'require(', 'exec(', 'spawn(', 'secrets.', 'createReview', 'merge('):
            self.assertNotIn(forbidden, json.dumps(step))
        self.assertNotIn('${{', step['with']['script'])

    def test_bounds_no_database_deployment_or_dependency_install_and_controls_protected(self):
        self.assertEqual(self.flow['concurrency']['cancel-in-progress'], 'false')
        for job in self.jobs.values():
            self.assertLessEqual(int(job['timeout-minutes']), 20)
        for forbidden in ('SNOWFLAKE', 'environment:', 'pip install', 'dbt ', 'ANTHROPIC', 'continue-on-error'):
            self.assertNotIn(forbidden, self.raw)
        self.assertIn('tests/test_codex_review.py', developer.PROTECTED)
        self.assertTrue(developer.allowed_path('tests/test_app.py'))
        self.assertFalse(developer.allowed_path('scripts/agents/review_pr.py'))

    def test_actual_missing_secret_program_fails_without_echoing_environment(self):
        step = next(step for step in self.jobs['review']['steps'] if 'OPENAI_API_KEY' in step.get('env', {}))
        script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for value, expected in (('', 1), ('   ', 1), ('dummy-offline-value', 0)):
            env = {**os.environ, 'OPENAI_API_KEY': value}
            result = subprocess.run([review.sys.executable, '-c', script], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected)
            self.assertNotIn('dummy-offline-value', result.stdout + result.stderr)
            if expected: self.assertIn('OPENAI_API_KEY is missing', result.stderr)


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get('CODEX_REVIEW_NODE') or shutil.which('node')
        if not cls.node:
            raise unittest.SkipTest('Node unavailable; set CODEX_REVIEW_NODE to exercise the actual publisher JavaScript.')
        workflow = yaml.load((ROOT / '.github/workflows/codex-review.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        cls.script = workflow['jobs']['publish']['steps'][0]['with']['script']

    def run_publisher(self, *, output=None, raw=None, job='success', changes=None, plan_changes=None, env_changes=None):
        plan = {'number': 42, 'head_sha': HEAD, 'base_sha': BASE, 'base_ref': 'dev', 'run_id': '100',
                'run_attempt': '1', 'actors': {'maintainer': HUMAN}, 'app': None}
        if plan_changes: plan.update(plan_changes)
        pr = FakeGitHub().pr
        data = {'pr': pr, 'second_pr': pr, 'users': {'maintainer': HUMAN}, 'permission': 'write', 'comments': [],
                'files': [{'filename': 'app/products.py'}]}
        if changes: data.update(changes)
        result = output or {'status': 'completed', 'head_sha': HEAD, 'summary': 'Static review completed.', 'findings': [], 'limitations': ['CI not observed.']}
        harness = """
        const input = JSON.parse(process.env.TEST_DATA);
        let reads = 0;
        const writes = [], errors = [], notices = [];
        const core = {notice: message => notices.push(message), setFailed: message => errors.push(message)};
        const github = {rest: {
          pulls: {get: async () => ({data: ++reads > 1 ? input.second_pr : input.pr}), listFiles: async () => ({data: input.files})},
          users: {getByUsername: async ({username}) => ({data: input.users[username]})},
          repos: {getCollaboratorPermissionLevel: async () => ({data: {permission: input.permission}})},
          issues: {listComments: async () => ({data: input.comments}),
            createComment: async data => writes.push({method: 'create', ...data}),
            updateComment: async data => writes.push({method: 'update', ...data})}
        }};
        (async () => { try { await (async () => { SCRIPT })(); } catch { errors.push('safe publisher exception'); }
          console.log(JSON.stringify({writes, errors, notices, reads})); })();
        """.replace('SCRIPT', self.script)
        env = {**os.environ, 'ENABLE_CODEX_REVIEW': 'true', 'REVIEW_PLAN': json.dumps(plan),
               'REVIEW_RESULT': raw if raw is not None else json.dumps(result), 'REVIEW_JOB_RESULT': job, 'TEST_DATA': json.dumps(data)}
        if env_changes: env.update(env_changes)
        completed = subprocess.run([self.node, '-e', harness], env=env, capture_output=True, encoding='utf-8', check=True)
        return json.loads(completed.stdout)

    def finding(self):
        return {'severity': 'P1', 'file': 'app/products.py', 'side': 'head', 'line': 4,
                'explanation': 'Wrong target on apply.', 'recommendation': 'Validate the destination before loading.'}

    def result(self, **changes):
        return {'status': 'completed', 'head_sha': HEAD, 'summary': 'Review completed.', 'findings': [self.finding()], 'limitations': [], **changes}

    def test_no_findings_and_actual_findings_both_record_exact_head(self):
        clean = self.run_publisher()
        self.assertFalse(clean['errors'])
        self.assertIn('No actionable findings.', clean['writes'][0]['body'])
        finding = self.run_publisher(output=self.result())
        body = finding['writes'][0]['body']
        for required in (HEAD, 'P1', 'app/products.py', 'line 4', 'Wrong target', 'Recommendation:'):
            self.assertIn(required, body)
        self.assertFalse(finding['errors'])

    def test_failed_cancelled_missing_or_invalid_output_never_claims_clean(self):
        cases = [{'job': 'failure'}, {'job': 'cancelled'}, {'job': 'skipped'}, {'raw': ''}, {'raw': 'not json'}, {'raw': 'x' * 32001},
                 {'output': self.result(head_sha=BASE)}, {'output': self.result(status='failed', findings=[])},
                 {'output': self.result(extra='forbidden')}, {'output': self.result(findings=[self.finding()] * 11)},
                 {'output': self.result(summary='sk-' + 'a' * 25)}]
        cases.append({'output': self.result(summary='github_' + 'pat_' + 'x' * 64)})
        for case in cases:
            with self.subTest(case=list(case)):
                outcome = self.run_publisher(**case)
                self.assertIn('Review failed.', outcome['writes'][0]['body'])
                self.assertNotIn('No actionable findings', outcome['writes'][0]['body'])
                self.assertTrue(outcome['errors'])

    def test_bad_finding_shapes_paths_lines_and_severity_rejected(self):
        for changes in ({'severity': 'low'}, {'file': 'unrelated.py'}, {'line': 0}, {'line': 1.5}, {'line': 1000001},
                        {'side': 'unknown'}, {'recommendation': ''}, {'extra': 'x'}, {'explanation': 'bad\x00control'}):
            with self.subTest(changes=changes):
                result = self.run_publisher(output=self.result(findings=[{**self.finding(), **changes}]))
                self.assertTrue(result['errors'])
                self.assertIn('Review failed.', result['writes'][0]['body'])

    def test_invalid_review_containers_and_boolean_line_numbers_are_failures(self):
        for value in (None, [], True, {'status': 'completed'}, self.result(findings=None),
                      self.result(limitations='no tests'), self.result(summary={'command': 'never execute'}),
                      self.result(findings=[{**self.finding(), 'line': True}])):
            with self.subTest(value=type(value).__name__):
                outcome = self.run_publisher(raw=json.dumps(value))
                self.assertTrue(outcome['errors'])
                self.assertIn('Review failed.', outcome['writes'][0]['body'])
                self.assertNotIn('No actionable findings', outcome['writes'][0]['body'])

    def test_model_quotes_newlines_and_commands_are_published_as_text_only(self):
        with tempfile.TemporaryDirectory() as folder:
            sentinel = Path(folder) / 'MODEL_MUST_NOT_EXECUTE'
            hostile = '\"quoted\" \'single\'\n$(touch MODEL_MUST_NOT_EXECUTE)\n' + \
                "require('fs').writeFileSync(process.env.TEST_SENTINEL, 'executed');"
            output = self.result(summary=hostile, findings=[{**self.finding(), 'recommendation': hostile}])
            outcome = self.run_publisher(output=output, env_changes={'TEST_SENTINEL': str(sentinel)})
            self.assertFalse(outcome['errors'])
            self.assertEqual(len(outcome['writes']), 1)
            self.assertIn('MODEL\\_MUST\\_NOT\\_EXECUTE', outcome['writes'][0]['body'])
            self.assertFalse(sentinel.exists())

    def test_closed_fork_and_stale_heads_cannot_publish_even_on_second_check(self):
        for field in ('closed', 'head', 'fork', 'base'):
            pr = deepcopy(FakeGitHub().pr)
            if field == 'closed': pr['state'] = 'closed'
            elif field in ('head', 'base'): pr[field]['sha'] = 'c' * 40
            else: pr['head']['repo']['full_name'] = 'fork/repo'
            for key in ('pr', 'second_pr'):
                with self.subTest(field=field, check=key):
                    self.assertEqual(self.run_publisher(changes={key: pr})['writes'], [])

    def comment(self, *, author='github-actions[bot]', run='99', attempt='1'):
        return {'id': 9, 'user': {'login': author, 'type': 'Bot'},
                'body': '<!-- codex-advisory-review:v1 -->\n' + f'<!-- head:{BASE} run:{run} attempt:{attempt} -->'}

    def test_update_one_owned_comment_ignore_foreign_and_block_duplicates_or_newer(self):
        self.assertEqual(self.run_publisher(changes={'comments': [self.comment()]})['writes'][0]['method'], 'update')
        self.assertEqual(self.run_publisher(changes={'comments': [self.comment(author='other[bot]')]})['writes'][0]['method'], 'create')
        for comments in ([self.comment(), self.comment()], [self.comment(run='101')], [self.comment(run='100', attempt='2')]):
            self.assertEqual(self.run_publisher(changes={'comments': comments})['writes'], [])

    def test_permission_revocation_and_markdown_injection(self):
        self.assertEqual(self.run_publisher(changes={'permission': 'read'})['writes'], [])
        result = self.run_publisher(output=self.result(summary='<img src="bad"> @maintainer [click](https://bad.invalid)'))
        body = result['writes'][0]['body']
        self.assertNotIn('<img', body)
        self.assertNotIn('@maintainer', body)
        self.assertIn('@\u200bmaintainer', body)
        self.assertIn('\\[click\\]', body)

    def test_publisher_rechecks_exact_app_id_and_gate(self):
        plan = {'actors': {BOT['login']: BOT}, 'app': BOT}
        users = {'users': {BOT['login']: BOT}}
        config = {'CLAUDE_AGENT_APP_SLUG': 'task-app', 'CLAUDE_AGENT_APP_BOT_ID': '17'}
        admitted = self.run_publisher(plan_changes=plan, changes=users, env_changes=config)
        self.assertEqual(len(admitted['writes']), 1)
        for wrong in ({'CLAUDE_AGENT_APP_BOT_ID': '18'}, {'CLAUDE_AGENT_APP_SLUG': '*'}, {'ENABLE_CODEX_REVIEW': 'false'}):
            outcome = self.run_publisher(plan_changes=plan, changes=users, env_changes={**config, **wrong})
            self.assertEqual(outcome['writes'], [])

    def test_render_expansion_limit_is_failure_and_never_unbounded(self):
        finding = {**self.finding(), 'explanation': '<' * 1900, 'recommendation': '<' * 900}
        outcome = self.run_publisher(output=self.result(findings=[finding] * 9))
        self.assertTrue(outcome['errors'])
        self.assertLess(len(outcome['writes'][0]['body'].encode()), 60000)
        self.assertIn('Review failed.', outcome['writes'][0]['body'])


if __name__ == '__main__':
    unittest.main()
