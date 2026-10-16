"""Check what Git could include, without displaying credential contents."""

from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.agents.issue_dispatch import SECRET_VALUE_PATTERN


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', check=False)

    def candidate_files(self):
        result = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
        self.assertEqual(result.returncode, 0)
        return [name for name in result.stdout.split("\0") if name]

    def test_private_configuration_keys_and_generated_artifacts_are_ignored(self):
        for path in (
            "config/local/profiles.yml", ".env", ".env.dev", "review-key.p8", "review-key.pem",
            "dbt/target/manifest.json", "dbt/logs/dbt.log", "logs/dbt.log",
            ".venv/pyvenv.cfg", "ingestion/__pycache__/ingest_products.pyc",
            ".pytest_cache/README.md",
            ".agent-task/issue.json", ".agent-task/report.json",
            ".agent-control/scripts/agents/issue_dispatch.py", ".claude/settings.local.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.git("check-ignore", "--quiet", "--", path).returncode, 0)

    def test_git_candidates_contain_no_private_or_generated_paths(self):
        for name in self.candidate_files():
            path = PurePosixPath(name)
            with self.subTest(path=name):
                self.assertFalse(name.startswith(("config/local/", ".venv/", "dbt/target/", "dbt/logs/", "dbt/dbt_packages/", "logs/", "target/")))
                self.assertNotIn("__pycache__", path.parts)
                self.assertNotIn(".pytest_cache", path.parts)
                self.assertNotIn(path.suffix.lower(), (".p8", ".pem", ".key", ".der", ".pyc"))
                self.assertFalse(path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example")

    def test_git_candidates_contain_no_armored_private_key_blocks(self):
        pattern = re.compile(rb"(?m)^-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----\s*$")
        for name in self.candidate_files():
            path = ROOT / name
            if path.is_file():
                self.assertIsNone(pattern.search(path.read_bytes()), f"Private key material in Git candidate: {name}")

    def test_git_candidates_contain_no_recognizable_api_tokens(self):
        indexed = set(self.git('ls-files', '-z').stdout.split('\0'))
        for name in self.candidate_files():
            path = ROOT / name
            texts = []
            if path.is_file():
                texts.append(('working tree', path.read_bytes().decode('utf-8', errors='replace')))
            if name in indexed:
                result = self.git('show', ':' + name)
                self.assertEqual(result.returncode, 0, 'Could not inspect indexed file: ' + name)
                texts.append(('index', result.stdout))
            for location, text in texts:
                match = SECRET_VALUE_PATTERN.search(text)
                # Do not pass the match to assertIsNone: that prints token values.
                self.assertFalse(bool(match), f"Potential API token in {location}: {name}")

    def test_indexed_token_cannot_be_hidden_by_clean_working_copy(self):
        with tempfile.TemporaryDirectory() as directory, patch(__name__ + '.ROOT', Path(directory)):
            self.assertEqual(self.git('init', '--quiet').returncode, 0)
            path = Path(directory) / 'example.txt'
            token = 'sk-' + 'proj-' + 'x' * 64
            path.write_text(token, encoding='utf-8')
            self.assertEqual(self.git('add', 'example.txt').returncode, 0)
            path.write_text('Public replacement text\n', encoding='utf-8')
            with self.assertRaises(AssertionError) as error:
                self.test_git_candidates_contain_no_recognizable_api_tokens()
            self.assertIn('index: example.txt', str(error.exception))
            self.assertNotIn(token, str(error.exception))

    def test_token_scan_recognizes_supported_formats_without_flagging_variable_names(self):
        candidates = ['sk-' + 'proj-' + 'x' * 64, 'sk-' + 'ant-api03-' + 'x' * 64,
                      'gh' + 's_' + 'x' * 40, 'github_' + 'pat_' + 'x' * 64]
        self.assertTrue(all(SECRET_VALUE_PATTERN.search(value) for value in candidates))
        self.assertIsNone(SECRET_VALUE_PATTERN.search('OPENAI_API_KEY ANTHROPIC_API_KEY github.token'))


if __name__ == "__main__":
    unittest.main()
