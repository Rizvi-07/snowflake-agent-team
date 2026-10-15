"""Check what Git could include, without displaying credential contents."""

from pathlib import Path, PurePosixPath
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)

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


if __name__ == "__main__":
    unittest.main()
